"""Turn a vehicle's resolved oil requirement into catalog matches.

The choosers say what the engine needs (SAE grade, product line, capacity, engine code).
Matches are found in our catalog by that requirement, never by the car's name:

* Tier 1: same brand, product line and grade as a chooser recommends (OIL_GRADE_BRAND).
* Tier 2: same grade from any brand (OIL_GRADE); approval stays unconfirmed.
* Engine family: oil filter elements and sump plugs that name the same engine family
  (ENGINE_FAMILY), for example ``M276`` but never ``M272/273``.

Chooser data is supplier-asserted, so every row stays UNKNOWN fitment.
"""

from __future__ import annotations

import re
from typing import Any

from automotive.engine_match import engine_family
from automotive.rules import approvals_stated
from automotive.stock import RANKS, classify

BRANDS = {'shell': 'SHELL', 'pakelo': 'PAKELO', 'fuchs': 'FUCHS'}
GRADE = re.compile(r'\b(\d{1,2})W[- ]?(\d{2})\b', re.IGNORECASE)
PACK = re.compile(
    r'(?<![\w.,])(?:\d+\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*'
    r'(?:lit(?:res?|ers?)|ltr|lt|l|ლიტრ[ა-ჰ]*|ლ)(?!\w)', re.IGNORECASE,
)
DIESEL = re.compile(r'(^|\W)diesel(\W|$)|დიზელ', re.IGNORECASE)
PETROL = re.compile(r'(^|\W)(petrol|gasoline|benzin)(\W|$)|ბენზინ', re.IGNORECASE)
TIER2_LIMIT = 15
NOTES = {
    'OIL_GRADE_BRAND': (
        'წყაროს რეკომენდებული ზეთის ხაზია; დაშვება (approval) დაუდასტურებელია.',
        'The product line the source recommends; approval unconfirmed.',
    ),
    'OIL_GRADE': (
        'სიბლანტე ემთხვევა წყაროს მოთხოვნას; დაშვება (approval) დაუდასტურებელია.',
        'Grade matches the source requirement; approval unconfirmed.',
    ),
    'NOT_STATED': (
        'დაშვება (approval) პროდუქტის სახელში მითითებული არ არის; გადაამოწმეთ მოთხოვნა.',
        'Approval is not stated in the product name; confirm it meets the requirement.',
    ),
    'ENGINE_FAMILY': (
        'ტექსტი ამ ძრავის ოჯახს ასახელებს (წყაროს მიხედვით); შეამოწმეთ კოდი.',
        'Text names this engine family (per the source); verify the code.',
    ),
}


def normalize_grade(value: Any) -> str | None:
    found = GRADE.search(str(value or ''))
    return f'{int(found.group(1))}W-{found.group(2)}' if found else None


