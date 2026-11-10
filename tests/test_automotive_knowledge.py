"""Learned knowledge: exact-profile reuse, fail-closed validation and role isolation."""

from __future__ import annotations

import copy
import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import anthropic
import httpx
import psycopg

from automotive.config import Settings
from automotive.knowledge import KnowledgeService, knowledge_for
from automotive.knowledge_judge import InvalidJudgement, check_verdicts
from automotive.knowledge_search import LearningSearchCoordinator
from automotive.knowledge_types import (
    claim,
    digest,
    frozen_context,
    normalize_vehicle,
    request_vehicle,
)
from automotive.knowledge_worker import run_once
from automotive.vin_search import TERMINAL

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv('WAREHOUSE_TEST_ADMIN_DATABASE_URL')
VIN = '1HGCM82633A004352'  # Public test fixture, not customer data.
VEHICLE = {'make': 'Mercedes-Benz', 'model': 'E-Class', 'model_year': 2015, 'body': 'Coupe',
           'engine_code': 'M276.850'}
POLICIES = {'shell': {'source': 'shell', 'reuse_permission': 'authorized', 'dataset': 'oats'}}
MANUAL = 'https://www.mercedes-benz.com/owners/e-class-coupe-2015/maintenance'
PASSAGE = ('E-Class Coupe 2015 with engine M276.850: use engine oil SAE 5W-30. '
           'Engine oil capacity 6.5 litres including filter.')


def viscosity(grade='5W-30'):
    return claim('oil_viscosity', 'engine_oil', 'engine:viscosity', {'grade': grade}, 'shell',
                 'https://www.shell.com/motorist/oil-selector')


def capacity(amount=6.5):
    return claim('capacity', 'engine_oil', 'engine:capacity', {'amount': amount, 'unit': 'L'},
                 'shell', 'https://www.shell.com/motorist/oil-selector')


def payload(*claims, vehicle=VEHICLE, request='oil change'):
    return {'context': frozen_context(vehicle, request, 'oil_change', {'make': 'user'}),
            'claims': list(claims)}


def receipt(**updates):
    return {'receipt_id': 'srvtoolu_manual', 'type': 'fetch', 'url': MANUAL,
            'retrieved_at': '2026-11-09T10:00:00Z', 'text': PASSAGE, **updates}


def verdict(candidate, context, decision='accepted', quote='use engine oil SAE 5W-30',
            **updates):
    return {'claim_id': candidate['claim_id'], 'verdict': decision,
            'applicability': copy.deepcopy(context['vehicle']),
            'unresolved_fields': list(context['unresolved_fields']), 'scope_supported': True,
            'citations': [{'receipt_id': 'srvtoolu_manual', 'quote': quote}],
            'reason': 'Manufacturer maintenance page for this exact engine.', **updates}


def fact(candidate, fact_id='fact-1'):
    return {'fact_id': fact_id, 'category': candidate['category'], 'kind': candidate['kind'],
            'slot': candidate['slot'], 'value': candidate['value'],
            'validated_at': '2026-11-09T10:01:00+00:00', 'valid_until': '2027-05-08T10:01:00+00:00',
            'provenance': {'candidate': candidate, 'validation': {'citations': [
                {'receipt_id': 'srvtoolu_manual', 'quote': 'SAE 5W-30'}]},
                'receipts': [{'receipt_id': 'srvtoolu_manual', 'url': MANUAL,
                              'retrieved_at': '2026-11-09T10:00:00Z'}]}}


