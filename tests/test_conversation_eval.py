"""The scripted conversation suite is a regression gate for accuracy and natural feel."""

from __future__ import annotations

import io
import unittest

from automotive import conversation_eval


class ConversationSuiteTests(unittest.TestCase):
    def test_every_mock_scenario_passes_its_hard_checks(self):
        store = conversation_eval.mock_store()
        self.addCleanup(store.close)
        scenarios = conversation_eval.load_scenarios()
        self.assertGreaterEqual(len(scenarios), 40)
        results = conversation_eval.evaluate(store, scenarios, conversation_eval.MOCK_VINS,
                                             io.StringIO(), timeout=10)
        summary = conversation_eval.summarize(results)
        failures = [(s['id'], index, name) for s in results
                    for index, turn in enumerate(s['turns'], 1)
                    for name, ok in turn['hard'].items() if not ok]
        self.assertEqual(failures, [])
        self.assertGreaterEqual(summary['naturalness'], 0.95)
        transcripts = str(results)
        for vin in conversation_eval.MOCK_VINS.values():
            self.assertNotIn(vin, transcripts)

    def test_scenario_ids_are_unique_and_turns_are_well_formed(self):
        for scenario in conversation_eval.load_scenarios():
            self.assertTrue(scenario['turns'], scenario['id'])
            for turn in scenario['turns']:
                self.assertIn('say', turn)
                if '{VIN' in turn['say']:
                    self.assertIn(turn.get('vin'), conversation_eval.MOCK_VINS, scenario['id'])

    def test_metrics_distinguish_completion_from_redundant_clarification(self):
        payload = {'status': 'COMPLETE', 'vehicle': {'make': 'Mercedes-Benz',
                    'model': 'E-Class', 'model_year': 2015}, 'cards': [],
                   'pending': {'field': 'model_year'},
                   'usage': {'model_requests': 2, 'tool_executions': 3, 'web_searches': 1},
                   'diagnostics': {'stage_timings': [{'stage': 'resolve_vehicle'},
                                                    {'stage': 'discover_parts'}]}}
        metrics = conversation_eval.turn_metrics({'kind': 'oil'}, payload,
                                                  {'model_calls': 2}, final_ms=320)
        self.assertTrue(metrics['redundant_clarification'])
        self.assertFalse(metrics['task_completion'])
        self.assertEqual(metrics['tools'], ['resolve_vehicle', 'discover_parts'])
        self.assertEqual(metrics['requests']['web_searches'], 1)
        self.assertTrue(metrics['within_budget'])

    def test_metrics_reject_claims_from_a_different_vehicle(self):
        payload = {'status': 'COMPLETE', 'vehicle': {'make': 'Mercedes-Benz'},
                   'vehicle_revision': 3, 'claims': [
                       {'claim_id': 'foreign', 'status': 'SOURCE_ASSERTION',
                        'applicability': {'make': 'BMW'}, 'vehicle_revision': 3,
                        'sources': ['Manufacturer']}], 'cards': []}
        metrics = conversation_eval.turn_metrics({'kind': 'oil'}, payload, {}, final_ms=20)
        self.assertEqual(metrics['grounded_claims'], 0)
        self.assertEqual(metrics['claim_count'], 1)

    def test_ai_scenarios_cover_typo_transliteration_scattered_details_and_fuel_followup(self):
        store = conversation_eval.mock_store(orchestration=True)
        self.addCleanup(store.close)
        scenarios = conversation_eval.load_ai_scenarios()
        texts = ' '.join(turn['say'] for scenario in scenarios for turn in scenario['turns'])
        self.assertIn('მერსედესსზე', texts)
        self.assertIn('mersedesze', texts)
        self.assertTrue(any(len(scenario['turns']) >= 3 for scenario in scenarios))
        results = conversation_eval.evaluate(store, scenarios, conversation_eval.MOCK_VINS,
                                             io.StringIO(), timeout=10)
        failures = [(s['id'], index, name) for s in results
                    for index, turn in enumerate(s['turns'], 1)
                    for name, ok in turn['hard'].items() if not ok]
        self.assertEqual(failures, [])
        summary = conversation_eval.summarize(results)
        self.assertEqual(summary['orchestration']['redundant_clarifications'], 0)
        self.assertEqual(summary['orchestration']['budget_violations'], 0)
        self.assertGreater(summary['orchestration']['requests']['model_requests'], 0)
        self.assertIn('resolve_vehicle', summary['orchestration']['tool_selections'])


if __name__ == '__main__':
    unittest.main()
