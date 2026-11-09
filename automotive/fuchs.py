"""Public Fuchs Oil Chooser. The vehicle tree is queried; the VIN is not sent."""

from __future__ import annotations

import json
from time import monotonic
from typing import Any
from urllib.parse import urlencode

from automotive.engine_match import engine_matches, identical, model_matches, trim_matches
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

ORIGIN = 'https://fuchs.webbhuset.com'
HEADERS = {
    'Accept': 'application/json', 'x-oly-dataset': 'fuchs_eu', 'x-oly-subscription': '',
    'Origin': 'https://www.fuchs.com', 'Referer': 'https://www.fuchs.com/de/en/oil-chooser/',
}


class FuchsClient:
    def __init__(self) -> None:
        self.http = PublicHttp(ORIGIN)

    def lookup(self, vin: str | None, vehicle: dict, *, deadline: float | None = None) -> dict:
        if vin is not None:  # Optional: the chooser is queried by make, model and year only.
            validate_vin(vin)
        make, model, year = vehicle.get('make'), vehicle.get('model'), vehicle.get('model_year')
        if (not isinstance(make, str) or not isinstance(model, str) or isinstance(year, bool)
                or not str(year).isdigit()):
            return fluid_report('Fuchs', 'INSUFFICIENT_DATA',
                                clarification='Fuchs-ს მწარმოებელი, მოდელი და წელი სჭირდება.')
        if deadline is not None and monotonic() >= deadline:
            raise PublicSourceError('TIME_LIMIT')
        year = int(year)
        self.http.reserve()
        categories = self._rows('/categories', {'language': 'en'}, deadline)
        cars = next((row for row in categories if row.get('categoryName') == 'Cars'), None)
        if not isinstance(cars, dict) or isinstance(cars.get('id'), bool):
            raise PublicSourceError('INVALID_RESPONSE')
        if not isinstance(cars.get('id'), int):
            raise PublicSourceError('INVALID_RESPONSE')
        makes = [row for row in self._rows('/makes', {
            'language': 'en', 'categoryId': cars['id']}, deadline) if _make_matches(row, make)]
        label = f'{make} {model} {year}'
        if not makes:
            return fluid_report('Fuchs', 'EMPTY', label=label)
        if len(makes) > 4:
            return _ambiguous(label, [row.get('makeName') for row in makes])
        def decided(row: dict) -> bool:
            return (trim_matches(row.get('typeName'), vehicle.get('trim'))
                    or engine_matches(row.get('typeName'), vehicle.get('engine_code')))

        stated = bool(vehicle.get('trim') or vehicle.get('engine_code'))
        types: list[dict] = []
        for make_row in makes:
            models = self._rows('/models', {'language': 'en', 'makeId': make_row['id'],
                                            'yearFilter': year}, deadline)
            # Only the generations built that year: each one costs a request.
            models = [row for row in models if _model_matches(row, model)
                      and _model_covers_year(row, year)]
            for model_row in models[:8]:
                rows = self._rows('/types', {
                    'language': 'en', 'modelId': model_row['id'], 'yearFilter': year,
                }, deadline)
                types.extend(row for row in rows if _covers_year(row, year))
                # A series lists many types; only the ones the trim names can be ambiguous.
                candidates = [row for row in types if decided(row)] if stated else types
                if len(candidates) > 8 or len(types) > 60:
                    return _ambiguous(label, [row.get('typeName') for row in candidates])
        if not types:
            return fluid_report('Fuchs', 'EMPTY', label=label)
        by_trim = [row for row in types if decided(row)]
        types = by_trim or types
        if len(types) > 3 or (len(types) > 1 and not by_trim):
            return _ambiguous(label, [row.get('typeName') for row in types])
        reports = [self._recommendation(row, deadline) for row in types]
        if len(reports) == 1 or identical(reports):
            return reports[0]
        return _ambiguous(label, [row.get('typeName') for row in types])

    def _rows(self, path: str, params: dict, deadline: float | None) -> list[dict]:
        status, body = self.http.read(
            'GET', ORIGIN + '/_api' + path + '?' + urlencode(params),
            headers=HEADERS, deadline=deadline,
        )
        if status != 200:
            raise PublicSourceError('PROVIDER_UNAVAILABLE')
        try:
            rows = json.loads(body)['resultData']
        except (json.JSONDecodeError, KeyError, TypeError):
            raise PublicSourceError('INVALID_RESPONSE') from None
        if not isinstance(rows, list):
            raise PublicSourceError('INVALID_RESPONSE')
        return [row for row in rows if isinstance(row, dict)]

    def _recommendation(self, vehicle_type: dict, deadline: float | None) -> dict:
        identifier = vehicle_type.get('id')
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise PublicSourceError('INVALID_RESPONSE')
        status, body = self.http.read(
            'GET', f'{ORIGIN}/_api/recommendations/{identifier}?language=en',
            headers=HEADERS, deadline=deadline,
        )
        if status != 200:
            raise PublicSourceError('PROVIDER_UNAVAILABLE')
        try:
            payload = json.loads(body)['resultData']
            components = payload['components']
        except (json.JSONDecodeError, KeyError, TypeError):
            raise PublicSourceError('INVALID_RESPONSE') from None
        engine = next((row for row in components if isinstance(row, dict)
                       and row.get('componentName') == 'Engine'), None)
        products = engine.get('productRecommendations') if isinstance(engine, dict) else None
        items = []
        for product in products[:8] if isinstance(products, list) else []:
            if not isinstance(product, dict):
                continue
            interval = None
            intervals = product.get('intervals')
            if isinstance(intervals, list) and intervals and isinstance(intervals[0], dict):
                interval = intervals[0].get('intervalName')
            row = fluid_product(
                str(product.get('productName') or ''), product.get('productCode'),
                component='Engine', interval=interval, kind='oil',
            )
            if row:
                items.append(row)
        label = ' '.join(redact(payload.get(key), 40) or ''
                         for key in ('makeName', 'modelName', 'typeName')).strip()
        if not items:
            return fluid_report('Fuchs', 'EMPTY', label=label)
        return fluid_report(
            'Fuchs', 'PROVISIONAL', label=label, items=items, warnings=[SUPPLIER_WARNING],
        )


def _make_matches(row: dict, make: str) -> bool:
    name = str(row.get('makeName') or '').split('(', 1)[0]
    return bool(identity(name)) and identity(name) == identity(make)


def _model_matches(row: dict, model: str) -> bool:
    return model_matches(row.get('modelName'), model)


def _model_covers_year(row: dict, year: int) -> bool:
    """A model row without a year range is kept; its types are checked anyway."""
    start = row.get('typeYearStart', row.get('yearStart'))
    end = row.get('typeYearEnd', row.get('yearEnd'))
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
        return True
    return start <= year <= end


def _covers_year(row: dict, year: int) -> bool:
    start, end = row.get('yearStart'), row.get('yearEnd')
    return all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)
               ) and start <= year <= end


def _ambiguous(label: str, names: list[Any]) -> dict:
    shown = [redact(name, 80) for name in names[:8]]
    return fluid_report(
        'Fuchs', 'CONFLICTED', label=label,
        clarification='რამდენიმე ძრავა ემთხვევა. ზეთი არ შერჩეულა: ' + '; '.join(
            name for name in shown if name),
        warnings=['ძრავა ცალსახა არ არის, ამიტომ პროდუქტი არ დაბრუნდა.'],
    )
