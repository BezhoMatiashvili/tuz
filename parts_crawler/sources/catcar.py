"""CatCar OEM catalogs (catcar.info): one URL scheme, a different step layout per brand.

Every catalog URL is `/<catalog>/?l=<base64>`. The base64 text is `key==value||...` and
holds the whole navigation state: `st` is the step number and `sts` a breadcrumb of
labels that the server does not need. A page's links fall into three groups:

* links to a higher step go deeper (market -> model -> modification -> groups -> parts);
* links to the same step that differ only in a `page..` key are tabs or pagination;
* everything else is breadcrumb navigation back up, which carries the deeper page's
  keys and so looks like a new URL for a page already seen. These are never followed;
  following them is what made the first adapter wander without reaching any part.

Steps below a catalog's `vehicle_step` are taxonomy and are crawled for every vehicle.
Part groups and part numbers are fetched only for vehicles seeded with `seed-vehicles`,
because one vehicle costs hundreds to thousands of requests at the 10 second pace that
robots.txt asks for. Toyota and Mitsubishi return part numbers from a separate request
per diagram row (`getArticles`, fetched as `...&st=81`); the other catalogs print them
in the page table. Labels are Russian or English depending on the catalog.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from parts_crawler.records import (
    Fitment,
    Link,
    ParseResult,
    Part,
    PartRef,
    Vehicle,
    clean,
    year_range,
)
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority
from parts_crawler.sources.generic import absolute, text

HOST = "www.catcar.info"
HOME = f"https://{HOST}/"
PAGE_KEY = re.compile(r"^page\d*$", re.I)
CALL = re.compile(r"getArticles\(\s*this\s*,\s*'([^']+)'")
# Consumables and wear parts, in the two languages the catalogs use for group names.
MAINTENANCE = (r"filter|фильтр|cleaner|brake|тормоз|spark|свеч|belt|ремень|ремн|wiper|щетк|"
               r"стеклоочист|clutch|сцеплен|shock|absorber|амортиз|bearing|подшипник|"
               r"water pump|водян|thermostat|термостат|pad|колодк|disc|диск")

# Part names worth one request each when a catalog serves numbers per diagram row.
WEAR_PARTS = (r"filter|element|pad|shoe|disc|rotor|drum|plug|belt|blade|absorber|clutch|"
              r"bearing|pump|thermostat|фильтр|колодк|диск|барабан|свеч|ремень|ремн|щетк|"
              r"амортиз|сцеплен|подшипник|насос|термостат")


def family_key(directory: str, state: dict[str, str]) -> str:
    """Vehicles that share a parts list closely enough to crawl one of them.

    Toyota lists every grade, gearbox and steering side as its own modification; the
    part rows returned for one of them already cover its frame and engine and state
    the remaining conditions in their notes.
    """
    if directory == "toyota":
        frame = re.sub(r"[LR]$", "", state.get("modifname", "").split("-", 1)[0])
        return "|".join((directory, state.get("market", ""), state.get("catalog", ""), frame,
                         state.get("engine", "")))
    if directory in {"hyundai", "kia"}:
        return f"{directory}|{state.get('cat', '')}"
    if directory == "honda":
        return "|".join((directory, state.get("year", ""), state.get("modname", ""),
                         state.get("doors", "")))
    return vehicle_id(directory, state)


NUMBER_HEADER = re.compile(
    r"(артикул|номер\s*(детали|запчасти)|part\s*(no|number|#)|article|oem)", re.I)
NAME_HEADER = re.compile(r"(наименование|название|описание|параметы запчасти|name|description)",
                         re.I)
QTY_HEADER = re.compile(r"(кол|qty|quantity)", re.I)
CODE_HEADER = re.compile(r"^(код|№|no\.?|code)$", re.I)
NOTE_HEADER = re.compile(r"(дата|date|period|примеч|дополн|сноск|замен|модел|верси|"
                         r"опцион|note|remark|info|option|альтернатив)", re.I)
MODEL = ("модель", "model")
DATES = ("дата производства", "production date")
SERIES = ("серия", "series")


@dataclass(frozen=True)
class Catalog:
    """How one brand's catalog is laid out.

    `fields` says where each typed vehicle field comes from: ("cell", "first" | "last",
    header names) reads a table cell on the way down, ("state", key) reads the vehicle
    link's state, ("label", index) takes a link label from the path.
    """

    directory: str
    make: str
    part_brand: str
    vehicle_step: int
    fields: dict[str, tuple] = field(default_factory=dict)
    # Levels below the vehicle that list broad groups ("Engine", "Body"); a group filter
    # starts to apply below them.
    main_group_levels: int = 1
    make_by: tuple[str, dict[str, str]] | None = None
    # Steps a link on the catalog's first page may lead to; empty means any.
    first_steps: frozenset[int] = frozenset()


CATALOGS: dict[str, Catalog] = {catalog.directory: catalog for catalog in (
    Catalog("toyota", "TOYOTA", "TOYOTA", 60, main_group_levels=0, fields={
        "model": ("cell", "first", MODEL), "model_code": ("state", "modifname"),
        "engine_code": ("state", "engine"), "dates": ("cell", "last", DATES),
        "market": ("state", "market")}),
    Catalog("nissan", "NISSAN", "NISSAN", 30, fields={
        "model": ("cell", "first", MODEL), "model_code": ("cell", "first", SERIES),
        "dates": ("cell", "first", DATES), "market": ("state", "region")},
        make_by=("region", {"cainf": "INFINITI", "elinf": "INFINITI", "erinf": "INFINITI",
                            "usinf": "INFINITI"})),
    Catalog("mitsubishi", "MITSUBISHI", "MITSUBISHI", 50, fields={
        "model": ("cell", "first", ("тип авто", "vehicle type")),
        "model_code": ("state", "Model"), "dates": ("cell", "first", DATES),
        "market": ("state", "market")}),
    Catalog("subaru", "SUBARU", "SUBARU", 40, fields={
        "model": ("cell", "first", MODEL), "model_code": ("cell", "first", SERIES),
        "dates": ("cell", "first", DATES), "market": ("state", "region")}),
    Catalog("honda", "HONDA", "HONDA", 40, fields={
        "model": ("state", "modname"), "model_code": ("state", "grade"),
        "transmission": ("state", "trans"), "dates": ("state", "year"),
        "market": ("state", "carea")}),
    Catalog("hyundai", "HYUNDAI", "HYUNDAI", 30, fields={
        "model": ("label", -1), "model_code": ("state", "cat"), "dates": ("label", -1),
        "market": ("state", "region")}),
    Catalog("kia", "KIA", "KIA", 30, fields={
        "model": ("label", -1), "model_code": ("state", "cat"), "dates": ("label", -1),
        "market": ("state", "region")}),
    # Only the USA market follows the model -> group layout captured here.
    Catalog("mazda", "MAZDA", "MAZDA", 30, first_steps=frozenset({20}), fields={
        "model": ("label", -1), "model_code": ("state", "catno"),
        "market": ("state", "market")}),
    Catalog("bmw", "BMW", "BMW", 50, fields={
        "model": ("state", "model"), "model_code": ("state", "bb"),
        "body": ("state", "emodel"), "market": ("state", "reg")},
        make_by=("bmt", {"Mini": "MINI", "Rolls-Royce": "ROLLS-ROYCE"})),
    Catalog("mercedes", "MERCEDES-BENZ", "MERCEDES-BENZ", 50, fields={
        "model": ("cell", "last", ("модификации", "modifications")),
        "model_code": ("cell", "last", ("модель авто", "vehicle model")),
        "market": ("state", "country")},
        make_by=("class", {"F": "SMART"})),
    Catalog("audivw", "VOLKSWAGEN", "VAG", 50, fields={
        "model": ("cell", "first", MODEL), "model_code": ("state", "mdl"),
        "dates": ("state", "Einsatz"), "market": ("state", "market")},
        make_by=("brand", {"au": "AUDI", "se": "SEAT", "sk": "SKODA", "vw": "VOLKSWAGEN"})),
)}


def _raw_state(url: str) -> str:
    # parse_qs would turn a base64 "+" into a space and corrupt the state.
    for pair in urlsplit(url).query.split("&"):
        if pair.startswith("l="):
            return unquote(pair[2:]).replace(" ", "+")
    return ""


def decode_step(url: str) -> dict[str, str]:
    """Every field of the `l` state, breadcrumb included."""
    raw = _raw_state(url)
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return {}
    fields = {}
    for pair in decoded.split("||"):
        key, _, value = pair.partition("==")
        if key:
            fields[key] = value
    return fields


def decode_state(url: str) -> dict[str, str]:
    """The navigation state in the server's key order, without the breadcrumb."""
    return {key: value for key, value in decode_step(url).items() if key != "sts"}


