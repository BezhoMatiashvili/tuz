"""Reply sentences: natural, specific, one question at most and only verified facts."""

from __future__ import annotations

import unittest

from automotive import grounding, replies

CAR = {'make': 'HONDA', 'model': 'Accord', 'model_year': 2015}


def card(identifier, state, brand='TRW', code='GDB3520', basis='code'):
    return {'product': {'source_product_id': identifier, 'brand': brand, 'product_code': code,
                        'product_name': 'ხუნდი'}, 'availability_status': state, 'basis': basis}


def compose(act, language='en', **fields):
    fields.setdefault('vehicle', CAR)
    return replies.compose(replies.Situation(act=act, language=language, **fields))


class ReplyTests(unittest.TestCase):
    def test_results_name_the_car_count_and_where_items_are(self):
        reply = compose('results', category='front brake pads',
                        cards=[card('a', 'IN_STORE'), card('b', 'WITHIN_1_2_HOURS')])
        self.assertIn('2 front brake pads for your 2015 Honda Accord', reply.text)
        self.assertIn('1 is in the store now, and 1 can be here from our warehouse in 1-2 hours',
                      reply.text)
        self.assertTrue(reply.text.endswith('Need the rear ones too?'))
        self.assertEqual(reply.offer, {'category': 'rear brake pads'})

    def test_georgian_results_are_native_and_specific(self):
        reply = compose('results', 'ka', category='წინა ხუნდები',
                        cards=[card('a', 'IN_STORE')])
        self.assertIn('თქვენი 2015 წლის Honda Accord-ისთვის', reply.text)
        self.assertIn('ახლა მაღაზიაშია', reply.text)
        self.assertIn('უკანაც გჭირდებათ?', reply.text)

    def test_phrasing_rotates_instead_of_repeating(self):
        cards = [card('a', 'IN_STORE')]
        first = compose('results', category='air filter', cards=cards).text
        second = compose('results', category='air filter', cards=cards, previous=[first]).text
        self.assertNotEqual(first, second)

    def test_typed_codes_and_missing_codes(self):
        found = compose('results', vehicle={}, codes=['AP104/2'],
                        cards=[card('a', 'UNKNOWN', brand='Maker', code='AP104/2')])
        self.assertEqual(found.text, "AP104/2 (Maker) is in our catalog. I can't confirm stock "
                                     'right now — the store can check.')
        stocked = compose('results', vehicle={}, codes=['AP104/2'],
                          cards=[card('a', 'IN_STORE', brand='Maker', code='AP104/2')])
        self.assertEqual(stocked.text, "Yes, we have AP104/2 (Maker). It's in the store now.")
        missing = compose('no_results', vehicle={}, codes=['XYZ9999'])
        self.assertIn("I couldn't find XYZ9999", missing.text)

    def test_one_note_and_at_most_one_question(self):
        reply = compose('results', category='front brake pads', cards=[card('a', 'IN_STORE')],
                        warnings=['SOURCE_FAILED:motorium', 'STOCK_MAY_BE_STALE',
                                  'VIN_NOT_PROVIDED'])
        self.assertEqual(reply.note,
                         "I couldn't reach every supplier just now, so there may be more options.")
        for act in ('clarify_vehicle', 'clarify_year', 'clarify_brakes', 'clarify_axle',
                    'vehicle_identified', 'thanks', 'greeting', 'no_vehicle_codes'):
            self.assertLessEqual(compose(act, category='brakes').text.count('?'), 1, act)

    def test_a_note_is_said_once_per_conversation(self):
        warnings = ['STOCK_MAY_BE_STALE']
        first = compose('results', category='air filter', cards=[card('a', 'IN_STORE')],
                        warnings=warnings)
        again = compose('results', category='air filter', cards=[card('a', 'IN_STORE')],
                        warnings=warnings, previous_notes=[first.note])
        self.assertTrue(first.note)
        self.assertEqual(again.note, '')

    def test_out_of_stock_code_is_not_contradictory_and_dead_ends_offer_a_next_step(self):
        gone = compose('results', vehicle={}, codes=['AP104/2'],
                       cards=[card('a', 'UNAVAILABLE', brand='FILTRON', code='AP104/2')])
        self.assertEqual(gone.text, "We carry AP104/2 (Filtron), but it's out of stock right now.")
        self.assertIn('Send me the VIN', compose('no_vehicle_codes', category='wipers').text)
        self.assertIn("old part's number",
                      compose('no_vehicle_codes', category='wipers', has_vin=True).text)
        self.assertIn('when more arrive', compose('none_in_stock', category='rear discs').text)

    def test_engine_question_does_not_ask_for_a_vin_already_given(self):
        self.assertIn('send the VIN', compose('clarify_engine', category='engine oil').text)
        given = compose('clarify_engine', category='engine oil', has_vin=True).text
        self.assertNotIn('VIN.', given.replace('from the VIN', ''))
        self.assertIn('engine code', given)

    def test_an_ambiguous_engine_is_asked_by_name_with_choices(self):
        options = [{'trim': 'E 320', 'fuel': 'Gasoline'},
                   {'trim': 'E 350 BlueTEC', 'fuel': 'Diesel'},
                   {'trim': 'E 400', 'fuel': 'Gasoline'}]
        car = {'make': 'MERCEDES-BENZ', 'model': 'E-Class', 'model_year': 2015}
        ka = compose('clarify_engine', 'ka', category='ზეთის შეცვლა', vehicle=car,
                     engine_options=options)
        self.assertIn('E 320 (ბენზინი), E 350 BlueTEC (დიზელი) თუ E 400 (ბენზინი)?', ka.text)
        self.assertNotIn('ძრავის კოდი', ka.text)
        self.assertEqual(ka.quick_replies,
                         ['E 320 (ბენზინი)', 'E 350 BlueTEC (დიზელი)', 'E 400 (ბენზინი)'])
        en = compose('clarify_engine', category='oil change', vehicle=car, engine_options=options)
        self.assertIn('2015 Mercedes-Benz E-Class came with several engines', en.text)
        self.assertEqual(en.text.count('?'), 1)

    def test_name_matched_reply_is_short_and_says_to_check(self):
        reply = compose('results', category='front brake pads', name_only=True,
                        cards=[card('a', 'IN_STORE', basis='name')])
        self.assertTrue(reply.text.startswith('1 front brake pad set is listed for your 2015 '
                                              'Honda Accord, but without an exact part number'),
                        reply.text)
        self.assertLess(len(reply.text), 200)
        again = compose('results', category='rear brake pads', name_only=True,
                        cards=[card('b', 'IN_STORE', basis='name')], previous=[reply.text])
        self.assertTrue(again.text.startswith('Here is 1 rear brake pad set for your 2015 Honda '
                                              'Accord — again, please check the fit.'), again.text)
        self.assertFalse(again.text.endswith('ones too?'), 'The offer is not asked twice running')

    def test_clarifications_offer_quick_replies(self):
        self.assertEqual(compose('clarify_axle', category='pads').quick_replies,
                         ['Front', 'Rear'])
        self.assertEqual(len(compose('clarify_brakes', 'ka').quick_replies), 4)
        self.assertEqual(compose('clarify_year', vehicle={'make': 'HONDA', 'model': 'Accord'})
                         .text, 'Got it, a Honda Accord. What year is it?')

    def test_oil_reply_states_the_requirement_and_what_is_missing(self):
        reply = compose('oil_resolved', category='oil change',
                        requirements={'engine_code': 'K24W', 'grades': ['0W-20'],
                                      'capacity_l': 4.2, 'missing': ['oil_filter']},
                        cards=[card('o', 'IN_STORE', basis='oil_grade_brand')])
        self.assertIn('(K24W) takes 0W-20, about 4.2 L', reply.text)
        self.assertIn("couldn't find a matching oil filter", reply.text)

    def test_reference_and_sold_out_reference(self):
        shown = compose('reference', cards=[card('b', 'WITHIN_1_2_HOURS', brand='BREMBO',
                                                 code='P28076')])
        self.assertTrue(shown.text.startswith("That's the Brembo one (P28076)."))
        gone = compose('reference', referenced=[{'brand': 'BREMBO'}], sold_out=1)
        self.assertEqual(gone.text, "Sorry, the Brembo one isn't in stock anymore.")
        changed = compose('reference', referenced=[{'brand': 'BREMBO'}])
        self.assertIn("isn't listed for your car anymore", changed.text)
        failed = compose('reference', referenced=[{'brand': 'BREMBO'}], status='DATA_UNAVAILABLE')
        self.assertIn("couldn't check that one", failed.text)

    def test_every_composed_reply_is_grounded_in_its_own_facts(self):
        situations = [
            ('results', {'category': 'front brake pads',
                         'cards': [card('a', 'IN_STORE'), card('b', 'UNKNOWN')]}),
            ('none_in_stock', {'category': 'rear brake discs'}),
            ('no_vehicle_codes', {'category': 'wipers'}),
            ('vehicle_identified', {}), ('clarify_vehicle', {'vehicle': {}}),
            ('greeting', {}), ('thanks', {}), ('identity', {}),
            ('oil_resolved', {'requirements': {'grades': ['5W-40'], 'capacity_l': 6.5},
                              'cards': [card('o', 'IN_STORE', basis='oil_grade')]}),
        ]
        for language in ('en', 'ka'):
            for act, fields in situations:
                reply = compose(act, language, **fields)
                payload = {'cards': fields.get('cards', []),
                           'vehicle': fields.get('vehicle', CAR),
                           'requirements': fields.get('requirements', {}), 'facts': reply.facts}
                allowed = grounding.facts_text(payload)
                found = grounding.problems(f'{reply.text} {reply.note}', allowed, language)
                self.assertEqual(found, [], f'{language} {act}: {reply.text}')


if __name__ == '__main__':
    unittest.main()
