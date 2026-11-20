"""In-memory inventory snapshot behind the public part search and the dashboard.

One read of ``warehouse.warehouse_products`` with the read-only runtime credential builds
an immutable ``Catalog``; visitor requests never query PostgreSQL. A background thread
rebuilds it when the synced catalogue changes (twice a day in production).

The public projection is deliberately narrow: an item leaves this module as name, vehicle
text, brand and price. Codes, quantities and identifiers stay with the staff views.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from automotive.categories import category_for_query, query_patterns
from automotive.codes import normalize_code
from automotive.name_match import OUT, year_fit
from automotive.rules import Rules
from automotive.stock import (
    IN_STORE,
    STORE_COLUMN,
    TUZ11_COLUMN,
    UNAVAILABLE,
    UNKNOWN,
    WITHIN_HOURS,
    age_hours,
    classify,
    parse_count,
)

LOGGER = logging.getLogger(__name__)
PRICE_COLUMN = "column_06"
PRICE = re.compile(r"^\s*(\d{1,9}(?:[.,]\d{1,4})?)\s*$")
VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
PAGE_SIZE = 20

# Canonical makes shown to customers.
MAKES = {
    "toyota": "Toyota", "lexus": "Lexus", "nissan": "Nissan", "infiniti": "Infiniti",
    "hyundai": "Hyundai", "kia": "Kia", "mercedes-benz": "Mercedes-Benz", "honda": "Honda",
    "acura": "Acura", "mitsubishi": "Mitsubishi", "bmw": "BMW", "mini": "MINI", "ford": "Ford",
    "volkswagen": "Volkswagen", "audi": "Audi", "skoda": "Skoda", "seat": "SEAT",
    "porsche": "Porsche", "subaru": "Subaru", "mazda": "Mazda", "suzuki": "Suzuki",
    "opel": "Opel", "chevrolet": "Chevrolet", "fiat": "Fiat", "jeep": "Jeep",
    "renault": "Renault", "land rover": "Land Rover", "tesla": "Tesla", "volvo": "Volvo",
    "chrysler": "Chrysler", "alfa romeo": "Alfa Romeo", "peugeot": "Peugeot",
    "citroen": "Citroen", "dodge": "Dodge", "isuzu": "Isuzu", "cadillac": "Cadillac",
    "jaguar": "Jaguar", "daewoo": "Daewoo", "maserati": "Maserati", "buick": "Buick",
    "iveco": "Iveco", "daihatsu": "Daihatsu", "hummer": "Hummer", "ssangyong": "SsangYong",
    "chery": "Chery", "great wall": "Great Wall", "gmc": "GMC", "lincoln": "Lincoln",
    "dacia": "Dacia", "smart": "Smart", "scion": "Scion",
}
# The catalogue's own spellings for the first word of its vehicle text, typos included.
TOKEN_MAKES: dict[str, tuple[str, ...]] = {
    **{name.upper().replace(" ", ""): (key,) for key, name in MAKES.items()},
    "MB": ("mercedes-benz",), "MERCEDES": ("mercedes-benz",), "MERCEDES-BENZ": ("mercedes-benz",),
    "MMC": ("mitsubishi",), "VW": ("volkswagen",), "WV": ("volkswagen",),
    "VAG": ("volkswagen", "audi", "skoda", "seat"),
    "HYNDAI": ("hyundai",), "HUYNDAI": ("hyundai",), "HYNDAY": ("hyundai",),
    "INFINITY": ("infiniti",), "PEGAUT": ("peugeot",), "SUZUKU": ("suzuki",),
    "LAND": ("land rover",), "LR": ("land rover",), "RANGE": ("land rover",),
    "ALFA": ("alfa romeo",), "GREAT": ("great wall",), "MODEL": ("tesla",),
    "CHEROKEE": ("jeep",), "CHEVY": ("chevrolet",), "GM": ("chevrolet",),
    "MAZDA6": ("mazda",), "SKOADA": ("skoda",), "SKO": ("skoda",),
}
VAG_BRANDS = {"VW": "volkswagen", "WV": "volkswagen", "VOLKSWAGEN": "volkswagen",
              "AUDI": "audi", "SKODA": "skoda", "SKOADA": "skoda", "SKO": "skoda",
              "SEAT": "seat", "PORSCHE": "porsche", "PORSHE": "porsche"}
# A bare `VAG A4,A6` or `VAG GOLF5` names no brand; the model does.
VAG_MODELS = (
    (re.compile(r"^(A[1-8]|Q[2-8]|TT|S[3-8]|RS\d)$"), "audi"),
    (re.compile(r"^(GOLF\w*|PASSAT\w*|JETTA|POLO|TOUAREG|TIGUAN|CADDY|T[4-6]|TRANSPORTER|AMAROK|"
                r"BORA|SHARAN|TOURAN|BEETLE|CC|CRAFTER|MULTIVAN|SCIROCCO|UP)$"), "volkswagen"),
    (re.compile(r"^(OCTAVIA|FABIA|SUPERB|RAPID|YETI|KODIAQ|KAROQ|ROOMSTER)$"), "skoda"),
    (re.compile(r"^(LEON|IBIZA|ALTEA|TOLEDO|ATECA)$"), "seat"),
    (re.compile(r"^(CAYENNE|MACAN|PANAMERA|BOXSTER|CAYMAN)$"), "porsche"),
)
# Engine and drivetrain words the catalogue writes after a model.
NOT_MODELS = {"FSI", "TSI", "TDI", "TFSI", "CDI", "XI", "XD", "AWD", "4WD", "2WD", "ABS",
              "NEW", "OLD", "ALL", "SET", "KIT"}
# Words that only complete a make name and are never a model.
MAKE_TAILS = {"ROVER", "ROMEO", "WALL", "BENZ"}
# How a decoder or a customer may spell a model the catalogue abbreviates, and the
# catalogue's own recurring misspellings folded onto the right model.
MODEL_ALIASES = {"LANDCRUISER": "LC", "LANDCRUISERPRADO": "PRADO", "SANTAFE": "SANTA",
                 "GRANDCHEROKEE": "CHEROKEE", "ODYSEY": "ODYSSEY", "PATHFAINDER": "PATHFINDER",
                 "PATHF": "PATHFINDER", "LEGAS": "LEGACY", "LEGASY": "LEGACY",
                 "ACORD": "ACCORD", "INSAIGHT": "INSIGHT", "DELIKA": "DELICA",
                 "AIRTEK": "AIRTREK", "AIRTRECK": "AIRTREK", "FITESTA": "FIESTA",
                 "MAVERIC": "MAVERICK", "OCTAV": "OCTAVIA", "STEPWGN": "STEPWGN"}
ENGINE_SIZE = re.compile(r"^\d+[.,]\d")
YEARISH = re.compile(r"^>?(19|20)\d\d[>+-]?$|^\d\d-(\d\d)?$|^(19|20)\d\d-((19|20)?\d\d)?$")


def model_key(text: str) -> str:
    """`CR-V`, `cr v` and `CRV` are one model; so are `RAV-4` and `RAV4`."""
    key = re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKC", str(text or "")).upper())
    return MODEL_ALIASES.get(key, key)


def vehicle_keys(model_text: str) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Canonical makes and (model key, catalogue spelling) pairs in a vehicle string.

    `TOYOTA/LEXUS LC200 2008>` gives (toyota, lexus) and LC200; `VAG VW GOLF5,JETTA` gives
    (volkswagen) and GOLF5, JETTA. Years and engine sizes are not models.
    """
    words = unicodedata.normalize("NFKC", str(model_text or "")).upper().replace(";", ",").split()
    if not words:
        return (), ()
    heads = [part.strip(" .,()") for part in words[0].split("/")]
    makes: list[str] = []
    for head in heads:
        makes.extend(TOKEN_MAKES.get(head, ()))
    rest = words[1:]
    if "VAG" in heads and rest and rest[0].strip(" .,") in VAG_BRANDS:
        makes = [VAG_BRANDS[rest[0].strip(" .,")]]
        rest = rest[1:]
    if not makes:
        return (), ()
    if "MAZDA6" in heads:
        rest = ["6", *rest]
    models: dict[str, str] = {}
    for word in rest:
        if ENGINE_SIZE.match(word):  # `3,5D`, `2.0FSI`: an engine, not a model list.
            continue
        for raw in re.split(r"[,/+]", word):
            raw = raw.strip(" .()<>")
            if not raw or raw in MAKE_TAILS or YEARISH.match(raw):
                continue
            key = model_key(raw)
            if ((len(key) >= 2 or key.isdigit()) and len(key) <= 12
                    and key not in TOKEN_MAKES and key not in NOT_MODELS):
                models.setdefault(key, raw)
    if "VAG" in heads and len(makes) > 1:
        named = [brand for key in models for pattern, brand in VAG_MODELS if pattern.match(key)]
        makes = named or makes
    return tuple(dict.fromkeys(makes)), tuple(models.items())


