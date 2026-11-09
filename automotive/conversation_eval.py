"""Scripted multi-turn evaluation of the parts chat: accuracy, natural feel and speed.

Every scenario turn goes through the same ``ConversationStore`` the web chat uses. Hard
checks (facts grounded in the cards, reply language, expected behaviour) must all pass;
soft checks score how natural the replies read; timings measure speed. ``mock`` mode is
offline and deterministic; ``live`` uses the configured database, providers and Anthropic
with VINs from a private file. VINs are never printed or written: transcripts show
``{VIN1}`` placeholders.
"""

from __future__ import annotations

import copy
import json
import re
import statistics
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from automotive import grounding
from automotive.categories import PATTERNS, category_for_query, matches_category
from automotive.codes import normalize_code, parse_codes, product_codes
from automotive.dialogue import VIN

DEFAULT_SCENARIOS = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'conversations'
RESULT_ACTS = {'results', 'no_results', 'none_in_stock', 'no_vehicle_codes', 'oil_resolved',
               'clarify_axle', 'clarify_brakes', 'clarify_engine', 'reference',
               'reference_missing'}
# Expectations that depend on the synthetic catalog and are skipped against live data.
LIVE_SKIP = {'cards_min', 'cards_max', 'answer_has'}
DISCLAIMERS = re.compile(
    r'does not confirm|not a (suitability )?recommendation|unconfirmed|not independently'
    r'|არ ადასტურებს|დაუდასტურებ|რეკომენდაციას არ ნიშნავს|დამოუკიდებლად', re.IGNORECASE)
SHORT_REPLY = 280
POLL_SECONDS = 0.02

MOCK_VINS = {'VIN1': '1HGCM82633A004352', 'VIN2': 'WDDKJ6HB0FF123456'}
MOCK_CARS = {
    MOCK_VINS['VIN1']: {'make': 'HONDA', 'model': 'Accord', 'model_year': 2015, 'trim': 'EX'},
    MOCK_VINS['VIN2']: {'make': 'MERCEDES-BENZ', 'model': 'E-Class', 'model_year': 2015,
                        'trim': 'E 400'},
}
MOCK_FLUIDS = {
    'HONDA': ('K24W', '0W-20', '4.2'),
    'MERCEDES-BENZ': ('M 276.850', '5W-40', '6.5'),
}


def _product(identifier: str, name: str, brand: str, code: str, store: float = 0.0,
             tuz11: float = 0.0, fits: tuple[str, ...] = (), **extra: Any) -> dict:
    return {
        'source_product_id': identifier, 'source_fingerprint': f'fp-{identifier}',
        'product_code': code, 'product_name': name, 'product_model': '', 'brand': brand,
        'original_code': '', 'duplicate_code': '', 'price': None, 'currency': None,
        'stock': {'store': store, 'tuz11': tuz11}, 'status': 'active',
        'last_observed_at': datetime.now(UTC) - timedelta(minutes=30), 'fits': fits, **extra,
    }


OIL_NAMES = ('Shell Helix Ultra 0W-20 4L ძრავის ზეთი', 'MOBIL 1 ESP 0W-20 1L ძრავის ზეთი',
             'Shell Helix Ultra 5W-40 4L ძრავის ზეთი')
MOCK_PRODUCTS = [
    _product('fp1', 'სამუხრუჭე ხუნდი წინა', 'TRW', 'GDB3520', store=3, fits=('HONDA',)),
    _product('fp2', 'სამუხრუჭე ხუნდი წნ', 'BREMBO', 'P28076', tuz11=2, fits=('HONDA',)),
    _product('rp1', 'სამუხრუჭე ხუნდი უკანა', 'TRW', 'GDB3521', store=1, fits=('HONDA',)),
    _product('fd1', 'სამუხრუჭე დისკი წინა', 'ATE', '24.0128', store=2, fits=('HONDA',)),
    _product('rd1', 'სამუხრუჭე დისკი უკანა', 'ATE', '24.0110', fits=('HONDA',)),
    _product('af1', 'ჰაერის ფილტრი', 'MANN', 'C 2201', store=4,
             fits=('HONDA', 'MERCEDES-BENZ'),
             last_observed_at=datetime.now(UTC) - timedelta(hours=5)),  # A stale-stock note.
    _product('cf1', 'სალონის ფილტრი', 'MANN', 'CU 2335', tuz11=1, fits=('HONDA',)),
    _product('of1', 'ზეთის ფილტრი K24', 'MANN', 'W 610/3', store=5, part_role='oil_filter',
             family='K24', fits=('HONDA',)),
    _product('of2', 'ზეთის ფილტრი M276', 'MANN', 'HU 7010 Z', store=2, part_role='oil_filter',
             family='M276', fits=('MERCEDES-BENZ',)),
    _product('oil1', OIL_NAMES[0], 'SHELL', '550046', store=6),
    _product('oil2', OIL_NAMES[1], 'MOBIL', '152560', tuz11=3),
    _product('oil3', OIL_NAMES[2], 'SHELL', '550052', store=4),
    _product('sp1', 'ანთების სანთელი', 'NGK', 'ILZKR7B11', store=8, fits=('HONDA',)),
    _product('bat1', 'აკუმულატორი 70Ah', 'VARTA', '570144', store=2, fits=('HONDA',)),
    _product('ap1', 'Rear brake pads', 'Maker', 'AP104/2', store=2),
]


