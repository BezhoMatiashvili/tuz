"""Clarification for engine-oil fitment when the current facts cannot support an offer."""

from __future__ import annotations

import re

from automotive.assistant_followup import oil_grade
from automotive.assistant_oil_quote import is_engine_oil
from automotive.categories import NOT_ENGINE_OIL, OIL_EXCLUSIONS, category_for_query

_FIT = re.compile(
    r"\b(?:(?:which|what)\s+(?:engine\s+)?oil|recommend|compatible|fits?|suitable)\b|"
    r"\bfor\s+(?:my|this)\s+(?:car|vehicle|engine)\b|რომელი|რა ზეთ|მირჩი|უხდება|მანქანისთვის|"
    r"ჩავასხ|подойд[её]т|какое|посовет", re.IGNORECASE,
)
_GENERIC = re.compile(
    r"(?:i need |i want )?(?:engine\s+)?oil|(?:ძრავის\s+)?ზეთი(?:\s+მინდა)?|"
    r"(?:нужно\s+)?(?:моторное\s+)?масло", re.IGNORECASE,
)
_UNKNOWN = re.compile(
    r"(?:(?:i\s+)?(?:don['’]?t know|do not know|not sure)|unsure|არ ვიცი|არ ვარ დარწმუნებული|"
    r"не знаю|не уверен[а]?|other|სხვა|другой)\s*[.!]?", re.IGNORECASE,
)
_SPEC = re.compile(
    r"\b(?:VW\s*\d{3}[. ]?\d{2}|MB(?:[- ]approval)?\s*\d{3}\.\d{1,2}|"
    r"(?:BMW\s*)?LL[- ]?(?:01|04|12|14|17|19)(?:\s*FE\+?)?|"
    r"ACEA\s*[AC]\d(?:/B\d)?|API\s*S[LMNPQ]|dexos\s*[12DR](?:\s*gen\s*\d)?|"
    r"WSS[- ]?M2C[\dA-Z-]+)(?![\w+])", re.IGNORECASE,
)

QUESTIONS = {
    "vehicle": (
        ("მომწერთ VIN-ს ან მანქანის მარკას, მოდელს და გამოშვების წელს?",
         "VIN", "მარკა, მოდელი და წელი", "არ ვიცი"),
        ("Can you send the VIN or the car’s make, model, and model year?",
         "VIN", "Make, model and year", "Not sure"),
        ("Можете прислать VIN или марку, модель и модельный год?",
         "VIN", "Марка, модель и год", "Не знаю"),
    ),
    "vehicle_after_vin": (
        ("VIN-ით მანქანის ზუსტი ვერსია ვერ დავადგინე. მომწერთ მოდელს და გამოშვების წელს?",
         "მოდელი და წელი", "არ ვიცი"),
        ("The VIN didn’t give me the exact car. Can you send the make, model, and model year?",
         "Make, model and year", "Not sure"),
        ("По VIN не удалось точно определить машину. Пришлите марку, модель и год?",
         "Марка, модель и год", "Не знаю"),
    ),
    "year": (
        ("რომელი წლისაა მანქანა?", "მოგწერთ წელს", "არ ვიცი"),
        ("What model year is the car?", "I’ll send the year", "Not sure"),
        ("Какой модельный год у машины?", "Пришлю год", "Не знаю"),
    ),
    "engine": (
        ("რომელი ძრავი აქვს მანქანას? მომწერეთ მოცულობა და საწვავი ან ძრავის კოდი.",
         "მოგწერთ ძრავის კოდს", "არ ვიცი"),
        ("Which engine does it have? The engine size and fuel or the engine code will help.",
         "I’ll send the engine code", "Not sure"),
        ("Какой двигатель установлен? Пришлите объём и тип топлива или код двигателя.",
         "Пришлю код двигателя", "Не знаю"),
    ),
    "engine_code": (
        ("მოცულობა და საწვავი ვიცით. ძრავის კოდსაც მომწერთ?",
         "მოგწერთ ძრავის კოდს", "არ ვიცი"),
        ("I have the engine size and fuel type. Can you also send the engine code?",
         "I’ll send the engine code", "Not sure"),
        ("Объём и тип топлива уже известны. Можете прислать код двигателя?",
         "Пришлю код двигателя", "Не знаю"),
    ),
    "dpf": (
        ("მანქანას დიზელის ნაწილაკების ფილტრი (DPF) აქვს?", "DPF აქვს", "DPF არ აქვს", "არ ვიცი"),
        ("Does the diesel have a particulate filter (DPF)?", "With DPF", "No DPF", "Not sure"),
        ("У дизеля есть сажевый фильтр (DPF)?", "С DPF", "Без DPF", "Не знаю"),
    ),
    "market": (
        ("რომელი ბაზრისთვის გამოვიდა მანქანა?", "აშშ", "ევროპა", "იაპონია", "სხვა", "არ ვიცი"),
        ("Which market was the car originally sold in?", "US", "Europe", "Japan", "Other",
         "Not sure"),
        ("Для какого рынка выпущена машина?", "США", "Европа", "Япония", "Другой", "Не знаю"),
    ),
    "spec": (
        ("მომწერთ ზეთის დაშვებას, რომელიც მანქანის სახელმძღვანელოში წერია?",
         "მოგწერთ დაშვებას", "არ ვიცი"),
        ("Can you send the oil approval or standard listed in the owner’s manual?",
         "I’ll send the oil spec", "Not sure"),
        ("Можете прислать допуск масла из руководства?", "Пришлю допуск", "Не знаю"),
    ),
}