def parse_price(value: Any) -> float | None:
    match = PRICE.match(value) if isinstance(value, str) else None
    return float(match.group(1).replace(",", ".")) if match else None


@dataclass(frozen=True, slots=True)
class Item:
    sid: str
    code: str
    name: str
    model: str
    brand: str
    price: float | None
    store: float | None
    tuz11: float | None
    state: str
    fold: str  # casefolded name, vehicle text and brand, for matching
    makes: tuple[str, ...]
    models: tuple[str, ...]
    codes: tuple[str, ...]
    original_code: str = ""  # as the source writes it; read by automotive.assistant_catalog
    duplicate_code: str = ""

    def public(self) -> dict:
        # Exactly four fields. A zero or unreadable price is "price on request".
        return {"name": self.name, "fits": self.model, "brand": self.brand,
                "price": self.price if self.price else None}

    def staff(self) -> dict:
        return {"code": self.code, "name": self.name, "fits": self.model, "brand": self.brand,
                "price": self.price, "store": self.store, "warehouse": self.tuz11,
                "state": self.state}


def _label(value: Any) -> str:
    # Some source cells arrive HTML-escaped (`2005&gt;`); templates and the DOM escape again.
    return " ".join(html.unescape(str(value or "")).split())


def _item(row: dict, rules: Rules) -> Item:
    name, model, brand = (_label(row.get(key))
                          for key in ("product_name", "product_model", "brand"))
    store, tuz11 = parse_count(row.get("stock_store")), parse_count(row.get("stock_tuz11"))
    makes, models = vehicle_keys(model)
    codes: list[str] = []
    for field in ("product_code", "original_code", "duplicate_code"):
        whole = normalize_code(str(row.get(field) or ""))
        # The value as printed (`AP104/2`) and each alternative a slash separates.
        for part in (whole, *whole.split("/")):
            if len(part) >= 4 and part not in codes:
                codes.append(part)
    return Item(
        sid=str(row.get("source_product_id") or ""), code=str(row.get("product_code") or ""),
        name=name, model=model, brand=brand, price=parse_price(row.get("price_raw")),
        store=store, tuz11=tuz11,
        state=classify({"store": store, "tuz11": tuz11}, rules.min_store, rules.min_tuz11),
        fold=f"{name} {model} {brand}".casefold(), makes=makes,
        models=tuple(key for key, _ in models), codes=tuple(codes),
        original_code=str(row.get("original_code") or ""),
        duplicate_code=str(row.get("duplicate_code") or ""))


