"""Federation acceptance against synthetic services; no live inventory assertions."""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from unittest.mock import Mock

from automotive.motorium import MotoriumError
from automotive.vin_search import TERMINAL, VINSearchCoordinator, merge_parts

VIN = '1HGCM82633A004352'  # Public test fixture, not customer data.
VEHICLE = {'make': 'Example', 'model': 'Car', 'model_year': 2013}


def product(identifier='p1', code='AB-123', brand='Maker'):
    return {'source_product_id': identifier, 'product_code': code, 'brand': brand,
            'product_name': 'Fixture part', 'stock': None, 'price': None}


def warehouse(identifier='p1', code='AB-123', brand='Maker', **updates):
    return {'product': product(identifier, code, brand), 'status': 'UNKNOWN',
            'compatible': None, 'match_type': 'CATALOG_TEXT', 'evidence': [],
            'warnings': [], **updates}


def supplier(identifier=10, code='AB-123', brand='Maker', **updates):
    return {'provider_product_id': identifier, 'name': '<script>fixture</script>',
            'manufacturer': brand, 'mpn': code, 'oem_codes': ['OE123456'],
            'supplier_sku': 'supplier-only', 'status': 'PROVISIONAL',
            'source_url': 'https://motorium.ge/product.php?id=' + str(identifier),
            'provider_fitment_text': 'Provider candidate', 'warnings': [], **updates}


