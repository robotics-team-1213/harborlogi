import unittest
from dataclasses import replace
from unittest.mock import patch

from tools.rehandling_baseline import fixture, run_sequence, compare


class RehandlingBaselineTests(unittest.TestCase):
    def test_sequence_counts_each_export_and_preserves_input(self):
        source, policy = fixture(False)
        original = source.placements
        result = run_sequence(source, policy, ('target', 'a', 'b'), {'a': (0, None), 'b': (1, None)})
        self.assertTrue(result['completed'])
        self.assertEqual([s['cargo_id'] for s in result['steps']], ['target', 'a', 'b'])
        self.assertEqual(result['outbound_moves'], 3)
        self.assertEqual(result['rehandle_moves'], 4)
        self.assertEqual(result['total_moves'], 7)
        self.assertIsNone(result['physical_work_seconds'])
        self.assertEqual(source.placements, original)

    def test_hold_is_reported_without_counting_partial_work(self):
        source, policy = fixture(False)
        result = run_sequence(source, replace(policy, arm_reach=frozenset()), ('target', 'a', 'b'), {})
        self.assertFalse(result['completed'])
        self.assertEqual(len(result['steps']), 1)
        self.assertEqual(result['steps'][0]['status'], 'HOLD')
        self.assertEqual(result['total_moves'], 0)

    def test_pairs_share_inputs_and_counterbalance_order(self):
        with patch('tools.rehandling_baseline.run_sequence', wraps=run_sequence) as run:
            rows = compare(repeats=2)
        self.assertEqual(len(rows), 16)
        for offset in range(0, len(run.call_args_list), 4):
            calls = run.call_args_list[offset:offset+4]
            self.assertEqual([bool(c.args[3]) for c in calls], [False, True, True, False])
            self.assertTrue(all(c.args[:3] == calls[0].args[:3] for c in calls))
        self.assertTrue(all(r['completed'] for r in rows))