class VerdictCheckTests(unittest.TestCase):
    def test_mixed_verdicts_keep_each_claim_separate(self):
        oil, amount = viscosity(), capacity()
        job = payload(oil, amount)
        result = check_verdicts(job, {'verdicts': [
            verdict(oil, job['context']),
            verdict(amount, job['context'], 'rejected', citations=[]),
        ]}, [receipt()], POLICIES)
        self.assertEqual({v['claim_id']: v['verdict'] for v in result},
                         {oil['claim_id']: 'accepted', amount['claim_id']: 'rejected'})

    def test_fabricated_snippet_same_dataset_and_unquoted_citations_fail_closed(self):
        oil = viscosity()
        job = payload(oil)
        for name, receipts, quote in (
            ('unknown receipt', [receipt(receipt_id='other')], 'use engine oil SAE 5W-30'),
            ('search snippet', [receipt(type='search')], 'use engine oil SAE 5W-30'),
            ('same dataset', [receipt(url='https://www.shell.com/oats/e-class')],
             'use engine oil SAE 5W-30'),
            ('invented quote', [receipt()], 'Mercedes approves SAE 5W-30 everywhere'),
            ('wrong value', [receipt(text=PASSAGE.replace('5W-30', '0W-40'))],
             'use engine oil SAE 0W-40'),
        ):
            with self.subTest(name), self.assertRaises(InvalidJudgement):
                check_verdicts(job, {'verdicts': [verdict(oil, job['context'], quote=quote)]},
                               receipts, POLICIES)

    def test_broader_scope_and_incomplete_verdict_sets_are_rejected(self):
        oil, amount = viscosity(), capacity()
        job = payload(oil, amount)
        broader = {k: v for k, v in job['context']['vehicle'].items() if k != 'engine_code'}
        rejected = verdict(amount, job['context'], 'rejected', citations=[])
        cases = {
            'engine omitted': [verdict(oil, job['context'], applicability=broader), rejected],
            'unknowns resolved': [verdict(oil, job['context'], unresolved_fields=[]), rejected],
            'scope unsupported': [verdict(oil, job['context'], scope_supported=False), rejected],
            'claim missing': [verdict(oil, job['context'])],
            'claim duplicated': [verdict(oil, job['context'])] * 2,
        }
        for name, verdicts in cases.items():
            with self.subTest(name), self.assertRaises(InvalidJudgement):
                check_verdicts(job, {'verdicts': verdicts}, [receipt()], POLICIES)
        with self.assertRaises(ValueError):
            check_verdicts(job, {'verdicts': 'accepted'}, [receipt()], POLICIES)

    def test_capacity_needs_amount_and_units_in_the_cited_passage(self):
        amount = capacity()
        job = payload(amount)
        accepted = check_verdicts(job, {'verdicts': [verdict(
            amount, job['context'], quote='Engine oil capacity 6.5 litres')]}, [receipt()],
            POLICIES)
        self.assertEqual(accepted[0]['verdict'], 'accepted')
        with self.assertRaises(InvalidJudgement):
            check_verdicts(job, {'verdicts': [verdict(
                amount, job['context'], quote='use engine oil SAE 5W-30')]}, [receipt()],
                POLICIES)


