"""Shared multilingual discovery filters; names never establish vehicle fitment."""

from __future__ import annotations

import re
import unicodedata

# Patterns are deliberately shared by PostgreSQL and Python. Georgian stems allow
# normal inflections; English terms have boundaries so oil does not match a coil.
PATTERNS = {
    # Non-engine oils first, so 'რედუქტორის ზეთის შეცვლა' is not an engine oil change.
    'transmission_oil': (r'(^|\W)(transmission|gear\s?box|atf|cvt)(\W|$)'
                         r'|კოლოფის\s+ზეთ|გადაცემათა|ტრანსმისი|ავტომატიკის\s+ზეთ'),
    'axle_oil': r'(^|\W)(axle|differential|diff)(\W|$)|რედუქტორ|დიფერენციალ',
    'steering_fluid': r'(^|\W)(power\s+steering|steering\s+fluid|hydraulic)(\W|$)|ჰიდრავლ'
                      r'|საჭის\s+(ზეთ|სითხ)',
    # A bundle: engine oil plus oil filter. Listed before 'oil' so it wins.
    'oil_change': (r'oil[ -]?change|change\s+(my\s+|the\s+)?oil|oil\s+service'
                   r'|ზეთ\S*\s+(შეცვლ|გამოცვლ|ცვლა|შევცვალ|გამოვცვალ)|(შეცვლ|გამოცვლ|შევცვალ)\S*\s+ზეთ'),
    # Before the brake categories, so a clutch disc is never a brake disc.
    'clutch': r'(^|\W)clutch(es)?(\W|$)|გადაბმულობ|კლაჩ|ქლაჩ|სცეპლენ',
    'brake_pads': r'(^|\W)(pads?|brake[ -]?pads?)(\W|$)|ხუნდ|კალოდკ',
    'brake_discs': r'(^|\W)(brake[ -]?dis[ck]s?|rotors?)(\W|$)|სამუხრუჭე\s+დისკ|მუხრუჭის\s+დისკ',
    'brakes': r'(^|\W)(brakes?|breaks?|pads?|rotors?)(\W|$)|სამუხრუჭ|მუხრუჭ|ხუნდ|კალოდკ',
    'oil_filter': r'(^|\W)oil[ -]?filters?(\W|$)|ზეთის\s+ფილტრ|ზეთფილტრ',
    'air_filter': r'(^|\W)air[ -]?filters?(\W|$)|ჰაერის\s+ფილტრ',
    'cabin_filter': r'(^|\W)(cabin|pollen)[ -]?filters?(\W|$)|სალონის\s+ფილტრ',
    'fuel_filter': r'(^|\W)(fuel|diesel|petrol)[ -]?filters?(\W|$)'
                   r'|საწვავის\s+ფილტრ|ბენზინის\s+ფილტრ|დიზელის\s+ფილტრ',
    'spark_plugs': r'(^|\W)spark[ -]?plugs?(\W|$)|სანთელ|სანთლ|სვეჩ',
    'battery': r'(^|\W)(batter(y|ies)|accumulators?)(\W|$)|აკუმულატორ|აკუმლიატორ|ბატარე',
    'wipers': r'(^|\W)(wipers?|wiper[ -]?blades?)(\W|$)|მინის\s+საწმენდ|დვორნიკ|საწმენდი\s+ჯაგრის',
    'timing_belt': r'(^|\W)(timing[ -]?(belt|chain|kit)s?|cam[ -]?belts?)(\W|$)'
                   r'|ტაიმინგ|გრმ|გაზგამანაწილებ',
    'coolant': r'(^|\W)(coolant|anti-?freeze)(\W|$)|ანტიფრიზ|ტოსოლ|გამაგრილებელი\s+სითხ',
    'bulbs': r'(^|\W)(bulbs?|headlight\s+bulbs?)(\W|$)|ნათურ',
    'engine_oil': r'(^|\W)(engine\s+oil|motor\s+oil|oil)(\W|$)|ძრავის\s+ზეთ|ზეთი|ზეთები',
    'suspension': r'(^|\W)(suspension|shocks?|struts?)(\W|$)|საკიდარ|ამორტიზატორ',
}
# Oils and fluids that are not engine oil; an oil change still includes the oil filter.
NOT_ENGINE_OIL = (
    r'(^|\W)(transmission|gear|brake|steering|hydraulic|axle|atf|cvt|coolant|antifreeze'
    r'|grease|sensor|radiator|pump|gasket|seal|cooler)(\W|$)'
    r'|ტრანსმისი|გადაცემ|სამუხრუჭ|ჰიდრავლ|რედუქტორ|ანტიფრიზ|საცხი|კოლოფის'
    r'|სენსორ|რადიატორ|ტუმბო|მანჟეტ|შუასადებ|საფენ|ჩობალ|კორპ'
)
OIL_EXCLUSIONS = r'(^|\W)filters?(\W|$)|ფილტრ|ფილ\.|' + NOT_ENGINE_OIL
OIL_WORD = r'ზეთ|(^|\W)(oils?|atf)(\W|$)'
FILTER_WORD = r'ფილტრ|(^|\W)filters?(\W|$)'
OTHER_OILS = ('transmission_oil', 'axle_oil', 'steering_fluid')
POSITIONS = {
    # `წნ` and `უკ` are the abbreviations the catalogue uses for front and rear.
    'front': r'(^|\W)front(\W|$)|წინა|(^|[^ა-ჰ])წნ($|[^ა-ჰ])',
    'rear': r'(^|\W)rear(\W|$)|უკანა|(^|[^ა-ჰ])უკ($|[^ა-ჰ])',
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
    if category == 'oil_change':
        # Names of either part of the bundle (engine oil or oil filter), never gear,
        # hydraulic or steering oils.
        return [f"({PATTERNS['engine_oil']})|({PATTERNS['oil_filter']})"], [NOT_ENGINE_OIL]
    if category in OTHER_OILS:
        return [PATTERNS[category], OIL_WORD + r'|სითხ|(^|\W)fluids?(\W|$)'], [FILTER_WORD]
    patterns = [PATTERNS[category]] if category else [re.escape(value)]
    patterns.extend(pattern for pattern in POSITIONS.values() if re.search(pattern, value))
    return patterns, [OIL_EXCLUSIONS] if category == 'engine_oil' else []


def matches_category(product: dict, query: str) -> bool:
    """Apply the same discovery filter used before SQL pagination."""
    text = ' '.join(str(product.get(key) or '') for key in ('product_name', 'product_model'))
    includes, excludes = query_patterns(query)
    return (all(re.search(pattern, text, re.IGNORECASE) for pattern in includes)
            and not any(re.search(pattern, text, re.IGNORECASE) for pattern in excludes))
