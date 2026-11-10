"""Read crawled parts_catalog data: code lookup, shared vehicles and alternative parts.

Every row is a crawled source assertion. Nothing here upgrades fitment to VERIFIED or
claims interchangeability; alternatives carry the evidence that produced them and the
warehouse matches carry their own stock, exactly as for any other code search.
"""

from __future__ import annotations

from typing import Any

import psycopg

from automotive.codes import normalize_code

WARNING = ("Crawled catalog assertions (RockAuto, MANN, NGK, CatCar, imports); "
           "verify fitment before selling. Not reviewed evidence.")


def _brand_norm(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(char for char in value.upper() if char.isalnum()) or None


class PartsCatalogService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def _parts(self, conn, normalized: str, brand: str | None) -> list[dict]:
        brand_norm = _brand_norm(brand)
        return conn.execute(
            """SELECT p.id, p.brand, p.number, p.normalized, p.name, p.kind, p.first_source
               FROM parts_catalog.parts p
               WHERE p.normalized = %s AND (%s::text IS NULL OR p.brand_norm = %s)
               ORDER BY p.brand LIMIT 20""", (normalized, brand_norm, brand_norm)).fetchall()

    @staticmethod
    def _public(part: dict) -> dict:
        return {key: part[key] for key in ("brand", "number", "normalized", "name", "kind")}

    def _vehicles(self, conn, ids: list[int], limit: int) -> list[dict]:
        return conn.execute(
            """SELECT v.make, v.model, v.year_from, v.year_to, v.engine, v.market, v.label,
                      NULLIF(v.model_code, '') AS model_code,
                      NULLIF(v.engine_code, '') AS engine_code, v.displacement_cc,
                      v.power_kw, NULLIF(v.fuel, '') AS fuel,
                      NULLIF(v.transmission, '') AS transmission,
                      NULLIF(v.drive, '') AS drive, f.source, f.position, f.note
               FROM parts_catalog.part_fitments f
               JOIN parts_catalog.vehicles v ON v.id = f.vehicle_id
               WHERE f.part_id = ANY(%s)
               ORDER BY v.make, v.model, v.year_from NULLS LAST, v.label LIMIT %s""",
            (ids, limit)).fetchall()

    def _cross_refs(self, conn, ids: list[int], limit: int) -> list[dict]:
        """One row per (other part, relation, source); direction is 'both' when a source
        published the link from each end."""
        merged: dict[tuple, dict] = {}
        for row in self._cross_ref_rows(conn, ids, limit):
            key = (row["id"], row["relation"], row["source"])
            if key in merged and merged[key]["direction"] != row["direction"]:
                merged[key]["direction"] = "both"
            merged.setdefault(key, dict(row))
        return list(merged.values())

    def _cross_ref_rows(self, conn, ids: list[int], limit: int) -> list[dict]:
        return conn.execute(
            """SELECT * FROM (
                   SELECT p.id, p.brand, p.number, p.normalized, p.name, p.kind,
                          x.relation, x.source, 'outgoing' AS direction
                   FROM parts_catalog.part_cross_refs x
                   JOIN parts_catalog.parts p ON p.id = x.other_part_id
                   WHERE x.part_id = ANY(%s)
                   UNION ALL
                   SELECT p.id, p.brand, p.number, p.normalized, p.name, p.kind,
                          x.relation, x.source, 'incoming' AS direction
                   FROM parts_catalog.part_cross_refs x
                   JOIN parts_catalog.parts p ON p.id = x.part_id
                   WHERE x.other_part_id = ANY(%s)) refs
               ORDER BY brand, number LIMIT %s""", (ids, ids, limit * 4)).fetchall()

    def _same_slot(self, conn, ids: list[int], limit: int) -> list[dict]:
        """Parts one source lists for the same vehicle and part type as the requested part."""
        return conn.execute(
            """SELECT p.id, p.brand, p.number, p.normalized, p.name, p.kind,
                      count(DISTINCT f2.vehicle_id) AS shared_vehicles,
                       array_agg(DISTINCT f2.source) AS sources,
                       array_agg(DISTINCT f2.position) AS positions
                FROM parts_catalog.part_fitments f1
                JOIN parts_catalog.part_fitments f2
                  ON f2.vehicle_id = f1.vehicle_id AND f2.position = f1.position
                 AND f2.source = f1.source AND f2.part_id <> f1.part_id
                JOIN parts_catalog.parts p ON p.id = f2.part_id
                WHERE f1.part_id = ANY(%s) AND f1.position <> ''
                GROUP BY p.id ORDER BY shared_vehicles DESC, p.brand LIMIT %s""",
            (ids, limit)).fetchall()

    def lookup(self, code: str, brand: str | None = None, limit: int = 50) -> dict:
        normalized = normalize_code(code)
        if not normalized:
            raise ValueError("A part identifier is required")
        try:
            with self.repository._connect() as conn:
                parts = self._parts(conn, normalized, brand)
                ids = [part["id"] for part in parts]
                vehicles = self._vehicles(conn, ids, limit) if ids else []
                refs = self._cross_refs(conn, ids, limit) if ids else []
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return {"status": "CATALOG_NOT_INSTALLED", "parts": [], "vehicles": [],
                    "cross_references": [],
                    "warnings": ["Apply the parts catalog migrations (0009 and 0011) first."]}
        return {
            "status": "PROVISIONAL" if parts else "NOT_FOUND",
            "code": normalized,
            "parts": [self._public(part) for part in parts],
            "vehicles": vehicles,
            "cross_references": [{**self._public(ref), "relation": ref["relation"],
                                  "direction": ref["direction"], "source": ref["source"]}
                                 for ref in refs],
            "warnings": [WARNING],
        }

    def alternatives(self, code: str, brand: str | None = None, limit: int = 50) -> dict:
        normalized = normalize_code(code)
        if not normalized:
            raise ValueError("A part identifier is required")
        try:
            with self.repository._connect() as conn:
                parts = self._parts(conn, normalized, brand)
                ids = [part["id"] for part in parts]
                refs = self._cross_refs(conn, ids, limit) if ids else []
                slots = self._same_slot(conn, ids, limit) if ids else []
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return {"status": "CATALOG_NOT_INSTALLED", "items": [],
                    "warnings": ["Apply the parts catalog migrations (0009 and 0011) first."]}
        items: dict[int, dict] = {}
        for ref in refs:
            if ref["id"] in ids:
                continue
            item = items.setdefault(ref["id"], {**self._public(ref), "evidence": [],
                                                "shared_vehicles": 0})
            item["evidence"].append({"type": "cross_reference", "relation": ref["relation"],
                                     "direction": ref["direction"], "source": ref["source"]})
        for slot in slots:
            if slot["id"] in ids:
                continue
            item = items.setdefault(slot["id"], {**self._public(slot), "evidence": [],
                                                 "shared_vehicles": 0})
            item["shared_vehicles"] = slot["shared_vehicles"]
            item["evidence"].append({"type": "same_vehicle_and_part_type",
                                     "sources": slot["sources"],
                                     "positions": slot["positions"]})
        stocked = self.repository.search_parts_many([item["normalized"]
                                                     for item in items.values()])
        for item in items.values():
            products = stocked.get(item["normalized"], [])
            item["warehouse"] = products
            stock = [product.get("stock") or {} for product in products]
            item["in_store"] = any((entry.get("store") or 0) > 0 for entry in stock)
            item["in_tuz11"] = any((entry.get("tuz11") or 0) > 0 for entry in stock)
            item["status"] = "PROVISIONAL"
        ranked = sorted(items.values(), key=lambda item: (
            not item["in_store"], not item["in_tuz11"], not item["warehouse"],
            -len({e.get("source") or tuple(e.get("sources") or ()) for e in item["evidence"]}),
            -item["shared_vehicles"], item["brand"], item["number"]))
        return {
            "status": "PROVISIONAL" if parts else "NOT_FOUND",
            "code": normalized,
            "parts": [self._public(part) for part in parts],
            "items": ranked[:limit],
            "warnings": [WARNING, "Same vehicle and part type does not guarantee "
                                  "interchangeability; check notes such as axle or trim."],
        }
