"""MANN-FILTER product pages listed in the commerce sitemap.

Each page embeds schema.org JSON-LD with the OE numbers and main vehicle applications.
The site's /api/graphql/ catalog endpoint is disallowed by robots.txt and is not used.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from parts_crawler.records import CrossRef, Fitment, Link, ParseResult, Part, PartRef, Vehicle
from parts_crawler.sources.base import CrawledPage, SourceAdapter
from parts_crawler.sources.generic import jsonld_products, vehicle_key

SITEMAP = "https://www.mann-filter.com/en.sitemap-commerce.xml"
PRODUCT_PATH = "/en/catalog/search-results/product.html/"
BRAND = "MANN-FILTER"
WORD = r"[A-Z][A-Z0-9&.\-]*(?=\s|$)"
MAKE = re.compile(rf"^({WORD}(?:\s+(?:\([A-Z0-9 &.\-]+\)(?=\s|$)|{WORD}))*)")


def split_application(label: str) -> tuple[str, str]:
    """'IVECO BUS Crossway / Crossway LE' -> ('IVECO BUS', 'Crossway / Crossway LE')."""
    match = MAKE.match(label.strip())
    if not match:
        return label.strip(), ""
    make = match.group(1).strip()
    return make, label.strip()[len(make):].strip()


class MannAdapter(SourceAdapter):
    name = "mann"
    hosts = frozenset({"www.mann-filter.com"})
    parser_version = "1"
    note = "MANN-FILTER products with OE numbers and main vehicle applications."

    def seeds(self) -> list[Link]:
        return [Link(SITEMAP, "sitemap", priority=0)]

    def classify(self, url: str) -> str | None:
        path = urlsplit(url).path
        if urlsplit(url).hostname not in self.hosts:
            return None
        if path.endswith(".xml"):
            return "sitemap"
        return "product" if path.startswith(PRODUCT_PATH) else None

    def parse(self, page: CrawledPage) -> ParseResult:
        if page.kind == "sitemap" or urlsplit(page.url).path.endswith(".xml"):
            return ParseResult(links=[
                Link(loc, "product", priority=10)
                for loc in dict.fromkeys(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page.text))
                if loc.startswith("https://www.mann-filter.com" + PRODUCT_PATH)])
        result = ParseResult()
        for product in jsonld_products(page.soup()):
            name = str(product.get("name") or "")
            number = name[len(BRAND):].strip() if name.upper().startswith(BRAND) else ""
            if not number:
                sku = str(product.get("sku") or "")
                number = sku.split("_")[0]
            if not number:
                continue
            part = Part(BRAND, number, name=str(product.get("category") or ""),
                        kind="aftermarket")
            result.parts.append(part)
            for prop in product.get("additionalProperty") or []:
                if not isinstance(prop, dict) or "OE" not in str(prop.get("name", "")):
                    continue
                values = prop.get("value")
                for oe in values if isinstance(values, list) else [values]:
                    if oe:
                        result.cross_refs.append(CrossRef(part.ref, PartRef("", str(oe)), "oe"))
            keywords = str(product.get("keywords") or "")
            if ":" in keywords:
                for label in re.split(r"\.\s+", keywords.split(":", 1)[1]):
                    label = label.strip(" .")
                    make, model = split_application(label)
                    if not label or not make:
                        continue
                    key = "app:" + vehicle_key([label])
                    result.vehicles.append(Vehicle(key=key, make=make, model=model, label=label,
                                                   raw={"application": label}))
                    result.fitments.append(Fitment(part.ref, key,
                                                   note="main vehicle application",
                                                   position=part.name))
        return result