class MockRepository:
    """The synthetic catalog behind mock mode, shaped like ``AutomotiveRepository``."""

    def __init__(self) -> None:
        self.products = {p['source_product_id']: p for p in MOCK_PRODUCTS}

    @staticmethod
    def public(product: dict) -> dict:
        return {k: copy.deepcopy(v) for k, v in product.items() if k not in {'fits', 'family'}}

    def search_parts(self, code: str, manufacturer: str | None = None, limit: int = 30) -> dict:
        wanted = normalize_code(code)
        items = []
        for product in self.products.values():
            matched = [c for c in product_codes(product) if c.normalized == wanted]
            if matched and (not manufacturer
                            or manufacturer.casefold() == product['brand'].casefold()):
                item = self.public(product)
                item['matched_codes'] = [{'original': c.original, 'normalized': c.normalized,
                                          'field': c.field, 'manufacturer': c.manufacturer,
                                          'raw_value': c.raw_value} for c in matched]
                items.append(item)
        return {'items': items[:limit], 'warnings': [], 'query': code,
                'status': 'PROVISIONAL' if items else 'UNKNOWN'}

    def search_parts_many(self, codes: list[str]) -> dict:
        found: dict[str, list[dict]] = {}
        for code in codes:
            items = self.search_parts(code)['items']
            if items:
                found[normalize_code(code)] = items
        return found

    def get_product(self, product_id: str) -> dict | None:
        product = self.products.get(product_id)
        return self.public(product) if product else None

    def search_products(self, query: str, manufacturer: str | None = None, limit: int = 30,
                        offset: int = 0) -> dict:
        items = [self.public(p) for p in self.products.values() if matches_category(p, query)]
        return {'items': items[:limit], 'has_more': False, 'status': 'PROVISIONAL'}

    def search_engine_oils(self, viscosity: str, brand: str | None = None,
                           limit: int = 100) -> dict:
        grade = re.sub(r'[^0-9W]', '', viscosity.upper())
        items = [self.public(p) for p in self.products.values()
                 if matches_category(p, 'engine oil')
                 and grade in re.sub(r'[^0-9W]', '', p['product_name'].upper())
                 and (not brand or brand.casefold() == p['brand'].casefold())]
        items.sort(key=lambda p: (p['stock']['store'] <= 0, p['stock']['tuz11'] <= 0))
        return {'items': items[:limit], 'viscosity': viscosity}

    def search_engine_family(self, family: str, limit: int = 50) -> dict:
        key = re.sub(r'[^A-Z0-9]', '', family.upper())
        items = [{**self.public(p), 'part_role': 'oil_filter'} for p in self.products.values()
                 if p.get('family') and p['family'] in key]
        return {'items': items[:limit], 'family': family}

    def vehicle_catalog_candidates(self, vehicle: dict, limit: int = 30,
                                   exclude_ids: list | None = None, offset: int = 0,
                                   part_query: str = '') -> dict:
        return {'items': [], 'has_more': False}

    def fitments(self, code: str, vehicle: dict | None = None) -> dict:
        return {'status': 'UNKNOWN', 'compatible': None, 'items': [], 'candidates': [],
                'warnings': []}

    def cross_references(self, code: str) -> dict:
        return self.fitments(code)

    def vehicle_fitment_records(self) -> list:
        return []

    def vehicle_specifications(self, vehicle: dict) -> list:
        return []

    def product_approvals(self, product_id: str) -> list:
        return []

    def inventory_products(self) -> list:
        return [self.public(p) for p in self.products.values()]


