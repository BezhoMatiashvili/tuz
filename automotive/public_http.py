"""Bounded reads of fixed public HTTPS endpoints. No redirects or stored cookies."""

from __future__ import annotations

import re
from collections import deque
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import requests

VIN_IN_TEXT = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b', re.IGNORECASE)
VISCOSITY = re.compile(r'\b\d{1,2}W-\d{2}\b', re.IGNORECASE)
SUPPLIER_WARNING = (
    'წყაროს რეკომენდაციაა. ეს ჩვენი მარაგი, ფასი ან დამოუკიდებელი დაშვება არ არის.'
)


class PublicSourceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__('External source unavailable')
        self.code = code


def redact(value: Any, maximum: int = 300) -> str | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    text = VIN_IN_TEXT.sub('[VIN]', str(value)).strip()
    return text[:maximum] or None


def identity(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def fluid_report(provider: str, status: str, *, label: str = '', items: list | None = None,
                 clarification: str | None = None, warnings: list[str] | None = None,
                 engine_code: str | None = None) -> dict:
    return {
        'provider': provider, 'status': status, 'vehicle_label': redact(label, 180) or '',
        'engine_code': redact(engine_code, 40),
        'items': items or [], 'clarification': redact(clarification, 500),
        'warnings': [text for item in (warnings or []) if (text := redact(item, 240))],
    }


def fluid_product(name: str, code: Any, *, component: str, capacity: Any = None,
                  interval: Any = None, kind: str) -> dict | None:
    clean_name = redact(name, 180)
    if not clean_name:
        return None
    viscosity = VISCOSITY.search(clean_name)
    return {
        'kind': kind, 'component': component, 'product_name': clean_name,
        'product_code': redact(code, 40),
        'viscosity': viscosity.group(0).upper() if viscosity else None,
        'interval': redact(interval, 80), 'capacity': redact(capacity, 40),
    }


class PublicHttp:
    """One reservation covers a whole lookup. Callers cannot choose the host."""

    def __init__(self, *prefixes: str, per_minute: int = 10) -> None:
        if not prefixes or any(not item.startswith('https://') for item in prefixes):
            raise ValueError('Public sources require fixed HTTPS prefixes.')
        self.prefixes = prefixes
        self.per_minute = per_minute
        self._requests: deque[float] = deque()
        self._lock = Lock()

    def reserve(self) -> None:
        now = monotonic()
        with self._lock:
            while self._requests and self._requests[0] < now - 60:
                self._requests.popleft()
            if len(self._requests) >= self.per_minute:
                raise PublicSourceError('RATE_LIMITED')
            self._requests.append(now)

    def read(self, method: str, url: str, *, params: dict | None = None,
             data: dict | None = None, headers: dict | None = None,
             deadline: float | None = None) -> tuple[int, bytes]:
        if method not in {'GET', 'POST'} or not url.startswith(self.prefixes):
            raise PublicSourceError('INVALID_RESPONSE')
        parts = urlsplit(url)
        if parts.username or parts.password or parts.hostname is None:
            raise PublicSourceError('INVALID_RESPONSE')
        response = None
        try:
            remaining = deadline - monotonic() if deadline is not None else 20.0
            if remaining <= 0:
                raise PublicSourceError('TIME_LIMIT')
            timeout = (min(3.05, remaining), min(15, remaining))
            request_headers = {
                'Accept': 'application/json, text/html;q=0.9',
                'User-Agent': 'automotive-intelligence/0.1 (read-only)',
            }
            if headers:
                request_headers.update(headers)
            with requests.Session() as session:
                response = session.request(
                    method, url, params=params, data=data, headers=request_headers,
                    timeout=timeout, allow_redirects=False, stream=True,
                )
                if response.status_code == 429:
                    raise PublicSourceError('RATE_LIMITED')
                if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
                    raise PublicSourceError('ACCESS_REQUIRED')
                body = bytearray()
                for chunk in response.iter_content(8192):
                    if deadline is not None and monotonic() >= deadline:
                        raise PublicSourceError('TIME_LIMIT')
                    if len(body) + len(chunk) > 2_000_000:
                        raise PublicSourceError('INVALID_RESPONSE')
                    body.extend(chunk)
                return response.status_code, bytes(body)
        except PublicSourceError:
            raise
        except requests.RequestException:
            raise PublicSourceError('PROVIDER_UNAVAILABLE') from None
        finally:
            if response is not None:
                response.close()