def step_labels(url: str) -> list[str]:
    try:
        trail = json.loads(decode_step(url).get("sts", "") or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(trail, dict):
        return []
    ordered = sorted(trail.items(), key=lambda item: int(item[0]) if item[0].isdigit() else 0)
    return [str(value) for _, value in ordered if value]


def encode_url(directory: str, state: dict[str, str], *, articles: bool = False) -> str:
    """A catalog URL rebuilt from its state alone.

    Dropping the breadcrumb gives one URL per page whatever path led to it, and keeps
    URLs short (Cyrillic labels in a breadcrumb cost about eight characters a letter).
    """
    plain = "||".join(f"{key}=={value}" for key, value in state.items())
    token = quote(base64.b64encode(plain.encode("utf-8")).decode("ascii"), safe="")
    return f"https://{HOST}/{directory}/?l={token}" + ("&st=81" if articles else "")


def directory_of(url: str) -> str:
    return (urlsplit(url).path.strip("/").split("/") or [""])[0]


def step_of(state: dict[str, str]) -> int:
    try:
        return int(state.get("st") or 0)
    except ValueError:
        return -1


def same_page_family(left: dict[str, str], right: dict[str, str]) -> bool:
    """True when two states differ only in tab or page keys."""
    def strip(state: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in state.items() if not PAGE_KEY.match(key)}
    return strip(left) == strip(right) and left != right


def vehicle_id(directory: str, state: dict[str, str]) -> str:
    plain = directory + ":" + "|".join(
        f"{key}={value.strip()}" for key, value in state.items()
        if key != "st" and not PAGE_KEY.match(key))
    if len(plain) <= 280:
        return plain
    return plain[:240] + "#" + hashlib.sha256(plain.encode()).hexdigest()[:16]


def row_cells(anchor: Tag) -> dict[str, str]:
    """header -> cell text for the table row a link sits in ({} outside a table)."""
    row = anchor.find_parent("tr")
    table = anchor.find_parent("table")
    if row is None or table is None:
        return {}
    first = table.find("tr")
    headers = [text(cell, 60).lower().rstrip(":") for cell in first.find_all(["th", "td"])] \
        if first is not None and first.find("th") else []
    cells = {}
    for index, cell in enumerate(row.find_all(["td", "th"])):
        value = text(cell, 200)
        if value:
            cells[headers[index] if index < len(headers) and headers[index]
                  else f"col{index}"] = value
    return cells


def legend(soup: BeautifulSoup) -> dict[str, str]:
    """code -> description from a two-column legend table (Toyota engine and option codes)."""
    codes: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows or len(rows[0].find_all(["th", "td"])) != 2:
            continue
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) == 2 and text(cells[0], 40) and text(cells[1], 200):
                codes.setdefault(text(cells[0], 40), text(cells[1], 200))
    return codes