class MockVinService:
    def decode(self, vin: str, model_year: int | None = None, deadline: float | None = None
               ) -> dict:
        if vin not in MOCK_CARS:
            raise RuntimeError('VIN_UNAVAILABLE')
        vehicle = dict(MOCK_CARS[vin])
        if model_year:
            vehicle['model_year'] = model_year
        return {'vehicle': vehicle, 'status': 'PROVISIONAL'}


class MockSearch:
    """Answers a vehicle part question with code-derived catalog rows, like the federation."""

    def __init__(self, repository: MockRepository) -> None:
        self.repository = repository
        self._jobs: dict[str, dict] = {}

    def prewarm(self, vin: str) -> None:
        return None

    def close(self) -> None:
        return None

    def start(self, vin: str | None, part_query: str = '', model_year: int | None = None,
              configuration: dict | None = None) -> dict:
        if vin is not None and vin not in MOCK_CARS:
            raise RuntimeError('VIN_UNAVAILABLE')
        vehicle = dict(MOCK_CARS[vin]) if vin else dict(configuration or {})
        make = str(vehicle.get('make') or '').upper()
        category = category_for_query(part_query) if part_query.strip() else None
        wanted = {'brakes': ('brake_pads', 'brake_discs')}.get(category or '', (category,))
        items = []
        for product in self.repository.products.values():
            if make not in product.get('fits', ()):
                continue
            if category in {'engine_oil', 'oil_change'}:
                continue
            if category and not any(key in PATTERNS and re.search(
                    PATTERNS[key], product['product_name'], re.IGNORECASE) for key in wanted):
                continue
            row = {'product': self.repository.public(product), 'status': 'UNKNOWN',
                   'compatible': None, 'match_type': 'CODE_CANDIDATE', 'evidence': [],
                   'warnings': [], 'provenance': [f"Catalog API: {product['product_code']}"]}
            items.append({'warehouse_records': [row], 'supplier_records': [],
                          'status': 'UNKNOWN', 'source_names': ['catalog_api']})
        fluids = []
        if category in {'engine_oil', 'oil_change'} and make in MOCK_FLUIDS:
            engine, grade, litres = MOCK_FLUIDS[make]
            fluids.append({'provider': 'Shell', 'status': 'PROVISIONAL', 'engine_code': engine,
                           'items': [{'kind': 'oil', 'product_name': f'Helix Ultra {grade}',
                                      'viscosity': grade, 'capacity': litres}]})
        identifier = f'job{len(self._jobs) + 1}'
        self._jobs[identifier] = {
            'search_id': identifier, 'status': 'COMPLETE', 'vehicle': vehicle, 'items': items,
            'next_offset': None, 'fluids': fluids, 'warnings': [],
            'sources': [{'source_id': 'catalog_api', 'status': 'OK', 'elapsed_ms': 900}],
        }
        return copy.deepcopy(self._jobs[identifier])

    def get(self, identifier: str, offset: int = 0, limit: int = 50) -> dict | None:
        job = self._jobs.get(identifier)
        return copy.deepcopy({**job, 'items': job['items'][offset:offset + limit]}) if job else None


class MockModel:
    """A scripted stand-in for the model that routes the way the system prompt asks."""

    def __init__(self) -> None:
        self.messages = self
        self.calls = 0

    @staticmethod
    def _reply(*blocks: dict, stop: str = 'tool_use') -> SimpleNamespace:
        return SimpleNamespace(content=list(blocks), stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=400, output_tokens=40))

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        system = kwargs.get('system')
        system_text = ' '.join(b.get('text', '') for b in system) if isinstance(
            system, list) else str(system or '')
        last = kwargs['messages'][-1]['content']
        if 'REPHRASE' in system_text:
            draft = json.loads(last if isinstance(last, str) else '{}').get('draft', '')
            return self._reply({'type': 'text', 'text': draft}, stop='end_turn')
        if isinstance(last, str):
            data = json.loads(last)
            codes = [c.original.strip('?!.,:;') for c in parse_codes(data.get('question', ''))
                     if not re.fullmatch(r'(19|20)\d\d', c.normalized)]
            if codes:
                return self._reply({'type': 'tool_use', 'id': 'm1', 'name': 'search_inventory',
                                    'input': {'code': codes[0], 'manufacturer': None}})
            if data.get('requested_category'):
                return self._reply({'type': 'tool_use', 'id': 'm1', 'name': 'search_products',
                                    'input': {'query': data['requested_category']}})
            return self._finish('help', [])
        refs = []
        for block in last:
            try:
                ref = json.loads(block.get('content', '{}')).get('result_id')
            except (ValueError, AttributeError):
                ref = None
            if ref:
                refs.append(ref)
        return self._finish('results' if refs else 'no_results', refs)

    def _finish(self, act: str, refs: list[str]) -> SimpleNamespace:
        return self._reply({'type': 'tool_use', 'id': f'f{self.calls}', 'name': 'finish_response',
                            'input': {'answer': act, 'result_ids': refs}})


