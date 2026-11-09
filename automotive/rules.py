"""Accuracy rules: built-in hard rules plus the staff-editable ``rules.toml``.

The hard rules (axle, part-type conflict, vehicle consistency, code safety) are code and
always on. Staff data (brand aliases, words that mean "not the part", oil approvals,
products to never offer, fitments staff confirmed) lives in ``rules.toml`` and is
validated at startup: an unknown key or a wrong type stops the application with a message
that names the section, so a typo can never silently disable a rule.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automotive.categories import OIL_EXCLUSIONS, PATTERNS
from automotive.name_match import MAKE_ALIASES

DEFAULT_PATH = Path(__file__).with_name('rules.toml')
AXLE_CATEGORIES = {'brake_pads', 'brake_discs', 'brakes', 'suspension'}
# Part types a requested category may legitimately contain.
BUNDLES = {
    'oil_change': {'engine_oil', 'oil_filter'},
    'brakes': {'brake_pads', 'brake_discs', 'brakes'},
}
LETTER = r'A-Za-zა-ჰ'
FRONT = re.compile(rf'(?<![{LETTER}])(?:front|წინა|წინ|წნ)(?![{LETTER}])', re.IGNORECASE)
REAR = re.compile(rf'(?<![{LETTER}])(?:rear|უკანა|უკან|უკ)(?![{LETTER}])', re.IGNORECASE)
APPROVAL_BOUNDARY = r'(?<![\d.]){}(?![\d])'


class RulesError(ValueError):
    """The rules file is invalid; the message names the section and entry."""


@dataclass(frozen=True)
class OilApproval:
    make: str
    model: str | None
    engine_family: str | None
    require_any: tuple[str, ...]
    strict: bool


@dataclass(frozen=True)
class NeverOffer:
    product_code: str
    make: str | None
    model: str | None
    reason: str


@dataclass(frozen=True)
class StaffConfirmed:
    make: str
    model: str
    year_from: int | None
    year_to: int | None
    category: str
    product_code: str
    brand: str | None
    note: str


@dataclass(frozen=True)
class Rules:
    min_store: float = 0.0
    min_tuz11: float = 0.0
    stale_hours: float = 3.0
    hide_after_hours: float = 24.0
    brand_groups: tuple[frozenset[str], ...] = ()
    not_the_part: dict[str, tuple[str, ...]] = field(default_factory=dict)
    oil_approvals: tuple[OilApproval, ...] = ()
    never_offer: tuple[NeverOffer, ...] = ()
    staff_confirmed: tuple[StaffConfirmed, ...] = ()

    # --- brands ---------------------------------------------------------------------
    def brands_match(self, left: Any, right: Any) -> bool:
        """`MANN` and `MANN-FILTER` are one brand; so is anything a brand_alias lists."""
        a, b = _key(left), _key(right)
        if len(a) < 2 or len(b) < 2:
            return False
        if a == b or a.startswith(b) or b.startswith(a):
            return True
        return any(a in group and b in group for group in self.brand_groups)

    # --- part-type rules --------------------------------------------------------------
    def not_the_part_words(self, category: str | None) -> tuple[str, ...]:
        keys = BUNDLES.get(category or '', {category or ''})
        return tuple(word for key in keys for word in self.not_the_part.get(key, ()))

    def part_conflict(self, name: str, category: str | None) -> bool:
        """True when the name is another kind of part, or an accessory listed as not the part."""
        if not category or category not in PATTERNS:
            return False
        text = name.casefold()
        if any(word in text for word in self.not_the_part_words(category)):
            return True
        if category == 'engine_oil' and re.search(OIL_EXCLUSIONS, name, re.IGNORECASE):
            return True  # Filters, gear oil, sensors and the like are not engine oil.
        allowed = BUNDLES.get(category, {category})
        if any(re.search(PATTERNS[key], name, re.IGNORECASE)
               for key in allowed if key in PATTERNS):
            return False
        others = [key for key in PATTERNS if key not in allowed and key != 'oil_change']
        return any(re.search(PATTERNS[key], name, re.IGNORECASE) for key in others)

    # --- staff data -------------------------------------------------------------------
    def never_reason(self, product: dict, vehicle: dict) -> str | None:
        code = _key(product.get('product_code'))
        for rule in self.never_offer:
            if _key(rule.product_code) == code and _vehicle_matches(
                    rule.make, rule.model, None, None, vehicle):
                return rule.reason or 'never_offer'
        return None

    def confirmed_for(self, vehicle: dict, category: str | None) -> list[StaffConfirmed]:
        allowed = BUNDLES.get(category or '', {category or ''})
        return [rule for rule in self.staff_confirmed if rule.category in allowed
                and _vehicle_matches(rule.make, rule.model, rule.year_from, rule.year_to,
                                     vehicle)]

    def approval_rule(self, vehicle: dict, engine_family: str | None) -> OilApproval | None:
        for rule in self.oil_approvals:
            if _key(rule.make) != _key(vehicle.get('make')):
                continue
            if rule.model and _key(rule.model) not in _key(vehicle.get('model')):
                continue
            if rule.engine_family and _key(rule.engine_family) != _key(engine_family):
                continue
            return rule
        return None


def _key(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _vehicle_matches(make: str | None, model: str | None, year_from: int | None,
                     year_to: int | None, vehicle: dict) -> bool:
    if make and _key(make) != _key(vehicle.get('make')):
        return False
    if model and _key(model) not in _key(vehicle.get('model')):
        return False
    year = vehicle.get('model_year')
    if isinstance(year, int):
        if year_from and year < year_from or year_to and year > year_to:
            return False
    elif year_from or year_to:
        return False
    return True


# --- built-in hard rules --------------------------------------------------------------
def requested_axle(text: str) -> str | None:
    """`front` or `rear` when the customer's wording names one axle."""
    front, rear = bool(FRONT.search(text or '')), bool(REAR.search(text or ''))
    return 'front' if front and not rear else 'rear' if rear and not front else None


