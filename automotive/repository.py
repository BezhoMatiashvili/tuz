"""Parameterized PostgreSQL services over the existing, authoritative inventory."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from automotive.categories import query_patterns
from automotive.codes import PARSER_VERSION, normalize_code, product_codes
from automotive.name_match import make_pattern, model_pattern
from automotive.stock import SQL_AVAILABLE_FIRST, STORE_COLUMN, TUZ11_COLUMN, parse_count

PRODUCT_COLUMNS = f"""p.source_product_id, p.product_code, p.product_name, p.product_model,
    p.original_code, p.duplicate_code, p.brand, p.last_observed_at, p.row_hash,
    p.source_columns->>'{STORE_COLUMN}' AS stock_store,
    p.source_columns->>'{TUZ11_COLUMN}' AS stock_tuz11"""
# Index freshness ignores the hourly stock columns (see migration 0007). A row is stale
# when it was never indexed, the parser changed, or its descriptive content changed.
CONTENT_HASH = "automotive.content_hash(p.source_columns)"
STALE_ROWS = f"""(s.source_product_id IS NULL OR s.parser_version <> %s
    OR (s.row_hash_seen IS DISTINCT FROM p.row_hash AND s.source_fingerprint <> {CONTENT_HASH}))"""
# Name filters for engine-family items (engine-dependent parts only).
OIL_FILTER_ELEMENT = r'ზეთის\s+ფილტრ|(^|\W)oil\s+filter'
OIL_FILTER_NOT_ELEMENT = (r'კორპ|საფენ|ხუფ|ჩობალ|შუასადებ|ბუდე|მილი|შემამჭ|კრონშტ'
                          r'|(^|\W)(housing|gasket|cap|seal|pipe|cooler)(\W|$)')
SUMP_PLUG = r'კარტერის\s+საცობ|(^|\W)(drain|sump)\s+plug'


class DatabaseNotConfigured(RuntimeError):
    """Automotive credentials are missing; do not fall back to ambient libpq settings."""


def _product(row: dict) -> dict:
    result = {key: row[key] for key in (
        "source_product_id", "product_code", "product_name", "product_model",
        "original_code", "duplicate_code", "brand", "last_observed_at",
    ) if key in row}
    stock = None
    if "stock_store" in row or "stock_tuz11" in row:
        # Quantities come from the synchronized source columns; a cell that is not
        # a plain number stays None, which classifies as unconfirmed, never as stocked.
        stock = {"store": parse_count(row.get("stock_store")),
                 "tuz11": parse_count(row.get("stock_tuz11"))}
    result.update(
        price=None, currency=None, stock=stock, status="PROVISIONAL",
        source_fingerprint=row.get("row_hash"),
    )
    return result


def _evidence(row: dict) -> dict:
    # Metadata is authoritative; a payload cannot override review or licensing.
    from automotive.evidence import verified_record

    result = {**row["payload"], **dict(row)}
    result["source_name"] = result.get("title", "")
    result["verification_status"] = (
        "VERIFIED" if verified_record(result) else
        "UNKNOWN" if (result.get("review_status") == "rejected"
                      or result.get("superseded_by") or result.get("revoked", False))
        else "PROVISIONAL"
    )
    result["status"] = result["verification_status"]
    return result


class AutomotiveRepository:
    """Short-lived transactions support FastAPI's synchronous worker threads."""

    def __init__(self, database_url: str, statement_timeout_ms: int | None = None) -> None:
        self.database_url = database_url
        self.statement_timeout_ms = statement_timeout_ms

    def _connect(self):
        if not self.database_url.strip():
            # An empty libpq DSN would silently use ambient PG* credentials.
            raise DatabaseNotConfigured("Automotive database is not configured")
        connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        if self.statement_timeout_ms is not None:
            try:
                connection.execute("SELECT set_config('statement_timeout',%s,false)",
                                   (str(self.statement_timeout_ms),))
            except psycopg.Error:
                connection.close()
                raise
        return connection

    def search_parts(
        self, code: str, manufacturer: str | None = None, limit: int = 30,
    ) -> dict:
        normalized = normalize_code(code)
        if not normalized:
            raise ValueError("A part identifier is required")
        limit = max(1, min(limit, 100))
        matches: dict[str, dict] = {}
        warnings = ["Inventory code associations do not establish vehicle compatibility."]
        with self._connect() as conn:
            # Matching cached associations are used only while both source and parser match.
            indexed = conn.execute(
                sql.SQL("""SELECT {}, n.original, n.normalized, n.field,
                    n.manufacturer, n.raw_value FROM automotive.part_numbers n
                    JOIN warehouse.warehouse_products p USING(source_product_id)
                    WHERE p.is_active AND n.normalized = %s AND n.source_fingerprint = {}
                    AND n.parser_version = %s ORDER BY p.source_product_id, n.field""").format(
                    sql.SQL(PRODUCT_COLUMNS), sql.SQL(CONTENT_HASH)),
                (normalized, PARSER_VERSION),
            )
            for row in indexed:
                if manufacturer and row["brand"].casefold() != manufacturer.strip().casefold():
                    continue
                product = matches.setdefault(row["source_product_id"], _product(row))
                product.setdefault("matched_codes", []).append({
                    key: row[key] for key in (
                        "original", "normalized", "field", "manufacturer", "raw_value",
                    )
                })
            # A changed/unindexed product is parsed directly. Never use an obsolete match.
            # Server cursor avoids materializing the full warehouse in application memory.
            with conn.cursor(name="automotive_current_inventory") as cursor:
                cursor.execute(
                    sql.SQL("""SELECT {} FROM warehouse.warehouse_products p
                    LEFT JOIN automotive.catalog_index_state s USING(source_product_id)
                    WHERE p.is_active AND {} ORDER BY p.source_product_id""").format(
                        sql.SQL(PRODUCT_COLUMNS), sql.SQL(STALE_ROWS)),
                    (PARSER_VERSION,),
                )
                for row in cursor:
                    if manufacturer and row["brand"].casefold() != manufacturer.strip().casefold():
                        continue
                    codes = [item for item in product_codes(row) if item.normalized == normalized]
                    if codes:
                        product = _product(row)
                        product["matched_codes"] = [asdict(item) for item in codes]
                        matches[row["source_product_id"]] = product
        items = [matches[key] for key in sorted(matches)]
        variants = {
            (item["brand"].casefold(), match["original"].upper())
            for item in items for match in item["matched_codes"]
        }
        if len(variants) > 1:
            warnings.append("Ambiguous normalized identifier: review all manufacturer candidates.")
        if len(items) > limit:
            warnings.append("More candidates exist; narrow the manufacturer or identifier.")
        return {"items": items[:limit], "warnings": warnings, "query": code,
                "status": "PROVISIONAL" if items else "UNKNOWN"}

    def search_parts_many(self, codes: list[str]) -> dict[str, list[dict]]:
        """Exact code matches for many identifiers in one pass: {normalized code: products}.

        The same freshness rule as search_parts: an indexed association counts only while
        its fingerprint and parser version are current; other rows are parsed directly.
        """
        wanted = {normalize_code(code) for code in codes if normalize_code(code)}
        if not wanted:
            return {}
        matches: dict[str, dict[str, dict]] = {}
        with self._connect() as conn:
            indexed = conn.execute(
                sql.SQL("""SELECT {}, n.original, n.normalized, n.field,
                    n.manufacturer, n.raw_value FROM automotive.part_numbers n
                    JOIN warehouse.warehouse_products p USING(source_product_id)
                    WHERE p.is_active AND n.normalized = ANY(%s)
                    AND n.source_fingerprint = {} AND n.parser_version = %s
                    ORDER BY p.source_product_id, n.field""").format(
                    sql.SQL(PRODUCT_COLUMNS), sql.SQL(CONTENT_HASH)),
                (sorted(wanted), PARSER_VERSION),
            )
            for row in indexed:
                product = matches.setdefault(row["normalized"], {}).setdefault(
                    row["source_product_id"], _product(row))
                product.setdefault("matched_codes", []).append({
                    key: row[key] for key in (
                        "original", "normalized", "field", "manufacturer", "raw_value",
                    )
                })
            with conn.cursor(name="automotive_current_inventory_many") as cursor:
                cursor.execute(
                    sql.SQL("""SELECT {} FROM warehouse.warehouse_products p
                    LEFT JOIN automotive.catalog_index_state s USING(source_product_id)
                    WHERE p.is_active AND {} ORDER BY p.source_product_id""").format(
                        sql.SQL(PRODUCT_COLUMNS), sql.SQL(STALE_ROWS)),
                    (PARSER_VERSION,),
                )
                for row in cursor:
                    for item in product_codes(row):
                        if item.normalized in wanted:
                            product = matches.setdefault(item.normalized, {}).setdefault(
                                row["source_product_id"], _product(row))
                            product.setdefault("matched_codes", []).append(asdict(item))
        return {key: list(found.values()) for key, found in matches.items()}

    def get_product(self, product_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                sql.SQL("SELECT {} FROM warehouse.warehouse_products p "
                "WHERE p.source_product_id = %s AND p.is_active").format(
                    sql.SQL(PRODUCT_COLUMNS)), (product_id,),
            ).fetchone()
        return _product(row) if row else None

    @staticmethod
    def _name_filters(query: str) -> tuple[sql.Composed, list[str]]:
        includes, excludes = query_patterns(query)
        field = "concat_ws(' ',p.product_name,p.product_model)"
        clauses = [sql.SQL(field + " ~* %s") for _ in includes]
        clauses += [sql.SQL(field + " !~* %s") for _ in excludes]
        return sql.SQL(' AND ').join(clauses or [sql.SQL('TRUE')]), includes + excludes

    def search_products(self, query: str, manufacturer: str | None = None,
                        limit: int = 30, offset: int = 0) -> dict:
        """Name/category discovery with filters applied before LIMIT/OFFSET."""
        if not query.strip() or len(query) > 200:
            raise ValueError('A product name or category of at most 200 characters is required')
        filters, parameters = self._name_filters(query)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        with self._connect() as conn:
            rows = conn.execute(
                sql.SQL("""SELECT {} FROM warehouse.warehouse_products p
                WHERE p.is_active AND {} AND (%s::text IS NULL OR lower(p.brand) = lower(%s))
                ORDER BY p.source_product_id LIMIT %s OFFSET %s""").format(
                    sql.SQL(PRODUCT_COLUMNS), filters),
                (*parameters, manufacturer, manufacturer, limit + 1, offset),
            ).fetchall()
        items = [_product(row) for row in rows[:limit]]
        return {'items': items, 'query': query, 'offset': offset,
                'has_more': len(rows) > limit,
                'next_offset': offset + limit if len(rows) > limit else None,
                'status': 'PROVISIONAL' if items else 'UNKNOWN',
                'warnings': ['Name/category matches do not establish vehicle compatibility.',
                             'Found in our catalog; stock unconfirmed.']}

    def search_engine_oils(self, viscosity: str, brand: str | None = None,
                           limit: int = 100) -> dict:
        """Engine oils of one SAE grade; stocked items first. Filters run before LIMIT."""
        match = re.fullmatch(r"\s*(\d{1,2})W[- ]?(\d{2})\s*", viscosity or "", re.IGNORECASE)
        if not match:
            raise ValueError("An SAE viscosity such as 5W-40 is required")
        grade = rf"(^|[^0-9]){match.group(1)}W-?{match.group(2)}([^0-9]|$)"
        includes, excludes = query_patterns("engine oil")
        field = "concat_ws(' ',p.product_name,p.product_model)"
        limit = max(1, min(limit, 100))
        query = sql.SQL("""SELECT {columns} FROM warehouse.warehouse_products p
            WHERE p.is_active AND {field} ILIKE %s
            AND {field} ~* %s AND {field} !~* %s AND {field} ~* %s
            AND (%s::text IS NULL OR lower(p.brand) = lower(%s))
            ORDER BY {stock_order}, p.source_product_id LIMIT %s""").format(
            columns=sql.SQL(PRODUCT_COLUMNS), field=sql.SQL(field),
            stock_order=sql.SQL(SQL_AVAILABLE_FIRST))
        # The cheap LIKE (`5W%40`) runs first so the slower regexes see only a few rows.
        cheap = f"%{match.group(1)}W%{match.group(2)}%"
        with self._connect() as conn:
            rows = conn.execute(
                query, (cheap, includes[0], excludes[0], grade, brand, brand, limit),
            ).fetchall()
        return {"items": [_product(row) for row in rows], "viscosity": viscosity}

    def search_engine_family(self, family: str, limit: int = 50) -> dict:
        """Oil filter elements and sump plugs whose text names exactly this engine family."""
        groups = re.findall(r"[A-Za-z]+|\d+", family or "")
        if len(groups) < 2 or len(family) > 20:
            raise ValueError("An engine family such as M276 is required")
        spaced = r"\s?".join(re.escape(g) for g in groups)
        pattern = r"(^|[^A-Za-z0-9])" + spaced + r"(?![A-Za-z0-9])"
        field = "concat_ws(' ',p.product_name,p.product_model)"
        limit = max(1, min(limit, 100))
        query = sql.SQL("""SELECT {columns} FROM warehouse.warehouse_products p
            WHERE p.is_active AND {field} ~* %s AND (
                ({field} ~* %s AND p.product_name !~* %s) OR {field} ~* %s)
            ORDER BY {stock_order}, p.source_product_id LIMIT %s""").format(
            columns=sql.SQL(PRODUCT_COLUMNS), field=sql.SQL(field),
            stock_order=sql.SQL(SQL_AVAILABLE_FIRST))
        with self._connect() as conn:
            rows = conn.execute(
                query, (pattern, OIL_FILTER_ELEMENT, OIL_FILTER_NOT_ELEMENT, SUMP_PLUG, limit),
            ).fetchall()
        items = []
        for row in rows:
            product = _product(row)
            product["part_role"] = ("sump_plug" if re.search(SUMP_PLUG, row["product_name"],
                                                             re.IGNORECASE) else "oil_filter")
            items.append(product)
        return {"items": items, "family": family}

    def inventory_products(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                sql.SQL("SELECT {} FROM warehouse.warehouse_products p WHERE p.is_active "
                "ORDER BY source_product_id").format(sql.SQL(PRODUCT_COLUMNS))
            ).fetchall()
        return [_product(row) for row in rows]

    def vehicle_fitment_records(self) -> list[dict]:
        return self._evidence_rows("fitment")

    def cross_reference_records(self) -> list[dict]:
        return self._evidence_rows("cross_reference")

    def vehicle_catalog_candidates(
        self, vehicle: dict, limit: int = 30, exclude_ids: list[str] | None = None,
        offset: int = 0, part_query: str = '',
    ) -> dict:
        """Literal make/model mentions are candidates, never fitment evidence."""
        make = str(vehicle.get("make") or "").strip()
        model = str(vehicle.get("model") or "").strip()
        if not make or not model:
            return {"items": [], "has_more": False}
        limit = max(1, min(limit, 100))
        filters, parameters = self._name_filters(part_query)
        with self._connect() as conn:
            rows = conn.execute(
                sql.SQL("""SELECT {} FROM warehouse.warehouse_products p
                WHERE p.is_active AND concat_ws(' ',p.product_name,p.product_model) ~* %s
                AND concat_ws(' ',p.product_name,p.product_model) ~* %s
                AND NOT (p.source_product_id = ANY(%s::text[]))
                AND {}
                ORDER BY {}, p.source_product_id LIMIT %s OFFSET %s""").format(
                    sql.SQL(PRODUCT_COLUMNS), filters, sql.SQL(SQL_AVAILABLE_FIRST)),
                (make_pattern(make), model_pattern(model),
                 exclude_ids or [], *parameters, limit + 1, max(0, offset)),
            ).fetchall()
        return {"items": [_product(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    def get_evidence(self, evidence_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM automotive.evidence WHERE evidence_id = %s", (evidence_id,),
            ).fetchone()
        return _evidence(row) if row else None

    def _evidence_rows(self, kind: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM automotive.evidence WHERE kind = %s ORDER BY evidence_id", (kind,),
            ).fetchall()
        return [_evidence(row) for row in rows]

    def fitments(self, code: str, vehicle: dict | None = None) -> dict:
        from automotive.evidence import evaluate_compatibility

        records = [row for row in self._evidence_rows("fitment")
                   if normalize_code(str(row.get("part_code", row.get("code", ""))))
                   == normalize_code(code)]
        result = evaluate_compatibility(records, vehicle)
        return self._code_evidence_result(code, records, result)

    def cross_references(self, code: str) -> dict:
        from automotive.evidence import evaluate_compatibility

        # Direction is never inverted, including supersession relationships.
        records = [row for row in self._evidence_rows("cross_reference")
                   if normalize_code(str(row.get("from_code", ""))) == normalize_code(code)]
        result = evaluate_compatibility(records)
        return self._code_evidence_result(code, records, result)

    def _code_evidence_result(self, code: str, records: list[dict], result: dict) -> dict:
        candidates = self.search_parts(code, limit=100)
        contexts = {
            (str(row.get("manufacturer", row.get("brand", ""))).strip().casefold(),
             str(row.get("part_code", row.get("from_code", code))).upper())
            for row in records
        }
        inventory_ambiguous = any("Ambiguous" in warning
                                  for warning in candidates["warnings"])
        if inventory_ambiguous or len(contexts) > 1:
            if result["status"] == "VERIFIED":
                result["status"] = "PROVISIONAL"
            result["warnings"].append(
                "Manufacturer or identifier ambiguity requires selecting a specific candidate."
            )
        return {**result, "items": records, "candidates": candidates["items"]}

    def vehicle_specifications(self, vehicle: dict) -> list[dict]:
        from automotive.evidence import applies_to

        return [row for row in self._evidence_rows("service_specification")
                if applies_to(row, vehicle)]

    def product_approvals(self, product_id: str) -> list[dict]:
        return [row for row in self._evidence_rows("product_approval")
                if str(row.get("product_id", row.get("source_product_id", ""))) == product_id]

    def get_cached_vin(self, vin_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM automotive.vin_identifications "
                "WHERE vin_hash = %s AND expires_at > now()", (vin_hash,),
            ).fetchone()
        return row["payload"] if row else None

    def cache_vin(self, vin_hash: str, payload: dict) -> None:
        def sanitized(value):
            if isinstance(value, dict):
                return {key: sanitized(item) for key, item in value.items()
                        if "vin" not in key.casefold()}
            if isinstance(value, list):
                return [sanitized(item) for item in value]
            return value

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO automotive.vin_identifications(vin_hash,payload)
                VALUES (%s,%s) ON CONFLICT(vin_hash) DO UPDATE SET
                payload=excluded.payload,observed_at=now(),expires_at=now()+INTERVAL '30 days'""",
                (vin_hash, Jsonb(sanitized(payload))),
            )

    def import_evidence(self, records: list[dict]) -> dict:
        ids = []
        with self._connect() as conn:
            for record in records:
                # Imports cannot self-approve, even when called outside the HTTP importer.
                clean = {key: record.get(key, default) for key, default in (
                    ("kind", ""), ("source_url", ""), ("title", ""),
                    ("authority", "unknown"), ("reuse_permission", "unknown"),
                    ("applicability", {}), ("restrictions", {}), ("payload", {}),
                )}
                if not clean["title"]:
                    clean["title"] = record.get("source_name", "")
                fingerprint = hashlib.sha256(json.dumps(
                    clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                ).encode()).hexdigest()
                evidence_id = "ev_" + fingerprint
                row = conn.execute(
                    """INSERT INTO automotive.evidence(evidence_id,kind,source_url,title,
                    authority,reuse_permission,applicability,restrictions,payload,fingerprint)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(fingerprint) DO NOTHING RETURNING evidence_id""",
                    (evidence_id, clean["kind"], clean["source_url"], clean["title"],
                     clean["authority"], clean["reuse_permission"], Jsonb(clean["applicability"]),
                     Jsonb(clean["restrictions"]), Jsonb(clean["payload"]), fingerprint),
                ).fetchone()
                if row:
                    self._associate_evidence(conn, evidence_id, clean)
                ids.append(evidence_id)
        return {"evidence_ids": ids, "imported": len(set(ids)), "status": "PROVISIONAL",
                "warnings": ["Imported assertions require an independent authorized review."]}

    @staticmethod
    def _associate_evidence(conn: Any, evidence_id: str, record: dict) -> None:
        payload = record["payload"]
        kind = record["kind"]
        if kind == "fitment":
            conn.execute(
                "INSERT INTO automotive.fitments(evidence_id,part_code,compatible) "
                "VALUES(%s,%s,%s)",
                (evidence_id,
                 normalize_code(str(payload.get("part_code", payload.get("code", "")))),
                 payload.get("compatible")),
            )
        elif kind == "cross_reference":
            conn.execute(
                """INSERT INTO automotive.cross_references
                (evidence_id,from_code,to_code,relationship,restrictions) VALUES(%s,%s,%s,%s,%s)""",
                (evidence_id, normalize_code(str(payload.get("from_code", ""))),
                 normalize_code(str(payload.get("to_code", ""))),
                 payload.get("relationship", "reference"), Jsonb(record["restrictions"])),
            )
        elif kind == "service_specification":
            conn.execute(
                "INSERT INTO automotive.service_specifications(evidence_id,category) VALUES(%s,%s)",
                (evidence_id, payload.get("category", "unknown")),
            )
        elif kind == "product_approval":
            conn.execute(
                """INSERT INTO automotive.product_approvals
                (evidence_id,source_product_id,approval) VALUES(%s,%s,%s)""",
                (evidence_id, str(payload.get("product_id", payload.get("source_product_id", ""))),
                 payload.get("approval", "")),
            )

    def create_job(self, kind: str, payload: dict) -> dict:
        if kind != "index" or payload:
            raise ValueError("Only inventory index jobs with an empty payload are supported")
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO automotive.enrichment_jobs(job_id,kind,payload) "
                "VALUES(%s,%s,%s) RETURNING *", (str(uuid.uuid4()), kind, Jsonb(payload)),
            ).fetchone()
        return self._public_job(row)

    @staticmethod
    def _public_job(row: dict) -> dict:
        return {key: value for key, value in row.items()
                if key not in {"payload", "lease_token", "lease_until"}}

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM automotive.enrichment_jobs WHERE job_id = %s", (job_id,),
            ).fetchone()
        return self._public_job(row) if row else None

    def claim_job(self) -> dict | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE automotive.enrichment_jobs SET status='failed',"
                "error_code='retry_exhausted',updated_at=now() "
                "WHERE status='running' AND lease_until < now() AND attempts >= 5"
            )
            row = conn.execute(
                """WITH candidate AS (SELECT job_id FROM automotive.enrichment_jobs
                WHERE (status IN ('pending','failed') OR
                    (status='running' AND lease_until < now())) AND attempts < 5
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE automotive.enrichment_jobs j SET status='running',attempts=attempts+1,
                lease_until=now()+INTERVAL '2 minutes',lease_token=%s,
                updated_at=now(),error_code=NULL
                FROM candidate c WHERE j.job_id=c.job_id RETURNING j.*""",
                (str(uuid.uuid4()),),
            ).fetchone()
        return row

    def index_batch(self, job: dict, batch_size: int = 500) -> bool:
        """Index one checkpointed batch; unchanged content only refreshes row_hash_seen."""
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM automotive.enrichment_jobs WHERE job_id=%s "
                "AND lease_token=%s AND status='running' FOR UPDATE",
                (job["job_id"], job["lease_token"]),
            ).fetchone()
            if not owned:
                raise RuntimeError("Job lease lost")
            rows = conn.execute(
                sql.SQL("SELECT {}, {} AS content_hash "
                "FROM warehouse.warehouse_products p "
                "WHERE p.source_product_id > %s ORDER BY p.source_product_id LIMIT %s").format(
                    sql.SQL(PRODUCT_COLUMNS), sql.SQL(CONTENT_HASH)),
                (job["checkpoint"], batch_size),
            ).fetchall()
            states = {row["source_product_id"]: row for row in conn.execute(
                "SELECT source_product_id, source_fingerprint, parser_version, row_hash_seen "
                "FROM automotive.catalog_index_state WHERE source_product_id = ANY(%s)",
                ([row["source_product_id"] for row in rows],),
            )}
            seen = []
            for product in rows:
                state = states.get(product["source_product_id"])
                if (state and state["parser_version"] == PARSER_VERSION
                        and state["source_fingerprint"] == product["content_hash"]):
                    if state["row_hash_seen"] != product["row_hash"]:
                        seen.append((product["row_hash"], product["source_product_id"]))
                    continue
                conn.execute("DELETE FROM automotive.part_numbers WHERE source_product_id=%s",
                             (product["source_product_id"],))
                codes = [(product["source_product_id"], code.field, code.original, code.normalized,
                          code.manufacturer, code.raw_value, product["content_hash"],
                          PARSER_VERSION) for code in product_codes(product)]
                if codes:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO automotive.part_numbers(source_product_id,field,
                            original,normalized,manufacturer,raw_value,source_fingerprint,
                            parser_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT DO NOTHING""", codes)
                conn.execute(
                    """INSERT INTO automotive.catalog_index_state
                    (source_product_id,source_fingerprint,parser_version,row_hash_seen)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(source_product_id) DO UPDATE SET
                    source_fingerprint=excluded.source_fingerprint,
                    parser_version=excluded.parser_version,
                    row_hash_seen=excluded.row_hash_seen,indexed_at=now()""",
                    (product["source_product_id"], product["content_hash"], PARSER_VERSION,
                     product["row_hash"]),
                )
            if seen:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "UPDATE automotive.catalog_index_state SET row_hash_seen=%s "
                        "WHERE source_product_id=%s", seen)
            done = len(rows) < batch_size
            checkpoint = rows[-1]["source_product_id"] if rows else job["checkpoint"]
            conn.execute(
                "UPDATE automotive.enrichment_jobs SET checkpoint=%s,status=%s,updated_at=now(),"
                "lease_until=now()+INTERVAL '2 minutes' WHERE job_id=%s AND lease_token=%s",
                (checkpoint, "succeeded" if done else "running", job["job_id"], job["lease_token"]),
            )
            job["checkpoint"] = checkpoint
        return done

    def needs_index(self) -> bool:
        """True when any product was never indexed, parsed by an older parser, or its row
        changed since the index last looked at it (including stock-only changes)."""
        with self._connect() as conn:
            return bool(conn.execute(
                """SELECT EXISTS (SELECT 1 FROM warehouse.warehouse_products p
                LEFT JOIN automotive.catalog_index_state s USING(source_product_id)
                WHERE s.source_product_id IS NULL OR s.parser_version <> %s
                    OR s.row_hash_seen IS DISTINCT FROM p.row_hash) AS needed""",
                (PARSER_VERSION,),
            ).fetchone()["needed"])

    def enqueue_index_if_needed(self) -> dict | None:
        """Queue one index job when the catalog changed and none is already waiting."""
        with self._connect() as conn:
            waiting = conn.execute(
                "SELECT 1 FROM automotive.enrichment_jobs WHERE kind='index' AND "
                "(status IN ('pending','running') OR (status='failed' AND attempts < 5)) LIMIT 1"
            ).fetchone()
        if waiting or not self.needs_index():
            return None
        return self.create_job("index", {})

    def fail_job(self, job: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE automotive.enrichment_jobs SET status='failed',error_code='index_failed',"
                "updated_at=now() WHERE job_id=%s AND lease_token=%s",
                (job["job_id"], job["lease_token"]),
            )
