#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from analyze_response_retire_witness import analyze


def uop(seq, issue, complete, retire, dep=0, response=None):
    return {'sequence': seq, 'actual_issue_cycle': issue,
            'actual_completion_cycle': complete, 'actual_retire_cycle': retire,
            'producer_dists': [dep, 0, 0, 0, 0], 'memory_events': [] if response is None else
            [{'instruction_fetch': 0, 'write': 0, 'blocks_retirement': 1,
              'response_cycle': response}]}


class ResponseRetireWitnessTest(unittest.TestCase):
    def test_dependent_chain_exposes_delay(self):
        r = analyze([uop(0, 1, 2, 2, response=5), uop(1, 2, 4, 4, dep=1)], 8, 0)
        self.assertTrue(r['zero_change_identity'])
        self.assertEqual(r['conditional_endpoint_retire_displacement'], 3)

    def test_older_long_request_masks_younger_delay(self):
        r = analyze([uop(0, 1, 20, 20), uop(1, 1, 2, 20, response=5)], 8, 0)
        self.assertEqual(r['local_completion_gap_sum_not_retire_cycles'], 3)
        self.assertEqual(r['conditional_endpoint_retire_displacement'], 0)

    def test_parallel_gaps_do_not_add_as_retire(self):
        r = analyze([uop(0, 1, 2, 2, response=5), uop(1, 1, 2, 2, response=5)], 8, 0)
        self.assertEqual(r['local_completion_gap_sum_not_retire_cycles'], 6)
        self.assertEqual(r['conditional_endpoint_retire_displacement'], 3)

    def test_rejects_invalid_zero_change_and_sparse_windows(self):
        with self.assertRaisesRegex(ValueError, 'zero-change'):
            analyze([uop(0, 1, 5, 5), uop(1, 2, 3, 5, dep=1)], 8, 0)
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            analyze([uop(0, 1, 2, 2), uop(2, 1, 2, 2)], 8, 0)


if __name__ == '__main__':
    unittest.main()
