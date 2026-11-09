"""Optional TecDoc-style catalog API: VIN to OE and part numbers per part group.

Turned on only when ``CATALOG_API_KEY`` is set. Fixed HTTPS host, GET only, no redirects,
bounded responses, no key or VIN in errors. The response shapes are parsed tolerantly
because they are known from the vendor's documentation, not from a live account: run
``uv run python -m automotive --check-catalog-api`` once after adding the key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote

from automotive.categories import category_for_query
from automotive.public_http import PublicHttp, PublicSourceError, redact
from automotive.vin import validate_vin

DEFAULT_BASE_URL = 'https://auto-parts-catalog.p.rapidapi.com'
DEFAULT_HOST = 'auto-parts-catalog.p.rapidapi.com'
DEFAULT_KEY_HEADER = 'x-rapidapi-key'
LANG_ID, TYPE_ID = 4, 1  # English, passenger cars
# Part groups asked for each chat category: the leaf names in the vehicle's category tree.
CATEGORY_TERMS = {
    'oil_filter': ['Oil Filter'], 'oil_change': ['Oil Filter'],
    'air_filter': ['Air Filter'], 'cabin_filter': ['Cabin Air Filter'],
    'brake_pads': ['Brake Pad'], 'brake_discs': ['Brake Disc'],
    'brakes': ['Brake Pad', 'Brake Disc'], 'suspension': ['Shock Absorber'],
}
DEFAULT_BROAD = ['oil_filter', 'air_filter', 'cabin_filter', 'brake_pads', 'brake_discs']
# Product names that mean the part itself (the group also holds wear sensors and kits).
PRODUCT_NAMES = {'brake pad': 'brake pad set'}
LINKAGE_TYPES = {'P': 1, 'O': 2, 'M': 3}  # passenger, commercial, motorbike
ARTICLE_KEYS = ('articleNo', 'articleNumber', 'article_no', 'partNumber')
SUPPLIER_KEYS = ('supplierName', 'brandName', 'supplier', 'mfrName')
OEM_KEYS = ('oemNumbers', 'oeNumbers', 'oem', 'oenumbers', 'oemNo')
OEM_CODE_KEYS = ('oemDisplayNo', 'oemNumber', 'articleNumber', 'oemNo', 'number')
MAX_ARTICLES, MAX_OEM = 150, 12
CACHE_SECONDS = 24 * 3600  # Catalog data is stable; stock is never cached here.
PARALLEL_CATEGORIES = 4


def categories_for(query: str) -> list[str]:
    """Chat category keys to request; a blank query asks a small broad set."""
    if not query.strip():
        return list(DEFAULT_BROAD)
    kind = category_for_query(query)
    return [kind] if kind in CATEGORY_TERMS else []


def _dicts(value: Any, depth: int = 0, limit: int = 500):
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child, depth + 1, limit)
    elif isinstance(value, list):
        for child in value[:limit]:
            yield from _dicts(child, depth + 1, limit)


def _text(row: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            return redact(str(value), 80)
    return None


def parse_vehicles(payload: Any) -> list[dict]:
    found: OrderedDict[str, dict] = OrderedDict()
    for row in _dicts(payload):
        identifier = row.get('vehicleId', row.get('carId'))
        if isinstance(identifier, bool) or not str(identifier or '').isdigit():
            continue
        found.setdefault(str(identifier), {
            'vehicle_id': int(identifier),
            'type_id': row.get('typeId') if isinstance(row.get('typeId'), int)
            else LINKAGE_TYPES.get(str(row.get('linkageTargetType')), TYPE_ID),
            'label': redact(row.get('carName') or ' '.join(str(row.get(k) or '') for k in (
                'manufacturerName', 'modelName', 'typeName')), 120),
        })
    return list(found.values())


def parse_articles(payload: Any) -> list[dict]:
    articles: list[dict] = []
    seen: set[str] = set()
    for row in _dicts(payload):
        code = _text(row, ARTICLE_KEYS)
        if not code or code in seen:
            continue
        seen.add(code)
        oem: list[str] = []
        for key in OEM_KEYS:
            value = row.get(key)
            for entry in value if isinstance(value, list) else []:
                number = (_text(entry, OEM_CODE_KEYS) if isinstance(entry, dict)
                          else redact(entry, 60))
                if number and number not in oem:
                    oem.append(number)
        articles.append({'code': code, 'supplier': _text(row, SUPPLIER_KEYS),
                         'product': _text(row, ('articleProductName', 'productName')),
                         'oem_codes': oem[:MAX_OEM]})
        if len(articles) >= MAX_ARTICLES:
            break
    return articles


def parse_group_ids(payload: Any) -> dict[str, list[int]]:
    """Leaf part-group name (casefolded) to its ids, from the vehicle's category rows."""
    groups: dict[str, list[int]] = {}
    for row in _dicts(payload, limit=5000):  # A vehicle's category rows run to ~700.
        names = [row.get(f'categoryName{i}') for i in range(1, 5)]
        ids = [row.get(f'categoryId{i}') for i in range(1, 5)]
        leaf = max((i for i in range(4) if names[i] and ids[i] is not None), default=None)
        if leaf is None or isinstance(ids[leaf], bool) or not str(ids[leaf]).isdigit():
            continue
        known = groups.setdefault(str(names[leaf]).casefold(), [])
        if int(ids[leaf]) not in known:
            known.append(int(ids[leaf]))
    return groups


