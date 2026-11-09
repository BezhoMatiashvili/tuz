"""Last-resort matching of catalog product names to a car (make, model, year range).

Used only when no part code could be derived for the car. A name is weaker than a code,
so results are labelled for verification; a stated year range outside the car's year is
always a rejection.
"""

from __future__ import annotations

import re
from typing import Any

IN, OUT, NONE = 'IN', 'OUT', 'NONE'
MAKE_ALIASES = {
    'mercedesbenz': ['MERCEDES-BENZ', 'MERCEDES', 'MB'],
    'volkswagen': ['VOLKSWAGEN', 'VW'],
    'chevrolet': ['CHEVROLET', 'CHEVY'],
    'landrover': ['LAND ROVER', 'LANDROVER'],
}
# `08-`, `98-03`, `2010-2014`, and the open-ended `2010 >` / `2010+`.
YEAR_RANGE = re.compile(r'^\s*(\d{4}|\d{2})\s*(?:[-–]\s*(\d{4}|\d{2})?|[>+])(?!\d)')


def _key(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _make_names(make: str) -> list[str]:
    return MAKE_ALIASES.get(_key(make), [str(make).strip()])


def _model_core(model: str) -> str:
    parts = [re.escape(p) for p in re.split(r'[-\s]+', str(model).strip()) if p]
    return r'[- ]?'.join(parts)


def make_pattern(make: str) -> str:
    """PostgreSQL word-bounded regex for a make and its common catalog abbreviations."""
    return r'\m(?:' + '|'.join(re.escape(n) for n in _make_names(make)) + r')\M'


def model_pattern(model: str) -> str:
    """PostgreSQL regex: `E-Class` also matches `E CLASS`; `CR-V` matches `CRV`."""
    return r'\m' + _model_core(model) + r'\M'


def year_fit(text: str, model: str, model_year: int | None) -> str:
    """IN/OUT when the name states a year range right after the model, else NONE."""
    if not model_year or not str(model or '').strip():
        return NONE
    pattern = r'(?<![A-Za-z0-9])' + _model_core(model) + r'(?![A-Za-z0-9])'
    for found in re.finditer(pattern, text or '', re.IGNORECASE):
        stated = YEAR_RANGE.match((text or '')[found.end():found.end() + 14])
        if not stated:
            continue
        start = _two_or_four(stated.group(1))
        end = _two_or_four(stated.group(2)) if stated.group(2) else 2100
        return IN if start <= model_year <= end else OUT
    return NONE


def _two_or_four(text: str) -> int:
    value = int(text)
    if len(text) == 4:
        return value
    return 1900 + value if value >= 70 else 2000 + value
