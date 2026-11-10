"""Plain records an adapter returns from one page; the store resolves their ids."""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from automotive.codes import normalize_code


def normalize_brand(value: str) -> str:
    """'Citroën' -> 'CITROEN', 'Mercedes-Benz' -> 'MERCEDESBENZ'.

    Accents are folded to their base letter so 'Škoda' and 'Skoda' meet; without that
    the accented letter would simply be dropped ('KODA').
    """
    folded = unicodedata.normalize("NFKD", value or "")
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", "", plain.upper())


def normalize_name(value: str) -> str:
    """Case-folded letters and digits of any script, for matching names such as 'ტოიოტა'."""
    folded = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(char for char in folded if char.isalnum())


def ascii_url(url: str) -> str:
    """Percent-encode non-ASCII, whitespace and control characters; the rest stays as written."""
    if url.isascii() and url.isprintable() and " " not in url:
        return url
    return quote(url, safe=string.punctuation)


def bounded(value: Any, low: int, high: int) -> int | None:
    """The integer when it lies in the column's allowed range, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def clean(value: Any, maximum: int = 300) -> str:
    # NUL is collapsed too: PostgreSQL text cannot hold it.
    text = re.sub(r"[\s\x00]+", " ", str(value or "")).strip()
    return text[:maximum]


@dataclass(frozen=True)
class Link:
    url: str
    kind: str
    priority: int = 100
    context: dict = field(default_factory=dict, hash=False, compare=False)
    # Queue the URL again when it is already known but with a different context (a
    # vehicle seeded again with another group filter). Default: a known URL is left alone.
    refresh: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class PartRef:
    brand: str
    number: str

    @property
    def key(self) -> tuple[str, str]:
        return normalize_brand(self.brand), normalize_code(self.number)

    @property
    def valid(self) -> bool:
        normalized = normalize_code(self.number)
        return 0 < len(normalized) <= 105 and any(char.isdigit() for char in normalized)


@dataclass
class Part:
    brand: str
    number: str
    name: str = ""
    kind: str = "unknown"

    @property
    def ref(self) -> PartRef:
        return PartRef(self.brand, self.number)


@dataclass
class Vehicle:
    key: str
    make: str
    model: str = ""
    year_from: int | None = None
    year_to: int | None = None
    engine: str = ""
    body: str = ""
    market: str = ""
    label: str = ""
    raw: dict = field(default_factory=dict)
    # Detail columns (migration 0011). Empty or None means the source did not state it.
    model_code: str = ""        # frame, chassis or full model code, e.g. NDE180L-DEFDXW
    engine_code: str = ""       # e.g. 1ND-TV
    displacement_cc: int | None = None
    power_kw: int | None = None
    fuel: str = ""
    transmission: str = ""
    drive: str = ""


@dataclass
class VehicleName:
    """A make or model name in one language, as a source spells it."""

    ref: str                    # the source's own id, e.g. a Wikidata QID
    kind: str                   # 'make' or 'model'
    language: str               # 'en', 'ka', 'ru', ...
    name: str
    name_kind: str = "label"    # 'label' or 'alias'
    make: str = ""              # for models: the maker's English name
    year_from: int | None = None
    year_to: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Fitment:
    part: PartRef
    vehicle_key: str
    note: str = ""
    position: str = ""
    quantity: str = ""


@dataclass
class CrossRef:
    part: PartRef
    other: PartRef
    relation: str = "equivalent"


@dataclass
class ParseResult:
    links: list[Link] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    vehicles: list[Vehicle] = field(default_factory=list)
    fitments: list[Fitment] = field(default_factory=list)
    cross_refs: list[CrossRef] = field(default_factory=list)

    def merge(self, other: ParseResult) -> ParseResult:
        self.links += other.links
        self.parts += other.parts
        self.vehicles += other.vehicles
        self.fitments += other.fitments
        self.cross_refs += other.cross_refs
        return self


def year_range(text: str) -> tuple[int | None, int | None]:
    """'10/91→12/94' -> (1991, 1994); '2015' -> (2015, 2015); '2015→' -> (2015, None)."""
    text = text or ""
    years = []
    for match in re.finditer(r"(?<!\d)(?:\d{1,2}/)?(\d{4}|\d{2})(?!\d)", text):
        value = int(match.group(1))
        if value < 100:
            value += 1900 if value > 30 else 2000
        if 1900 <= value <= 2100:
            years.append(value)
    if not years:
        return None, None
    if len(years) == 1:
        open_ended = bool(re.search(r"[-–→>]\s*$", text.strip()))
        return years[0], None if open_ended else years[0]
    return min(years), max(years)