class Counting:
    """Counts calls on a wrapped service so a turn can be checked for 'did not search'."""

    def __init__(self, target: Any, names: set[str]) -> None:
        self._target, self._names, self.calls = target, names, 0
        self.rephrase_calls = 0

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if name in self._names and callable(value):
            def counted(*args: Any, **kwargs: Any) -> Any:
                # A rewording of a finished reply is not a routing call; it is reported apart.
                if 'REPHRASE' in json.dumps(kwargs.get('system', ''), default=str):
                    self.rephrase_calls += 1
                else:
                    self.calls += 1
                return value(*args, **kwargs)
            return counted
        return value


READS = {'search_parts', 'search_parts_many', 'search_products', 'search_engine_oils',
         'search_engine_family', 'vehicle_catalog_candidates', 'fitments', 'get_product',
         'inventory_products', 'vehicle_specifications'}


def mock_store() -> Any:
    """A ConversationStore over the synthetic catalog and the scripted model."""
    from dataclasses import fields

    from automotive.chat import ChatService
    from automotive.config import Settings
    from automotive.conversations import ConversationStore
    from automotive.rules import load_rules

    repository = MockRepository()
    options = {'anthropic_api_key': 'mock-key', 'vin_hmac_key': 'm' * 32}
    if 'chat_rephrase' in {f.name for f in fields(Settings)}:
        options['chat_rephrase'] = True
    chat = ChatService(repository, MockVinService(), Settings(**options), client=MockModel(),
                       rules=load_rules())
    return ConversationStore(chat, MockSearch(repository))


def live_store() -> Any:
    from automotive.api import create_app
    from automotive.config import Settings

    return create_app(settings=Settings.from_env()).state.conversations


def load_scenarios(path: Path | str = DEFAULT_SCENARIOS) -> list[dict]:
    target = Path(path)
    files = sorted(target.glob('*.json')) if target.is_dir() else [target]
    scenarios: list[dict] = []
    for file in files:
        scenarios.extend(json.loads(file.read_text(encoding='utf-8')))
    ids = Counter(s['id'] for s in scenarios)
    duplicates = [i for i, n in ids.items() if n > 1]
    if duplicates:
        raise ValueError(f'Duplicate scenario ids: {", ".join(duplicates)}')
    return scenarios


def legacy_language(message: str) -> str:
    """The language the old interface chose per message: any Georgian letter means ka."""
    return 'ka' if re.search(r'[\u10a0-\u10ff]', message) else 'en'


def _fill(text: str, vins: dict[str, str]) -> str:
    return re.sub(r'\{(VIN\d)\}', lambda m: vins[m.group(1)], text)


def _hide(text: str, vins: dict[str, str]) -> str:
    for key, vin in vins.items():
        text = text.replace(vin, '{' + key + '}')
    return VIN.sub('[VIN]', text)


def _key(value: Any) -> str:
    return re.sub(r'[^a-z0-9ა-ჰ]+', '', str(value or '').casefold())


def run_turn(store: Any, conversation: str, message: str, language: str, vin: str | None,
             vehicle: dict | None, timeout: float) -> tuple[dict, float, float | None]:
    """Submit one message and poll like the interface; returns payload and timings (ms)."""
    started = time.monotonic()
    turn = store.submit(conversation, message, vehicle=vehicle, vin=vin, language=language)
    first_card = None
    while True:
        payload = store.get(conversation, turn['turn_id'], 0, 20)
        elapsed = (time.monotonic() - started) * 1000
        if first_card is None and payload.get('cards'):
            first_card = elapsed
        if payload.get('status') not in {'RUNNING', 'PENDING'}:
            return payload, elapsed, first_card
        if elapsed > timeout * 1000:
            return {**payload, 'status': 'HARNESS_TIMEOUT'}, elapsed, first_card
        time.sleep(POLL_SECONDS)