class ProfileTests(unittest.TestCase):
    def key(self, vehicle, request=''):
        return digest(normalize_vehicle(request_vehicle(vehicle, request)))

    def test_only_the_exact_configuration_shares_a_profile(self):
        base = self.key(VEHICLE)
        self.assertEqual(base, self.key({**VEHICLE, 'make': ' mercedes-benz ',
                                         'model': 'E-CLASS'}))
        for change in ({'engine_code': 'M274.920'}, {'model_year': 2016}, {'body': 'Saloon'},
                       {'market': 'EU'}, {'engine_code': None}):
            with self.subTest(change=change):
                self.assertNotEqual(base, self.key({**VEHICLE, **change}))
        self.assertNotEqual(base, self.key(VEHICLE, 'E400 coupe 3.0 twin turbo'))

    def test_vin_never_enters_frozen_research_context(self):
        context = frozen_context(VEHICLE, f'{VIN} oil change', 'oil_change', {})
        self.assertNotIn(VIN, str(context))
        self.assertIn('engine_code', context['vehicle'])
        self.assertIn('market', context['unresolved_fields'])


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.policies.return_value = POLICIES
        self.repository.lookup.return_value = [fact(viscosity())]
        self.service = KnowledgeService(self.repository)

    def test_stored_grade_completes_oil_and_stays_provisional(self):
        hit = self.service.reuse(VEHICLE, 'engine oil', 'which oil?')
        self.assertEqual(hit['complete_categories'], ['engine_oil'])
        report = hit['fluids'][0]
        self.assertEqual(report['status'], 'PROVISIONAL')
        self.assertEqual(report['validation_status'], 'MACHINE_ACCEPTED')
        self.assertEqual(report['items'][0]['viscosity'], '5W-30')
        self.assertIsNone(report['items'][0]['capacity'])

    def test_unanswered_parts_of_the_question_and_withdrawn_policies_are_misses(self):
        hit = self.service.reuse(VEHICLE, 'engine oil', 'oil capacity in litres?')
        self.assertEqual(hit['complete_categories'], [])
        self.repository.policies.return_value = {}
        self.assertEqual(self.service.reuse(VEHICLE, 'engine oil')['facts'], [])

    def test_incomplete_vehicle_or_unavailable_database_preserves_live_discovery(self):
        self.service.reuse({'make': 'Mercedes-Benz', 'model': 'E-Class'}, 'engine oil')
        self.repository.lookup.assert_not_called()
        self.repository.policies.side_effect = psycopg.OperationalError('password=private')
        self.assertEqual(self.service.reuse(VEHICLE, 'engine oil')['complete_categories'], [])

    def test_collection_splits_claims_and_skips_unauthorized_stored_or_conflicting_reports(self):
        report = {'provider': 'Shell', 'status': 'PROVISIONAL', 'items': [{
            'viscosity': 'SAE 5W-30', 'product_name': 'Shell Helix Ultra 5W-30',
            'capacity': '6.5 L', 'approvals': ['MB 229.5'], 'provider_product_id': 'h1'}]}
        self.service.collect(VEHICLE, 'oil change', [
            ('shell', report),
            ('shell', {**report, 'provenance': 'stored'}),
            ('shell', {**report, 'status': 'CONFLICTED'}),
            ('pakelo', report),
        ], request=f'{VIN} ზეთის შეცვლა', origins={'make': 'user'})
        context, claims = self.repository.enqueue.call_args.args
        self.assertEqual(sorted(c['kind'] for c in claims), [
            'capacity', 'manufacturer_approval', 'oil_viscosity', 'product_recommendation'])
        self.assertEqual({c['source'] for c in claims}, {'shell'})
        self.assertEqual(next(c for c in claims if c['kind'] == 'capacity')['value'],
                         {'amount': 6.5, 'unit': 'L'})
        self.assertNotIn(VIN, str(context))
        self.repository.enqueue.reset_mock()
        self.service.collect({'make': 'Mercedes-Benz'}, 'oil change', [('shell', report)])
        self.repository.enqueue.assert_not_called()

    def test_only_a_real_knowledge_store_enables_learning_arguments(self):
        self.assertIsNone(knowledge_for(Mock()))
        self.assertIs(knowledge_for(Mock(knowledge=self.service)), self.service)


class LearningSearchTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.vehicle_fitment_records.return_value = []
        self.repository.vehicle_catalog_candidates.return_value = {'items': [], 'has_more': False}
        self.repository.search_parts.return_value = {'items': [], 'warnings': []}
        self.repository.cross_reference_records.return_value = []
        self.vin = Mock()
        self.vin.decode.return_value = {'vehicle': VEHICLE, 'warnings': []}
        self.shell = Mock()
        self.shell.lookup.return_value = {'provider': 'Shell', 'status': 'PROVISIONAL',
                                          'items': [{'viscosity': '5W-30',
                                                     'product_name': 'Helix'}]}
        self.knowledge = KnowledgeService(Mock())
        self.knowledge.reuse = Mock()
        self.knowledge.collect = Mock()
        self.coordinator = LearningSearchCoordinator(
            self.repository, self.vin, Mock(), 'k' * 32, knowledge=self.knowledge,
            shell_service=self.shell, motorium_enabled=False, fuchs_enabled=False,
            pakelo_enabled=False, jsfilter_enabled=False)
        self.addCleanup(self.coordinator.close)

    def finish(self):
        search = self.coordinator.start(VIN, 'engine oil', request='which oil?')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.coordinator.get(search['search_id'])
            if result['status'] in TERMINAL:
                return result
            time.sleep(0.005)
        self.fail('Search did not finish')

    def test_complete_hit_skips_suppliers_but_refreshes_the_warehouse(self):
        self.knowledge.reuse.return_value = {
            'facts': [fact(viscosity())], 'complete_categories': ['engine_oil'],
            'catalog_items': [],
            'fluids': [{'provider': 'Shell', 'status': 'PROVISIONAL', 'provenance': 'stored',
                        'items': [{'kind': 'oil', 'viscosity': '5W-30'}]}]}
        result = self.finish()
        self.shell.lookup.assert_not_called()
        self.repository.vehicle_catalog_candidates.assert_called()
        self.knowledge.collect.assert_not_called()
        self.assertEqual(result['provenance'], 'stored')
        self.assertEqual(result['knowledge'][0]['validation_status'], 'MACHINE_ACCEPTED')
        self.assertNotIn('text', result['knowledge'][0]['receipts'][0])

    def test_miss_runs_live_sources_and_queues_their_findings(self):
        self.knowledge.reuse.return_value = {'facts': [], 'complete_categories': [],
                                             'fluids': [], 'catalog_items': []}
        result = self.finish()
        self.shell.lookup.assert_called_once()
        self.assertEqual(result['provenance'], 'live')
        deadline = time.monotonic() + 2
        while not self.knowledge.collect.called and time.monotonic() < deadline:
            time.sleep(0.005)
        reports = self.knowledge.collect.call_args.args[2]
        self.assertIn('shell', [source for source, _ in reports])
        self.assertEqual(self.knowledge.collect.call_args.kwargs['request'], 'which oil?')

    def test_stated_values_are_not_rewritten_by_profile_normalization(self):
        self.knowledge.reuse.return_value = {'facts': [], 'complete_categories': [],
                                             'fluids': [], 'catalog_items': []}
        snapshot = self.coordinator.start(None, 'engine oil', 2015, dict(VEHICLE),
                                          request='E coupe 3.0 twin turbo')
        vehicle = self.coordinator._jobs[snapshot['search_id']]['vehicle']
        self.assertEqual(vehicle['make'], 'Mercedes-Benz')
        self.assertEqual(vehicle['displacement_l'], 3.0)
        self.assertEqual(vehicle['aspiration'], 'twin turbo')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.claim.return_value = {'job_id': 'j1', 'lease_token': 't1',
                                              'payload': payload(viscosity())}
        self.judge = Mock(receipts=[])
        self.settings = Settings(knowledge_enabled=True)

    def test_no_job_or_success_publishes_through_the_lease(self):
        self.judge.run.return_value = {'verdicts': []}
        self.assertTrue(run_once(self.repository, self.judge, self.settings))
        self.repository.finish.assert_called_once()
        self.repository.claim.return_value = None
        self.assertFalse(run_once(self.repository, self.judge, self.settings))

    def test_technical_failures_retry_and_invalid_judgements_do_not(self):
        request = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
        for error, retry in ((anthropic.APITimeoutError(request=request), True),
                             (TimeoutError('deadline'), True),
                             (InvalidJudgement('fabricated'), False),
                             (ValueError('malformed json'), False)):
            with self.subTest(error=type(error).__name__):
                self.repository.reset_mock()
                self.judge.run.side_effect = error
                run_once(self.repository, self.judge, self.settings)
                self.repository.finish.assert_not_called()
                self.assertEqual(self.repository.fail.call_args.kwargs['retry'], retry)


