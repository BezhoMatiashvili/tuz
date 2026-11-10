"""No-VIN catalog fixtures and persistent cache safety, without network access."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from automotive.catalog_api import CatalogApiClient
from automotive.public_http import PublicSourceError
from automotive.ttl_cache import SQLiteTTLCache
from tests.test_automotive_catalog_fallback import (
    ARTICLES_REPLY,
    GROUPS_REPLY,
    FakeCatalogHttp,
)

VEHICLE = {'make': 'Mercedes-Benz', 'model': 'E-Class', 'model_year': 2015,
           'body': 'coupe', 'displacement_l': 3.0}
MANUFACTURERS = {'manufacturers': [{'manuId': 74, 'manuName': 'MERCEDES-BENZ'}]}
MODELS = {'models': [
    {'modelId': 8035, 'modelName': 'E-CLASS Coupe (C207)',
     'yearOfConstructionFrom': '200901', 'yearOfConstructionTo': '201612'},
    {'modelId': 999, 'modelName': 'E-CLASS Saloon (W212)', 'bodyType': 'saloon'},
]}
VARIANTS = {'vehicles': [
    {'vehicleId': 1, 'typeName': 'E 400', 'fuelType': 'Petrol', 'cylinderCapacityCcm': 2996,
     'yearOfConstructionFrom': '201301', 'yearOfConstructionTo': '201612',
     'engineCodes': ['M 276.820']},
    {'vehicleId': 2, 'typeName': 'E 350 CDI', 'fuelType': 'Diesel',
     'cylinderCapacityCcm': 2987, 'yearOfConstructionFrom': '201001',
     'yearOfConstructionTo': '201612', 'engineCodes': ['OM 642.838']},
    {'vehicleId': 3, 'typeName': 'E 500', 'fuelType': 'Petrol', 'cylinderCapacityCcm': 4663},
]}


class ResolutionTests(unittest.TestCase):
    def api(self, *responses, cache=None):
        api = CatalogApiClient('fixture-key', cache=cache)
        api.http = FakeCatalogHttp(responses)
        return api

    def test_no_vin_uses_documented_hierarchy_and_preserves_engine_variants(self):
        api = self.api((200, MANUFACTURERS), (200, MODELS), (200, VARIANTS))
        report = api.resolve_vehicle(VEHICLE)
        self.assertEqual(report['status'], 'AMBIGUOUS')
        self.assertEqual([row['vehicle_id'] for row in report['candidates']], [1, 2])
        self.assertEqual(report['unresolved_fields'][0], 'fuel_type')
        self.assertEqual(report['candidates'][0]['engine_code'], 'M 276.820')
        urls = [call[1] for call in api.http.calls]
        self.assertIn('/api/manufacturers/list/type-id/1', urls[0])
        self.assertIn('/api/models/list/type-id/1/manufacturer-id/74/', urls[1])
        self.assertIn('/api/types/type-id/1/list-vehicles-types/8035/', urls[2])
        self.assertTrue(all(row['source_refs'] for row in report['candidates']))

    def test_fuel_or_short_engine_correction_filters_candidates_before_parts(self):
        api = self.api((200, MANUFACTURERS), (200, MODELS), (200, VARIANTS),
                       (200, GROUPS_REPLY), (200, ARTICLES_REPLY))
        report = api.lookup(None, {**VEHICLE, 'fuel_type': 'petrol'}, ['oil_filter'])
        self.assertEqual(report['status'], 'PROVISIONAL')
        self.assertEqual(report['items'][0]['code'], 'HU 7025 z')
        self.assertEqual(report['items'][0]['supplier'], 'MANN-FILTER')
        self.assertIn('/vehicle-id/1/', api.http.calls[-1][1])

    def test_ambiguous_candidates_do_not_look_up_parts_or_guess_an_engine(self):
        api = self.api((200, MANUFACTURERS), (200, MODELS), (200, VARIANTS))
        report = api.lookup(None, VEHICLE, ['oil_filter'])
        self.assertEqual(report['status'], 'CONFLICTED')
        self.assertEqual(report['items'], [])
        self.assertEqual(len(api.http.calls), 3)

    def test_unsupported_route_is_capability_failure_and_is_not_retried(self):
        api = self.api((404, b''))
        with self.assertRaises(PublicSourceError) as caught:
            api.resolve_vehicle(VEHICLE)
        self.assertEqual(caught.exception.code, 'CAPABILITY_UNAVAILABLE')
        with self.assertRaises(PublicSourceError):
            api.resolve_vehicle(VEHICLE)
        self.assertEqual(len(api.http.calls), 1)

    def test_quota_failure_opens_a_circuit_instead_of_repeating_requests(self):
        api = self.api((429, b''))
        for _ in range(2):
            with self.assertRaises(PublicSourceError) as caught:
                api.resolve_vehicle(VEHICLE)
            self.assertEqual(caught.exception.code, 'RATE_LIMITED')
        self.assertEqual(len(api.http.calls), 1)

    def test_expired_deadline_schedules_no_network(self):
        api = self.api()
        with self.assertRaises(PublicSourceError) as caught:
            api.resolve_vehicle(VEHICLE, deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, 'TIME_LIMIT')
        self.assertEqual(api.http.calls, [])

    def test_stated_year_body_engine_and_displacement_exclude_conflicts(self):
        variants = {'vehicles': [*VARIANTS['vehicles'],
            {'vehicleId': 4, 'typeName': 'E 400', 'fuelType': 'Petrol',
             'cylinderCapacityCcm': 2996, 'yearOfConstructionFrom': '201701',
             'engineCodes': ['M 276.820']},
            {'vehicleId': 5, 'typeName': 'E 400', 'fuelType': 'Petrol', 'bodyType': 'saloon',
             'cylinderCapacityCcm': 2996, 'engineCodes': ['M 276.820']},
        ]}
        api = self.api((200, MANUFACTURERS), (200, MODELS), (200, variants))
        report = api.resolve_vehicle({**VEHICLE, 'engine_code': 'M276820'})
        self.assertEqual(report['status'], 'RESOLVED')
        self.assertEqual([row['vehicle_id'] for row in report['candidates']], [1])

    def test_missing_provider_details_are_exposed_and_never_invented(self):
        api = self.api((200, MANUFACTURERS), (200, MODELS),
                       (200, {'vehicles': [{'vehicleId': 1, 'typeName': 'E 400'}]}))
        report = api.resolve_vehicle(VEHICLE)
        self.assertEqual(report['status'], 'UNRESOLVED')
        self.assertIn('engine_code', report['unresolved_fields'])
        self.assertIsNone(report['candidates'][0]['engine_code'])
        self.assertIsNone(report['candidates'][0]['displacement_l'])

    def test_retry_requires_enough_remaining_budget_and_runs_only_once(self):
        api = self.api((500, b''), (500, b''))
        with patch('automotive.catalog_api.sleep') as pause:
            with self.assertRaises(PublicSourceError):
                api.resolve_vehicle(VEHICLE)
        self.assertEqual(len(api.http.calls), 2)
        pause.assert_called_once_with(0.6)
        api = self.api((500, b''))
        with patch('automotive.catalog_api.sleep') as pause:
            with self.assertRaises(PublicSourceError) as caught:
                api.resolve_vehicle(VEHICLE, deadline=time.monotonic() + 0.2)
        self.assertEqual(caught.exception.code, 'TIME_LIMIT')
        pause.assert_not_called()
        self.assertEqual(len(api.http.calls), 1)

    def test_same_code_from_two_manufacturers_keeps_both_identities(self):
        from automotive.catalog_api import parse_articles

        records = parse_articles({'articles': [
            {'articleNo': 'A-1', 'supplierName': 'FIRST'},
            {'articleNo': 'A-1', 'supplierName': 'SECOND'},
        ]})
        self.assertEqual(len(records), 2)


class TTLCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private' / 'cache.sqlite3'
        self.now = 1000.0

    def cache(self):
        return SQLiteTTLCache(self.path, clock=lambda: self.now)

    def test_success_persists_for_24_hours_and_empty_for_60_seconds(self):
        cache = self.cache()
        cache.put('catalog.resolution', {'make': 'Example'}, {'candidates': [{'id': 1}]})
        cache.put('research', {'question': 'example'}, [], empty=True)
        self.assertEqual(self.cache().get('research', {'question': 'example'}), [])
        self.now += 61
        self.assertIsNone(cache.get('research', {'question': 'example'}))
        self.assertIsNotNone(cache.get('catalog.resolution', {'make': 'Example'}))
        self.now += 24 * 3600
        self.assertIsNone(cache.get('catalog.resolution', {'make': 'Example'}))

    def test_private_versioned_database_and_normalized_keys(self):
        cache = self.cache()
        cache.put('chooser', {'b': 2, 'a': 1}, {'records': []})
        self.assertEqual(cache.get('chooser', {'a': 1, 'b': 2}), {'records': []})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_no_stock_credentials_or_raw_vin_are_persisted(self):
        cache = self.cache()
        self.assertFalse(cache.put('catalog', {'vin': 'WDB12345678901234'}, {'id': 1}))
        self.assertFalse(cache.put('research', 'question', {'stock': 5}))
        self.assertFalse(cache.put('research', 'question', {'api_key': 'sensitive'}))
        self.assertFalse(cache.put('research', 'question', {'excerpt': 'WDB12345678901234'}))
        self.assertIsNone(cache.get('research', 'question'))
        if self.path.exists():
            self.assertNotIn(b'WDB12345678901234', self.path.read_bytes())

    def test_resolution_cache_avoids_repeated_catalog_calls(self):
        api = CatalogApiClient('fixture-key', cache=self.cache())
        api.http = FakeCatalogHttp([(200, MANUFACTURERS), (200, MODELS), (200, VARIANTS)])
        first = api.resolve_vehicle(VEHICLE)
        api2 = CatalogApiClient('fixture-key', cache=self.cache())
        api2.http = FakeCatalogHttp([])
        second = api2.resolve_vehicle(VEHICLE)
        self.assertEqual(first['candidates'], second['candidates'])
        self.assertTrue(second['cache_hit'])
        self.assertEqual(api2.http.calls, [])

    def test_unwritable_or_invalid_cache_is_only_a_cache_miss(self):
        self.path.parent.mkdir()
        self.path.write_text('not a sqlite database')
        cache = self.cache()
        self.assertIsNone(cache.get('catalog', 'key'))
        self.assertFalse(cache.put('catalog', 'key', {'records': []}))


if __name__ == '__main__':
    unittest.main()
