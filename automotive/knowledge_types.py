"""Private, VIN-free learning contracts. Machine acceptance never means VERIFIED."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from automotive.public_http import VIN_IN_TEXT

PARSER_VERSION = "knowledge-1"
PROMPT_VERSION = "independent-research-1"
VEHICLE_FIELDS = (
    "make", "model", "model_year", "trim", "generation", "engine_code", "displacement_l",
    "engine_cylinders", "aspiration", "fuel", "body", "transmission", "drive", "market",
)
ClaimKind = Literal[
    "vehicle_identity", "oil_viscosity", "manufacturer_approval", "capacity",
    "product_recommendation", "part_number", "fitment",
]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def private_text(value: Any, maximum: int = 4000) -> str:
    return VIN_IN_TEXT.sub("[VIN]", str(value or ""))[:maximum]


def public_url(value: Any) -> str:
    text = str(value or "")
    parsed = urlsplit(text)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or VIN_IN_TEXT.search(text)):
        return ""
    return text[:1000]


def normalize_vehicle(vehicle: dict) -> dict:
    result = {}
    for key in VEHICLE_FIELDS:
        value = vehicle.get(key, vehicle.get("fuel_type") if key == "fuel" else None)
        if value in (None, ""):
            continue
        if key in {"model_year", "engine_cylinders"}:
            if isinstance(value, bool):
                raise ValueError("Invalid vehicle number")
            result[key] = int(value)
        elif key == "displacement_l":
            result[key] = float(value)
        else:
            result[key] = unicodedata.normalize("NFKC", private_text(value, 160)).strip().casefold()
    return result


def request_vehicle(vehicle: dict, request: str) -> dict:
    """Literal qualifiers participate in identity even when an earlier parser omitted them."""
    result = normalize_vehicle(vehicle)
    match = re.search(r"\b(\d[.,]\d)\s*(?:l(?:iter|itre)?s?\b)?", request, re.I)
    if match and "displacement_l" not in result:
        result["displacement_l"] = float(match[1].replace(",", "."))
    for pattern, value in ((r"twin[ -]?turbo|biturbo", "twin turbo"),
                           (r"naturally aspirated", "naturally aspirated"),
                           (r"supercharged", "supercharged"), (r"\bturbo\b", "turbo")):
        if re.search(pattern, request, re.I):
            result.setdefault("aspiration", value)
            break
    return result


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Claim(Strict):
    claim_id: str
    kind: ClaimKind
    category: str
    slot: str
    value: dict[str, Any]
    source: str
    source_url: str
    provider_ids: dict[str, Any] = Field(default_factory=dict)
    observed_at: str
    parser_version: str = PARSER_VERSION
    restrictions: dict[str, Any] = Field(default_factory=dict)


class Citation(Strict):
    receipt_id: str
    quote: str = Field(min_length=1, max_length=2000)


class Verdict(Strict):
    claim_id: str
    verdict: Literal["accepted", "rejected", "insufficient_evidence"]
    applicability: dict[str, Any]
    unresolved_fields: list[str]
    scope_supported: bool
    citations: list[Citation]
    reason: str = Field(min_length=1, max_length=3000)


class Judgement(Strict):
    verdicts: list[Verdict]


def frozen_context(vehicle: dict, request: str, category: str, origins: dict,
                   provider_ids: dict | None = None) -> dict:
    normalized = request_vehicle(vehicle, request)
    return {
        "request": private_text(request), "vehicle": normalized, "category": category,
        "field_origins": {k: private_text(origins.get(k, "unknown"), 100) for k in normalized},
        "unresolved_fields": [k for k in VEHICLE_FIELDS if k not in normalized],
        "provider_ids": provider_ids or {}, "parser_version": PARSER_VERSION,
    }


def claim(kind: str, category: str, slot: str, value: dict, source: str, url: str,
          *, provider_ids: dict | None = None, restrictions: dict | None = None) -> dict:
    data = dict(kind=kind, category=category, slot=slot, value=value, source=source,
                source_url=public_url(url), provider_ids=provider_ids or {},
                restrictions=restrictions or {})
    return Claim(claim_id=digest(data), observed_at=datetime.now(UTC).isoformat(),
                 **data).model_dump()
