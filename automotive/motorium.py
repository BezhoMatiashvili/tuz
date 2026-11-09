"""Observed public Motorium protocol. No model prose is promoted to technical facts."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from datetime import UTC, datetime
from html.parser import HTMLParser
from threading import Lock
from time import monotonic
from typing import Any

import requests

from automotive.codes import normalize_code, parse_codes
from automotive.vin import validate_vin

ORIGIN = 'https://motorium.ge'
ASSISTANT_URL = ORIGIN + '/api/ai-assistant.php'
VIN_IN_TEXT = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b', re.IGNORECASE)


class MotoriumError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__('Motorium lookup unavailable')
        self.code = code


def _text(value: Any, maximum: int = 300) -> str | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    value = VIN_IN_TEXT.sub('[VIN]', str(value)).strip()
    return value[:maximum] or None


def _identity(value: Any) -> str:
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


class ProductMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.current: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'script' and dict(attrs).get('type') == 'application/ld+json':
            self.active, self.current = True, []

    def handle_data(self, data: str) -> None:
        if self.active:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'script' and self.active:
            self.documents.append(''.join(self.current))
            self.active = False

    def product(self) -> dict | None:
        for document in self.documents:
            try:
                value = json.loads(document)
            except (ValueError, RecursionError):
                continue
            nodes = value if isinstance(value, list) else [value]
            for node in nodes[:100]:
                if not isinstance(node, dict):
                    continue
                if node.get('@type') == 'Product':
                    return node
                graph = node.get('@graph', [])
                if isinstance(graph, list):
                    for child in graph[:100]:
                        if isinstance(child, dict) and child.get('@type') == 'Product':
                            return child
        return None


class MotoriumClient:
    """Each lookup uses a fresh anonymous session and repeats the VIN explicitly.

    Observed context-only follow-ups can lose the VIN and return generic products.
    Never accept caller-supplied history/context, cookies, endpoints or source URLs.
    """

    def __init__(self) -> None:
        self._requests: deque[float] = deque()
        self._lock = Lock()

    def _reserve(self) -> None:
        now = monotonic()
        with self._lock:
            while self._requests and self._requests[0] < now - 60:
                self._requests.popleft()
            if len(self._requests) >= 10:
                raise MotoriumError('RATE_LIMITED')
            self._requests.append(now)

    def _body(self, session: requests.Session, url: str, payload: dict | None = None,
              deadline: float | None = None) -> bytes:
        response = None
        try:
            remaining = deadline - monotonic() if deadline is not None else 30.0
            if remaining <= 0:
                raise MotoriumError('TIME_LIMIT')
            timeout = (min(3.05, remaining), min(20 if payload is None else 30, remaining))
            if payload is None:
                response = session.get(url, timeout=timeout, allow_redirects=False, stream=True)
            else:
                response = session.post(url, json=payload, timeout=timeout,
                                        allow_redirects=False, stream=True)
            if response.status_code == 429:
                raise MotoriumError('RATE_LIMITED')
            if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                raise MotoriumError('ACCESS_REQUIRED')
            if response.status_code != 200:
                raise MotoriumError('PROVIDER_UNAVAILABLE')
            body = bytearray()
            for chunk in response.iter_content(8192):
                if deadline is not None and monotonic() >= deadline:
                    raise MotoriumError('TIME_LIMIT')
                if len(body) + len(chunk) > 2_000_000:
                    raise MotoriumError('INVALID_RESPONSE')
                body.extend(chunk)
            return bytes(body)
        except requests.RequestException:
            raise MotoriumError('PROVIDER_UNAVAILABLE') from None
        finally:
            if response is not None:
                response.close()

    def lookup(self, vin: str, part_query: str = '', limit: int = 6,
               *, deadline: float | None = None) -> dict:
        vin = validate_vin(vin)
        if not isinstance(part_query, str) or len(part_query) > 160:
            raise ValueError('Part query must contain at most 160 characters.')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 6:
            raise ValueError('Limit must be between 1 and 6.')
        if deadline is not None and monotonic() >= deadline:
            raise MotoriumError('TIME_LIMIT')
        self._reserve()
        with requests.Session() as session:
            session.headers.update({
                'Accept': 'application/json, text/html;q=0.9',
                'X-Requested-With': 'XMLHttpRequest', 'Origin': ORIGIN, 'Referer': ORIGIN + '/',
                'User-Agent': 'automotive-intelligence/0.1 (read-only)',
            })
            payload = {
                'message': f'VIN {vin}. {part_query.strip() or "ამ ავტომობილის ნაწილები"}. '
                           'ნაწილის OEM/MPN კოდები მინდა.',
                'history': [], 'context': {'vehicle': None, 'part': None},
            }
            try:
                source = json.loads(self._body(session, ASSISTANT_URL, payload, deadline))
            except (ValueError, RecursionError):
                raise MotoriumError('INVALID_RESPONSE') from None
            if not isinstance(source, dict) or not isinstance(source.get('products'), list):
                raise MotoriumError('INVALID_RESPONSE')
            result = self._result(source, vin, part_query)
            if result['status'] == 'CONFLICTED' or not result['vehicle_context_matched']:
                return result
            exhausted = False
            for raw in source['products'][:limit]:
                if not isinstance(raw, dict):
                    continue
                item = self._item(raw)
                if item is None:
                    continue
                try:
                    metadata = ProductMetadata()
                    metadata.feed(self._body(session, item['source_url'],
                                             deadline=deadline).decode('utf-8'))
                    self._details(item, metadata.product())
                except MotoriumError as error:
                    if error.code in ('RATE_LIMITED', 'ACCESS_REQUIRED'):
                        raise
                    item['warnings'].append('PRODUCT_DETAILS_UNAVAILABLE')
                    exhausted = error.code == 'TIME_LIMIT'
                except (UnicodeError, ValueError, RecursionError):
                    item['warnings'].append('PRODUCT_DETAILS_UNAVAILABLE')
                result['items'].append(item)
                if exhausted:
                    result['warnings'].append('TIME_LIMIT')
                    break
            result['has_more'] = len(source['products']) > limit or exhausted
            if result['items']:
                result['status'] = ('CONFLICTED' if any(i['status'] == 'CONFLICTED'
                                                      for i in result['items']) else 'PROVISIONAL')
                result['clarification'] = None
            return result

    @staticmethod
    def _result(source: dict, vin: str, part_query: str) -> dict:
        decoded = source.get('vehicle')
        context = source.get('context')
        catalog = context.get('vehicle') if isinstance(context, dict) else None
        vehicle = {}
        if isinstance(decoded, dict):
            vehicle = {key: _text(decoded.get(field)) for key, field in (
                ('make', 'make'), ('model', 'model'), ('model_year', 'year'),
                ('body', 'generation'), ('drive', 'drive_type'),
            )}
            vehicle['engine_code'] = None
            if str(vehicle.get('model_year', '')).isdigit():
                vehicle['model_year'] = int(vehicle['model_year'])
        result = {
            'provider': 'Motorium', 'status': 'INSUFFICIENT_DATA', 'vehicle': vehicle,
            'vehicle_context_matched': False, 'items': [], 'has_more': False,
            'observed_at': datetime.now(UTC).isoformat(),
            'warnings': ['Motorium-ის კატალოგის კანდიდატებია; VIN-ის ზუსტი თავსებადობა '
                         'დამოუკიდებლად არ არის გადამოწმებული. კოდები ჩვენი მარაგის რაოდენობას '
                         'არ ადასტურებს.'],
            'clarification': 'დააზუსტეთ ნაწილის სახეობა ან მოძებნეთ ცნობილი ნაწილის კოდი.',
        }
        if not isinstance(decoded, dict) or not isinstance(catalog, dict):
            result['warnings'].append('VIN_CONTEXT_NOT_ESTABLISHED')
            return result
        if not isinstance(decoded.get('frame'), str) or not decoded['frame'].strip():
            result['warnings'].append('VIN_CONTEXT_NOT_ESTABLISHED')
            return result
        if not all(isinstance(data.get(key), str) and data[key].strip()
                   for data in (decoded, catalog) for key in ('make', 'model')):
            result['warnings'].append('VIN_CONTEXT_NOT_ESTABLISHED')
            return result
        if not all(not isinstance(data.get('year'), bool) and
                   str(data.get('year', '')).isdigit() and
                   1886 <= int(data['year']) <= datetime.now(UTC).year + 2
                   for data in (decoded, catalog)):
            result['warnings'].append('VIN_CONTEXT_NOT_ESTABLISHED')
            return result
        if (not all(decoded.get(k) and catalog.get(k) for k in ('make', 'model', 'year'))
                or any(_identity(decoded.get(k)) != _identity(catalog.get(k))
                       for k in ('make', 'model', 'year'))
                or (decoded.get('frame') and str(decoded['frame']).strip().upper() != vin)):
            result['status'] = 'CONFLICTED'
            result['warnings'].append('VEHICLE_CONTEXT_MISMATCH')
            result['clarification'] = 'Motorium-მა VIN სხვა მოდელს დაუკავშირა; კოდები არ დაბრუნდა.'
            return result
        if part_query.strip() and not isinstance(context.get('part'), dict):
            result['warnings'].append('PART_QUERY_NOT_ESTABLISHED')
            return result
        result['vehicle_context_matched'] = True
        return result

    @staticmethod
    def _item(raw: dict) -> dict | None:
        identifier = raw.get('id')
        if isinstance(identifier, bool) or not str(identifier).isdigit():
            return None
        if len(str(identifier)) > 18:
            return None
        mpn = _text(raw.get('mpn'))
        if not mpn:
            return None
        return {
            'provider_product_id': int(identifier), 'name': _text(raw.get('name')),
            'manufacturer': _text(raw.get('brand')), 'mpn': mpn,
            'oem_codes': [], 'supplier_sku': _text(raw.get('sap')),
            'source_url': f'{ORIGIN}/product.php?id={int(identifier)}',
            'status': 'PROVISIONAL', 'provider_fitment_text': None, 'warnings': [],
        }

    @staticmethod
    def _details(item: dict, metadata: dict | None) -> None:
        if not metadata:
            item['warnings'].append('PRODUCT_METADATA_MISSING')
            return
        brand = metadata.get('brand')
        brand = brand.get('name') if isinstance(brand, dict) else brand
        if (normalize_code(str(metadata.get('mpn', ''))) != normalize_code(item['mpn'])
                or str(metadata.get('sku', '')) != str(item['supplier_sku'] or '')
                or (brand and _identity(brand) != _identity(item['manufacturer']))):
            item['status'] = 'CONFLICTED'
            item['warnings'].append('PRODUCT_DETAIL_MISMATCH')
            return
        properties = metadata.get('additionalProperty', [])
        if not isinstance(properties, list):
            return
        for prop in properties[:100]:
            if not isinstance(prop, dict):
                continue
            name, value = _text(prop.get('name')), _text(prop.get('value'), 3000)
            if name and value and name.casefold() == 'oem':
                item['oem_codes'].extend(code.original for code in parse_codes(value)[:20])
            if name and value and name.casefold() == 'compatible vehicles':
                item['provider_fitment_text'] = value
        item['oem_codes'] = list(dict.fromkeys(item['oem_codes']))