def axle_verdict(name: str, requested: str | None, category: str | None) -> str:
    """MATCH, OPPOSITE (named for the other axle) or UNSTATED; N/A when it does not apply."""
    if requested is None or category not in AXLE_CATEGORIES:
        return 'N/A'
    front, rear = bool(FRONT.search(name)), bool(REAR.search(name))
    if front and rear:
        return 'MATCH'
    if (requested == 'front' and rear) or (requested == 'rear' and front):
        return 'OPPOSITE'
    return 'MATCH' if front or rear else 'UNSTATED'


def vehicle_consistent(label: str | None, vehicle: dict) -> bool:
    """Does the catalog vehicle label name the same make and model as the decoded car?

    Unknown on either side is not a contradiction; a different make, or a model sharing
    nothing with the label, is."""
    text = _key(label)
    make, model = _key(vehicle.get('make')), _key(vehicle.get('model'))
    if not text or not make or not model:
        return True
    names = [_key(n) for n in MAKE_ALIASES.get(make, [vehicle.get('make')])]
    if not any(name and name in text for name in names):
        return False
    return model in text or model[:4] in text


def oem_code_ok(code: str) -> bool:
    """A bare OEM-labelled code is only trusted when long enough to be distinctive."""
    normalized = re.sub(r'[^A-Za-z0-9]+', '', code or '')
    return len(normalized) >= 6 and (not normalized.isdigit() or len(normalized) >= 8)


def approvals_stated(name: str, approvals: tuple[str, ...]) -> list[str]:
    """Required approvals that the product name states (`229.5` is not `229.52`)."""
    return [a for a in approvals if re.search(
        APPROVAL_BOUNDARY.format(re.escape(a)), name or '', re.IGNORECASE)]


# --- loading and validation -----------------------------------------------------------
_TOP = {'stock', 'brand_alias', 'not_the_part', 'oil_approval', 'never_offer',
        'staff_confirmed'}
_ALLOWED = {
    'stock': {'min_store', 'min_tuz11', 'stale_hours', 'hide_after_hours'},
    'brand_alias': {'names'},
    'not_the_part': {'category', 'words'},
    'oil_approval': {'make', 'model', 'engine_family', 'require_any', 'strict'},
    'never_offer': {'product_code', 'make', 'model', 'reason'},
    'staff_confirmed': {'make', 'model', 'year_from', 'year_to', 'category', 'product_code',
                        'brand', 'note'},
}


def _check(section: str, index: int, entry: Any, required: set[str]) -> dict:
    where = f'[{section}]' if index < 0 else f'[[{section}]] #{index + 1}'
    if not isinstance(entry, dict):
        raise RulesError(f'{where} must be a table')
    unknown = set(entry) - _ALLOWED[section]
    if unknown:
        raise RulesError(f'{where} has unknown keys: {", ".join(sorted(unknown))}')
    missing = required - set(entry)
    if missing:
        raise RulesError(f'{where} is missing: {", ".join(sorted(missing))}')
    return entry


def _text(where: str, entry: dict, key: str, optional: bool = False) -> str | None:
    value = entry.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RulesError(f'{where} "{key}" must be a non-empty string')
    return value.strip()


