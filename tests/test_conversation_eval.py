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


if __name__ == '__main__':
    unittest.main()
