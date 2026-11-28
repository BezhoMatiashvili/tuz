"""Deterministic oil eligibility; model output cannot supply missing technical evidence."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import product as combinations
from time import monotonic
from typing import Any

import psycopg

from automotive.evidence import (
    VEHICLE_KEYS,
    applies_to,
    flatten_record,
    missing_oil_detail,
    verified_record,
)
from automotive.oil_match import normalize_grade

CLARIFICATION = (
    'ზეთის შესარჩევად საჭიროა ზუსტი ძრავის კოდი, მოდელის წელი და შესაბამისი '
    'მწარმოებლის მოთხოვნისა და კონკრეტული პროდუქტის დაშვების დადასტურება.'
)
OIL_DPF_REQUIRED = 'Verified oil requirements depend on DPF status.'
OIL_MARKET_REQUIRED = 'Verified oil requirements depend on the original sales market.'


def _approval_code(value: str) -> str:
    code = ' '.join(value.casefold().split())
    vw = re.fullmatch(r'vw\s*(\d{3})[. ]?(\d{2})', code)
    if vw:
        return f'vw {vw[1]} {vw[2]}'
    mb = re.fullmatch(r'mb(?:[- ](?:approval|freigabe))?\s*(\d{3})[. ](\d{1,2})', code)
    if mb:
        return f'mb {mb[1]}.{mb[2]}'
    return code


def _required(row: dict[str, Any]) -> set[str]:
    if 'required_approvals' in row:
        values = row['required_approvals']
        if not isinstance(values, list) or not values:
            raise ValueError('Malformed required approvals.')
    elif 'approval' in row:
        values = [row['approval']]
    elif 'specification' in row:
        values = [row['specification']]
    else:
        return set()
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('Malformed required approvals.')
    return {_approval_code(value) for value in values}


def positive_approval(row: dict[str, Any]) -> bool:
    approval = row.get('approval')
    return (isinstance(approval, str) and bool(approval.strip())
            and row.get('claim_type', 'approval') == 'approval'
            and ('approved' not in row or row['approved'] is True))


def _approval_options(row: dict[str, Any]) -> list[set[str]]:
    if 'approval_options' not in row:
        required = _required(row)
        return [required] if required else []
    options = row['approval_options']
    if (any(key in row for key in ('required_approvals', 'approval', 'specification'))
            or not isinstance(options, list) or not 1 <= len(options) <= 20):
        raise ValueError('Malformed required approvals.')
    return [_required({'required_approvals': option}) for option in options]


def _active(row: dict) -> bool:
    return (row.get('review_status') != 'rejected' and not row.get('superseded_by')
            and not row.get('revoked', False))


def _vehicle_requirements(rows: list[dict], vehicle: dict
                          ) -> tuple[list[dict], list[set[str]], set[str], str]:
    active = [row for row in rows if row.get('category') == 'engine_oil' and _active(row)]
    applicable = [row for row in active if applies_to(row, vehicle)]
    details = {missing_oil_detail(row, vehicle) for row in active if row not in applicable
               and verified_record(row) and _specific_configuration(row)}
    for field, message in (('dpf', OIL_DPF_REQUIRED), ('market', OIL_MARKET_REQUIRED)):
        if field in details:
            return applicable, [], set(), message
    try:
        assertions = [(row, _approval_options(row)) for row in applicable]
    except ValueError:
        return applicable, [], set(), 'Applicable oil approval requirements are malformed.'
    verified = [row for row, options in assertions
                if options and verified_record(row) and _specific_configuration(row)]
    if not verified:
        return applicable, [], set(), 'No applicable verified engine-oil specification exists.'
    try:
        signatures = {
            (frozenset(map(frozenset, options)), frozenset(_viscosities(row)))
            for row, options in assertions if options
        }
    except ValueError:
        return applicable, [], set(), 'Oil viscosity evidence has an unsupported format.'
    if len(signatures) > 1:
        return applicable, [], set(), 'CONFLICTED'
    return applicable, _approval_options(verified[0]), _viscosities(verified[0]), ''


def current_product_approval(row: dict, product: dict) -> bool:
    key = ('approval_content_fingerprint' if 'approval_content_fingerprint' in row
           else 'source_fingerprint')
    fingerprint = product.get(key)
    return (isinstance(fingerprint, str) and bool(fingerprint)
            and row.get(key) == fingerprint
            and str(row.get('product_id', row.get('source_product_id', '')))
            == str(product.get('source_product_id', '')))


def _matching_product(product: dict, approvals: list[dict], vehicle: dict,
                      options: list[set[str]], viscosities: set[str]) -> dict | None:
    applicable = [row for row in approvals if _active(row) and applies_to(row, vehicle)
                  and current_product_approval(row, product)]
    valid = [row for row in applicable if verified_record(row) and positive_approval(row)]
    catalog_grade = normalize_grade(product.get('product_name'))
    if viscosities and ((catalog_grade and catalog_grade not in viscosities)
                        or not _matches_viscosity(valid, viscosities)):
        return None
    denied = {_approval_code(row['approval']) for row in applicable
              if row.get('approved') is False and isinstance(row.get('approval'), str)}
    approved = {_approval_code(row['approval']) for row in valid}
    for required in options:
        if not required.issubset(approved) or required & denied:
            continue
        proof = [row for row in valid if _approval_code(row['approval']) in required]
        return {
            'product': product, 'status': 'VERIFIED',
            'required_approvals': sorted(required), 'evidence': proof,
            'required_viscosities': sorted(viscosities),
        }
    return None


class RecommendationService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def recommend_oil(self, vehicle: dict[str, Any], *, deadline: float | None = None
                      ) -> dict[str, Any]:
        result: dict[str, Any] = {
            'status': 'INSUFFICIENT_DATA', 'items': [], 'evidence': [], 'warnings': [],
            'clarification': CLARIFICATION, 'capacity_liters': None, 'filter_references': [],
        }
        if not all(vehicle.get(key) for key in ('make', 'model', 'model_year', 'engine_code')):
            result['warnings'].append('Exact vehicle and engine identification is incomplete.')
            return result
        rows = [flatten_record(row) for row in self.repository.vehicle_specifications(vehicle)]
        applicable, options, viscosities, problem = _vehicle_requirements(rows, vehicle)
        result['evidence'] = applicable
        if problem == 'CONFLICTED':
            result['status'] = 'CONFLICTED'
            result['warnings'].append(
                'Applicable sources disagree about required oil approvals or viscosity.',
            )
            return result
        if problem:
            result['warnings'].append(problem)
            return result
        reviewed = [row for row in applicable if verified_record(row)
                    and _specific_configuration(row)]
        capacities = {float(value) for row in reviewed
                      if isinstance(value := row.get('capacity_liters'), int | float)
                      and not isinstance(value, bool) and 0 < value <= 100}
        if len(capacities) == 1:
            result['capacity_liters'] = capacities.pop()
        result['requirements'] = {
            'approval_options': sorted(sorted(option) for option in options),
            'grades': sorted(viscosities),
        }
        try:
            products = self.repository.oil_approval_products()
        except (OSError, RuntimeError, psycopg.Error):
            result['warnings'].append('Inventory oil approvals could not be checked.')
            return result
        for product, approvals in products:
            if deadline is not None and monotonic() >= deadline:
                result['has_more'] = True
                result['warnings'].append('Oil approval lookup reached the time limit.')
                break
            item = _matching_product(product, [flatten_record(row) for row in approvals],
                                     vehicle, options, viscosities)
            if item is not None:
                result['items'].append(item)
                result['evidence'].extend(item['evidence'])
        if result['items']:
            result['status'] = 'VERIFIED'
            result['clarification'] = None
        else:
            result['warnings'].append('No inventory product has all required verified approvals.')
        return result

    def oil_vehicles(self, product_id: str, *, offset: int = 0, limit: int = 50) -> dict:
        """List evidenced configurations where the same forward match verifies this SKU."""
        items: list[dict] = []
        result = {'status': 'INSUFFICIENT_DATA', 'items': items, 'next_offset': None}
        products = self.repository.oil_approval_products(product_id)
        if not products:
            return result
        product, approvals = products[0]
        approvals = [flatten_record(row) for row in approvals]
        approved = {_approval_code(row['approval']) for row in approvals
                    if (verified_record(row) and positive_approval(row)
                        and current_product_approval(row, product))}
        if not approved:
            return result
        rows = [flatten_record(row) for row in self.repository.vehicle_specifications()]
        rows = [row for row in rows if row.get('category') == 'engine_oil']
        seen = set()
        matched = 0
        for row in rows:
            if not (verified_record(row) and _specific_configuration(row)):
                continue
            try:
                if not any(option.issubset(approved) for option in _approval_options(row)):
                    continue
            except ValueError:
                continue
            for vehicle in _configurations(row):
                key = tuple(sorted((name, str(value).casefold())
                                   for name, value in vehicle.items()))
                if key in seen:
                    continue
                seen.add(key)
                evidence, options, viscosities, problem = _vehicle_requirements(rows, vehicle)
                if problem:
                    continue
                item = _matching_product(product, approvals, vehicle, options, viscosities)
                if item is None:
                    continue
                result['status'] = 'VERIFIED'
                if matched >= offset + limit:
                    result['next_offset'] = offset + limit
                    return result
                if matched >= offset:
                    items.append({**item, 'vehicle': vehicle,
                                  'evidence': [*evidence, *item['evidence']]})
                matched += 1
        return result


def _configurations(row: dict) -> Iterator[dict]:
    scope = row['applicability']
    restrictions = row.get('restrictions', {})
    dimensions = dict(scope)
    if not isinstance(restrictions, dict):
        return
    for key, value in restrictions.items():
        dimensions.setdefault(key, value)
    try:
        start = max(1980, int(scope.get('model_year_from', 1980)),
                    int(restrictions.get('model_year_from', 1980)))
        end = min(2100, int(scope.get('model_year_to', 2100)),
                  int(restrictions.get('model_year_to', 2100)))
        years = dimensions.get('model_year', range(start, end + 1))
        if not isinstance(years, list | range):
            years = [years]
        dimensions['model_year'] = sorted({int(y) for y in years if start <= int(y) <= end})
    except (TypeError, ValueError, OverflowError):
        return
    options = {key: value if isinstance(value, list) else [value]
               for key, value in dimensions.items() if key in VEHICLE_KEYS}
    if not all(all(isinstance(value, str | int | float) and value != ''
                   for value in values) for values in options.values()):
        return
    for values in combinations(*options.values()):
        vehicle = dict(zip(options, values, strict=True))
        if applies_to(row, vehicle):
            yield vehicle


def _specific_configuration(row: dict[str, Any]) -> bool:
    scope = row.get('applicability', {})
    if not isinstance(scope, dict):
        return False
    return (all(scope.get(key) for key in ('make', 'model', 'engine_code'))
            and (bool(scope.get('model_year'))
                 or bool(scope.get('model_year_from') and scope.get('model_year_to'))))


def _viscosities(row: dict[str, Any]) -> set[str]:
    """Accept explicit SAE multigrade strings or alternatives; never parse prose."""
    signatures = []
    for key in ('viscosity', 'viscosities'):
        if key not in row:
            continue
        values = row[key] if isinstance(row[key], list) else [row[key]]
        if not values or len(values) > 20:
            raise ValueError('Unsupported viscosity evidence.')
        grades = set()
        for value in values:
            match = re.fullmatch(r'(?:SAE\s*)?(\d{1,2})\s*W\s*-?\s*(\d{1,2})',
                                 value.strip(), re.IGNORECASE) if isinstance(value, str) else None
            if not match:
                raise ValueError('Unsupported viscosity evidence.')
            grades.add(f'{int(match[1])}W-{int(match[2])}')
        signatures.append(grades)
    if signatures and any(grades != signatures[0] for grades in signatures[1:]):
        raise ValueError('Conflicting viscosity evidence fields.')
    return signatures[0] if signatures else set()


def _matches_viscosity(records: list[dict[str, Any]], required: set[str]) -> bool:
    try:
        assertions = [_viscosities(row) for row in records
                      if 'viscosity' in row or 'viscosities' in row]
    except ValueError:
        return False
    if not assertions:
        return False
    # Preserve every asserted possible grade rather than silently narrowing disagreements.
    grades = set.union(*assertions)
    return bool(grades) and grades.issubset(required)
