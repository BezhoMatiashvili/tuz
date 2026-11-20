"""Pages for search engines: the parts in stock by make and model, and the sitemap.

The landing page's part search runs in the browser, so a crawler sees none of the stock.
These pages list the same items server-side from the in-memory catalogue snapshot (no
database query per visitor), with exactly the fields the public search shows: name, fits,
brand and price. A make or model gets a page only when it has enough items to be worth one.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from html import escape
from threading import Lock
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from automotive.site_content import localized
from automotive.site_strings import STRINGS

PER_PAGE = 60
MIN_ITEMS = 5  # a make or model with fewer available items gets no page of its own
LANDING_MAKES = 18  # make links shown under the landing page's part search
# How the makes are spelled in Georgian script, so a search typed in Georgian matches too.
# Makes without a settled spelling are left out rather than guessed.
LOCAL_NAMES = {
    "toyota": "ტოიოტა", "lexus": "ლექსუსი", "nissan": "ნისანი", "hyundai": "ჰიუნდაი",
    "mercedes-benz": "მერსედესი", "honda": "ჰონდა", "mitsubishi": "მიცუბიში",
    "ford": "ფორდი", "kia": "კია", "infiniti": "ინფინიტი", "volkswagen": "ფოლკსვაგენი",
    "subaru": "სუბარუ", "mazda": "მაზდა", "audi": "აუდი", "suzuki": "სუზუკი",
    "chevrolet": "შევროლე", "skoda": "შკოდა", "opel": "ოპელი", "land rover": "ლენდ როვერი",
    "jeep": "ჯიპი", "fiat": "ფიატი", "tesla": "ტესლა", "renault": "რენო", "porsche": "პორშე",
}


DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TIMES = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-\u2013\u2014]\s*(\d{1,2})[:.](\d{2})")


def make_slug(key: str) -> str:
    return key.replace(" ", "-")


def model_slug(key: str) -> str:
    return key.lower()


def parts_path(language: str, make: str | None = None, model: str | None = None,
               page: int = 1) -> str:
    path = ("" if language == "ka" else "/en") + "/parts"
    if make:
        path += "/" + make_slug(make)
        if model:
            path += "/" + model_slug(model)
    return path + (f"?page={page}" if page > 1 else "")


def tidy(value: str) -> str:
    """As the page scripts write catalogue text: a spaced dash is a comma, others a hyphen."""
    text = " ".join(str(value or "").split())
    for dash in ("–", "—"):
        text = text.replace(f" {dash} ", ", ").replace(dash, "-")
    return text


def money(value: float | None, language: str, currency: str, ask: str) -> str:
    """As the page scripts write a price: `25 ₾` in Georgian, `₾25` in English."""
    if not value or value <= 0:
        return ask
    whole = math.trunc(value)
    cents = round((value - whole) * 100)
    amount = f"{whole:,}".replace(",", " ") + (f".{cents:02d}" if cents else "")
    return f"{amount} {currency}" if language == "ka" else currency + amount


def day_names(code: str) -> list[str]:
    """`Mo-Sa` or `Mo-Fr, Su` as schema.org day names; anything unreadable is no days."""
    found: list[str] = []
    for part in str(code or "").split(","):
        first, _, last = part.strip().partition("-")
        try:
            start = DAYS.index(first.strip().title())
            end = DAYS.index(last.strip().title()) if last else start
        except ValueError:
            return []
        for offset in range((end - start) % 7 + 1):
            name = DAY_NAMES[(start + offset) % 7]
            if name not in found:
                found.append(name)
    return found


def opening_hours(rows: list[dict]) -> list[dict]:
    """The dashboard's hours rows that carry both a day code and readable times."""
    found = []
    for row in rows or []:
        days, times = day_names(row.get("days_code", "")), TIMES.search(row.get("time") or "")
        if days and times:
            opens = f"{int(times[1]):02d}:{times[2]}"
            closes = f"{int(times[3]):02d}:{times[4]}"
            found.append({"@type": "OpeningHoursSpecification", "dayOfWeek": days,
                          "opens": opens, "closes": closes})
    return found


