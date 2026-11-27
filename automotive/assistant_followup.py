"""Facts repeated from customer messages and explicit clarification replies."""

from __future__ import annotations

import re
from typing import Any

from automotive.assistant_oil_quote import requested_litres
from automotive.dialogue import engine_details, read_vehicle
from automotive.oil_match import normalize_grade

IDENTITY = ("make", "model", "model_year")
DETAILS = ("engine_code", "trim", "displacement_l", "fuel", "body", "dpf", "market")
_YEAR = re.compile(r"(?<![\d./-])(?:19[89]\d|20\d\d)(?![\d./-])")
_ENGINE = re.compile(r"\b(?:engine\s+code|ძრავის\s+კოდი|код\s+двигателя)\s*[:=]?\s*"
                     r"([A-Za-z0-9][A-Za-z0-9-]{1,15})", re.IGNORECASE)
_ENGINE_QUESTION = re.compile(r"engine|ძრავ|двигател", re.IGNORECASE)
_PRODUCT_FOLLOWUP = re.compile(
    r"\b(?:with (?:the )?filter|the same oil|that oil|და ფილტრ[ა-ჰ]*|ფილტრ[ა-ჰ]* ერთად|"
    r"ეს ზეთი|с фильтром|это масло)\b", re.IGNORECASE,
)
_VOLUME_FOLLOWUP = re.compile(r"\b(?:total|altogether|ჯამში|სულ|итого|вместе)\b",
                              re.IGNORECASE)
_UNKNOWN_ENGINE = {"ka": "არ ვიცი", "en": "Not sure", "ru": "Не знаю"}
ENGINE_QUESTION = {
    "ka": "რომელი ძრავი აქვს მანქანას?",
    "en": "Which engine does your car have?",
    "ru": "Какой двигатель у вашей машины?",
}


def _key(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold()).replace("_", "")


def conflicts(before: dict, after: dict, keys: tuple[str, ...] = IDENTITY) -> bool:
    return any(before.get(key) and after.get(key) and _key(before[key]) != _key(after[key])
               for key in keys)


def stated_vehicle(text: str) -> dict:
    """Read one dated vehicle, or an undated model the parser recognizes by itself."""
    years = _YEAR.findall(text)
    if len(years) > 1:
        return {}
    parsed = read_vehicle(text)
    if not parsed.make or not parsed.model:
        return {}
    if not parsed.model_year:
        named = read_vehicle(parsed.model)
        if named.make != parsed.make or named.model != parsed.model:
            return {}
    return parsed.as_vehicle()


def stated_market(text: str) -> str:
    """The sales market a whole message names, as an answer to the question about it."""
    return {
        "აშშ": "US", "us": "US", "usa": "US", "united states": "US", "сша": "US",
        "ევროპა": "Europe", "europe": "Europe", "европа": "Europe", "eu": "Europe",
        "იაპონია": "Japan", "japan": "Japan", "япония": "Japan", "jp": "Japan",
    }.get(text.strip().casefold(), "")


def engine_question(text: str) -> bool:
    return bool(_ENGINE_QUESTION.search(text))


def stated_engine(text: str, vehicle: dict, *, answering_engine: bool = False) -> dict:
    found = list(dict.fromkeys(_ENGINE.findall(text)))
    if len(found) == 1 and re.search(r"[A-Za-z]", found[0]) and re.search(r"\d", found[0]):
        return {"engine_code": found[0].upper()}
    return engine_details(text, vehicle.get("make")) if answering_engine and not found else {}


def engine_options(variants: tuple[str, ...], language: str) -> list[dict[str, str]]:
    """Keep the full source title for a later lookup, with a short unique tap label."""
    unknown = _UNKNOWN_ENGINE[language]
    labels = [title if len(title) <= 40 else title[:39].rstrip() + "…" for title in variants]
    if not 2 <= len(labels) <= 7 or len({label.casefold() for label in [*labels, unknown]}) \
            != len(labels) + 1:
        return []
    return [{"label": label, "meaning": title} for label, title in
            zip(labels, variants, strict=True)] + [{"label": unknown, "meaning": ""}]


def oil_grade(text: str) -> str | None:
    grades = {normalize_grade(match.group()) for match in re.finditer(
        r"(?<!\w)\d{1,2}W[- ]?\d{2}(?!\w)", text, re.IGNORECASE)}
    return grades.pop() if len(grades) == 1 else None


def oil_litres(text: str, grade: str | None) -> float | None:
    amounts = requested_litres(text)
    return amounts.pop() if grade and len(amounts) == 1 else None


def refers_to_oil(text: str) -> bool:
    return bool(_PRODUCT_FOLLOWUP.search(text))


def asks_for_total(text: str) -> bool:
    return bool(_VOLUME_FOLLOWUP.search(text))


def from_transcript(transcript: Any) -> dict:
    """Recover explicit customer facts in saved conversations that predate this state."""
    vehicle: dict = {}
    oil: dict = {}
    for entry in transcript[-12:] if isinstance(transcript, list) else ():
        if not isinstance(entry, dict) or not isinstance(text := entry.get("user"), str):
            continue
        stated = stated_vehicle(text)
        engine = stated_engine(text, stated or vehicle)
        if conflicts(vehicle, stated):
            vehicle, oil = {}, {}
        elif conflicts(vehicle, {**stated, **engine}, DETAILS):
            vehicle = {key: vehicle[key] for key in IDENTITY if key in vehicle}
            oil = {}
        vehicle.update(stated)
        vehicle.update(engine)
        if grade := oil_grade(text):
            if grade != oil.get("oil_grade"):
                oil = {"oil_grade": grade}
            if litres := oil_litres(text, grade):
                oil["oil_litres"] = litres
    return {"vehicle": vehicle, **oil}
