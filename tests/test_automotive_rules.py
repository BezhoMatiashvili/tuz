"""Hard accuracy rules, the staff rules file, evaluator helpers and index plumbing."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from automotive import evaluate, oil_match
from automotive.categories import POSITIONS
from automotive.repository import AutomotiveRepository
from automotive.rules import (
    NeverOffer,
    OilApproval,
    Rules,
    RulesError,
    StaffConfirmed,
    approvals_stated,
    axle_verdict,
    load_rules,
    oem_code_ok,
    parse_rules,
    requested_axle,
    vehicle_consistent,
)
from automotive.worker import run_once


class RulesFileTests(unittest.TestCase):
    def test_the_shipped_file_loads_and_carries_the_documented_defaults(self):
        rules = load_rules()
        self.assertEqual((rules.min_store, rules.min_tuz11), (0.0, 0.0))
        self.assertEqual((rules.stale_hours, rules.hide_after_hours), (3.0, 24.0))
        self.assertTrue(rules.brands_match('MANN', 'MANN-FILTER'))
        self.assertTrue(rules.brands_match('MB', 'MERCEDES-BENZ'))
        self.assertIn('სენს', rules.not_the_part_words('brake_pads'))

    def test_every_error_names_its_section_and_stops_loading(self):
        cases = {
            'Unknown top-level': {'bogus': {}},
            'unknown keys: colour': {'brand_alias': [{'names': ['A'], 'colour': 'x'}]},
            'is missing: names': {'brand_alias': [{}]},
            'must be a number': {'stock': {'min_store': 'many'}},
            'hide_after_hours must not be below': {'stock': {'stale_hours': 5,
                                                              'hide_after_hours': 2}},
            'not a known category': {'not_the_part': [{'category': 'wheels', 'words': ['x']}]},
            '"words" must be a non-empty list': {'not_the_part': [{'category': 'brake_pads',
                                                                     'words': []}]},
            '"strict" must be true or false': {'oil_approval': [
                {'make': 'X', 'require_any': ['1'], 'strict': 'yes'}]},
            'year between 1950 and 2100': {'staff_confirmed': [
                {'make': 'X', 'model': 'Y', 'category': 'brake_pads', 'product_code': '1',
                 'year_from': 1800}]},
        }
        for fragment, data in cases.items():
            with self.subTest(fragment), self.assertRaisesRegex(RulesError, fragment):
                parse_rules(data)

    def test_invalid_toml_and_a_missing_explicit_file_fail_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'rules.toml'
            bad.write_text('[stock\nmin_store = 1')
            with self.assertRaisesRegex(RulesError, 'not valid TOML'):
                load_rules(bad)
            with self.assertRaisesRegex(RulesError, 'not found'):
                load_rules(Path(folder) / 'missing.toml')

    def test_staff_sections_round_trip(self):
        rules = parse_rules({
            'oil_approval': [{'make': 'MERCEDES-BENZ', 'engine_family': 'M276',
                              'require_any': ['229.5'], 'strict': True}],
            'never_offer': [{'product_code': 'X1', 'make': 'HONDA', 'reason': 'bad'}],
            'staff_confirmed': [{'make': 'HONDA', 'model': 'Accord', 'year_from': 2003,
                                 'year_to': 2007, 'category': 'brake_pads',
                                 'product_code': 'P1', 'note': 'checked'}],
        })
        car = {'make': 'HONDA', 'model': 'Accord', 'model_year': 2005}
        self.assertEqual(rules.never_reason({'product_code': 'x-1'}, car), 'bad')
        self.assertIsNone(rules.never_reason({'product_code': 'x-1'}, {'make': 'FORD'}))
        self.assertEqual([r.product_code for r in rules.confirmed_for(car, 'brake_pads')], ['P1'])
        self.assertEqual(rules.confirmed_for({**car, 'model_year': 2010}, 'brake_pads'), [])
        self.assertEqual(rules.confirmed_for(car, 'brake_discs'), [])
        merc = {'make': 'Mercedes-Benz', 'model': 'E-Class'}
        self.assertTrue(rules.approval_rule(merc, 'M276').strict)
        self.assertIsNone(rules.approval_rule(merc, 'M272'))


class HardRuleTests(unittest.TestCase):
    rules = Rules(not_the_part={'brake_pads': ('სენს', 'sensor')})

    def test_axle_uses_the_catalogue_abbreviations(self):
        self.assertEqual(requested_axle('front brake pads'), 'front')
        self.assertEqual(requested_axle('უკანა ხუნდები'), 'rear')
        self.assertIsNone(requested_axle('brake pads'))
        self.assertIsNone(requested_axle('front and rear pads'))
        self.assertEqual(axle_verdict('ხუნდი წნ', 'front', 'brake_pads'), 'MATCH')
        self.assertEqual(axle_verdict('ხუნდი უკ', 'front', 'brake_pads'), 'OPPOSITE')
        self.assertEqual(axle_verdict('ხუნდი უკანა', 'front', 'brake_pads'), 'OPPOSITE')
        self.assertEqual(axle_verdict('Rear pads', 'front', 'brake_pads'), 'OPPOSITE')
        self.assertEqual(axle_verdict('ხუნდი', 'front', 'brake_pads'), 'UNSTATED')
        self.assertEqual(axle_verdict('ხუნდი წინა/უკანა', 'rear', 'brake_pads'), 'MATCH')
        self.assertEqual(axle_verdict('ფილტრი უკ', 'front', 'oil_filter'), 'N/A')
        self.assertEqual(axle_verdict('ხუნდი უკ', None, 'brake_pads'), 'N/A')
        # An abbreviation is a whole word, not the start of a longer one.
        self.assertEqual(axle_verdict('წნევის სენსორი', 'rear', 'brake_pads'), 'UNSTATED')

    def test_sql_position_patterns_know_the_same_abbreviations(self):
        self.assertIn('წნ', POSITIONS['front'])
        self.assertIn('უკ', POSITIONS['rear'])

    def test_part_type_conflict_and_accessory_words(self):
        self.assertTrue(self.rules.part_conflict('ხუნდის სენსორი', 'brake_pads'))
        self.assertTrue(self.rules.part_conflict('სამუხრუჭე დისკი წნ', 'brake_pads'))
        self.assertTrue(self.rules.part_conflict('ზეთის ფილტრი', 'engine_oil'))
        self.assertFalse(self.rules.part_conflict('ხუნდი წნ', 'brake_pads'))
        self.assertFalse(self.rules.part_conflict('BRAND 12345', 'brake_pads'))  # No category word.
        self.assertFalse(self.rules.part_conflict('სამუხრუჭე დისკი', 'brakes'))
        self.assertFalse(self.rules.part_conflict('ძრავის ზეთი 5W40', 'oil_change'))
        self.assertFalse(self.rules.part_conflict('ზეთის ფილტრი', 'oil_change'))
        self.assertTrue(self.rules.part_conflict('ხუნდი წნ', 'oil_change'))
        self.assertFalse(self.rules.part_conflict('anything', None))

    def test_catalog_vehicle_must_agree_with_the_decoded_car(self):
        car = {'make': 'MERCEDES-BENZ', 'model': 'E-Class'}
        self.assertTrue(vehicle_consistent('MERCEDES-BENZ E-CLASS Coupe (C207) E 400', car))
        self.assertFalse(vehicle_consistent('BMW 3 (F30) 320d', car))
        self.assertFalse(vehicle_consistent('MERCEDES-BENZ C-CLASS (W205)', car))
        self.assertTrue(vehicle_consistent('VW GOLF VII', {'make': 'VOLKSWAGEN',
                                                           'model': 'Golf'}))
        self.assertTrue(vehicle_consistent('HONDA CR-V III', {'make': 'HONDA', 'model': 'CR-V'}))
        self.assertTrue(vehicle_consistent(None, car))
        self.assertTrue(vehicle_consistent('anything', {}))

    def test_bare_oem_codes_must_be_distinctive(self):
        self.assertFalse(oem_code_ok('OE123'))
        self.assertFalse(oem_code_ok('1681'))
        self.assertFalse(oem_code_ok('1234567'))
        self.assertTrue(oem_code_ok('A2761800009'))
        self.assertTrue(oem_code_ok('11427566327'))  # An all-digit BMW number is long enough.
        self.assertTrue(oem_code_ok('04152-YZZA1'))

    def test_an_approval_is_not_its_longer_sibling(self):
        self.assertEqual(approvals_stated('MOBIL 0W40 (LL01; 229.5)', ('229.5', '229.52')),
                         ['229.5'])
        self.assertEqual(approvals_stated('OIL MB 229.52', ('229.5', '229.52')), ['229.52'])
        self.assertEqual(approvals_stated('OIL 5W40', ('229.5',)), [])


class OilApprovalAndCoverageTests(unittest.TestCase):
    fluids = [{'provider': 'Shell', 'status': 'PROVISIONAL', 'engine_code': 'M 276.850',
               'items': [{'kind': 'oil', 'product_name': 'Helix Ultra 5W-40',
                          'viscosity': '5W-40', 'capacity': '6.5'}]}]

    def product(self, identifier, name, brand, store=0, tuz11=0):
        return {'source_product_id': identifier, 'product_name': name, 'brand': brand,
                'stock': {'store': store, 'tuz11': tuz11}}

    def repository(self, *products):
        repository = Mock()
        repository.search_engine_oils.side_effect = lambda grade, brand=None, limit=100: {
            'items': [p for p in products if not brand or p['brand'] == brand]}
        return repository

    def test_stated_approvals_rank_first_and_strict_hides_the_rest(self):
        plain = self.product('a', 'SHELL HELIX HX7 5W40 4L', 'SHELL', store=9)
        stated = self.product('b', 'MOBIL 5W40 MB 229.5 4L', 'MOBIL', store=9)
        need = oil_match.requirements(self.fluids)
        car = {'make': 'MERCEDES-BENZ', 'model': 'E-Class'}
        loose = Rules(oil_approvals=(OilApproval('MERCEDES-BENZ', None, 'M276', ('229.5',),
                                                 False),))
        rows = oil_match.oil_rows(self.repository(plain, stated), need, car, 'en', loose)
        self.assertEqual([r['product']['source_product_id'] for r in rows], ['b', 'a'])
        self.assertEqual([r['approval'] for r in rows], ['STATED', 'NOT_STATED'])
        self.assertIn('Approval in name: 229.5', rows[0]['provenance'])
        self.assertIn('not stated', rows[1]['warnings'][-1])
        strict = Rules(oil_approvals=(OilApproval('MERCEDES-BENZ', None, 'M276', ('229.5',),
                                                  True),))
        rows = oil_match.oil_rows(self.repository(plain, stated), need, car, 'en', strict)
        self.assertEqual([r['product']['source_product_id'] for r in rows], ['b'])
        # A different engine family has no rule: nothing is labelled or hidden.
        other = Rules(oil_approvals=(OilApproval('MERCEDES-BENZ', None, 'M272', ('229.5',),
                                                 True),))
        rows = oil_match.oil_rows(self.repository(plain, stated), need, car, 'en', other)
        self.assertEqual(len(rows), 2)
        self.assertNotIn('approval', rows[0])

    def test_pack_sizes_add_up_to_the_required_capacity(self):
        rows = [
            {'match_type': 'OIL_GRADE_BRAND', 'pack_litres': 4.0,
             'product': self.product('1', 'Shell Helix Ultra 5W-40 4L', 'SHELL', store=1)},
            {'match_type': 'OIL_GRADE_BRAND', 'pack_litres': 1.0,
             'product': self.product('2', 'Shell Helix Ultra 5W-40 1L', 'SHELL', store=3,
                                     tuz11=10)},
            {'match_type': 'OIL_GRADE', 'pack_litres': 4.0,
             'product': self.product('3', 'MOBIL 5W-40 4L', 'MOBIL', store=2)},
            {'match_type': 'OIL_GRADE', 'pack_litres': 4.0,
             'product': self.product('4', 'ZZZ 5W-40 4L', 'ZZZ')},  # No stock: not listed.
        ]
        groups = oil_match.coverage(rows, 6.5)
        shell, mobil = groups
        self.assertEqual((shell['brand'], shell['litres_store'], shell['litres_tuz11']),
                         ('SHELL', 7.0, 10.0))
        self.assertTrue(shell['enough_store'])  # 4 + 3 x 1 L = 7 L.
        self.assertEqual((mobil['litres_store'], mobil['enough_store']), (8.0, True))
        short = oil_match.coverage(rows[:1], 6.5)[0]
        self.assertEqual((short['litres_store'], short['enough_store']), (4.0, False))
        self.assertEqual(oil_match.coverage(rows, None), [])


class IndexPlumbingTests(unittest.TestCase):
    def fake_connection(self, executed):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, query, params=None):
                executed.append(repr(query))

            def __iter__(self):
                return iter([])

        class Result:
            def fetchall(self):
                return []

            def fetchone(self):
                return None

            def __iter__(self):
                return iter([])

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, query, params=None):
                executed.append(repr(query))
                return Result()

            def cursor(self, name=None):
                return Cursor()

        return Connection()

    def test_code_search_compares_the_stock_independent_hash_and_only_active_rows(self):
        executed: list[str] = []
        repository = AutomotiveRepository('postgresql://example')
        with patch.object(AutomotiveRepository, '_connect',
                          return_value=self.fake_connection(executed)):
            repository.search_parts_many(['HU 7025 z'])
            repository.search_parts('HU7025Z')
        text = ' '.join(executed)
        self.assertIn('automotive.content_hash(p.source_columns)', text)
        self.assertNotIn('source_fingerprint = p.row_hash', text)
        self.assertNotIn('s.source_fingerprint <> p.row_hash', text)
        self.assertGreaterEqual(text.count('p.is_active'), 4)

    def test_every_customer_facing_catalog_query_filters_inactive_products(self):
        import inspect

        source = inspect.getsource(AutomotiveRepository)
        # get_product, search_products, inventory_products, vehicle_catalog_candidates,
        # search_engine_oils, search_engine_family and both code searches (index + scan).
        self.assertGreaterEqual(source.count('p.is_active'), 9)

    def test_the_worker_queues_an_index_job_only_when_one_is_needed(self):
        repository = Mock(spec=AutomotiveRepository)
        repository.claim_job.return_value = None
        self.assertFalse(run_once(repository))
        repository = AutomotiveRepository('postgresql://example')
        with patch.object(AutomotiveRepository, 'needs_index', return_value=False), patch.object(
                AutomotiveRepository, '_connect',
                return_value=self.fake_connection([])), patch.object(
                AutomotiveRepository, 'create_job') as create:
            self.assertIsNone(repository.enqueue_index_if_needed())
            create.assert_not_called()


class EvaluatorTests(unittest.TestCase):
    def response(self, cards=0, hidden=0, vehicle=True, **extra):
        return {
            'vehicle': {'make': 'HONDA', 'model': 'Accord', 'model_year': 2003,
                        'trim': 'EX'} if vehicle else {},
            'cards': [{'availability_status': 'IN_STORE'}] * cards,
            'diagnostics': {'hidden_unavailable': hidden, 'rejections': {'axle': 2}},
            'progress': [{'source_id': 'motorium', 'elapsed_ms': 5400}],
            'warnings': ['SOURCE_FAILED:motorium'], **extra}

    def test_results_are_classified_and_summarized_without_any_vin(self):
        self.assertEqual(evaluate.classify(self.response(cards=2)), 'OFFERS')
        self.assertEqual(evaluate.classify(self.response(hidden=3)), 'NONE_IN_STOCK')
        self.assertEqual(evaluate.classify(self.response()), 'NO_CODES')
        self.assertEqual(evaluate.classify(self.response(vehicle=False)), 'NO_VEHICLE')
        rows = [evaluate.row_for(1, 'pads', 2.0, self.response(cards=1)),
                evaluate.row_for(2, 'pads', 6.0, self.response())]
        summary = evaluate.summarize(rows)['pads']
        self.assertEqual((summary['requests'], summary['offers_share']), (2, 0.5))
        self.assertEqual((summary['median_seconds'], summary['slowest_seconds']), (4.0, 6.0))
        self.assertEqual(summary['rejections'], {'axle': 4})
        self.assertEqual(summary['failed_sources'], {'motorium': 2})
        misses = evaluate.anonymous_misses(rows)
        self.assertEqual(misses, [{'make': 'HONDA', 'model': 'Accord', 'year': 2003,
                                   'trim': 'EX', 'category': 'pads', 'reason': 'no_codes_found'}])

    def test_vin_list_reading_and_the_printed_table_never_show_a_vin(self):
        vin = '1HGCM82633A004352'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'vins.csv'
            path.write_text(f'VIN,note\n{vin.lower()},first\n# comment\n{vin}\nnot-a-vin\n')
            self.assertEqual(evaluate.read_vins(path), ([vin], 1))
            chat = MagicMock()
            chat.respond_conversation.return_value = self.response(cards=1)
            out = io.StringIO()
            rows = evaluate.evaluate([vin], ['air filter'], chat, Mock(), out)
        self.assertNotIn(vin, out.getvalue())
        self.assertIn('VIN#1', out.getvalue())
        self.assertIn('Totals', out.getvalue())
        self.assertEqual(rows[0]['result'], 'OFFERS')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'misses.csv'
            self.assertEqual(evaluate.write_misses(
                [evaluate.row_for(1, 'x', 1.0, self.response())], target), 1)
            self.assertNotIn(vin, target.read_text())


class DataClassTests(unittest.TestCase):
    def test_rules_records_are_immutable_values(self):
        record = NeverOffer('X', None, None, '')
        with self.assertRaises(AttributeError):
            record.reason = 'changed'  # type: ignore[misc]
        self.assertIsNone(
            StaffConfirmed('A', 'B', None, None, 'brake_pads', 'C', None, '').brand)


if __name__ == '__main__':
    unittest.main()