def business_data(view: dict, origin: str, image: str, logo: str) -> dict:
    """The landing page's structured data: the website and, once the dashboard has a phone
    or an address, the business. Only what the dashboard stores is stated."""
    brand = view["brand"]
    home = origin + "/"
    graph: list[dict] = [{
        "@type": "WebSite", "@id": home + "#website", "url": home, "name": "Garage 1886",
        "alternateName": "ფარეხი 1886", "inLanguage": ["ka", "en"]}]
    if brand.get("phone") or brand.get("address"):
        business: dict[str, Any] = {
            "@type": ["AutoRepair", "AutoPartsStore"], "@id": home + "#business",
            "name": "Garage 1886", "url": home, "image": image, "logo": logo,
            "description": view["seo"]["description"]}
        graph[0]["publisher"] = {"@id": home + "#business"}
        for key, name in (("phone", "telephone"), ("email", "email"),
                          ("map_url", "hasMap")):
            if brand.get(key):
                business[name] = brand[key]
        if brand.get("address"):
            address = {"@type": "PostalAddress", "streetAddress": brand["address"],
                       "addressCountry": "GE"}
            if brand.get("city"):
                address["addressLocality"] = brand["city"]
            if brand.get("postal_code"):
                address["postalCode"] = brand["postal_code"]
            business["address"] = address
        if brand.get("geo"):
            latitude, _, longitude = brand["geo"].partition(",")
            business["geo"] = {"@type": "GeoCoordinates", "latitude": float(latitude),
                               "longitude": float(longitude)}
        hours = opening_hours(brand.get("hours") or [])
        if hours:
            business["openingHoursSpecification"] = hours
        profiles = [brand[key] for key in ("facebook", "instagram", "tiktok") if brand.get(key)]
        if profiles:
            business["sameAs"] = profiles
        graph.append(business)
    return {"@context": "https://schema.org", "@graph": graph}


class PartsIndex:
    """Which makes and models get a page, read from one catalogue snapshot."""

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        # Counts are of what a page lists (identical stock lines once), so a link's number
        # matches the page it opens.
        self.totals: dict[tuple[str, str | None], int] = {}
        self.makes = []
        for entry in catalog.options()["makes"]:
            total = len(catalog.listing(make=entry["key"]))
            if total >= MIN_ITEMS:
                self.totals[(entry["key"], None)] = total
                self.makes.append({**entry, "count": total})
        self.by_slug = {make_slug(entry["key"]): entry for entry in self.makes}
        # A row written "TOYOTA/LEXUS RX300" counts under both makes, which would give
        # "Toyota RX300" a page. A model belongs to the make it is most often written with
        # alone; a model never written with one make alone gets no model page.
        alone: dict[str, Counter] = defaultdict(Counter)
        for item in catalog.available:
            if len(item.makes) == 1 and item.models:
                alone[item.models[0]][item.makes[0]] += 1
        home = {model: counts.most_common(1)[0][0] for model, counts in alone.items()}
        self._models: dict[str, list[dict]] = {}
        for entry in self.makes:
            found = []
            for model in catalog.models(entry["key"]):
                if home.get(model["key"]) != entry["key"]:
                    continue
                total = len(catalog.listing(make=entry["key"], model=model["key"]))
                if total >= MIN_ITEMS:
                    self.totals[(entry["key"], model["key"])] = total
                    found.append({**model, "count": total})
            self._models[entry["key"]] = sorted(
                found, key=lambda model: (-model["count"], model["name"]))

    def models(self, make: str) -> list[dict]:
        return self._models.get(make, [])

    def model(self, make: str, slug: str) -> dict | None:
        return next((entry for entry in self.models(make)
                     if model_slug(entry["key"]) == slug), None)

    def pages(self, make: str, model: str | None = None) -> int:
        return max(1, math.ceil(self.totals.get((make, model), 0) / PER_PAGE))


