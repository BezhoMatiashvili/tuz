"""NGK UK part finder: product type, make, model, year; the year page lists fitments.

Select options are plain URLs, so the tree is crawled with ordinary GET requests.
"""

from __future__ import annotations

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
    year_range,
)
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority
from parts_crawler.sources.generic import text, vehicle_key

ROOT = "https://www.ngkpartfinder.co.uk"
SEARCH = "/catalogues/cars/search/"
KINDS = {0: "catalogue", 1: "type", 2: "make", 3: "model", 4: "year"}


def segments(url: str) -> list[str]:
    path = urlsplit(url).path
    if not path.startswith(SEARCH):
        return []
    return [unquote_plus(part) for part in path[len(SEARCH):].strip("/").split("/") if part]


class NgkAdapter(SourceAdapter):
    name = "ngk"
    hosts = frozenset({"www.ngkpartfinder.co.uk"})
    parser_version = "1"
    note = "NGK/NTK spark plugs, glow plugs, coils and sensors per vehicle (UK catalogue)."

    def seeds(self) -> list[Link]:
        return [Link(f"{ROOT}/catalogues/cars/", "catalogue", priority=0)]

    def classify(self, url: str) -> str | None:
        if urlsplit(url).hostname not in self.hosts:
            return None
        if urlsplit(url).path.rstrip("/") == "/catalogues/cars":
            return "catalogue"
        return KINDS.get(len(segments(url))) if segments(url) else None

    def parse(self, page: CrawledPage) -> ParseResult:
        soup = page.soup()
        result = ParseResult()
        current = segments(page.url)
        hrefs = [anchor["href"] for anchor in soup.select(f'a[href^="{SEARCH}"]')]
        hrefs += [option["value"] for option in soup.select(f'option[value^="{SEARCH}"]')]
        seen: set[str] = set()
        for href in hrefs:
            url = ROOT + href.split("#")[0].split("?")[0]
            child = segments(url)
            if (url in seen or len(child) != len(current) + 1 or len(child) > 4
                    or child[:len(current)] != current):
                continue
            seen.add(url)
            make = child[1] if len(child) > 1 else ""
            result.links.append(Link(url, KINDS[len(child)],
                                     priority=make_priority(make) * 10 + 4 - len(child)))
        if len(current) == 4:
            result.merge(self.results(soup, current))
        return result

    def results(self, soup, current: list[str]) -> ParseResult:
        product_type, make, model, _year = current
        position = product_type.replace("-", " ").rstrip("s").title()
        result = ParseResult()
        for table in soup.find_all("table"):
            headers = [text(cell, 40).lower() for cell in table.find_all("th")]
            if "part no" not in headers:
                continue
            column = {name: index for index, name in enumerate(headers)}
            for row in table.select("tbody tr"):
                cells = row.find_all("td")
                if len(cells) < len(headers) - 1:
                    continue

                def cell(name: str, cells=cells) -> str:
                    index = column.get(name)
                    return text(cells[index], 120) if index is not None and index < len(cells) \
                        else ""

                number = cell("part no")
                if not number or number == "-":
                    continue
                details = [cell("engine size"), cell("model detail"), cell("vehicle detail"),
                           cell("engine type"), cell("dates")]
                details = ["" if value == "-" else value for value in details]
                key = vehicle_key([make, model] + details)
                years = year_range(details[4])
                label = clean(" ".join([make, model] + [v for v in details[:4] if v]), 300)
                result.vehicles.append(Vehicle(
                    key=key, make=make, model=model, year_from=years[0], year_to=years[1],
                    engine=clean(f"{details[0]} {details[3]}", 200), label=label,
                    raw={"engine_size": details[0], "model_detail": details[1],
                         "vehicle_detail": details[2], "engine_type": details[3],
                         "dates": details[4]}))
                part = Part("NGK", number, name=position, kind="aftermarket")
                result.parts.append(part)
                info = cell("part info")
                result.fitments.append(Fitment(part.ref, key, note="" if info == "-" else info,
                                               position=position))
                stock = cell("stock no")
                if stock and stock != "-" and stock != number:
                    result.cross_refs.append(CrossRef(part.ref, PartRef("NGK", stock),
                                                      "equivalent"))
        return result