def _predicate(query: str) -> Any:
    """How a keyword narrows items: a known category's patterns, else every word present."""
    value = unicodedata.normalize("NFKC", query).strip().casefold()
    if category_for_query(value):
        includes, excludes = query_patterns(value)
        wanted = [re.compile(pattern, re.IGNORECASE) for pattern in includes]
        unwanted = [re.compile(pattern, re.IGNORECASE) for pattern in excludes]
        return lambda item: (all(p.search(item.fold) for p in wanted)
                             and not any(p.search(item.fold) for p in unwanted))
    words = value.split()
    return lambda item: all(word in item.fold for word in words)


class Catalog:
    """An immutable view of the inventory at one sync. Build with ``from_rows``."""

    RANK = {IN_STORE: 0, WITHIN_HOURS: 1, UNKNOWN: 2, UNAVAILABLE: 3}

    def __init__(self, items: list[Item], observed_at: datetime | None, rules: Rules) -> None:
        self.items, self.observed_at, self.rules = items, observed_at, rules
        self.by_id = {item.sid: item for item in items}
        # In the store first; within that, items with a price before "price on request".
        self.available = sorted(
            (item for item in items if item.state in (IN_STORE, WITHIN_HOURS)),
            key=lambda item: (self.RANK[item.state], not item.price, item.name, item.brand))
        spellings: dict[tuple[str, str], Counter] = defaultdict(Counter)
        make_counts: Counter = Counter()
        model_counts: dict[str, Counter] = defaultdict(Counter)
        brand_counts: Counter = Counter()
        for item in self.available:
            if item.brand:
                brand_counts[item.brand] += 1
            for make in item.makes:
                make_counts[make] += 1
                if item.models:
                    model_counts[make][item.models[0]] += 1
        # The spelling shown in the model list is the one the catalogue uses most, ignoring
        # misspellings that were folded onto the key.
        for item in self.available:
            if item.makes and item.models:
                key = item.models[0]
                raw = dict(vehicle_keys(item.model)[1]).get(key, key)
                if re.sub(r"[^A-Z0-9]", "", raw) == key:
                    for make in item.makes:
                        spellings[(make, key)][raw] += 1
        self._makes = [{"key": key, "name": MAKES[key], "count": count}
                       for key, count in make_counts.most_common() if count >= 3]
        self._models = {
            make: sorted(({"key": key, "count": count,
                           "name": (spellings[(make, key)].most_common(1) or [(key, 0)])[0][0]}
                          for key, count in counts.items() if count >= 2),
                         key=lambda entry: (-entry["count"], entry["name"]))[:60]
            for make, counts in model_counts.items()}
        self._brands = [name for name, _ in brand_counts.most_common(80)]

    @classmethod
    def from_rows(cls, rows: list[dict], rules: Rules | None = None) -> Catalog:
        rules = rules or Rules()
        stamps = [row["last_observed_at"] for row in rows
                  if isinstance(row.get("last_observed_at"), datetime)]
        return cls([_item(row, rules) for row in rows], max(stamps) if stamps else None, rules)

    # -- freshness ---------------------------------------------------------------------

    def freshness(self, now: datetime | None = None) -> str:
        """fresh, confirm (older than stale_hours) or stale (older than hide_after_hours)."""
        age = age_hours(self.observed_at, now or datetime.now(UTC))
        if age is None or age > self.rules.hide_after_hours:
            return "stale"
        return "confirm" if age > self.rules.stale_hours else "fresh"

    # -- public ------------------------------------------------------------------------

    def options(self) -> dict:
        return {"makes": self._makes, "brands": self._brands,
                "in_stock": len(self.available), "freshness": self.freshness()}

    def models(self, make: str) -> list[dict]:
        return self._models.get(make, [])

    def resolve_vehicle(self, make: str | None, model: str | None) -> tuple[str | None,
                                                                             str | None]:
        """Map a decoder's make and model onto this catalogue's keys, where it has them."""
        token = re.sub(r"[^A-Z0-9-]", "", str(make or "").upper().replace(" ", ""))
        found = TOKEN_MAKES.get(token, ())
        make_key = found[0] if len(found) == 1 else None
        if make_key is None:
            return None, None
        key = model_key(model or "")
        known = {entry["key"] for entry in self.models(make_key)}
        return make_key, key if key in known else None

    def _filter(self, pool: list[Item], *, make: str | None, model: str | None,
                year: int | None, query: str, brand: str | None) -> list[Item]:
        matched = pool
        if brand:
            wanted = brand.casefold()
            matched = [item for item in matched if item.brand.casefold() == wanted]
        if make:
            matched = [item for item in matched if make in item.makes]
        if model:
            key = model_key(model)
            matched = [item for item in matched if key in item.models]
            if year:
                # Look for the row's own spelling of the model (`CR-V` for the key `CRV`);
                # the key alone would not find its year range.
                matched = [item for item in matched if year_fit(
                    item.model, dict(vehicle_keys(item.model)[1]).get(key, model), year) != OUT]
        if query.strip():
            code = normalize_code(query)
            by_code = ([item for item in matched if code in item.codes]
                       if len(code) >= 4 and any(char.isdigit() for char in code) else [])
            if by_code:
                return by_code
            wanted_text = _predicate(query)
            matched = [item for item in matched if wanted_text(item)]
        return matched

    def listing(self, *, make: str | None = None, model: str | None = None,
                year: int | None = None, query: str = "",
                brand: str | None = None) -> list[Item]:
        """Every available match in the order the search shows them, without pages."""
        matched = self._filter(self.available, make=make, model=model, year=year,
                               query=query, brand=brand)
        # Two stock lines that read identically to a customer are shown once, where the
        # better-ranked of them sorts (an in-store line is not moved down by a warehouse one).
        unique: dict[tuple, Item] = {}
        for item in matched:
            unique.setdefault((item.name, item.model, item.brand, item.price or None), item)
        return list(unique.values())

    def search(self, *, make: str | None = None, model: str | None = None,
               year: int | None = None, query: str = "", brand: str | None = None,
               page: int = 1) -> dict:
        """Available items only, in-store first. Each item is name, fits, brand and price."""
        rows = self.listing(make=make, model=model, year=year, query=query, brand=brand)
        page = max(1, page)
        start = (page - 1) * PAGE_SIZE
        return {"items": [item.public() for item in rows[start:start + PAGE_SIZE]],
                "page": page, "total": len(rows),
                "has_more": start + PAGE_SIZE < len(rows),
                "freshness": self.freshness()}

    # -- staff -------------------------------------------------------------------------

    def inventory(self, *, query: str = "", brand: str | None = None, make: str | None = None,
                  state: str = "all", page: int = 1, size: int = 50) -> dict:
        pool = self.items
        if state == "available":
            pool = self.available
        elif state == "out":
            pool = [item for item in pool if item.state == UNAVAILABLE]
        matched = self._filter(pool, make=make, model=None, year=None, query=query, brand=brand)
        page, size = max(1, page), max(1, min(size, 100))
        start = (page - 1) * size
        return {"items": [item.staff() for item in matched[start:start + size]], "page": page,
                "total": len(matched), "has_more": start + size < len(matched)}


