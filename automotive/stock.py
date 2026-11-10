"""Store and TUZ 11 stock read from the synchronized source columns.

Source column ``column_10`` ("1") is the quantity in the store. ``column_12``
("TUZ 11") is held in inventory and reaches the store in one to two hours.
``column_11`` ("TUZ 6") has no agreed meaning and is deliberately never used.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

STORE_COLUMN = 'column_10'
TUZ11_COLUMN = 'column_12'
IN_STORE, WITHIN_HOURS, UNAVAILABLE, UNKNOWN = (
    'IN_STORE', 'WITHIN_1_2_HOURS', 'UNAVAILABLE', 'UNKNOWN',
)
RANKS = {IN_STORE: 0, WITHIN_HOURS: 1, UNKNOWN: 2, UNAVAILABLE: 3}
NUMBER = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*$')
FRESH_HOURS = 13

# The same numeric test as a SQL expression; non-numeric cells count as zero.
_SQL_NUMBER = (r"CASE WHEN p.source_columns->>'{col}' ~ '^\s*[0-9]+([.,][0-9]+)?\s*$' "
               r"THEN replace(p.source_columns->>'{col}', ',', '.')::numeric ELSE 0 END")
SQL_STORE = _SQL_NUMBER.format(col=STORE_COLUMN)
SQL_TUZ11 = _SQL_NUMBER.format(col=TUZ11_COLUMN)
SQL_AVAILABLE_FIRST = f'(({SQL_STORE}) > 0) DESC, (({SQL_TUZ11}) > 0) DESC'


def parse_count(value: Any) -> float | None:
    """A non-negative quantity, or None when the source cell is not a plain number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    match = NUMBER.match(value) if isinstance(value, str) else None
    return float(match.group(1).replace(',', '.')) if match else None


def classify(stock: dict | None, min_store: float = 0.0, min_tuz11: float = 0.0) -> str:
    """IN_STORE, WITHIN_1_2_HOURS, UNAVAILABLE, or UNKNOWN when a count is missing.

    A quantity counts when it is greater than its minimum (rules.toml [stock])."""
    if not isinstance(stock, dict):
        return UNKNOWN
    store, tuz11 = parse_count(stock.get('store')), parse_count(stock.get('tuz11'))
    if store is not None and store > min_store:
        return IN_STORE
    if tuz11 is not None and tuz11 > min_tuz11:
        return WITHIN_HOURS
    # Zero everywhere is only a statement when both cells were actually read.
    return UNAVAILABLE if store is not None and tuz11 is not None else UNKNOWN


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f'{value:g}'


STALE_TEXT = (
    'მარაგის მონაცემი ძველია; გთხოვთ, გადაამოწმოთ მაღაზიაში.',
    'The stock data is old; please confirm in the store.',
)


def availability_text(stock: dict | None, language: str = 'ka',
                      min_store: float = 0.0, min_tuz11: float = 0.0) -> str:
    ka = language == 'ka'
    state = classify(stock, min_store, min_tuz11)
    store = parse_count((stock or {}).get('store'))
    tuz11 = parse_count((stock or {}).get('tuz11'))
    if state == IN_STORE:
        text = (f'მაღაზიაშია: {_number(store)} ც.' if ka else f'In the store: {_number(store)}')
        if tuz11:
            text += (f'; კიდევ {_number(tuz11)} ც. საწყობში (TUZ 11, 1–2 საათში)' if ka
                     else f'; {_number(tuz11)} more at our warehouse (TUZ 11, 1-2 hours)')
        return text
    if state == WITHIN_HOURS:
        return (f'ჩვენს საწყობშია (TUZ 11): {_number(tuz11)} ც.; მაღაზიაში იქნება 1–2 საათში.'
                if ka else
                f'At our warehouse (TUZ 11): {_number(tuz11)}; in the store within 1-2 hours.')
    if state == UNAVAILABLE:
        return ('ახლა არ არის მარაგში (არც მაღაზიაში, არც საწყობში).' if ka
                else 'Not available now (none in the store or our warehouse).')
    return ('ნაპოვნია ჩვენს კატალოგში; მარაგი დაუდასტურებელია.' if ka
            else 'Found in our catalog; stock unconfirmed.')


def age_hours(observed_at: Any, now: datetime | None = None) -> float | None:
    """Hours since the last sync observation; None when the timestamp is unknown."""
    if not isinstance(observed_at, datetime):
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - observed_at).total_seconds() / 3600


def is_stale(observed_at: Any, now: datetime | None = None, hours: float = FRESH_HOURS) -> bool:
    """True when the last sync observation is old enough that counts may have moved."""
    age = age_hours(observed_at, now)
    return age is not None and age > hours
