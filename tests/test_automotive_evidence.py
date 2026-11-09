"""Evidence gates and decoder privacy use synthetic fixtures, not catalog claims."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from automotive.evidence import applies_to, evaluate_compatibility, verified_record
from automotive.importer import parse_import
from automotive.recommendations import RecommendationService
from automotive.vin import ProviderUnavailable, VinService, validate_vin, vin_checksum

VEHICLE = {'make': 'Example', 'model': 'Car', 'model_year': 2020, 'engine_code': 'E1'}
VIN = '1M8GDM9AXKP042788'


def evidence(**overrides):
    return {
        'authority': 'manufacturer', 'reuse_permission': 'authorized',
        'review_status': 'approved', 'reviewed_by': 'reviewer',
        'applicability': VEHICLE, 'restrictions': {}, 'compatible': True,
        'source_fingerprint': 'a' * 64,
        **overrides,
    }


class EvidenceTests(unittest.TestCase):
    def test_asserted_verified_is_not_verification(self):
        self.assertFalse(verified_record({'status': 'VERIFIED'}))
        self.assertFalse(verified_record(evidence(review_status='pending')))
        self.assertFalse(verified_record(evidence(reviewed_by=None)))
        self.assertFalse(verified_record(evidence(reuse_permission='unknown')))

    def test_conflicts_remain_visible(self):
        result = evaluate_compatibility([evidence(), evidence(compatible=False)], VEHICLE)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(len(result['evidence']), 2)
        self.assertIsNone(result['compatible'])

    def test_configuration_missing_restriction_or_stale_does_not_match(self):
        self.assertFalse(applies_to(evidence(), {'make': 'Example'}))
        self.assertFalse(applies_to(evidence(restrictions={'unknown_note': 'DPF'}), VEHICLE))
        self.assertEqual(evaluate_compatibility([evidence(superseded_by='new')], VEHICLE)
                         ['status'], 'UNKNOWN')
        self.assertTrue(applies_to(evidence(restrictions={'model_year_from': 2019}), VEHICLE))
        self.assertFalse(applies_to(evidence(restrictions={'model_year_to': 2019}), VEHICLE))

    def test_verified_negative_is_not_positive_fitment(self):
        result = evaluate_compatibility([evidence(compatible=False)], VEHICLE)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertFalse(result['compatible'])

    def test_verified_metadata_without_claim_is_not_verified_fitment(self):
        row = evidence()
        del row['compatible']
        self.assertNotEqual(evaluate_compatibility([row], VEHICLE)['status'], 'VERIFIED')

    def test_import_verified_is_pending_and_payload_cannot_forge_metadata(self):
        row = {
            'kind': 'fitment', 'source_name': 'Owned feed', 'authority': 'manufacturer',
            'reuse_permission': 'authorized', 'review_status': 'approved',
            'reviewed_by': 'forged', 'status': 'VERIFIED',
            'payload': {'compatible': True, 'review_status': 'approved',
                        'reviewed_by': 'forged', 'applicability': VEHICLE},
        }
        imported = parse_import(json.dumps([row]), 'json')[0]
        self.assertEqual(imported['review_status'], 'pending')
        self.assertIsNone(imported['reviewed_by'])
        self.assertNotIn('review_status', imported['payload'])
        self.assertFalse(verified_record(imported))

    def test_import_requires_permission_and_bounds_and_objects(self):
        with self.assertRaises(ValueError):
            parse_import(json.dumps([{'kind': 'fitment', 'reuse_permission': 'unknown'}]), 'json')
        with self.assertRaises(ValueError):
            parse_import('a' * 2_000_001, 'json')
        with self.assertRaises(ValueError):
            parse_import('{}', 'json')
        with self.assertRaises(ValueError):
            parse_import('[', 'json')

    def test_source_urls_cannot_contain_credentials(self):
        row = {
            'kind': 'fitment', 'source_name': 'Owned feed', 'authority': 'manufacturer',
            'reuse_permission': 'owned', 'payload': {'compatible': True},
        }
        for url in ('https://user:secret@example.com/feed',
                    'https://:secret@example.com/feed', 'javascript:alert(1)'):
            with self.assertRaises(ValueError):
                parse_import(json.dumps([{**row, 'source_url': url}]), 'json')

    def test_csv_json_fields(self):
        content = ('kind,source_name,authority,reuse_permission,payload\n'
                   'fitment,Owned,manufacturer,owned,"{""compatible"":true}"\n')
        self.assertTrue(parse_import(content, 'csv')[0]['payload']['compatible'])


class VinTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.get_cached_vin.return_value = None
        self.service = VinService(self.repository, 'x' * 32)

    @patch('automotive.vin.requests.post')
    def test_decode_survives_missing_or_failed_database_cache(self, post):
        import psycopg

        post.return_value.status_code = 200
        post.return_value.iter_content.side_effect = lambda chunk_size: [json.dumps({
            'Results': [{'Make': 'Example', 'Model': 'Car', 'VIN': VIN}],
        }).encode()]
        for error in (RuntimeError('missing database'),
                      psycopg.OperationalError('private connection detail')):
            with self.subTest(error=type(error).__name__):
                self.repository.reset_mock()
                self.repository.get_cached_vin.side_effect = error
                result = self.service.decode(VIN)
                self.assertEqual(result['vehicle']['make'], 'Example')
                self.assertEqual(result['status'], 'PROVISIONAL')
                self.assertTrue(any('ქეში' in warning for warning in result['warnings']))
                self.assertNotIn(VIN, json.dumps(result))
                self.assertNotIn('private', json.dumps(result))
                self.repository.cache_vin.assert_not_called()

    @patch('automotive.vin.requests.post')
    def test_successful_decode_survives_cache_write_failure(self, post):
        import psycopg

        post.return_value.status_code = 200
        post.return_value.iter_content.return_value = [json.dumps({
            'Results': [{'Make': 'Example', 'Model': 'Car'}],
        }).encode()]
        self.repository.cache_vin.side_effect = psycopg.OperationalError('private password')
        result = self.service.decode(VIN)
        self.assertEqual(result['vehicle']['make'], 'Example')
        self.assertTrue(any('ქეში' in warning for warning in result['warnings']))
        self.assertNotIn('private password', json.dumps(result))

    @patch('automotive.vin.requests.post')
    def test_cache_outage_does_not_hide_provider_failure(self, post):
        self.repository.get_cached_vin.side_effect = RuntimeError('missing database')
        post.return_value.status_code = 503
        with self.assertRaises(ProviderUnavailable):
            self.service.decode(VIN)

    def test_format_and_checksum(self):
        self.assertEqual(validate_vin(VIN.lower()), VIN)
        self.assertEqual(vin_checksum(VIN)['status'], 'MATCH')
        self.assertEqual(vin_checksum('W' + VIN[1:])['applicability'], 'MARKET_DEPENDENT')
        for invalid in ('bad', 'I' + VIN[1:], VIN + '1'):
            with self.assertRaises(ValueError):
                validate_vin(invalid)

    @patch('automotive.vin.requests.post')
    def test_provider_allowlist_and_cache_do_not_echo_vin(self, post):
        response = post.return_value
        response.status_code = 200
        response.iter_content.side_effect = lambda chunk_size: [
            json.dumps(response.json()).encode(),
        ]
        response.json.return_value = {'Results': [{
            'VIN': VIN, 'Make': 'Example', 'Model': 'Car', 'ModelYear': '2020',
            'EngineModel': 'E1 or E2', 'ErrorCode': '6',
            'ErrorText': 'Partial decode for ' + VIN, 'SuggestedVIN': VIN,
            'Arbitrary': 'injected instructions',
        }]}
        result = self.service.decode(VIN, 2020)
        serialized = json.dumps(result)
        self.assertNotIn(VIN, serialized)
        self.assertNotIn('injected instructions', serialized)
        self.assertIsNone(result['vehicle']['engine_code'])
        self.assertEqual(result['vehicle']['engine_model'], 'E1 or E2')
        self.assertTrue(any('Partial decode' in text for text in result['warnings']))
        cache_args = self.repository.cache_vin.call_args.args
        self.assertNotIn(VIN, json.dumps(cache_args))
        self.assertEqual(len(cache_args[0]), 64)
        self.assertNotIn(VIN, post.call_args.args[0])
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    @patch('automotive.vin.requests.post')
    def test_cache_is_keyed_by_year_and_requests_are_bounded(self, post):
        post.return_value.status_code = 200
        post.return_value.iter_content.side_effect = lambda chunk_size: [
            json.dumps(post.return_value.json()).encode(),
        ]
        post.return_value.json.return_value = {'Results': [{'Make': 'Example', 'Model': 'Car'}]}
        self.service.decode(VIN, 2020)
        key1 = self.repository.get_cached_vin.call_args.args[0]
        self.service.decode(VIN, 2021)
        self.assertNotEqual(key1, self.repository.get_cached_vin.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['timeout'], (3.05, 15))

    @patch('automotive.vin.requests.post')
    def test_cached_response_and_malformed_provider(self, post):
        self.repository.get_cached_vin.return_value = {'vehicle': {}, 'warnings': []}
        self.assertTrue(self.service.decode(VIN)['cached'])
        post.assert_not_called()
        self.repository.get_cached_vin.return_value = None
        post.return_value.status_code = 200
        post.return_value.iter_content.side_effect = lambda chunk_size: [
            json.dumps(post.return_value.json()).encode(),
        ]
        post.return_value.json.return_value = {'Results': []}
        with self.assertRaises(ProviderUnavailable):
            self.service.decode(VIN)

    @patch('automotive.vin.requests.post')
    def test_stream_size_and_redirects_are_rejected_and_closed(self, post):
        post.return_value.status_code = 302
        with self.assertRaises(ProviderUnavailable):
            self.service.decode(VIN)
        post.return_value.iter_content.assert_not_called()
        post.return_value.close.assert_called_once()
        post.return_value.status_code = 200
        post.return_value.iter_content.return_value = [b'x' * 600_000, b'x' * 600_000]
        with self.assertRaises(ProviderUnavailable):
            self.service.decode(VIN)
        self.repository.cache_vin.assert_not_called()
        self.assertEqual(post.return_value.close.call_count, 2)


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_specifications.return_value = [
            evidence(category='engine_oil', approval='SPEC-A'),
        ]
        self.repository.inventory_products.return_value = [{
            'source_product_id': 'p1', 'source_fingerprint': 'a' * 64,
            'product_code': 'OIL-A', 'brand': 'Example',
        }]
        self.repository.product_approvals.return_value = [
            evidence(product_id='p1', approval='SPEC-A'),
        ]
        self.service = RecommendationService(self.repository)

    def test_spec_and_actual_product_approval_required(self):
        result = self.service.recommend_oil(VEHICLE)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertIsNone(result['capacity_liters'])
        self.assertEqual(result['filter_references'], [])
        self.repository.product_approvals.return_value = [
            evidence(product_id='p2', approval='SPEC-A'),
        ]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_mismatch_or_provisional_or_unknown_engine_returns_insufficient_data(self):
        for approval in (evidence(product_id='p1', approval='SPEC-B'),
                         evidence(product_id='p1', approval='SPEC-A', review_status='pending')):
            self.repository.product_approvals.return_value = [approval]
            self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')
        incomplete = {**VEHICLE, 'engine_code': None}
        self.assertEqual(self.service.recommend_oil(incomplete)['status'], 'INSUFFICIENT_DATA')

    def test_conflicting_requirements_block_all_recommendations(self):
        self.repository.vehicle_specifications.return_value.append(
            evidence(category='engine_oil', approval='SPEC-B', review_status='pending'),
        )
        result = self.service.recommend_oil(VEHICLE)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'], [])

    def test_contrary_product_approval_blocks_recommendation(self):
        self.repository.product_approvals.return_value.append(
            evidence(product_id='p1', approval='SPEC-A', approved=False),
        )
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_generic_spec_cannot_verify_configuration(self):
        self.repository.vehicle_specifications.return_value = [
            evidence(category='engine_oil', approval='SPEC-A', applicability={}),
        ]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_stale_approval_does_not_apply_after_catalog_identity_changes(self):
        self.repository.inventory_products.return_value = [{
            'source_product_id': 'p1', 'source_fingerprint': 'b' * 64,
            'product_code': 'DIFFERENT-OIL', 'brand': 'Other manufacturer',
        }]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_missing_approval_or_inventory_fingerprint_cannot_verify(self):
        self.repository.product_approvals.return_value = [
            evidence(product_id='p1', approval='SPEC-A', source_fingerprint=None),
        ]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')
        self.repository.inventory_products.return_value[0].pop('source_fingerprint')
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_required_viscosity_must_match_verified_product_evidence(self):
        self.repository.vehicle_specifications.return_value[0]['viscosity'] = '5W30'
        self.repository.product_approvals.return_value[0]['viscosity'] = '10W40'
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')
        self.repository.product_approvals.return_value[0]['viscosity'] = 'SAE 5W-30'
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'VERIFIED')

    def test_required_viscosity_cannot_come_from_unverified_product_metadata(self):
        self.repository.vehicle_specifications.return_value[0]['viscosity'] = '5W30'
        self.repository.inventory_products.return_value[0]['viscosity'] = '5W30'
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_explicit_viscosity_alternatives_supported_and_ambiguous_product_blocked(self):
        self.repository.vehicle_specifications.return_value[0]['viscosities'] = ['5W30', '0W30']
        self.repository.product_approvals.return_value[0]['viscosity'] = '0W-30'
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'VERIFIED')
        self.repository.product_approvals.return_value[0]['viscosity'] = ['5W30', '10W40']
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_unsupported_viscosity_prose_returns_insufficient_data(self):
        self.repository.vehicle_specifications.return_value[0]['viscosity'] = '5W30 or 0W30'
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_conflicting_viscosities_block_recommendation_with_same_approval(self):
        self.repository.vehicle_specifications.return_value[0]['viscosity'] = '5W30'
        self.repository.vehicle_specifications.return_value.append(
            evidence(category='engine_oil', approval='SPEC-A', viscosity='10W40'),
        )
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'CONFLICTED')

    def test_blank_or_mixed_required_approval_values_never_reduce_constraints(self):
        for malformed in (['   '], ['SPEC-A', ''], ['SPEC-A', 42], [], None, 'SPEC-A'):
            with self.subTest(malformed=malformed):
                self.repository.vehicle_specifications.return_value = [
                    evidence(category='engine_oil', required_approvals=malformed),
                ]
                result = self.service.recommend_oil(VEHICLE)
                self.assertEqual(result['status'], 'INSUFFICIENT_DATA')
                self.assertTrue(any('malformed' in warning for warning in result['warnings']))
        self.repository.vehicle_specifications.return_value = [
            evidence(category='engine_oil', required_approvals=['   ']),
        ]
        self.repository.product_approvals.return_value = [evidence(product_id='p1')]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')

    def test_product_approval_requires_nonempty_string_and_exact_positive_boolean(self):
        for flag in ('false', 'true', 0, 1, None, False):
            with self.subTest(flag=flag):
                self.repository.product_approvals.return_value = [
                    evidence(product_id='p1', approval='SPEC-A', approved=flag),
                ]
                self.assertEqual(self.service.recommend_oil(VEHICLE)['status'],
                                 'INSUFFICIENT_DATA')
        for malformed in ('', '   ', True, None):
            self.repository.product_approvals.return_value = [
                evidence(product_id='p1', approval=malformed),
            ]
            self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')
        self.repository.product_approvals.return_value = [
            evidence(product_id='p1', approval='SPEC-A', approved=True),
        ]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'VERIFIED')

    def test_viscosity_assertions_cannot_hide_disagreement_by_intersection(self):
        self.repository.vehicle_specifications.return_value[0]['viscosity'] = '5W30'
        self.repository.product_approvals.return_value = [
            evidence(product_id='p1', approval='SPEC-A', viscosity=['5W30', '10W40']),
            evidence(product_id='p1', approval='SPEC-A', viscosity='5W30'),
        ]
        self.assertEqual(self.service.recommend_oil(VEHICLE)['status'], 'INSUFFICIENT_DATA')