def clean_number(value: str) -> str:
    """'(3C0010492K)' -> '3C0010492K': some catalogs bracket superseded numbers."""
    return value.strip().strip("()[]").strip()


def _valid(number: str) -> bool:
    return PartRef("", number).valid and len(PartRef("", number).key[1]) >= 4


def part_rows(soup: BeautifulSoup) -> list[dict]:
    """Rows of every table that has a part-number column.

    A "Код"/"№" column is the diagram callout, never the part number, so only a column
    named as an article or part number counts.
    """
    found: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [text(cell, 80) for cell in rows[0].find_all(["th", "td"])]
        number_col = next((i for i, h in enumerate(headers) if NUMBER_HEADER.search(h)), None)
        if number_col is None:
            continue
        name_col = next((i for i, h in enumerate(headers)
                         if i != number_col and NAME_HEADER.search(h)), None)
        qty_col = next((i for i, h in enumerate(headers) if QTY_HEADER.search(h)), None)
        code_col = next((i for i, h in enumerate(headers) if CODE_HEADER.search(h)), None)
        note_cols = [i for i, h in enumerate(headers)
                     if i not in {number_col, name_col, qty_col, code_col}
                     and NOTE_HEADER.search(h)]
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= number_col:
                continue
            number = clean_number(text(cells[number_col], 60))
            if not _valid(number):
                continue

            def cell(index: int | None, size: int, cells=cells) -> str:
                return text(cells[index], size) if index is not None and index < len(cells) \
                    else ""
            found.append({
                "number": number, "name": cell(name_col, 200), "quantity": cell(qty_col, 20),
                "callout": cell(code_col, 40),
                "note": "; ".join(value for value in (cell(i, 120) for i in note_cols)
                                  if value)})
    return found


