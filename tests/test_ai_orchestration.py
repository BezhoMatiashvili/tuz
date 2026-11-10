"""AI intent precedes clarification; model text never becomes automotive evidence."""

import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automotive.chat import ChatService
from automotive.config import Settings


def response(name, args):
    return SimpleNamespace(content=[{'type': 'tool_use', 'id': 't1', 'name': name,
                                     'input': args}], stop_reason='tool_use', usage=None)


def intent(message, *, engine=None):
    fields = [
        {'field': 'make', 'value': 'Mercedes-Benz', 'quote': message.split()[0]},
        {'field': 'model', 'value': 'E-Class', 'quote': 'E'},
        {'field': 'model_year', 'value': 2015, 'quote': '2015'},
        {'field': 'displacement_l', 'value': 3.0, 'quote': '3.0'},
        {'field': 'body', 'value': 'Coupe', 'quote': 'coupe'},
    ]
    if engine:
        fields.append({'field': 'engine_code', 'value': engine, 'quote': '3.0'})
    return {'fields': fields, 'requested_parts': [{'category': 'oil_change', 'quote': 'oil'}],
            'references': [], 'correction': False}


class AIOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.vehicle_specifications.return_value = []
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_engine_oils.return_value = {'items': []}
        self.repository.search_parts_many.return_value = {}
        self.client = Mock()
        self.settings = SimpleNamespace(**{
            **{name: getattr(Settings(), name) for name in Settings.__dataclass_fields__},
            'chat_orchestration': True, 'automotive_cache_path': '',
        })
        self.service = ChatService(self.repository, Mock(), self.settings, self.client)
        self.search = Mock()
        self.search._catalog = None
        self.search.start.return_value = {
            'search_id': 'search', 'status': 'COMPLETE', 'vehicle': {}, 'sources': [],
            'items': [], 'fluids': [], 'next_offset': None,
        }
        research = patch('automotive.research.AutomotiveResearch.run', return_value={
            'status': 'EMPTY', 'records': [], 'citations': [], 'sources': []})
        research.start()
        self.addCleanup(research.stop)

    def run_turn(self, message, **kwargs):
        return self.service.respond_conversation(message, language='en',
                                                  search_service=self.search, **kwargs)

    def finish(self, **kwargs):
        return response('finish_response', {'result_ids': [], 'product_ids': [],
                                           'claim_ids': [], 'question_field': None,
                                           'presentation': 'concise', **kwargs})

    def test_spelling_variants_reach_ai_before_any_vehicle_clarification(self):
        for spelling in ('მერსედესსზე', 'მერსედესზე', 'mersedesze', 'Mercedes'):
            with self.subTest(spelling=spelling):
                message = f'{spelling} E coupe 2015 3.0 engine oil change'
                self.client.messages.create.side_effect = [
                    response('resolve_vehicle', intent(message)), self.finish(),
                ]
                result = self.run_turn(message)
                self.assertEqual(result['vehicle']['make'], 'Mercedes-Benz')
                self.assertEqual(result['vehicle']['model_year'], 2015)
                self.assertEqual(result['vehicle']['displacement_l'], 3.0)
                self.assertNotIn('make, model', result['answer'].lower())
                self.assertGreaterEqual(result['usage']['model_requests'], 1)
                self.assertIn(spelling, result['customer_stated']['make']['quote'])

    def test_engine_invented_from_displacement_is_rejected(self):
        message = 'Mercedes E coupe 2015 3.0 engine oil change'
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent(message, engine='M276.850')), self.finish(),
        ]
        result = self.run_turn(message)
        self.assertNotIn('M276', json.dumps(result))
        self.assertNotIn('engine_code', result['vehicle'])

    def test_correction_invalidates_old_configuration_and_result_authorization(self):
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', {'fields': [
                {'field': 'fuel', 'value': 'Diesel', 'quote': 'diesel'}],
                'requested_parts': [], 'references': [], 'correction': True}), self.finish(),
        ]
        result = self.run_turn('Actually diesel', vehicle={
            'make': 'Mercedes-Benz', 'model': 'E-Class', 'model_year': 2015,
            'fuel': 'Gasoline', 'engine_code': 'M276.850'},
            dialogue={'vehicle_revision': 4, 'selected_configuration': {'engine_code': 'M276.850'}})
        self.assertEqual(result['vehicle']['fuel'], 'Diesel')
        self.assertNotIn('engine_code', result['vehicle'])
        self.assertGreater(result['vehicle_revision'], 4)
        self.assertEqual(result['selected_configuration'], {})

    def test_unknown_answer_claim_and_product_references_fall_back(self):
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', {'fields': [], 'requested_parts': [],
                                         'references': [], 'correction': False}),
            self.finish(claim_ids=['invented_capacity'], product_ids=['invented_stock']),
            self.finish(),
        ]
        result = self.run_turn('Tell me what you found')
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['cards'], [])
        self.assertNotIn('invented', result['answer'])

    def test_model_timeout_is_remaining_shared_deadline_and_requests_are_bounded(self):
        self.client.messages.create.return_value = response('resolve_vehicle', {
            'fields': [], 'requested_parts': [], 'references': [], 'correction': False})
        result = self.run_turn('What oil should I use?', deadline=time.monotonic() + 0.5)
        self.assertLessEqual(result['usage']['model_requests'], 6)
        self.assertLessEqual(result['usage']['tool_executions'], 10)
        self.assertLessEqual(result['usage']['web_searches'], 2)
        for call in self.client.messages.create.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 0.5)

    def test_catalog_candidates_are_distinct_from_customer_details(self):
        message = 'Mercedes E coupe 2015 3.0 engine oil change'
        candidate = {'vehicle_id': 12, 'type_id': 1, 'engine_code': 'M276.850',
                     'fuel': 'Gasoline', 'label': 'E 400 Coupe', 'status': 'SOURCE_ASSERTION'}
        self.search._catalog = Mock()
        self.search._catalog.resolve_vehicle.return_value = {
            'status': 'RESOLVED', 'candidates': [candidate], 'records': [candidate],
            'sources': [{'provider': 'Catalog API'}], 'unresolved_fields': [],
        }
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent(message)), self.finish(),
        ]
        result = self.run_turn(message)
        self.assertEqual(result['resolution_candidates'][0]['engine_code'], 'M276.850')
        self.assertNotIn('engine_code', result['customer_stated'])
        self.assertEqual(result['selected_configuration']['engine_code'], 'M276.850')
        self.assertNotIn('verified', result['answer'].lower())

    def test_model_question_and_choices_replace_searching(self):
        message = 'მერსედეს E კლასი 2015 ზეთი მინდა'
        fields = intent(message)
        fields['requested_parts'] = [{'category': 'oil_change', 'quote': 'ზეთი'}]
        question = 'რისი ზეთი გჭირდებათ და ძრავი ბენზინზეა თუ დიზელზე?'
        choices = ['ძრავის ზეთი', 'გადაცემათა კოლოფის ზეთი', 'რედუქტორის ზეთი']
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', fields),
            self.finish(question=question, choices=choices, question_field='part_type'),
        ]
        result = self.service.respond_conversation(message, language='ka',
                                                   search_service=self.search)
        self.assertIn(question, result['answer'])
        self.assertEqual(result['clarification_choices'], choices)
        self.assertEqual(result['pending']['field'], 'part_type')
        self.assertNotIn('ვერ დადასტურდა', result['answer'])
        self.search.start.assert_not_called()

    def test_model_question_that_states_unsupplied_facts_falls_back(self):
        message = 'Mercedes E coupe oil change'
        fields = {'fields': [{'field': 'make', 'value': 'Mercedes-Benz', 'quote': 'Mercedes'}],
                  'requested_parts': [{'category': 'oil_change', 'quote': 'oil change'}],
                  'references': [], 'correction': False}
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', fields),
            self.finish(question='Is it the M276 engine with 6.5 litres of 5W-40?',
                        choices=['Castrol EDGE 5W-30']),
        ]
        result = self.run_turn(message)
        self.assertNotIn('M276', result['answer'])
        self.assertNotIn('Castrol', json.dumps(result['clarification_choices']))
        self.assertTrue(result['diagnostics']['question_rejected'])
        self.assertEqual(result['pending']['field'], 'model')

    def test_web_part_numbers_find_warehouse_products_with_a_verification_warning(self):
        pad = {'source_product_id': 'b1', 'source_fingerprint': 'fresh', 'brand': 'TRW',
               'product_code': 'GDB1916', 'product_name': 'სამუხრუჭე ხუნდი წინა',
               'stock': None, 'price': None}
        self.repository.get_product.return_value = copy.deepcopy(pad)
        self.repository.search_code_tokens.return_value = {'GDB1916': [copy.deepcopy(pad)]}
        quote = 'Front brake pad set TRW GDB1916 for Mercedes-Benz E-Class Coupe (C207)'
        research = {'status': 'PROVISIONAL', 'citations': [], 'sources': [], 'records': [{
            'claim_id': 'research:1', 'statement': quote, 'supporting_excerpt': quote,
            'source_url': 'https://www.trwaftermarket.com/x', 'source_title': 'TRW catalog',
            'status': 'SOURCE_ASSERTION', 'applicability': {}}]}
        message = 'Mercedes E coupe 2015 front brake pads'
        fields = intent(message)
        fields['requested_parts'] = [{'category': 'brake_pads', 'quote': 'front brake pads',
                                      'axle': 'front'}]
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', fields), response('discover_parts', {}), self.finish(),
        ]
        with patch('automotive.research.AutomotiveResearch.run', return_value=research):
            result = self.run_turn(message)
        self.repository.search_code_tokens.assert_called_once()
        self.assertIn('GDB1916', self.repository.search_code_tokens.call_args.args[0])
        self.assertEqual([c['product']['product_code'] for c in result['cards']], ['GDB1916'])
        self.assertIn('Part number from a web source; please verify fitment.',
                      result['cards'][0]['warnings'])
        self.assertIsNone(result['cards'][0]['compatible'])

    def test_exhausted_model_requests_after_discovery_are_not_reported_as_unavailable(self):
        message = 'Mercedes E coupe 2015 3.0 engine oil change'
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent(message)), response('discover_parts', {}),
        ] + [response('search_inventory', {'category': 'oil_change'})] * 4
        self.repository.search_products.return_value = {'items': []}
        result = self.run_turn(message)
        self.assertEqual(result['usage']['model_requests'], 6)
        self.assertNotIn('LIMIT_EXHAUSTED', result['warnings'])
        self.assertNotIn('unavailable', result['answer'])

    def test_category_search_is_a_name_match_not_a_typed_code(self):
        gear_oil = {'source_product_id': 'g1', 'source_fingerprint': 'fresh',
                    'product_code': '866904', 'brand': 'Valvoline', 'stock': None,
                    'price': None, 'product_name': 'რედუქტორის ზეთი AXLE OIL 75W90'}
        self.repository.search_products.return_value = {'items': [gear_oil]}
        self.repository.get_product.return_value = copy.deepcopy(gear_oil)
        message = 'Mercedes E coupe 2015 3.0 engine oil change'
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent(message)),
            response('search_inventory', {'category': 'oil_change'}),
            self.finish(product_ids=['g1']),
            self.finish(),
        ]
        result = self.run_turn(message)
        self.assertEqual(result['cards'], [])
        from automotive.categories import matches_category

        self.assertFalse(matches_category(gear_oil, 'oil change'))
        self.assertTrue(matches_category({'product_name': 'ფილტრი ზეთის'}, 'oil change'))

    def test_missing_vehicle_details_are_asked_together(self):
        message = 'მერცედესზე მინდა ძრავის ზეთის შეცვლა'
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', {'fields': [
                {'field': 'make', 'value': 'Mercedes-Benz', 'quote': 'მერცედესზე'}],
                'requested_parts': [{'category': 'oil_change', 'quote': 'ზეთის შეცვლა'}],
                'references': [], 'correction': False}),
            self.finish(),
        ]
        result = self.service.respond_conversation(message, language='ka',
                                                   search_service=self.search)
        for detail in ('მოდელი', 'გამოშვების წელი', 'ძრავის მოცულობა', 'საწვავი'):
            self.assertIn(detail, result['answer'])
        self.assertNotIn('მარკა', result['answer'])
        self.assertEqual(result['answer'].count('?'), 0)
        self.assertEqual(result['pending']['field'], 'model')

    def test_unsupported_catalog_route_and_early_model_stop_still_run_discovery(self):
        from automotive.public_http import PublicSourceError

        message = 'მერსედესსზე E coupe 2015 3.0 engine oil change'
        self.search._catalog = Mock()
        self.search._catalog.resolve_vehicle.side_effect = PublicSourceError(
            'CAPABILITY_UNAVAILABLE')
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent(message)),
            SimpleNamespace(content=[{'type': 'text', 'text': 'I could not resolve it.'}],
                            stop_reason='end_turn', usage=None),
        ]
        result = self.run_turn(message)
        resolved = json.loads(
            self.client.messages.create.call_args.kwargs['messages'][2]['content'][0]['content'])
        self.assertEqual(resolved['data']['failure_reason'], 'CAPABILITY_UNAVAILABLE')
        self.search.start.assert_called_once()
        self.assertNotEqual(result['status'], 'FAILED')
        self.assertNotIn('UNGROUNDED_RESPONSE', result['warnings'])
        self.assertIn('SOURCE_FAILED:resolve_vehicle:CAPABILITY_UNAVAILABLE', result['warnings'])
        self.assertTrue(any(t.get('fallback') for t in result['diagnostics']['stage_timings']))

    def test_direct_greeting_and_exact_code_keep_zero_model_calls(self):
        self.assertEqual(self.run_turn('Hello')['usage']['model_requests'], 0)
        self.repository.search_parts.return_value = {'items': []}
        self.assertEqual(self.run_turn('AP104/2')['usage']['model_requests'], 0)
        self.client.messages.create.assert_not_called()

    def test_unavailable_ai_keeps_supplied_vehicle_and_clear_limitation(self):
        self.service.client = None
        result = self.run_turn('engine oil change', vehicle={
            'make': 'Mercedes-Benz', 'model': 'E-Class', 'model_year': 2015})
        self.assertEqual(result['vehicle']['model_year'], 2015)
        self.assertIn('NOT_CONFIGURED', result['warnings'])

    def test_partial_stock_cards_are_refreshed_and_web_text_cannot_supply_stock(self):
        product = {'source_product_id': 'p1', 'source_fingerprint': 'fresh',
                   'product_code': 'AP104/2', 'brand': 'Maker',
                   'product_name': 'Engine oil 5W-30', 'stock': None, 'price': None}
        self.repository.get_product.return_value = copy.deepcopy(product)
        self.search.start.return_value.update(items=[{
            'warehouse_records': [{'product': product, 'match_type': 'CODE_CANDIDATE',
                                    'status': 'UNKNOWN', 'compatible': None}],
            'supplier_records': [], 'source_names': ['Catalog API']}])
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', intent('Mercedes E coupe 2015 3.0 engine oil change')),
            response('discover_parts', {}), self.finish(result_ids=['r2'], product_ids=['p1']),
        ]
        result = self.run_turn('Mercedes E coupe 2015 3.0 engine oil change')
        self.assertIsNone(result['cards'][0]['product']['stock'])
        self.assertIsNone(result['cards'][0]['product']['price'])
        self.assertIsNone(result['cards'][0]['compatible'])
        self.repository.get_product.assert_called()