def check_turn(turn: dict, payload: dict, context: dict, live: bool) -> dict:
    """Hard and soft checks for one reply."""
    expect = dict(turn.get('expect') or {})
    if live:
        for key in LIVE_SKIP:
            expect.pop(key, None)
        if turn.get('vin'):
            expect.pop('vehicle', None)
        if expect.get('act') and set(expect['act']) <= RESULT_ACTS:
            expect['act'] = sorted(RESULT_ACTS)
    answer = str(payload.get('answer') or '')
    note = str(payload.get('note') or '')
    shown = f'{answer} {note}'.strip()
    expected_language = expect.get('language') or context['language']
    hard: dict[str, bool] = {}
    allowed = grounding.facts_text(payload, context['messages'])
    found = [p for p in grounding.problems(shown, allowed, expected_language)
             if p != 'wrong_language']
    hard['grounded'] = not found
    hard['language'] = grounding.language_ok(shown, expected_language, allowed)
    hard['completed'] = payload.get('status') in {'COMPLETE', 'PARTIAL'}
    if 'searched' in expect:
        hard['searched' if expect['searched'] else 'no_search'] = (
            context['searched'] == expect['searched'])
    if 'act' in expect:
        hard['act'] = payload.get('act') in expect['act']
    category_text = str(payload.get('category') or '')
    if 'category' in expect:
        hard['category'] = category_for_query(category_text) == expect['category']
    if 'axle' in expect:
        from automotive.rules import requested_axle
        hard['axle'] = requested_axle(category_text) == expect['axle']
    cards = payload.get('cards') or []
    total = payload.get('total', len(cards))
    if 'cards_min' in expect:
        hard['cards_min'] = total >= expect['cards_min']
    if 'cards_max' in expect:
        hard['cards_max'] = total <= expect['cards_max']
    if 'vehicle' in expect:
        vehicle = payload.get('vehicle') or {}
        hard['vehicle'] = all(_key(vehicle.get(k)) == _key(v)
                              for k, v in expect['vehicle'].items())
    if 'answer_has' in expect:
        hard['answer_has'] = any(_key(x) in _key(shown) for x in expect['answer_has'])
    if 'model_calls_max' in expect:
        hard['model_calls'] = context['model_calls'] <= expect['model_calls_max']
    if 'quick_replies_min' in expect:
        hard['quick_replies'] = len(payload.get('quick_replies') or []) >= expect[
            'quick_replies_min']
    soft: dict[str, bool] = {
        'short': len(shown) <= SHORT_REPLY,
        'one_question': shown.count('?') <= 1,
        'not_repeated': answer not in context['previous'],
    }
    # The old interface printed every warning; the new one prints the single note.
    notes = [note] if 'note' in payload else list(payload.get('warnings') or [])
    soft['few_disclaimers'] = len([n for n in notes if n]) + len(DISCLAIMERS.findall(answer)) <= 1
    vehicle = payload.get('vehicle') or {}
    if cards and vehicle.get('model') and turn.get('kind') in {'lookup', 'oil'}:
        names = {_key(vehicle.get('model')), _key(vehicle.get('make'))} - {''}
        soft['personal'] = any(name in _key(answer) for name in names)
    if cards:
        soft['specific'] = bool(re.search(r'\d', answer))
    return {'hard': hard, 'soft': soft, 'problems': found}


def _reply_language(turn: dict, message: str, previous: str) -> str:
    """The language a person expects back: explicit choice, else the letters they typed."""
    if turn.get('language') in {'ka', 'en'}:
        return turn['language']
    words = VIN.sub(' ', message)
    words = ' '.join(t for t in words.split() if not any(ch.isdigit() for ch in t))
    if re.search(r'[ა-ჰ]', words):
        return 'ka'
    return 'en' if re.search(r'[A-Za-z]{2}', words) else previous


