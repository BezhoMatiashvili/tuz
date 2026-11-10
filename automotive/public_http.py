"""Bounded reads of fixed public HTTPS endpoints. No redirects or stored cookies."""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, time
from typing import Any
from urllib.parse import urlsplit

import requests

VIN_IN_TEXT = re.compile(r'(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])', re.IGNORECASE)
VISCOSITY = re.compile(r'\b\d{1,2}W-\d{2}\b', re.IGNORECASE)
SUPPLIER_WARNING = (
    'წყაროს რეკომენდაციაა. ეს ჩვენი მარაგი, ფასი ან დამოუკიდებელი დაშვება არ არის.'
)


class PublicSourceError(RuntimeError):
    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__('External source unavailable')
        self.code = code
        self.retry_after = (min(86400.0, max(1.0, retry_after))
                            if retry_after is not None and math.isfinite(retry_after) else None)


def retry_delay(headers: Mapping, *, now: float | None = None) -> float:
    """Read Retry-After and common quota reset headers without retaining headers.

    RapidAPI's requests-reset is a duration; generic reset headers may be Unix
    timestamps. Durations are bounded to a day to handle malformed provider data.
    """
    current = time() if now is None else now
    clean = {str(key).casefold(): str(value) for key, value in headers.items()}
    delays = []
    for name in ('retry-after', 'x-ratelimit-requests-reset', 'x-ratelimit-reset',
                 'ratelimit-reset'):
        value = clean.get(name)
        if not value:
            continue
        try:
            number = float(value)
            if not math.isfinite(number):
                continue
            if name in {'x-ratelimit-reset', 'ratelimit-reset'} and number > 1_000_000_000:
                number -= current
        except ValueError:
            if name != 'retry-after':
                continue
            try:
                number = parsedate_to_datetime(value).timestamp() - current
            except (ValueError, TypeError, OverflowError):
                continue
        delays.append(number)
    return min(86400.0, max(1.0, max(delays))) if delays else 60.0


def redact(value: Any, maximum: int = 300) -> str | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    text = VIN_IN_TEXT.sub('[VIN]', str(value)).strip()
    return text[:maximum] or None


def identity(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def fluid_report(provider: str, status: str, *, label: str = '', items: list | None = None,
                 clarification: str | None = None, warnings: list[str] | None = None,
                 engine_code: str | None = None, candidates: list | None = None) -> dict:
    return {
        'provider': provider, 'status': status, 'vehicle_label': redact(label, 180) or '',
        'engine_code': redact(engine_code, 40),
        'items': items or [], 'clarification': redact(clarification, 500),
        'warnings': [text for item in (warnings or []) if (text := redact(item, 240))],
        # The variants a customer could choose between when the engine is ambiguous.
        'candidates': [text for item in (candidates or [])[:12] if (text := redact(item, 120))],
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
        self._retry_until = 0.0

    def reserve(self) -> None:
        now = monotonic()
        with self._lock:
            if self._retry_until > now:
                raise PublicSourceError('RATE_LIMITED', retry_after=self._retry_until - now)
            while self._requests and self._requests[0] < now - 60:
                self._requests.popleft()
            if len(self._requests) >= self.per_minute:
                raise PublicSourceError('RATE_LIMITED',
                                        retry_after=60 - (now - self._requests[0]))
            self._requests.append(now)

    def read(self, method: str, url: str, *, params: dict | None = None,
             data: dict | None = None, headers: dict | None = None,
             deadline: float | None = None) -> tuple[int, bytes]:
        if method not in {'GET', 'POST'} or not url.startswith(self.prefixes):
            raise PublicSourceError('INVALID_RESPONSE')
        parts = urlsplit(url)
        if parts.username or parts.password or parts.hostname is None:
            raise PublicSourceError('INVALID_RESPONSE')
        with self._lock:
            remaining_retry = self._retry_until - monotonic()
        if remaining_retry > 0:
            raise PublicSourceError('RATE_LIMITED', retry_after=remaining_retry)
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
                    delay = retry_delay(response.headers)
                    with self._lock:
                        self._retry_until = monotonic() + delay
                    raise PublicSourceError('RATE_LIMITED', retry_after=delay)
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