EMPTY = Catalog([], None, Rules())
# Literal column names keep this a constant statement; tests pin them to PRICE_COLUMN and
# automotive.stock's STORE_COLUMN and TUZ11_COLUMN.
SNAPSHOT_SQL = """SELECT source_product_id, product_code, product_name, product_model,
    original_code, duplicate_code, brand, last_observed_at,
    source_columns->>'column_06' AS price_raw,
    source_columns->>'column_10' AS stock_store,
    source_columns->>'column_12' AS stock_tuz11
    FROM warehouse.warehouse_products WHERE is_active"""
SNAPSHOT_COLUMNS = (PRICE_COLUMN, STORE_COLUMN, TUZ11_COLUMN)
# What says the catalogue changed (the number of products, the newest content change), and when
# the sync last looked (which moves on every page of every sync and needs no reload).
FINGERPRINT_SQL = ("SELECT count(*), max(last_changed_at), max(last_observed_at) "
                   "FROM warehouse.warehouse_products WHERE is_active")
# A sync commits page by page for half an hour: edits to existing products reload the snapshot
# at most this often (a product added or gone reloads at once). Syncs run twice a day.
MIN_RELOAD_SECONDS = 600.0


class CatalogStore:
    """Holds the current Catalog and rebuilds it off the request path when the sync moves."""

    def __init__(self, database_url: str, rules: Rules | None = None, *,
                 poll_seconds: float = 120.0,
                 min_reload_seconds: float = MIN_RELOAD_SECONDS) -> None:
        self.database_url = (database_url or "").strip()
        self.rules = rules or Rules()
        self.poll_seconds = poll_seconds
        self.min_reload_seconds = min_reload_seconds
        self._catalog = EMPTY
        self._fingerprint: Any = None
        self._loaded_at = 0.0
        self._hooks: list[Any] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.loaded = False

    def on_load(self, hook: Any) -> None:
        """``hook(catalog)`` runs on the refresh thread for each new snapshot before it is
        installed (the assistant builds its search index there, so no customer's turn waits
        for it). A snapshot that is already loaded gets it on a thread of its own."""
        self._hooks.append(hook)
        if self.loaded:
            threading.Thread(target=self._prepare, args=(self._catalog, (hook,)),
                             name="site-catalog-prepare", daemon=True).start()

    def _prepare(self, catalog: Catalog, hooks: Any = None) -> None:
        for hook in list(self._hooks if hooks is None else hooks):
            try:
                hook(catalog)
            except Exception as error:  # the snapshot is installed all the same
                LOGGER.warning("site_catalog_prepare_failed %s", type(error).__name__)

    def _observe(self, observed: Any) -> None:
        """The sync looked at the products again without changing them: only the snapshot's
        age moves (the freshness the site and the bot show), no reload."""
        current = self._catalog
        if isinstance(observed, datetime) and current is not EMPTY and (
                current.observed_at is None or observed > current.observed_at):
            current.observed_at = observed

    def current(self) -> Catalog:
        return self._catalog

    def replace(self, catalog: Catalog) -> None:
        """Install a prepared catalog (tests and tools)."""
        self._catalog, self.loaded = catalog, True

    def refresh(self) -> bool:
        """Reload when the catalogue changed. True when a new snapshot was installed."""
        if not self.database_url:
            return False
        with psycopg.connect(self.database_url, connect_timeout=5) as conn:
            conn.execute("SELECT set_config('statement_timeout', '20000', false)")
            count, changed, observed = conn.execute(FINGERPRINT_SQL).fetchone()
            fingerprint = (count, changed)
            if self.loaded and self._fingerprint is not None and (
                    fingerprint == self._fingerprint or (
                        count == self._fingerprint[0]
                        and time.monotonic() - self._loaded_at < self.min_reload_seconds)):
                self._observe(observed)
                return False
            with conn.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(SNAPSHOT_SQL).fetchall()
        catalog = Catalog.from_rows(rows, self.rules)
        self._prepare(catalog)  # before anyone can read it
        self._catalog = catalog
        self._fingerprint, self.loaded, self._loaded_at = fingerprint, True, time.monotonic()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                started = time.monotonic()
                if self.refresh():
                    LOGGER.info("site_catalog_loaded items=%d seconds=%.1f",
                                len(self._catalog.items), time.monotonic() - started)
            except Exception:  # Keep serving the last snapshot; details stay out of the log.
                LOGGER.warning("site_catalog_refresh_failed")
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self.database_url and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="site-catalog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class VinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vin: str = Field(min_length=17, max_length=17)


