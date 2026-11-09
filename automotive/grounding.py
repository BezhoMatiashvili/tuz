"""Checks that customer-visible chat text states only facts the backend supplied.

Every code-like token, number, number word, known brand and mid-sentence name in a reply
must appear in the supplied facts. Claim classes such as price, guarantee, delivery or
contact details may appear only where the backend's own sentence already makes them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

GEORGIAN = re.compile(r'[ა-ჰ]')
# A token with a digit: counts, years, grades (5W-40), litres (6.5), codes (AP104/2).
DIGIT_TOKEN = re.compile(
    r'[A-Za-z0-9](?:[A-Za-z0-9./\-–]|,(?=\d))*\d(?:[A-Za-z0-9./\-–]|,(?=\d))*|\d')
WORD = re.compile(r"[A-Za-zა-ჰ][A-Za-zა-ჰ'’\-]*")
NUMBER_WORDS = {
    'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7',
    'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17', 'eighteen': '18',
    'nineteen': '19', 'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50',
    'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90', 'hundred': '100',
    'thousand': '1000', 'dozen': '12', 'both': '2',
}
# Georgian counts take case endings ('ორს', 'სამით'); 'ორივე' is "both".
GEORGIAN_NUMBERS = {'ერთ': '1', 'ორ': '2', 'სამ': '3', 'ოთხ': '4', 'ხუთ': '5', 'ექვს': '6',
                    'შვიდ': '7', 'რვ': '8', 'ცხრ': '9', 'ათ': '10', 'ოც': '20', 'ას': '100'}
GEORGIAN_NUMBER_ENDINGS = ('', 'ი', 'ა', 'ს', 'ის', 'ით', 'ივე', 'ზე')
# Sentence openers that are ordinary words, not names that need a fact behind them.
SENTENCE_STARTERS = frozenset({
    'i', 'it', "it's", 'its', 'they', "they're", 'we', 'you', 'your', 'this', 'that', 'these',
    'those', 'here', "here's", 'there', "there's", 'good', 'great', 'sure', 'yes', 'no', 'sorry',
    'both', 'all', 'some', 'none', 'one', 'need', 'want', 'would', 'could', 'can', 'please',
    'just', 'also', 'unfortunately', 'meanwhile', 'hi', 'hello', 'thanks', 'happy', 'got',
    'okay', 'ok', 'for', 'and', 'but', 'so', 'the', 'a', 'an', 'we\'ve', 'i\'ve', 'i\'m',
    'i\'ll', 'let', "let's", 'right', 'now', 'at', 'in', 'on', 'if', 'what', 'which', 'only',
    'still', 'again', 'perfect', 'nice', 'found', 'looks', 'everything', 'either', 'each',
    "that's", "you're", "we're", "don't", "can't", "couldn't", "isn't", "what's", 'or', 'which',
    'to', 'then', 'pads', 'front', 'rear', 'discs', 'stock', 'send', 'take', 'drive', 'bye',
    'problem', 'anything', 'chat', 'direct', 'oh', 'well', 'actually', 'alright', 'sounds',
    'glad', 'thank', 'welcome', 'anytime', 'goodbye', 'see', 'oil', 'engine', 'brake', 'air',
    'cabin', 'our', 'my', 'is', 'are', 'do', 'does', 'did', 'have', 'has', 'will', 'should',
})
PART_BRANDS = frozenset({
    'bosch', 'trw', 'brembo', 'ate', 'textar', 'ferodo', 'mann', 'mahle', 'knecht', 'hengst',
    'ngk', 'denso', 'valeo', 'sachs', 'lemforder', 'febi', 'swag', 'meyle', 'castrol', 'mobil',
    'shell', 'total', 'motul', 'fuchs', 'pakelo', 'elf', 'zic', 'kixx', 'liqui', 'ravenol',
    'varta', 'exide', 'champion', 'filtron', 'ufi', 'purflux', 'kyb', 'monroe', 'bilstein',
    'akebono', 'nisshinbo', 'sangsin', 'blue print', 'nipparts', 'herth', 'valvoline',
    'hella', 'osram', 'philips', 'gates', 'contitech', 'dayco', 'luk', 'aisin', 'exedy',
    'ashika', 'sato', 'sb', 'japanparts', 'kyb', 'sakura', 'vic', 'mobis', 'petronas',
})
# Georgian spellings of brands, matched as a stem with an ordinary case ending.
GEORGIAN_BRANDS = ('ბოშ', 'ბრემბო', 'კასტროლ', 'მობილ', 'შელ', 'ტოტალ', 'ვარტა', 'დენსო',
                   'მაჰლე', 'ფილტრონ', 'მოტულ', 'ფუქს', 'პაკელო', 'ზიკ', 'ნგკ', 'ტოიოტ',
                   'ჰონდ', 'მერსედეს', 'ბმვ', 'ნისან', 'ჰიუნდა', 'ლექსუს', 'ჰელა', 'ოსრამ')
CAR_MAKES = frozenset({
    'toyota', 'lexus', 'honda', 'nissan', 'mazda', 'subaru', 'mitsubishi', 'suzuki', 'hyundai',
    'kia', 'mercedes', 'mercedes-benz', 'bmw', 'audi', 'volkswagen', 'vw', 'opel', 'ford',
    'chevrolet', 'porsche', 'volvo', 'jeep', 'dodge', 'renault', 'peugeot', 'citroen', 'skoda',
    'fiat', 'infiniti', 'acura', 'tesla', 'jaguar', 'land rover',
    'ტოიოტა', 'ჰონდა', 'მერსედესი', 'ბმვ',
})
# Naming a brand or make that is not in the facts is an invention.
BRANDS = PART_BRANDS | CAR_MAKES
# Claims a rephrase may only repeat, never introduce.
CLAIMS = {
    'price': r'price|cost|cheap|expensive|discount|\bsale\b|bargain|\bdeal\b|\blari\b|₾|\bgel\b'
             r'|\$|€|ფას|ლარ|იაფ|ძვირ|ღირს|აქცი',
    'guarantee': r'guarantee|definitely|certainly|100\s*%|perfect(ly)? fit|warrant'
                 r'|\b(fits?|match(es)?)\b[^.!?]{0,40}\bexactly\b|oem quality|lifetime'
                 r'|safe to (use|drive)|გარანტ|აუცილებლად|ზუსტად (მოერგ|ერგება|შეესაბამ)'
                 r'|უსაფრთხო',
    'delivery': r'deliver|shipping|courier|tomorrow|today|minute|\d+\s*(days?|weeks?)\b'
                r'|next (day|week)|same day|მიწოდ|კურიერ|ხვალ|დღეს|წუთ|\d+\s*დღე|კვირაში',
    'contact': r'call us|phone|e-?mail|whatsapp|telegram|viber|instagram|facebook|messenger'
               r'|address|დარეკ|ტელეფონ|მისამართ|ტელეგრამ|ვაიბერ',
    'url': r'https?://|www\.|[a-z0-9-]+\.[a-z]{2,}(?![a-z0-9])|@[a-z0-9_]{2,}',
    'store': r'in the store|in stock|available|on hand|მაღაზი|მარაგ',
    'tuz11': r'tuz\s*11|\b1\s*[-–]\s*2\s*hours?|1\s*[-–]\s*2\s*საათ',
    'fitment': r'\bverified\b|\bconfirmed\b|compatible|\bfits?\b|დადასტურ|თავსებად|ერგება',
}
NEVER_WITHOUT_FACT = ('price', 'guarantee', 'delivery', 'contact', 'url')
SAFE_NAMES = frozenset({'i', "i'm", "i'll", "i've", "i'd", 'tuz', 'vin', 'ok', 'oe', 'oem'})
MAX_LENGTH = 400


def _norm(value: str) -> str:
    """Spacing and slashes vary in codes; a hyphen stays, so '1-2' never reads as 12."""
    value = unicodedata.normalize('NFKC', value).casefold().replace('–', '-').replace(',', '.')
    return re.sub(r'[\s/]+', '', value).strip('.-')


def _plain(value: Any) -> str:
    """Flatten nested facts into text."""
    if isinstance(value, dict):
        return ' '.join(_plain(v) for v in value.values())
    if isinstance(value, list | tuple | set):
        return ' '.join(_plain(v) for v in value)
    return '' if value is None else str(value)


def facts_text(payload: dict, messages: Iterable[str] = ()) -> str:
    """Everything a reply may state for one answer: cards, car, needs, counts, user text."""
    cards = [c for c in payload.get('cards') or [] if isinstance(c, dict)]
    parts: list[str] = list(messages)
    states = [c.get('availability_status') for c in cards]
    total = payload.get('total', len(cards))
    counts = {len(cards), total, states.count('IN_STORE'), states.count('WITHIN_1_2_HOURS'),
              states.count('UNKNOWN'), len(payload.get('supplier_only') or [])}
    parts.append(' '.join(str(n) for n in counts))
    parts.append('1-2')  # The warehouse-to-store time every availability sentence uses.
    for card in cards:
        product = card.get('product') or {}
        parts.append(_plain({k: product.get(k) for k in (
            'product_code', 'product_name', 'product_model', 'original_code', 'duplicate_code',
            'brand', 'stock')}))
        parts.append(_plain(product.get('matched_codes') or []))
        parts.append(_plain([card.get('availability'), card.get('pack_litres'),
                             card.get('matched_via')]))
    parts.append(_plain(payload.get('vehicle') or {}))
    parts.append(_plain(payload.get('requirements') or {}))
    parts.append(_plain(payload.get('supplier_only') or []))
    parts.append(_plain(payload.get('facts') or {}))
    return ' '.join(p for p in parts if p)


def digit_tokens(text: str) -> set[str]:
    """Normalized numbers and codes in a text ('1-2', 'AP104/2', '6.5')."""
    return {_norm(t) for t in DIGIT_TOKEN.findall(text or '')} - {''}


def numbers(text: str) -> set[str]:
    """Digit tokens plus counts written as words ('One is in the store', 'ორი')."""
    words = {w.casefold() for w in WORD.findall(text or '')}
    spelled = {NUMBER_WORDS[w] for w in words if w in NUMBER_WORDS}
    if words & {'one', 'ერთი'}:
        spelled.add('1')
    return digit_tokens(text) | spelled


def part_brand(text: str) -> str:
    """The first parts or oil brand a message names, as written there; '' when none."""
    value = unicodedata.normalize('NFKC', text or '')
    for brand in sorted(PART_BRANDS, key=len, reverse=True):
        found = re.search(rf'(?<![a-zA-Zა-ჰ0-9]){re.escape(brand)}(?![a-zA-Zა-ჰ0-9])', value,
                          re.IGNORECASE)
        if found:
            return found.group(0)
    return ''


def names_part_brand(text: str) -> bool:
    return bool(part_brand(text))


def _claims(text: str) -> set[str]:
    return {name for name, pattern in CLAIMS.items() if re.search(pattern, text, re.IGNORECASE)}


def problems(text: str, allowed: str, language: str, draft: str | None = None,
             max_questions: int | None = None) -> list[str]:
    """Reasons the text is not grounded; an empty list means it may be shown.

    ``draft`` is the backend's own sentence: claims are allowed only where it makes them.
    Without a draft, only claims that no backend sentence ever makes are rejected."""
    found: list[str] = []
    if not text or not text.strip():
        return ['empty']
    if len(text) > MAX_LENGTH:
        return ['too_long']  # Checked first: the token patterns are not run on long input.
    for char in text:
        if not char.isascii() and unicodedata.digit(char, None) is not None:
            return ['non_ascii_digit']
        if char.isalpha() and not char.isascii() and not GEORGIAN.match(char):
            return ['foreign_script']  # Look-alike letters such as Cyrillic 'В'.
    reference = f'{allowed[:20000]} {draft or ""}'
    allowed_tokens = {_norm(t) for t in DIGIT_TOKEN.findall(reference)}
    allowed_tokens |= {_norm(t) for t in re.findall(r'\d+(?:[.,]\d+)?', reference)}
    for token in DIGIT_TOKEN.findall(text):
        key = _norm(token)
        if key and key not in allowed_tokens and not all(
                _norm(part) in allowed_tokens for part in re.split(r'[-–/]', token) if part):
            found.append(f'unknown_token:{token}')
    reference_words = {w.casefold() for w in WORD.findall(reference)}
    for word in WORD.findall(text):
        lowered_word = word.casefold()
        digit = NUMBER_WORDS.get(lowered_word) or next(
            (value for stem, value in GEORGIAN_NUMBERS.items()
             if lowered_word.startswith(stem)
             and lowered_word[len(stem):] in GEORGIAN_NUMBER_ENDINGS), None)
        if digit and digit not in allowed_tokens and lowered_word not in reference_words:
            found.append(f'unknown_number:{word}')
        brand = next((stem for stem in GEORGIAN_BRANDS if lowered_word.startswith(stem)
                      and lowered_word[len(stem):] in GEORGIAN_NUMBER_ENDINGS + ('ის', 'ი')),
                     None)
        if brand and lowered_word not in reference_words and not any(
                w.startswith(brand) for w in reference_words):
            found.append(f'unknown_brand:{word}')
    lowered = unicodedata.normalize('NFKC', text).casefold()
    reference_lower = unicodedata.normalize('NFKC', reference).casefold()
    for brand in BRANDS:
        pattern = rf'(?<![a-zა-ჰ0-9]){re.escape(brand)}(?![a-zა-ჰ0-9])'
        if re.search(pattern, lowered) and not re.search(pattern, reference_lower):
            found.append(f'unknown_brand:{brand}')
    # A capitalized Latin word is a name unless it is an ordinary sentence opener; names
    # must come from the facts.
    for match in re.finditer(r"(?:^|(?<=\s)|(?<=[(\"“]))([A-Z][A-Za-z'’\-]+)", text):
        word = match.group(1).rstrip("-'’")  # `VIN-ით` and `Accord-ისთვის` carry suffixes.
        key = word.casefold()
        if key in reference_words or key in SAFE_NAMES:
            continue
        before = text[:match.start(1)].rstrip(' (\"“')
        sentence_start = not before or before[-1] in '.!?:—-\n'
        if not (sentence_start and key in SENTENCE_STARTERS):
            found.append(f'unknown_name:{word}')
    claims = _claims(text)
    if draft is not None:
        for name in claims - _claims(draft):
            found.append(f'claim:{name}')
    else:
        for name in claims & set(NEVER_WITHOUT_FACT):
            if name not in _claims(allowed):
                found.append(f'claim:{name}')
    if max_questions is not None and text.count('?') > max_questions:
        found.append('too_many_questions')
    if not language_ok(text, language, allowed):
        found.append('wrong_language')
    return list(dict.fromkeys(found))


def language_ok(text: str, language: str, allowed: str = '') -> bool:
    """Georgian replies are mostly Georgian; English replies are mostly not.

    Words copied from the facts (product names, car names) do not count either way."""
    reference = {w.casefold() for w in WORD.findall(allowed)}
    words = [w for w in WORD.findall(text) if w.casefold() not in reference]
    if not words:
        return True
    georgian = sum(1 for w in words if GEORGIAN.search(w))
    share = georgian / len(words)
    return share >= 0.5 if language == 'ka' else share <= 0.2
