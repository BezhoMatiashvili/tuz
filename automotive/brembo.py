"""Brembo Parts car catalogue, one vehicle at a time (live lookups only).

Every page of bremboparts.com says its data and complete database may not be copied
without TecAlliance's consent. The owner chose (2026-11-21) to use it only the way a
person at the counter would: when a customer asks about one car, look that car up and
quote the answer. Nothing here walks the catalogue, and nothing is kept beyond the
federation's one-day answer cache and this process's small cache of the selection lists.

The site's own pages make these calls (mapped 2026-11-21):

  GET  /europe/en                                   a page carrying the anti-forgery token
  POST /europe/en/catalogue/search/getsearchbrands  {"vehicleType": "Car"}
  POST /europe/en/catalogue/search/getsearchmodels  {"brandCode", "vehicleType"}
  POST /europe/en/catalogue/search/getsearchtypes   {"modelCode"}
  POST /europe/en/catalogue/search/searchtype       {..., "typeCodeAndVehicleType"} -> {"url"}
  GET  <url>                                        results: one <app-globalcaritem> each
  POST /europe/en/catalogue/getproductmanufacturerreferences  {"bremboCode"} -> OE numbers

The POST calls need the token from the page in a `RequestVerificationToken` header and
the cookie set with it; that is the site's ordinary form protection, nothing more. A
licence-plate search with a captcha exists and is never used. Any 401, 403, redirect or
429 stops the lookup.

Choosing the car is left to the assistant: when the make, model or engine does not name
exactly one Brembo vehicle, the report is CONFLICTED (or EMPTY) and lists Brembo's own
titles, which the assistant can pass back word for word.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Lock
from typing import Any

import requests
from bs4 import BeautifulSoup

from automotive.codes import latin
from automotive.public_http import SUPPLIER_WARNING, PublicSourceError, fluid_report, redact

ORIGIN = "https://www.bremboparts.com"
BASE = ORIGIN + "/europe/en"
PROVIDER = "Brembo"
TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
MONTH_YEAR = re.compile(r"^\s*(\d{1,2})/(\d{2})\s*$")
LITRES = re.compile(r"^\s*(\d{1,2}\.\d)\b")
# Brembo product types kept for the brake questions this source answers.
KINDS = {"Pad": "brake pads", "Disc": "brake disc", "Brakeshoe": "brake shoes",
         "Drum": "brake drum"}
MAX_CANDIDATES = 12
MAX_PER_KIND = 4
MAX_BODY = 3_000_000
LIST_TTL = 24 * 3600.0
TOKEN_TTL = 20 * 60.0


def tokens(value: Any) -> list[str]:
    """'COROLLA (_E12_) 01/01 - 04/08' -> ['COROLLA', 'E12', '01', '01', '04', '08']."""
    return re.sub(r"[^A-Z0-9.]+", " ", latin(str(value or "")).upper()).split()


def full_year(two_digits: str) -> int:
    value = int(two_digits)
    return value + (2000 if value <= date.today().year % 100 + 1 else 1900)


def year_of(value: Any) -> int | None:
    match = MONTH_YEAR.match(str(value or ""))
    return full_year(match.group(2)) if match else None


def in_years(year: int | None, start: Any, end: Any) -> bool:
    """True when `year` lies in the 'MM/YY' range; an unknown year or bound is no limit."""
    if year is None:
        return True
    first, last = year_of(start), year_of(end)
    return (first is None or first <= year) and (last is None or year <= last)


def engine_title(row: dict) -> str:
    """'1.4 D (NDE120_, NDE120R) 66 kW': short enough to come back whole in `trim`."""
    power = f" {row.get('kw')} kW" if str(row.get("kw") or "").isdigit() else ""
    return f"{row.get('typeName') or row.get('title') or ''}{power}".strip()


def contains(words: list[str], within: list[str]) -> bool:
    return bool(words) and all(word in within for word in words)


class BremboClient:
    def __init__(self, *, per_minute: int = 40,
                 session_factory: Any = requests.Session, clock: Any = time.monotonic) -> None:
        self.per_minute = per_minute
        self.session_factory = session_factory
        self.clock = clock
        self._lock = Lock()
        self._requests: deque[float] = deque()
        self._session: Any = None
        self._antiforgery = ""
        self._antiforgery_at = 0.0
        self._lists: dict[tuple, tuple[float, Any]] = {}

    # --- transport -------------------------------------------------------------------

    def _reserve(self) -> None:
        now = self.clock()
        with self._lock:
            while self._requests and self._requests[0] < now - 60:
                self._requests.popleft()
            if len(self._requests) >= self.per_minute:
                raise PublicSourceError("RATE_LIMITED",
                                        retry_after=60 - (now - self._requests[0]))
            self._requests.append(now)

    def _send(self, method: str, path: str, *, body: dict | None = None,
              deadline: float | None) -> bytes:
        url = path if path.startswith(ORIGIN) else ORIGIN + path
        if not url.startswith(BASE + "/") and url != BASE:
            raise PublicSourceError("INVALID_RESPONSE")
        remaining = (deadline - self.clock()) if deadline is not None else 20.0
        if remaining <= 0.2:
            raise PublicSourceError("TIME_LIMIT")
        self._reserve()
        headers = {"User-Agent": "automotive-intelligence/0.1 (read-only)",
                   "Accept": "application/json, text/html;q=0.9"}
        if body is not None:
            headers.update({"Content-Type": "application/json;charset=UTF-8",
                            "RequestVerificationToken": self._antiforgery,
                            "Origin": ORIGIN, "Referer": BASE})
        response = None
        try:
            response = self._session.request(
                method, url, data=json.dumps(body) if body is not None else None,
                headers=headers, timeout=(min(3.05, remaining), min(15.0, remaining)),
                allow_redirects=False, stream=True)
            status = response.status_code
            if status == 429:
                raise PublicSourceError("RATE_LIMITED")
            if status in {301, 302, 303, 307, 308, 401, 403}:
                raise PublicSourceError("ACCESS_REQUIRED")
            if status == 400 and body is not None:
                raise PublicSourceError("TOKEN_REJECTED")
            if status != 200:
                raise PublicSourceError("PROVIDER_UNAVAILABLE")
            data = bytearray()
            for chunk in response.iter_content(65536):
                if deadline is not None and self.clock() >= deadline:
                    raise PublicSourceError("TIME_LIMIT")
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    raise PublicSourceError("INVALID_RESPONSE")
            return bytes(data)
        except PublicSourceError:
            raise
        except requests.RequestException:
            raise PublicSourceError("PROVIDER_UNAVAILABLE") from None
        finally:
            if response is not None:
                response.close()

    def _ready(self, deadline: float | None) -> None:
        """A session holding the site's anti-forgery cookie and its matching token."""
        with self._lock:
            fresh = self._session is not None and self.clock() - self._antiforgery_at < TOKEN_TTL
        if fresh:
            return
        self._session = self.session_factory()
        page = self._send("GET", BASE, deadline=deadline).decode("utf-8", "replace")
        match = TOKEN.search(page)
        if not match:
            raise PublicSourceError("INVALID_RESPONSE")
        with self._lock:
            self._antiforgery, self._antiforgery_at = match.group(1), self.clock()

    def _post(self, path: str, body: dict, deadline: float | None) -> Any:
        try:
            raw = self._send("POST", BASE + path, body=body, deadline=deadline)
        except PublicSourceError as error:
            if error.code != "TOKEN_REJECTED":
                raise
            # The token outlived its cookie: fetch both again, once.
            with self._lock:
                self._antiforgery_at = float("-inf")
            self._ready(deadline)
            raw = self._send("POST", BASE + path, body=body, deadline=deadline)
        try:
            return json.loads(raw)
        except ValueError:
            raise PublicSourceError("INVALID_RESPONSE") from None

    def _listing(self, key: tuple, path: str, body: dict, deadline: float | None) -> list:
        """Brand, model and engine lists, kept for a day; they name vehicles, not parts."""
        with self._lock:
            hit = self._lists.get(key)
        if hit and hit[0] > self.clock():
            return hit[1]
        value = self._post(path, body, deadline)
        if not isinstance(value, list):
            raise PublicSourceError("INVALID_RESPONSE")
        rows = [row for row in value if isinstance(row, dict)]
        with self._lock:
            if len(self._lists) > 2000:
                self._lists.clear()
            self._lists[key] = (self.clock() + LIST_TTL, rows)
        return rows

    # --- lookup ----------------------------------------------------------------------

    def lookup(self, vin: str | None, vehicle: dict, *, deadline: float | None = None) -> dict:
        del vin  # Brembo is asked by make, model and engine; a VIN is never sent.
        make, model = tokens(vehicle.get("make")), tokens(vehicle.get("model"))
        if not make or not model:
            return fluid_report(PROVIDER, "INSUFFICIENT_DATA")
        year = vehicle.get("model_year")
        year = int(year) if str(year or "").isdigit() else None
        self._ready(deadline)
        brands = self._listing(("brands",), "/catalogue/search/getsearchbrands",
                               {"vehicleType": "Car"}, deadline)
        brand = self._brand(brands, make)
        if brand is None:
            return fluid_report(PROVIDER, "EMPTY",
                                warnings=["Brembo has no car make by this name."])
        models = self._listing(("models", brand["brandCode"]),
                               "/catalogue/search/getsearchmodels",
                               {"brandCode": brand["brandCode"], "vehicleType": "Car"}, deadline)
        dated = [row for row in models
                 if in_years(year, row.get("modelDateStart"), row.get("modelDateEnd"))]
        chosen = [row for row in dated if contains(model, tokens(row.get("title")))]
        if len(chosen) != 1:
            # Brembo's own model titles, closest first; the assistant passes one back.
            pool = chosen or sorted(dated, key=lambda row: -len(
                set(model) & set(tokens(row.get("title")))))
            return self._choose("model", chosen, [str(row.get("title")) for row in pool])
        types = self._listing(("types", chosen[0]["modelCode"]),
                              "/catalogue/search/getsearchtypes",
                              {"modelCode": chosen[0]["modelCode"]}, deadline)
        engine = self._engines(types, vehicle, year)
        if len(engine) != 1:
            titles = [engine_title(row) for row in (engine or types)
                      if in_years(year, row.get("typeDateStart"), row.get("typeDateEnd"))]
            return self._choose("engine", engine, titles, model_title=chosen[0].get("title"))
        return self._products(brand, chosen[0], engine[0], year, deadline)

    @staticmethod
    def _brand(brands: list[dict], make: list[str]) -> dict | None:
        wanted = "".join(make)
        names = {"".join(tokens(row.get("brandName"))): row for row in brands
                 if row.get("brandCode")}
        if wanted in names:
            return names[wanted]
        # 'MERCEDES' for 'MERCEDES-BENZ': one brand starting with the name, never a guess.
        close = [row for name, row in names.items() if name.startswith(wanted)]
        return close[0] if len(close) == 1 else None

    @staticmethod
    def _engines(types: list[dict], vehicle: dict, year: int | None) -> list[dict]:
        rows = [row for row in types
                if in_years(year, row.get("typeDateStart"), row.get("typeDateEnd"))]
        litres = vehicle.get("displacement_l")
        if isinstance(litres, int | float) and litres > 0:
            rows = [row for row in rows
                    if (match := LITRES.match(str(row.get("typeName") or "")))
                    and abs(float(match.group(1)) - float(litres)) < 0.06]
        for key in ("engine_code", "trim"):
            words = tokens(vehicle.get(key))
            if words:
                rows = [row for row in rows if contains(
                    words, tokens(f"{row.get('title')} {row.get('engine') or ''}"))]
        return rows

    @staticmethod
    def _choose(level: str, chosen: list, titles: list[str], model_title: Any = None) -> dict:
        titles = list(dict.fromkeys(title for title in titles if title))[:MAX_CANDIDATES]
        label = redact(model_title, 120) or ""
        if not titles:
            return fluid_report(PROVIDER, "EMPTY", label=label,
                                warnings=[f"Brembo lists no {level} for this car and year."])
        status = "CONFLICTED" if chosen else "EMPTY"
        what = ("Several Brembo " + level + "s fit" if chosen
                else "No Brembo " + level + " matched the words given")
        return fluid_report(
            PROVIDER, status, label=label, candidates=titles,
            clarification=f"{what}. " + (
                "To choose, call again with use_vin false, the make and year, and one of "
                "these titles as model." if level == "model" else
                "To choose, call again with one of these titles as trim (or ask the "
                "customer which engine the car has)."))

    def _products(self, brand: dict, model: dict, engine: dict, year: int | None,
                  deadline: float | None) -> dict:
        found = self._post("/catalogue/search/searchtype", {
            "brandCode": brand["brandCode"], "modelYear": str(year) if year else None,
            "modelCode": model["modelCode"],
            "typeCodeAndVehicleType": engine["typeCodeAndVehicleType"]}, deadline)
        path = found.get("url") if isinstance(found, dict) else None
        if not isinstance(path, str) or not path.startswith("/europe/en/catalogue/"):
            raise PublicSourceError("INVALID_RESPONSE")
        page = self._send("GET", path, deadline=deadline).decode("utf-8", "replace")
        items = [item for kind in KINDS.values()
                 for item in [row for row in parse_results(page)
                              if row["component"].endswith(kind)][:MAX_PER_KIND]]
        label = redact(f"{brand.get('brandName')} {model.get('title')} · {engine.get('title')}",
                       180) or ""
        if not items:
            return fluid_report(PROVIDER, "EMPTY", label=label,
                                warnings=["Brembo lists no brake pads or discs for this car."])
        self._oe_numbers(items, deadline)
        return fluid_report(PROVIDER, "PROVISIONAL", label=label, items=items,
                            warnings=[SUPPLIER_WARNING])

    def _oe_numbers(self, items: list[dict], deadline: float | None) -> None:
        """The car makers' numbers for each product, three at a time; a miss is skipped."""
        def one(item: dict) -> list[dict]:
            try:
                rows = self._post("/catalogue/getproductmanufacturerreferences",
                                  {"bremboCode": item["product_code"]}, deadline)
            except PublicSourceError:
                return []
            return [row for row in rows if isinstance(row, dict) and row.get("code")][:20] \
                if isinstance(rows, list) else []

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="brembo-oe") as pool:
            for item, rows in zip(items, pool.map(one, items), strict=True):
                # Numbers as the maker writes them ('A 000 420 30 20'), makers alongside.
                item["oem_codes"] = [code for row in rows
                                     if (code := redact(row.get("code"), 40))]
                item["oem_makers"] = sorted({maker for row in rows
                                             if (maker := redact(row.get("brandsName"), 40))})