def _texts(where: str, entry: dict, key: str) -> tuple[str, ...]:
    value = entry.get(key)
    if (not isinstance(value, list) or not value
            or not all(isinstance(v, str) and v.strip() for v in value)):
        raise RulesError(f'{where} "{key}" must be a non-empty list of strings')
    return tuple(v.strip() for v in value)


def _number(where: str, entry: dict, key: str, default: float) -> float:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise RulesError(f'{where} "{key}" must be a number of 0 or more')
    return float(value)


def _year(where: str, entry: dict, key: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1950 <= value <= 2100:
        raise RulesError(f'{where} "{key}" must be a year between 1950 and 2100')
    return value


def parse_rules(data: dict) -> Rules:
    unknown = set(data) - _TOP
    if unknown:
        raise RulesError(f'Unknown top-level sections: {", ".join(sorted(unknown))}')
    stock = _check('stock', -1, data.get('stock', {}), set())
    min_store = _number('[stock]', stock, 'min_store', 0.0)
    min_tuz11 = _number('[stock]', stock, 'min_tuz11', 0.0)
    stale = _number('[stock]', stock, 'stale_hours', 3.0)
    hide = _number('[stock]', stock, 'hide_after_hours', 24.0)
    if hide < stale:
        raise RulesError('[stock] hide_after_hours must not be below stale_hours')
    groups = []
    for i, entry in enumerate(data.get('brand_alias', [])):
        where = f'[[brand_alias]] #{i + 1}'
        names = _texts(where, _check('brand_alias', i, entry, {'names'}), 'names')
        groups.append(frozenset(_key(n) for n in names))
    not_part: dict[str, list[str]] = {}
    for i, entry in enumerate(data.get('not_the_part', [])):
        where = f'[[not_the_part]] #{i + 1}'
        entry = _check('not_the_part', i, entry, {'category', 'words'})
        category = _text(where, entry, 'category')
        if category not in PATTERNS:
            raise RulesError(f'{where} category "{category}" is not a known category')
        not_part.setdefault(category, []).extend(
            w.casefold() for w in _texts(where, entry, 'words'))
    approvals = []
    for i, entry in enumerate(data.get('oil_approval', [])):
        where = f'[[oil_approval]] #{i + 1}'
        entry = _check('oil_approval', i, entry, {'make', 'require_any'})
        strict = entry.get('strict', False)
        if not isinstance(strict, bool):
            raise RulesError(f'{where} "strict" must be true or false')
        approvals.append(OilApproval(
            _text(where, entry, 'make') or '', _text(where, entry, 'model', True),
            _text(where, entry, 'engine_family', True), _texts(where, entry, 'require_any'),
            strict))
    never = []
    for i, entry in enumerate(data.get('never_offer', [])):
        where = f'[[never_offer]] #{i + 1}'
        entry = _check('never_offer', i, entry, {'product_code'})
        never.append(NeverOffer(_text(where, entry, 'product_code') or '',
                                _text(where, entry, 'make', True),
                                _text(where, entry, 'model', True),
                                _text(where, entry, 'reason', True) or ''))
    confirmed = []
    for i, entry in enumerate(data.get('staff_confirmed', [])):
        where = f'[[staff_confirmed]] #{i + 1}'
        entry = _check('staff_confirmed', i, entry,
                       {'make', 'model', 'category', 'product_code'})
        category = _text(where, entry, 'category') or ''
        if category not in PATTERNS:
            raise RulesError(f'{where} category "{category}" is not a known category')
        confirmed.append(StaffConfirmed(
            _text(where, entry, 'make') or '', _text(where, entry, 'model') or '',
            _year(where, entry, 'year_from'), _year(where, entry, 'year_to'), category,
            _text(where, entry, 'product_code') or '', _text(where, entry, 'brand', True),
            _text(where, entry, 'note', True) or ''))
    return Rules(
        min_store=min_store, min_tuz11=min_tuz11, stale_hours=stale, hide_after_hours=hide,
        brand_groups=tuple(groups),
        not_the_part={k: tuple(dict.fromkeys(v)) for k, v in not_part.items()},
        oil_approvals=tuple(approvals), never_offer=tuple(never),
        staff_confirmed=tuple(confirmed),
    )


def load_rules(path: Path | str | None = None) -> Rules:
    """Read and validate the rules file; a missing default file means built-in defaults."""
    target = Path(path) if path else DEFAULT_PATH
    if not target.is_file():
        if path:
            raise RulesError(f'Rules file not found: {target}')
        return Rules()
    try:
        data = tomllib.loads(target.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as exc:
        raise RulesError(f'{target.name} is not valid TOML: {exc}') from None
    return parse_rules(data)
