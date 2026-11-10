"""HTML helpers shared by adapters, and a breadcrumb-driven adapter for EPC-style sites.

The EPC adapter is deliberately conservative: it only follows links under configured
path prefixes, takes vehicle identity from on-page breadcrumbs (or the trail of link
labels that led to the page), and reads parts from tables with a part-number header.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from automotive.codes import normalize_code
from parts_crawler.records import Fitment, Link, ParseResult, Part, Vehicle, clean, year_range
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority

TRACKING = re.compile(r"^(utm_|fbclid|gclid|_ga)", re.I)
NUMBER_HEADER = re.compile(
    r"(part\s*(no|number|#)|^p/?n$|номер\s*(детали|запчасти)|артикул|^код|article|oem|"
    r"teilenummer|référence)", re.I)
# A bare "No."/"Number"/"Номер" column is often a diagram position; used only as fallback.
WEAK_NUMBER_HEADER = re.compile(r"^(no\.?|number|номер)$", re.I)
NAME_HEADER = re.compile(r"(name|description|наименование|название|описание|bezeichnung)", re.I)
QTY_HEADER = re.compile(r"(qty|quantity|кол|anzahl|menge)", re.I)
NOTE_HEADER = re.compile(r"(note|remark|comment|примеч|info|date|from|to|period|период)", re.I)


def text(element: Tag | None, maximum: int = 300) -> str:
    return clean(element.get_text(" ", strip=True), maximum) if element is not None else ""


def absolute(base: str, href: str, hosts: frozenset[str]) -> str | None:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    parts = urlsplit(urljoin(base, href.strip()))
    if parts.hostname not in hosts or parts.scheme not in {"http", "https"}:
        return None
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING.match(key)])
    return urlunsplit(("https", parts.netloc, parts.path or "/", query, ""))


def jsonld_products(soup: BeautifulSoup) -> list[dict]:
    products: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type")
                if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                    products.append(node)
                stack.extend(node.get("@graph", []) if isinstance(node.get("@graph"), list) else [])
    return products


def looks_like_part_number(value: str) -> bool:
    normalized = normalize_code(value)
    return (4 <= len(normalized) <= 30 and any(char.isdigit() for char in normalized)
            and not re.fullmatch(r"(19|20)\d\d", normalized) and value.count(" ") <= 4)


def part_table_rows(soup: BeautifulSoup) -> list[dict]:
    """Rows of every table whose header names a part-number column."""
    found: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_index = next((index for index, row in enumerate(rows[:3]) if row.find("th")), 0)
        headers = [text(cell, 80) for cell in rows[header_index].find_all(["th", "td"])]
        number_col = next((i for i, h in enumerate(headers) if NUMBER_HEADER.search(h)), None)
        if number_col is None:
            number_col = next((i for i, h in enumerate(headers)
                               if WEAK_NUMBER_HEADER.search(h)), None)
        if number_col is None:
            continue
        name_col = next((i for i, h in enumerate(headers) if NAME_HEADER.search(h)), None)
        qty_col = next((i for i, h in enumerate(headers) if QTY_HEADER.search(h)), None)
        note_cols = [i for i, h in enumerate(headers) if NOTE_HEADER.search(h)]
        for row in rows[header_index + 1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= number_col:
                continue
            number = text(cells[number_col], 60)
            if not looks_like_part_number(number):
                continue
            found.append({
                "number": number,
                "name": text(cells[name_col], 200) if name_col is not None
                and name_col < len(cells) else "",
                "quantity": text(cells[qty_col], 20) if qty_col is not None
                and qty_col < len(cells) else "",
                "note": "; ".join(text(cells[i], 80) for i in note_cols
                                  if i < len(cells) and text(cells[i], 80)),
                "position": text(cells[0], 40) if number_col != 0 else "",
            })
    return found


def breadcrumbs(soup: BeautifulSoup) -> list[str]:
    containers = soup.select('[class*="breadcrumb"], nav[aria-label="breadcrumb"], '
                             '[itemtype*="BreadcrumbList"]')
    for container in containers:
        items = [text(item, 80) for item in container.find_all(["a", "span", "li"])
                 if text(item, 80) and not item.find(["a", "span", "li"])]
        unique = [item for index, item in enumerate(items) if item not in items[:index]]
        if len(unique) >= 2:
            return unique[:12]
    return []


def vehicle_key(labels: list[str]) -> str:
    joined = " / ".join(labels)
    if len(joined) <= 200:
        return joined
    return joined[:150] + " #" + hashlib.sha256(joined.encode()).hexdigest()[:16]


class EpcAdapter(SourceAdapter):
    """Follow catalog links under `follow_prefixes`; read part tables on any page."""

    start_urls: ClassVar[tuple[str, ...]] = ()
    follow_prefixes: ClassVar[tuple[str, ...]] = ()
    exclude: ClassVar[re.Pattern] = re.compile(r"$^")
    max_depth: ClassVar[int] = 10
    fixed_make: ClassVar[str] = ""
    home_labels: ClassVar[frozenset[str]] = frozenset({"home", "главная", "catalog", "catalogs",
                                                       "каталог", "main"})

    def seeds(self) -> list[Link]:
        return [Link(url, "page", priority=500) for url in self.start_urls]

    def classify(self, url: str) -> str | None:
        return "page" if self.follows(url) else None

    def follows(self, url: str) -> bool:
        parts = urlsplit(url)
        return (parts.hostname in self.hosts and parts.path.startswith(self.follow_prefixes)
                and not self.exclude.search(url))

    def make_for(self, url: str, trail: list[str]) -> str:
        return self.fixed_make or (trail[0] if trail else "")

    def vehicle_labels(self, page: CrawledPage, soup: BeautifulSoup) -> list[str]:
        crumbs = [crumb for crumb in breadcrumbs(soup) if crumb.lower() not in self.home_labels]
        trail = crumbs or list(page.context.get("trail", []))
        # The last label names the part group/diagram, not the vehicle.
        return trail[:-1] if len(trail) > 1 else trail

    def parse(self, page: CrawledPage) -> ParseResult:
        soup = page.soup()
        result = ParseResult()
        trail = list(page.context.get("trail", []))
        depth = len(trail)
        if depth < self.max_depth:
            seen: set[str] = set()
            for anchor in soup.find_all("a", href=True):
                url = absolute(page.url, anchor["href"], self.hosts)
                if not url or url in seen or not self.follows(url) or url == page.url:
                    continue
                seen.add(url)
                label = text(anchor, 80) or clean(anchor.get("title", ""), 80)
                child = trail + [label] if label else trail
                make = self.make_for(url, child)
                result.links.append(Link(
                    url, "page", priority=make_priority(make) * 20 + max(0, 12 - len(child)),
                    context={"trail": child[-10:]}))
        rows = part_table_rows(soup)
        if rows:
            labels = self.vehicle_labels(page, soup)
            make = self.make_for(page.url, labels) or (labels[0] if labels else "")
            group = clean((breadcrumbs(soup) or trail or [""])[-1], 200)
            key = vehicle_key(labels) if labels else ""
            if key and make:
                years = year_range(" ".join(labels))
                result.vehicles.append(Vehicle(
                    key=key, make=make, model=labels[1] if len(labels) > 1 else "",
                    year_from=years[0], year_to=years[1], label=" / ".join(labels),
                    raw={"labels": labels, "url": page.url}))
            for row in rows:
                part = Part(make or "", row["number"], name=row["name"], kind="oe")
                result.parts.append(part)
                if key and make:
                    result.fitments.append(Fitment(
                        part.ref, key, note=row["note"], position=group or row["position"],
                        quantity=row["quantity"]))
        return result
