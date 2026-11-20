"""Editable landing-page content: the schema, packaged defaults and an in-memory snapshot.

Every text field is a Georgian/English pair. The dashboard edits documents through
``clean``; public pages read ``SiteContent.snapshot`` and never query the database per
visitor. Without a site database the packaged defaults are served, so the page still works.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

LOGGER = logging.getLogger(__name__)
LANGUAGES = ("ka", "en")
PHONE = r"^\+?[0-9][0-9 ()-]{5,23}$"
HTTPS_URL = r"^https://[a-z0-9][a-z0-9.-]*(?::443)?(?:[/?#][^\s\\<>\"']*)?$"
EMAIL = r"^[^@\s<>\"']{1,64}@[a-z0-9][a-z0-9.-]{1,80}\.[a-z]{2,24}$"
POSTAL_CODE = r"^[0-9]{4,6}$"
GEO = r"^-?[0-9]{1,2}\.[0-9]{3,8}, ?-?[0-9]{1,3}\.[0-9]{3,8}$"
_DAY = "(?:mo|tu|we|th|fr|sa|su)"
DAY_CODES = rf"^{_DAY}(?:-{_DAY})?(?:, ?{_DAY}(?:-{_DAY})?)*$"
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ]")


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    kind: str  # text | i18n | flag | list
    label: tuple[str, str]  # (ka, en) label shown in the dashboard
    maximum: int = 120
    multiline: bool = False
    words: int = 0  # word limit per language; 0 means none
    pattern: str = ""
    fields: tuple[Field, ...] = ()
    items: int = 0


@dataclass(frozen=True, slots=True)
class Section:
    label: tuple[str, str]
    fields: tuple[Field, ...]


def _i18n(name: str, ka: str, en: str, maximum: int = 120, **extra: Any) -> Field:
    return Field(name, "i18n", (ka, en), maximum, **extra)


def _text(name: str, ka: str, en: str, maximum: int = 120, **extra: Any) -> Field:
    return Field(name, "text", (ka, en), maximum, **extra)


SECTIONS: dict[str, Section] = {
    "brand": Section(("კონტაქტი და ბრენდი", "Contact and brand"), (
        _text("phone", "ტელეფონი", "Phone", 24, pattern=PHONE),
        _text("whatsapp", "WhatsApp ნომერი", "WhatsApp number", 24, pattern=PHONE),
        _text("email", "ელფოსტა", "Email", 120, pattern=EMAIL),
        _i18n("address", "მისამართი", "Address", 160),
        # The next three only feed what search engines read (structured data), never the page.
        _i18n("city", "ქალაქი", "City", 60),
        _text("postal_code", "საფოსტო ინდექსი", "Postal code", 10, pattern=POSTAL_CODE),
        _text("geo", "კოორდინატები Google-ისთვის (განედი, გრძედი)",
              "Coordinates for Google (latitude, longitude)", 40, pattern=GEO),
        _text("map_url", "რუკის ბმული (https)", "Map link (https)", 300, pattern=HTTPS_URL),
        Field("hours", "list", ("სამუშაო საათები", "Opening hours"), items=7, fields=(
            _i18n("days", "დღეები", "Days", 40),
            _text("time", "საათები", "Hours", 40),
            _text("days_code", "დღეები Google-ისთვის, მაგ. Mo-Sa", "Days for Google, e.g. Mo-Sa",
                  40, pattern=DAY_CODES),
        )),
        _text("facebook", "Facebook (https)", "Facebook (https)", 300, pattern=HTTPS_URL),
        _text("instagram", "Instagram (https)", "Instagram (https)", 300, pattern=HTTPS_URL),
        _text("tiktok", "TikTok (https)", "TikTok (https)", 300, pattern=HTTPS_URL),
        _text("currency", "ვალუტის ნიშანი", "Currency symbol", 4),
    )),
    "hero": Section(("მთავარი ეკრანი", "Hero"), (
        _i18n("title", "სათაური", "Headline", 40),
        _i18n("subline", "ქვესათაური (მაქს. 20 სიტყვა)", "Subline (20 words max)", 160,
              words=20),
    )),
    "checklist": Section(("ყვითელი ზოლი", "Checklist band"), (
        Field("items", "list", ("პუნქტები (4)", "Items (4)"), items=4, fields=(
            _i18n("label", "ტექსტი", "Label", 24),
        )),
    )),
    "engine": Section(("ძრავის სექცია", "Engine section"), (
        _i18n("title", "სათაური", "Heading", 40),
        _i18n("beat1_title", "ეტაპი 1: სათაური", "Beat 1: title", 40),
        _i18n("beat1_text", "ეტაპი 1: ტექსტი", "Beat 1: text", 140),
        _i18n("beat2_title", "ეტაპი 2: სათაური", "Beat 2: title", 40),
        _i18n("beat2_text", "ეტაპი 2: ტექსტი", "Beat 2: text", 140),
        _i18n("beat3_title", "ეტაპი 3: სათაური", "Beat 3: title", 40),
        _i18n("beat3_text", "ეტაპი 3: ტექსტი", "Beat 3: text", 140),
    )),
    "parts": Section(("ნაწილების ძიება", "Part search"), (
        _i18n("title", "სათაური", "Heading", 40),
        _i18n("intro", "შესავალი", "Intro", 160),
    )),
    "services_intro": Section(("სერვისების სექცია", "Services section"), (
        _i18n("title", "სათაური", "Heading", 40),
        _i18n("intro", "შესავალი", "Intro", 160),
    )),
    "story": Section(("ჩვენი ისტორია", "Our story"), (
        _i18n("title", "სათაური", "Heading", 40),
        _i18n("statement", "ტექსტი", "Statement", 400, multiline=True),
        Field("milestones", "list", ("ეტაპები (3-დან ჩნდება ქრონოლოგია)",
                                     "Milestones (a timeline appears from 3)"),
              items=12, fields=(
                  _text("year", "წელი", "Year", 9),
                  _i18n("title", "სათაური", "Title", 60),
                  _i18n("text", "ტექსტი", "Text", 240, multiline=True),
              )),
    )),
    "gallery": Section(("გალერეა", "Gallery"), (
        _i18n("title", "სათაური", "Heading", 40),
    )),
    "visit": Section(("კონტაქტის სექცია", "Visit section"), (
        _i18n("title", "სათაური", "Heading", 40),
        _i18n("note", "ტექსტი", "Note", 160),
    )),
    "seo": Section(("საძიებო სისტემები", "Search engines"), (
        _i18n("title", "გვერდის სათაური", "Page title", 70),
        _i18n("description", "აღწერა", "Description", 160),
    )),
    "chat": Section(("ჩატი", "Chat"), (
        Field("enabled", "flag", ("ჩატი ჩართულია", "Chat enabled")),
        _i18n("launcher", "ღილაკის ტექსტი", "Launcher label", 32),
        _i18n("greeting", "მისალმება", "Greeting", 200),
    )),
}


def _pair(ka: str, en: str) -> dict[str, str]:
    return {"ka": ka, "en": en}


DEFAULT_SECTIONS: dict[str, dict] = {
    "brand": {"phone": "", "whatsapp": "", "email": "", "address": _pair("", ""),
              "city": _pair("", ""), "postal_code": "", "geo": "", "map_url": "", "hours": [],
              "facebook": "", "instagram": "", "tiktok": "",
              "currency": "₾"},
    "hero": {
        "title": _pair("მეტი, ვიდრე ფარეხი.", "More than a garage."),
        "subline": _pair(
            "სერვისი, დიაგნოსტიკა, ძრავის აწყობა და ნაწილები მარაგში. ყველაფერი ერთ ჭერქვეშ.",
            "Service, diagnostics, engine builds and parts in stock, all under one roof."),
    },
    "checklist": {"items": [
        {"label": _pair("აწყობა", "Build")},
        {"label": _pair("სერვისი", "Service")},
        {"label": _pair("დიაგნოსტიკა", "Diagnostics")},
        {"label": _pair("სიმძლავრე", "Performance")},
    ]},
    "engine": {
        "title": _pair("კაპოტის ქვეშ", "Under the hood"),
        "beat1_title": _pair("ვიცით, რა არის შიგნით.", "We know what is inside."),
        "beat1_text": _pair("ყველა სამუშაო ძრავის დეტალურად ცოდნით იწყება.",
                            "Every job starts with knowing the engine part by part."),
        "beat2_title": _pair("ბოლო ჭანჭიკამდე.", "Down to the last bolt."),
        "beat2_text": _pair(
            "დიაგნოსტიკა, შეკეთება და აწყობა მათგან, ვინც ძრავებს ყოველდღე შლის.",
            "Diagnostics, repair and builds by people who take engines apart every day."),
        "beat3_title": _pair("ნაწილებიც ჩვენთანაა.", "And we stock the parts."),
        "beat3_text": _pair("მოძებნეთ VIN კოდით ან მანქანით და ნახეთ ფასი.",
                            "Search by VIN or by car and see the price."),
    },
    "parts": {
        "title": _pair("იპოვეთ ნაწილი", "Find your part"),
        "intro": _pair("მოძებნეთ ის, რაც ახლა მარაგშია. ხედავთ დასახელებას და ფასს.",
                       "Search what is in stock right now. You see the name and the price."),
    },
    "services_intro": {
        "title": _pair("რას ვაკეთებთ", "What we do"),
        "intro": _pair("ზეთის შეცვლიდან ძრავის სრულ აწყობამდე.",
                       "From a quick oil change to a full engine build."),
    },
    "story": {
        "title": _pair("რატომ 1886", "Why 1886"),
        "statement": _pair(
            "1886 წელს კარლ ბენცმა პირველი ავტომობილი დააპატენტა. ეს წელი ჩვენს სახელშია.",
            "1886 is the year Karl Benz patented the first automobile. "
            "We carry that year in our name."),
        "milestones": [],
    },
    "gallery": {"title": _pair("ჩვენი ფარეხი", "Inside the shop")},
    "visit": {
        "title": _pair("გვეწვიეთ", "Come by"),
        "note": _pair("დაგვირეკეთ, მოგვწერეთ ან პირდაპირ მობრძანდით.",
                      "Call, message or just drive in."),
    },
    # What Google shows as the result's title and snippet: the shop's services, its
    # neighbourhood and city, which is what people type ("ავტოსერვისი საბურთალო").
    "seo": {
        "title": _pair("Garage 1886 | ავტოსერვისი და ავტონაწილები, საბურთალო, თბილისი",
                       "Garage 1886 | Car service and auto parts in Saburtalo, Tbilisi"),
        "description": _pair(
            "ავტოსერვისი და ავტონაწილები ქავთარაძის 17-ში, საბურთალოზე: დიაგნოსტიკა, "
            "ზეთის შეცვლა, მუხრუჭები, სავალი ნაწილი, ძრავი. ნაწილების მარაგი და ფასები ონლაინ.",
            "Car service and auto parts at 17 Kavtaradze St, Saburtalo, Tbilisi: diagnostics, "
            "oil changes, brakes, suspension, engine work. Parts stock and prices online."),
    },
    "chat": {
        "enabled": True,
        "launcher": _pair("ჰკითხეთ ასისტენტს", "Ask the assistant"),
        "greeting": _pair("მომწერეთ მანქანა და საჭირო ნაწილი. ყველაზე ზუსტია VIN კოდი.",
                          "Tell me your car and the part you need. A VIN works best."),
    },
}

# Generic descriptions of what the shop's own murals advertise; prices stay empty until the
# owner enters real ones in the dashboard.
DEFAULT_SERVICES: list[dict] = [
    {"icon": "gauge", "title_ka": "დიაგნოსტიკა", "title_en": "Diagnostics",
     "summary_ka": "კომპიუტერული და ხელით დიაგნოსტიკა, სანამ რომელიმე ნაწილი შეიცვლება.",
     "summary_en": "Computer and hands-on diagnostics before any part is replaced."},
    {"icon": "drop", "title_ka": "ზეთი და ფილტრები", "title_en": "Oil and filters",
     "summary_ka": "ძრავის ზეთისა და ფილტრების შეცვლა თქვენი ძრავისთვის სწორი სპეციფიკაციით.",
     "summary_en": "Engine oil and filter changes with the right grade for your engine."},
    {"icon": "disc", "title_ka": "მუხრუჭები", "title_en": "Brakes",
     "summary_ka": "ხუნდები, დისკები და სამუხრუჭე სითხე: შემოწმება და შეცვლა.",
     "summary_en": "Pads, discs and brake fluid, checked and replaced."},
    {"icon": "steering-wheel", "title_ka": "სავალი ნაწილი", "title_en": "Suspension and steering",
     "summary_ka": "ამორტიზატორები, ბერკეტები, მილისები და საჭის მექანიზმის ნაწილები.",
     "summary_en": "Shocks, arms, bushings and steering parts."},
    {"icon": "engine", "title_ka": "ძრავის შეკეთება და აწყობა",
     "title_en": "Engine repair and build",
     "summary_ka": "საფენებიდან და გრმ-ის კომპლექტიდან სრულ აღდგენამდე.",
     "summary_en": "From gaskets and timing kits to a complete rebuild."},
    {"icon": "flag-checkered", "title_ka": "სიმძლავრე", "title_en": "Performance",
     "summary_ka": "გაუმჯობესებები მათთვის, ვისაც მანქანისგან მეტი სურს.",
     "summary_en": "Upgrades for drivers who want more from their car."},
    {"icon": "package", "title_ka": "სათადარიგო ნაწილები", "title_en": "Spare parts",
     "summary_ka": "ნაწილები მარაგში ყველაზე გავრცელებული მარკებისთვის.",
     "summary_en": "Parts in stock for the most common makes."},
]
SERVICE_ICONS = ("wrench", "gauge", "drop", "disc", "steering-wheel", "engine", "flag-checkered",
                 "package", "tire", "car-battery", "fan", "snowflake", "lightning", "headlights",
                 "toolbox", "car-profile", "gas-pump", "thermometer", "shield-check", "sparkle")
SERVICE_LIMITS = {"title_ka": 60, "title_en": 60, "summary_ka": 200, "summary_en": 200}


def _clean_text(value: Any, field: Field, errors: dict, path: str,
                blank: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[path] = "INVALID"
        return ""
    text = CONTROL.sub("", value)
    if field.multiline:
        text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
    else:
        text = " ".join(text.split())
    if len(text) > field.maximum:
        errors[path] = "TOO_LONG"
    elif field.words and len(text.split()) > field.words:
        errors[path] = "TOO_MANY_WORDS"
    elif text and field.pattern and not re.fullmatch(field.pattern, text, re.IGNORECASE):
        errors[path] = "INVALID"
    # ``blank`` is for stored text on its way to a page: what would be refused on save is
    # shown as empty, never as it stands.
    return "" if blank and path in errors else text


def _clean_field(value: Any, field: Field, errors: dict, path: str,
                 blank: bool = False) -> Any:
    if field.kind == "flag":
        if not isinstance(value, bool):
            errors[path] = "INVALID"
            return False
        return value
    if field.kind == "text":
        return _clean_text(value, field, errors, path, blank)
    if field.kind == "i18n":
        if not isinstance(value, dict):
            errors[path] = "INVALID"
            value = {}
        return {language: _clean_text(value.get(language), field, errors,
                                      f"{path}.{language}", blank)
                for language in LANGUAGES}
    if not isinstance(value, list):
        errors[path] = "INVALID"
        return []
    if len(value) > field.items:
        errors[path] = "TOO_MANY"
    rows = []
    for index, row in enumerate(value[:field.items]):
        if not isinstance(row, dict):
            errors[f"{path}.{index}"] = "INVALID"
            continue
        cleaned = {child.name: _clean_field(row.get(child.name), child, errors,
                                            f"{path}.{index}.{child.name}", blank)
                   for child in field.fields}
        # A row with nothing in it is an editor artefact, not an opening-hours line.
        if any(any(value.values()) if isinstance(value, dict) else value
               for value in cleaned.values()):
            rows.append(cleaned)
    return rows


def clean(key: str, document: Any, *,
          blank_invalid: bool = False) -> tuple[dict, dict[str, str]]:
    """A normalized document for ``key`` plus field-path error codes (never the values).

    The editor saves only a document with no errors. ``blank_invalid`` is for reading one
    back: text that breaks a rule comes out empty instead of unchecked.
    """
    section = SECTIONS.get(key)
    if section is None:
        return {}, {"": "UNKNOWN_SECTION"}
    if not isinstance(document, dict):
        return {}, {"": "INVALID"}
    errors: dict[str, str] = {}
    result = {field.name: _clean_field(document.get(field.name), field, errors, field.name,
                                       blank_invalid)
              for field in section.fields}
    return result, errors


def clean_service(payload: Any) -> tuple[dict, dict[str, str]]:
    """Validated service fields. Titles are required in both languages."""
    if not isinstance(payload, dict):
        return {}, {"": "INVALID"}
    errors: dict[str, str] = {}
    result: dict[str, Any] = {}
    for name, maximum in SERVICE_LIMITS.items():
        field = Field(name, "text", ("", ""), maximum)
        result[name] = _clean_text(payload.get(name), field, errors, name)
        if name.startswith("title") and not result[name] and name not in errors:
            errors[name] = "REQUIRED"
    icon = payload.get("icon", "wrench")
    if icon not in SERVICE_ICONS:
        errors["icon"] = "INVALID"
    result["icon"] = icon if icon in SERVICE_ICONS else "wrench"
    price = payload.get("price_from")
    if price in (None, ""):
        result["price_from"] = None
    elif isinstance(price, bool) or not isinstance(price, int | float) or not 0 <= price < 1e7:
        errors["price_from"] = "INVALID"
        result["price_from"] = None
    else:
        result["price_from"] = round(float(price), 2)
    published = payload.get("is_published", True)
    if not isinstance(published, bool):
        errors["is_published"] = "INVALID"
    result["is_published"] = published is True
    return result, errors


def describe_schema() -> dict:
    """The form description the dashboard renders its editors from."""
    def field(item: Field) -> dict:
        return {"name": item.name, "kind": item.kind,
                "label": dict(zip(LANGUAGES, item.label, strict=True)),
                "max": item.maximum, "multiline": item.multiline, "words": item.words,
                "items": item.items, "fields": [field(child) for child in item.fields]}
    return {"sections": [{"key": key, "label": dict(zip(LANGUAGES, section.label, strict=True)),
                          "fields": [field(item) for item in section.fields]}
                         for key, section in SECTIONS.items()],
            "icons": list(SERVICE_ICONS), "service_limits": SERVICE_LIMITS}


def pick(value: Any, language: str) -> str:
    """One language of a pair, falling back to the other so a page never shows a blank."""
    if isinstance(value, dict):
        other = "en" if language == "ka" else "ka"
        return str(value.get(language) or value.get(other) or "")
    return str(value or "")


def _localize(value: Any, language: str) -> Any:
    if isinstance(value, dict) and set(value) == set(LANGUAGES):
        return pick(value, language)
    if isinstance(value, dict):
        return {key: _localize(item, language) for key, item in value.items()}
    if isinstance(value, list):
        return [_localize(item, language) for item in value]
    return value


def localized(snapshot: dict, language: str) -> dict:
    """The public, single-language view of a snapshot: published items only.

    Every section keeps its own key (``gallery`` is the section with its heading). The
    published services are under ``services`` and the published photos under ``photos``,
    so a list can never replace a section a template reads fields from.
    """
    sections = {key: _localize(document, language)
                for key, document in snapshot["sections"].items()}
    services = [{"icon": row["icon"], "title": (row[f"title_{language}"] or row["title_en"]
                                                or row["title_ka"]),
                 "summary": (row[f"summary_{language}"] or row["summary_en"]
                             or row["summary_ka"]),
                 "price_from": float(row["price_from"]) if row.get("price_from") is not None
                 else None}
                for row in snapshot["services"] if row.get("is_published", True)]
    photos = [{"sha256": row["sha256"], "width": row["width"], "height": row["height"],
               "alt": (row.get(f"alt_{language}") or row.get("alt_en")
                       or row.get("alt_ka") or "")}
              for row in snapshot["media"] if row.get("is_published")]
    return {**sections, "services": services, "photos": photos}


def _filled(value: Any) -> bool:
    return any(value.values()) if isinstance(value, dict) else bool(value)


def missing_setup(snapshot: dict) -> list[str]:
    """Owner-only facts that are still empty; the dashboard shows them as a checklist."""
    brand, story = snapshot["sections"]["brand"], snapshot["sections"]["story"]
    todo = [name for name in ("phone", "map_url") if not brand.get(name)]
    if not any((brand.get("address") or {}).values()):
        todo.append("address")
    if not brand.get("hours"):
        todo.append("hours")
    if len(story.get("milestones") or []) < 3:
        todo.append("milestones")
    if not any(row.get("price_from") is not None for row in snapshot["services"]):
        todo.append("service_prices")
    return todo


def default_snapshot() -> dict:
    return {"sections": copy.deepcopy(DEFAULT_SECTIONS),
            "versions": dict.fromkeys(DEFAULT_SECTIONS, 0),
            "services": [{**service, "service_id": index, "price_from": None,
                          "position": index, "is_published": True, "version": 1}
                         for index, service in enumerate(DEFAULT_SERVICES, start=1)],
            "media": [], "source": "defaults"}


class SiteContent:
    """Caches the content snapshot; refreshed after dashboard edits and every ``ttl`` seconds."""

    def __init__(self, repository: Any = None, *, clock: Any = time.monotonic,
                 ttl: float = 60.0) -> None:
        self.repository, self.clock, self.ttl = repository, clock, ttl
        self._lock = Lock()  # guards the fields below; never held during a load
        self._reload = Lock()  # held by the one caller that is loading
        self._snapshot: dict | None = None
        self._loaded = 0.0
        self._edits = 0
        self._reported: dict[str, int] = {}

    def invalidate(self) -> None:
        with self._lock:
            # Not 0.0: a monotonic clock younger than ``ttl`` would still count as fresh.
            self._loaded = float("-inf")
            self._edits += 1

    def snapshot(self) -> dict:
        with self._lock:
            current, loaded = self._snapshot, self._loaded
            if current is not None and self.clock() - loaded < self.ttl:
                return current
        # A copy that is merely out of date is reloaded by one caller while everyone else
        # keeps the copy they have: a slow database must not queue every page behind it.
        # After an edit, and before the first load, callers wait for the new copy.
        must_wait = current is None or loaded == float("-inf")
        if not self._reload.acquire(blocking=must_wait):
            return current
        try:
            with self._lock:
                if self._snapshot is not None and self.clock() - self._loaded < self.ttl:
                    return self._snapshot  # loaded while this caller waited
                edits = self._edits
            loaded_now = self._fetch()
            with self._lock:
                self._snapshot = loaded_now or self._snapshot or default_snapshot()
                # An edit that landed while this load ran is not lost: the copy stays
                # marked out of date and the next reader loads again.
                self._loaded = self.clock() if edits == self._edits else float("-inf")
                return self._snapshot
        finally:
            self._reload.release()

    def _fetch(self) -> dict | None:
        """A fresh snapshot, or None when there is no database or it cannot be read; the
        caller then keeps the last good copy. Never a 500 on the home page."""
        if self.repository is None or not getattr(self.repository, "configured", False):
            return None
        try:
            return self._load()
        except Exception:
            LOGGER.warning("site_content_unavailable")
            return None

    def _load(self) -> dict:
        raw = self.repository.load_content()
        sections, versions, invalid = {}, {}, {}
        for key, default in DEFAULT_SECTIONS.items():
            stored = raw["sections"].get(key)
            # Rows are re-checked on the way out as well as on the way in: one written
            # around the editor, or under older limits, must not reach a page unchecked.
            document, errors = (clean(key, stored["document"], blank_invalid=True)
                                if stored else (None, {}))
            if errors:
                invalid[key] = len(errors)
            usable = stored is not None and "" not in errors
            sections[key] = document if usable else copy.deepcopy(default)
            # A field that ends up empty because of what was refused shows its packaged
            # text, so one bad row cannot leave the page without a headline.
            # A switch is never restored this way: a flag that cannot be read stays off.
            for name in {path.split(".")[0] for path in errors if path and usable}:
                if (not isinstance(default.get(name), bool) and not _filled(document.get(name))
                        and _filled(default.get(name))):
                    document[name] = copy.deepcopy(default[name])
            versions[key] = stored["version"] if stored else 0
        if invalid != self._reported:  # Said once per change, not on every reload.
            self._reported = invalid
            for key, count in invalid.items():
                LOGGER.warning("site_content_invalid section=%s fields=%d", key, count)
        return {"sections": sections, "versions": versions, "services": list(raw["services"]),
                "media": list(raw["media"]), "source": "database"}
