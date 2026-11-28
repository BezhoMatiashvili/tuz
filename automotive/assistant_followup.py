"""Facts repeated from customer messages and explicit clarification replies."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from automotive.assistant_oil_quote import requested_litres
from automotive.categories import FILTER_WORD, NOT_ENGINE_OIL, category_for_query
from automotive.dialogue import engine_details, read_vehicle
from automotive.oil_match import normalize_grade

IDENTITY = ("make", "model", "model_year")
DETAILS = ("engine_code", "trim", "displacement_l", "fuel", "body", "dpf", "market")
ENGINE_FIELDS = ("engine_code", "displacement_l", "fuel")
_YEAR = re.compile(r"(?<![\d./-])(?:19[89]\d|20\d\d)(?![\d./-])")
_ENGINE = re.compile(r"\b(?:engine\s+code|ძრავის\s+კოდი|код\s+двигателя)\s*[:=]?\s*"
                     r"([A-Za-z0-9][A-Za-z0-9.-]{1,15})", re.IGNORECASE)
_ENGINE_QUESTION = re.compile(r"engine|ძრავ|двигател", re.IGNORECASE)
_ENGINE_REPLACED = re.compile(
    r"ძრავი\s+(?:შეცვლილია|გამოცვლილია|შევცვალე|გამოვცვალე)|"
    r"\b(?:replaced|swapped) (?:the|my) engine\b|\bengine (?:was|has been) (?:replaced|swapped)\b",
    re.IGNORECASE,
)
_PRODUCT_FOLLOWUP = re.compile(
    r"\b(?:with (?:the )?filter|the same oil|that oil|და ფილტრ[ა-ჰ]*|ფილტრ[ა-ჰ]* ერთად|"
    r"ეს ზეთი|с фильтром|это масло)\b", re.IGNORECASE,
)
_VOLUME_FOLLOWUP = re.compile(r"\b(?:total|altogether|ჯამში|სულ|итого|вместе)\b",
                              re.IGNORECASE)
_OIL_GRADE = re.compile(r"(?<!\w)\d{1,2}W[- ]?\d{2}(?!\w)", re.IGNORECASE)
_GRADE_SHORTHAND = re.compile(
    r"(?<![\w./:\-])(?P<cold>0|5|10|15|20|25)"
    r"(?P<separator>[ \t]*(?:[/:.x×\-‐‑–—]|w[ \t]*\.)[ \t]*|[ \t]+)"
    r"(?P<hot>16|20|30|40|50|60)(?![\w/:\-]|\.\d)",
    re.IGNORECASE,
)
_CLOCK_OR_DATE = re.compile(
    r"\b(?:at|on|am|pm|appointment|book|booking)\b|"
    r"საათ|ჩაწერ|время|часов|запис", re.IGNORECASE,
)
_STOCK_REQUEST = re.compile(
    r"\b(?:do you have|in stock|available|got|price|how much)\b|გაქვთ|გაქ|მარაგ|რა\s*ღირს|"
    r"რა\s+ფასია|"
    r"\b(?:есть|в наличии|сколько стоит)\b",
    re.IGNORECASE,
)
_PART_NUMBER = re.compile(r"\b(?:part (?:number|code)|sku|oem|артикул)\b|ნაწილის კოდ",
                          re.IGNORECASE)
_AMOUNT_UNIT = re.compile(
    r"\s*(?:[₾$€]|(?:gel|lari|l|litre[s]?|liter[s]?|ლიტრ[ა-ჰ]*|ლარ[ა-ჰ]*|"
    r"литр[а-яё]*|лари)(?!\w))", re.IGNORECASE,
)
_OTHER_FLUID = re.compile(r"коробк|трансмисс|редуктор|гидравл", re.IGNORECASE)
_UNKNOWN_ENGINE = {"ka": "არ ვიცი", "en": "Not sure", "ru": "Не знаю"}
ENGINE_QUESTION = {
    "ka": "რომელი ძრავი აქვს მანქანას?",
    "en": "Which engine does your car have?",
    "ru": "Какой двигатель у вашей машины?",
}


def _key(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold()).replace("_", "")


def conflicts(before: dict, after: dict, keys: tuple[str, ...] = IDENTITY) -> bool:
    return any(before.get(key) and after.get(key) and not _same_detail(key, before[key], after[key])
               for key in keys)


def _same_detail(key: str, left: Any, right: Any) -> bool:
    if key == "displacement_l":
        try:
            first, second = (Decimal(str(value).replace(",", ".")) for value in (left, right))
            if first == second:
                return True
            # A customer's nominal 1.8 L can confirm a decoded 1.798 L. Two different
            # precise sizes still conflict, as do two distinct nominal sizes.
            if abs(first - second) < Decimal("0.05"):
                tenth = Decimal("0.1")
                return any(value == value.quantize(tenth) for value in (first, second))
            return False
        except InvalidOperation:
            return str(left) == str(right)
    first, second = _key(left), _key(right)
    return first == second or (key == "fuel" and {first, second} == {"petrol", "gasoline"})


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


def engine_question(text: str) -> bool:
    return bool(_ENGINE_QUESTION.search(text))


def engine_replaced(text: str) -> bool:
    return bool(_ENGINE_REPLACED.search(text))


def stated_engine(text: str, vehicle: dict, *, answering_engine: bool = False) -> dict:
    found = list(dict.fromkeys(code.rstrip(".") for code in _ENGINE.findall(text)))
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
    grades = oil_grades(text)
    return grades[0] if len(grades) == 1 else None


def oil_grades(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(grade for match in _OIL_GRADE.finditer(text)
                               if (grade := normalize_grade(match.group()))))


def normalize_oil_shorthand(text: str, *, oil_context: bool = False,
                            allowed: tuple[str, ...] = ()) -> str:
    """Normalize only plausible SAE shorthand in a recognized oil request."""
    value = unicodedata.normalize("NFKC", text)
    if not _GRADE_SHORTHAND.search(value):
        return text
    category = category_for_query(value)
    if (category not in (None, "engine_oil", "oil_change")
            or (category is None and re.search(FILTER_WORD, value, re.IGNORECASE))
            or re.search(NOT_ENGINE_OIL, value, re.IGNORECASE) or _OTHER_FLUID.search(value)):
        return text
    stock = category is None and stock_request(value) and not _PART_NUMBER.search(value)
    standalone = bool(_GRADE_SHORTHAND.fullmatch(value.strip(" ?!.")))
    explicit = (category in {"engine_oil", "oil_change"}
                or bool(re.search(r"\bмасло\b", value, re.IGNORECASE))
                or (oil_context and (allowed or stock or standalone)))
    scheduled = bool(_CLOCK_OR_DATE.search(value))

    def replace(match: re.Match) -> str:
        separator = match["separator"].strip()
        grade = f"{match['cold']}W-{match['hot']}"
        if allowed and grade not in allowed:
            return match.group()
        if ((scheduled and separator in ("/", ":"))
                or _AMOUNT_UNIT.match(value, match.end())):
            return match.group()
        if explicit or (stock and separator in ("/", "x", "X", "×", "-")):
            return grade
        return match.group()

    changed = _GRADE_SHORTHAND.sub(replace, value)
    return changed if changed != value else text


def stock_request(text: str) -> bool:
    return bool(_STOCK_REQUEST.search(text))


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
        normalized = normalize_oil_shorthand(text, oil_context=bool(oil))
        if grade := oil_grade(normalized):
            if grade != oil.get("oil_grade"):
                oil = {"oil_grade": grade}
            if litres := oil_litres(text, grade):
                oil["oil_litres"] = litres
    return {"vehicle": vehicle, **oil}
