"""Name fallback and the optional catalog API (synthetic fixtures, no network)."""

from __future__ import annotations

import json
import unittest

from automotive import name_match
from automotive.catalog_api import (
    CatalogApiClient,
    categories_for,
    parse_articles,
    parse_group_ids,
    parse_vehicles,
)
from automotive.public_http import PublicSourceError
from tests.test_automotive_vin_search import VIN

KEY = 'secret-catalog-key-123'


class YearFitTests(unittest.TestCase):
    def fit(self, text, year, model='Accord'):
        return name_match.year_fit(text, model, year)

    def test_a_stated_range_decides_in_or_out(self):
        self.assertEqual(self.fit('ხუნდი წინა HONDA ACCORD 98-03', 2003), name_match.IN)
        self.assertEqual(self.fit('ხუნდი წინა HONDA ACCORD 98-03', 2004), name_match.OUT)
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD 08-', 2015), name_match.IN)
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD 08-', 2003), name_match.OUT)
        self.assertEqual(self.fit('ხუნდი HONDA FIT 2010 >', 2012, 'Fit'), name_match.IN)
        self.assertEqual(self.fit('ხუნდი HONDA FIT 2010 >', 2008, 'Fit'), name_match.OUT)
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD 2010-2014', 2012), name_match.IN)
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD 18-', 2003), name_match.OUT)

    def test_no_range_or_an_ambiguous_one_is_not_a_decision(self):
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD', 2003), name_match.NONE)
        self.assertEqual(self.fit('ხუნდი HONDA CRV, 3, ACCORD 07,', 2003), name_match.NONE)
        self.assertEqual(self.fit('ხუნდი HONDA ACCORD 08-', None), name_match.NONE)

    def test_the_range_belongs_to_the_models_named_next_to_it(self):
        text = 'ხუნდი წინა HONDA CRV-3, ACCORD 07-'
        self.assertEqual(self.fit(text, 2003), name_match.OUT)
        self.assertEqual(self.fit(text, 2009), name_match.IN)
        self.assertEqual(self.fit(text, 2003, 'CRV'), name_match.NONE)

    def test_make_aliases_and_flexible_model_spelling(self):
        self.assertIn('MB', name_match.make_pattern('MERCEDES-BENZ'))
        self.assertIn('VW', name_match.make_pattern('Volkswagen'))
        self.assertEqual(name_match.make_pattern('Honda'), r'\m(?:Honda)\M')
        self.assertEqual(name_match.year_fit('MB E CLASS 09-', 'E-Class', 2012), name_match.IN)
        self.assertEqual(name_match.year_fit('HONDA CRV 07-', 'CR-V', 2010), name_match.IN)


class FakeCatalogHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def reserve(self):
        return None

    def read(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get('headers')))
        status, body = self.responses.pop(0)
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


VEHICLE_REPLY = {'data': {'matchingVehicles': {'array': [
    {'vehicleId': 19942, 'carName': 'EXAMPLE CAR Coupe E 400', 'linkageTargetType': 'P',
     'manuId': 74, 'modelId': 8035}]}, 'matchingVehiclesCount': 1}, 'status': 200}


def group_row(*pairs):
    row = {f'categoryName{i}': None for i in range(1, 5)} | {f'categoryId{i}': None
                                                              for i in range(1, 5)}
    for index, (name, identifier) in enumerate(pairs, 1):
        row[f'categoryName{index}'], row[f'categoryId{index}'] = name, identifier
    return row


GROUPS_REPLY = {'categories': [
    group_row(('Filters', 100005)), group_row(('Filters', 100005), ('Oil Filter', 100259)),
    group_row(('Engine', 100002), ('Lubrication', 100245), ('Oil Filter', 100470)),
    group_row(('Braking System', 100001), ('Disc Brake', 100626), ('Brake Pad', 100030)),
    group_row(('Braking System', 100001), ('Disc Brake', 100626), ('Brake Disc', 100032)),
]}
ARTICLES_REPLY = {'articles': [
    {'articleNo': 'HU 7025 z', 'supplierName': 'MANN-FILTER', 'articleProductName': 'Oil Filter'},
    {'articleNo': 'OX 814D', 'supplierName': 'KNECHT', 'articleProductName': 'Oil Filter'},
    {'articleNo': 'GASKET1', 'supplierName': 'ELRING', 'articleProductName': 'Seal Ring'},
]}
PADS_REPLY = {'articles': [
    {'articleNo': '39590', 'supplierName': 'ABS',
     'articleProductName': 'Warning Contact, brake pad wear'},
    {'articleNo': '37586', 'supplierName': 'ABS',
     'articleProductName': 'Brake Pad Set, disc brake'},
]}


def client(*responses):
    result = CatalogApiClient(KEY)
    result.http = FakeCatalogHttp(responses)
    return result


