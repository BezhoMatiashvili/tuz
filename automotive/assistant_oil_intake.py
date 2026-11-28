"""Reviewed engine-oil facts for documented VIN series and bottle labels."""

from __future__ import annotations

import json
import re
from pathlib import Path

from automotive.assistant_followup import IDENTITY, conflicts, oil_grade
from automotive.assistant_oil_quote import is_engine_oil
from automotive.categories import OIL_EXCLUSIONS
from automotive.codes import normalize_code
from automotive.vin import vin_checksum

_SPEC = re.compile(
    r"\b(?:VW\s*\d{3}[. ]?\d{2}|MB(?:[- ]approval)?\s*\d{3}\.\d{1,2}|"
    r"(?:BMW\s*)?LL[- ]?(?:01|04|12|14|17|19)(?:\s*FE\+?)?|"
    r"ACEA\s*[AC]\d(?:/B\d)?|API\s*S[LMNPQ]|ILSAC\s*GF[- ]?[4567][AB]?|"
    r"dexos\s*[12DR](?:\s*gen\s*\d)?|"
    r"WSS[- ]?M2C[\dA-Z-]+)(?![\w+])", re.IGNORECASE,
)


_REFERENCES = {
    entry["descriptor"]: entry for entry in json.loads(
        Path(__file__).with_name("oil_vehicle_references.json").read_text(encoding="utf-8")
    )
}


_ENGINE_ALIASES = {entry["engine_code"]: frozenset(entry.get("aliases", []))
                   for entry in _REFERENCES.values()}


def reference_engine_matches(supplied: str | None, documented: str) -> bool:
    code = str(supplied or documented).upper()
    return code == documented or code in _ENGINE_ALIASES.get(documented, ())


def manufacturer_reference(vin: str | None, decoded: dict, stated: dict) -> dict | None:
    """Service facts for documented US-market VIN series and corroborating decoder facts."""
    if not vin or len(vin) != 17 or not re.fullmatch(r"[0-9X]", vin[8]):
        return None
    reference = _REFERENCES.get(f"{vin[:8]}*{vin[9:11]}")
    if not reference or not re.fullmatch(r"[0-9]{6}", vin[11:]):
        return None
    expected, displacement = reference["decoder"], reference["displacement_l"]
    engine_code = reference["engine_code"]
    fuels = reference.get("fuels", ("gasoline", "petrol"))
    if (vin_checksum(vin)["status"] != "MATCH"
            or str(decoded.get("fuel", "")).casefold() not in fuels
            or str(decoded.get("displacement_l", "")).rstrip("0").rstrip(".") != displacement
            or any(str(decoded.get(key, "")).casefold() != value
                   for key, value in expected.items())
            or conflicts(decoded, stated, (*IDENTITY, "displacement_l"))):
        return None
    for vehicle in (decoded, stated):
        if (str(vehicle.get("market") or "US").casefold() != "us"
                or not reference_engine_matches(vehicle.get("engine_code"), engine_code)
                or str(vehicle.get("fuel") or "gasoline").casefold() not in fuels):
            return None
    return {"engine_code": engine_code, "capacity_l": reference["capacity_l"],
            "approval_options": [list(option) for option in reference["approval_options"]],
            "grades": list(reference["grades"]), "sources": list(reference["sources"]),
            "source_url": reference["source_url"]}


# These are catalogue leads for a physical-label check. The underlying documentation
# identifies the formulations, while the card remains unverified for the actual package.
_STOCK_FORMULATIONS = (
    ("VW 502 00", "0W-40", "petronas", r"\bsyntium\s+supreme\s+e\b"),
    ("API CERTIFICATION SEAL", "0W-20", "petronas",
     r"\bsyntium\s+hybrid\b.*\bsp\s*/\s*dexos\s*1\b"),
    ("API CERTIFICATION SEAL", "0W-20", "mobil",
     r"\b831976\b.*\bafe\b.*\bgen\s*3\b"),
)


_MB_MOBIL_LABEL = re.compile(
    r"\b(?:831705|832142)\s*/\s*mobil-1\s+afs\b.*\b229\.5(?![\w.])", re.IGNORECASE,
)


def stock_candidate(name: str, brand: str, needs: dict) -> bool:
    grade = oil_grade(name)
    if not grade or grade not in needs.get("grades", []) or not oil_candidate(name, ""):
        return False
    if (["MB 229.5"] in needs["approval_options"] and grade == "0W-40"
            and brand.casefold() in {"mobil", "mobil-1"} and _MB_MOBIL_LABEL.search(name)):
        return True
    return any(
        all(states_spec(name, spec) for spec in option)
        or any(option == [approval] and grade == viscosity and brand.casefold() == maker
               and re.search(formulation, name, re.IGNORECASE)
               for approval, viscosity, maker, formulation in _STOCK_FORMULATIONS)
        for option in needs["approval_options"]
    )


def documented_standards(claims: list[str]) -> set[str]:
    """Industry standards usable as leads at the same grade, per the API Motor Oil Guide."""
    codes = {normalize_code(claim) for claim in claims}
    for series in (("APISJ", "APISL", "APISM", "APISN", "APISP", "APISQ"),
                   ("ILSACGF5", "ILSACGF6A", "ILSACGF7A")):
        newest = max((index for index, code in enumerate(series) if code in codes), default=-1)
        codes.update(series[:newest + 1])
    return codes


def oil_candidate(name: str, code: str) -> bool:
    return is_engine_oil(name) or (bool(oil_grade(name) or oil_grade(code))
                                   and not re.search(OIL_EXCLUSIONS, name, re.IGNORECASE))


def states_spec(text: str, spec: str) -> bool:
    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9+]", "", value.casefold()).removeprefix("bmw")

    return any(key(match.group()) == key(spec) for match in _SPEC.finditer(text))