class MergeTests(unittest.TestCase):
    def test_unique_code_and_manufacturer_joins_provenance_without_verifying_fitment(self):
        result = merge_parts({'items': [warehouse()]}, [supplier(), supplier()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['status'], 'UNKNOWN')
        self.assertIsNone(result[0]['compatible'])
        self.assertEqual(len(result[0]['supplier_records']), 1)
        self.assertEqual(result[0]['source_names'], ['warehouse', 'motorium'])
        self.assertIsNone(result[0]['warehouse_records'][0]['product']['stock'])

    def test_collisions_aliases_other_brands_and_duplicate_catalog_stay_separate(self):
        for local, external in [
            ([warehouse()], supplier(code='AB123')),
            ([warehouse()], supplier(brand='Other')),
            ([warehouse(code='OE123456')], supplier()),
            ([warehouse(), warehouse('p2')], supplier()),
            ([warehouse()], supplier(status='CONFLICTED')),
        ]:
            with self.subTest(local=local, external=external):
                self.assertEqual(len(merge_parts({'items': local}, [external])), len(local) + 1)

    def test_verified_negative_cannot_reappear_via_supplier(self):
        result = merge_parts({'items': [], 'excluded_items': [warehouse(compatible=False)]},
                             [supplier()])
        self.assertEqual(result, [])


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.vehicle_catalog_candidates.return_value = {
            'items': [product()], 'has_more': False,
        }
        self.repository.search_parts.return_value = {'items': [], 'warnings': []}
        self.repository.cross_reference_records.return_value = []
        self.vin = Mock()
        self.vin.decode.return_value = {'vehicle': VEHICLE, 'warnings': []}
        self.motorium = Mock()
        self.motorium.lookup.return_value = {
            'vehicle': VEHICLE, 'vehicle_context_matched': True,
            'status': 'PROVISIONAL', 'items': [supplier()], 'has_more': False,
        }
        self.coordinator = self.create_coordinator()

    def create_coordinator(self, **options):
        coordinator = VINSearchCoordinator(self.repository, self.vin, self.motorium, 'k' * 32,
                                           queries=('first', 'second'), **options)
        self.addCleanup(coordinator.close)
        return coordinator

    def finish(self, coordinator=None, **options):
        coordinator = coordinator or self.coordinator
        initial = coordinator.start(VIN, **options)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = coordinator.get(initial['search_id'])
            if result['status'] in TERMINAL:
                return result
            time.sleep(0.005)
        self.fail('Read job did not reach terminal state')

    def source(self, result, name):
        return next(s for s in result['sources'] if s['source_id'] == name)

    def test_all_sources_run_and_identical_read_is_cached_without_storing_vin(self):
        result = self.finish()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(self.source(result, 'motorium')['completed_queries'], 2)
        self.assertEqual(result['maintenance']['status'], 'INSUFFICIENT_DATA')
        self.assertEqual(self.coordinator.start(VIN)['search_id'], result['search_id'])
        self.assertEqual(self.motorium.lookup.call_count, 2)
        serialized = json.dumps(self.coordinator._jobs)
        self.assertNotIn(VIN, serialized)
        self.assertNotIn('k' * 32, serialized)
        for call in self.motorium.lookup.call_args_list:
            self.assertEqual(call.args[0], VIN)  # Always repeat VIN to provider.
            self.assertIn('deadline', call.kwargs)

    def test_query_narrows_external_categories(self):
        self.finish(part_query='oil filter')
        self.assertEqual(self.motorium.lookup.call_count, 1)
        self.assertEqual(self.motorium.lookup.call_args.args[1], 'oil filter')

    def test_long_supplier_lists_do_not_mark_the_source_or_job_partial(self):
        self.motorium.lookup.return_value['has_more'] = True
        result = self.finish()
        self.assertEqual(self.source(result, 'motorium')['status'], 'OK')
        self.assertEqual(result['status'], 'COMPLETE')

    def test_category_runs_only_the_choosers_that_can_answer_it(self):
        fuchs, jsfilter = Mock(), Mock()
        fuchs.lookup.return_value = {'provider': 'Fuchs', 'status': 'EMPTY', 'items': []}
        jsfilter.lookup.return_value = {'provider': 'JS Asakashi', 'status': 'EMPTY', 'items': []}
        coordinator = self.create_coordinator(fuchs_service=fuchs, jsfilter_service=jsfilter)
        result = self.finish(coordinator, part_query='brake pads')
        fuchs.lookup.assert_not_called()
        jsfilter.lookup.assert_not_called()
        self.assertEqual(self.source(result, 'fuchs')['error_code'], 'NOT_REQUESTED')
        self.assertEqual(result['status'], 'COMPLETE')
        result = self.finish(coordinator, part_query='engine oil')
        fuchs.lookup.assert_called_once()
        jsfilter.lookup.assert_not_called()
        self.assertEqual(self.source(result, 'fuchs')['status'], 'EMPTY')

    def test_filter_codes_from_the_vehicle_catalogue_are_matched_in_the_warehouse(self):
        jsfilter = Mock()
        jsfilter.lookup.return_value = {'provider': 'JS Asakashi', 'status': 'PROVISIONAL',
                                        'items': [{'component': 'OIL', 'product_code': 'C-101'},
                                                  {'component': 'AIR', 'product_code': 'A-202'}]}
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_parts.side_effect = lambda c, **_: {
            'items': [product('hit', code='C-101', brand='Brand only')] if c == 'C-101' else [],
            'warnings': [],
        }
        coordinator = self.create_coordinator(jsfilter_service=jsfilter)
        result = self.finish(coordinator, part_query='oil filter')
        calls = [c.args[0] for c in self.repository.search_parts.call_args_list]
        self.assertIn('C-101', calls)
        self.assertNotIn('A-202', calls)  # Only the requested filter type is searched.
        record = result['items'][0]['warehouse_records'][0]
        self.assertEqual(record['match_type'], 'CODE_CANDIDATE')
        self.assertEqual(record['product']['product_name'], 'Fixture part')

    def test_make_model_year_without_vin_skips_vin_providers_and_still_searches(self):
        fuchs = Mock()
        fuchs.lookup.return_value = {'provider': 'Fuchs', 'status': 'PROVISIONAL',
                                     'items': [{'viscosity': '5W-30'}]}
        coordinator = self.create_coordinator(fuchs_service=fuchs)
        initial = coordinator.start(None, 'engine oil', None, VEHICLE)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = coordinator.get(initial['search_id'])
            if result['status'] in TERMINAL:
                break
            time.sleep(0.005)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['vehicle'], VEHICLE)
        self.vin.decode.assert_not_called()
        self.motorium.lookup.assert_not_called()
        self.assertEqual(self.source(result, 'vpic')['error_code'], 'VIN_REQUIRED')
        self.assertEqual(fuchs.lookup.call_args.args[0], None)
        self.assertEqual(self.source(result, 'warehouse')['status'], 'OK')
        with self.assertRaisesRegex(ValueError, 'VEHICLE_REQUIRED'):
            coordinator.start(None, 'engine oil', None, {'make': 'Example'})

    def test_oil_change_asks_motorium_for_the_filter_and_runs_only_the_oil_choosers(self):
        fuchs, jsfilter = Mock(), Mock()
        fuchs.lookup.return_value = {'provider': 'Fuchs', 'status': 'EMPTY', 'items': []}
        coordinator = self.create_coordinator(fuchs_service=fuchs, jsfilter_service=jsfilter)
        self.finish(coordinator, part_query='oil change')
        self.assertEqual(self.motorium.lookup.call_args.args[1], 'ზეთის ფილტრი')
        fuchs.lookup.assert_called_once()
        jsfilter.lookup.assert_not_called()

    def test_code_matches_record_which_source_supplied_the_code(self):
        self.repository.search_parts.side_effect = lambda c, **_: {
            'items': [product('hit', code='zzz', brand='Brand')] if c == 'AB-123' else [],
            'warnings': []}
        result = self.finish()
        records = [r for i in result['items'] for r in i['warehouse_records']
                   if r['match_type'] == 'CODE_CANDIDATE']
        self.assertEqual(records[0]['provenance'], ['Motorium: AB-123'])

    def test_catalog_api_codes_are_searched_in_the_warehouse_with_their_origin(self):
        catalog = Mock()
        catalog.lookup.return_value = {'status': 'PROVISIONAL', 'items': [
            {'code': 'HU 7025 z', 'supplier': 'MANN', 'category': 'oil_filter',
             'oem_codes': ['A2761800009']}]}
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_parts.side_effect = lambda c, **_: {
            'items': [product('f1', code='HU7025Z', brand='MANN')] if c == 'A2761800009' else [],
            'warnings': []}
        coordinator = self.create_coordinator(catalog_service=catalog)
        result = self.finish(coordinator, part_query='oil filter')
        self.assertEqual(catalog.lookup.call_args.args[2], ['oil_filter'])
        self.assertEqual(self.source(result, 'catalog_api')['status'], 'OK')
        calls = [c.args[0] for c in self.repository.search_parts.call_args_list]
        self.assertLess(calls.index('A2761800009'), calls.index('HU 7025 z'))  # OE first.
        record = next(r for i in result['items'] for r in i['warehouse_records']
                      if r['match_type'] == 'CODE_CANDIDATE')
        self.assertEqual(record['provenance'], ['Catalog API OE: A2761800009'])

    def test_a_catalog_part_number_must_come_with_the_same_brand(self):
        catalog = Mock()
        catalog.lookup.return_value = {'status': 'PROVISIONAL', 'items': [
            {'code': '1681', 'supplier': '3F QUALITY', 'category': 'cabin_filter',
             'oem_codes': []},
            {'code': 'C 21 020', 'supplier': 'MANN-FILTER', 'category': 'air_filter',
             'oem_codes': []}]}
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_parts.side_effect = lambda c, **_: {
            'items': [product('other', code='1681', brand='GSP')] if c == '1681'
            else [product('mann', code='C21020', brand='MANN')], 'warnings': []}
        coordinator = self.create_coordinator(catalog_service=catalog)
        result = self.finish(coordinator, part_query='air filter')
        ids = {r['product']['source_product_id'] for i in result['items']
               for r in i['warehouse_records'] if r['match_type'] == 'CODE_CANDIDATE'}
        self.assertEqual(ids, {'mann'})  # '1681' belongs to another brand: a collision.

    def test_catalog_api_is_off_without_a_service_and_skipped_for_oil(self):
        result = self.finish(part_query='oil filter')
        self.assertEqual(self.source(result, 'catalog_api')['status'], 'DISABLED')
        catalog = Mock()
        coordinator = self.create_coordinator(catalog_service=catalog)
        result = self.finish(coordinator, part_query='engine oil')
        catalog.lookup.assert_not_called()
        self.assertEqual(self.source(result, 'catalog_api')['error_code'], 'NOT_REQUESTED')

    def test_catalog_api_failure_is_named_and_keeps_other_sources(self):
        from automotive.public_http import PublicSourceError

        catalog = Mock()
        catalog.lookup.side_effect = PublicSourceError('ACCESS_REQUIRED')
        coordinator = self.create_coordinator(catalog_service=catalog)
        result = self.finish(coordinator, part_query='brake pads')
        self.assertEqual(self.source(result, 'catalog_api')['error_code'], 'ACCESS_REQUIRED')
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(self.source(result, 'warehouse')['status'], 'OK')

    def catalog_ok(self, label='EXAMPLE CAR Coupe'):
        catalog = Mock()
        catalog.lookup.return_value = {'status': 'PROVISIONAL', 'vehicle_label': label, 'items': [
            {'code': 'HU 7025 z', 'supplier': 'MANN', 'category': 'oil_filter',
             'oem_codes': []}]}
        return catalog

    def oil_services(self, shell_status='PROVISIONAL'):
        report = {'provider': 'x', 'status': shell_status, 'items': [], 'warnings': []}
        shell, pakelo, fuchs, jsfilter = Mock(), Mock(), Mock(), Mock()
        for service in (shell, pakelo, fuchs, jsfilter):
            service.lookup.return_value = dict(report)
        return shell, pakelo, fuchs, jsfilter

    def test_a_working_catalog_api_makes_the_slow_code_sources_unnecessary(self):
        shell, pakelo, fuchs, jsfilter = self.oil_services()
        coordinator = self.create_coordinator(
            catalog_service=self.catalog_ok(), shell_service=shell, pakelo_service=pakelo,
            fuchs_service=fuchs, jsfilter_service=jsfilter)
        result = self.finish(coordinator, part_query='engine oil filter')
        self.motorium.lookup.assert_not_called()
        jsfilter.lookup.assert_not_called()
        for name in ('motorium', 'jsfilter'):
            self.assertEqual(self.source(result, name)['error_code'], 'NOT_NEEDED')
        self.assertEqual(self.source(result, 'catalog_api')['status'], 'OK')
        self.assertEqual(result['status'], 'COMPLETE')  # Unneeded sources are not failures.

    def test_the_slow_code_sources_run_when_the_catalog_api_fails_or_is_empty(self):
        from automotive.public_http import PublicSourceError

        for catalog in (Mock(lookup=Mock(side_effect=PublicSourceError('RATE_LIMITED'))),
                        Mock(lookup=Mock(return_value={'status': 'EMPTY', 'items': []}))):
            self.motorium.lookup.reset_mock()
            coordinator = self.create_coordinator(catalog_service=catalog)
            self.finish(coordinator, part_query='brake pads')
            self.motorium.lookup.assert_called()

    def test_pakelo_and_fuchs_back_up_shell_only_when_it_cannot_resolve_the_engine(self):
        for shell_status, backups in (('PROVISIONAL', 0), ('CONFLICTED', 1)):
            shell, pakelo, fuchs, jsfilter = self.oil_services(shell_status)
            coordinator = self.create_coordinator(
                catalog_service=self.catalog_ok(), shell_service=shell, pakelo_service=pakelo,
                fuchs_service=fuchs, jsfilter_service=jsfilter)
            result = self.finish(coordinator, part_query='engine oil')
            self.assertEqual((pakelo.lookup.call_count, fuchs.lookup.call_count),
                             (backups, backups), shell_status)
            if not backups:
                self.assertEqual(self.source(result, 'pakelo')['error_code'], 'NOT_NEEDED')

    def test_chooser_answers_are_shared_between_customers_with_the_same_car(self):
        shell, pakelo, fuchs, jsfilter = self.oil_services()
        coordinator = self.create_coordinator(shell_service=shell, pakelo_service=pakelo,
                                              fuchs_service=fuchs, jsfilter_service=jsfilter)
        self.finish(coordinator, part_query='engine oil')
        self.finish(coordinator, part_query='motor oil')  # A different job, the same car.
        self.assertEqual(shell.lookup.call_count, 1)

    def test_a_catalog_car_that_contradicts_the_decoded_car_is_ignored(self):
        catalog = self.catalog_ok(label='BMW 3 (F30) 320d')
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        coordinator = self.create_coordinator(catalog_service=catalog)
        result = self.finish(coordinator, part_query='oil filter')
        source = self.source(result, 'catalog_api')
        self.assertEqual((source['status'], source['error_code']),
                         ('CONFLICTED', 'VEHICLE_MISMATCH'))
        calls = [c.args[0] for c in self.repository.search_parts.call_args_list]
        self.assertNotIn('HU 7025 z', calls)
        self.motorium.lookup.assert_called()  # The fallback code source takes over.

    def test_short_bare_oem_codes_are_not_searched_but_distinctive_ones_are(self):
        self.motorium.lookup.return_value['items'] = [
            supplier(code='AB-123', brand='Maker') | {'oem_codes': ['OE12', 'A2761800009']}]
        self.repository.search_parts.side_effect = lambda c, **_: {'items': [], 'warnings': []}
        self.finish()
        calls = [c.args[0] for c in self.repository.search_parts.call_args_list]
        self.assertIn('A2761800009', calls)
        self.assertNotIn('OE12', calls)

    def test_each_source_reports_how_long_it_took(self):
        result = self.finish()
        timed = {s['source_id']: s['elapsed_ms'] for s in result['sources']
                 if s.get('elapsed_ms') is not None}
        self.assertIn('vpic', timed)
        self.assertIn('motorium', timed)
        self.assertTrue(all(ms >= 0 for ms in timed.values()))

    def test_prewarm_resolves_the_car_in_the_background_and_never_raises(self):
        catalog = Mock()
        coordinator = self.create_coordinator(catalog_service=catalog)
        coordinator.prewarm(VIN)
        deadline = time.monotonic() + 2
        while not catalog.warm.called and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(catalog.warm.call_args.args[0], VIN)
        coordinator.prewarm('not a vin')  # Silently ignored.
        self.create_coordinator().prewarm(VIN)  # No catalog service: nothing to warm.

    def test_provider_error_retains_inventory_and_never_exposes_exception_message(self):
        self.motorium.lookup.side_effect = MotoriumError('RATE_LIMITED')
        result = self.finish()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['total'], 1)
        self.assertEqual(self.source(result, 'motorium')['error_code'], 'RATE_LIMITED')
        self.assertEqual(self.source(result, 'warehouse')['status'], 'OK')

    def test_category_failure_preserves_already_received_codes(self):
        self.motorium.lookup.side_effect = [self.motorium.lookup.return_value,
                                           MotoriumError('ACCESS_REQUIRED')]
        result = self.finish()
        self.assertEqual(self.source(result, 'motorium')['status'], 'PARTIAL')
        self.assertEqual(result['items'][0]['supplier_records'][0]['mpn'], 'AB-123')

    def test_fitment_failure_does_not_hide_catalog_or_supplier(self):
        self.repository.vehicle_fitment_records.side_effect = RuntimeError('postgres://secret')
        result = self.finish()
        self.assertEqual(self.source(result, 'fitment')['status'], 'ERROR')
        self.assertEqual(self.source(result, 'warehouse')['status'], 'OK')
        self.assertEqual(result['total'], 1)
        self.assertNotIn('postgres://secret', json.dumps(result))

    def test_supplier_identity_conflict_is_withheld_but_warehouse_survives(self):
        self.motorium.lookup.return_value['vehicle'] = {**VEHICLE, 'model': 'Different'}
        result = self.finish()
        self.assertEqual(self.source(result, 'motorium')['status'], 'CONFLICTED')
        self.assertEqual(result['items'][0]['supplier_records'], [])
        self.repository.search_parts.assert_not_called()

    def test_failed_vpic_can_use_matched_supplier_identification(self):
        self.vin.decode.side_effect = RuntimeError('private provider error')
        result = self.finish()
        self.assertEqual(result['vehicle'], VEHICLE)
        self.assertEqual(self.source(result, 'vpic')['status'], 'ERROR')
        self.assertEqual(result['total'], 1)
        self.assertNotIn('private provider error', json.dumps(result))

    def test_unknown_vehicle_does_not_query_or_list_arbitrary_inventory(self):
        self.vin.decode.return_value = {'vehicle': {}, 'warnings': []}
        self.motorium.lookup.return_value.update(
            vehicle={}, vehicle_context_matched=False, items=[],
        )
        result = self.finish()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['items'], [])
        self.repository.vehicle_catalog_candidates.assert_not_called()
        self.repository.search_parts.assert_not_called()

    def test_codes_find_products_without_vehicle_name_and_preserve_direction(self):
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_parts.side_effect = lambda c, **_: {
            'items': [product('code-only', code='internal-id')] if c == 'OE123456' else [],
            'warnings': [],
        }
        forward = {'evidence_id': 'forward', 'from_code': 'AB-123', 'to_code': 'XYZ789',
                   'manufacturer': 'Maker', 'status': 'PROVISIONAL'}
        reverse = {**forward, 'evidence_id': 'reverse', 'from_code': 'XYZ789', 'to_code': 'AB-123'}
        restricted = {**forward, 'evidence_id': 'restricted',
                      'restrictions': {'engine_code': 'unknown'}}
        self.repository.cross_reference_records.return_value = [forward, reverse, restricted]
        result = self.finish()
        self.assertEqual(result['total'], 2)
        local = next(i for i in result['items'] if i['warehouse_records'])
        self.assertEqual(local['status'], 'UNKNOWN')
        self.assertEqual(local['warehouse_records'][0]['match_type'], 'CODE_CANDIDATE')
        self.assertEqual([r['evidence_id'] for r in result['cross_references']], ['forward'])
        calls = [c.args[0] for c in self.repository.search_parts.call_args_list]
        self.assertNotIn('XYZ789', calls)  # No implied interchange or inverse traversal.

    def test_pagination_is_stable_and_returned_objects_cannot_mutate_snapshot(self):
        self.repository.vehicle_catalog_candidates.return_value = {
            'items': [product(str(i), code=str(i)) for i in range(80)], 'has_more': False,
        }
        result = self.finish()
        page = self.coordinator.get(result['search_id'], 50, 50)
        self.assertEqual(result['total'], 81)
        self.assertEqual(page['offset'], 50)
        self.assertIsNone(page['next_offset'])
        self.assertFalse({i['part_id'] for i in result['items']} &
                         {i['part_id'] for i in page['items']})
        expected = copy.deepcopy(page)
        page['items'].clear()
        self.assertEqual(self.coordinator.get(result['search_id'], 50, 50), expected)

    def test_configuration_cannot_replace_decoded_vehicle_identity(self):
        result = self.finish(configuration={**VEHICLE, 'model_year': 2001, 'engine_code': 'E1'})
        self.assertEqual(result['vehicle'], VEHICLE)
        self.assertIn('USER_CONFIGURATION_MISMATCH', result['warnings'])
        result = self.finish(configuration={'body': 'Sedan'})
        self.assertEqual(result['vehicle']['body'], 'Sedan')

    def test_expired_snapshots_are_unavailable_and_capacity_is_bounded(self):
        result = self.finish()
        self.coordinator._jobs[result['search_id']]['expires'] = 0
        self.assertIsNone(self.coordinator.get(result['search_id']))
        self.coordinator._jobs = {str(i): {'expires': 0, 'status': 'RUNNING',
                                          'fingerprint': str(i)} for i in range(20)}
        with self.assertRaisesRegex(RuntimeError, 'SEARCH_CAPACITY'):
            self.coordinator.start(VIN)

    def test_deadline_freezes_snapshot_and_late_provider_cannot_mutate_it(self):
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        def delayed(*args, **kwargs):
            blocker.wait(2)
            return self.motorium.lookup.return_value
        self.motorium.lookup.side_effect = delayed
        coordinator = self.create_coordinator(budget_seconds=0.05)
        result = self.finish(coordinator)
        self.assertEqual(result['status'], 'PARTIAL')
        blocker.set()
        time.sleep(0.05)
        self.assertEqual(coordinator.get(result['search_id']), result)

    def test_repeated_supplier_identity_disagreement_remains_conflicted(self):
        self.motorium.lookup.side_effect = [
            {**self.motorium.lookup.return_value, 'items': [supplier()]},
            {**self.motorium.lookup.return_value, 'items': [supplier(code='DIFFERENT123')]},
        ]
        result = self.finish()
        self.assertEqual(self.source(result, 'motorium')['status'], 'CONFLICTED')
        external = [i for i in result['items'] if i['supplier_records']]
        self.assertEqual(len(external), 2)
        self.assertTrue(all(i['status'] == 'CONFLICTED' for i in external))
        self.assertEqual({i['main_code'] for i in external}, {'AB-123', 'DIFFERENT123'})
        self.repository.search_parts.assert_not_called()