FALLBACK = {
    "ka": "ამ ზეთის შესარჩევად მანქანის ზუსტი მოთხოვნა უნდა გადაამოწმოს მაღაზიამ.",
    "en": "The shop can help check the car’s oil requirement before you choose a bottle.",
    "ru": "Магазин поможет проверить требования к маслу перед покупкой.",
}

MATCH = {
    "ka": "ეს ზეთი მოცემულ მოთხოვნას შეესაბამება.",
    "en": "This oil matches the requirement we checked.",
    "ru": "Это масло соответствует проверенному требованию.",
}


def oil_candidate(name: str, code: str) -> bool:
    return is_engine_oil(name) or (bool(oil_grade(name) or oil_grade(code))
                                   and not re.search(OIL_EXCLUSIONS, name, re.IGNORECASE))


def fitment_request(text: str, has_car: bool) -> bool:
    category = category_for_query(text)
    if category not in {"engine_oil", "oil_change"}:
        if category is not None or not re.search(r"\bмасло\b", text, re.IGNORECASE):
            return False
        if re.search(r"коробк|трансмисс|редуктор", text, re.IGNORECASE):
            return False
    return (not re.search(NOT_ENGINE_OIL, text, re.IGNORECASE)
            and (bool(_FIT.search(text)) or bool(_GENERIC.fullmatch(text.strip(" ?.!")))
                 or (has_car and not oil_grade(text))))


def unsure(text: str) -> bool:
    return bool(_UNKNOWN.fullmatch(text.strip()))


def answers_question(text: str) -> bool:
    if "[VIN]" in text or unsure(text) or stated_spec(text):
        return True
    category = category_for_query(text)
    return (category in {"engine_oil", "oil_change"} or (category is None and bool(re.search(
        r"\d|\bdpf\b|\b(?:yes|no|petrol|gasoline|diesel|us|europe|japan)\b|"
        r"კი|არა|ბენზინ|დიზელ|აშშ|ევროპ|იაპონ|^да$|^нет$", text, re.IGNORECASE))))


def stated_market(text: str) -> str:
    return {
        "აშშ": "US", "us": "US", "usa": "US", "united states": "US", "сша": "US",
        "ევროპა": "Europe", "europe": "Europe", "европа": "Europe", "eu": "Europe",
        "იაპონია": "Japan", "japan": "Japan", "япония": "Japan", "jp": "Japan",
    }.get(text.strip().casefold(), "")


def stated_spec(text: str) -> str:
    matches = list(_SPEC.finditer(text))
    if len(matches) != 1 or re.match(r"\s*/\s*\d", text[matches[0].end():]):
        return ""
    return matches[0].group()


def states_spec(text: str, spec: str) -> bool:
    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9+]", "", value.casefold()).removeprefix("bmw")

    return any(key(match.group()) == key(spec) for match in _SPEC.finditer(text))


def needed(vehicle: dict) -> str:
    if not vehicle.get("make") or not vehicle.get("model"):
        return "vehicle"
    if not vehicle.get("model_year"):
        return "year"
    if not vehicle.get("engine_code"):
        return "engine_code" if vehicle.get("displacement_l") and vehicle.get("fuel") else "engine"
    return "spec"


def question(field: str, language: str) -> dict:
    title, *options = QUESTIONS[field][("ka", "en", "ru").index(language)]
    return {"text": title, "options": [{"label": option, "meaning": ""} for option in options]}
