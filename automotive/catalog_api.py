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
import re
import secrets
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote

from automotive.categories import category_for_query
from automotive.public_http import PublicHttp, PublicSourceError, redact
from automotive.ttl_cache import SQLiteTTLCache
from automotive.vin import validate_vin

DEFAULT_BASE_URL = 'https://auto-parts-catalog.p.rapidapi.com'
DEFAULT_HOST = 'auto-parts-catalog.p.rapidapi.com'
DEFAULT_KEY_HEADER = 'x-rapidapi-key'
LANG_ID, TYPE_ID = 4, 1  # English, passenger cars
COUNTRY_ID = 63  # Provider's documented worldwide fallback; not a claimed vehicle market.
PROVIDER_DOCUMENTATION = 'https://auto-parts-catalog.apiprofile.com/documentation'
# Part groups asked for each chat category: the leaf names in the vehicle's category tree.
CATEGORY_TERMS = {
    'oil_filter': ['Oil Filter'], 'oil_change': ['Oil Filter'],
    'air_filter': ['Air Filter'], 'cabin_filter': ['Cabin Air Filter'],
    'brake_pads': ['Brake Pad'], 'brake_discs': ['Brake Disc'],
    'brakes': ['Brake Pad', 'Brake Disc'], 'suspension': ['Shock Absorber'],
    'fuel_filter': ['Fuel Filter'], 'spark_plugs': ['Spark Plug'],
    'battery': ['Starter Battery'], 'wipers': ['Wiper Blade'],
    'timing_belt': ['Timing Belt Kit', 'Timing Belt'], 'clutch': ['Clutch Kit'],
    'bulbs': ['Bulb, headlight'],
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
    seen: set[tuple[str, str | None]] = set()
    for row in _dicts(payload):
        code = _text(row, ARTICLE_KEYS)
        supplier = _text(row, SUPPLIER_KEYS)
        identity = (code, supplier)
        if not code or identity in seen:
            continue
        seen.add(identity)
        oem: list[str] = []
        for key in OEM_KEYS:
            value = row.get(key)
            for entry in value if isinstance(value, list) else []:
                number = (_text(entry, OEM_CODE_KEYS) if isinstance(entry, dict)
                          else redact(entry, 60))
                if number and number not in oem:
                    oem.append(number)
        articles.append({'code': code, 'supplier': supplier,
                         'supplier_id': _text(row, ('supplierId', 'mfrId')),
                         'article_id': _text(row, ('articleId',)),
                         'product': _text(row, ('articleProductName', 'productName')),
                         'oem_codes': oem[:MAX_OEM]})
        if len(articles) >= MAX_ARTICLES:
            break
    return articles


def _normalized(value: Any) -> str:
    return re.sub(r'[^\w]+', '', str(value or '').casefold())


def _year(value: Any) -> int | None:
    match = re.search(r'(?:19|20)\d{2}', str(value or ''))
    return int(match.group()) if match else None


def _years(row: dict) -> tuple[int | None, int | None]:
    return (_year(_text(row, ('yearOfConstructionFrom', 'constructionIntervalStart',
                             'yearFrom', 'modelYearFrom', 'yearStart', 'beginYearMonth'))),
            _year(_text(row, ('yearOfConstructionTo', 'constructionIntervalEnd',
                             'yearTo', 'modelYearTo', 'yearEnd', 'endYearMonth'))))


def _within_year(row: dict, year: Any) -> bool:
    start, end = _years(row)
    current = _year(year)
    return not current or ((start is None or start <= current) and (end is None or end >= current))


def _fuel(value: Any) -> str:
    text = _normalized(value)
    if any(word in text for word in ('petrol', 'gasoline', 'ბენზინ')):
        return 'petrol'
    if any(word in text for word in ('diesel', 'დიზელ')):
        return 'diesel'
    return text


def _body(value: Any) -> str:
    text = _normalized(value)
    for name, words in {
        'coupe': ('coupe', 'coupé', 'კუპე'),
        'saloon': ('saloon', 'sedan', 'სედანი'),
        'estate': ('estate', 'wagon', 'touring', 'უნივერსალი'),
        'convertible': ('convertible', 'cabrio', 'კაბრიო'),
        'hatchback': ('hatchback', 'ჰეჩბეკი'),
    }.items():
        if any(word in text for word in words):
            return name
    return text


def _engine_codes(row: dict) -> list[str]:
    value = row.get('engineCodes', row.get('motorCodes', row.get('engineCode')))
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        code = (_text(item, ('engineCode', 'motorCode', 'code')) if isinstance(item, dict)
                else redact(item, 64))
        if code and code not in result:
            result.append(code)
    return result[:12]


def _capacity(row: dict) -> float | None:
    value = _text(row, ('cylinderCapacityCcm', 'cylinderCapacity', 'capacityCcm',
                        'cubicCapacity', 'ccm', 'cc', 'displacement'))
    try:
        number = float(str(value).replace(',', '.'))
        return round(number / 1000 if number > 100 else number, 3)
    except (ValueError, TypeError):
        return None


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
                 per_minute: int = 60, hmac_key: str = '',
                 cache: SQLiteTTLCache | None = None) -> None:
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
        self.cache = cache
        self._cooldown_until = 0.0
        self._cooldown_reason = 'RATE_LIMITED'
        self._unsupported_routes: set[str] = set()

    def _get(self, path: str, deadline: float | None) -> Any:
        self._check_deadline(deadline)
        if monotonic() < self._cooldown_until:
            raise PublicSourceError(self._cooldown_reason)
        retried = False
        try:
            status, body = self.http.read('GET', f'{self.base_url}{path}',
                                          headers=self._headers, deadline=deadline)
        except PublicSourceError as exc:
            if exc.code in {'RATE_LIMITED', 'ACCESS_REQUIRED'}:
                self._cooldown_reason = exc.code
                self._cooldown_until = monotonic() + (exc.retry_after or 60)
                raise
            if exc.code != 'PROVIDER_UNAVAILABLE':
                raise
            status, body = self._retry(path, deadline)
            retried = True
        if status in {405, 501} and path.startswith('/api/'):
            raise PublicSourceError('CAPABILITY_UNAVAILABLE')
        if status >= 500 and not retried:
            status, body = self._retry(path, deadline)
        if status in {401, 403}:
            self._cooldown_reason = 'ACCESS_REQUIRED'
            self._cooldown_until = monotonic() + 60
            raise PublicSourceError('ACCESS_REQUIRED')
        if status == 429:
            self._cooldown_reason = 'RATE_LIMITED'
            self._cooldown_until = monotonic() + 60
            raise PublicSourceError('RATE_LIMITED')
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
            store[key] = (monotonic() + (CACHE_SECONDS if value else 60), value)
            while len(store) > limit:
                store.popitem(last=False)

    def _retry(self, path: str, deadline: float | None) -> tuple[int, bytes]:
        """One short retry for a transient provider error; never a loop."""
        if deadline is not None and deadline - monotonic() < 0.7:
            raise PublicSourceError('TIME_LIMIT')
        sleep(0.6)
        try:
            return self.http.read('GET', f'{self.base_url}{path}', headers=self._headers,
                                  deadline=deadline)
        except PublicSourceError as exc:
            if exc.code in {'RATE_LIMITED', 'ACCESS_REQUIRED'}:
                self._cooldown_reason = exc.code
                self._cooldown_until = monotonic() + (exc.retry_after or 60)
            raise

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and monotonic() >= deadline:
            raise PublicSourceError('TIME_LIMIT')

    def _documented_get(self, path: str, deadline: float | None) -> Any:
        """Version-2 endpoints are a capability, never guessed legacy aliases."""
        self._check_deadline(deadline)
        route = path.split('/type-id/')[0]
        if route in self._unsupported_routes:
            raise PublicSourceError('CAPABILITY_UNAVAILABLE')
        key = {'base_url': self.base_url, 'path': path}
        if self.cache:
            cached = self.cache.get('catalog.routes', key)
            if cached is not None:
                return cached
        self.http.reserve()
        try:
            payload = self._get(path, deadline)
        except PublicSourceError as exc:
            if exc.code == 'CAPABILITY_UNAVAILABLE':
                self._unsupported_routes.add(route)
            raise
        if payload is None:
            self._unsupported_routes.add(route)
            raise PublicSourceError('CAPABILITY_UNAVAILABLE')
        if self.cache:
            has_rows = any(_text(row, ('manuId', 'manufacturerId', 'modelId',
                                       'vehicleId', 'carId', 'kTypNr'))
                           for row in _dicts(payload, limit=2000))
            self.cache.put('catalog.routes', key, payload, empty=not has_rows)
        return payload

    def resolve_vehicle(self, vehicle: dict, *, deadline: float | None = None) -> dict:
        """Resolve stated details through manufacturer → model → engine variants.

        Source assertions stay provisional; a unique variant is not reviewed fitment.
        Missing source criteria remain explicit instead of being filled by model knowledge.
        """
        self._check_deadline(deadline)
        fields = ('make', 'model', 'model_year', 'engine_code', 'displacement_l',
                  'fuel_type', 'fuel', 'trim', 'body', 'drive', 'market')
        stated = {key: vehicle[key] for key in fields if vehicle.get(key) is not None}
        cache_key = {'base_url': self.base_url, 'vehicle': stated}
        if self.cache:
            cached = self.cache.get('catalog.resolution', cache_key)
            if cached is not None:
                return {**cached, 'cache_hit': True}
        missing = [key for key in ('make', 'model') if not stated.get(key)]
        report = {'provider': self.provider, 'status': 'UNRESOLVED', 'records': [],
                  'candidates': [], 'sources': [], 'applicability': stated,
                  'unresolved_fields': missing, 'failure_reason': None, 'cache_hit': False}
        if missing:
            report['failure_reason'] = 'MISSING_VEHICLE_DETAILS'
            return report
        manufacturers = self._documented_get('/api/manufacturers/list/type-id/1', deadline)
        make = _normalized(stated['make'])
        make_aliases = {'mercedes': 'mercedesbenz', 'mb': 'mercedesbenz',
                        'vw': 'volkswagen'}
        make = make_aliases.get(make, make)
        selected_makes = []
        for row in _dicts(manufacturers, limit=2000):
            identifier = _text(row, ('manuId', 'manufacturerId', 'id'))
            label = _text(row, ('manuName', 'manufacturerName', 'name'))
            if identifier and identifier.isdigit() and label and _normalized(label) == make:
                selected_makes.append((int(identifier), label))
        candidates: OrderedDict[int, dict] = OrderedDict()
        for manufacturer_id, make_name in selected_makes[:2]:
            model_path = (f'/api/models/list/type-id/1/manufacturer-id/{manufacturer_id}'
                          f'/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}')
            models = self._documented_get(model_path, deadline)
            selected_models: OrderedDict[int, tuple[str, dict]] = OrderedDict()
            for row in _dicts(models, limit=2000):
                identifier = _text(row, ('modelId', 'id'))
                name = _text(row, ('modelName', 'name'))
                if not identifier or not identifier.isdigit() or not name:
                    continue
                if _normalized(stated['model']) not in _normalized(name):
                    continue
                if not _within_year(row, stated.get('model_year')):
                    continue
                source_body = _body(row.get('bodyType') or name)
                requested_body = _body(stated.get('body'))
                known_bodies = {'coupe', 'saloon', 'estate', 'convertible', 'hatchback'}
                if requested_body and source_body in known_bodies and requested_body != source_body:
                    continue
                selected_models.setdefault(int(identifier), (name, row))
            for model_id, (model_name, model_row) in list(selected_models.items())[:8]:
                self._check_deadline(deadline)
                path = (f'/api/types/type-id/1/list-vehicles-types/{model_id}'
                        f'/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}')
                variants = self._documented_get(path, deadline)
                source_id = f'catalog.model.{model_id}'
                report['sources'].append({'id': source_id, 'provider': self.provider,
                                          'url': self.base_url + path,
                                          'kind': 'supplier_assertion'})
                for row in _dicts(variants, limit=2000):
                    candidate = self._candidate(row, stated, make_name, model_name, model_row,
                                                source_id)
                    if candidate:
                        candidates.setdefault(candidate['vehicle_id'], candidate)
        report['records'] = report['candidates'] = list(candidates.values())[:100]
        if not candidates:
            report.update(status='EMPTY', failure_reason='NO_MATCHING_VARIANTS')
        elif len(candidates) == 1:
            candidate = next(iter(candidates.values()))
            report['unresolved_fields'] = candidate['unresolved_fields']
            report['status'] = 'UNRESOLVED' if candidate['unresolved_fields'] else 'RESOLVED'
        else:
            report['status'] = 'AMBIGUOUS'
            for field in ('fuel_type', 'trim', 'engine_code', 'body', 'drive', 'market'):
                values = {str(row.get(field) or '') for row in candidates.values()}
                if not stated.get(field) and len(values) > 1:
                    report['unresolved_fields'].append(field)
            if not report['unresolved_fields']:
                report['unresolved_fields'] = ['engine_code']
        if self.cache:
            self.cache.put('catalog.resolution', cache_key, report, empty=not candidates)
        return report

    @staticmethod
    def _candidate(row: dict, stated: dict, make: str, model: str,
                   model_row: dict, source_id: str) -> dict | None:
        identifier = _text(row, ('vehicleId', 'carId', 'kTypNr'))
        if not identifier or not identifier.isdigit() or not _within_year(
                row, stated.get('model_year')):
            return None
        label = _text(row, ('typeName', 'carName', 'vehicleName', 'name')) or ''
        codes = _engine_codes(row)
        fuel = _text(row, ('fuelType', 'fuelTypeName', 'fuel', 'fuelName'))
        displacement = _capacity(row)
        body = _text(row, ('bodyType', 'bodyTypeName', 'body'))
        if not body and _body(model) in {'coupe', 'saloon', 'estate', 'convertible', 'hatchback'}:
            body = _body(model)
        drive = _text(row, ('driveType', 'driveTypeName', 'drive'))
        market = _text(row, ('market', 'marketName'))
        requested_fuel = stated.get('fuel_type') or stated.get('fuel')
        if requested_fuel and fuel and _fuel(requested_fuel) != _fuel(fuel):
            return None
        if stated.get('engine_code') and codes and not any(
                _normalized(stated['engine_code']) == _normalized(code) for code in codes):
            return None
        try:
            if (stated.get('displacement_l') and displacement
                    and abs(float(stated['displacement_l']) - displacement) > 0.11):
                return None
        except (TypeError, ValueError):
            return None
        if stated.get('trim') and label and _normalized(stated['trim']) not in _normalized(label):
            return None
        for field, source in (('body', body), ('drive', drive), ('market', market)):
            wanted = stated.get(field)
            normalize = _body if field == 'body' else _normalized
            if wanted and source and normalize(wanted) != normalize(source):
                return None
        start, end = _years(row)
        model_start, model_end = _years(model_row)
        config = {'make': make, 'model': model,
                  'engine_code': codes[0] if len(codes) == 1 else None,
                  'engine_codes': codes, 'fuel_type': _fuel(fuel) if fuel else None,
                  'displacement_l': displacement, 'trim': label or None, 'body': body,
                  'drive': drive, 'market': market}
        unresolved = [key for key in ('engine_code', 'fuel_type', 'displacement_l')
                      if config.get(key) is None]
        for key in ('body', 'drive', 'market'):
            if stated.get(key) and not config.get(key):
                unresolved.append(key)
        if stated.get('model_year') and not any((start, end, model_start, model_end)):
            unresolved.append('model_year')
        return {'vehicle_id': int(identifier), 'type_id': TYPE_ID,
                'label': f'{make} {model} {label}'.strip(), **config,
                'year_from': start or model_start, 'year_to': end or model_end,
                'source_refs': [source_id], 'compatibility': 'SUPPLIER_ASSERTED',
                'unresolved_fields': unresolved,
                'applicability': {'year_from': start or model_start, 'year_to': end or model_end,
                                  **{key: value for key, value in config.items() if value}}}

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

    def lookup(self, vin: str | None, vehicle: dict, categories: list[str], *,
               deadline: float | None = None) -> dict:
        """Supplier codes from a VIN or a unique source-supported no-VIN variant."""
        self._check_deadline(deadline)
        resolution = None
        if vin:
            matches = self.vehicle(vin, deadline)
        else:
            resolution = self.resolve_vehicle(vehicle, deadline=deadline)
            matches = resolution['candidates']
        report: dict[str, Any] = {'provider': self.provider, 'items': [], 'warnings': [],
                                  'vehicle_label': matches[0]['label'] if matches else ''}
        if resolution:
            report.update(candidates=matches, sources=resolution['sources'],
                          unresolved_fields=resolution['unresolved_fields'],
                          applicability=resolution['applicability'],
                          failure_reason=resolution['failure_reason'])
        if not matches:
            report['status'] = 'EMPTY'
            return report
        if len(matches) > 1:
            # Several catalog vehicles for one VIN: parts could differ, so none is chosen.
            report['status'] = 'CONFLICTED'
            return report
        car = matches[0]
        if not vin and car.get('unresolved_fields'):
            # A variant can lack fields in an incomplete provider response.
            report['warnings'].append('INCOMPLETE_VEHICLE_SPECIFICATION')
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
