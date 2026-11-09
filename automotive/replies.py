"""Customer-facing reply sentences built only from the verified answer.

Replies read like a shop assistant texting: they name the car, say how many parts were
found and where they are, ask at most one question, and fold warnings into one short
note. Every number, code, brand and car name comes from the cards, the decoded or typed
car, the requirements or the customer's own message.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from automotive.categories import category_for_query
from automotive.rules import requested_axle

NOUNS = {
    'brake_pads': ('brake pad set', 'brake pads', 'სამუხრუჭე ხუნდი'),
    'brake_discs': ('brake disc', 'brake discs', 'სამუხრუჭე დისკი'),
    'brakes': ('brake part', 'brake parts', 'სამუხრუჭე ნაწილი'),
    'oil_filter': ('oil filter', 'oil filters', 'ზეთის ფილტრი'),
    'air_filter': ('air filter', 'air filters', 'ჰაერის ფილტრი'),
    'cabin_filter': ('cabin filter', 'cabin filters', 'სალონის ფილტრი'),
    'fuel_filter': ('fuel filter', 'fuel filters', 'საწვავის ფილტრი'),
    'engine_oil': ('engine oil', 'engine oils', 'ძრავის ზეთი'),
    'oil_change': ('item for the oil change', 'items for the oil change',
                   'ზეთის შესაცვლელი პროდუქტი'),
    'suspension': ('shock absorber', 'shock absorbers', 'ამორტიზატორი'),
    'spark_plugs': ('spark plug', 'spark plugs', 'ანთების სანთელი'),
    'battery': ('battery', 'batteries', 'აკუმულატორი'),
    'wipers': ('wiper blade', 'wiper blades', 'მინის საწმენდი'),
    'timing_belt': ('timing belt part', 'timing belt parts', 'ტაიმინგის ღვედი'),
    'coolant': ('coolant', 'coolants', 'ანტიფრიზი'),
    'bulbs': ('bulb', 'bulbs', 'ნათურა'),
    'clutch': ('clutch part', 'clutch parts', 'გადაბმულობის ნაწილი'),
    None: ('part', 'parts', 'ნაწილი'),
}
AXLE_CATEGORIES = {'brake_pads', 'brake_discs'}
SHORT_MAKES = {'BMW', 'KIA', 'GMC', 'VW', 'MG'}
EXAMPLE_CAR = 'Toyota Camry 2015'
CHECK_FIT = ('გადაამოწმეთ, რომ მოერგება', 'please check the fit')
FAILURE_WARNINGS = {'SOURCE_UNAVAILABLE', 'FITMENT_UNAVAILABLE', 'PRODUCT_REFRESH_UNAVAILABLE',
                    'SEARCH_EXPIRED', 'TIMEOUT', 'DATA_UNAVAILABLE', 'PROVIDER_UNAVAILABLE',
                    'PROVIDER_RATE_LIMITED', 'TURN_DEADLINE'}
QUICK = {
    'clarify_axle': (['Front', 'Rear'], ['წინა', 'უკანა']),
    'clarify_brakes': (['Front pads', 'Rear pads', 'Front discs', 'Rear discs'],
                       ['წინა ხუნდები', 'უკანა ხუნდები', 'წინა დისკები', 'უკანა დისკები']),
    'vehicle_identified': (['Oil change', 'Front brake pads', 'Air filter', 'Cabin filter'],
                           ['ზეთის შეცვლა', 'წინა ხუნდები', 'ჰაერის ფილტრი', 'სალონის ფილტრი']),
}


@dataclass
class Reply:
    text: str
    note: str = ''
    quick_replies: list[str] = field(default_factory=list)
    offer: dict | None = None
    facts: dict = field(default_factory=dict)


@dataclass
class Situation:
    """Everything a reply may mention, already verified by the backend."""

    act: str
    language: str
    status: str = 'OK'
    vehicle: dict = field(default_factory=dict)
    category: str = ''
    cards: list[dict] = field(default_factory=list)
    requirements: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    grades: list[str] = field(default_factory=list)
    missing_codes: list[str] = field(default_factory=list)
    referenced: list[dict] = field(default_factory=list)
    missing_brand: str = ''
    sold_out: int = 0
    greeted: bool = False
    name_only: bool = False
    has_vin: bool = False
    previous: Sequence[str] = ()
    previous_notes: Sequence[str] = ()


def make_name(make: str | None) -> str:
    """'HONDA' reads as 'Honda'; 'BMW' stays."""
    value = str(make or '').strip()
    if not value or value.upper() in SHORT_MAKES or not value.isupper():
        return value
    return ' '.join('-'.join(p.capitalize() for p in word.split('-'))
                    for word in value.split(' '))


def brand_name(brand: str | None) -> str:
    value = str(brand or '').strip()
    return value.title() if value.isupper() and len(value) > 3 else value


def car_label(vehicle: dict, with_year: bool = True) -> str:
    """'2015 Honda Accord' from the vehicle fields that are known."""
    model = str(vehicle.get('model') or '').strip()
    trim = str(vehicle.get('trim') or '').strip()
    if trim and len(trim) <= 12 and trim.casefold() not in model.casefold():
        model = f'{model} {trim}'.strip()
    parts = [make_name(vehicle.get('make')), model]
    if with_year and vehicle.get('model_year'):
        parts.insert(0, str(vehicle['model_year']))
    return ' '.join(p for p in parts if p)


def _ka_car(vehicle: dict) -> str:
    """'2015 წლის Honda Accord'."""
    label = car_label(vehicle, with_year=False)
    year = vehicle.get('model_year')
    return f'{year} წლის {label}' if year and label else label


def _ka_for(vehicle: dict) -> str:
    return f'თქვენი {_ka_car(vehicle)}-ისთვის'


def _pick(options: Sequence[str], previous: Sequence[str]) -> str:
    """A phrasing the customer has not just read; rotates with the conversation."""
    seen = {p.strip()[:40] for p in previous if p}
    start = len(previous) % len(options)
    for offset in range(len(options)):
        option = options[(start + offset) % len(options)]
        if option.strip()[:40] not in seen:
            return option
    return options[start]


def _noun(category: str | None, count: int, language: str) -> str:
    singular, plural, georgian = NOUNS.get(category, NOUNS[None])
    if language == 'ka':
        return georgian
    return singular if count == 1 else plural


def _axle(category_text: str, language: str) -> str:
    side = requested_axle(category_text)
    if not side:
        return ''
    return {'front': ('წინა ', 'front '), 'rear': ('უკანა ', 'rear ')}[side][
        0 if language == 'ka' else 1]


def availability(cards: list[dict], language: str) -> str:
    """Where the offered items are: store now, TUZ 11 in 1-2 hours, or unconfirmed."""
    ka = language == 'ka'
    states = [c.get('availability_status') for c in cards]
    total, store = len(states), states.count('IN_STORE')
    tuz, unknown = states.count('WITHIN_1_2_HOURS'), states.count('UNKNOWN')
    gone = states.count('UNAVAILABLE')
    if not total:
        return ''
    if total == 1:
        if store:
            return 'ახლა მაღაზიაშია.' if ka else "It's in the store now."
        if tuz:
            return ('ჩვენს საწყობშია და მაღაზიაში 1–2 საათში იქნება.' if ka
                    else "It's at our warehouse and can be in the store in 1-2 hours.")
        if gone:
            return 'ახლა მარაგში არ არის.' if ka else "It's not in stock right now."
        return ('მარაგს ახლა ვერ ვადასტურებ — მაღაზიაში გადაგიმოწმებენ.' if ka
                else "I can't confirm stock right now — the store can check.")
    sentences = []
    both = total == 2
    if store == total:
        sentences.append(('ორივე' if both else 'ყველა') + ' ახლა მაღაზიაშია.' if ka else
                         ('Both are' if both else 'All of them are') + ' in the store now.')
    elif tuz == total:
        sentences.append(('ორივე' if both else 'ყველა') + ' ჩვენს საწყობშია და მაღაზიაში 1–2 '
                         'საათში იქნება.' if ka else
                         ('Both are' if both else "They're") + ' at our warehouse and can be in '
                         'the store in 1-2 hours.')
    elif store or tuz:
        if ka:
            parts = [f'{store} ახლა მაღაზიაშია'] if store else []
            if tuz:
                parts.append(f'{tuz} ჩვენი საწყობიდან 1–2 საათში ჩამოვა')
            sentences.append(', '.join(parts) + '.')
        else:
            parts = [f"{store} {'is' if store == 1 else 'are'} in the store now"] if store else []
            if tuz:
                parts.append(f'{tuz} can be here from our warehouse in 1-2 hours')
            sentence = ', and '.join(parts)
            sentences.append(sentence[0].upper() + sentence[1:] + '.')
    if unknown == total:
        sentences.append('მარაგს ახლა ვერ ვადასტურებ — მაღაზიაში გადაგიმოწმებენ.' if ka
                         else "I can't confirm stock right now — the store can check.")
    elif unknown:
        sentences.append(f'{unknown}-ის მარაგს ვერ ვადასტურებ.' if ka
                         else f"I can't confirm stock for {unknown} of them.")
    if gone and gone < total:
        sentences.append(f'{gone} ახლა მარაგში არ არის.' if ka
                         else f"{gone} {'is' if gone == 1 else 'are'} out of stock right now.")
    return ' '.join(sentences)


def note(s: Situation) -> str:
    """At most one short caveat, the most important one."""
    ka = s.language == 'ka'
    warnings = set(s.warnings)
    if 'USER_CONFIGURATION_MISMATCH' in warnings:
        return ('VIN სხვა მანქანას აჩვენებს, ვიდრე მითითებული იყო, ამიტომ VIN-ის მონაცემი '
                'გამოვიყენე.' if ka else
                'The VIN shows a different car than you mentioned, so I went with the VIN.')
    failed = any(w in FAILURE_WARNINGS or w.startswith('SOURCE_FAILED:') for w in warnings)
    if (failed or s.status == 'PARTIAL') and s.cards and s.status != 'TIMEOUT':
        return ('ყველა მომწოდებელს ახლა ვერ დავუკავშირდი, ამიტომ შეიძლება მეტი ვარიანტიც იყოს.'
                if ka else "I couldn't reach every supplier just now, so there may be more "
                           'options.')
    if 'STOCK_MAY_BE_STALE' in warnings and s.cards:
        return ('მარაგი დღის განმავლობაში იცვლება — მაღაზიაში დაგიდასტურებენ.' if ka
                else 'Stock can change quickly, so the store will confirm it.')
    if s.act == 'oil_resolved' and s.cards:
        return ('ზეთის დაშვებები (approval) დამოუკიდებლად შემოწმებული არ არის.' if ka
                else "The oil approvals on the label aren't independently checked.")
    if s.missing_codes and s.cards:
        codes = ', '.join(s.missing_codes)
        return (f'{codes} ჩვენს კატალოგში ვერ ვიპოვე.' if ka
                else f"I couldn't find {codes} in our catalog.")
    if 'VIN_NOT_PROVIDED' in warnings and s.cards and s.vehicle.get('model') and (
            s.name_only or s.act == 'results'):
        return ('უფრო ზუსტი შედეგისთვის გამომიგზავნეთ VIN.' if ka
                else 'Send the VIN for a more precise match.')
    return ''


def _offer(s: Situation, category: str | None) -> tuple[str, dict | None]:
    """One follow-up question with a defined meaning for 'yes'."""
    side = requested_axle(s.category)
    if s.act != 'results' or category not in AXLE_CATEGORIES or not side or not s.cards \
            or s.codes:
        return '', None
    other = 'rear' if side == 'front' else 'front'
    kind = 'brake pads' if category == 'brake_pads' else 'brake discs'
    if s.language == 'ka':
        question = 'უკანაც გჭირდებათ?' if other == 'rear' else 'წინაც გჭირდებათ?'
    else:
        question = f'Need the {other} ones too?'
    if any(p.rstrip().endswith(('ones too?', 'უკანაც გჭირდებათ?', 'წინაც გჭირდებათ?'))
           for p in s.previous[-1:]):
        return '', None  # Just asked; asking again every reply sounds scripted.
    return question, {'category': f'{other} {kind}'}


def _results(s: Situation, category: str | None) -> str:
    ka, count = s.language == 'ka', len(s.cards)
    noun = _axle(s.category, s.language) + _noun(category, count, s.language)
    stock = availability(s.cards, s.language)
    car = s.vehicle if s.vehicle.get('model') else {}
    if s.codes:
        code = ', '.join(s.codes)
        if count == 1:
            brand = brand_name(s.cards[0].get('product', {}).get('brand'))
            state = s.cards[0].get('availability_status')
            label = f'{code} ({brand})' if brand else code
            if state == 'UNAVAILABLE':
                return (f'{label} ჩვენს კატალოგშია, მაგრამ ახლა მარაგში არ არის.' if ka
                        else f"We carry {label}, but it's out of stock right now.")
            if state == 'UNKNOWN':
                return (f'{label} ჩვენს კატალოგშია. მარაგს ახლა ვერ ვადასტურებ — მაღაზიაში '
                        'გადაგიმოწმებენ.' if ka else
                        f"{label} is in our catalog. I can't confirm stock right now — the store "
                        'can check.')
            return (f'დიახ, {label} გვაქვს. {stock}' if ka
                    else f"Yes, we have {label}. {stock}")
        return (f'{code}-ზე ვიპოვე {count} პროდუქტი. {stock}' if ka
                else f'I found {count} items for {code}. {stock}')
    if s.grades:
        # A grade search checks the oil, not the car's requirement: never word it as a match.
        grade = ' / '.join(s.grades)
        return (f'{grade} ძრავის ზეთი გვაქვს — {count} ვარიანტი. {stock}' if ka
                else f"Yes, we have {grade} engine oil — {count} option"
                     f"{'s' if count != 1 else ''}. {stock}")
    if s.name_only and car:
        # The caveat is explained once; later replies only remind.
        explained = any(CHECK_FIT[0] in p or CHECK_FIT[1] in p for p in s.previous)
        if ka:
            return (f'{_ka_for(car)} კიდევ {count} {noun} ვიპოვე — ისევ {CHECK_FIT[0]}. {stock}'
                    if explained else
                    f'{_ka_for(car)} ვიპოვე {count} {noun}, მაგრამ ზუსტი კოდის გარეშე — '
                    f'{CHECK_FIT[0]}. {stock}')
        listed = 'is' if count == 1 else 'are'
        return (f'Here {listed} {count} {noun} for your {car_label(car)} — again, '
                f'{CHECK_FIT[1]}. {stock}' if explained else
                f'{count} {noun} {listed} listed for your {car_label(car)}, but without an '
                f'exact part number — {CHECK_FIT[1]}. {stock}')
    if car:
        options = ([f'{_ka_for(car)} ვიპოვე {count} {noun}. {stock}',
                    f'{_ka_for(car)} გვაქვს {count} {noun}. {stock}',
                    f'აი, {count} {noun} {_ka_for(car)}. {stock}'] if ka else
                   [f'I found {count} {noun} for your {car_label(car)}. {stock}',
                    f'For your {car_label(car)} I have {count} {noun}. {stock}',
                    f"Here {'is' if count == 1 else 'are'} {count} {noun} for your "
                    f'{car_label(car)}. {stock}'])
        return _pick(options, s.previous)
    return f'ვიპოვე {count} {noun}. {stock}' if ka else f'I found {count} {noun}. {stock}'


def _oil(s: Situation) -> str:
    ka, need = s.language == 'ka', s.requirements
    grade = (' ან ' if ka else ' or ').join(need.get('grades') or [])
    engine = need.get('engine_code')
    litres = need.get('capacity_l')
    car = s.vehicle if s.vehicle.get('model') else {}
    oils = [c for c in s.cards
            if c.get('basis') in {'oil_grade_brand', 'oil_grade', 'verified_oil'}]
    filters = [c for c in s.cards if c not in oils]
    missing = set(need.get('missing') or [])
    if ka:
        who = f'თქვენს {_ka_car(car)}-ს' if car else 'ამ ძრავს'
        text = f"{who}{f' ({engine})' if engine and car else ''} სჭირდება {grade}"
        text += f', დაახლოებით {litres} ლ.' if litres else '.'
        found = ([f'{len(oils)} შესაბამისი ზეთი'] if oils else []) + (
            [f'{len(filters)} ზეთის ფილტრი'] if filters else [])
        if found:
            text += ' ვიპოვე ' + ' და '.join(found) + '.'
        if 'engine_oil' in missing or (not oils and s.cards):
            text += ' შესაბამისი ზეთი მარაგში ვერ ვიპოვე.'
        if 'oil_filter' in missing:
            text += ' შესაბამისი ზეთის ფილტრი მარაგში ვერ ვიპოვე.'
    else:
        who = f'Your {car_label(car)}' if car else 'This engine'
        text = f"{who}{f' ({engine})' if engine and car else ''} takes {grade}"
        text += f', about {litres} L.' if litres else '.'
        found = ([f"{len(oils)} matching oil{'s' if len(oils) != 1 else ''}"] if oils else []) \
            + ([f"{len(filters)} oil filter{'s' if len(filters) != 1 else ''}"]
               if filters else [])
        if found:
            text += ' I found ' + ' and '.join(found) + '.'
        if 'engine_oil' in missing or (not oils and s.cards):
            text += " I couldn't find a matching oil in stock."
        if 'oil_filter' in missing:
            text += " I couldn't find a matching oil filter in stock."
    stock = availability(s.cards, s.language)
    return f'{text} {stock}'.strip()


SMALL_TALK_TEXT = {
    'greeting': (['გამარჯობა! რა ნაწილს ეძებთ?',
                  'გამარჯობა! მომწერეთ მანქანა (ან VIN) და რა ნაწილი გჭირდებათ.'],
                 ['Hi! What part are you looking for?',
                  'Hello! Tell me the car (or send the VIN) and the part you need.']),
    'thanks': (['არაფრის! კიდევ რამე ხომ არ გჭირდებათ?',
                'სიამოვნებით! სხვა რამე თუ დაგჭირდებათ, მომწერეთ.'],
               ["You're welcome! Anything else I can find for you?",
                'Happy to help! Just message me if you need anything else.']),
    'ack': (['კარგი. თუ კიდევ რამე დაგჭირდებათ, მომწერეთ.',
             'გასაგებია. სხვა ნაწილი თუ დაგჭირდებათ, აქ ვარ.'],
            ['Great. Just tell me if you need anything else.',
             "Sure — I'm here if you need another part."]),
    'bye': (['ნახვამდის! კარგ გზას გისურვებთ.', 'კარგად იყავით — ნებისმიერ დროს მომწერეთ.'],
            ['Bye! Drive safe.', 'Take care — message me anytime.']),
    'identity': (['მაღაზიის ნაწილების ავტომატური ასისტენტი ვარ — VIN-ით, კოდით ან დასახელებით '
                  'ვეძებ ჩვენს კატალოგსა და მარაგში. რა გჭირდებათ?'],
                 ["I'm the store's automated parts assistant. I search our catalog and stock by "
                  'VIN, part number or name. What do you need?']),
    'help': (['შემიძლია ნაწილის მოძებნა VIN-ით, კოდით ან დასახელებით და გეტყვით, რა არის '
              'მაღაზიაში. მაგალითად, მომწერეთ „წინა ხუნდები“ და თქვენი VIN.'],
             ["I can find parts by VIN, part number or name and tell you what's in the store. "
              'For example, send “front brake pads” with your VIN.']),
    'no': (['არა უშავს. კიდევ რამე?', 'კარგი. სხვა რამე თუ დაგჭირდებათ, მომწერეთ.'],
           ['No problem. Anything else?', 'Okay. Message me if you need anything else.']),
    'yes': (['კარგი — რა გჭირდებათ?'], ['Sure — what do you need?']),
}


def _small_talk(s: Situation) -> str:
    ka = s.language == 'ka'
    if s.act == 'greeting' and s.vehicle.get('model'):
        return (f'გამარჯობა! კიდევ რა გჭირდებათ {_ka_for(s.vehicle)}?' if ka
                else f'Hi again! What else do you need for your {car_label(s.vehicle)}?')
    if s.act == 'thanks' and s.vehicle.get('model') and not ka:
        return _pick([f"You're welcome! Anything else for your {car_label(s.vehicle)}?",
                      'Happy to help! Just message me if you need anything else.'], s.previous)
    return _pick(SMALL_TALK_TEXT[s.act][0 if ka else 1], s.previous)


def _clarify(s: Situation, category: str | None) -> tuple[str, dict]:
    ka = s.language == 'ka'
    vehicle = s.vehicle
    if s.act == 'clarify_vehicle':
        facts = {'example': EXAMPLE_CAR}
        if category:
            noun = _axle(s.category, s.language) + _noun(category, 2, s.language)
            return ((f'რომელი მანქანისთვის გჭირდებათ {noun}? მომწერეთ VIN ან მარკა, მოდელი და '
                     f'წელი (მაგ., {EXAMPLE_CAR}).' if ka else
                     f'Sure — which car are the {noun} for? Send the VIN, or the make, model and '
                     f'year (like {EXAMPLE_CAR}).'), facts)
        return (('რომელი მანქანისთვის? მომწერეთ VIN ან მარკა, მოდელი და წელი '
                 f'(მაგ., {EXAMPLE_CAR}).' if ka else
                 'Which car is it for? Send the VIN, or the make, model and year '
                 f'(like {EXAMPLE_CAR}).'), facts)
    if s.act == 'clarify_year':
        label = car_label(vehicle, with_year=False)
        return ((f'გასაგებია, {label}. რომელი წლისაა?' if ka
                 else f'Got it, a {label}. What year is it?'), {})
    if s.act == 'clarify_model':
        make = make_name(vehicle.get('make'))
        return ((f'{make}-ის რომელი მოდელია და რომელი წლის?' if ka
                 else f'Which {make} model is it, and what year?'), {})
    car = vehicle if vehicle.get('model') else {}
    count = len(s.cards)
    lead = ''
    if count and car:
        noun = _noun(category, count, s.language)
        lead = (f'{_ka_for(car)} ვიპოვე {count} {noun}. ' if ka
                else f'I found {count} {noun} for your {car_label(car)}. ')
    if s.act == 'clarify_axle':
        return lead + ('წინა თუ უკანა გჭირდებათ?' if ka else 'Front or rear?'), {}
    if s.act == 'clarify_brakes':
        return lead + ('ხუნდები თუ დისკები — წინა თუ უკანა?' if ka
                       else 'Pads or discs — and front or rear?'), {}
    if s.has_vin:
        text = ('ამ VIN-ით ძრავი ზუსტად ვერ დავადგინე. მომწერეთ ძრავის კოდი — ის ძრავზე ან '
                'მანქანის დოკუმენტებშია.' if ka else
                "I couldn't pin down the engine from the VIN. Could you send the engine code? "
                "It's on the engine or in the car's documents.")
    else:
        text = ('სწორი ზეთის შესარჩევად ძრავის კოდი მჭირდება — ის მანქანის დოკუმენტებშია. '
                'ან უბრალოდ VIN გამომიგზავნეთ.' if ka else
                "To pick the right oil I need the engine code — it's in the car's documents. "
                'Or just send the VIN.')
    if count:
        text += (' მანამდე, აი ზეთები, რაც გვაქვს, მაგრამ ჯერ ვერ ვიტყვი, რომელია სწორი.' if ka
                 else " Meanwhile, here are oils we have, but I can't say yet which one is right.")
    return text, {}


def _reference(s: Situation) -> str:
    ka = s.language == 'ka'
    if not s.cards:
        brand = brand_name((s.referenced[0] if s.referenced else {}).get('brand'))
        if s.status not in {'OK', 'CLARIFICATION'}:
            return ('ბოდიში, ახლა ვერ შევამოწმე. სცადეთ ცოტა ხანში.' if ka
                    else "Sorry, I couldn't check that one just now. Please try again shortly.")
        if s.sold_out:
            return ((f'სამწუხაროდ, {brand}-ის ვარიანტი ახლა მარაგში აღარ არის.' if brand else
                     'სამწუხაროდ, ის ახლა მარაგში აღარ არის.') if ka else
                    (f"Sorry, the {brand} one isn't in stock anymore." if brand else
                     "Sorry, that one isn't in stock anymore."))
        # Changed in the catalog, or no longer offered for this car.
        return ('ეს ვარიანტი თქვენი მანქანისთვის აღარ ჩანს — შეიძლება კატალოგში შეიცვალა.'
                if ka else
                "That one isn't listed for your car anymore — it may have changed in our "
                'catalog.')
    stock = availability(s.cards, s.language)
    if len(s.cards) == 1:
        product = s.cards[0].get('product', {})
        brand, code = brand_name(product.get('brand')), product.get('product_code') or ''
        if ka:
            label = ' '.join(p for p in (brand, f'({code})' if code else '') if p)
            return f'ეს არის {label}. {stock}'
        label = ' '.join(p for p in (f'{brand} one' if brand else 'one', f'({code})' if code
                                     else '') if p)
        return f"That's the {label}. {stock}"
    return (f'ეს {len(s.cards)} ვარიანტია. {stock}' if ka
            else f'Those are these {len(s.cards)}. {stock}')


def _greet(s: Situation, text: str) -> str:
    if not s.greeted:
        return text
    return ('გამარჯობა! ' if s.language == 'ka' else 'Hi! ') + text


def _none_in_stock(s: Situation, category: str | None) -> str:
    car = s.vehicle if s.vehicle.get('model') else {}
    noun = _axle(s.category, s.language) + _noun(category, 2, s.language)
    if s.language == 'ka':
        return (f'{_ka_for(car) + " " if car else ""}შესაბამისი {noun} ვიპოვე, მაგრამ ახლა არც '
                'მაღაზიაშია და არც ჩვენს საწყობში. როდის შემოვა, მაღაზიაში დაგიზუსტებენ.')
    return (f"I found {noun} that match your {car_label(car)}, but none are in the store or "
            "our warehouse right now. The store can tell you when more arrive." if car else
            f'I found matching {noun}, but none are in the store or our warehouse right now. '
            'The store can tell you when more arrive.')


def _no_codes(s: Situation, category: str | None) -> str:
    car = s.vehicle if s.vehicle.get('model') else {}
    noun = _noun(category, 1, s.language)
    if s.language == 'ka':
        who = _ka_for(car) if car else 'ამ მანქანისთვის'
        ask = ('გამომიგზავნეთ VIN და ზუსტ ნაწილებს მოვძებნი, ან ძველი ნაწილის კოდი.'
               if not s.has_vin else 'თუ ძველი ნაწილის კოდი გაქვთ, მომწერეთ და შევამოწმებ.')
        return (f'{who} ზუსტად ვერ დავადგინე, რომელი {noun} მოერგება, და გამოცნობას არ ვაპირებ. '
                + ask)
    ask = ("Send me the VIN and I can look up the exact part, or the old part's number."
           if not s.has_vin else "If you have the old part's number, send it and I'll check.")
    return (f"I couldn't work out which {noun} your {car_label(car) or 'car'} takes, and I'd "
            f'rather not guess. {ask}')


def _nothing(s: Situation) -> str:
    ka = s.language == 'ka'
    if s.status not in {'OK', 'CLARIFICATION'}:
        # A failed read is not evidence that the part or code does not exist.
        return ('ბოდიში, ახლა ვერ დავასრულე. სცადეთ ხელახლა ცოტა ხანში.' if ka
                else "Sorry, I couldn't finish that just now. Please try again in a moment.")
    if s.codes:
        code = ', '.join(s.codes)
        return (f'{code} ჩვენს კატალოგში ვერ ვიპოვე. შეგიძლიათ კოდი გადაამოწმოთ?' if ka
                else f"I couldn't find {code} in our catalog. Could you double-check the number?")
    return ('ჩვენს კატალოგში ვერ ვიპოვე. შეგიძლიათ მომწეროთ ნაწილის კოდი ან რომელი მანქანაა?'
            if ka else "I couldn't find that in our catalog. Could you send the part number, or "
                       'tell me the car?')


def compose(s: Situation) -> Reply:
    """The reply text, its single note, quick replies and what a 'yes' would mean."""
    reply = _compose(s)
    if reply.note and reply.note in s.previous_notes:
        reply.note = ''  # Said once in this conversation; repeating it reads like a system.
    return reply


def _compose(s: Situation) -> Reply:
    ka = s.language == 'ka'
    category = category_for_query(s.category) if s.category else None
    act = s.act
    quick = list(QUICK.get(act, ([], []))[1 if ka else 0])
    if act in SMALL_TALK_TEXT:
        return Reply(_small_talk(s))
    if act == 'acknowledge':
        return Reply('გასაგებია. რომელი ნაწილი გჭირდებათ?' if ka
                     else 'Got it. Which part do you need?')
    if s.status == 'NOT_CONFIGURED':
        return Reply('ჩატს სჭირდება Anthropic-ის API გასაღები. პირდაპირი ძიება ხელმისაწვდომია.'
                     if ka else 'Chat requires an Anthropic API key. Direct search is available.')
    if act.startswith('clarify'):
        text, facts = _clarify(s, category)
        return Reply(_greet(s, text), note(s) if s.cards else '', quick, facts=facts)
    if act == 'vehicle_identified':
        label = _ka_car(s.vehicle) if ka else car_label(s.vehicle)
        text = (f'ეს არის {label}. რა ნაწილი გჭირდებათ?' if ka
                else f"That's a {label}. What do you need for it?")
        return Reply(_greet(s, text), note(s), quick)
    if act == 'vin_unreadable':
        return Reply('ამ VIN-ით მანქანა ვერ ამოვიცანი. გადაამოწმეთ ან მომწერეთ მარკა, მოდელი და '
                     'წელი.' if ka else
                     "I couldn't read that VIN. Could you check it, or tell me the make, model "
                     'and year?')
    if s.status == 'TIMEOUT' and not s.cards:
        return Reply('ბოდიში, ძალიან დიდხანს გაგრძელდა. სცადეთ ხელახლა?' if ka
                     else 'Sorry, that took too long. Could you try again?')
    if act == 'reference':
        return Reply(_greet(s, _reference(s)), note(s))
    if act == 'reference_missing':
        shown = list(dict.fromkeys(brand_name(c.get('brand')) for c in s.referenced
                                   if c.get('brand')))[:3]
        asked = brand_name(s.missing_brand.upper()) or s.missing_brand
        names = ', '.join(shown)
        text = ((f'ნაჩვენებ ვარიანტებში {asked} არ არის' + (f' — ესენია: {names}.' if names
                                                         else '.')) if ka else
                (f"None of the ones I showed are {asked}" + (f" — they're {names}." if names
                                                             else '.')))
        return Reply(text, facts={'shown_brands': shown})
    if act == 'oil_resolved':
        return Reply(_greet(s, _oil(s)), note(s))
    if act == 'none_in_stock':
        return Reply(_greet(s, _none_in_stock(s, category)))
    if act == 'no_vehicle_codes':
        return Reply(_greet(s, _no_codes(s, category)))
    if act == 'no_results' or not s.cards:
        return Reply(_greet(s, _nothing(s)))
    text = _results(s, category)
    if s.status == 'TIMEOUT':
        text = ('ძიებამ ჩვეულებრივზე დიდხანს გასტანა — აი, რაც აქამდე ვიპოვე. ' if ka
                else "That took longer than usual — here's what I found so far. ") + \
            availability(s.cards, s.language)
    question, offer = _offer(s, category)
    if question:
        text = f'{text} {question}'
    return Reply(_greet(s, text), note(s), offer=offer)