def run_scenario(store: Any, scenario: dict, vins: dict[str, str], *, live: bool,
                 counters: dict, timeout: float) -> dict:
    conversation = store.create()['conversation_id']
    context: dict[str, Any] = {'messages': [], 'previous': [],
                               'language': scenario.get('language', 'ka')}
    auto = getattr(store, 'AUTO_LANGUAGE', False)
    turns = []
    try:
        for turn in scenario['turns']:
            message = _fill(turn['say'], vins)
            context['language'] = turn.get('reply_language') or _reply_language(
                turn, message, context['language'])
            language = turn.get('language', 'auto')
            if language == 'auto' and not auto:
                language = legacy_language(message)
            before = {name: counter.calls for name, counter in counters.items()}
            rephrases = counters['model'].rephrase_calls
            try:
                payload, final_ms, first_ms = run_turn(
                    store, conversation, message, language,
                    vins.get(turn['vin']) if turn.get('vin') else None,
                    turn.get('vehicle'), timeout)
            except Exception as exc:  # A rejected submit is a failed turn, not a crash.
                payload, final_ms, first_ms = {'status': 'ERROR', 'answer': '',
                                               'error': type(exc).__name__}, 0.0, None
            context['searched'] = any(counters[n].calls > before[n]
                                      for n in ('search', 'repository'))
            context['model_calls'] = counters['model'].calls - before['model']
            context['messages'].append(message)
            result = check_turn(turn, payload, context, live)
            context['previous'].append(str(payload.get('answer') or ''))
            turns.append({
                'say': _hide(message, vins), 'kind': turn.get('kind', 'other'),
                'answer': _hide(str(payload.get('answer') or ''), vins),
                'note': payload.get('note'), 'act': payload.get('act'),
                'status': payload.get('status'), 'cards': payload.get('total', 0),
                'final_ms': round(final_ms), 'first_card_ms': first_ms and round(first_ms),
                'model_calls': context['model_calls'],
                'rephrase_calls': counters['model'].rephrase_calls - rephrases,
                'rephrased': bool(payload.get('rephrased')), **result,
            })
    finally:
        store.delete(conversation)
    return {'id': scenario['id'], 'tags': scenario.get('tags', []), 'turns': turns}


def summarize(results: list[dict]) -> dict:
    turns = [t for s in results for t in s['turns']]
    hard = [ok for t in turns for ok in t['hard'].values()]
    soft = [ok for t in turns for ok in t['soft'].values()]
    failures = Counter(name for t in turns for name, ok in t['hard'].items() if not ok)
    misses = Counter(name for t in turns for name, ok in t['soft'].items() if not ok)
    speed = {}
    for kind in sorted({t['kind'] for t in turns}):
        group = [t for t in turns if t['kind'] == kind]
        firsts = [t['first_card_ms'] for t in group if t['first_card_ms'] is not None]
        speed[kind] = {
            'turns': len(group),
            'median_ms': round(statistics.median(t['final_ms'] for t in group)),
            'max_ms': max(t['final_ms'] for t in group),
            'median_first_card_ms': round(statistics.median(firsts)) if firsts else None,
            'model_calls': sum(t['model_calls'] for t in group),
            'rephrased': f"{sum(t['rephrased'] for t in group)}/"
                         f"{sum(t['rephrase_calls'] for t in group)}",
        }
    return {
        'scenarios': len(results), 'turns': len(turns),
        'scenarios_passed': sum(all(ok for t in s['turns'] for ok in t['hard'].values())
                                for s in results),
        'hard_pass_rate': round(sum(hard) / len(hard), 3) if hard else 1.0,
        'naturalness': round(sum(soft) / len(soft), 3) if soft else 1.0,
        'hard_failures': dict(failures.most_common()), 'soft_misses': dict(misses.most_common()),
        'speed': speed,
    }


JUDGE_PROMPT = (
    'You review a customer chat with an auto parts store assistant. Score how much it feels '
    'like texting a helpful, competent human shop assistant: 5 natural, warm, brief and '
    'specific; 3 usable but robotic or repetitive; 1 confusing or unhelpful. Ignore whether '
    'the parts are right. [VIN] marks a VIN redacted for privacy; it is not an error. The '
    'chat may be Georgian. Reply only with JSON {"score": n, "issue": "one short English '
    'sentence"}.'
)


