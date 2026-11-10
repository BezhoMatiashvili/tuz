"""Car models and their makers from Wikidata (CC0), with names in several languages.

Wikidata holds about 13,500 items typed "automobile model". For each one this importer
stores the label and aliases in the requested languages (English, Georgian and Russian
by default), the maker or brand, production years and predecessor/successor links, plus
the same names for every maker. The public SPARQL endpoint is queried in small pages
with a pause between requests; the 100 GB database dump is not needed.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable

import requests

from automotive.public_http import retry_delay
from parts_crawler.config import Settings
from parts_crawler.importers import require_schema
from parts_crawler.records import VehicleName
from parts_crawler.store import Store
from parts_crawler.term import Tracker

ENDPOINT = "https://query.wikidata.org/sparql"
SOURCE = "wikidata"
MODEL_CLASS = "Q3231690"  # "automobile model"
ENTITY = "http://www.wikidata.org/entity/"
LANGUAGES = ("en", "ka", "ru")
QID = re.compile(r"^Q\d{1,12}$")
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")
log = logging.getLogger("parts_crawler.importers.wikidata")

COUNT = "SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE { ?item wdt:P31 wd:%s }"
# Keyset paging on the item IRI: a page never shifts when Wikidata gains or loses items.
MODELS = """SELECT ?item ?maker ?brand ?inception ?start ?end ?discontinued ?follows ?followed
WHERE {
  { SELECT ?item WHERE { ?item wdt:P31 wd:%(model_class)s . FILTER(STR(?item) > "%(after)s") }
    ORDER BY STR(?item) LIMIT %(limit)d }
  OPTIONAL { ?item wdt:P176 ?maker }
  OPTIONAL { ?item wdt:P1716 ?brand }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P580 ?start }
  OPTIONAL { ?item wdt:P582 ?end }
  OPTIONAL { ?item wdt:P2669 ?discontinued }
  OPTIONAL { ?item wdt:P155 ?follows }
  OPTIONAL { ?item wdt:P156 ?followed }
}"""
NAMES = """SELECT ?item ?kind ?name WHERE {
  VALUES ?item { %(items)s }
  { ?item rdfs:label ?name . BIND("label" AS ?kind) }
  UNION { ?item skos:altLabel ?name . BIND("alias" AS ?kind) }
  FILTER(LANG(?name) IN (%(languages)s))
}"""


class Wikidata:
    """SPARQL client that paces itself and waits out rate limits and server errors."""

    def __init__(self, user_agent: str, pause: float = 5.0,
                 session: requests.Session | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent,
                                     "Accept": "application/sparql-results+json"})
        self.pause = pause
        self.sleeper = sleeper
        self._next = 0.0

    def ask(self, query: str, attempts: int = 6) -> list[dict]:
        for attempt in range(1, attempts + 1):
            wait = self._next - time.monotonic()
            if wait > 0:
                self.sleeper(wait)
            self._next = time.monotonic() + self.pause
            try:
                response = self.session.post(ENDPOINT, data={"query": query}, timeout=(10, 90))
            except requests.RequestException as error:
                delay = min(300.0, 10.0 * 2 ** (attempt - 1))
                log.warning("wikidata: %s; trying again in %.0f s (attempt %s of %s)",
                            type(error).__name__, delay, attempt, attempts)
                self._next = time.monotonic() + delay
                continue
            if response.status_code == 429 or response.status_code >= 500:
                delay = min(900.0, retry_delay(response.headers) if "retry-after" in
                            {key.lower() for key in response.headers}
                            else 30.0 * 2 ** (attempt - 1))
                log.warning("wikidata: HTTP %s; waiting %.0f s (attempt %s of %s)",
                            response.status_code, delay, attempt, attempts)
                self._next = time.monotonic() + delay
                continue
            response.raise_for_status()
            return response.json()["results"]["bindings"]
        raise SystemExit("Wikidata did not answer. Run the same command again later; "
                         "it continues from the last saved page.")


def qid(value: str) -> str:
    """'http://www.wikidata.org/entity/Q53268' -> 'Q53268'; anything else -> ''."""
    tail = value[len(ENTITY):] if value.startswith(ENTITY) else ""
    return tail if QID.match(tail) else ""


def year(value: str) -> int | None:
    """'1997-01-01T00:00:00Z' -> 1997; dates Wikidata marks unknown or BCE -> None."""
    return int(value[:4]) if len(value) >= 4 and value[:4].isdigit() else None


def collect_models(bindings: list[dict]) -> dict[str, dict]:
    """Group the row-per-combination result into one entry per model item."""
    fields = (("maker", "makers", qid), ("brand", "brands", qid), ("inception", "from", year),
              ("start", "from", year), ("end", "to", year), ("discontinued", "to", year),
              ("follows", "follows", qid), ("followed", "followed", qid))
    models: dict[str, dict] = {}
    for row in bindings:
        item = qid(row.get("item", {}).get("value", ""))
        if not item:
            continue
        entry = models.setdefault(item, {"makers": [], "brands": [], "from": [], "to": [],
                                         "follows": [], "followed": []})
        for key, target, convert in fields:
            value = convert(row[key].get("value", "")) if key in row else None
            if value and value not in entry[target]:
                entry[target].append(value)
    return models


def collect_names(bindings: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    """item -> [(language, 'label' | 'alias', text)]."""
    names: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in bindings:
        item = qid(row.get("item", {}).get("value", ""))
        literal = row.get("name", {})
        language = literal.get("xml:lang", "")
        text = " ".join(str(literal.get("value", "")).split())
        kind = row.get("kind", {}).get("value", "label")
        if item and language and text and kind in {"label", "alias"}:
            names[item].append((language, kind, text))
    return names


def english(names: dict, item: str) -> str:
    return next((text for language, kind, text in names.get(item, [])
                 if language == "en" and kind == "label"), "")


def make_of(label: str, fallback: str, known_makes: list[str]) -> str:
    """The make a model's English name starts with, else the brand or maker label.

    Wikidata often names only the company ("Toyota Motor Corporation") as manufacturer,
    while the model's own label ("Toyota Corolla") begins with the make drivers use.
    `known_makes` must be ordered longest first.
    """
    lowered = label.casefold()
    for make in known_makes:
        key = make.casefold()
        if lowered == key or lowered.startswith(key + " "):
            return make
    return fallback


def build(models: dict[str, dict], names: dict,
          known_makes: list[str] | None = None) -> list[VehicleName]:
    """Name rows for every model on the page and for the makers they refer to."""
    rows: list[VehicleName] = []
    owners: dict[str, None] = {}
    for item, entry in models.items():
        # The brand is what a driver calls the car (Lexus), the manufacturer may be the
        # parent company (Toyota), so the brand wins when both are present.
        candidates = entry["brands"] + entry["makers"]
        make = make_of(english(names, item), next(
            (english(names, owner) for owner in candidates if english(names, owner)), ""),
            known_makes or [])
        raw = {key: entry[key] for key in ("makers", "brands", "follows", "followed")
               if entry[key]}
        for language, kind, text in names.get(item, []):
            rows.append(VehicleName(
                ref=item, kind="model", language=language, name=text, name_kind=kind,
                make=make, year_from=min(entry["from"], default=None),
                year_to=max(entry["to"], default=None), raw=raw))
        owners.update(dict.fromkeys(candidates))
    for owner in owners:
        make = english(names, owner)
        for language, kind, text in names.get(owner, []):
            rows.append(VehicleName(ref=owner, kind="make", language=language, name=text,
                                    name_kind=kind, make=make))
    return rows


def import_wikidata(settings: Settings, *, restart: bool = False, page: int = 400,
                    languages: tuple[str, ...] = LANGUAGES,
                    client: Wikidata | None = None) -> dict:
    languages = tuple(code for code in languages if LANGUAGE.match(code)) or LANGUAGES
    client = client or Wikidata(settings.user_agent, pause=max(settings.min_delay, 5.0))
    store = Store(settings.database_url)
    totals = {"models": 0, "names": 0, "pages": 0, "resumed_at": 0}
    try:
        require_schema(store)
        store.ensure_source(SOURCE, 0)
        state = {} if restart else store.checkpoint(SOURCE)
        if state.get("complete"):
            log.info("wikidata: already complete (%s models)", state.get("items"))
            return totals | {"models": int(state.get("items", 0)),
                             "status": "already complete; use --restart to refresh"}
        after = str(state.get("after", ""))
        done = int(state.get("items", 0))
        totals["resumed_at"] = done
        total = int(state.get("total") or
                    client.ask(COUNT % MODEL_CLASS)[0]["n"]["value"])
        quoted = ", ".join(f'"{code}"' for code in languages)
        known_makes = store.known_makes()
        with Tracker("Wikidata car models", total, unit="models", done=done,
                     logger=log) as tracker:
            while True:
                models = collect_models(client.ask(MODELS % {
                    "model_class": MODEL_CLASS, "after": after, "limit": page}))
                if not models:
                    store.set_checkpoint(SOURCE, {"after": after, "items": done,
                                                  "total": total, "complete": True})
                    break
                wanted = sorted(set(models) | {owner for entry in models.values()
                                               for owner in entry["makers"] + entry["brands"]})
                names = collect_names(client.ask(NAMES % {
                    "items": " ".join("wd:" + item for item in wanted), "languages": quoted}))
                rows = build(models, names, known_makes)
                after = max(ENTITY + item for item in models)
                done += len(models)
                # Names and the cursor commit together: a stop between pages loses nothing.
                with store.conn.transaction():
                    written = store.upsert_vehicle_names(SOURCE, rows)
                    store.set_checkpoint(SOURCE, {"after": after, "items": done,
                                                  "total": total, "complete": False})
                totals["models"] += len(models)
                totals["names"] += written
                totals["pages"] += 1
                log.info("wikidata: page %s stored, %s models, %s names", totals["pages"],
                         len(models), written)
                tracker.advance(len(models), total=max(total, done))
    finally:
        store.close()
    return totals | {"status": "complete"}
