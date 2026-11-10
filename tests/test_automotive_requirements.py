"""Stock rule, engine matching and requirement-to-catalog matching (synthetic data only)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from automotive import engine_match, oil_match, stock
from automotive.categories import category_for_query, matches_category
from automotive.oats import OatsCatalog
from automotive.repository import _product
from tests.test_automotive_fluids import FakeHttp

COUPE = 'E-CLASS Coupe (C207) E 400 245kW (M 276.850) (P) (2015)'


class StockTests(unittest.TestCase):
    def test_store_beats_tuz11_and_tuz11_means_one_to_two_hours(self):
        self.assertEqual(stock.classify({'store': 8, 'tuz11': 0}), stock.IN_STORE)
        self.assertEqual(stock.classify({'store': 0, 'tuz11': 2}), stock.WITHIN_HOURS)
        self.assertEqual(stock.classify({'store': 3, 'tuz11': 9}), stock.IN_STORE)
        self.assertEqual(stock.classify({'store': 0, 'tuz11': 0}), stock.UNAVAILABLE)

    def test_missing_or_non_numeric_cells_are_never_a_statement_of_stock(self):
        self.assertEqual(stock.classify(None), stock.UNKNOWN)
        self.assertEqual(stock.classify({'store': None, 'tuz11': 0}), stock.UNKNOWN)
        self.assertEqual(stock.classify({'store': 'many', 'tuz11': '-1'}), stock.UNKNOWN)
        self.assertIsNone(stock.parse_count(True))
        self.assertEqual(stock.parse_count(' 2,5 '), 2.5)

    def test_tuz6_is_not_part_of_the_stock_record(self):
        product = _product({'source_product_id': 'p', 'stock_store': '0', 'stock_tuz11': '0',
                            'stock_tuz6': '9'})
        self.assertEqual(product['stock'], {'store': 0.0, 'tuz11': 0.0})
        self.assertEqual(stock.classify(product['stock']), stock.UNAVAILABLE)

    def test_catalog_rows_without_stock_columns_carry_no_stock(self):
        self.assertIsNone(_product({'source_product_id': 'p'})['stock'])
        product = _product({'source_product_id': 'p', 'stock_store': '16', 'stock_tuz11': 'x'})
        self.assertEqual(product['stock'], {'store': 16.0, 'tuz11': None})

    def test_text_is_bilingual_and_states_the_one_to_two_hour_rule(self):
        self.assertEqual(stock.availability_text({'store': 16, 'tuz11': 0}, 'en'),
                         'In the store: 16')
        self.assertIn('1-2 hours', stock.availability_text({'store': 0, 'tuz11': 2}, 'en'))
        self.assertIn('1–2 საათში', stock.availability_text({'store': 0, 'tuz11': 2}, 'ka'))
        self.assertIn('Not available now', stock.availability_text({'store': 0, 'tuz11': 0}, 'en'))
        self.assertIn('unconfirmed', stock.availability_text(None, 'en'))

    def test_observations_older_than_thirteen_hours_are_stale(self):
        now = datetime(2026, 11, 9, 12, tzinfo=UTC)
        self.assertTrue(stock.is_stale(now - timedelta(hours=14), now))
        self.assertFalse(stock.is_stale(now - timedelta(hours=12), now))
        self.assertFalse(stock.is_stale('2020-01-01', now))


class EngineMatchTests(unittest.TestCase):
    def test_trim_is_matched_ignoring_spaces_but_not_longer_numbers(self):
        self.assertTrue(engine_match.trim_matches('E 400 4-matic 245kW', 'E400'))
        self.assertTrue(engine_match.trim_matches('E-CLASS E400 (P)', 'E 400'))
        self.assertFalse(engine_match.trim_matches('Class E 4000', 'E400'))
        self.assertFalse(engine_match.trim_matches('anything', 'E4'))
        self.assertEqual(engine_match.trim_search_text('E400'), 'E 400')

    def test_trim_after_the_class_and_never_the_power(self):
        # Shell and Pakelo labels as returned for these cars.
        self.assertTrue(engine_match.trim_matches('GLE (W166) 350 d 4-matic 183kW', 'GLE 350d'))
        self.assertTrue(engine_match.trim_matches('IS II 250 149kW', 'IS 250'))
        self.assertTrue(engine_match.trim_matches('G Coupe 37 235kW', 'G 37'))
        self.assertTrue(engine_match.trim_matches('3 (F30, F80) 328 i 180kW', '328i'))
        self.assertFalse(engine_match.trim_matches('GLC (X253) 350 d', 'GLE 350d'))
        self.assertFalse(engine_match.trim_matches('E-CLASS (W212) E 63 AMG 400kW', 'E 400'))

    def test_supplier_model_names(self):
        matches = [('E-CLASS (W212)', 'E-Class'), ('GLE (W166)', 'GLE-Class'),
                   ('GLE-Class Coupé', 'GLE-Class'), ('3 (F30, F80) 328 i', '3 Series'),
                   ('3 series Gran Turismo', '3 Series'), ('3 Gran Turismo (F34)', '3-Series'),
                   ('IS II 250 149kW', 'IS'), ('CR-V', 'CRV'), ('RX 350 202kW', 'RX350'),
                   ('RANGE ROVER SPORT II', 'Range Rover Sport'), ('G25 / G35 / G37 / G', 'G'),
                   ('3 2.0 110kW', '3')]
        for label, model in matches:
            self.assertTrue(engine_match.model_matches(label, model), (label, model))
        misses = [('CLE-Class', 'E-Class'), ('GLS-Class', 'S-Class'), ('CX-3 2.0', '3'),
                  ('CX-30 2.0', '3'), ('A45 AMG', 'A4'), ('B-CLASS B 200 c', 'C-Class')]
        for label, model in misses:
            self.assertFalse(engine_match.model_matches(label, model), (label, model))

    def test_search_terms_use_the_spelling_in_supplier_labels(self):
        self.assertEqual(engine_match.search_terms('GLE-Class', 'GLE 350d'),
                         ['GLE 350 d', 'GLE-Class GLE 350 d', 'GLE-Class', 'GLE'])
        self.assertEqual(engine_match.search_terms('3 Series', '328i')[0], '328 i')
        self.assertEqual(engine_match.search_terms('IS', 'IS 250')[0], 'IS 250')
        self.assertEqual(engine_match.search_terms('Car', 'E400'), ['Car E 400', 'Car'])
        self.assertEqual(engine_match.search_terms('3 Series', None), ['3 Series', '3'])
        self.assertEqual(engine_match.search_terms('Accord', None), ['Accord'])
        self.assertEqual(engine_match.search_terms('E-Class', None, 'Coupe'),
                         ['E-Class Coupe', 'E-Class', 'E'])

    def test_engine_choices_from_supplier_labels(self):
        self.assertEqual(engine_match.engine_option(
            'E-CLASS Coupe (C207) E 350 BlueTEC / d 190kW (D) (2015)'),
            {'trim': 'E 350 BlueTEC / d', 'fuel': 'Diesel'})
        self.assertEqual(engine_match.engine_option('3 (F30, F80) 328 i xDrive 180kW (P) (2014)'),
                         {'trim': '328 i xDrive', 'fuel': 'Gasoline'})
        self.assertEqual(engine_match.engine_option('IS 250 RWD (USA)'),
                         {'trim': 'IS 250 RWD', 'fuel': None})
        self.assertTrue(engine_match.trim_matches('E 350 BlueTEC / d 190kW (D)',
                                                  'E 350 BlueTEC / d'))
        self.assertEqual(engine_match.engine_options([
            'E-CLASS Coupe (C207) E 350 BlueTEC / d 190kW (D) (2015)',
            'E-CLASS Coupe (C207) E 350 BlueTEC 185kW (D) (2015)',
            'E-CLASS Coupe (C207) E 350 245kW (P) (2015)']),
            [{'trim': 'E 350 BlueTEC', 'fuel': 'Diesel'}, {'trim': 'E 350', 'fuel': 'Gasoline'}])

    def test_fuel_marker_must_agree_when_present(self):
        self.assertTrue(engine_match.fuel_matches('E 400 (P)', 'Gasoline'))
        self.assertTrue(engine_match.fuel_matches('E 400 (B)', 'Gasoline'))
        self.assertFalse(engine_match.fuel_matches('E 350 BlueTEC (D)', 'Gasoline'))
        self.assertTrue(engine_match.fuel_matches('E 350', 'Gasoline'))

    def test_engine_code_comes_from_the_label_and_chassis_codes_are_ignored(self):
        self.assertEqual(engine_match.engine_code(COUPE), 'M 276.850')
        self.assertIsNone(engine_match.engine_code('E-CLASS (W212) E 400 (P) (2015)'))
        self.assertEqual(engine_match.engine_family('M 276.850'), 'M276')
        self.assertIsNone(engine_match.engine_family('276'))

    def test_narrowing_uses_trim_fuel_and_body_not_displacement(self):
        labels = [
            {'l': 'E-CLASS (W212) E 350 BlueTEC (D)'},
            {'l': 'E-CLASS (W212) E 400 (M 276.850) (P)'},
            {'l': 'E-CLASS Coupe (C207) E 400 (M 276.850) (P)'},
            {'l': 'E-CLASS Convertible (A207) E 400 (M 276.850) (P)'},
        ]
        vehicle = {'trim': 'E400', 'fuel': 'Gasoline', 'body': 'Coupe', 'displacement_l': '3.0'}
        chosen = engine_match.narrow(labels, lambda item: item['l'], vehicle)
        self.assertEqual([item['l'] for item in chosen],
                         ['E-CLASS Coupe (C207) E 400 (M 276.850) (P)'])
        # A trim that exists only as a diesel is a disagreement, not a match.
        self.assertEqual(engine_match.narrow(labels[:1] + [{'l': 'E 400 (D)'}],
                                             lambda item: item['l'], vehicle), [])

    def test_a_stated_engine_code_keeps_only_its_family(self):
        labels = [{'l': 'E-CLASS (W212) E 350 (M 272.980) (P)'},
                  {'l': 'E-CLASS (W212) E 400 (M 276.820) (P)'},
                  {'l': 'E-CLASS (W212) E 300 (P)'}]
        chosen = engine_match.narrow(labels, lambda item: item['l'], {'engine_code': 'M276'})
        self.assertEqual([item['l'] for item in chosen], [labels[1]['l']])
        self.assertTrue(engine_match.engine_matches(labels[1]['l'], 'M 276.850'))
        self.assertFalse(engine_match.engine_matches(labels[0]['l'], 'M276'))
        # Labels without a code are unknown, not contradicted; a contradicted code is no match.
        self.assertEqual(engine_match.narrow(labels[::2], lambda item: item['l'],
                                             {'engine_code': 'M276'}), [labels[2]])
        self.assertEqual(engine_match.narrow(labels[:1], lambda item: item['l'],
                                             {'engine_code': 'M276'}), [])


def detail(*products):
    return {'equipment': {'display_name_long': 'x', 'application': [{
        'name': 'Engine', 'display_capacity': '6.5',
        'product': [{'name': name, 'external_id': name[:4]} for name in products],
    }]}}


def item(identifier, label, displacement='3.0'):
    return {'@id': identifier, 'manufacturer': 'Example', 'model': label,
            'display_name_long': label, 'yearfrom': 2012, 'yearto': 2016,
            'displacement': [displacement]}


class ChooserResolutionTests(unittest.TestCase):
    vehicle = {'make': 'Example', 'model': 'Car', 'model_year': 2015, 'trim': 'E400',
               'fuel': 'Gasoline', 'displacement_l': '3.0'}

    def client(self, equipment, *details):
        client = OatsCatalog('Shell', 'https://example.test/oats', 'en_GB')
        client.http = FakeHttp([(200, {'equipment_list': {'equipment': equipment}}),
                                *[(200, d) for d in details]])
        return client

    def test_trim_and_fuel_select_the_petrol_engine_despite_a_displacement_disagreement(self):
        client = self.client(
            [item('diesel1', 'Car E 350 BlueTEC 190kW (D) (2015)', '3.0'),
             item('diesel2', 'Car E 350 BlueTEC 4-matic (D) (2015)', '3.0'),
             item('petrol1', 'Car E 400 245kW (M 276.850) (P) (2015)', '3.5')],
            detail('Helix Ultra 0W-40 (SP)', 'Helix Ultra 5W-40'))
        result = client.lookup(None, self.vehicle)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(result['engine_code'], 'M 276.850')
        self.assertEqual([i['viscosity'] for i in result['items']], ['0W-40', '5W-40'])
        self.assertIn('E 400', client.http.calls[0][2]['q'])
        self.assertEqual(len(client.http.calls), 2)

    def test_body_type_picks_the_decoded_coupe_among_same_engine_variants(self):
        client = self.client(
            [item('sedan1', 'Car (W212) E 400 245kW (M 276.850) (P) (2015)'),
             item('coupe1', 'Car Coupe (C207) E 400 245kW (M 276.850) (P) (2015)'),
             item('convert1', 'Car Convertible (A207) E 400 245kW (M 276.850) (P) (2015)')],
            detail('Helix Ultra 5W-40'))
        result = client.lookup(None, {**self.vehicle, 'body': 'Coupe'})
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(len(client.http.calls), 2)

    def test_variants_with_identical_recommendations_are_accepted(self):
        client = self.client(
            [item('first1', 'Car E 400 245kW (M 276.850) (P) (2015)'),
             item('second1', 'Car E 400 4-matic 245kW (M 276.850) (P) (2015)')],
            detail('Helix Ultra 5W-40'), detail('Helix Ultra 5W-40'))
        self.assertEqual(client.lookup(None, self.vehicle)['status'], 'PROVISIONAL')

    def test_variants_with_different_recommendations_stay_conflicted(self):
        client = self.client(
            [item('first1', 'Car E 400 245kW (M 276.850) (P) (2015)'),
             item('second1', 'Car E 400 4-matic 245kW (M 276.850) (P) (2015)')],
            detail('Helix Ultra 5W-40'), detail('Helix HX7 10W-40'))
        result = client.lookup(None, self.vehicle)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'], [])

    def test_a_typed_class_and_trim_find_the_one_engine_across_body_styles(self):
        def mercedes(identifier, model, label):
            return {**item(identifier, label), 'manufacturer': 'MERCEDES-BENZ', 'model': model}

        equipment = [
            mercedes('sedan1', 'E-CLASS (W212)', 'E-CLASS (W212) E 400 (M 276.820) (P) (2015)'),
            mercedes('coupe1', 'E-CLASS Coupe (C207)',
                     'E-CLASS Coupe (C207) E 400 (M 276.820) (P) (2015)'),
            mercedes('diesel1', 'E-CLASS (W212)', 'E-CLASS (W212) E 250 BlueTEC (D) (2015)'),
        ]
        typed = {'make': 'MERCEDES-BENZ', 'model': 'E-Class', 'model_year': 2015,
                 'trim': 'E 400'}
        client = self.client(equipment, detail('Helix Ultra 5W-40'), detail('Helix Ultra 5W-40'))
        result = client.lookup(None, typed)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(result['engine_code'], 'M 276.820')
        self.assertIn('E 400', client.http.calls[0][2]['q'])
        # The old reading of "Mercedes Benz E400" matched no chooser model at all.
        client = self.client(equipment)
        self.assertEqual(client.lookup(None, {**typed, 'model': 'Benz', 'trim': None})['status'],
                         'EMPTY')

    def test_a_stated_engine_code_decides_without_a_trim(self):
        client = self.client(
            [item('first1', 'Car 350 (M 272.980) (P) (2015)', '3.5'),
             item('second1', 'Car 400 (M 276.820) (P) (2015)'),
             item('third1', 'Car 400 4-matic (M 276.820) (P) (2015)')],
            detail('Helix Ultra 5W-40'), detail('Helix Ultra 5W-40'))
        vehicle = {'make': 'Example', 'model': 'Car', 'model_year': 2015, 'engine_code': 'M276'}
        result = client.lookup(None, vehicle)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(result['engine_code'], 'M 276.820')

    def test_a_stated_engine_size_decides_when_one_engine_code_remains(self):
        client = self.client(
            [item('first1', 'Car 250 (M 274.920) (P) (2015)', '2.0'),
             item('second1', 'Car 400 (M 276.820) (P) (2015)'),
             item('third1', 'Car 400 Coupe (M 276.820) (P) (2015)')],
            detail('Helix Ultra 5W-40'), detail('Helix Ultra 5W-40'))
        vehicle = {'make': 'Example', 'model': 'Car', 'model_year': 2015, 'displacement_l': '3.0',
                   'fuel': 'Gasoline'}
        result = client.lookup(None, vehicle)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(result['engine_code'], 'M 276.820')

    def test_a_size_without_the_fuel_asks_and_offers_the_engines_near_it(self):
        # Shell's E-Class coupe 2015 list: the petrol E 400 is a 3.0 L listed as 3.5 L.
        def coupe(identifier, label, size):
            return {**item(identifier, label, size), 'manufacturer': 'MERCEDES-BENZ',
                    'model': 'E-CLASS Coupe (C207)'}

        equipment = [
            coupe('e200p', 'E-CLASS Coupe (C207) E 200 135kW (P) (2015)', '2.0'),
            coupe('e320p', 'E-CLASS Coupe (C207) E 320 200kW (P) (2015)', '3.0'),
            coupe('e350d', 'E-CLASS Coupe (C207) E 350 BlueTEC 185kW (D) (2015)', '3.0'),
            coupe('e400p', 'E-CLASS Coupe (C207) E 400 245kW (M 276.850) (P) (2015)', '3.5'),
            coupe('e500p', 'E-CLASS Coupe (C207) E 500 300kW (P) (2015)', '4.7'),
        ]
        vehicle = {'make': 'MERCEDES-BENZ', 'model': 'E-Class', 'model_year': 2015,
                   'body': 'Coupe', 'displacement_l': '3.0'}
        client = self.client(equipment)
        result = client.lookup(None, vehicle)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(len(client.http.calls), 1)  # Nothing is read or guessed.
        self.assertEqual(client.http.calls[0][2]['q'], 'MERCEDES-BENZ E-Class Coupe 2015')
        options = engine_match.engine_options(result['candidates'])
        self.assertEqual(options, [{'trim': 'E 320', 'fuel': 'Gasoline'},
                                   {'trim': 'E 350 BlueTEC', 'fuel': 'Diesel'},
                                   {'trim': 'E 400', 'fuel': 'Gasoline'}])

    def test_every_named_variant_is_read_and_must_agree_without_an_engine_code(self):
        def gle(identifier, label):
            return {**item(identifier, label), 'manufacturer': 'MERCEDES-BENZ',
                    'model': label.split(' 350', 1)[0]}

        equipment = [gle('suv183', 'GLE (W166) 350 d 4-matic 183kW (D) (2015)'),
                     gle('suv190', 'GLE (W166) 350 d 4-matic 190kW (D) (2015)'),
                     gle('coupe183', 'GLE Coupe (C292) 350 d 4-matic 183kW (D) (2015)'),
                     gle('coupe190', 'GLE Coupe (C292) 350 d 4-matic 190kW (D) (2015)')]
        vehicle = {'make': 'MERCEDES-BENZ', 'model': 'GLE-Class', 'model_year': 2015,
                   'trim': 'GLE 350d'}
        same = detail('Helix Ultra ECT C3 0W-30')
        client = self.client(equipment, same, same, same, same)
        result = client.lookup(None, vehicle)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(len(client.http.calls), 5)  # The search and all four variants.
        self.assertEqual(client.http.calls[0][2]['q'], 'MERCEDES-BENZ GLE 350 d 2015')
        # A variant that disagrees stops the reads: nothing is guessed.
        client = self.client(equipment, same, detail('Helix HX8 5W-40'))
        self.assertEqual(client.lookup(None, vehicle)['status'], 'CONFLICTED')
        self.assertEqual(len(client.http.calls), 3)

    def test_more_than_eight_variants_are_not_read(self):
        equipment = [item(f'v{n}aa', f'Car E 400 {200 + n}kW (P) (2015)') for n in range(9)]
        client = self.client(equipment)
        self.assertEqual(client.lookup(None, self.vehicle)['status'], 'CONFLICTED')
        self.assertEqual(len(client.http.calls), 1)

    def test_no_trim_means_several_engines_are_not_read_or_guessed(self):
        client = self.client([item('first1', 'Car 2.0 (P)', '2.0'),
                              item('second1', 'Car 2.4 (P)', '2.4')])
        vehicle = {k: v for k, v in self.vehicle.items()
                   if k not in {'trim', 'displacement_l'}}
        self.assertEqual(client.lookup(None, vehicle)['status'], 'CONFLICTED')
        self.assertEqual(len(client.http.calls), 1)


class CategoryTests(unittest.TestCase):
    def test_oil_change_phrases_in_both_languages_are_a_bundle(self):
        for text in ('I need an oil change', 'oil change', 'ზეთის შეცვლა მინდა',
                     'ზეთის გამოცვლა', 'მინდა ზეთის შევცვალო'):
            self.assertEqual(category_for_query(text), 'oil_change', text)
        self.assertEqual(category_for_query('engine oil'), 'engine_oil')
        self.assertEqual(category_for_query('ზეთის ფილტრი'), 'oil_filter')

    def test_the_bundle_covers_the_oil_and_the_filter_but_not_other_parts(self):
        self.assertTrue(matches_category({'product_name': 'ძრავის ზეთი 5W40'}, 'oil change'))
        self.assertTrue(matches_category({'product_name': 'ზეთის ფილტრი'}, 'oil change'))
        self.assertFalse(matches_category({'product_name': 'სამუხრუჭე ხუნდი'}, 'oil change'))
        for other in ('რედუქტორის ზეთი AXLE OIL 75W90', 'ზეთი ჰიდრავლიკის (MB 236.3)',
                      'Power Steering Fluid', 'ATF Dexron III'):
            self.assertFalse(matches_category({'product_name': other}, 'oil change'), other)


class OilMatchTests(unittest.TestCase):
    fluids = [{'provider': 'Shell', 'status': 'PROVISIONAL', 'engine_code': 'M 276.850',
               'items': [
                   {'kind': 'oil', 'product_name': 'Helix Ultra 0W-40 (SP)', 'capacity': '6.5'},
                   {'kind': 'oil', 'product_name': 'Helix Ultra 5W-40', 'viscosity': '5W-40',
                    'capacity': '6.5'}]},
              {'provider': 'Pakelo', 'status': 'CONFLICTED', 'items': []}]

    def test_requirement_merges_grades_capacity_and_engine_from_resolved_reports_only(self):
        need = oil_match.requirements(self.fluids)
        self.assertEqual(need['grades'], ['0W-40', '5W-40'])
        self.assertEqual(need['capacity_l'], 6.5)
        self.assertEqual(need['engine_code'], 'M 276.850')
        self.assertEqual(need['sources'], ['Shell'])
        self.assertEqual(need['recommended'][0]['line'], ['helix', 'ultra'])
        self.assertEqual(oil_match.requirements([])['grades'], [])

    def test_grade_and_pack_parsing_normalizes_spelling(self):
        self.assertEqual(oil_match.normalize_grade('SAE 5W40 4L'), '5W-40')
        self.assertEqual(oil_match.normalize_grade('0w-40'), '0W-40')
        self.assertIsNone(oil_match.normalize_grade('10W'))
        self.assertEqual(oil_match.pack_litres('Shell Helix Ultra 5W-40 4L'), 4.0)
        self.assertEqual(oil_match.pack_litres('Oil 1 LT'), 1.0)
        self.assertIsNone(oil_match.pack_litres('Oil 5W40'))

    def product(self, identifier, name, brand, store=0, tuz11=0):
        return {'source_product_id': identifier, 'product_name': name, 'brand': brand,
                'stock': {'store': store, 'tuz11': tuz11}}

    def test_tiers_stock_order_and_fuel_exclusion(self):
        shell = self.product('s1', 'Shell Helix Ultra 5W-40 4L', 'SHELL', store=16)
        shell_hx7 = self.product('s2', 'SHELL HELIX HX7 5W40 1L', 'SHELL', tuz11=2)
        diesel = self.product('d1', 'SHELL HELIX ULTRA 5W40 DIESEL 4L', 'SHELL', store=9)
        other = self.product('o1', 'MOBIL 5W-40 1L', 'MOBIL', store=3)
        wrong_grade = self.product('w1', 'MOBIL 10W-40 1L', 'MOBIL', store=3)
        repository = Mock()
        repository.search_engine_oils.side_effect = lambda grade, brand=None, limit=100: {
            'items': [shell, shell_hx7, diesel] if brand and grade == '5W-40' else
            [shell, shell_hx7, diesel, other, wrong_grade] if grade == '5W-40' else []}
        need = oil_match.requirements(self.fluids)
        rows = oil_match.oil_rows(repository, need, {'fuel': 'Gasoline'}, 'en')
        by_id = {row['product']['source_product_id']: row for row in rows}
        self.assertEqual(set(by_id), {'s1', 's2', 'o1'})  # Diesel and wrong grade are out.
        self.assertEqual(by_id['s1']['match_type'], 'OIL_GRADE_BRAND')
        self.assertEqual(by_id['s2']['match_type'], 'OIL_GRADE')  # Different product line.
        self.assertEqual(by_id['o1']['match_type'], 'OIL_GRADE')
        self.assertEqual([row['product']['source_product_id'] for row in rows],
                         ['s1', 'o1', 's2'])  # Store before TUZ 11.
        self.assertTrue(all(row['status'] == 'UNKNOWN' and row['compatible'] is None
                            for row in rows))
        self.assertEqual(by_id['s1']['pack_litres'], 4.0)
        self.assertIn('approval unconfirmed', by_id['s1']['warnings'][0])

    def test_engine_family_rows_need_an_engine_code_from_the_source(self):
        repository = Mock()
        repository.search_engine_family.return_value = {'items': [
            {**self.product('f1', 'ზეთის ფილტრი', 'MANN', store=1), 'part_role': 'oil_filter'}]}
        rows = oil_match.engine_family_rows(repository, oil_match.requirements(self.fluids))
        repository.search_engine_family.assert_called_once_with('M276', limit=50)
        self.assertEqual((rows[0]['match_type'], rows[0]['role']), ('ENGINE_FAMILY', 'oil_filter'))
        self.assertEqual(oil_match.engine_family_rows(Mock(), {'engine_code': None,
                                                                'sources': []}), [])


if __name__ == '__main__':
    unittest.main()
