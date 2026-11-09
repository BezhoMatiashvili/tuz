"""Stock rule, engine matching and requirement-to-catalog matching (synthetic data only)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from automotive import engine_match, oil_match, stock
from automotive.categories import category_for_query, matches_category, query_patterns
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

    def test_observations_older_than_three_hours_are_stale(self):
        now = datetime(2026, 11, 9, 12, tzinfo=UTC)
        self.assertTrue(stock.is_stale(now - timedelta(hours=4), now))
        self.assertFalse(stock.is_stale(now - timedelta(hours=1), now))
        self.assertFalse(stock.is_stale('2020-01-01', now))


class EngineMatchTests(unittest.TestCase):
    def test_trim_is_matched_ignoring_spaces_but_not_longer_numbers(self):
        self.assertTrue(engine_match.trim_matches('E 400 4-matic 245kW', 'E400'))
        self.assertTrue(engine_match.trim_matches('E-CLASS E400 (P)', 'E 400'))
        self.assertFalse(engine_match.trim_matches('Class E 4000', 'E400'))
        self.assertFalse(engine_match.trim_matches('anything', 'E4'))
        self.assertEqual(engine_match.trim_search_text('E400'), 'E 400')

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
        self.assertEqual(query_patterns('oil change')[1], [])
        self.assertTrue(matches_category({'product_name': 'ძრავის ზეთი 5W40'}, 'oil change'))
        self.assertTrue(matches_category({'product_name': 'ზეთის ფილტრი'}, 'oil change'))
        self.assertFalse(matches_category({'product_name': 'სამუხრუჭე ხუნდი'}, 'oil change'))


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
