#!/usr/bin/env python3
"""Keep unsupported MMIO separate from ordinary memory timing evidence."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_tail_timing_pairs import (
    compact_native_fact,
    experimental_model_features,
    has_native_response_timestamps,
    memory_event_scope,
)


class MemoryScopeTest(unittest.TestCase):
    def setUp(self):
        self.sample = {'has_load': 0, 'has_store': 0, 'memory_events': []}
        self.record = {'is_load': 1, 'is_store': 0, 'paddr': 8192, 'size': 8}
        self.config = {'allow_mmio_escape': True, 'dram': {'size_bytes': 4096}}

    def test_verified_mmio_explicitly_classified(self):
        self.assertEqual(memory_event_scope(self.sample, self.record, self.config),
                         'mmio-escape-no-data-event')

    def test_missing_in_dram_load_is_not_accepted(self):
        self.record['paddr'] = 64
        with self.assertRaises(ValueError):
            memory_event_scope(self.sample, self.record, self.config)

    def test_disabled_escape_is_not_accepted(self):
        self.config['allow_mmio_escape'] = False
        with self.assertRaises(ValueError):
            memory_event_scope(self.sample, self.record, self.config)

    def test_missing_capacity_is_not_guessed(self):
        del self.config['dram']
        with self.assertRaises(ValueError):
            memory_event_scope(self.sample, self.record, self.config)

    def test_data_event_cannot_be_explained_as_escape(self):
        self.sample['memory_events'] = [{'instruction_fetch': 0}]
        with self.assertRaises(ValueError):
            memory_event_scope(self.sample, self.record, self.config)

    def test_ordinary_load_and_non_memory_keep_existing_semantics(self):
        self.sample['has_load'] = 1
        self.assertEqual(memory_event_scope(self.sample, self.record, self.config),
                         'data-hierarchy')
        self.sample['has_load'] = self.record['is_load'] = 0
        self.assertEqual(memory_event_scope(self.sample, self.record, self.config),
                         'non-data')

    def test_native_fact_keeps_exact_hierarchy_semantics_without_ticks(self):
        fact = {
            'attribution_source': 'packet',
            'proxy_path_class': 4,
            'line_requests': 1,
            'native_admission_count': 1,
            'native_aliased_admissions': 0,
            'native_hierarchy_request_count': 1,
            'native_response_count': 1,
            'native_coalesced': 0,
            'native_terminal_no_ruby': False,
            'native_hierarchy': {
                'l1d': {'accesses': 1, 'hits': 0, 'tag_misses': 1},
                'l2': {'accesses': 1, 'hits': 0, 'tag_misses': 1},
                'llc': {'accesses': 1, 'hits': 0, 'tag_misses': 1},
                'unique_fills': 1,
                'ruby_memory_fetches': 1,
                'memory_read_transactions': 1,
            },
        }
        compact = compact_native_fact(fact)
        self.assertEqual(compact['outcome'], 'ruby_memory_read')
        self.assertEqual(compact['memory_read_transactions'], 1)
        self.assertNotIn('native_first_admission_tick', compact)
        self.assertFalse(compact['response_timestamps_available'])

    def test_native_fact_normalizes_ordered_v7_timing(self):
        fact = {
            'native_response_count': 1,
            'native_admission_count': 1,
            'line_requests': 1,
            'native_first_admission_tick': 1332,
            'native_last_admission_tick': 1665,
            'native_last_response_tick': 3330,
            'native_hierarchy': {},
        }
        compact = compact_native_fact(
            fact, 333, issue_tick=999, commit_tick=3663)
        self.assertTrue(compact['response_timestamps_available'])
        self.assertEqual(compact['timing_cycles'], {
            'first_to_last_admission': 1.0,
            'first_admission_to_last_response': 6.0,
            'issue_to_first_admission': 1.0,
            'last_response_to_commit': 1.0,
        })

    def test_native_fact_rejects_partial_or_reversed_timing(self):
        base = {
            'native_response_count': 1,
            'native_admission_count': 1,
            'native_hierarchy': {},
        }
        with self.assertRaisesRegex(ValueError, 'partial timing'):
            compact_native_fact({
                **base, 'native_first_admission_tick': 10})
        with self.assertRaisesRegex(ValueError, 'not ordered'):
            compact_native_fact({
                **base,
                'native_first_admission_tick': 20,
                'native_last_admission_tick': 10,
                'native_last_response_tick': 30,
            })

    def test_timestamp_summary_accepts_non_memory_pairs(self):
        results = [{'pairs': [
            {'gem5_native': None},
            {'gem5_native': {'response_timestamps_available': True}},
        ]}]
        self.assertTrue(has_native_response_timestamps(results))
        self.assertFalse(has_native_response_timestamps([
            {'pairs': [{'gem5_native': None}]},
        ]))

    def test_experimental_model_features_are_explicit(self):
        self.assertEqual(experimental_model_features({
            'response_pending_fill': False,
            'ruby_sequencer_line_coalescing': True,
            'interval_response_retime': True,
        }), [
            'ruby_sequencer_line_coalescing',
            'interval_response_retime',
        ])


if __name__ == '__main__':
    unittest.main()
