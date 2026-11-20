"""RockAuto catalog tree: make, year, model, engine, category, part type (leaf listings).

Leaf pages list every aftermarket part for one vehicle and part type, so parts sharing
a vehicle and part type are alternatives. Each listing may also name the OE number it
fits and the alternate/OE numbers it replaces. Image paths under /info/ are disallowed
by robots.txt and are never requested.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlsplit

from parts_crawler.records import (
    CrossRef,
    Fitment,
    Link,
    ParseResult,
    Part,
    PartRef,
    Vehicle,
    clean,
    normalize_brand,
)
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority
from parts_crawler.sources.generic import text

PREFIX = "/en/catalog/"
KINDS = {0: "root", 1: "make", 2: "year", 3: "model", 5: "engine", 6: "category", 8: "parttype"}
NEXT_DEPTH = {0: 1, 1: 2, 2: 3, 3: 5, 5: 6, 6: 8}
OE_NUMBER = re.compile(r"OE\s*#\s*([A-Z0-9][A-Z0-9.\-/]{3,30})", re.I)


def raw_path(url: str) -> str:
    path = urlsplit(url).path
    return path[len(PREFIX):] if path.startswith(PREFIX) else ""


def segments(url: str) -> list[str]:
    raw = raw_path(url).strip("/")
    return [unquote_plus(part) for part in raw.split(",")] if raw else []


def priority(child: list[str]) -> int:
    rank = make_priority(child[0]) if child else 0
    penalty = 0
    if len(child) > 1 and child[1].isdigit():
        year = int(child[1])
        penalty = 0 if year >= 2005 else 5 if year >= 1995 else 15
    return rank * 100 + penalty * 5 + (8 - len(child))


class RockAutoAdapter(SourceAdapter):
    name = "rockauto"
    hosts = frozenset({"www.rockauto.com"})
    parser_version = "1"
    note = "Aftermarket listings per vehicle and part type, with OE and replaced numbers."
    # At the default 5 s pace the site stopped answering this address after ~620 pages
    # (2026-11-09). Its robots.txt sets no crawl delay; this pace and cap keep well below
    # that rate. The circuit breaker still pauses the source if it stops answering again.
    min_delay = 30.0
    daily_budget = 1500

    def seeds(self) -> list[Link]:
        return [Link("https://www.rockauto.com/en/catalog/", "root", priority=0)]

    def classify(self, url: str) -> str | None:
        if urlsplit(url).hostname not in self.hosts or not urlsplit(url).path.startswith(PREFIX):
            return None
        return KINDS.get(len(segments(url)))

    def parse(self, page: CrawledPage) -> ParseResult:
        soup = page.soup()
        result = ParseResult()
        current = raw_path(page.url).strip("/")
        depth = len(segments(page.url))
        expected = NEXT_DEPTH.get(depth)
        if expected is not None:
            seen: set[str] = set()
            for anchor in soup.select(f'a[href^="{PREFIX}"]'):
                href = anchor["href"].split("#")[0].split("?")[0]
                child_raw = href[len(PREFIX):].strip("/")
                if not child_raw or child_raw in seen:
                    continue
                if current and not child_raw.startswith(current + ","):
                    continue
                child = [unquote_plus(part) for part in child_raw.split(",")]
                if len(child) != expected:
                    continue
                seen.add(child_raw)
                result.links.append(Link(f"https://www.rockauto.com{PREFIX}{child_raw}",
                                         KINDS[expected], priority=priority(child)))
        if depth == 8:
            result.merge(self.listings(soup, segments(page.url)))
        return result

    def listings(self, soup, segs: list[str]) -> ParseResult:
        make, year, model, engine, carcode, _category, part_type, _ = segs
        result = ParseResult()
        year_value = int(year) if year.isdigit() else None
        result.vehicles.append(Vehicle(
            key=carcode, make=make.upper(), model=model.upper(), year_from=year_value,
            year_to=year_value, engine=engine.upper(), market="US",
            label=f"{year} {make} {model} {engine}".upper(), raw={"carcode": carcode}))
        for box in soup.select('[id^="listingcontainer["]'):
            brand = text(box.select_one(".listing-final-manufacturer"), 120)
            number = text(box.select_one(".listing-final-partnumber"), 60)
            if not brand or not number:
                continue
            notes = [text(note, 120) for note in box.select(".listing-footnote-text")]
            info = text(box.select_one(".listing-text-row"), 300)
            kind = "oe" if normalize_brand(brand) == normalize_brand(make) else "aftermarket"
            part = Part(brand, number, name=part_type.title(), kind=kind)
            result.parts.append(part)
            note = clean("; ".join(item for item in notes if item and item != "Note:"), 300)
            result.fitments.append(Fitment(part.ref, carcode, note=note,
                                           position=part_type.title()))
            for oe in OE_NUMBER.findall(info):
                result.cross_refs.append(CrossRef(part.ref, PartRef(make.upper(), oe), "oe"))
            replaced = box.find("span", title=re.compile(r"Replaces these", re.I))
            if replaced is not None:
                for other in replaced.get_text().split(","):
                    other = other.strip()
                    if other:
                        result.cross_refs.append(CrossRef(part.ref, PartRef("", other),
                                                          "replaces"))
        return result
