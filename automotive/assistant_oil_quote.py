"""Pack pricing for the assistant's catalogue, whose oil prices and counts are per litre."""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from automotive import stock
from automotive.categories import OIL_EXCLUSIONS, PATTERNS
from automotive.oil_match import normalize_grade, pack_litres

_FLUID = re.compile(
    r"^(?:ზეთი|(?:ძრავის|ტრანსმისიის|კოლოფის|ჰიდრავლიკის|რედუქტორის|კომპრესორის|ხიდის)"
    r"\s+ზეთი?|(?:სინთეტური|სინთეთური|მექანიკური)\s+"
    r"(?:ძრავის|კოლოფის|ჰიდრავლიკის)\s+ზეთი|engine oil|motor oil|gear oil|"
    r"transmission (?:oil|fluid))(?=\W|$)", re.IGNORECASE,
)
_VOLUME = re.compile(
    r"(?<![\w.,/\-])(\d{1,3}(?:[.,]\d{1,3})?)\s*"
    r"(?:lit(?:res?|ers?)|ltr|lt|l|ლიტრ[ა-ჰ]*|ლ|литр[а-яё]*|л)(?!\w)", re.IGNORECASE,
)


def is_oil(name: str) -> bool:
    return bool(_FLUID.match(name.strip()))


def is_engine_oil(name: str) -> bool:
    return (is_oil(name) and bool(re.search(PATTERNS["engine_oil"], name, re.IGNORECASE))
            and not re.search(OIL_EXCLUSIONS, name, re.IGNORECASE))


def is_oil_filter(name: str) -> bool:
    return bool(re.search(PATTERNS["oil_filter"], name, re.IGNORECASE))


def oil_pack(name: str) -> float | None:
    return pack_litres(name) if is_oil(name) else None


def oil_grade_conflict(name: str, code: str) -> bool:
    if not is_oil(name):
        return False
    named, coded = normalize_grade(name), normalize_grade(code)
    return bool(named and coded and named != coded)


def pack_price(price_per_litre: Any, litres: float) -> float | None:
    if (isinstance(price_per_litre, bool) or not isinstance(price_per_litre, int | float)
            or not 0 < price_per_litre < float("inf")):
        return None
    return _money(Decimal(str(price_per_litre)) * Decimal(str(litres)))


def _money(value: Decimal) -> float | None:
    with localcontext() as context:
        context.prec = max(context.prec, value.adjusted() + 4)
        rounded = float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return rounded if 0 < rounded < float("inf") else None


def pack_counts(counts: Any, litres: float) -> dict[str, int | None]:
    counts = counts if isinstance(counts, dict) else {}
    size = Decimal(str(litres))
    result: dict[str, int | None] = {}
    for key in ("store", "tuz11"):
        amount = stock.parse_count(counts.get(key))
        result[key] = (int(Decimal(str(amount)) // size)
                       if amount is not None and 0 <= amount < float("inf") else None)
    return result


def requested_litres(text: str) -> set[float]:
    """Read explicit volume units so bare numbers and SAE grades cannot become quantities."""
    return {float(match.replace(",", ".")) for match in _VOLUME.findall(text)}


def oil_filter_total(oil: dict, oil_filter: dict, litres: float) -> tuple[int, float] | None:
    pack = Decimal(str(oil["pack_litres"]))
    count = int((Decimal(str(litres)) / pack).to_integral_value(rounding=ROUND_CEILING))
    for card, needed in ((oil, count), (oil_filter, 1)):
        product = card["product"]
        if card["availability_status"] not in (stock.IN_STORE, stock.WITHIN_HOURS):
            return None
        counts = product.get("stock") or {}
        if sum(stock.parse_count(counts.get(key)) or 0 for key in ("store", "tuz11")) < needed:
            return None
        if pack_price(product.get("price"), 1) is None:
            return None
    total = Decimal(str(oil["product"]["price"])) * count
    total += Decimal(str(oil_filter["product"]["price"]))
    rounded = _money(total)
    return (count, rounded) if rounded is not None else None
