import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_load_retirement import analyze_core, analyze_readiness, load_edges, partition


class LoadRetirementTests(unittest.TestCase):
    def setUp(self):
        self.pair = dict(
            record_ordinal=11, pc=100, is_load=True, is_store=False,
            is_atomic=False, memory_event_scope='data-hierarchy',
            gem5_elapsed_from_roi=20, gem5_issue_to_commit=14,
            gem5_fetch_to_issue=3, fastsim_retire_from_roi=16,
            fastsim_issue_to_retire=10, fastsim_fetch_to_issue=3,
            gem5_native=dict(
                response_timestamps_available=True, attribution_source='packet',
                terminal_no_ruby=False, line_requests=1, admission_count=1,
                response_count=1, hierarchy_request_count=1,
                aliased_admissions=0, coalesced=0,
                native_first_admission_tick=1080,
                native_last_admission_tick=1080, native_last_response_tick=1150))
        self.sample = dict(
            sequence=11, pc=100, actual_retire_cycle=116,
            actual_completion_cycle=114, memory_response_cycle=113,
            memory_events=[dict(instruction_fetch=False, write=False,
                                blocks_retirement=True, corrected_issue_cycle=106,
                                response_cycle=113, line=4)])

    def test_partition_is_disjoint_not_sum_of_request_durations(self):
        edges, reason = load_edges(self.pair, self.sample, 10, 1000)
        self.assertIsNone(reason)
        result = partition(0, self.pair, 'gem5', edges, False)
        self.assertEqual(dict(result), dict(productive=1, not_fetched=2,
            fetched_not_issued=3, load_before_acceptance=2,
            load_acceptance_to_callback=7, load_after_callback=5))
        self.assertEqual(sum(result.values()), 20)
        result = partition(17, self.pair, 'gem5', edges, False)
        self.assertEqual(result['load_acceptance_to_callback'], 0)
        self.assertEqual(result['load_after_callback'], 2)
        self.assertEqual(sum(result.values()), 3)

    def test_split_and_alias_cannot_become_single_request_truth(self):
        for field in ('line_requests', 'admission_count', 'response_count',
                      'hierarchy_request_count', 'aliased_admissions', 'coalesced'):
            pair = copy.deepcopy(self.pair)
            pair['gem5_native'][field] = 2
            edges, reason = load_edges(pair, self.sample, 10, 1000)
            self.assertIsNone(edges)
            self.assertIn('identity-unavailable', reason)

    def test_missing_data_stays_unresolved(self):
        self.pair['gem5_native'] = None
        edges, reason = load_edges(self.pair, self.sample, 10, 1000)
        self.assertEqual(reason, 'native-timestamps-unavailable')
        result = partition(0, self.pair, 'gem5', edges, False)
        self.assertEqual(result['load_unresolved'], 14)
        self.assertEqual(sum(result.values()), 20)

    def test_subcycle_is_not_rounded(self):
        self.pair['gem5_native']['native_last_response_tick'] += 1
        self.assertEqual(load_edges(self.pair, self.sample, 10, 1000)[1],
                         'native-subcycle-boundary')

    def test_callback_after_retirement_does_not_become_negative_wait(self):
        self.sample['memory_events'][0]['response_cycle'] = 120
        self.assertEqual(load_edges(self.pair, self.sample, 10, 1000)[1],
                         'fastsim-lifecycle-outside-issued-retire')

    def test_auxiliary_marks_whole_gap_unknown(self):
        result = partition(0, self.pair, 'gem5', None, True)
        self.assertEqual(dict(result), dict(unknown_auxiliary=20))

    def fixture(self):
        first = copy.deepcopy(self.pair)
        first.update(record_ordinal=10, gem5_elapsed_from_roi=0,
                     gem5_issue_to_commit=0, gem5_fetch_to_issue=0,
                     fastsim_retire_from_roi=0, fastsim_issue_to_retire=0,
                     fastsim_fetch_to_issue=0, is_load=False)
        sample = copy.deepcopy(self.sample)
        sample.update(sequence=10, actual_retire_cycle=100)
        joined = dict(core=0, pairs=[first, self.pair], auxiliary_milestones_skipped=[],
                      clock_period_ticks=10, gem5_roi_origin_tick=1000, idle_cycle_budget=3)
        return joined, [sample, self.sample]

    def test_span_difference_conserves_and_cpi_contribution_is_not_claimed(self):
        joined, samples = self.fixture()
        result = analyze_core(joined, samples)
        self.assertTrue(result['conserved'])
        self.assertEqual(result['span_difference'], -4)
        self.assertEqual(sum(result['difference'].values()), -4)
        self.assertIsNone(result['actual_cpi_contribution'])
        self.assertFalse(result['native_data_ready_available'])
        self.assertEqual(result['single_unaliased_loads'], 1)

    def test_missing_or_duplicate_hardware_identity_rejected(self):
        joined, samples = self.fixture()
        joined['pairs'][1] = dict(self.pair, record_ordinal=12)
        samples[1] = dict(self.sample, sequence=12)
        with self.assertRaisesRegex(ValueError, 'missing hardware'):
            analyze_core(joined, samples)
        joined['auxiliary_milestones_skipped'] = [11]
        result = analyze_core(joined, samples)
        self.assertEqual(result['difference']['unknown_auxiliary'], -4)
        with self.assertRaisesRegex(ValueError, 'duplicate FastSim'):
            analyze_core(joined, samples + [samples[0]])
        joined['pairs'].append(joined['pairs'][-1])
        with self.assertRaisesRegex(ValueError, 'unordered pairs'):
            analyze_core(joined, samples)

    def test_selected_cases_belong_to_standard_36(self):
        root = Path(__file__).resolve().parents[1] / 'configs'
        selection = json.loads((root / 'buffer-tso-diagnosis-representatives-v1.json').read_text())
        matrix = json.loads((root / selection['matrix']).read_text())
        workloads = {row['name'] for row in matrix['workloads']} - set(selection['excluded_workloads'])
        standard = {f'uarch-baseline-c{cores:02d}-{workload}'
                    for cores in matrix['core_counts'] for workload in workloads}
        selected = [row['case_id'] for row in selection['cases']]
        self.assertEqual(len(standard), selection['standard_case_count'])
        self.assertEqual(len(selected), len(set(selected)))
        self.assertTrue(set(selected) <= standard)
        self.assertEqual({name.split('-')[2] for name in selected}, {'c04', 'c08', 'c16', 'c32'})

    def test_internal_ready_floor_and_extended_register_edges(self):
        samples = [
            dict(sequence=10, actual_issue_cycle=1, actual_completion_cycle=8,
                 actual_retire_cycle=8, memory_events=[], has_load=False,
                 has_store=False, producer_dists=[0, 0, 0, 0, 99]),
            dict(sequence=11, actual_issue_cycle=7, actual_completion_cycle=10,
                 actual_retire_cycle=11, has_load=True, has_store=False,
                 producer_dists=[0, 0, 0, 0, 0], producer_dist_extensions=[1, 20],
                 memory_events=[dict(response_cycle=10, write=False,
                                     instruction_fetch=False, blocks_retirement=True)]),
        ]
        result = analyze_readiness(samples, dict(load_response_to_ready=1))
        self.assertEqual(result['counts']['completion_before_response'], 0)
        self.assertEqual(result['counts']['completion_before_configured_ready_floor'], 1)
        self.assertEqual(result['counts']['consumer_issue_before_producer_completion'], 1)
        self.assertEqual(result['counts']['incoming_register_edges_outside_window'], 1)
        self.assertFalse(result['native_comparison'])
        samples[1]['sequence'] = 12
        with self.assertRaisesRegex(ValueError, 'dense ordered'):
            analyze_readiness(samples, dict(load_response_to_ready=1))

    def test_idle_bounds_do_not_subtract_whole_budget_as_exact_wait(self):
        joined, samples = self.fixture()
        result = analyze_core(joined, samples)
        self.assertEqual(result['span_difference_active_bounds'], [-4, -1])
        self.assertEqual(result['span_difference'], -4)


if __name__ == '__main__':
    unittest.main()
