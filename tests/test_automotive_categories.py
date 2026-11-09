"""Multilingual discovery filters and filtering before warehouse pagination."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, Mock

from automotive.categories import category_for_query, matches_category
from automotive.repository import AutomotiveRepository
from automotive.vehicle_inventory import VehicleInventoryService

VEHICLE = {'make': 'Example', 'model': 'Car', 'model_year': 2013}


def product(identifier: str, name: str) -> dict:
    return {'source_product_id': identifier, 'product_code': 'AP104/2',
            'product_name': name, 'product_model': 'Example Car',
            'brand': 'Maker', 'stock': None, 'price': None}


class CategoryTests(unittest.TestCase):
    def test_multilingual_and_typo_queries(self):
        for query, category in [
            ('brakes', 'brakes'), ('breaks', 'brakes'), ('მუხრუჭები', 'brakes'),
            ('სამუხრუჭე ხუნდები', 'brake_pads'), ('rear brake pads', 'brake_pads'),
            ('სამუხრუჭე დისკი', 'brake_discs'), ('oil filter', 'oil_filter'),
            ('ზეთის ფილტრი', 'oil_filter'), ('engine oil', 'engine_oil'),
            ('ძრავის ზეთი', 'engine_oil'), ('air filters', 'air_filter'),
        ]:
            with self.subTest(query=query):
                self.assertEqual(category_for_query(query), category)

    def test_oil_filter_and_engine_oil_are_distinct(self):
        for name in ['Oil filter', 'ზეთის ფილტრი', 'Transmission oil', 'Brake oil', 'Coil']:
            self.assertFalse(matches_category(product('1', name), 'engine oil'), name)
        for name in ['Engine oil 5W30', 'ძრავის ზეთი 5W30']:
            self.assertTrue(matches_category(product('1', name), 'engine oil'), name)
            self.assertFalse(matches_category(product('1', name), 'oil filter'), name)

    def test_position_and_category_must_both_match(self):
        self.assertTrue(matches_category(product('1', 'უკანა სამუხრუჭე ხუნდი'),
                                         'rear brake pads'))
        self.assertFalse(matches_category(product('1', 'წინა სამუხრუჭე ხუნდი'),
                                          'rear brake pads'))
        self.assertFalse(matches_category(product('1', 'Rear shock absorber'),
                                          'rear brake pads'))

    def test_literal_unknown_queries_cannot_be_regex_injection(self):
        self.assertFalse(matches_category(product('1', 'ordinary product'), '.*'))
        self.assertTrue(matches_category(product('1', 'Literal .* product'), '.*'))

    def test_brakes_do_not_include_clutch_or_wheel_discs(self):
        for query in ('brakes', 'brake discs', 'discs'):
            self.assertFalse(matches_category(product('1', 'Clutch disc'), query))
            self.assertFalse(matches_category(product('1', 'Wheel disc'), query))
        self.assertTrue(matches_category(product('1', 'Rear brake disc'), 'discs'))


class CategoryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.inventory_products.return_value = []

    def test_warehouse_filter_is_passed_before_pagination(self):
        products = [product(str(i), 'Rear brake pads') for i in range(105)]
        def page(vehicle, limit, exclude_ids, offset=0, part_query=''):
            self.assertEqual(part_query, 'rear brake pads')
            filtered = [row for row in products if matches_category(row, part_query)]
            return {'items': filtered[offset:offset + limit],
                    'has_more': offset + limit < len(filtered)}
        self.repository.vehicle_catalog_candidates.side_effect = page
        result = VehicleInventoryService(self.repository).find_all(
            VEHICLE, part_query='rear brake pads',
        )
        self.assertEqual(len(result['items']), 105)
        self.assertFalse(result['has_more'])
        self.assertEqual(self.repository.vehicle_catalog_candidates.call_count, 2)

    def test_fitment_entries_are_filtered_before_slice_and_denials_remain(self):
        service = VehicleInventoryService(self.repository)
        entries = {
            key: {'product': product(key, name), 'compatible': compatible,
                  'status': 'VERIFIED', 'evidence': []}
            for key, name, compatible in [
                ('1', 'Engine oil', True), ('2', 'Brake pads', True),
                ('3', 'Brake discs', False),
            ]
        }
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        service.fitment_entries = Mock(return_value=entries)
        result = service.find(VEHICLE, limit=1, part_query='brakes')
        self.assertEqual(result['items'][0]['product']['source_product_id'], '2')
        self.assertEqual(result['excluded_items'][0]['product']['source_product_id'], '3')
        self.assertFalse(result['has_more'])

    def test_repository_parameterizes_name_and_filters_before_limit(self):
        repository = AutomotiveRepository('unused')
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [product('1', 'Rear brake pads')]
        context = MagicMock()
        context.__enter__.return_value = connection
        repository._connect = Mock(return_value=context)
        result = repository.search_products('rear brake pads', limit=1, offset=4)
        statement, parameters = connection.execute.call_args.args
        rendered = statement.as_string()
        self.assertLess(rendered.index('~* %s'), rendered.index('LIMIT %s'))
        self.assertNotIn('rear', rendered)
        self.assertEqual(parameters[-2:], (2, 4))
        self.assertIsNone(result['items'][0]['stock'])
        self.assertIsNone(result['items'][0]['price'])

    def test_vehicle_query_parameters_keep_category_before_limit(self):
        repository = AutomotiveRepository('unused')
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        context = MagicMock()
        context.__enter__.return_value = connection
        repository._connect = Mock(return_value=context)
        repository.vehicle_catalog_candidates(VEHICLE, limit=2, offset=4, part_query='engine oil')
        statement, parameters = connection.execute.call_args.args
        rendered = statement.as_string()
        self.assertLess(rendered.index('!~* %s'), rendered.index('LIMIT %s'))
        self.assertEqual(parameters[-2:], (3, 4))


if __name__ == '__main__':
    unittest.main()
