"""Usage-only collection and exact-profile reuse; failures preserve ordinary discovery."""

from __future__ import annotations

import re
from typing import Any

import psycopg

from automotive.catalog_api import categories_for
from automotive.categories import category_for_query
from automotive.knowledge_repository import KnowledgeRepository
from automotive.knowledge_types import claim, frozen_context, normalize_vehicle, request_vehicle
from automotive.oil_match import GRADE, normalize_grade

SOURCE_URLS = {
    "vpic": "https://vpic.nhtsa.dot.gov/api/",
    "catalog_api": "https://auto-parts-catalog.apiprofile.com/",
    "motorium": "https://motorium.ge/",
    "shell": "https://www.shell.com/",
    "pakelo": "https://pakelo.com/",
    "fuchs": "https://www.fuchs.com/",
    "jsfilter": "https://www.js-filter.com/",
}
PROVIDERS = {"Shell": "shell", "Pakelo": "pakelo", "Fuchs": "fuchs",
             "JS Asakashi": "jsfilter"}


def requested_categories(query: str) -> list[str]:
    categories = list(categories_for(query))
    kind = category_for_query(query)
    if kind in {"engine_oil", "oil_change"} or not query.strip():
        categories.append("engine_oil")
    return list(dict.fromkeys(categories))


def knowledge_for(search_service: Any) -> KnowledgeService | None:
    knowledge = getattr(search_service, "knowledge", None)
    return knowledge if isinstance(knowledge, KnowledgeService) else None