def _tokens(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', text.casefold())


def product_line(name: str) -> list[str]:
    """`Helix Ultra 0W-40 (SP)` -> ['helix', 'ultra']: the name without grade and notes."""
    cleaned = re.sub(r'\([^)]*\)', ' ', GRADE.sub(' ', name or ''))
    return [t for t in _tokens(cleaned) if t not in {'sae', 'oil', 'engine'}]


def pack_litres(name: str) -> float | None:
    volumes = {float(value.replace(',', '.')) for value in PACK.findall(name or '')}
    if len(volumes) != 1:
        return None
    litres = volumes.pop()
    return litres if 0 < litres < float('inf') else None


def requirements(fluids: list[dict]) -> dict:
    """Merge the choosers' oil recommendations for this vehicle."""
    recommended: list[dict] = []
    capacity: float | None = None
    codes: list[str] = []
    sources: list[str] = []
    for report in fluids or []:
        if report.get('status') != 'PROVISIONAL':
            continue
        provider = str(report.get('provider') or '')
        if report.get('engine_code') and report['engine_code'] not in codes:
            codes.append(report['engine_code'])
        for item in report.get('items') or []:
            grade = normalize_grade(item.get('viscosity') or item.get('product_name'))
            if item.get('kind') != 'oil' or not grade:
                continue
            recommended.append({'provider': provider, 'grade': grade,
                                'name': item.get('product_name') or '',
                                'line': product_line(item.get('product_name') or '')})
            if provider and provider not in sources:
                sources.append(provider)
            try:
                value = float(str(item.get('capacity')).replace(',', '.'))
                capacity = max(capacity or 0.0, value)
            except ValueError:
                pass
    return {
        'recommended': recommended,
        'grades': list(dict.fromkeys(item['grade'] for item in recommended)),
        'capacity_l': capacity,
        'engine_code': codes[0] if len(codes) == 1 else None,
        'sources': sources,
    }


def summary(need: dict) -> dict:
    """The public, display-safe requirement shown next to the offers."""
    return {key: need[key] for key in ('grades', 'capacity_l', 'engine_code', 'sources')
            if need.get(key)}


def _fuel_excludes(name: str, fuel: Any) -> bool:
    text = str(fuel or '').casefold()
    if text in {'gasoline', 'petrol'}:
        return bool(DIESEL.search(name))
    if text == 'diesel':
        return bool(PETROL.search(name))
    return False


def _row(product: dict, match_type: str, via: str, language: str) -> dict:
    note = NOTES[match_type][0 if language == 'ka' else 1]
    row = {'product': product, 'status': 'UNKNOWN', 'compatible': None,
           'match_type': match_type, 'evidence': [], 'warnings': [note], 'provenance': [via]}
    litres = pack_litres(product.get('product_name', ''))
    if litres:
        row['pack_litres'] = litres
    return row


def oil_rows(repository: Any, need: dict, vehicle: dict, language: str = 'ka',
             rules: Any = None) -> list[dict]:
    """Catalog engine oils for the resolved grades, stocked items first."""
    rows: dict[str, dict] = {}
    fuel = vehicle.get('fuel') or vehicle.get('fuel_type')
    for item in need['recommended']:
        brand = BRANDS.get(item['provider'].casefold())
        if not brand or not item['line']:
            continue
        found = repository.search_engine_oils(item['grade'], brand=brand, limit=100)
        for product in found.get('items', []):
            name = product.get('product_name', '')
            if _fuel_excludes(name, fuel) or normalize_grade(name) != item['grade']:
                continue
            if set(item['line']) <= set(_tokens(name)):
                rows.setdefault(product['source_product_id'], _row(
                    product, 'OIL_GRADE_BRAND',
                    f"{item['provider']}: {item['name']}", language))
    for grade in need['grades']:
        found = repository.search_engine_oils(grade, limit=100)
        added = 0
        for product in found.get('items', []):
            name = product.get('product_name', '')
            if (product['source_product_id'] in rows or _fuel_excludes(name, fuel)
                    or normalize_grade(name) != grade):
                continue
            if added >= TIER2_LIMIT:
                break
            rows[product['source_product_id']] = _row(
                product, 'OIL_GRADE', f"{'/'.join(need['sources'])}: {grade}", language)
            added += 1
    result = list(rows.values())
    rule = rules.approval_rule(vehicle, engine_family(need.get('engine_code'))) if rules else None
    if rule:
        # Staff say which approvals this car needs; the product name is the only evidence.
        kept = []
        for row in result:
            stated = approvals_stated(row['product'].get('product_name', ''), rule.require_any)
            row['approval'] = 'STATED' if stated else 'NOT_STATED'
            if stated:
                row['provenance'].append(f"Approval in name: {', '.join(stated)}")
            elif rule.strict:
                continue
            else:
                row['warnings'].append(NOTES['NOT_STATED'][0 if language == 'ka' else 1])
            kept.append(row)
        result = kept
    return sort_rows(result)


def coverage(rows: list[dict], capacity: float | None) -> list[dict]:
    """Do the stocked pack sizes add up to the litres the engine needs?"""
    if not capacity:
        return []
    groups: dict[tuple, dict] = {}
    tiers = {'OIL_GRADE_BRAND': 0, 'OIL_GRADE': 1}
    for row in rows:
        litres = row.get('pack_litres')
        product = row['product']
        if not litres or row['match_type'] not in tiers:
            continue
        have = product.get('stock') or {}
        key = (str(product.get('brand') or '').upper(), normalize_grade(
            product.get('product_name')))
        group = groups.setdefault(key, {'brand': key[0], 'grade': key[1], 'litres_store': 0.0,
                                        'litres_tuz11': 0.0, 'tier': tiers[row['match_type']]})
        group['tier'] = min(group['tier'], tiers[row['match_type']])
        group['litres_store'] += litres * (have.get('store') or 0)
        group['litres_tuz11'] += litres * (have.get('tuz11') or 0)
    result = []
    for group in groups.values():
        total = group['litres_store'] + group['litres_tuz11']
        if total <= 0:
            continue
        group['enough_store'] = group['litres_store'] >= capacity
        group['enough_with_tuz11'] = total >= capacity
        result.append(group)
    result.sort(key=lambda g: (g['tier'], -(g['litres_store'] + g['litres_tuz11'])))
    return [{k: v for k, v in g.items() if k != 'tier'} for g in result[:4]]


def engine_family_rows(repository: Any, need: dict, language: str = 'ka') -> list[dict]:
    """Oil filters and sump plugs naming the engine family the source gave."""
    family = engine_family(need.get('engine_code'))
    if not family:
        return []
    found = repository.search_engine_family(family, limit=50)
    source = '/'.join(need['sources'])
    rows = []
    for product in found.get('items', []):
        row = _row(product, 'ENGINE_FAMILY', f'{source}: {family}', language)
        row['role'] = product.get('part_role')
        rows.append(row)
    return sort_rows(rows)


def sort_rows(rows: list[dict]) -> list[dict]:
    tiers = {'OIL_GRADE_BRAND': 0, 'OIL_GRADE': 1, 'ENGINE_FAMILY': 0}
    return sorted(rows, key=lambda row: (
        RANKS[classify(row['product'].get('stock'))],
        0 if row.get('approval') == 'STATED' else 1, tiers.get(row['match_type'], 2)))