class StoredAIConversationTests(unittest.TestCase):
    """Exercise real store, orchestrator, extraction evidence, and follow-up state."""

    def setUp(self):
        from automotive.conversations import ConversationStore

        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.vehicle_specifications.return_value = []
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_engine_oils.return_value = {'items': []}
        self.repository.search_parts_many.return_value = {}
        self.repository.search_parts.return_value = {'items': []}
        self.client = Mock()
        self.service = ChatService(self.repository, Mock(), Settings(
            chat_orchestration=True, automotive_cache_path=''), self.client)
        self.search = Mock()
        self.search._catalog = None
        self.search.start.return_value = {
            'search_id': 'search', 'status': 'COMPLETE', 'vehicle': {}, 'sources': [],
            'items': [], 'fluids': [], 'next_offset': None,
        }
        self.store = ConversationStore(self.service, self.search)
        self.addCleanup(self.store.close)
        research = patch('automotive.research.AutomotiveResearch.run', return_value={
            'status': 'EMPTY', 'records': [], 'citations': [], 'sources': []})
        research.start()
        self.addCleanup(research.stop)
        self.cid = self.store.create()['conversation_id']

    def turn(self, message, fields, category=None):
        parts = ([{'category': category, 'quote': message.split()[-1]}] if category else [])
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', {
                'fields': [{'field': key, 'value': value, 'quote': quote}
                           for key, value, quote in fields],
                'requested_parts': parts, 'references': [], 'correction': False}),
            response('finish_response', {'result_ids': [], 'product_ids': [], 'claim_ids': [],
                                         'question_field': None, 'presentation': 'concise'}),
        ]
        submitted = self.store.submit(self.cid, message, language='en')
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = self.store.get(self.cid, submitted['turn_id'])
            if result['status'] != 'RUNNING':
                self.assertLessEqual(result['answer'].count('?'), 1)
                return result
            time.sleep(0.005)
        self.fail('Synthetic stored AI turn did not finish')

    @staticmethod
    def report(candidates):
        return {'status': 'RESOLVED' if len(candidates) == 1 else 'AMBIGUOUS',
                'candidates': candidates, 'records': candidates,
                'sources': [{'provider': 'Catalog API'}], 'unresolved_fields': []}

    def test_scattered_georgian_details_keep_original_quotes_and_one_question(self):
        first = self.turn('მერსედესსზე ძრავის ზეთის შეცვლა', [
            ('make', 'Mercedes-Benz', 'მერსედესსზე')], 'oil_change')
        self.assertEqual(first['vehicle'], {'make': 'Mercedes-Benz'})
        self.assertEqual(first['pending']['field'], 'model')
        second = self.turn('E-Class coupe', [
            ('model', 'E-Class', 'E-Class'), ('body', 'Coupe', 'coupe')])
        self.assertEqual(second['pending']['field'], 'model_year')
        third = self.turn('2015 3.0', [
            ('model_year', 2015, '2015'), ('displacement_l', 3.0, '3.0')])
        self.assertEqual(third['vehicle'], {
            'make': 'Mercedes-Benz', 'model': 'E-Class', 'body': 'Coupe',
            'model_year': 2015, 'displacement_l': 3.0})
        state = self.store._conversations[self.cid]
        self.assertEqual(state['customer_stated']['make']['quote'], 'მერსედესსზე')
        self.assertEqual(state['customer_stated']['displacement_l']['quote'], '3.0')
        self.assertEqual(state['category'], 'oil_change')
        self.assertNotIn('What make', third['answer'])
        self.assertGreater(third['vehicle_revision'], first['vehicle_revision'])

    def test_model_question_is_kept_as_pending_and_the_answer_reaches_the_model(self):
        def finish(**extra):
            return response('finish_response', {
                'result_ids': [], 'product_ids': [], 'claim_ids': [], 'presentation': 'concise',
                **extra})

        stated = [{'field': 'make', 'value': 'Mercedes-Benz', 'quote': 'Mercedes'},
                  {'field': 'model', 'value': 'E-Class', 'quote': 'E-Class'},
                  {'field': 'model_year', 'value': 2015, 'quote': '2015'}]
        self.client.messages.create.side_effect = [
            response('resolve_vehicle', {'fields': stated, 'references': [], 'correction': False,
                                         'requested_parts': [{'category': 'oil_change',
                                                              'quote': 'oil'}]}),
            finish(question='Which oil do you need?', question_field='part_type',
                   choices=['Engine oil', 'Gearbox oil']),
        ]
        first = self.store.submit(self.cid, 'Mercedes E-Class 2015 oil', language='en')
        until = time.monotonic() + 3
        while self.store.get(self.cid, first['turn_id'])['status'] == 'RUNNING':
            self.assertLess(time.monotonic(), until)
            time.sleep(0.005)
        asked = self.store.get(self.cid, first['turn_id'])
        self.assertEqual(asked['clarification_choices'], ['Engine oil', 'Gearbox oil'])
        self.search.start.assert_not_called()
        second = self.turn('Engine oil', [], 'engine_oil')
        sent = json.loads(self.client.messages.create.call_args_list[2].kwargs[
            'messages'][0]['content'])
        self.assertEqual(sent['pending']['question'], 'Which oil do you need?')
        self.assertEqual(self.store._conversations[self.cid]['category'], 'engine_oil')
        self.search.start.assert_called()
        self.assertIsNone(second['pending'])

    def test_short_fuel_answer_selects_variant_without_becoming_an_engine_statement(self):
        petrol = {'engine_code': 'M276.850', 'fuel': 'Gasoline', 'label': 'E 400 petrol'}
        diesel = {'engine_code': 'OM642', 'fuel': 'Diesel', 'label': 'E 350 diesel'}
        self.search._catalog = Mock()
        self.search._catalog.resolve_vehicle.side_effect = lambda vehicle, **_: self.report(
            [diesel] if vehicle.get('fuel') == 'Diesel' else [petrol, diesel])
        first = self.turn('Mercedes E-Class 2015 3.0 engine oil', [
            ('make', 'Mercedes-Benz', 'Mercedes'), ('model', 'E-Class', 'E-Class'),
            ('model_year', 2015, '2015'), ('displacement_l', 3.0, '3.0')], 'oil_change')
        self.assertEqual(first['pending']['field'], 'fuel')
        second = self.turn('diesel', [('fuel', 'Diesel', 'diesel')])
        self.assertEqual(second['vehicle']['engine_code'], 'OM642')
        self.assertEqual(second['vehicle']['displacement_l'], 3.0)
        stated = self.store._conversations[self.cid]['customer_stated']
        self.assertEqual(stated['fuel']['quote'], 'diesel')
        self.assertNotIn('engine_code', stated)
        self.assertIsNone(second['pending'])

    def test_bare_engine_answer_follows_pending_engine_question_instead_of_part_search(self):
        candidates = [{'engine_code': code, 'fuel': 'Gasoline', 'label': code}
                      for code in ('M276.850', 'M276.820')]
        self.search._catalog = Mock()
        self.search._catalog.resolve_vehicle.side_effect = lambda vehicle, **_: self.report(
            [candidate for candidate in candidates
             if not vehicle.get('engine_code')
             or candidate['engine_code'] == vehicle['engine_code']])
        first = self.turn('Mercedes E-Class 2015 engine oil', [
            ('make', 'Mercedes-Benz', 'Mercedes'), ('model', 'E-Class', 'E-Class'),
            ('model_year', 2015, '2015')], 'oil_change')
        self.assertEqual(first['pending']['field'], 'engine_code')
        second = self.turn('M276.850', [('engine_code', 'M276.850', 'M276.850')])
        self.assertEqual(second['vehicle']['engine_code'], 'M276.850')
        self.assertGreater(second['usage']['model_requests'], 0)
        self.assertEqual(self.store._conversations[self.cid]['customer_stated'][
            'engine_code']['quote'], 'M276.850')
        self.repository.search_parts.assert_not_called()

    def test_vehicle_switch_discards_old_engine_and_preserves_source_statement_boundary(self):
        self.search._catalog = Mock()
        self.search._catalog.resolve_vehicle.side_effect = lambda vehicle, **_: self.report([
            {'engine_code': 'A25A-FKS' if vehicle.get('make') == 'Toyota' else 'M276.850',
             'fuel': 'Gasoline', 'status': 'SOURCE_ASSERTION'}])
        first = self.turn('Mercedes E-Class 2015 oil', [
            ('make', 'Mercedes-Benz', 'Mercedes'), ('model', 'E-Class', 'E-Class'),
            ('model_year', 2015, '2015')], 'oil_change')
        self.assertEqual(first['vehicle']['engine_code'], 'M276.850')
        self.assertNotIn('engine_code', self.store._conversations[self.cid]['customer_stated'])
        second = self.turn('Toyota Camry 2020 filter', [
            ('make', 'Toyota', 'Toyota'), ('model', 'Camry', 'Camry'),
            ('model_year', 2020, '2020')], 'cabin_filter')
        self.assertEqual(second['vehicle']['engine_code'], 'A25A-FKS')
        self.assertNotIn('M276', json.dumps(second))
        third = self.turn('Still the Toyota', [])
        stated = self.store._conversations[self.cid]['customer_stated']
        self.assertEqual(stated['make']['value'], 'Toyota')
        self.assertNotIn('engine_code', stated)
        self.assertEqual(third['selected_configuration']['engine_code'], 'A25A-FKS')
        self.assertGreater(second['vehicle_revision'], first['vehicle_revision'])
