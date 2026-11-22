"""NiBK (JNBK Corporation) brake catalogue for cars: makers, models, fitments, parts.

The site's own pages call these endpoints. They were read from its script.js and
confirmed with a reverse-api-engineer browser capture on 2026-11-21. All are plain GETs:

  /catalogue/cars                                    the maker <select> (175 makers)
  /application/get_application_models/{maker}/1/0   every model of one maker
  /application/get_applications/{model}/1/0/0/0     every fitment of one model, with the
                                                     NiBK number for each brake position
  /catalogue/cars/brake/{part id}/{number}           one part: variants, cross references

`1` selects the car catalogue; `0` means "any" for series, year and engine volume. The
series level is skipped: series 0 already returns every model of a maker, and a model's
fitments are the same whichever series is named. The two /application/ endpoints answer
with an empty body unless the request carries `X-Requested-With: XMLHttpRequest`, which
the site's own pages send.

Part ids map one to one to part numbers, so each part page is queued once. Its vehicle
table repeats the model pages and is not read again. The part pages also list dimensions
(diameter, thickness, pad size); the parts table has no column for them yet, and the raw
pages are kept, so they can be parsed later.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from parts_crawler.records import (
    CrossRef,
    Fitment,
    Link,
    ParseResult,
    Part,
    PartRef,
    Vehicle,
    bounded,
    clean,
    normalize_brand,
)
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority
from parts_crawler.sources.generic import text, vehicle_key

HOST = "www.jnbk-brakes.com"
ROOT = f"https://{HOST}"
BRAND = "NiBK"
CATALOGUE_PATH = "/catalogue/cars"
MODELS = ROOT + "/application/get_application_models/{maker}/1/0"
APPLICATIONS = ROOT + "/application/get_applications/{model}/1/0/0/0"
MODELS_PATH = re.compile(r"/application/get_application_models/\d+/1/0")
APPLICATIONS_PATH = re.compile(r"/application/get_applications/\d+/1/0/0/0")
# Some numbers hold a slash ('PN1524/E'), so the number runs to the end of the path.
PART_PATH = re.compile(r"/catalogue/cars/brake/(\d+)/(.+)")
MONTH_YEAR = re.compile(r"(\d{1,2})\.(\d{2})")
VARIANT = re.compile(r"(.+?)\s*\[([^\]]+)\]")
GROUP = re.compile(r"\s*\(([^)]*)\)\s*$")


def full_year(two_digits: str) -> int:
    value = int(two_digits)
    return value + (2000 if value <= 40 else 1900)


def jnbk_years(value: str) -> tuple[int | None, int | None]:
    """'01.77~11.79' -> (1977, 1979); '03.20~' -> (2020, None); '05.04' -> (2004, 2004).

    The site writes month.two-digit-year; the shared `year_range` would read the months
    as years.
    """
    start, tilde, end = (value or "").partition("~")
    first, last = MONTH_YEAR.search(start), MONTH_YEAR.search(end)
    year_from = full_year(first.group(2)) if first else None
    if not tilde:
        return year_from, year_from
    return year_from, full_year(last.group(2)) if last else None


def base_make(make: str) -> str:
    """'TOYOTA (FAW)' -> 'TOYOTA': a joint venture's cars carry the parent's OE numbers."""
    return GROUP.sub("", make).strip() or make


def heading_make_model(value: str) -> tuple[str, str]:
    """'TOYOTA » Corolla' -> ('TOYOTA', 'Corolla')."""
    make, _, model = value.partition("»")
    return clean(make, 120), clean(model, 200)


def part_url(href: str) -> tuple[str, str] | None:
    """The canonical part page URL and its part number, or None for any other link."""
    parts = urlsplit(href or "")
    match = PART_PATH.fullmatch(parts.path)
    if parts.hostname != HOST or not match:
        return None
    return ROOT + parts.path, unquote(match.group(2))


def unslashed(number: str) -> str:
    return PartRef(BRAND, number).key[1].replace("/", "")


def kind_name(value: str) -> str:
    """'BRAKE PADS' -> 'Brake pads'."""
    return clean(value, 80).capitalize()


class JnbkAdapter(SourceAdapter):
    name = "jnbk"
    hosts = frozenset({HOST})
    parser_version = "1"
    request_headers = {"X-Requested-With": "XMLHttpRequest"}
    # The catalogue page loads reCAPTCHA for its enquiry form.
    embeds_captcha_widget = True
    note = ("NiBK brake pads, shoes and discs per vehicle, with OE and other-brand "
            "cross references (JNBK catalogue).")

    def seeds(self) -> list[Link]:
        return [Link(ROOT + CATALOGUE_PATH, "catalogue", priority=0)]

    def classify(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.hostname != HOST:
            return None
        path = parts.path.rstrip("/")
        if path == CATALOGUE_PATH:
            return "catalogue"
        if MODELS_PATH.fullmatch(path):
            return "models"
        if APPLICATIONS_PATH.fullmatch(path):
            return "applications"
        return "part" if PART_PATH.fullmatch(parts.path) else None

    def parse(self, page: CrawledPage) -> ParseResult:
        if not page.body.strip():
            # An /application/ call without the XHR header, or a part id the site lost.
            raise ValueError("empty response")
        kind = self.classify(page.url) or page.kind
        if kind == "catalogue":
            return self.catalogue(page)
        if kind == "models":
            return self.models(page)
        if kind == "applications":
            return self.applications(page)
        if kind == "part":
            return self.part(page)
        return ParseResult()

    def catalogue(self, page: CrawledPage) -> ParseResult:
        select = page.soup().select_one("select#selBrand")
        if select is None:
            raise ValueError("the catalogue page has no maker list")
        result = ParseResult()
        for option in select.find_all("option"):
            maker, make = option.get("value", "").strip(), text(option, 120)
            if maker.isdigit() and make:
                result.links.append(Link(MODELS.format(maker=maker), "models",
                                         priority=make_priority(make) * 10 + 3,
                                         context={"make": make}))
        return result

    def models(self, page: CrawledPage) -> ParseResult:
        fragment = page.json()
        if not isinstance(fragment, str):
            raise ValueError("the model list is not an HTML fragment")
        make = page.context.get("make", "")
        result = ParseResult()
        for option in BeautifulSoup(fragment, "html.parser").find_all("option"):
            model_id, model = option.get("value", "").strip(), text(option, 200)
            if model_id.isdigit() and model:
                result.links.append(Link(APPLICATIONS.format(model=model_id), "applications",
                                         priority=make_priority(make) * 10 + 2,
                                         context={"make": make, "model": model}))
        return result

    def applications(self, page: CrawledPage) -> ParseResult:
        if "<html" in page.text[:3000].lower():
            # The site answers unknown URLs with its home page and status 200.
            raise ValueError("a full page came back instead of the applications fragment")
        result = ParseResult()
        table = page.soup().select_one("table.search-result-table")
        if table is None:
            return result  # a model without any listed application
        positions = [text(header, 60) for header in table.select("th.thParts")]
        make, model = page.context.get("make", ""), page.context.get("model", "")
        parts: dict[str, Part] = {}
        queued: set[str] = set()
        for row in table.find_all("tr"):
            heading = row.select_one("h3")
            if heading is not None and "model-title" in (row.get("class") or []):
                make, model = heading_make_model(text(heading, 300))
                continue
            if row.select_one("td.tdYear") is None:
                continue
            vehicle = self.vehicle(row, make, model)
            result.vehicles.append(vehicle)
            for index, cell in enumerate(row.select("td.parts")):
                position = positions[index] if index < len(positions) else \
                    clean(cell.get("data-title", ""), 60)
                for anchor in cell.select("a.parts"):
                    found = part_url(anchor.get("href", ""))
                    number = text(anchor, 60)
                    if not number:
                        continue
                    part = parts.setdefault(number, Part(
                        BRAND, number, name=kind_name(anchor.get("title", "")),
                        kind="aftermarket"))
                    result.fitments.append(Fitment(part.ref, vehicle.key, position=position))
                    if found and found[0] not in queued:
                        queued.add(found[0])
                        result.links.append(Link(found[0], "part",
                                                 priority=make_priority(make) * 10 + 1,
                                                 context={"make": make}))
                # A plain number in a cell is the car maker's own part for that position.
                for genuine in cell.find_all("font"):
                    number = text(genuine, 60)
                    if number and make:
                        oe = parts.setdefault(f"oe:{number}", Part(base_make(make), number,
                                                                   kind="oe"))
                        result.fitments.append(Fitment(oe.ref, vehicle.key, position=position))
        result.parts = list(parts.values())
        return result

    def vehicle(self, row, make: str, model: str) -> Vehicle:
        years = text(row.select_one("td.tdYear"), 40)
        start = text(row.select_one("td.tdtest"), 20)
        volume = text(row.select_one("td.tdEngineVol"), 40)
        engine_no = text(row.select_one("td.tdEngineDetails, td.tdEngineNo"), 80)
        body = text(row.select_one("td.tdBody"), 80)
        year_from, year_to = jnbk_years(years)
        if year_from is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            year_from = int(start[:4])
        return Vehicle(
            key=vehicle_key([make, model, years, volume, engine_no, body]),
            make=make, model=model, year_from=year_from, year_to=year_to,
            engine=clean(f"{volume} {engine_no}", 200),
            label=clean(" ".join(v for v in (make, model, volume, engine_no, body, years) if v),
                        300),
            raw={"years": years, "start": start, "engine_volume": volume,
                 "engine_no": engine_no, "body": body},
            model_code=clean(body, 120), engine_code=clean(engine_no, 120),
            displacement_cc=bounded(int(volume), 50, 20000) if volume.isdigit() else None)

    def part(self, page: CrawledPage) -> ParseResult:
        found = part_url(page.url)
        soup = page.soup()
        title = text(soup.title, 300)
        # 'NiBK Brakes:: Application Cross Reference and Image for NiBK : BRAKE PADS : PN1524'
        # The title drops a number's slash ('PN1543E' for PN1543/E). The check ignores the
        # slash; the URL's spelling, the one the vehicle tables link to, is the one stored.
        pieces = [clean(piece, 120) for piece in title.split(":")]
        if found is None or len(pieces) < 3 or unslashed(pieces[-1]) != unslashed(found[1]):
            raise ValueError("the part page did not load (its title does not name the part)")
        part = Part(BRAND, found[1], name=kind_name(pieces[-2]), kind="aftermarket")
        result = ParseResult(parts=[part])
        # Owners that are car makers (and their groups, 'PONTIAC (GM)') are OE numbers;
        # AKEBONO, BENDIX, FMSI and the like are other brands' equivalents.
        makers: set[str] = set()
        for heading in soup.select("div.model-title h3"):
            make, _ = heading_make_model(text(heading, 300))
            makers.add(normalize_brand(base_make(make)))
            group = GROUP.search(make)
            if group:
                makers.add(normalize_brand(group.group(1)))
        makers.discard("")
        for plate in soup.select("div.detail__plate"):
            label = text(plate.select_one("p.box-title"), 60).lower()
            if label == "variation":
                for anchor in plate.select("a[href]"):
                    variant = part_url(anchor["href"])
                    match = VARIANT.fullmatch(text(anchor, 120))
                    if variant is None or match is None:
                        continue
                    other = PartRef(match.group(2), match.group(1))
                    if other.key != part.ref.key:
                        result.cross_refs.append(CrossRef(part.ref, other, "equivalent"))
                    result.links.append(Link(variant[0], "part", priority=page_priority(page),
                                             context=dict(page.context)))
            elif label == "cross reference":
                for entry in plate.select("div.str"):
                    owner = text(entry.select_one(".owner"), 120)
                    number = text(entry.select_one(".field"), 120)
                    if not owner or not number:
                        continue
                    other = PartRef(owner, number)
                    if other.key == part.ref.key:
                        continue  # the table rejects a part cross-referenced to itself
                    relation = "oe" if normalize_brand(owner) in makers else "equivalent"
                    result.cross_refs.append(CrossRef(part.ref, other, relation))
        return result


def page_priority(page: CrawledPage) -> int:
    return make_priority(page.context.get("make", "")) * 10 + 1