def fitment_note(box: Any) -> str:
    """The short fitment note ('Construction Year to:12/2006'). The site writes its class
    as a Vue binding, `:class="['short', ...]"`, so a plain class selector misses it."""
    info = box.select_one("div.vehicle-info")
    for part in info.find_all("div", recursive=False) if info else []:
        classes = " ".join([*(part.get("class") or []), str(part.get(":class") or "")])
        if re.search(r"(^|[\s'\"])short([\s'\"]|$)", classes):
            return " ".join(part.get_text(" ", strip=True).split())
    return ""


def parse_results(page: str) -> list[dict]:
    """Brake pads, discs, shoes and drums on a Brembo vehicle results page."""
    soup = BeautifulSoup(page, "html.parser")
    items: list[dict] = []
    seen: set[str] = set()
    for box in soup.find_all("app-globalcaritem"):
        kind = KINDS.get(str(box.get("product-type") or ""))
        head = box.select_one("button.codes-list")
        code = head.select_one("div.code span.label") if head else None
        number = redact(code.get_text(" ", strip=True), 40) if code else None
        if not kind or not number or number in seen:
            continue
        seen.add(number)
        line_tag = head.select_one("div.type span.label")
        line = redact(line_tag.get_text(" ", strip=True), 40) if line_tag else ""
        axle = box.select_one("div.axle div.tooltip")
        position = redact(axle.get_text(" ", strip=True), 20) if axle else ""
        specs = []
        for row in box.select("div.detail > div.data > div.item"):
            name, value = row.select_one("div.label"), row.select_one("div.detail")
            if name and value:
                text = " ".join(value.get_text(" ", strip=True).split())
                specs.append(f"{name.get_text(' ', strip=True)} {text}")
        variants = [redact(link.get_text(" ", strip=True), 40)
                    for link in head.select("div.info-product a.link")] if head else []
        note = fitment_note(box)
        items.append({
            "kind": "brake", "component": " ".join(filter(None, (position, kind))),
            "product_name": " ".join(filter(None, ("Brembo", line, kind))),
            "product_code": number, "position": position or None, "line": line or None,
            "details": redact("; ".join(specs), 400),
            "variants": [value for value in variants if value and value != number][:4],
            "note": redact(note, 200), "oem_codes": [], "oem_makers": [],
            "viscosity": None, "interval": None, "capacity": None,
        })
    return items
