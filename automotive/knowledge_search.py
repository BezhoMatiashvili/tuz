"""Additive learning integration around the existing supplier federation."""

from __future__ import annotations

import copy
import time
from typing import Any

from automotive.catalog_api import categories_for
from automotive.knowledge import PROVIDERS, KnowledgeService, requested_categories
from automotive.knowledge_types import request_vehicle
from automotive.public_http import PublicSourceError
from automotive.vin_search import SOURCE_LABELS, TERMINAL, VINSearchCoordinator, same_vehicle


class LearningSearchCoordinator(VINSearchCoordinator):
    def __init__(self, *args: Any, knowledge: KnowledgeService, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.knowledge = knowledge

    def start(self, vin: str | None, part_query: str = "", model_year: int | None = None,
              configuration: dict | None = None, *, request: str = "",
              field_origins: dict | None = None, **kwargs: Any) -> dict:
        if configuration:
            # Literal qualifiers join the profile; stated values keep their original form.
            qualified = request_vehicle(configuration, request)
            configuration = {**{k: qualified[k] for k in ("displacement_l", "aspiration")
                                if k in qualified}, **configuration}
        with self._lock:
            snapshot = super().start(vin, part_query, model_year, configuration, **kwargs)
            job = self._jobs[snapshot["search_id"]]
            job["knowledge_request"] = request or part_query
            job["field_origins"] = field_origins or {}
            return snapshot

    def prewarm(self, vin: str) -> None:
        # Decode before supplier prewarming: unknown VINs may still need vPIC.
        try:
            self._warm.submit(self._knowledge_prewarm, vin)
        except RuntimeError:
            pass

    def _knowledge_prewarm(self, vin: str) -> None:
        try:
            decoded = self.vin_service.decode(vin, deadline=time.monotonic() + 10)
            hit = self.knowledge.reuse(decoded.get("vehicle", {}), "")
            if hit["complete_categories"]:
                return
            super().prewarm(vin)
        except Exception:
            return

    def _run(self, identifier: str, vin: str | None, query: str, year: int | None,
             configuration: dict | None, turn_deadline: float | None = None) -> None:
        deadline = min(time.monotonic() + self.budget_seconds, turn_deadline or float("inf"))
        with self._lock:
            job = self._jobs[identifier]
            request = job.get("knowledge_request", query)
        vehicle = {k: v for k, v in (configuration or {}).items() if v not in (None, "")}
        decoded = None
        try:
            if vin:
                decoded = self.vin_service.decode(vin, year, deadline=deadline)
                incoming = decoded.get("vehicle") or {}
                if vehicle and not same_vehicle(incoming, vehicle):
                    vehicle = incoming
                else:
                    vehicle = {**incoming, **vehicle}
            hit = self.knowledge.reuse(vehicle, query, request)
        except Exception:
            hit = {"facts": [], "complete_categories": [], "fluids": [], "catalog_items": []}
        with self._lock:
            job["knowledge_hit"] = hit
            job["fluids"].extend(hit["fluids"])
            job["catalog_items"].extend(hit["catalog_items"])
        wanted = requested_categories(query)
        if wanted and set(wanted) <= set(hit["complete_categories"]):
            with self._lock:
                job["vehicle"] = vehicle
                for name in SOURCE_LABELS:
                    job["sources"][name].update(status="SKIPPED", error_code="NOT_NEEDED")
            try:
                self._local(identifier, vehicle, deadline)
                self._codes(identifier, deadline)
                with self._lock:
                    job.update(status="COMPLETE", expires=time.monotonic() + self.ttl_seconds)
            except Exception:
                with self._lock:
                    job.update(status="PARTIAL", expires=time.monotonic() + self.ttl_seconds)
            return
        super()._run(identifier, vin, query, year, configuration, turn_deadline)
        with self._lock:
            snapshot = copy.deepcopy(self._jobs[identifier])
        reports = [(PROVIDERS.get(r.get("provider"), ""), r) for r in snapshot["fluids"]]
        reports.append(("catalog_api", {"status": snapshot["sources"]["catalog_api"]["status"],
                                        "items": [r for r in snapshot["catalog_items"]
                                                  if r.get("provenance") != "stored"],
                                        "provider_ids": snapshot.get("catalog_provider_ids", {})}))
        if same_vehicle(snapshot["vehicle"], snapshot["motorium_vehicle"]):
            reports.append(("motorium", {"status": snapshot["sources"]["motorium"]["status"],
                                         "items": snapshot["supplied"]}))
        if decoded:
            reports.append(("vpic", decoded))
        origins = {key: ("user" if configuration and key in configuration else "vpic")
                   for key in snapshot["vehicle"]}
        origins.update(snapshot.get("field_origins") or {})
        self.knowledge.collect(snapshot["vehicle"], query, reports, request=request,
                               origins=origins)

    def _fluid(self, identifier: str, source: str, service: Any, vin: str,
               vehicle: dict, deadline: float) -> None:
        with self._lock:
            complete = set(self._jobs[identifier].get("knowledge_hit", {}).get(
                "complete_categories", []))
            wanted = set(requested_categories(self._jobs[identifier]["part_query"]))
        if ((source in {"shell", "pakelo", "fuchs"} and "engine_oil" in complete)
                or (source == "jsfilter" and wanted - {"engine_oil"} <= complete)):
            self._source(identifier, source, status="SKIPPED", error_code="NOT_NEEDED")
            return
        super()._fluid(identifier, source, service, vin, vehicle, deadline)

    def _motorium(self, identifier: str, vin: str, query: str, deadline: float) -> None:
        with self._lock:
            complete = set(self._jobs[identifier].get("knowledge_hit", {}).get(
                "complete_categories", []))
        wanted = set(requested_categories(query)) - {"engine_oil"}
        if wanted and wanted <= complete:
            self._source(identifier, "motorium", status="SKIPPED", error_code="NOT_NEEDED")
            return
        super()._motorium(identifier, vin, query, deadline)

    def _catalog_api(self, identifier: str, vin: str, vehicle: dict, query: str,
                     deadline: float) -> None:
        with self._lock:
            hit = self._jobs[identifier].get("knowledge_hit", {})
        complete = hit.get("complete_categories", [])
        categories = [c for c in categories_for(query) if c not in complete]
        if not categories:
            self._source(identifier, "catalog_api", status="OK", count=len(hit.get(
                "catalog_items", [])))
            return
        self._source(identifier, "catalog_api", status="RUNNING", total_queries=len(categories))
        try:
            report = self._catalog.lookup(vin, vehicle, categories, deadline=deadline)
        except PublicSourceError as exc:
            self._source(identifier, "catalog_api", status="ERROR", error_code=exc.code)
            return
        except Exception:
            self._source(identifier, "catalog_api", status="ERROR",
                         error_code="PROVIDER_UNAVAILABLE")
            return
        status = {"PROVISIONAL": "OK", "EMPTY": "EMPTY", "CONFLICTED": "CONFLICTED"}.get(
            str(report.get("status")), "ERROR")
        items = list(report.get("items") or [])
        with self._lock:
            job = self._jobs.get(identifier)
            if not job or job["status"] in TERMINAL:
                return
            job["catalog_items"] = list(hit.get("catalog_items", [])) + items
            job["catalog_label"] = report.get("vehicle_label") or ""
            job["catalog_provider_ids"] = report.get("provider_ids") or {}
        self._source(identifier, "catalog_api", status=status, count=len(items),
                     completed_queries=len(categories),
                     error_code="MULTIPLE_VEHICLES" if status == "CONFLICTED" else None)

    def get(self, identifier: str, offset: int = 0, limit: int = 50) -> dict | None:
        snapshot = super().get(identifier, offset, limit)
        if snapshot is not None:
            with self._lock:
                facts = self._jobs[identifier].get("knowledge_hit", {}).get("facts", [])
            snapshot["knowledge"] = [{
                "fact_id": f["fact_id"], "kind": f["kind"], "value": f["value"],
                "provenance": "stored", "validation_status": "MACHINE_ACCEPTED",
                "validated_at": str(f["validated_at"]), "valid_until": str(f["valid_until"]),
                "citations": f["provenance"]["validation"]["citations"],
                "receipts": [{k: r[k] for k in ("receipt_id", "url", "retrieved_at")}
                             for r in f["provenance"]["receipts"]],
            } for f in facts]
            snapshot["provenance"] = "stored" if facts else "live"
            snapshot["validation_timestamp"] = max((str(f["validated_at"]) for f in facts),
                                                    default=None)
        return snapshot