class SettingsTests(unittest.TestCase):
    def test_validator_credentials_and_research_caps_are_enforced(self):
        base = {'AUTOMOTIVE_DATABASE_URL': 'postgresql://runtime@127.0.0.1/db'}
        for updates in ({'AUTOMOTIVE_VALIDATOR_DATABASE_URL': base['AUTOMOTIVE_DATABASE_URL']},
                        {'AUTOMOTIVE_KNOWLEDGE_DAILY_ATTEMPTS': '26'},
                        {'AUTOMOTIVE_KNOWLEDGE_MAX_SEARCHES': '0'}):
            with self.subTest(updates=updates), patch.dict(os.environ, {**base, **updates}):
                with self.assertRaises(ValueError):
                    Settings.from_env()


@unittest.skipUnless(ADMIN_URL, 'requires disposable PostgreSQL admin URL')
class KnowledgePostgresTests(unittest.TestCase):
    """Temporary database; checks grants, RLS, leases and publication end to end."""

    @classmethod
    def setUpClass(cls):
        import uuid

        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from automotive.knowledge_repository import KnowledgeRepository
        from warehouse_sync.migrations import apply_migrations_on_connection

        cls.repository_class = KnowledgeRepository
        cls.dbname = 'auto_knowledge_' + uuid.uuid4().hex
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.dbname)))
        info = conninfo_to_dict(ADMIN_URL)
        info['dbname'] = cls.dbname
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as conn:
            conn.execute((ROOT / 'ops/sql/provision_automotive_roles.sql').read_text())
            conn.execute('GRANT automotive_runtime, automotive_enrichment, automotive_validator '
                         'TO CURRENT_USER')
            apply_migrations_on_connection(conn)
            conn.execute(
                "INSERT INTO automotive_knowledge.source_policies"
                "(source,reuse_permission,dataset,configured_by) VALUES "
                "('shell','authorized','oats','test-operator'),"
                "('motorium','unknown','motorium','test-operator')")

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql

        cls.admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(
            sql.Identifier(cls.dbname)))
        cls.admin.close()

    def setUp(self):
        with psycopg.connect(self.url) as conn:
            conn.execute('TRUNCATE automotive_knowledge.accepted_facts, '
                         'automotive_knowledge.validation_audits, '
                         'automotive_knowledge.candidate_jobs, '
                         'automotive_knowledge.vehicle_profiles, '
                         'automotive_knowledge.daily_quota')

    def role(self, name):
        repository = self.repository_class(self.url)
        original = repository._connect

        def connect():
            conn = original()
            conn.execute(psycopg.sql.SQL('SET ROLE {}').format(psycopg.sql.Identifier(name)))
            return conn
        repository._connect = connect
        return repository

    def publish(self, oil_decision, capacity_decision, vehicle=VEHICLE):
        oil, amount = viscosity(), capacity()
        job = payload(oil, amount, vehicle=vehicle)
        self.assertTrue(self.role('automotive_runtime').enqueue(job['context'], job['claims']))
        validator = self.role('automotive_validator')
        claimed = validator.claim('claude-sonnet-5-5')
        verdicts = [verdict(c, job['context'], decision,
                            **({} if decision == 'accepted' else {'citations': []}))
                    for c, decision in ((oil, oil_decision), (amount, capacity_decision))]
        return validator, claimed, {'model': 'claude-sonnet-5-5', 'receipts': [receipt()],
                                    'verdicts': verdicts}

    def test_runtime_and_enrichment_roles_cannot_bypass_validation(self):
        runtime = self.role('automotive_runtime')
        job = payload(viscosity(), claim('part_number', 'oil_filter', 'Maker:AB-1',
                                         {'code': 'AB-1', 'manufacturer': 'Maker'}, 'motorium',
                                         'https://motorium.ge/'))
        self.assertTrue(runtime.enqueue(job['context'], job['claims']))
        with psycopg.connect(self.url) as conn:
            sources = conn.execute(
                "SELECT jsonb_path_query_array(payload,'$.claims[*].source') "
                "FROM automotive_knowledge.candidate_jobs").fetchone()[0]
        self.assertEqual(sources, ['shell'])  # Unknown reuse permission is never queued.
        for statement in (
            'SELECT * FROM automotive_knowledge.candidate_jobs',
            "INSERT INTO automotive_knowledge.accepted_facts(fact_id) VALUES ('forged')",
            "UPDATE automotive_knowledge.source_policies SET reuse_permission='owned'",
            "SELECT * FROM automotive_knowledge.claim_job('t','m','p')",
        ):
            with self.subTest(statement=statement), runtime._connect() as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        enrichment = self.role('automotive_enrichment')
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            enrichment.policies()
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            enrichment.enqueue(job['context'], job['claims'])
        validator = self.role('automotive_validator')
        with validator._connect() as conn, self.assertRaises(
                psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE automotive_knowledge.source_policies SET dataset='x'")

    def test_mixed_verdicts_publish_once_and_reuse_survives_restart(self):
        validator, claimed, result = self.publish('accepted', 'rejected')
        self.assertTrue(validator.finish(claimed, result))
        self.assertFalse(validator.finish(claimed, result))
        restarted = KnowledgeService(self.role('automotive_runtime'))
        facts = restarted.repository.lookup(VEHICLE, ['engine_oil'])
        self.assertEqual([f['kind'] for f in facts], ['oil_viscosity'])
        self.assertNotIn('text', facts[0]['provenance']['receipts'][0])
        self.assertEqual(restarted.reuse(VEHICLE, 'engine oil')['complete_categories'],
                         ['engine_oil'])
        self.assertEqual(restarted.repository.lookup({**VEHICLE, 'engine_code': 'M274.920'},
                                                     ['engine_oil']), [])

    def test_daily_cap_concurrency_and_expired_leases_prevent_duplicate_publication(self):
        validator, first, result = self.publish('accepted', 'accepted')
        other = payload(viscosity(), vehicle={**VEHICLE, 'model_year': 2016})
        self.role('automotive_runtime').enqueue(other['context'], other['claims'])
        self.assertIsNone(validator.claim('claude-sonnet-5-5'))  # One attempt at a time.
        with psycopg.connect(self.url) as conn:
            conn.execute("UPDATE automotive_knowledge.candidate_jobs "
                         "SET lease_until=now()-interval '1 second' WHERE job_id=%s",
                         (first['job_id'],))
        retried = validator.claim('claude-sonnet-5-5')
        self.assertEqual((retried['job_id'], retried['attempts']), (first['job_id'], 2))
        self.assertFalse(validator.finish(first, result))  # The crashed worker's lease.
        self.assertTrue(validator.finish(retried, result))
        used = validator.status()['attempts_today']
        self.assertEqual(used, 2)
        self.assertIsNone(validator.claim('claude-sonnet-5-5', cap=used))
        with psycopg.connect(self.url) as conn:
            count = conn.execute('SELECT count(*) FROM automotive_knowledge.accepted_facts'
                                 ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_contradiction_suspends_and_provisioning_rerun_keeps_roles_scoped(self):
        validator, claimed, result = self.publish('accepted', 'insufficient_evidence')
        self.assertTrue(validator.finish(claimed, result))
        runtime = self.role('automotive_runtime')
        contrary = payload(viscosity('0W-40'))
        runtime.enqueue(contrary['context'], contrary['claims'])
        self.assertEqual(runtime.lookup(VEHICLE, ['engine_oil']), [])
        with psycopg.connect(self.url) as conn:
            conn.execute((ROOT / 'ops/sql/provision_automotive_roles.sql').read_text())
            state = conn.execute('SELECT state FROM automotive_knowledge.accepted_facts'
                                 ).fetchone()[0]
        self.assertEqual(state, 'suspended')
        with runtime._connect() as conn, self.assertRaises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE automotive_knowledge.accepted_facts SET state='active'")
        with self.assertRaises(ValueError):
            validator.revoke('any', ' ', 'operator')


if __name__ == '__main__':
    unittest.main()