Text80 = Annotated[str | None, Query(max_length=80)]


def _litres(value: Any) -> str:
    """`2.998832712` from the decoder reads as `3.0 L`; anything unreadable is omitted."""
    try:
        return f"{float(value):.1f} L" if float(value) > 0 else ""
    except (TypeError, ValueError):
        return ""


def _plain(value: str | None) -> str | None:
    """Trimmed text, or None; control characters are a client error, not a search."""
    if value is None:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("control characters")
    return value.strip() or None


def build_catalog_router(store: CatalogStore, vin_service: Any, vin_budget: Any) -> APIRouter:
    """Public routes under /api/site. Rate limits and body caps are applied by the guard."""
    router = APIRouter(prefix="/api/site", include_in_schema=False)

    @router.get("/parts/options")
    def options() -> dict:
        return store.current().options()

    @router.get("/parts/models")
    def models(make: Annotated[str, Query(min_length=1, max_length=40)]) -> dict:
        return {"models": store.current().models(make.strip().lower())}

    @router.get("/parts/search")
    def search(make: Text80 = None, model: Text80 = None, q: Text80 = None,
               brand: Text80 = None,
               year: Annotated[int | None, Query(ge=1950, le=2100)] = None,
               page: Annotated[int, Query(ge=1, le=200)] = 1) -> dict:
        make_key = (_plain(make) or "").lower() or None
        if make_key and make_key not in MAKES:
            make_key = None
        return store.current().search(make=make_key, model=_plain(model), year=year,
                                      query=_plain(q) or "", brand=_plain(brand), page=page)

    # A decode asks an outside service and can hold a worker thread for seconds. The pages
    # run on the same threads, so only a few decodes run at once and the rest are told to
    # try again, without being charged.
    decoding = threading.BoundedSemaphore(4)

    @router.post("/vin")
    def vin(payload: VinBody) -> Any:
        from automotive.vin import ProviderUnavailable

        value = payload.vin.strip().upper()
        if not VIN.fullmatch(value):
            return JSONResponse({"detail": {"code": "INVALID_VIN"}}, status_code=422)
        if not decoding.acquire(blocking=False):
            return JSONResponse({"detail": {"code": "VIN_BUSY"}}, status_code=429)
        try:
            if not vin_budget.take():
                return JSONResponse({"detail": {"code": "VIN_BUDGET_EXHAUSTED"}},
                                    status_code=429)
            decoded = vin_service.decode(value, deadline=time.monotonic() + 8)
        except ProviderUnavailable:
            return JSONResponse({"detail": {"code": "VIN_PROVIDER_UNAVAILABLE"}},
                                status_code=503)
        finally:
            decoding.release()
        vehicle = decoded.get("vehicle") or {}
        make_key, model = store.current().resolve_vehicle(vehicle.get("make"),
                                                          vehicle.get("model"))
        engine = " ".join(str(part) for part in (
            _litres(vehicle.get("displacement_l")),
            vehicle.get("fuel") or "") if part).strip()
        # Only what the form needs: no trim, plant, checksum or provider details.
        return {"vehicle": {"make": vehicle.get("make"), "model": vehicle.get("model"),
                            "year": vehicle.get("model_year"), "engine": engine or None},
                "make_key": make_key, "model_key": model,
                "recognized": bool(vehicle.get("make") and vehicle.get("model"))}

    return router
