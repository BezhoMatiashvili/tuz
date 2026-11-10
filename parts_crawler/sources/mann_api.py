"""MANN-FILTER catalog through the JSON endpoint the site's own pages call.

`GET /api/graphql/catalog-prod` needs no login and returns, per filter, the OE numbers
and the competitor numbers it replaces, and per vehicle type the engine code, capacity,
power and the filters that fit. The product pages (`mann` source) show only part of that.

The site's robots.txt disallows `/api/graphql/`. This source therefore never starts on
its own: the operator has to name it in PARTS_CRAWLER_ROBOTS_EXEMPT, and the exemption
covers that one path. Pacing, the honest User-Agent and the stop on any block stay the
same as for every other source.

Order of work: all products first (about 82 pages of 150), then the vehicle tree, most
common makes first: brand search -> model series -> vehicle types -> filters per type.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from parts_crawler.records import (
    CrossRef,
    Fitment,
    Link,
    ParseResult,
    Part,
    PartRef,
    Vehicle,
    clean,
)
from parts_crawler.sources.base import CrawledPage, SourceAdapter, make_priority

ENDPOINT = "https://www.mann-filter.com/api/graphql/catalog-prod"
BRAND = "MANN-FILTER"
PAGE_SIZE = 150
SEARCH_CAP = 10          # brand_id_suggestion returns at most this many matches
SEARCH_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789 -"
# 01 cars and vans, 02 trucks and buses. Off-highway machinery (03) is left out.
SEGMENTS = frozenset({"01", "02"})
FUELS = {"04": "petrol", "08": "diesel"}  # the two codes confirmed against known engines

PRODUCTS = """query($page:Int!,$size:Int!){products(search:"",pageSize:$size,currentPage:$page){
 total_count page_info{current_page total_pages}
 items{sku name attributes_value{key value}
  ... on SimpleProduct{oe_numbers{label value} comparison_numbers{label value}
   successor_product{sales_designation} replaced_product{sales_designation}}}}}"""
BRANDS = """query($search:String!){brand_id_suggestion(search:$search){suggestions{
 vehicle_brand_id vehicle_brand_name vehicle_application_segment_id}}}"""
SERIES = """query($brand:String!){modelCollectionByBrandId(vehicle_brand_id:$brand,
 vehicle_category_id:"",model_name:"",pageSize:500,currentPage:1){models{
 model_series_id model_series_name model_series_date}}}"""
TYPES = """query($series:String!){modelTypeCollection(model_series_id:$series){allModelTypes{
 model_type_id vehicle_name engine_code fuel_type ccm kw bhp model_code serial_number_range
 vehicle_manufactured_from vehicle_manufactured_to}}}"""
LINKAGE = """query($type:String!,$page:Int!){productLinkageCollection(vehicle_model_type_id:$type,
 pageSize:50,currentPage:$page,filterBy:ALL_FILTER){page_info{current_page total_pages}
 items{product_type linkages{date_interval{linkage_fits_from linkage_fits_to}
  text{module_name module_value module_unit}} product{sku}}}}"""


def graphql_url(query: str, variables: dict | None = None) -> str:
    """One canonical URL per request, so the queue's (source, url) key deduplicates."""
    params = [("query", " ".join(query.split()))]
    if variables:
        params.append(("variables", json.dumps(variables, sort_keys=True,
                                               separators=(",", ":"))))
    return ENDPOINT + "?" + urlencode(params, quote_via=quote)


def variables_of(url: str) -> dict:
    values = parse_qs(urlsplit(url).query).get("variables")
    try:
        return json.loads(values[0]) if values else {}
    except json.JSONDecodeError:
        return {}


def part_number(sku: str) -> str:
    """'W712/83_MANN-FILTER' -> 'W712/83'."""
    return (sku or "").split("_", 1)[0].strip()


