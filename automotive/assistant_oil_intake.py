"""Clarification for engine-oil fitment when the current facts cannot support an offer."""

from __future__ import annotations

import json
import re
from pathlib import Path

from automotive.assistant_followup import IDENTITY, conflicts, oil_grade
from automotive.assistant_oil_quote import is_engine_oil
from automotive.categories import NOT_ENGINE_OIL, OIL_EXCLUSIONS, category_for_query
from automotive.codes import normalize_code
from automotive.vin import VIN_IN_TEXT, vin_checksum

_FIT = re.compile(
    r"\b(?:(?:which|what)\s+(?:engine\s+)?oil|recommend|compatible|fits?|suitable)\b|"
    r"\bfor\s+(?:my|this)\s+(?:car|vehicle|engine)\b|რომელი|რა ზეთ|მირჩი|უხდება|მანქანისთვის|"
    r"ჩავასხ|подойд[её]т|какое|посовет", re.IGNORECASE,
)
_GENERIC = re.compile(
    r"(?:i need |i want )?(?:engine\s+)?oil|(?:ძრავის\s+)?ზეთი(?:\s+მინდა)?|"
    r"(?:dzravis\s+)?zeti(?:\s+minda)?|(?:нужно\s+)?(?:моторное\s+)?масло", re.IGNORECASE,
)
_STOCK_FOLLOWUP = re.compile(
    r"(?:is it )?(?:available|in stock)|do you have it|how much(?: is it)?|(?:the )?price|"
    r"(?:ეს |ეგ |ის )?(?:ზეთი )?(?:გაქვთ|გახვთ|გაქ|მარაგშია|რა ღირს|რაღირს|რა ფასია|ფასი)|"
    r"(?:оно )?в наличии|есть ли оно|сколько стоит|цена", re.IGNORECASE,
)
_UNKNOWN = re.compile(
    r"(?:(?:i\s+)?(?:don['’]?t know|do not know|not sure)|unsure|არ ვიცი|არ ვარ დარწმუნებული|"
    r"не знаю|не уверен[а]?|other|სხვა|другой)\s*[.!]?", re.IGNORECASE,
)
_SPEC = re.compile(
    r"\b(?:VW\s*\d{3}[. ]?\d{2}|MB(?:[- ]approval)?\s*\d{3}\.\d{1,2}|"
    r"(?:BMW\s*)?LL[- ]?(?:01|04|12|14|17|19)(?:\s*FE\+?)?|"
    r"ACEA\s*[AC]\d(?:/B\d)?|API\s*S[LMNPQ]|ILSAC\s*GF[- ]?[4567][AB]?|"
    r"dexos\s*[12DR](?:\s*gen\s*\d)?|"
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
ENGINE_CHANGED = {
    "ka": "ძრავის შესახებ თქვენი დაზუსტება ჩავიწერე. ამ ძრავისთვის ზეთი უნდა გადავამოწმოთ.",
    "en": "I saved your engine correction. The oil for that engine still needs a check.",
    "ru": "Я сохранил уточнение о двигателе. Масло для этого двигателя нужно проверить.",
}
_SUPPLIER_GRADES = {
    "ka": "ზეთის კატალოგში ამ მანქანისთვის მითითებულია {grades} ({sources}).",
    "en": "The oil selector lists {grades} for this vehicle ({sources}).",
    "ru": "Каталог масел указывает {grades} для этого автомобиля ({sources}).",
}
_SUPPLIER_HANDOFF = {
    "ka": "ზუსტ დაშვებას და შესაფერის პროდუქტს მარაგში მაღაზია გადაამოწმებს.",
    "en": "The shop needs to verify the required approval and a matching stocked oil.",
    "ru": "Магазину нужно проверить допуск и подобрать подходящее масло в наличии.",
}


def supplier_reply(needs: dict, language: str, *, unconfirmed_stock: bool = False) -> str:
    summary = _SUPPLIER_GRADES[language].format(
        grades=", ".join(needs["grades"]), sources=", ".join(needs["sources"]),
    )
    if capacity := needs.get("capacity_l"):
        if 0 < capacity <= 100:
            summary += "\n" + _SPECIFICATION[language][2].format(litres=capacity)
    if unconfirmed_stock:
        summary += "\n" + _SUPPLIER_HANDOFF[language]
    return summary

MATCH = {
    "ka": "ეს ზეთი მოცემულ მოთხოვნას შეესაბამება.",
    "en": "This oil matches the requirement we checked.",
    "ru": "Это масло соответствует проверенному требованию.",
}

_REFERENCES = {
    entry["descriptor"]: entry for entry in json.loads(
        Path(__file__).with_name("oil_vehicle_references.json").read_text(encoding="utf-8")
    )
}
_ENGINE_ALIASES = {entry["engine_code"]: frozenset(entry.get("aliases", []))
                   for entry in _REFERENCES.values()}
_SPECIFICATION = {
    "ka": ("ძრავის ზეთის დაშვება — {approval}.", "დაშვებული სიბლანტე — {grades}.",
           "ფილტრის შეცვლით დაახლოებით {litres:g} ლიტრი.",
           "მარაგში ამ დაშვების მქონე ზეთი ვერ დავადასტურე."),
    "en": ("Engine oil approval — {approval}.", "Allowed viscosities — {grades}.",
           "Approximately {litres:g} L with a filter change.",
           "I couldn’t verify a stocked oil with this approval."),
    "ru": ("Допуск моторного масла — {approval}.", "Допустимая вязкость — {grades}.",
           "Около {litres:g} л при замене фильтра.",
           "Наличие масла с этим допуском не удалось подтвердить."),
}
_OR = {"ka": " ან ", "en": " or ", "ru": " или "}


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


def specification_reply(needs: dict, language: str, *, candidates: bool = False,
                        verified: bool = False) -> str:
    approval, grades, capacity, stock = _SPECIFICATION[language]
    alternatives = _OR[language].join(" + ".join(option) for option in needs["approval_options"])
    lines = [approval.format(approval=alternatives)]
    if needs.get("grades"):
        lines.append(grades.format(grades=", ".join(needs["grades"])))
    if needs.get("capacity_l"):
        lines.append(capacity.format(litres=needs["capacity_l"]))
    source_url = needs.get("source_url")
    if source_url:
        lines.append(f"{needs['sources'][0]} ({needs['engine_code']}) — {source_url}")
    if not verified:
        lines.append(PACKAGE_CHECK[language] if candidates else stock)
    return "\n".join(lines)


PACKAGE_CHECK = {
    "ka": "ეს ვარიანტი მარაგშია. გამოყენებამდე დაშვება შეფუთვაზე უნდა გადამოწმდეს.",
    "en": "This option is in stock. Check the required approval on the bottle before use.",
    "ru": "Этот вариант есть в наличии. Перед применением проверьте допуск на упаковке.",
}
PACKAGES = {
    "ka": "შესავსებად საჭიროა {count} ცალი {size:g}-ლიტრიანი შეფუთვა.",
    "en": "You’ll need {count} × {size:g} L bottles for the refill.",
    "ru": "Для замены нужно {count} упаковки по {size:g} л.",
}

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
OIL_GROUP = {"ka": "ძრავის ზეთი", "en": "Engine oil", "ru": "Моторное масло"}


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


def generic_request(text: str) -> bool:
    text = VIN_IN_TEXT.sub("", text).replace("[VIN]", "")
    return bool(_GENERIC.fullmatch(text.strip(" ?.!")))


def stock_followup(text: str) -> bool:
    return bool(_STOCK_FOLLOWUP.fullmatch(text.strip(" ?.!")))


def oil_candidate(name: str, code: str) -> bool:
    return is_engine_oil(name) or (bool(oil_grade(name) or oil_grade(code))
                                   and not re.search(OIL_EXCLUSIONS, name, re.IGNORECASE))


def fitment_request(text: str, has_car: bool) -> bool:
    if generic_request(text):
        return True
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
