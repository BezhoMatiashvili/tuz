"""Deterministic conversation understanding: small talk, typed cars, short answers.

Nothing here calls a model. A typed car is the customer's own statement, used like the
manual vehicle form; it never becomes fitment evidence.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from automotive.categories import category_for_query

SMALL_TALK: dict[str, tuple[str, ...]] = {
    'greeting': (
        'hi', 'hello', 'hey', 'hiya', 'yo', 'good morning', 'good afternoon', 'good evening',
        'გამარჯობა', 'გამარჯობათ', 'სალამი', 'მოგესალმებით', 'დილა მშვიდობისა',
        'საღამო მშვიდობისა', 'gamarjoba', 'gamarjobat', 'salami', 'gaumarjos',
    ),
    'thanks': (
        'thanks', 'thank you', 'thx', 'ty', 'cheers', 'thanks a lot', 'many thanks',
        'მადლობა', 'მადლობთ', 'გმადლობთ', 'გაიხარე', 'გაიხარეთ', 'მადლობელი ვარ',
        'madloba', 'gmadlobt', 'gaixare', 'mersi', 'merci',
    ),
    'bye': (
        'bye', 'goodbye', 'bye bye', 'see you', 'see ya', 'later',
        'ნახვამდის', 'კარგად', 'დროებით', 'ჯერჯერობით', 'nakhvamdis', 'naxvamdis', 'kargad',
    ),
    'ack': (
        'ok', 'okay', 'k', 'cool', 'great', 'nice', 'got it', 'understood', 'alright', 'fine',
        'perfect', 'good', 'awesome', 'კარგი', 'კაი', 'გასაგებია', 'გავიგე', 'კარგით', 'ოკ',
        'სუპერ', 'მაგარია', 'ნორმალურია', 'შესანიშნავია', 'kargi', 'kai', 'gasagebia', 'super',
    ),
    'yes': (
        'yes', 'yeah', 'yep', 'yup', 'sure', 'yes please', 'please do', 'go ahead', 'of course',
        'კი', 'დიახ', 'ჰო', 'ხო', 'რა თქმა უნდა', 'ki', 'diax', 'ho', 'xo',
    ),
    'no': (
        'no', 'nope', 'nah', 'not now', 'no need', 'არა', 'არ მინდა', 'არ მჭირდება', 'ara',
    ),
    'identity': (
        'who are you', 'what are you', 'are you a bot', 'are you a robot', 'are you human',
        'are you real', 'are you ai', 'is this a bot', 'ვინ ხარ', 'ვინ ხართ', 'ბოტი ხარ',
        'რობოტი ხარ', 'ადამიანი ხარ', 'ბოტი ხართ',
    ),
    'help': (
        'help', 'what can you do', 'how does this work', 'what do you do', 'how do i use this',
        'დახმარება', 'რა შეგიძლია', 'რა შეგიძლიათ', 'როგორ მუშაობ', 'რას აკეთებ',
    ),
}
FILLER = ('there', 'again', 'so much', 'very much', 'a lot', 'man', 'bro', 'friend', 'guys',
          'dear', 'please', 'all', 'then', 'for now', 'for that', 'ძმაო', 'ძალიან', 'დიდი',
          'უღრმესი', 'თქვენ', 'შენ', 'ყველას', 'ჯერ', 'ბატონო', 'ქალბატონო')
PRIORITY = ('identity', 'help', 'thanks', 'bye', 'greeting', 'no', 'yes', 'ack')
_KIND = {phrase: kind for kind, phrases in SMALL_TALK.items() for phrase in phrases}
_ALL = sorted(list(_KIND) + list(FILLER), key=len, reverse=True)
_PHRASE = '|'.join(re.escape(p).replace(r'\ ', r'\s+') for p in _ALL)
_BOUNDARY = r'(?![0-9a-zა-ჰ_])'
_ONLY_SMALL_TALK = re.compile(rf'(?:(?:{_PHRASE}){_BOUNDARY}[\s,.!?;:)(\-]*)+')
_FIND = re.compile(rf'(?<![0-9a-zა-ჰ_])(?:{_PHRASE}){_BOUNDARY}')
_LEADING_GREETING = re.compile(
    rf"^(?:{'|'.join(re.escape(p) for p in SMALL_TALK['greeting'])}){_BOUNDARY}")

# Words that are Georgian written in Latin letters: the reply should be Georgian.
TRANSLIT = frozenset({
    'gamarjoba', 'gamarjobat', 'madloba', 'gmadlobt', 'kargi', 'kai', 'ki', 'ara', 'diax',
    'xundebi', 'xundi', 'zeti', 'zetis', 'filtri', 'filtrebi', 'diskebi', 'diski', 'saloni',
    'salonis', 'haeris', 'maqvs', 'gaqvt', 'gaqvs', 'minda', 'mchirdeba', 'manqana',
    'manqanis', 'tsina', 'wina', 'ukana', 'nakhvamdis', 'naxvamdis', 'gaixare', 'rogor',
})
AMBIGUOUS = frozenset({'ok', 'okay', 'k', 'vin', 'id', 'tuz', 'oe', 'oem'})

MAKES: dict[str, tuple[str, ...]] = {
    'TOYOTA': ('toyota', 'ტოიოტ'), 'LEXUS': ('lexus', 'ლექსუს'), 'HONDA': ('honda', 'ჰონდ'),
    'NISSAN': ('nissan', 'ნისან'), 'MAZDA': ('mazda', 'მაზდ'), 'SUBARU': ('subaru', 'სუბარუ'),
    'MITSUBISHI': ('mitsubishi', 'მიცუბიშ'), 'SUZUKI': ('suzuki', 'სუზუკ'),
    'HYUNDAI': ('hyundai', 'hundai', 'ჰიუნდა', 'ჰუნდა', 'ჰიუნდე'), 'KIA': ('kia', 'კია'),
    'MERCEDES-BENZ': ('mercedes benz', 'mercedes-benz', 'mercedes', 'benz', 'mb', 'მერსედეს',
                      'ბენც'),
    'BMW': ('bmw', 'ბმვ', 'ბეემვე'), 'AUDI': ('audi', 'აუდ'),
    'VOLKSWAGEN': ('volkswagen', 'vw', 'ფოლკსვაგენ', 'ფოლცვაგენ', 'ფოლქსვაგენ'),
    'OPEL': ('opel', 'ოპელ'), 'FORD': ('ford', 'ფორდ'),
    'CHEVROLET': ('chevrolet', 'chevy', 'შევროლე'), 'PORSCHE': ('porsche', 'პორშე'),
    'VOLVO': ('volvo', 'ვოლვო'), 'JEEP': ('jeep',), 'DODGE': ('dodge', 'დოჯ'),
    'CHRYSLER': ('chrysler',), 'RENAULT': ('renault', 'რენო'), 'PEUGEOT': ('peugeot', 'პეჟო'),
    'CITROEN': ('citroen', 'სიტროენ'), 'SKODA': ('skoda', 'შკოდ'), 'FIAT': ('fiat', 'ფიატ'),
    'INFINITI': ('infiniti', 'ინფინიტ'), 'ACURA': ('acura', 'აკურ'), 'TESLA': ('tesla', 'ტესლ'),
    'JAGUAR': ('jaguar', 'იაგუარ'), 'LAND ROVER': ('land rover', 'range rover', 'landrover'),
    'CADILLAC': ('cadillac',), 'LINCOLN': ('lincoln',), 'BUICK': ('buick',), 'GMC': ('gmc',),
    'SSANGYONG': ('ssangyong',), 'DAEWOO': ('daewoo', 'დეუ'), 'LADA': ('lada', 'ლადა'),
}
# Popular models, so "Prius 2012" or "კამრი" identify the make too.
MODELS: dict[str, tuple[str, str]] = {
    'camry': ('TOYOTA', 'Camry'), 'corolla': ('TOYOTA', 'Corolla'), 'prius': ('TOYOTA', 'Prius'),
    'aqua': ('TOYOTA', 'Aqua'), 'rav4': ('TOYOTA', 'RAV4'), 'yaris': ('TOYOTA', 'Yaris'),
    'auris': ('TOYOTA', 'Auris'), 'highlander': ('TOYOTA', 'Highlander'),
    'accord': ('HONDA', 'Accord'), 'civic': ('HONDA', 'Civic'), 'fit': ('HONDA', 'Fit'),
    'cr-v': ('HONDA', 'CR-V'), 'crv': ('HONDA', 'CR-V'), 'insight': ('HONDA', 'Insight'),
    'odyssey': ('HONDA', 'Odyssey'), 'altima': ('NISSAN', 'Altima'),
    'sentra': ('NISSAN', 'Sentra'), 'rogue': ('NISSAN', 'Rogue'), 'juke': ('NISSAN', 'Juke'),
    'tiida': ('NISSAN', 'Tiida'), 'x-trail': ('NISSAN', 'X-Trail'), 'murano': ('NISSAN', 'Murano'),
    'elantra': ('HYUNDAI', 'Elantra'), 'sonata': ('HYUNDAI', 'Sonata'),
    'tucson': ('HYUNDAI', 'Tucson'), 'accent': ('HYUNDAI', 'Accent'),
    'optima': ('KIA', 'Optima'), 'sportage': ('KIA', 'Sportage'), 'sorento': ('KIA', 'Sorento'),
    'cerato': ('KIA', 'Cerato'), 'forester': ('SUBARU', 'Forester'),
    'outback': ('SUBARU', 'Outback'), 'impreza': ('SUBARU', 'Impreza'),
    'outlander': ('MITSUBISHI', 'Outlander'), 'lancer': ('MITSUBISHI', 'Lancer'),
    'pajero': ('MITSUBISHI', 'Pajero'), 'golf': ('VOLKSWAGEN', 'Golf'),
    'passat': ('VOLKSWAGEN', 'Passat'), 'jetta': ('VOLKSWAGEN', 'Jetta'),
    'tiguan': ('VOLKSWAGEN', 'Tiguan'), 'touareg': ('VOLKSWAGEN', 'Touareg'),
    'focus': ('FORD', 'Focus'), 'fusion': ('FORD', 'Fusion'), 'escape': ('FORD', 'Escape'),
    'cruze': ('CHEVROLET', 'Cruze'), 'malibu': ('CHEVROLET', 'Malibu'),
    'astra': ('OPEL', 'Astra'), 'vectra': ('OPEL', 'Vectra'), 'zafira': ('OPEL', 'Zafira'),
    'mazda2': ('MAZDA', '2'), 'mazda3': ('MAZDA', '3'), 'mazda5': ('MAZDA', '5'),
    'mazda6': ('MAZDA', '6'), 'cx-3': ('MAZDA', 'CX-3'), 'cx-30': ('MAZDA', 'CX-30'),
    'cx-5': ('MAZDA', 'CX-5'), 'cx5': ('MAZDA', 'CX-5'), 'cx-7': ('MAZDA', 'CX-7'),
    'cx-9': ('MAZDA', 'CX-9'), 'mx-5': ('MAZDA', 'MX-5'),
}
# Two-word models; a make alias such as 'range rover' is also the start of the model.
TWO_WORD_MODELS: dict[str, tuple[str, str]] = {
    'land cruiser': ('TOYOTA', 'Land Cruiser'), 'santa fe': ('HYUNDAI', 'Santa Fe'),
    'grand cherokee': ('JEEP', 'Grand Cherokee'), 'grand vitara': ('SUZUKI', 'Grand Vitara'),
    'range rover': ('LAND ROVER', 'Range Rover'),
}
MODELS_KA: dict[str, str] = {
    'კამრ': 'camry', 'კოროლ': 'corolla', 'პრიუს': 'prius', 'აქვა': 'aqua', 'აკვა': 'aqua',
    'რავ4': 'rav4', 'იარის': 'yaris', 'აკორდ': 'accord', 'სივიკ': 'civic', 'ფიტ': 'fit',
    'ინსაით': 'insight', 'ელანტრ': 'elantra', 'სონატ': 'sonata', 'ტუსან': 'tucson',
    'ოპტიმ': 'optima', 'სპორტეიჯ': 'sportage', 'სორენტ': 'sorento', 'ფორესტერ': 'forester',
    'აუთბექ': 'outback', 'ჯეტა': 'jetta', 'გოლფ': 'golf', 'პასატ': 'passat', 'ტიგუან': 'tiguan',
    'აუთლენდერ': 'outlander', 'ფოკუს': 'focus', 'ფიუჟენ': 'fusion', 'კრუზ': 'cruze',
}
GEORGIAN_ENDINGS = frozenset({
    '', 'ი', 'ის', 'ს', 'ით', 'ისთვის', 'სთვის', 'ზე', 'ში', 'მა', 'ა', 'ო', 'ად', 'ები', 'ების',
    'ა-ს', 'ას', 'ა-ზე', 'ს-ის', 'ისზე', 'ისში', 'ისით', 'ისგან', 'ისთან', 'ია', 'ზეც',
})
# Class letters written in Georgian: 'ე კლასი' is the E-Class, 'მლ' the ML.
GEORGIAN_LETTERS = {'ა': 'a', 'ბ': 'b', 'ც': 'c', 'ე': 'e', 'გ': 'g', 'ს': 's', 'ვ': 'v',
                    'რ': 'r', 'ლ': 'l', 'მ': 'm', 'კ': 'k'}
CLASS_WORD = r'(?:class|კლას[ა-ჰ]*)'
# Body and fuel words, English and Georgian stems; values as a VIN decoder spells them.
BODIES = {
    'Coupe': ('coupe', 'coupé', 'კუპე'),
    'Convertible': ('convertible', 'cabrio', 'cabriolet', 'კაბრიოლეტ', 'კაბრიო'),
    'Wagon': ('wagon', 'estate', 'touring', 'უნივერსალ'),
    'Sedan': ('sedan', 'saloon', 'სედან'),
    'Hatchback': ('hatchback', 'hatch', 'ჰეჩბექ'),
}
FUELS = {
    'Gasoline': re.compile(r'(?<![a-z])(?:petrol|gasoline|benzin[a-z]*)(?![a-z])|ბენზინ[ა-ჰ]*'),
    'Diesel': re.compile(r'(?<![a-z])diesel(?![a-z])|დიზელ[ა-ჰ]*'),
}
ENGINE_WORD = re.compile(r'ძრავ|მოტორ|(?<![a-z])(?:engine|motor)(?![a-z])')
# Ordinary English words that are also model names; they need a make or a year.
AMBIGUOUS_MODELS = frozenset({'fit', 'focus', 'escape', 'fusion', 'accent', 'rogue', 'insight',
                              'golf', 'juke', 'astra'})
MODEL_STOP = frozenset({
    'for', 'my', 'the', 'a', 'an', 'car', 'model', 'year', 'and', 'with', 'of', 'is', 'it',
    'need', 'i', 'please', 'from', 'to', 'in', 'on', 'do', 'you', 'have', 'any', 'some',
    'ჩემი', 'ჩემს', 'წლის', 'წელი', 'მოდელი', 'მანქანა', 'მანქანის', 'და', 'გაქვთ', 'მჭირდება',
})
# Words around a VIN or a typed car that do not ask for anything ('here is my VIN').
VEHICLE_FILLER = MODEL_STOP | frozenset({
    'vin', 'here', 'this', 'one', 'its', "it's", 'got', 'that', 'hi', 'hello', 'hey', 'so',
    'and', 'what', 'about', 'how', 'აი', 'ეს', 'არის', 'მაქვს', 'ჩემია', 'გამარჯობა', 'სალამი',
    'ვინ', 'ვინი', 'ვინკოდი', 'კოდი', 'ჩემ',
})
ORDINAL_ENDINGS = frozenset({'', 'ი', 'ის', 'ს', 'ზე'})
ORDINALS = {
    'first': 0, '1st': 0, 'second': 1, '2nd': 1, 'third': 2, '3rd': 2, 'fourth': 3, '4th': 3,
    'fifth': 4, '5th': 4, 'last': -1, 'პირველ': 0, 'მეორე': 1, 'მესამე': 2, 'მეოთხე': 3,
    'მეხუთე': 4, 'ბოლო': -1,
}
YEAR = re.compile(r'(?<![\d.])(19[89]\d|20[0-3]\d)(?![\d.])')
VIN = re.compile(r'(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])', re.IGNORECASE)
TOKEN = re.compile(r"[A-Za-zა-ჰ0-9][A-Za-zა-ჰ0-9\-]*")
GRADE = re.compile(r'(?<![A-Za-z0-9])(\d{1,2})\s?W\s?-?\s?(\d{2})(?![0-9])', re.IGNORECASE)
# Designations typed instead of the model: `E400`, `RX 350`, `G37`. Per make: the letters
# (longest first), the number, and the model the choosers list them under.
LETTERED: dict[str, tuple[str, str, Callable[[str], str]]] = {
    'MERCEDES-BENZ': (r'(cla|cls|clk|gla|glb|glc|gle|gls|slk|slc|sl|gl|ml|cl|a|b|c|e|g|r|s|v)',
                      r'\d{2,3}', lambda letters: {'ml': 'M-Class'}.get(
                          letters, f'{letters.upper()}-Class')),
    'LEXUS': (r'(is|es|gs|ls|rx|nx|ux|lx|gx|rc|lc|ct|hs|sc)', r'\d{3}', str.upper),
    'INFINITI': (r'(fx|ex|jx|g|m)', r'\d{2}', str.upper),
}
# BMW `328i`, `520d`, `M340i`: the first digit is the series.
BMW_DESIGNATION = re.compile(r'(m?)([1-8])(\d{2})([a-z]{0,2})')
SERIES_NAME = re.compile(r'([1-8])-?(?:series|er)')
# An engine size given in reply to the engine question ('3.0', '3.0L', '2,0 ლიტრი').
DISPLACEMENT = re.compile(r'(?<![\d.,])([1-8][.,]\d)\s*(?:l|ltr|lt|ლ|ლიტრ[ა-ჰ]*)?(?![\d.,A-Za-z])',
                          re.IGNORECASE)


def _clean(text: str | None) -> str:
    return unicodedata.normalize('NFKC', text or '').casefold().strip()


def small_talk(text: str) -> str | None:
    """The kind of a message that is only small talk ('ok thanks', 'გამარჯობათ'), else None."""
    value = re.sub(r"[^\w\sა-ჰ'’?!.,;:()-]", ' ', _clean(text))
    value = re.sub(r'\s+', ' ', value).strip(' .!?,;:-')
    if not value or len(value) > 80 or not _ONLY_SMALL_TALK.fullmatch(value + ' '):
        return None
    kinds = {_KIND[found] for found in (re.sub(r'\s+', ' ', m.group(0))
                                        for m in _FIND.finditer(value)) if found in _KIND}
    return next((kind for kind in PRIORITY if kind in kinds), None)


def starts_with_greeting(text: str) -> bool:
    return bool(_LEADING_GREETING.match(_clean(text)))


def detect_language(text: str, previous: str | None = None) -> str:
    """ka or en from what the customer typed; codes, VINs and 'ok' keep the previous one."""
    value = VIN.sub(' ', text or '')
    words = [w for w in TOKEN.findall(value) if not any(ch.isdigit() for ch in w)]
    if any(re.search(r'[ა-ჰ]', w) for w in words):
        return 'ka'
    latin = [w.casefold() for w in words if len(w) >= 2 and w.casefold() not in AMBIGUOUS]
    translit = sum(w in TRANSLIT for w in latin)
    if translit and translit * 2 >= len(latin):
        return 'ka'
    if latin:
        return 'en'
    return previous or 'ka'


@dataclass(frozen=True)
class TypedVehicle:
    """What the customer typed about their car; empty fields were not stated."""

    make: str | None = None
    model: str | None = None
    model_year: int | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)
    trim: str | None = None
    body: str | None = None
    fuel: str | None = None
    displacement_l: str | None = None

    def as_vehicle(self) -> dict:
        return {k: v for k, v in (
            ('make', self.make), ('model', self.model), ('model_year', self.model_year),
            ('trim', self.trim), ('body', self.body), ('fuel', self.fuel),
            ('displacement_l', self.displacement_l)) if v}


def continues(text: str) -> bool:
    """'And for this one?', 'what about…', 'იგივე ამისთვის': the same request, another car."""
    return bool(re.match(r"\s*(and|also|what about|how about|same|და|ამისთვის|ამ მანქანისთვის"
                         r"|ამასაც|ესეც|იგივე)(?![a-zა-ჰ])", _clean(text)))


def _georgian_form(word: str, stem: str) -> bool:
    """A Georgian stem with an ordinary case ending: 'პრიუსის', not 'ფიტინგი' for 'ფიტ'."""
    return word.startswith(stem) and word[len(stem):] in GEORGIAN_ENDINGS


def _make_at(tokens: list[str], index: int) -> tuple[str, int] | None:
    """Canonical make starting at tokens[index], and how many tokens it uses."""
    word = tokens[index]
    pair = f'{word} {tokens[index + 1]}' if index + 1 < len(tokens) else ''
    for make, aliases in MAKES.items():
        for alias in aliases:
            if ' ' in alias:
                if pair == alias:
                    return make, 2
            elif re.search(r'[ა-ჰ]', alias):
                if _georgian_form(word, alias):
                    return make, 1
            elif word == alias:
                return make, 1
    return None


def _known_model(word: str) -> tuple[str, str] | None:
    if word in MODELS:
        return MODELS[word]
    for stem, key in MODELS_KA.items():
        if _georgian_form(word, stem):
            return MODELS[key]
    return None


def _model_word(word: str) -> bool:
    if word in MODEL_STOP or YEAR.fullmatch(word) or re.search(r'[ა-ჰ]', word):
        return False
    # "Toyota 04465-33450" is a make followed by a part number, not a model.
    if sum(ch.isdigit() for ch in word) >= 4:
        return False
    return category_for_query(word) is None and len(word) <= 20


def _display(word: str) -> str:
    if any(ch.isdigit() for ch in word) or len(word) <= 3:
        return word.upper()
    return word.capitalize() if word.islower() else word


def _designation_at(make: str, tokens: list[str],
                    index: int) -> tuple[str, str | None, int] | None:
    """A designation typed as the model -> (model, trim, tokens used).

    Choosers list `E 400` under `E-CLASS`, `328 i` under `3 series` and `RX 350` under `RX`,
    so the designation is the trim. `E-Class E400` and `3 series 328i` are read too.
    """
    if index >= len(tokens):
        return None
    if make == 'BMW':
        return _bmw_at(tokens, index)
    if make not in LETTERED:
        return None
    letters_pattern, digits_pattern, model_of = LETTERED[make]
    named, start = _class_at(letters_pattern, tokens, index)
    word = _latin(tokens[start]) if start < len(tokens) else ''
    after = tokens[start + 1] if start + 1 < len(tokens) else ''
    found = re.fullmatch(f'{letters_pattern}({digits_pattern})([a-z]{{0,2}})', word)
    number = (re.fullmatch(f'({digits_pattern})([a-z]{{0,2}})', after)
              if re.fullmatch(letters_pattern, word) else None)
    if found:
        (letters, digits, suffix), width = found.groups(), 1
    elif number:
        letters, (digits, suffix), width = word, number.groups(), 2
    elif named:
        return model_of(named), None, start - index  # 'ე კლასზე': the class, no engine yet.
    else:
        return None
    if named and named != letters:
        return None
    return model_of(letters), f'{letters.upper()} {digits}{suffix}', start - index + width


def _latin(word: str) -> str:
    """'ე400' -> 'e400'; a word with other Georgian letters stays as it is."""
    converted = ''.join(GEORGIAN_LETTERS.get(ch, ch) for ch in word)
    return word if re.search(r'[ა-ჰ]', converted) else converted


def _class_at(letters_pattern: str, tokens: list[str], index: int) -> tuple[str | None, int]:
    """`E-Class`, `e class`, `ე კლასზე` -> ('e', index after it); otherwise (None, index)."""
    joined = re.fullmatch(f'([a-zა-ჰ]{{1,3}}?)-?{CLASS_WORD}', tokens[index])
    if joined:
        letters, width = _latin(joined.group(1)), 1
    elif index + 1 < len(tokens) and re.fullmatch(CLASS_WORD, tokens[index + 1]):
        letters, width = _latin(tokens[index]), 2
    else:
        return None, index
    return (letters, index + width) if re.fullmatch(letters_pattern, letters) else (None, index)


def _bmw_at(tokens: list[str], index: int) -> tuple[str, str, int] | None:
    """`328i`, `3 series 328i` or `3er 328i` -> ('3 Series', '328i', tokens used)."""
    named = SERIES_NAME.fullmatch(tokens[index])
    start = index + 1 if named else index
    if (not named and index + 1 < len(tokens) and re.fullmatch(r'[1-8]', tokens[index])
            and tokens[index + 1] == 'series'):
        named, start = re.fullmatch(r'([1-8])', tokens[index]), index + 2
    found = BMW_DESIGNATION.fullmatch(tokens[start]) if start < len(tokens) else None
    if not found or (named and named.group(1) != found.group(2)):
        return None
    sport, series, rest, suffix = found.groups()
    return f'{series} Series', f'{sport.upper()}{series}{rest}{suffix}', start - index + 1


def _two_word_model(tokens: list[str], index: int) -> tuple[str, str] | None:
    return TWO_WORD_MODELS.get(' '.join(tokens[index:index + 2])) if index + 1 < len(
        tokens) else None


def read_vehicle(text: str) -> TypedVehicle:
    """Make, model and year typed in a message ('Honda Accord 2015', 'პრიუსი 2012')."""
    original = VIN.sub(' ', unicodedata.normalize('NFKC', text or ''))
    raw = TOKEN.findall(original)
    tokens = [t.casefold() for t in raw]
    used: set[str] = set()
    year_match = YEAR.search(original)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        used.add(year_match.group(1))
    make = model = trim = None
    for index in range(len(tokens)):
        found = _make_at(tokens, index)
        if not found:
            continue
        make, width = found
        used.update(raw[index:index + width])
        start = index + width
        # 'Range Rover' names the make and starts the model.
        prefix = (_two_word_model(tokens, index) or (None, None))[1] if width == 2 else None
        # "Mercedes Benz": a second name of the same make is not the model.
        while start < len(tokens) and (again := _make_at(tokens, start)) and again[0] == make:
            if again[1] == 2:
                prefix = (_two_word_model(tokens, start) or (None, prefix))[1]
            used.update(raw[start:start + again[1]])
            start += again[1]
        if prefix is None and (named := _two_word_model(tokens, start)) and named[0] == make:
            prefix = named[1]
            used.update(raw[start:start + 2])
            start += 2
        if prefix:
            # 'Range Rover Sport', 'Land Cruiser'.
            word = tokens[start] if start < len(tokens) else ''
            extra = word.isalpha() and _model_word(word) and prefix == 'Range Rover'
            model = f'{prefix} {_display(raw[start])}' if extra else prefix
            if extra:
                used.add(raw[start])
            break
        designation = _designation_at(make, tokens, start)
        if designation:
            model, trim, width = designation
            used.update(raw[start:start + width])
            break
        following: list[int] = []
        for offset in range(start, min(start + 3, len(tokens))):
            word = tokens[offset]
            known = _known_model(word)
            if known and known[0] == make:
                model = known[1]
                used.add(raw[offset])
                break
            if not _model_word(word):
                break
            # A second token only completes a model such as `E 400`, `3 series` or `C 200`.
            if following and not ((word.isdigit() and len(word) <= 3)
                                  or word in {'class', 'series'}):
                break
            following.append(offset)
            if len(following) == 2:
                break
        if model is None and following:
            model = ' '.join(_display(tokens[i]) if tokens[i] == raw[i] else raw[i]
                             for i in following)
            used.update(raw[i] for i in following)
        break
    if model is None:
        for position in range(len(tokens)):
            named = _two_word_model(tokens, position)
            if named and (make is None or named[0] == make):
                make, model = named
                used.update(raw[position:position + 2])
                break
    if model is None:
        for position, word in enumerate(tokens):
            known = _known_model(word)
            if word in AMBIGUOUS_MODELS and not (make or year):
                continue  # "Will it fit?" is not a Honda Fit.
            if known and (make is None or known[0] == make):
                make, model = known
                used.add(raw[position])
                break
    if make is None:
        return TypedVehicle(make, model, year, frozenset(used), trim)
    body = next((name for name, stems in BODIES.items() for word in tokens
                 if any(_georgian_form(word, stem) if re.search(r'[ა-ჰ]', stem) else word == stem
                        for stem in stems)), None)
    return TypedVehicle(make, model, year, frozenset(used), trim, body, fuel_named(text),
                        _stated_size(text))


def fuel_named(text: str) -> str | None:
    """'ბენზინზე', 'diesel' -> the fuel; both or neither -> None."""
    value = _clean(text)
    named = [fuel for fuel, pattern in FUELS.items() if pattern.search(value)]
    return named[0] if len(named) == 1 else None


def _stated_size(text: str) -> str | None:
    """'3.0 ძრავზე', '2.0 engine': one engine size next to the word engine, not '4L of oil'."""
    value = unicodedata.normalize('NFKC', text or '')
    found = DISPLACEMENT.findall(value)
    if len(found) != 1 or not ENGINE_WORD.search(value.casefold()):
        return None
    return found[0].replace(',', '.')


def year_answer(text: str) -> int | None:
    """A bare year reply ('2015', 'it's a 2015', '2015 წლის')."""
    value = _clean(text)
    found = YEAR.findall(value)
    rest = YEAR.sub(' ', value)
    words = [w for w in TOKEN.findall(rest) if w not in MODEL_STOP and w not in {
        's', 'its', 'წლისაა', 'წელია', 'წლისა'}]
    return int(found[0]) if len(found) == 1 and not words else None


def engine_answer(text: str) -> str | None:
    """A bare engine code given in reply to the engine question ('K24W', 'M276')."""
    match = re.fullmatch(r"\s*(?:it'?s\s+|engine\s+|ძრავი\s+)?"
                         r"([A-Za-z]{1,3} ?\d[A-Za-z0-9.\-]{0,12}|[A-Za-z0-9][A-Za-z0-9.\-]{1,14})"
                         r"[\s.!]*", unicodedata.normalize('NFKC', text or ''), re.IGNORECASE)
    if not match:
        return None
    code = match.group(1).strip()
    if (not (re.search(r'\d', code) and re.search(r'[A-Za-z]', code)) or YEAR.fullmatch(code)
            or DISPLACEMENT.fullmatch(code)):
        return None
    return code


def engine_details(text: str, make: str | None, options: list | tuple = ()) -> dict:
    """What a reply to the engine question states: an offered engine ('E 400 (ბენზინი)'),
    a designation, the fuel, the engine size, or an engine code."""
    found: dict = {}
    fuel = fuel_named(text)
    picked = pick_option(text, options, fuel)
    if picked:
        found['trim'] = picked['trim']
        fuel = picked.get('fuel') or fuel
    else:
        tokens = [t.casefold() for t in TOKEN.findall(VIN.sub(' ', text or ''))]
        designation = next((d for i in range(len(tokens)) if (d := _designation_at(
            make or '', tokens, i)) and d[1]), None)
        if designation:
            found['trim'] = designation[1]
        size = displacement_answer(text)
        if size:
            found['displacement_l'] = size
    if fuel:
        found['fuel'] = fuel
    if not found:
        code = engine_answer(text)
        if code:
            found['engine_code'] = code
    return found


def pick_option(text: str, options: list | tuple, fuel: str | None = None) -> dict | None:
    """The offered engine a reply names; the fuel word only separates same-named engines."""
    valid = [o for o in options if isinstance(o, dict) and isinstance(o.get('trim'), str)]
    value = re.sub(r'\([^)]*\)', ' ', _clean(text))
    for pattern in FUELS.values():
        value = pattern.sub(' ', value)
    key = re.sub(r'[^a-z0-9]+', '', value)
    hits = [o for o in valid if key and re.sub(r'[^a-z0-9]+', '', o['trim'].casefold()) == key]
    if not hits and not key and fuel:
        hits = [o for o in valid if o.get('fuel') == fuel]
    if fuel:
        hits = [o for o in hits if o.get('fuel') in (fuel, None)] or hits
    return hits[0] if len({o['trim'].casefold() for o in hits}) == 1 else None


def displacement_answer(text: str) -> str | None:
    """An engine size given in reply to the engine question: '3.0 twin turbo' -> '3.0'."""
    value = unicodedata.normalize('NFKC', text or '')
    found = DISPLACEMENT.findall(value)
    if len(value) > 80 or len(found) != 1 or YEAR.search(value):
        return None
    return found[0].replace(',', '.')


def part_codes(text: str, ignore: tuple[str, ...] = ()) -> list[str]:
    """Part numbers the customer typed: not years, oil grades, quantities or car models."""
    from automotive.codes import normalize_code, parse_codes

    vehicle = {t.casefold() for t in read_vehicle(text).tokens}
    skip = {normalize_code(value) for value in ignore if value}
    found = []
    for code in parse_codes(VIN.sub(' ', text or '')):
        token = code.original.strip('!?.,:;"\'()[]')
        normalized = normalize_code(token)
        if (not normalized or YEAR.fullmatch(normalized) or token.casefold() in vehicle
                or re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', normalized)
                or normalized in skip or GRADE.fullmatch(token)
                or (normalized.isdigit() and len(normalized) < 4)
                or re.fullmatch(r'\d+(?:[.,]\d+)?(?:l|ltr|lt|ah|w|v|mm|x|pcs|ც)', token,
                                re.IGNORECASE)):
            continue
        found.append(token)
    return list(dict.fromkeys(found))


CODE_FILLER = frozenset({
    'do', 'you', 'have', 'has', 'any', 'find', 'need', 'i', 'me', 'a', 'an', 'the', 'is', 'it',
    'in', 'stock', 'available', 'got', 'for', 'my', 'price', 'how', 'much', 'part', 'parts',
    'number', 'code', 'codes', 'please', 'search', 'look', 'up', 'check', 'there', 'this',
    'that', 'want', 'looking', 'can', 'get', 'we', 'what', 'about', 'and', 'or', 'oem', 'oe',
    'original', 'genuine', 'still', 'one', 'ones', 'front', 'rear', 'filter', 'filters',
    'engine', 'these', 'those', 'them', 'sell', 'carry', 'order', 'buy', 'hi', 'hello', 'hey',
    'thanks', 'გაქვთ', 'გაქვს', 'მაქვს', 'მინდა', 'მჭირდება', 'მოძებნე', 'მოძებნეთ',
    'მარაგში', 'მარაგშია', 'არის', 'კოდი', 'კოდით', 'ნაწილი', 'ეს', 'ხომ', 'თუ', 'რა', 'ღირს',
    'მაინტერესებს', 'და', 'ან', 'წინა', 'უკანა', 'ფილტრი', 'ძრავის', 'გვაქვს', 'ამ', 'ამის',
    'მომიძებნე', 'შეგიძლიათ', 'შეიძლება', 'ვეძებ', 'გამარჯობა', 'სალამი',
})


def code_question(text: str, codes: list[str]) -> bool:
    """Only part numbers plus filler ('do you have AP104/2?'): a direct catalog lookup.

    Anything more ('does it fit…', 'cross reference for…') is left to the read tools."""
    vehicle = {t.casefold() for t in read_vehicle(text).tokens}
    for word in TOKEN.findall(VIN.sub(' ', _clean(text))):
        if (word in CODE_FILLER or word in vehicle or any(ch.isdigit() for ch in word)
                or category_for_query(word) or any(c.casefold().startswith(word) for c in codes)):
            continue
        return False
    return True


def grades(text: str) -> list[str]:
    """SAE grades the customer typed, normalized ('5w40' is '5W-40')."""
    return list(dict.fromkeys(f'{a}W-{b}' for a, b in GRADE.findall(text or '')))


def references(text: str, cards: list[dict]) -> list[str]:
    """IDs of previously shown cards the customer points at ('the Bosch one', 'მეორე')."""
    if not cards:
        return []
    value = _clean(text)
    tokens = TOKEN.findall(value)
    picked: list[str] = []
    for position, token in enumerate(tokens):
        following = tokens[position + 1] if position + 1 < len(tokens) else ''
        if following.startswith(('car', 'მანქან', 'ავტო')):
            continue  # "for the second car" is about a car, not a shown part.
        for word, index in ORDINALS.items():
            ending = token[len(word):] if token.startswith(word) else None
            if ending is None or (ending not in ORDINAL_ENDINGS if not word.isascii() else ending):
                continue
            if word == 'ბოლო' and ending == 'ს':
                continue  # "ბოლოს" usually means "finally".
            if -len(cards) <= index < len(cards):
                picked.append(cards[index]['id'])
    for card in cards:
        brand = _clean(card.get('brand'))
        if len(brand) >= 2 and re.search(
                rf'(?<![0-9a-zა-ჰ]){re.escape(brand)}(?![0-9a-z])', value):
            picked.append(card['id'])
    return list(dict.fromkeys(picked))
