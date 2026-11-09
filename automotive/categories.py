"""Shared multilingual discovery filters; names never establish vehicle fitment."""

from __future__ import annotations

import re
import unicodedata

# Patterns are deliberately shared by PostgreSQL and Python. Georgian stems allow
# normal inflections; English terms have boundaries so oil does not match a coil.
PATTERNS = {
    'brake_pads': r'(^|\W)(pads?|brake[ -]?pads?)(\W|$)|ხუნდ|კალოდკ',
    'brake_discs': r'(^|\W)(brake[ -]?dis[ck]s?|rotors?)(\W|$)|სამუხრუჭე\s+დისკ|მუხრუჭის\s+დისკ',
    'brakes': r'(^|\W)(brakes?|breaks?|pads?|rotors?)(\W|$)|სამუხრუჭ|მუხრუჭ|ხუნდ|კალოდკ',
    'oil_filter': r'(^|\W)oil[ -]?filters?(\W|$)|ზეთის\s+ფილტრ|ზეთფილტრ',
    'air_filter': r'(^|\W)air[ -]?filters?(\W|$)|ჰაერის\s+ფილტრ',
    'cabin_filter': r'(^|\W)(cabin|pollen)[ -]?filters?(\W|$)|სალონის\s+ფილტრ',
    'engine_oil': r'(^|\W)(engine\s+oil|motor\s+oil|oil)(\W|$)|ძრავის\s+ზეთ|ზეთი|ზეთები',
    'suspension': r'(^|\W)(suspension|shocks?|struts?)(\W|$)|საკიდარ|ამორტიზატორ',
}
OIL_EXCLUSIONS = (
    r'(^|\W)(filters?|transmission|gear|brake|steering)(\W|$)|ფილტრ|ტრანსმისი|გადაცემ|სამუხრუჭ'
)
POSITIONS = {
    'front': r'(^|\W)front(\W|$)|წინა',
    'rear': r'(^|\W)rear(\W|$)|უკანა',
}


def category_for_query(query: str) -> str | None:
    """Return the most specific category, including conversational spelling variants."""
    value = unicodedata.normalize('NFKC', query).casefold().replace('_', ' ')
    for category, pattern in PATTERNS.items():
        if category == 'brake_discs' and re.search(r'(^|\W)dis[ck]s?(\W|$)', value):
            return category
        if re.search(pattern, value, re.IGNORECASE):
            return category
    return None


def query_patterns(query: str) -> tuple[list[str], list[str]]:
    """Parameterized AND filters plus exclusions for a product-name query."""
    value = unicodedata.normalize('NFKC', query).strip().casefold()
    if not value:
        return [], []
    category = category_for_query(value)
    patterns = [PATTERNS[category]] if category else [re.escape(value)]
    patterns.extend(pattern for pattern in POSITIONS.values() if re.search(pattern, value))
    return patterns, [OIL_EXCLUSIONS] if category == 'engine_oil' else []


def matches_category(product: dict, query: str) -> bool:
    """Apply the same discovery filter used before SQL pagination."""
    text = ' '.join(str(product.get(key) or '') for key in ('product_name', 'product_model'))
    includes, excludes = query_patterns(query)
    return (all(re.search(pattern, text, re.IGNORECASE) for pattern in includes)
            and not any(re.search(pattern, text, re.IGNORECASE) for pattern in excludes))