class CatalogApiTests(unittest.TestCase):
    def test_vin_to_vehicle_to_part_numbers_by_the_vehicles_own_part_group(self):
        api = client((200, VEHICLE_REPLY), (200, GROUPS_REPLY), (200, ARTICLES_REPLY))
        report = api.lookup(VIN, {}, ['oil_filter'])
        self.assertEqual(report['status'], 'PROVISIONAL')
        self.assertEqual(report['vehicle_label'], 'EXAMPLE CAR Coupe E 400')
        # Only articles named for the part; the seal ring in the same group is dropped.
        self.assertEqual([i['code'] for i in report['items']], ['HU 7025 z', 'OX 814D'])
        self.assertEqual(report['items'][0]['category'], 'oil_filter')
        urls = [call[1] for call in api.http.calls]
        self.assertIn('/vin/tecdoc-vin-check/', urls[0])
        self.assertIn('/products-groups-variant-1/19942/', urls[1])
        self.assertIn('/vehicle-id/19942/category-id/100259/', urls[2])  # First oil filter group.
        self.assertNotIn(VIN, json.dumps(report))
        self.assertNotIn(KEY, json.dumps(report))
        self.assertEqual(api.http.calls[0][2]['x-rapidapi-key'], KEY)

    def test_brake_pads_keep_pad_sets_not_wear_sensors_and_groups_are_cached(self):
        api = client((200, VEHICLE_REPLY), (200, GROUPS_REPLY), (200, PADS_REPLY))
        report = api.lookup(VIN, {}, ['brake_pads'])
        self.assertEqual([i['code'] for i in report['items']], ['37586'])
        self.assertIn('/category-id/100030/', api.http.calls[2][1])
        again = api.lookup(VIN, {}, ['brake_pads'])  # Vehicle, groups and articles are cached.
        self.assertEqual(len(api.http.calls), 3)
        self.assertEqual([i['code'] for i in again['items']], ['37586'])

    def test_several_categories_run_side_by_side_and_keep_their_own_items(self):
        api = client((200, VEHICLE_REPLY), (200, GROUPS_REPLY))
        api.http.read = lambda method, url, **kw: (
            api.http.calls.append((method, url, kw.get('headers'))) or
            (200, json.dumps(PADS_REPLY if '/category-id/100030/' in url else ARTICLES_REPLY)
             .encode()) if '/articles/list/' in url else
            (200, json.dumps(VEHICLE_REPLY if 'vin-check' in url else GROUPS_REPLY).encode()))
        report = api.lookup(VIN, {}, ['brake_pads', 'oil_filter'])
        by_category = {}
        for item in report['items']:
            by_category.setdefault(item['category'], []).append(item['code'])
        self.assertEqual(by_category, {'brake_pads': ['37586'], 'oil_filter': ['HU 7025 z',
                                                                              'OX 814D']})

    def test_the_vin_cache_holds_no_raw_vin_and_warm_resolves_vehicle_and_groups(self):
        api = client((200, VEHICLE_REPLY), (200, GROUPS_REPLY))
        api.warm(VIN)
        self.assertEqual(len(api.http.calls), 2)
        self.assertTrue(all(VIN not in key for key in api._vehicles))

    def test_a_part_group_the_car_does_not_have_is_simply_empty(self):
        api = client((200, VEHICLE_REPLY), (200, GROUPS_REPLY))
        report = api.lookup(VIN, {}, ['suspension'])
        self.assertEqual((report['status'], report['items']), ('EMPTY', []))

    def test_several_catalog_vehicles_for_one_vin_are_not_guessed(self):
        api = client((200, {'data': {'matchingVehicles': {'array': [
            {'vehicleId': 1}, {'vehicleId': 2}]}}}))
        report = api.lookup(VIN, {}, ['oil_filter'])
        self.assertEqual((report['status'], report['items']), ('CONFLICTED', []))
        self.assertEqual(len(api.http.calls), 1)

    def test_no_vehicle_means_empty_and_errors_carry_only_a_safe_code(self):
        self.assertEqual(client((404, b'')).lookup(VIN, {}, ['oil_filter'])['status'], 'EMPTY')
        with self.assertRaises(PublicSourceError) as caught:
            client((401, b'denied')).lookup(VIN, {}, ['oil_filter'])
        self.assertEqual(caught.exception.code, 'ACCESS_REQUIRED')
        self.assertNotIn(KEY, str(caught.exception))
        with self.assertRaises(PublicSourceError) as caught:
            client((200, b'not json')).lookup(VIN, {}, ['oil_filter'])
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')

    def test_requires_a_key_and_https(self):
        with self.assertRaises(ValueError):
            CatalogApiClient('')
        with self.assertRaises(ValueError):
            CatalogApiClient(KEY, base_url='http://insecure.example')

    def test_category_mapping_and_tolerant_parsers(self):
        self.assertEqual(categories_for('oil change'), ['oil_change'])
        self.assertEqual(categories_for('front brake pads'), ['brake_pads'])
        self.assertEqual(categories_for('engine oil'), [])  # Oil is chosen by grade instead.
        self.assertIn('brake_discs', categories_for(''))
        vehicles = parse_vehicles({'x': [{'carId': '7'}, {'carId': True}]})
        self.assertEqual(vehicles[0]['vehicle_id'], 7)
        article = parse_articles({'a': {'b': [{'articleNumber': 'X1', 'oemNumbers': [
            {'oemDisplayNo': 'A 1'}, 'A2']}]}})[0]
        self.assertEqual((article['code'], article['oem_codes']), ('X1', ['A 1', 'A2']))
        groups = parse_group_ids(GROUPS_REPLY)
        self.assertEqual(groups['oil filter'], [100259, 100470])
        self.assertEqual(groups['brake pad'], [100030])
        self.assertNotIn('lubrication', groups)  # Only the deepest name of a row is a leaf.


if __name__ == '__main__':
    unittest.main()