class CatalogApiClient:
    provider = 'Catalog API'

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 host: str = DEFAULT_HOST, key_header: str = DEFAULT_KEY_HEADER,
                 per_minute: int = 60, hmac_key: str = '') -> None:
        if not api_key or not base_url.startswith('https://'):
            raise ValueError('A key and an HTTPS base URL are required.')
        self.base_url = base_url.rstrip('/')
        self.http = PublicHttp(self.base_url + '/', per_minute=per_minute)
        self._headers = {key_header: api_key}
        if key_header.lower() == 'x-rapidapi-key':
            self._headers['x-rapidapi-host'] = host
        self._groups: OrderedDict[int, tuple[float, dict[str, list[int]]]] = OrderedDict()
        self._vehicles: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._articles: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
        # The VIN cache is keyed by an HMAC so a raw VIN is never held as a dictionary key.
        self._key = (hmac_key or secrets.token_urlsafe(32)).encode()
        self._lock = Lock()

    def _get(self, path: str, deadline: float | None) -> Any:
        try:
            status, body = self.http.read('GET', f'{self.base_url}{path}',
                                          headers=self._headers, deadline=deadline)
        except PublicSourceError as exc:
            if exc.code not in {'RATE_LIMITED', 'PROVIDER_UNAVAILABLE'}:
                raise
            status, body = self._retry(path, deadline)
        if status >= 500:
            status, body = self._retry(path, deadline)
        if status in {401, 403}:
            raise PublicSourceError('ACCESS_REQUIRED')
        if status == 404:
            return None
        if status != 200:
            raise PublicSourceError('PROVIDER_UNAVAILABLE')
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise PublicSourceError('INVALID_RESPONSE') from None

    def _cached(self, store: OrderedDict, key: Any) -> Any:
        with self._lock:
            entry = store.get(key)
            if entry and entry[0] > monotonic():
                return entry[1]
        return None

    def _remember(self, store: OrderedDict, key: Any, value: Any, limit: int = 400) -> None:
        with self._lock:
            store[key] = (monotonic() + CACHE_SECONDS, value)
            while len(store) > limit:
                store.popitem(last=False)

    def _retry(self, path: str, deadline: float | None) -> tuple[int, bytes]:
        """One short retry for a transient provider error; never a loop."""
        sleep(0.6)
        return self.http.read('GET', f'{self.base_url}{path}', headers=self._headers,
                              deadline=deadline)

    def vehicle(self, vin: str, deadline: float | None) -> list[dict]:
        vin = validate_vin(vin)
        key = hmac.new(self._key, vin.encode(), hashlib.sha256).hexdigest()
        cached = self._cached(self._vehicles, key)
        if cached is not None:
            return cached
        self.http.reserve()
        payload = self._get(f'/vin/tecdoc-vin-check/{quote(vin)}', deadline)
        vehicles = parse_vehicles(payload)
        self._remember(self._vehicles, key, vehicles, 1000)
        return vehicles

    def warm(self, vin: str, deadline: float | None = None) -> None:
        """Resolve the vehicle and its part groups ahead of the customer's part question."""
        matches = self.vehicle(vin, deadline)
        if len(matches) == 1:
            self.groups(matches[0], deadline)

    def groups(self, car: dict, deadline: float | None) -> dict[str, list[int]]:
        """The part groups this exact vehicle has, by leaf name (one cached call per car)."""
        cached = self._cached(self._groups, car['vehicle_id'])
        if cached is not None:
            return cached
        self.http.reserve()
        payload = self._get(
            f"/category/type-id/{car['type_id']}/products-groups-variant-1/{car['vehicle_id']}"
            f'/lang-id/{LANG_ID}', deadline)
        groups = parse_group_ids(payload)
        self._remember(self._groups, car['vehicle_id'], groups)
        return groups

    def _category(self, car: dict, groups: dict, key: str,
                  deadline: float | None) -> list[dict]:
        """Articles for one chat category; the same part can sit under two groups."""
        items: list[dict] = []
        for term in CATEGORY_TERMS.get(key, []):
            for group in groups.get(term.casefold(), []):
                cache_key = (car['type_id'], car['vehicle_id'], group)
                articles = self._cached(self._articles, cache_key)
                if articles is None:
                    self.http.reserve()
                    payload = self._get(
                        f"/articles/list/type-id/{car['type_id']}/vehicle-id/{car['vehicle_id']}"
                        f'/category-id/{group}/lang-id/{LANG_ID}', deadline)
                    articles = parse_articles(payload)
                    self._remember(self._articles, cache_key, articles, 2000)
                # Keep articles named for the part when the group is broader than it.
                wanted = PRODUCT_NAMES.get(term.casefold(), term.casefold())
                named = [a for a in articles if wanted in str(a.get('product') or '').casefold()]
                chosen = named or articles
                items.extend({**a, 'category': key} for a in chosen)
                if chosen:
                    break  # One group is enough.
        return items

    def lookup(self, vin: str, vehicle: dict, categories: list[str], *,
               deadline: float | None = None) -> dict:
        """OE and part numbers for the VIN's vehicle, per requested chat category."""
        del vehicle  # The catalog identifies the car from the VIN itself.
        if deadline is not None and monotonic() >= deadline:
            raise PublicSourceError('TIME_LIMIT')
        matches = self.vehicle(vin, deadline)
        report: dict[str, Any] = {'provider': self.provider, 'items': [], 'warnings': [],
                                  'vehicle_label': matches[0]['label'] if matches else ''}
        if not matches:
            report['status'] = 'EMPTY'
            return report
        if len(matches) > 1:
            # Several catalog vehicles for one VIN: parts could differ, so none is chosen.
            report['status'] = 'CONFLICTED'
            return report
        car = matches[0]
        groups = self.groups(car, deadline)
        keys = list(dict.fromkeys(categories))
        if len(keys) > 1:
            # Independent part groups are fetched side by side.
            with ThreadPoolExecutor(max_workers=min(PARALLEL_CATEGORIES, len(keys))) as pool:
                results = list(pool.map(lambda k: self._category(car, groups, k, deadline), keys))
        else:
            results = [self._category(car, groups, k, deadline) for k in keys]
        for rows in results:
            report['items'].extend(rows)
        report['status'] = 'PROVISIONAL' if report['items'] else 'EMPTY'
        return report
