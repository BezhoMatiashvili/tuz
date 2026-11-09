"""Synthetic catalog/evidence tests; no actual customer inventory claims."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from automotive.vehicle_inventory import VehicleInventoryService

VEHICLE = {'make': 'Example', 'model': 'Car', 'model_year': 2013}


def product(product_id='p1', **overrides):
    return {'source_product_id': product_id, 'product_code': 'AP104/2',
            'original_code': '', 'duplicate_code': '', 'brand': 'Maker',
            'source_fingerprint': 'a' * 64, 'price': None, 'stock': None,
            'product_model': 'Example Car', **overrides}


def evidence(**overrides):
    return {'evidence_id': 'ev_fixture', 'part_code': 'AP104/2', 'manufacturer': 'Maker',
            'compatible': True, 'applicability': VEHICLE, 'restrictions': {},
            'authority': 'manufacturer', 'reuse_permission': 'authorized',
            'review_status': 'approved', 'reviewed_by': 'reviewer', **overrides}


class VehicleInventoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.inventory_products.return_value = [product()]
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.service = VehicleInventoryService(self.repository)

    def test_requires_vehicle_instead_of_listing_arbitrary_inventory(self):
        result = self.service.find({})
        self.assertEqual(result['status'], 'INSUFFICIENT_DATA')
        self.repository.vehicle_fitment_records.assert_not_called()

    def test_catalog_text_matches_never_become_fitment_or_stock_claims(self):
        self.repository.vehicle_catalog_candidates.return_value = {
            'items': [product(product_model='Example Car 2001')], 'has_more': False,
        }
        result = self.service.find(VEHICLE)
        self.assertEqual(result['verified_count'], 0)
        item = result['items'][0]
        self.assertEqual(item['status'], 'UNKNOWN')
        self.assertIsNone(item['compatible'])
        self.assertIsNone(item['product']['stock'])
        self.assertEqual(item['match_type'], 'CATALOG_TEXT')
        self.assertTrue(result['clarification'])
        self.repository.inventory_products.assert_not_called()

    def test_exact_manufacturer_code_and_applicable_verified_evidence(self):
        self.repository.vehicle_fitment_records.return_value = [evidence()]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['verified_count'], 1)
        self.assertEqual(result['items'][0]['status'], 'VERIFIED')
        self.assertTrue(result['items'][0]['compatible'])
        self.assertEqual(result['items'][0]['evidence'][0]['evidence_id'], 'ev_fixture')

    def test_code_cooccurrence_does_not_establish_interchangeability(self):
        self.repository.vehicle_fitment_records.return_value = [evidence(part_code='OE123')]
        self.repository.inventory_products.return_value = [product(original_code='OE123')]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['verified_count'], 0)
        self.assertEqual(result['items'][0]['status'], 'PROVISIONAL')
        self.assertIsNone(result['items'][0]['compatible'])

    def test_normalization_collision_remains_candidates(self):
        self.repository.vehicle_fitment_records.return_value = [evidence(part_code='AB123')]
        self.repository.inventory_products.return_value = [
            product(product_code='AB123'), product('p2', product_code='AB-123'),
        ]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['verified_count'], 0)
        self.assertEqual(result['candidate_count'], 2)

    def test_missing_engine_specific_scope_does_not_match(self):
        self.repository.vehicle_fitment_records.return_value = [
            evidence(applicability={**VEHICLE, 'engine_code': 'E1'}),
        ]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['items'], [])
        self.assertTrue(any('ძრავის კოდი' in warning for warning in result['warnings']))

    def test_drive_and_body_constraints_use_selected_configuration(self):
        self.repository.vehicle_fitment_records.return_value = [
            evidence(applicability={**VEHICLE, 'drive': 'AWD', 'body': 'Sedan'}),
        ]
        self.assertEqual(self.service.find(VEHICLE)['verified_count'], 0)
        selected = {**VEHICLE, 'drive': 'AWD', 'body': 'Sedan'}
        self.assertEqual(self.service.find(selected)['verified_count'], 1)
        self.assertEqual(self.service.find({**selected, 'drive': 'FWD'})['verified_count'], 0)

    def test_generic_or_different_vehicle_evidence_is_not_applied(self):
        for scope in ({}, {**VEHICLE, 'model': 'Different'}, {**VEHICLE, 'model_year': 2001}):
            with self.subTest(scope=scope):
                self.repository.vehicle_fitment_records.return_value = [
                    evidence(applicability=scope),
                ]
                self.assertEqual(self.service.find(VEHICLE)['verified_count'], 0)

    def test_missing_year_scope_cannot_confirm_current_model_year(self):
        self.repository.vehicle_fitment_records.return_value = [
            evidence(applicability={'make': 'Example', 'model': 'Car'}),
        ]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['verified_count'], 0)
        self.assertIsNone(result['items'][0]['compatible'])

    def test_product_specific_evidence_requires_current_fingerprint(self):
        self.repository.vehicle_fitment_records.return_value = [
            evidence(product_id='p1', source_fingerprint='b' * 64),
        ]
        self.assertEqual(self.service.find(VEHICLE)['items'], [])
        self.repository.vehicle_fitment_records.return_value = [
            evidence(product_id='p1', source_fingerprint='a' * 64, part_code='OE123'),
        ]
        self.assertEqual(self.service.find(VEHICLE)['verified_count'], 1)

    def test_negative_fitment_excluded_and_cannot_reappear_as_text_candidate(self):
        self.repository.vehicle_fitment_records.return_value = [evidence(compatible=False)]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['excluded_count'], 1)
        excluded = self.repository.vehicle_catalog_candidates.call_args.kwargs['exclude_ids']
        self.assertIn('p1', excluded)

    def test_pending_and_conflicting_evidence_do_not_confirm_fitment(self):
        self.repository.vehicle_fitment_records.return_value = [evidence(review_status='pending')]
        self.assertEqual(self.service.find(VEHICLE)['verified_count'], 0)
        self.repository.vehicle_fitment_records.return_value = [
            evidence(), evidence(compatible=False, evidence_id='ev_other'),
        ]
        result = self.service.find(VEHICLE)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(len(result['items'][0]['evidence']), 2)
        self.assertIsNone(result['items'][0]['compatible'])

    def test_limits_and_priority_preserve_verified_results(self):
        self.repository.vehicle_fitment_records.return_value = [evidence()]
        self.repository.vehicle_catalog_candidates.return_value = {
            'items': [product('p2')], 'has_more': True,
        }
        result = self.service.find(VEHICLE, limit=1)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['verified_count'], 1)
        self.assertTrue(result['has_more'])

    def test_all_pages_are_read_once_and_bound_is_reported(self):
        products = [product(str(i)) for i in range(205)]
        def page(vehicle, limit, exclude_ids, offset=0):
            return {'items': products[offset:offset + limit],
                    'has_more': offset + limit < len(products)}
        self.repository.vehicle_catalog_candidates.side_effect = page
        result = self.service.find_all(VEHICLE)
        self.assertEqual(len(result['items']), 205)
        self.assertFalse(result['has_more'])
        self.repository.vehicle_fitment_records.assert_called_once()
        self.assertEqual(self.repository.vehicle_catalog_candidates.call_count, 3)
        bounded = self.service.find_all(VEHICLE, max_items=150)
        self.assertEqual(len(bounded['items']), 150)
        self.assertTrue(bounded['has_more'])

    def test_evidence_pages_do_not_skip_the_first_text_products(self):
        self.repository.inventory_products.return_value = [product(str(i)) for i in range(105)]
        self.repository.vehicle_fitment_records.return_value = [evidence()]
        products = [product('text-' + str(i)) for i in range(103)]
        def page(vehicle, limit, exclude_ids, offset=0):
            return {'items': products[offset:offset + limit],
                    'has_more': offset + limit < len(products)}
        self.repository.vehicle_catalog_candidates.side_effect = page
        result = self.service.find_all(VEHICLE)
        self.assertEqual(len(result['items']), 208)
        self.assertEqual(len({i['product']['source_product_id'] for i in result['items']}), 208)
        self.assertFalse(result['has_more'])

    def test_later_page_failure_preserves_read_rows_and_reports_partial(self):
        products = [product(str(i)) for i in range(100)]
        self.repository.vehicle_catalog_candidates.side_effect = [
            {'items': products, 'has_more': True}, RuntimeError('private SQL details'),
        ]
        result = self.service.find_all(VEHICLE)
        self.assertEqual(len(result['items']), 100)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['error_code'], 'DATABASE_UNAVAILABLE')
        self.assertNotIn('private SQL details', str(result))