class KnowledgeService:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    @classmethod
    def from_settings(cls, settings: Any) -> KnowledgeService | None:
        if not (settings.knowledge_enabled and settings.database_url
                and settings.anthropic_api_key and settings.knowledge_judge_model):
            return None
        return cls(KnowledgeRepository(settings.database_url, statement_timeout_ms=1500))

    def reuse(self, vehicle: dict, query: str, request: str = "") -> dict:
        empty = {"facts": [], "complete_categories": [], "fluids": [], "catalog_items": []}
        vehicle = request_vehicle(vehicle, request)
        if not all(vehicle.get(k) for k in ("make", "model", "model_year")):
            return empty
        try:
            policies = self.repository.policies()
            facts = self.repository.lookup(vehicle, requested_categories(query))
            # Policy removal takes effect on reads as well as publication.
            facts = [f for f in facts if f["provenance"]["candidate"]["source"] in policies]
        except (RuntimeError, psycopg.Error, ValueError, KeyError):
            return empty
        complete, fluids, parts = [], [], []
        for category in requested_categories(query):
            rows = [f for f in facts if f["category"] == category]
            kinds = {f["kind"] for f in rows}
            if category == "engine_oil":
                required = {"oil_viscosity"}
                if re.search(r"capacity|lit[er]|მოცულ|რამდენ", request, re.I):
                    required.add("capacity")
                if re.search(r"approval|დაშვებ", request, re.I):
                    required.add("manufacturer_approval")
                if required <= kinds:
                    complete.append(category)
                grade = next((f["value"]["grade"] for f in rows
                              if f["kind"] == "oil_viscosity"), None)
                capacity = next((str(f["value"]["amount"]) for f in rows
                                 if f["kind"] == "capacity"), None)
                products = [f for f in rows if f["kind"] == "product_recommendation"] or [None]
                for product in products:
                    name = product["value"]["product_name"] if product else ""
                    if not grade:
                        name = GRADE.sub("", name).strip()
                    source = (product["provenance"]["candidate"]["source"] if product
                              else "knowledge")
                    fluids.append({"provider": source.title(), "status": "PROVISIONAL",
                                   "provenance": "stored", "validation_status": "MACHINE_ACCEPTED",
                                   "items": [{"kind": "oil", "component": "Engine",
                                              "product_name": name, "viscosity": grade,
                                              "product_code": None, "capacity": capacity,
                                              "interval": None}]})
            else:
                if "part_number" in kinds:
                    complete.append(category)
                for row in rows:
                    if row["kind"] == "part_number":
                        value = row["value"]
                        parts.append({"code": value["code"], "supplier": value["manufacturer"],
                                      "product": value.get("name", ""), "category": category,
                                      "oem_codes": [], "provenance": "stored",
                                      "validation_status": "MACHINE_ACCEPTED"})
        return {"facts": facts, "complete_categories": complete,
                "fluids": fluids, "catalog_items": parts}

    def collect(self, vehicle: dict, query: str, reports: list[tuple[str, dict]], *,
                request: str = "", origins: dict | None = None) -> None:
        try:
            policies = self.repository.policies()
            context = frozen_context(vehicle, request or query, category_for_query(query) or query,
                                     origins or {})
            if not all(context["vehicle"].get(k) for k in ("make", "model", "model_year")):
                return
            claims = []
            for source, report in reports:
                if source not in policies or report.get("provenance") == "stored":
                    continue
                if report.get("status") in {"CONFLICTED", "ERROR", "EMPTY", "UNRESOLVED"}:
                    continue
                url = report.get("source_url") or SOURCE_URLS.get(source, "")
                ids = report.get("provider_ids") or {}
                context["provider_ids"][source] = ids
                restrictions = report.get("restrictions") or {}

                def add(kind: str, category: str, slot: str, value: dict,
                        item: dict | None = None) -> None:
                    row = item or {}
                    claims.append(claim(kind, category, slot, value, source,
                                        row.get("source_url") or url,
                                        provider_ids={**ids, **{k: row[k] for k in (
                                            "provider_product_id", "product_code", "article_id",
                                            "supplier_id") if row.get(k) is not None}},
                                        restrictions=row.get("restrictions") or restrictions))

                if source == "vpic":
                    add("vehicle_identity", "vehicle_identity", "identity",
                        normalize_vehicle(report.get("vehicle") or vehicle))
                for item in report.get("items") or []:
                    if source in {"shell", "pakelo", "fuchs"}:
                        grade = normalize_grade(item.get("viscosity"))
                        name = str(item.get("product_name") or "")
                        if grade:
                            add("oil_viscosity", "engine_oil", "engine:viscosity",
                                {"grade": grade}, item)
                        if name:
                            add("product_recommendation", "engine_oil", "product:" + name,
                                {"product_name": name}, item)
                        capacity = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:[lL]|litres?)?\s*",
                                                str(item.get("capacity") or ""))
                        if capacity:
                            add("capacity", "engine_oil", "engine:capacity",
                                {"amount": float(capacity[1].replace(",", ".")), "unit": "L"},
                                item)
                        for approval in item.get("approvals") or []:
                            add("manufacturer_approval", "engine_oil", "approval:" + approval,
                                {"approval": approval}, item)
                    else:
                        code = item.get("code") or item.get("mpn") or item.get("product_code")
                        manufacturer = item.get("supplier") or item.get("manufacturer") or (
                            "JS Asakashi" if source == "jsfilter" else "")
                        category = item.get("category") or {
                            "OIL": "oil_filter", "AIR": "air_filter", "CABIN": "cabin_filter",
                            "FUEL": "fuel_filter"}.get(item.get("component")) or category_for_query(
                                query)
                        if code and manufacturer and category:
                            slot = f"{manufacturer}:{code}"
                            add("part_number", category, slot,
                                {"code": code, "manufacturer": manufacturer,
                                 "name": item.get("product") or item.get("name") or ""}, item)
                            # Fitment is a distinct assertion even on a vehicle-specific result.
                            add("fitment", category, slot,
                                {"code": code, "manufacturer": manufacturer, "compatible": True},
                                item)
            self.repository.enqueue(context, list({c["claim_id"]: c for c in claims}.values()))
        except (RuntimeError, psycopg.Error, ValueError, KeyError, TypeError):
            return
