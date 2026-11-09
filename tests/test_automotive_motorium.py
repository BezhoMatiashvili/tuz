"""Observed Motorium schemas with synthetic, non-customer protocol fixtures."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from automotive.motorium import ASSISTANT_URL, MotoriumClient, MotoriumError

VIN = '1HGCM82633A004352'


def source(**overrides):
    return {
        'reply': 'Invented model prose: FAKE999 is guaranteed to fit.',
        'vehicle': {'make': 'Example', 'model': 'Car', 'year': 2013, 'frame': VIN},
        'context': {'vehicle': {'make': 'Example', 'model': 'Car', 'year': 2013},
                    'part': {'label': 'ზეთის ფილტრი'}},
        'products': [{'id': 17, 'mpn': 'AP104/2', 'sap': 'supplier-17',
                      'brand': 'Maker', 'name': 'Synthetic filter'}],
        'ai_enabled': True, **overrides,
    }


def response(body, status=200):
    result = MagicMock()
    result.status_code = status
    result.iter_content.return_value = [body if isinstance(body, bytes) else
                                        json.dumps(body).encode()]
    return result


def details(**overrides):
    product = {'@type': 'Product', 'mpn': 'AP104/2', 'sku': 'supplier-17',
               'brand': {'name': 'Maker'}, 'additionalProperty': [
                   {'name': 'OEM', 'value': 'OE-123; AP104/2'},
                   {'name': 'Compatible vehicles', 'value': 'Example Car (2010–2015)'},
               ], **overrides}
    return ('<html><script type="application/ld+json">' + json.dumps(product) +
            '</script></html>').encode()


class MotoriumTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch('automotive.motorium.requests.Session')
        self.addCleanup(self.patch.stop)
        self.factory = self.patch.start()
        self.session = self.factory.return_value.__enter__.return_value
        self.session.post.return_value = response(source())
        self.session.get.return_value = response(details())
        self.client = MotoriumClient()

    def test_observed_protocol_and_detail_codes_ignore_model_prose(self):
        result = self.client.lookup(VIN, 'ზეთის ფილტრი')
        self.assertEqual(result['status'], 'PROVISIONAL')
        item = result['items'][0]
        self.assertEqual(item['mpn'], 'AP104/2')
        self.assertEqual(item['oem_codes'], ['OE-123', 'AP104/2'])
        self.assertEqual(item['supplier_sku'], 'supplier-17')
        self.assertEqual(item['source_url'], 'https://motorium.ge/product.php?id=17')
        self.assertNotIn('FAKE999', json.dumps(result))
        self.assertNotIn(VIN, json.dumps(result))
        call = self.session.post.call_args
        self.assertEqual(call.args[0], ASSISTANT_URL)
        self.assertEqual(call.kwargs['json']['history'], [])
        self.assertEqual(call.kwargs['json']['context'], {'vehicle': None, 'part': None})
        self.assertIn(VIN, call.kwargs['json']['message'])
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertTrue(call.kwargs['stream'])
        self.session.post.return_value.close.assert_called_once()

    def test_wrong_catalog_model_or_wrong_echoed_vin_withholds_codes(self):
        for overrides in (
            {'context': {'vehicle': {'make': 'Example', 'model': 'Different', 'year': 2013}}},
            {'vehicle': {'make': 'Example', 'model': 'Car', 'year': 2013,
                         'frame': '1M8GDM9AXKP042788'}},
        ):
            with self.subTest(overrides=overrides):
                self.session.post.return_value = response(source(**overrides))
                result = self.client.lookup(VIN, 'filter')
                self.assertEqual(result['status'], 'CONFLICTED')
                self.assertEqual(result['items'], [])
                self.session.get.assert_not_called()

    def test_generic_products_without_vin_context_never_become_vin_results(self):
        self.session.post.return_value = response(source(vehicle=None))
        result = self.client.lookup(VIN, 'filter')
        self.assertEqual(result['items'], [])
        self.assertFalse(result['vehicle_context_matched'])
        self.session.get.assert_not_called()
        self.session.post.return_value = response(source(vehicle={
            'make': 'Example', 'model': 'Car', 'year': 2013,
        }))
        self.assertEqual(self.client.lookup(VIN, 'filter')['items'], [])

    def test_missing_part_context_and_invalid_identity_are_not_guessed(self):
        for data in (
            source(context={'vehicle': {'make': 'Example', 'model': 'Car', 'year': 2013}}),
            source(vehicle={'make': True, 'model': 'Car', 'year': 2013}),
            source(vehicle={'make': 'Example', 'model': 'Car', 'year': True}),
        ):
            self.session.post.return_value = response(data)
            self.assertEqual(self.client.lookup(VIN, 'filter')['items'], [])

    def test_detail_identity_mismatch_does_not_merge_oem_codes(self):
        self.session.get.return_value = response(details(mpn='OTHER999'))
        result = self.client.lookup(VIN, 'filter')
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'][0]['oem_codes'], [])
        self.assertIn('PRODUCT_DETAIL_MISMATCH', result['items'][0]['warnings'])

    def test_detail_failure_preserves_catalog_mpn_and_unknown_oem(self):
        self.session.get.return_value = response(b'unavailable', status=503)
        result = self.client.lookup(VIN, 'filter')
        self.assertEqual(result['items'][0]['mpn'], 'AP104/2')
        self.assertEqual(result['items'][0]['oem_codes'], [])
        self.assertIn('PRODUCT_DETAILS_UNAVAILABLE', result['items'][0]['warnings'])

    def test_rate_limit_and_access_requirement_are_not_retried_or_bypassed(self):
        for status, code in ((429, 'RATE_LIMITED'), (403, 'ACCESS_REQUIRED'),
                             (302, 'ACCESS_REQUIRED')):
            self.session.post.reset_mock()
            self.session.post.return_value = response(b'private body', status=status)
            with self.assertRaises(MotoriumError) as error:
                self.client.lookup(VIN)
            self.assertEqual(error.exception.code, code)
            self.assertNotIn('private', str(error.exception))
            self.session.post.assert_called_once()

    def test_detail_rate_limit_stops_further_product_requests(self):
        self.session.post.return_value = response(source(products=source()['products'] * 3))
        self.session.get.return_value = response(b'rate limited', status=429)
        with self.assertRaises(MotoriumError) as error:
            self.client.lookup(VIN, limit=3)
        self.assertEqual(error.exception.code, 'RATE_LIMITED')
        self.session.get.assert_called_once()

    def test_size_limits_and_invalid_json_fail_closed(self):
        for body in (b'bad JSON', b'0' * 2_000_001):
            self.session.post.return_value = response(body)
            with self.assertRaises(MotoriumError) as error:
                self.client.lookup(VIN)
            self.assertEqual(error.exception.code, 'INVALID_RESPONSE')

    def test_inputs_are_bounded_before_outbound_request(self):
        for vin, query, limit in (('bad', '', 1), (VIN, 'x' * 161, 1),
                                  (VIN, '', 7), (VIN, '', True), (VIN, '', 1.5)):
            with self.assertRaises(ValueError):
                self.client.lookup(vin, query, limit)
        self.session.post.assert_not_called()

    def test_limits_and_invalid_product_ids_cannot_trigger_arbitrary_requests(self):
        self.session.post.return_value = response(source(products=[
            {'id': 'https://invalid.example', 'mpn': 'AB123'},
            {'id': True, 'mpn': 'AB123'},
            *source()['products'],
        ]))
        result = self.client.lookup(VIN, limit=2)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['has_more'])
        self.session.get.assert_not_called()

    def test_shared_lookup_budget_prevents_unbounded_category_calls(self):
        self.session.post.return_value = response(source(products=[]))
        for _ in range(10):
            self.client.lookup(VIN)
        with self.assertRaises(MotoriumError) as error:
            self.client.lookup(VIN)
        self.assertEqual(error.exception.code, 'RATE_LIMITED')
        self.assertEqual(self.session.post.call_count, 10)