def brand_name(label: str) -> str:
    """'MINI (BMW GROUP)' -> 'MINI'; a trailing bracket names the group, not the brand."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", label or "").strip() or (label or "").strip()


def _year(value: str | None) -> int | None:
    return int(value[:4]) if value and value[:4].isdigit() else None


def _int(value: str | None) -> int | None:
    return int(value) if value and str(value).isdigit() else None


class MannApiAdapter(SourceAdapter):
    name = "mann_api"
    hosts = frozenset({"www.mann-filter.com"})
    parser_version = "1"
    needs_robots_exemption = True
    robots_exempt_paths = ("/api/graphql/",)
    request_headers = {"Accept": "application/json"}
    note = ("MANN-FILTER JSON catalog: OE and competitor cross references, vehicle types "
            "with engine codes and the filters that fit. Runs only when listed in "
            "PARTS_CRAWLER_ROBOTS_EXEMPT.")

    def seeds(self) -> list[Link]:
        links = [Link(graphql_url(PRODUCTS, {"page": 1, "size": PAGE_SIZE}), "products", 0)]
        links += [Link(graphql_url(BRANDS, {"search": char}), "brands", 900,
                       {"search": char}) for char in SEARCH_CHARS if char.isalnum()]
        return links

    def parse(self, page: CrawledPage) -> ParseResult:
        body = page.json()
        if not isinstance(body, dict) or body.get("errors") or not body.get("data"):
            message = (body.get("errors") or [{}])[0].get("message", "no data") \
                if isinstance(body, dict) else "not an object"
            raise ValueError(f"GraphQL error: {str(message)[:200]}")
        handler = {"products": self._products, "brands": self._brands, "series": self._series,
                   "types": self._types, "linkage": self._linkage}.get(page.kind)
        return handler(page, body["data"]) if handler else ParseResult()

    def _products(self, page: CrawledPage, data: dict) -> ParseResult:
        result = ParseResult()
        block = data.get("products") or {}
        for item in block.get("items") or []:
            attributes = {entry.get("key"): entry.get("value")
                          for entry in item.get("attributes_value") or []}
            number = clean(attributes.get("sales_designation") or part_number(item.get("sku")),
                           120)
            if not number:
                continue
            part = Part(BRAND, number, kind="aftermarket", name=clean(
                attributes.get("product_shape_group_product_type_group_name") or "", 300))
            result.parts.append(part)
            for field, relation in (("oe_numbers", "oe"), ("comparison_numbers", "equivalent")):
                for group in item.get(field) or []:
                    brand = brand_name(group.get("label") or "")
                    for value in group.get("value") or []:
                        result.cross_refs.append(
                            CrossRef(part.ref, PartRef(brand, str(value)), relation))
            for field, relation in (("successor_product", "replaced_by"),
                                    ("replaced_product", "replaces")):
                for other in item.get(field) or []:
                    designation = clean((other or {}).get("sales_designation") or "", 120)
                    if designation:
                        result.cross_refs.append(
                            CrossRef(part.ref, PartRef(BRAND, designation), relation))
        info = block.get("page_info") or {}
        if info.get("current_page") == 1:
            result.links += [
                Link(graphql_url(PRODUCTS, {"page": number, "size": PAGE_SIZE}), "products", 1)
                for number in range(2, int(info.get("total_pages") or 1) + 1)]
        return result

    def _brands(self, page: CrawledPage, data: dict) -> ParseResult:
        result = ParseResult()
        found = (data.get("brand_id_suggestion") or {}).get("suggestions") or []
        search = str(page.context.get("search") or variables_of(page.url).get("search") or "")
        for brand in found:
            identifier = brand.get("vehicle_brand_id")
            name = brand_name(brand.get("vehicle_brand_name") or "")
            if (not identifier or not name
                    or brand.get("vehicle_application_segment_id") not in SEGMENTS):
                continue
            result.links.append(Link(
                graphql_url(SERIES, {"brand": identifier}), "series",
                200 + make_priority(name) * 10, {"brand": name, "brand_id": identifier}))
        # The search matches anywhere in the name and is capped, so a full answer may
        # hide more brands: narrow it by one more character.
        if len(found) >= SEARCH_CAP and 0 < len(search) < 4:
            result.links += [Link(graphql_url(BRANDS, {"search": search + char}), "brands",
                                  900 + len(search), {"search": search + char})
                             for char in SEARCH_CHARS if (search + char).strip() == search + char]
        return result

    def _series(self, page: CrawledPage, data: dict) -> ParseResult:
        result = ParseResult()
        brand = page.context.get("brand") or ""
        for series in (data.get("modelCollectionByBrandId") or {}).get("models") or []:
            identifier = series.get("model_series_id")
            if not identifier:
                continue
            result.links.append(Link(
                graphql_url(TYPES, {"series": identifier}), "types",
                300 + make_priority(brand) * 10,
                {"brand": brand, "series": clean(series.get("model_series_name") or "", 200),
                 "series_date": series.get("model_series_date") or ""}))
        return result

    def _types(self, page: CrawledPage, data: dict) -> ParseResult:
        result = ParseResult()
        brand = page.context.get("brand") or ""
        series = page.context.get("series") or ""
        for kind in (data.get("modelTypeCollection") or {}).get("allModelTypes") or []:
            identifier = kind.get("model_type_id")
            if not identifier or not brand:
                continue
            key = f"type:{identifier}"
            name = clean(kind.get("vehicle_name") or "", 200)
            engine_code = clean(kind.get("engine_code") or "", 60)
            result.vehicles.append(Vehicle(
                key=key, make=brand, model=series,
                year_from=_year(kind.get("vehicle_manufactured_from")),
                year_to=_year(kind.get("vehicle_manufactured_to")), engine=name,
                label=" ".join(part for part in (brand, series, name, engine_code) if part),
                model_code=clean(kind.get("model_code") or "", 120), engine_code=engine_code,
                displacement_cc=_int(kind.get("ccm")), power_kw=_int(kind.get("kw")),
                fuel=FUELS.get(kind.get("fuel_type") or "", ""),
                raw={"model_type_id": identifier, "fuel_type_code": kind.get("fuel_type"),
                     "bhp": kind.get("bhp"), "series_date": page.context.get("series_date"),
                     "serial_number_range": kind.get("serial_number_range")}))
            result.links.append(Link(graphql_url(LINKAGE, {"type": identifier, "page": 1}),
                                     "linkage", 400 + make_priority(brand) * 10,
                                     {"vehicle": key}))
        return result

    def _linkage(self, page: CrawledPage, data: dict) -> ParseResult:
        result = ParseResult()
        vehicle = page.context.get("vehicle")
        block = data.get("productLinkageCollection") or {}
        if not vehicle:
            return result
        for item in block.get("items") or []:
            number = part_number((item.get("product") or {}).get("sku") or "")
            if not number or number.upper() == "NOFILTER":
                continue
            notes = []
            for linkage in item.get("linkages") or []:
                interval = linkage.get("date_interval") or {}
                span = "-".join(value[:7] for value in (interval.get("linkage_fits_from"),
                                                        interval.get("linkage_fits_to"))
                                if value)
                if span:
                    notes.append(span)
                notes += [" ".join(str(entry.get(key) or "") for key in
                                   ("module_name", "module_value", "module_unit")).strip()
                          for entry in linkage.get("text") or []]
            result.fitments.append(Fitment(
                PartRef(BRAND, number), vehicle, position=clean(item.get("product_type"), 200),
                note="; ".join(dict.fromkeys(note for note in notes if note))))
        info = block.get("page_info") or {}
        variables = variables_of(page.url)
        if info.get("current_page") == 1 and variables.get("type"):
            result.links += [
                Link(graphql_url(LINKAGE, {"type": variables["type"], "page": number}),
                     "linkage", 400, {"vehicle": vehicle})
                for number in range(2, int(info.get("total_pages") or 1) + 1)]
        return result
