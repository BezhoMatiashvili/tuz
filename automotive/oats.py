"""Shared OATS vehicle search used by the public Shell and Pakelo choosers."""

from __future__ import annotations

import json
import re
from time import monotonic
from typing import Any
from urllib.parse import urljoin

from automotive.engine_match import (
    engine_code,
    identical,
    narrow,
    trim_search_text,
)
from automotive.public_http import (
    SUPPLIER_WARNING,
    PublicHttp,
    PublicSourceError,
    fluid_product,
    fluid_report,
    identity,
    redact,
)
from automotive.vin import validate_vin

EQUIPMENT_ID = re.compile(r'^[A-Za-z0-9_-]{4,64}$')
TOKEN = re.compile(r'<find-your-oil\b[^>]*\btoken="([A-Za-z0-9_-]{8,64})"')


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None


class OatsCatalog:
    """Make, model and year select equipment. A VIN is optional, validated and never sent."""

    def __init__(self, provider: str, base_url: str, language: str, *,
                 catalog_page: str | None = None) -> None:
        prefixes = [base_url]
        if catalog_page:
            origin = catalog_page[:catalog_page.find('/', len('https://'))]
            prefixes.append(origin)
        self.provider = provider
        self.base_url = base_url.rstrip('/')
        self.language = language
        self.catalog_page = catalog_page
        self.http = PublicHttp(*prefixes)

    def lookup(self, vin: str | None, vehicle: dict, *, deadline: float | None = None) -> dict:
        if vin is not None:  # Optional: the chooser is queried by make, model and year only.
            validate_vin(vin)
        make, model, year = vehicle.get('make'), vehicle.get('model'), vehicle.get('model_year')
        if not isinstance(make, str) or not isinstance(model, str) or isinstance(year, bool):
            return fluid_report(self.provider, 'INSUFFICIENT_DATA',
                                clarification='ზეთის წყაროს მწარმოებელი, მოდელი და წელი სჭირდება.')
        if not str(year).isdigit():
            return fluid_report(self.provider, 'INSUFFICIENT_DATA',
                                clarification='ზეთის წყაროს მწარმოებელი, მოდელი და წელი სჭირდება.')
        if deadline is not None and monotonic() >= deadline:
            raise PublicSourceError('TIME_LIMIT')
        self.http.reserve()
        token = self._token(deadline)
        found = self._search(make, model, int(year), token, deadline,
                             trim_search_text(vehicle.get('trim')))
        matched = self._match(found, make, model, int(year), vehicle)
        label = f'{make} {model} {year}'
        if not matched:
            return fluid_report(self.provider, 'EMPTY', label=label,
                                warnings=['შერჩეულ წელს ცალსახა მანქანა ვერ მოიძებნა.'])
        # Several variants are only read when the trim already identified the engine
        # family (2WD and 4-matic); otherwise the choice stays ambiguous.
        decided = len(matched) > 1 and all(self._trim_decided(i, vehicle) for i in matched)
        codes = {engine_code(i.get('display_name_long')) for i in matched}
        if len(matched) > 1 and not decided:
            return self._conflict(label, matched)
        if len(matched) > 3 and not (len(codes) == 1 and None not in codes
                                     and len(matched) <= 8):
            return self._conflict(label, matched)
        # Variants of one engine share a recommendation; verify with a small sample.
        sample = list({id(i): i for i in (matched[0], matched[len(matched) // 2],
                                          matched[-1])}.values()) if len(matched) > 3 else matched
        reports = [self._equipment(item, token, deadline) for item in sample]
        if len(reports) == 1 or all(r.get('status') == 'EMPTY' for r in reports):
            return self._with_engine(reports[0], matched)
        if identical(reports):
            # Variants such as 2WD and 4-matic share one recommendation: it is unambiguous.
            return self._with_engine(reports[0], matched)
        return self._conflict(label, matched)

    @staticmethod
    def _with_engine(report: dict, matched: list[dict]) -> dict:
        codes = {engine_code(item.get('display_name_long')) for item in matched} - {None}
        report['engine_code'] = redact(next(iter(codes)), 40) if len(codes) == 1 else None
        return report

    def _conflict(self, label: str, matched: list[dict]) -> dict:
        names = [redact(item.get('display_name_long') or item.get('model'), 80)
                 for item in matched[:8]]
        return fluid_report(
            self.provider, 'CONFLICTED', label=label,
            clarification='რამდენიმე ძრავა ემთხვევა. ზეთი არ შერჩეულა: ' + '; '.join(
                name for name in names if name),
            warnings=['ძრავა ცალსახა არ არის, ამიტომ პროდუქტი არ დაბრუნდა.'],
        )

    def _token(self, deadline: float | None) -> str | None:
        if not self.catalog_page:
            return None
        status, body = self.http.read('GET', self.catalog_page, deadline=deadline)
        if status != 200:
            raise PublicSourceError('ACCESS_REQUIRED')
        match = TOKEN.search(body.decode('utf-8', 'replace'))
        if not match:
            raise PublicSourceError('ACCESS_REQUIRED')
        return match.group(1)

    def _search(self, make: str, model: str, year: int, token: str | None,
                deadline: float | None, trim: str = '') -> list[dict]:
        params: dict[str, Any] = {
            'q': ' '.join(part for part in (make, model, trim, str(year)) if part)[:80],
            'page': 1, 'page_size': 20,
            'language': self.language,
        }
        if token:
            params['token'] = token
        status, body = self.http.read('GET', self.base_url + '/search', params=params,
                                       deadline=deadline)
        if status != 200:
            raise PublicSourceError('ACCESS_REQUIRED' if token else 'PROVIDER_UNAVAILABLE')
        try:
            equipment = json.loads(body)['equipment_list']['equipment']
        except (json.JSONDecodeError, KeyError, TypeError):
            raise PublicSourceError('INVALID_RESPONSE') from None
        if not isinstance(equipment, list):
            raise PublicSourceError('INVALID_RESPONSE')
        return [item for item in equipment[:20] if isinstance(item, dict)]

    def _match(self, equipment: list[dict], make: str, model: str, year: int,
               vehicle: dict) -> list[dict]:
        matched = []
        for item in equipment:
            if identity(item.get('manufacturer')) != identity(make):
                continue
            if identity(model) not in identity(item.get('model')):
                continue
            start, end = _number(item.get('yearfrom')), _number(item.get('yearto'))
            if start is None or end is None or not start <= year <= end:
                continue
            matched.append(item)
        if len(matched) < 2:
            return matched
        # Trim and fuel decide first; a decoder's displacement can disagree with the
        # supplier's (3.0 vs 3.5 L for one engine), so it is only a last tie-breaker.
        matched = narrow(matched, lambda item: item.get('display_name_long') or item.get('model'),
                         vehicle)
        wanted = _number(vehicle.get('displacement_l'))
        if wanted is None or len(matched) < 2 or any(
                self._trim_decided(item, vehicle) for item in matched):
            return matched
        narrowed = []
        for item in matched:
            values = item.get('displacement') if isinstance(item.get('displacement'), list) else []
            if any(_number(value) == wanted for value in values):
                narrowed.append(item)
        return narrowed or matched

    @staticmethod
    def _trim_decided(item: dict, vehicle: dict) -> bool:
        from automotive.engine_match import trim_matches

        return trim_matches(item.get('display_name_long') or item.get('model'),
                            vehicle.get('trim'))

    def _equipment(self, summary: dict, token: str | None, deadline: float | None) -> dict:
        identifier = str(summary.get('@id') or '')
        if not EQUIPMENT_ID.fullmatch(identifier):
            raise PublicSourceError('INVALID_RESPONSE')
        params = {'language': self.language}
        if token:
            params['token'] = token
        status, body = self.http.read(
            'GET', urljoin(self.base_url + '/', f'equipment/{identifier}'),
            params=params, deadline=deadline,
        )
        if status != 200:
            raise PublicSourceError('PROVIDER_UNAVAILABLE')
        try:
            equipment = json.loads(body)['equipment']
        except (json.JSONDecodeError, KeyError, TypeError):
            raise PublicSourceError('INVALID_RESPONSE') from None
        if not isinstance(equipment, dict):
            raise PublicSourceError('INVALID_RESPONSE')
        label = str(equipment.get('display_name_long') or summary.get('model') or '')
        items = []
        applications = equipment.get('application')
        applications = applications if isinstance(applications, list) else []
        for application in applications:
            if not isinstance(application, dict) or identity(application.get('name')) != 'engine':
                continue
            capacity = application.get('display_capacity')
            products = application.get('product')
            products = products if isinstance(products, list) else []
            for product in products[:8]:
                if not isinstance(product, dict):
                    continue
                row = fluid_product(
                    str(product.get('name') or ''), product.get('external_id'),
                    component='Engine', capacity=capacity, kind='oil',
                )
                if row:
                    items.append(row)
        if not items:
            return fluid_report(self.provider, 'EMPTY', label=label,
                                warnings=['არჩეულ მანქანაზე ძრავის ზეთის ჩანაწერი არ იყო.'])
        return fluid_report(self.provider, 'PROVISIONAL', label=label, items=items[:8],
                            warnings=[SUPPLIER_WARNING])