def judge(results: list[dict], settings: Any, out: TextIO) -> dict:
    """Optional model rating of each transcript (live only, one bounded call each)."""
    import anthropic

    headers = ({'anthropic-workspace-id': settings.anthropic_workspace_id}
               if settings.anthropic_workspace_id else {})
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=30,
                                 default_headers=headers)
    scores = []
    for scenario in results:
        transcript = re.sub(r'\{VIN\d\}', '[VIN]', '\n'.join(
            f"Customer: {t['say']}\nAssistant: {t['answer']} {t['note'] or ''} "
            f"[{t['cards']} part cards shown]" for t in scenario['turns']))
        try:
            response = client.messages.create(
                model=settings.anthropic_model, max_tokens=300, system=JUDGE_PROMPT,
                messages=[{'role': 'user', 'content': transcript[:6000]}])
            text = ''.join(getattr(b, 'text', '') for b in response.content)
            verdict = json.loads(text[text.index('{'):text.rindex('}') + 1])
            scenario['judge'] = {'score': int(verdict['score']),
                                 'issue': str(verdict.get('issue', ''))[:200]}
            scores.append(scenario['judge']['score'])
            out.write(f"judge {scenario['id']}: {verdict['score']} {verdict.get('issue', '')}\n")
        except (anthropic.APIError, ValueError, KeyError, TypeError):
            out.write(f"judge {scenario['id']}: unavailable\n")
    client.close()
    return {'judged': len(scores),
            'mean_score': round(statistics.mean(scores), 2) if scores else None}


def evaluate(store: Any, scenarios: list[dict], vins: dict[str, str], out: TextIO, *,
             live: bool = False, timeout: float = 90.0) -> list[dict]:
    """Run scenarios and print one line per turn; returns the per-scenario results."""
    chat = store.chat_service
    counters: dict[str, Counting] = {
        'search': Counting(store.search_service, {'start'}),
        'repository': Counting(chat.repository, READS),
    }
    store.search_service = counters['search']
    chat.repository = counters['repository']
    client = chat.client if chat.client is not None else chat._client()
    counters['model'] = Counting(client.messages, {'create'})
    chat.client = SimpleNamespace(messages=counters['model'],
                                  close=getattr(client, 'close', lambda: None))
    results = []
    for scenario in scenarios:
        result = run_scenario(store, scenario, vins, live=live, counters=counters,
                              timeout=timeout)
        results.append(result)
        for index, turn in enumerate(result['turns'], 1):
            failed = [n for n, ok in turn['hard'].items() if not ok]
            missed = [n for n, ok in turn['soft'].items() if not ok]
            verdict = 'FAIL ' + ','.join(failed) if failed else 'ok'
            soft = 'soft:' + ','.join(missed) if missed else ''
            out.write(f"{result['id'][:26]:26} #{index} {turn['kind'][:10]:10} "
                      f"{turn['final_ms']:6d}ms {verdict:30} {soft:36} "
                      f"{(turn['answer'] or '')[:90]!r}\n")
            for problem in turn['problems']:
                out.write(f"{'':30}grounding: {problem}\n")
        out.flush()
    return results


def print_summary(summary: dict, out: TextIO) -> None:
    out.write('\nSummary\n')
    out.write(f"- scenarios passing every hard check: {summary['scenarios_passed']}/"
              f"{summary['scenarios']} ({summary['turns']} turns)\n")
    out.write(f"- hard check pass rate: {summary['hard_pass_rate']:.1%}; naturalness (soft "
              f"checks): {summary['naturalness']:.1%}\n")
    out.write(f"- hard failures: {summary['hard_failures'] or '-'}\n")
    out.write(f"- soft misses: {summary['soft_misses'] or '-'}\n")
    for kind, data in summary['speed'].items():
        first = data['median_first_card_ms']
        out.write(f"- speed {kind}: {data['turns']} turns, median {data['median_ms']} ms, "
                  f"max {data['max_ms']} ms"
                  f"{f', first card {first} ms' if first is not None else ''}, "
                  f"model calls {data['model_calls']}, rephrase kept/tried "
                  f"{data['rephrased']}\n")
    if summary.get('judge'):
        out.write(f"- judge: {summary['judge']}\n")


def read_vins(path: Path | str) -> dict[str, str]:
    from automotive.evaluate import read_vins as read

    vins, _ = read(path)
    return {f'VIN{i}': vin for i, vin in enumerate(vins, 1)}