class PartsPages:
    def __init__(self, pages: Any) -> None:
        self.pages = pages
        self._lock = Lock()
        self._cached: PartsIndex | None = None

    def _index(self) -> PartsIndex | None:
        """The index of the current catalogue snapshot, built once per snapshot."""
        store = self.pages.catalog_store
        if not getattr(store, "loaded", True):
            return None
        catalog = store.current()
        with self._lock:
            if self._cached is None or self._cached.catalog is not catalog:
                self._cached = PartsIndex(catalog)
            return self._cached

    def origin(self, request: Request) -> str:
        return self.pages.public_origin or str(request.base_url).rstrip("/")

    # -- links for the landing page ----------------------------------------------------

    def landing_links(self, language: str, view: dict | None = None, origin: str = "") -> dict:
        index = self._index()
        makes = index.makes[:LANDING_MAKES] if index else []
        asset = self.pages.assets.url
        return {"parts_makes": [{"name": entry["name"], "count": entry["count"],
                                 "href": parts_path(language, entry["key"])}
                                for entry in makes],
                "parts_index": parts_path(language) if makes else "",
                "business_ld": business_data(
                    view, origin, origin + asset("site/img/og.jpg"),
                    origin + asset("site/img/apple-touch-icon.png")) if view else None}

    # -- pages -------------------------------------------------------------------------

    def render(self, request: Request, language: str, make: str | None = None,
               model: str | None = None) -> Response:
        index = self._index()
        if index is None:
            # Starting up: say so, so a crawler comes back instead of dropping the page.
            return PlainTextResponse("Starting", status_code=503, headers={"retry-after": "60"})
        page = request.query_params.get("page", "1")
        if not re.fullmatch(r"[0-9]{1,4}", page) or int(page) < 1:
            return _missing()
        page_number = int(page)
        if make is None:
            if not index.makes or page_number != 1:
                return _missing()
            return self._page(request, language, index, None, None, 1)
        found = index.by_slug.get(make)
        if found is None:
            return _missing()
        model_entry = None
        if model is not None:
            model_entry = index.model(found["key"], model)
            if model_entry is None:
                return _missing()
        if page_number > index.pages(found["key"], model_entry and model_entry["key"]):
            return _missing()
        return self._page(request, language, index, found, model_entry, page_number)

    def _page(self, request: Request, language: str, index: PartsIndex, make: dict | None,
              model: dict | None, page: int) -> Response:
        t = STRINGS[language]
        snapshot = self.pages.content.snapshot()
        view = localized(snapshot, language)
        origin = self.origin(request)
        make_key = make["key"] if make else None
        model_key = model["key"] if model else None
        home = "/" if language == "ka" else "/en"
        crumbs = [(t["nav.home"], home), (t["seo.parts"], parts_path(language))]
        items: list[dict] = []
        pages = 1
        models: list[dict] = []
        makes: list[dict] = []
        currency = view["brand"].get("currency") or "₾"
        if make is None:
            heading = t["seo.parts_h1"]
            title = t["seo.parts_title"]
            description = t["seo.parts_description"].replace(
                "{count}", f"{len(index.catalog.available):,}".replace(",", " "))
            makes = [{"name": entry["name"], "count": entry["count"],
                      "local": LOCAL_NAMES.get(entry["key"], "") if language == "ka" else "",
                      "href": parts_path(language, entry["key"])} for entry in index.makes]
            lead = t["seo.parts_lead"]
        else:
            name = make["name"] + (" " + model["name"] if model else "")
            local = LOCAL_NAMES.get(make_key, "") if language == "ka" and not model else ""
            shown = f"{name} ({local})" if local else name
            rows = index.catalog.listing(make=make_key, model=model_key)
            pages = max(1, math.ceil(len(rows) / PER_PAGE))
            ask = t["js.search.price_request"]
            for item in rows[(page - 1) * PER_PAGE:page * PER_PAGE]:
                meta = " · ".join(part for part in (tidy(item.brand), tidy(item.model)) if part)
                items.append({"name": tidy(item.name), "meta": meta,
                              "price": money(item.price, language, currency, ask),
                              "ask": not item.price})
            count = f"{len(rows):,}".replace(",", " ")
            heading = t["seo.make_h1"].replace("{make}", name)
            title = t["seo.make_title"].replace("{make}", shown)
            description = t["seo.make_description"].replace("{make}", shown).replace(
                "{count}", count)
            lead = t["seo.make_lead"].replace("{count}", count)
            if page > 1:
                title = t["seo.make_title_paged"].replace("{make}", shown).replace(
                    "{page}", str(page))
            crumbs.append((make["name"], parts_path(language, make_key)))
            if model:
                crumbs.append((model["name"], parts_path(language, make_key, model_key)))
            else:
                models = [{"name": entry["name"], "count": entry["count"],
                           "href": parts_path(language, make_key, entry["key"])}
                          for entry in index.models(make_key)]
        path = parts_path(language, make_key, model_key, page)
        alternates = {lang: origin + parts_path(lang, make_key, model_key, page)
                      for lang in ("ka", "en")}
        breadcrumb = {
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": position, "name": label,
                                 "item": origin + href}
                                for position, (label, href) in enumerate(crumbs, start=1)]}
        pager = {
            "page": page, "pages": pages,
            "prev": parts_path(language, make_key, model_key, page - 1) if page > 1 else "",
            "next": (parts_path(language, make_key, model_key, page + 1)
                     if page < pages else "")}
        freshness = index.catalog.freshness()
        observed = index.catalog.observed_at
        return self.pages.render(
            "parts_page.html", request, cache="revalidate", lang=language, t=t, c=view,
            home=home, base=home, canonical=origin + path, alternates=alternates,
            origin=origin, title=title, description=description, heading=heading,
            lead=lead, crumbs=crumbs, ld=breadcrumb, items=items, models=models,
            makes=makes, pager=pager, make=make, search_href=home + "#parts",
            note=(t[f"js.search.{freshness}"] if freshness in ("confirm", "stale") else ""),
            updated=observed.strftime("%Y-%m-%d") if observed else "",
            parts_index=parts_path(language), year=time.localtime().tm_year)

    # -- sitemap -----------------------------------------------------------------------

    def sitemap(self, request: Request) -> str:
        origin = self.origin(request)
        entries: list[tuple[str, str, str]] = [("/", "/en", "")]
        index = self._index()
        if index is not None and index.makes:
            observed = index.catalog.observed_at
            stamp = observed.strftime("%Y-%m-%d") if observed else ""
            entries.append((parts_path("ka"), parts_path("en"), stamp))
            for entry in index.makes:
                targets = [(entry["key"], None)] + [
                    (entry["key"], model["key"]) for model in index.models(entry["key"])]
                for make, model in targets:
                    for page in range(1, index.pages(make, model) + 1):
                        entries.append((parts_path("ka", make, model, page),
                                        parts_path("en", make, model, page), stamp))
        urls = []
        for ka, en, stamp in entries:
            links = (f'<xhtml:link rel="alternate" hreflang="ka" href="{escape(origin + ka)}"/>'
                     f'<xhtml:link rel="alternate" hreflang="en" href="{escape(origin + en)}"/>'
                     f'<xhtml:link rel="alternate" hreflang="x-default" '
                     f'href="{escape(origin + ka)}"/>')
            modified = f"<lastmod>{stamp}</lastmod>" if stamp else ""
            for path in (ka, en):
                urls.append(f"<url><loc>{escape(origin + path)}</loc>{modified}{links}</url>")
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                'xmlns:xhtml="http://www.w3.org/1999/xhtml">' + "".join(urls) + "</urlset>")


def _missing() -> Response:
    return PlainTextResponse("Not found", status_code=404)


def build_seo_router(parts: PartsPages) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.api_route("/parts", methods=["GET", "HEAD"])
    def parts_ka(request: Request) -> Response:
        return parts.render(request, "ka")

    @router.api_route("/en/parts", methods=["GET", "HEAD"])
    def parts_en(request: Request) -> Response:
        return parts.render(request, "en")

    @router.api_route("/parts/{make}", methods=["GET", "HEAD"])
    def make_ka(make: str, request: Request) -> Response:
        return parts.render(request, "ka", make)

    @router.api_route("/en/parts/{make}", methods=["GET", "HEAD"])
    def make_en(make: str, request: Request) -> Response:
        return parts.render(request, "en", make)

    @router.api_route("/parts/{make}/{model}", methods=["GET", "HEAD"])
    def model_ka(make: str, model: str, request: Request) -> Response:
        return parts.render(request, "ka", make, model)

    @router.api_route("/en/parts/{make}/{model}", methods=["GET", "HEAD"])
    def model_en(make: str, model: str, request: Request) -> Response:
        return parts.render(request, "en", make, model)

    return router
