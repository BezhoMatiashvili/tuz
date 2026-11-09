"""Deterministic oil eligibility; model output cannot supply missing technical evidence."""

from __future__ import annotations

import re
from time import monotonic
from typing import Any

from automotive.evidence import applies_to, flatten_record, verified_record

CLARIFICATION = (
    'ზეთის შესარჩევად საჭიროა ზუსტი ძრავის კოდი, მოდელის წელი და შესაბამისი '
    'მწარმოებლის მოთხოვნისა და კონკრეტული პროდუქტის დაშვების დადასტურება.'
)


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
    return {value.strip().casefold() for value in values}


def _positive_approval(row: dict[str, Any]) -> bool:
    approval = row.get('approval')
    return (isinstance(approval, str) and bool(approval.strip())
            and ('approved' not in row or row['approved'] is True))


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
        applicable = [row for row in rows if row.get('category') == 'engine_oil'
                      and applies_to(row, vehicle) and not row.get('superseded_by')
                      and row.get('review_status') != 'rejected' and not row.get('revoked', False)]
        result['evidence'] = rows
        try:
            for row in applicable:
                _required(row)
        except ValueError:
            result['warnings'].append('Applicable oil approval requirements are malformed.')
            return result
        verified = [row for row in applicable
                    if verified_record(row) and _required(row) and _specific_configuration(row)]
        if not verified:
            result['warnings'].append('No applicable verified engine-oil specification exists.')
            return result
        # All surviving sources must agree. A pending contrary assertion blocks eligibility too.
        try:
            requirement_sets = {_signature(row) for row in applicable if _required(row)}
            viscosities = _viscosities(verified[0])
        except ValueError:
            result['warnings'].append('Oil viscosity evidence has an unsupported format.')
            return result
        if len(requirement_sets) > 1:
            result['status'] = 'CONFLICTED'
            result['warnings'].append(
                'Applicable sources disagree about required oil approvals or viscosity.',
            )
            return result
        requirements = _required(verified[0])
        for product in self.repository.inventory_products():
            if deadline is not None and monotonic() >= deadline:
                result['has_more'] = True
                result['warnings'].append('Oil approval lookup reached the time limit.')
                break
            product_id = product.get('source_product_id')
            if not product_id:
                continue
            fingerprint = product.get('source_fingerprint')
            if not isinstance(fingerprint, str) or not fingerprint:
                result['warnings'].append(
                    f'Current inventory fingerprint is unavailable for product {product_id}.',
                )
                continue
            approvals = [flatten_record(row)
                         for row in self.repository.product_approvals(str(product_id))]
            valid = [row for row in approvals
                     if verified_record(row) and _positive_approval(row)
                     and applies_to(row, vehicle)
                     and row.get('source_fingerprint') == fingerprint
                     and str(row.get('product_id', row.get('source_product_id', '')))
                     == str(product_id)]
            approved = {row['approval'].strip().casefold() for row in valid}
            if not requirements.issubset(approved):
                continue
            if viscosities and not _matches_viscosity(valid, viscosities):
                result['warnings'].append(
                    f'Verified viscosity is missing or unsuitable for product {product_id}.',
                )
                continue
            # Explicit contrary product approval evidence blocks a recommendation.
            if any(row.get('approved') is False and applies_to(row, vehicle)
                   and row.get('review_status') != 'rejected'
                   and not row.get('superseded_by') for row in approvals):
                result['warnings'].append(
                    f'Conflicting approval evidence for product {product_id}.',
                )
                continue
            result['items'].append({
                'product': product, 'status': 'VERIFIED',
                'required_approvals': sorted(requirements), 'evidence': valid,
                'required_viscosities': sorted(viscosities),
            })
            result['evidence'].extend(valid)
        if result['items']:
            result['status'] = 'VERIFIED'
            result['clarification'] = None
        else:
            result['warnings'].append('No inventory product has all required verified approvals.')
        return result


def _signature(row: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(sorted(_required(row))), tuple(sorted(_viscosities(row)))


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