def article_rows(soup: BeautifulSoup) -> list[dict]:
    """Rows of a `getArticles` fragment: part number first, then dates and applicability."""
    found: list[dict] = []
    for row in soup.find_all("tr"):
        cells = [text(cell, 160) for cell in row.find_all("td")]
        if not cells:
            continue
        number = clean_number(cells[0])
        if _valid(number):
            found.append({"number": number,
                          "note": "; ".join(value for value in cells[1:] if value)})
    return found


def _years(value: str) -> tuple[int | None, int | None]:
    """Production years from '06/2013-05/2016', '2012', '(2014-)' or '01.04.1990-03.06.2000'."""
    value = value or ""
    found = [int(year) for year in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", value)]
    if found:
        if len(found) == 1:
            open_ended = bool(re.search(r"(19|20)\d\d\s*[-–]\s*(\.\.\.|\)|$)", value.strip()))
            return found[0], None if open_ended else found[0]
        return min(found), max(found)
    return year_range(value)


class CatCarAdapter(SourceAdapter):
    name = "catcar"
    hosts = frozenset({HOST})
    parser_version = "2"
    daily_budget = 8000
    min_delay = 10.0
    # Every page carries a contact form with a reCAPTCHA widget; it is not a challenge.
    embeds_captcha_widget = True
    note = ("OEM catalogs for " + ", ".join(sorted(CATALOGS)) + ": vehicle codes for all, "
            "part numbers for seeded vehicles; 10 s crawl delay per robots.txt.")

    def seeds(self) -> list[Link]:
        return [Link(HOME, "home", priority=0)]

    def classify(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.hostname not in self.hosts:
            return None
        if parts.path.strip("/") == "":
            return "home"
        if directory_of(url) not in CATALOGS:
            return None
        return "articles" if re.search(r"(^|&)st=81(&|$)", parts.query) else "step"

    fair_split = 80
    auto_groups = MAINTENANCE
    auto_part_names = WEAR_PARTS

    def vehicle_seeds(self, vehicles: list[dict], pattern: str | None,
                      parts: str | None = None) -> list[Link]:
        """Queue the part groups of stored vehicles.

        `pattern` limits them by group name; `parts` limits, by part name, which diagram
        rows get their own part-number request.
        """
        links = []
        for row in vehicles:
            raw = row.get("raw") or {}
            directory, state = raw.get("catalog"), raw.get("state")
            if directory not in CATALOGS or not isinstance(state, dict) or not state:
                continue
            links.append(Link(encode_url(directory, state), "step", priority=5, context={
                "vehicle": row["source_vehicle_id"], "groups": [], "matched": False,
                "filter": pattern or "", "parts": parts or ""}))
        return links

    def family(self, vehicle: dict) -> str | None:
        raw = vehicle.get("raw") or {}
        directory, state = raw.get("catalog"), raw.get("state")
        if directory not in CATALOGS or not isinstance(state, dict) or not state:
            return None
        return family_key(directory, state)

    # Parsing -------------------------------------------------------------------
    def parse(self, page: CrawledPage) -> ParseResult:
        if page.kind == "home":
            return self._home(page)
        directory = directory_of(page.url)
        catalog = CATALOGS.get(directory)
        if catalog is None or page.kind not in {"step", "articles"}:
            return ParseResult()  # rows queued by the first adapter carry nothing usable
        if page.kind == "articles":
            return self._articles(page, catalog)
        return self._step(page, catalog)

    def _home(self, page: CrawledPage) -> ParseResult:
        result = ParseResult()
        seen: set[str] = set()
        for anchor in page.soup().find_all("a", href=True):
            url = absolute(page.url, anchor["href"], self.hosts)
            if not url or directory_of(url) not in CATALOGS:
                continue
            directory = directory_of(url)
            state = decode_state(url)
            target = encode_url(directory, state) if state else f"https://{HOST}/{directory}/"
            if target in seen:
                continue
            seen.add(target)
            label = text(anchor, 80)
            result.links.append(Link(
                target, "step", priority=self._priority(CATALOGS[directory], state, 0),
                context={"path": [{"st": step_of(state), "label": label, "cells": {}}]
                         if state and label else []}))
        return result

    def _priority(self, catalog: Catalog, state: dict[str, str], step: int) -> int:
        make = self._make(catalog, state)
        # Popular makes first; within a make, deeper pages first so vehicles appear early.
        return 100 + make_priority(make) * 10 - min(max(step, 0) // 10, 9)

    def _make(self, catalog: Catalog, state: dict[str, str]) -> str:
        if catalog.make_by:
            key, mapping = catalog.make_by
            return mapping.get(state.get(key, ""), catalog.make)
        return catalog.make

    def _step(self, page: CrawledPage, catalog: Catalog) -> ParseResult:
        soup = page.soup()
        result = ParseResult()
        state = decode_state(page.url)
        current = step_of(state)
        context = page.context or {}
        path = list(context.get("path") or [])
        vehicle = context.get("vehicle")
        groups = list(context.get("groups") or [])
        pattern = context.get("filter") or ""
        part_names = context.get("parts") or ""
        matched = bool(context.get("matched"))
        codes = legend(soup) if current < catalog.vehicle_step else {}
        seen: set[str] = {encode_url(catalog.directory, state)} if state else set()

        for anchor in soup.find_all("a", href=True):
            url = absolute(page.url, anchor["href"], self.hosts)
            if not url or directory_of(url) != catalog.directory:
                continue
            target_state = decode_state(url)
            step = step_of(target_state)
            if not target_state or step < 0:
                continue
            target = encode_url(catalog.directory, target_state)
            if target in seen:
                continue
            image = anchor.find("img")
            label = text(anchor, 120) or clean(
                (image.get("alt") or image.get("title") or "") if image else "", 120)
            if current < catalog.vehicle_step:
                # A link to the same step with another value (a model year, say) lists
                # more vehicles; a link to a lower step is the breadcrumb.
                if step < current or (step == current and target_state == state) or (
                        current == 0 and catalog.first_steps
                        and step not in catalog.first_steps):
                    continue
                seen.add(target)
                child = path + [{"st": step, "label": label, "cells": row_cells(anchor)}]
                if step >= catalog.vehicle_step:
                    result.vehicles.append(self._vehicle(catalog, target_state, child, codes))
                else:
                    result.links.append(Link(
                        target, "step", self._priority(catalog, target_state, step),
                        {"path": child[-6:]}))
            elif vehicle:
                # refresh: seeding the vehicle again with another filter re-reads its
                # group pages; part-number requests already made are not repeated.
                if step == current and same_page_family(state, target_state):
                    seen.add(target)
                    result.links.append(Link(target, "step", 5 + len(groups), {
                        "vehicle": vehicle, "groups": groups, "matched": matched,
                        "filter": pattern, "parts": part_names}, refresh=True))
                elif step > current and len(groups) < 6:
                    hit = bool(pattern) and re.search(pattern, label, re.I) is not None
                    if (pattern and not matched and not hit
                            and len(groups) >= catalog.main_group_levels):
                        continue
                    seen.add(target)
                    result.links.append(Link(target, "step", 5 + len(groups), {
                        "vehicle": vehicle, "groups": groups + [label],
                        "matched": matched or hit, "filter": pattern,
                        "parts": part_names}, refresh=True))

        if vehicle and current >= catalog.vehicle_step:
            position = clean(groups[-1] if groups else "", 200)
            for row in part_rows(soup):
                part = Part(catalog.part_brand, row["number"], name=row["name"], kind="oe")
                result.parts.append(part)
                result.fitments.append(Fitment(part.ref, vehicle, note=row["note"],
                                               position=position or row["name"],
                                               quantity=row["quantity"]))
            for row in soup.find_all("tr", onclick=True):
                call = CALL.search(row["onclick"])
                if not call:
                    continue
                # The handler's argument is already URL-encoded, like an href value.
                target_state = decode_state("https://x/?l=" + call.group(1))
                if not target_state:
                    continue
                cells = [text(cell, 120) for cell in row.find_all("td")]
                name = next((value for value in cells[1:] if value), "")
                # Each row costs one request, so a part-name filter skips the rest.
                if part_names and not re.search(part_names, name, re.I):
                    continue
                result.links.append(Link(
                    encode_url(catalog.directory, target_state, articles=True), "articles", 4,
                    {"vehicle": vehicle, "groups": groups,
                     "callout": cells[0] if cells else "", "name": name}))
        return result

    def _articles(self, page: CrawledPage, catalog: Catalog) -> ParseResult:
        result = ParseResult()
        context = page.context or {}
        vehicle = context.get("vehicle")
        if not vehicle:
            return result
        groups = context.get("groups") or []
        name = clean(context.get("name") or "", 300)
        position = clean(groups[-1] if groups else name, 200)
        for row in article_rows(page.soup()):
            part = Part(catalog.part_brand, row["number"], name=name, kind="oe")
            result.parts.append(part)
            result.fitments.append(Fitment(part.ref, vehicle, note=row["note"],
                                           position=position))
        return result

    def _vehicle(self, catalog: Catalog, state: dict[str, str], path: list[dict],
                 codes: dict[str, str]) -> Vehicle:
        def read(name: str) -> str:
            spec = catalog.fields.get(name)
            if not spec:
                return ""
            if spec[0] == "state":
                return clean(state.get(spec[1], ""), 200)
            if spec[0] == "label":
                return clean(path[spec[1]]["label"], 200) if path else ""
            entries = path if spec[1] == "first" else list(reversed(path))
            for entry in entries:
                for header, value in (entry.get("cells") or {}).items():
                    if header in spec[2] and value.strip(" -"):
                        return clean(value, 200)
            return ""

        make = self._make(catalog, state)
        model = read("model") or (path[-1]["label"] if path else "")
        if catalog.directory == "toyota" and model.upper().startswith("LEXUS "):
            make, model = "LEXUS", model[6:].strip()
        engine_code = read("engine_code")
        description = codes.get(engine_code, "") if engine_code else ""
        size = re.search(r"(\d{3,5})\s*CC", description, re.I)
        year_from, year_to = _years(read("dates"))
        labels = [entry["label"] for entry in path if entry.get("label")]
        return Vehicle(
            key=vehicle_id(catalog.directory, state), make=make, model=model,
            year_from=year_from, year_to=year_to, engine=description, body=read("body"),
            market=read("market"), label=" / ".join(labels),
            model_code=read("model_code"), engine_code=engine_code,
            displacement_cc=int(size.group(1)) if size else None,
            fuel="diesel" if "DIESEL" in description.upper() else "",
            transmission=read("transmission"),
            raw={"catalog": catalog.directory, "state": state, "path": path[-6:]})
