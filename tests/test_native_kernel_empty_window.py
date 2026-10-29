"""A native-capable measurement window need not execute kernel code."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_native_kernel_fastsim_validation as validator
from merge_kernel_events_oracle_v3 import merge
from test_native_kernel_pmu_validation import prediction
from test_p0_contract import row
import test_p1_native_response_sideband as native_fixture


class EmptyKernelWindowTest(unittest.TestCase):
    def rescore(self, directory, *, reference_kernel=0, replay_kernel=0,
                kernel_memory=0, include_kernel=True, native_mode=True):
        root = Path(directory)
        result = native_fixture.NativeResponseSidebandTest().make_result_summary(root)
        reference = row()
        reference['user_plus_kernel_retired_instructions'] = 2 + reference_kernel
        reference['perf_like_cpi_user_plus_kernel'] = 10 / (2 + reference_kernel)
        reference['pmu_user_plus_kernel'].update(
            retired_instructions=2 + reference_kernel,
            retired_uops=4 + reference_kernel)
        reference['pmu_kernel_by_class']['scheduler'].update(
            retired_instructions=reference_kernel, retired_uops=reference_kernel)
        (result / 'oracle/kernel_events.json').write_text(json.dumps(merge([reference])))
        request = json.loads((result / 'request.json').read_text())
        request.update(sampling={'functional_include_kernel': include_kernel},
                       profile={'cores': 1})
        (result / 'request.json').write_text(json.dumps(request))
        trace = result / 'tao_trace'
        trace.mkdir()
        (trace / 'manifest.txt').write_text('fixture\n')
        (trace / 'trace.json').write_text(json.dumps(dict(
            trace_scope='user-plus-kernel', functional_boundaries={'0': dict(
                measurement_user_records=4, measurement_records=4 + reference_kernel,
                measurement_user_instructions=2,
                measurement_instructions=2 + reference_kernel)})))
        pmu = prediction(branch=0, l1d=0, l2=0, llc=0)
        pmu.update(retired_uops=4 + replay_kernel,
                   retired_instructions=2 + replay_kernel, llc_tag_accesses=0)
        simulation = dict(
            measurement_scope='user-plus-kernel',
            configuration=dict(cores=1, native_kernel_trace=native_mode),
            scope_metrics=dict(
                pmu_source='fastsim-functional-native-user-plus-kernel-v1',
                pmu_contract_id=validator.PMU_CONTRACT_ID, pmu=pmu,
                user_trace_uops=4, native_kernel_trace_uops=replay_kernel,
                user_trace_instructions=2,
                native_kernel_trace_instructions=replay_kernel, perf_like_cpi=4.0),
            totals=dict(native_kernel_trace=dict(
                records=replay_kernel, retired_instructions=replay_kernel,
                memory_uops=kernel_memory), syscall_uops=0, llc_outcomes_conserved=True,
                **{name: {'events': 0} for name in (
                    'synthetic_syscall_kernel', 'synthetic_page_fault_kernel',
                    'synthetic_irq_kernel', 'synthetic_kernel_total')}), cha=[])
        stats = root / 'stats'
        stats.mkdir()
        (stats / 'fixture.json').write_text(json.dumps(simulation))
        args = SimpleNamespace(
            output_dir=root / 'reports', stats_dir=stats, repo_root=ROOT,
            fastsim=root / 'must-not-run', config=root / 'unused.cfg',
            event_dictionary=validator.EVENT_DICTIONARY)
        case = dict(case_id='fixture', cores=1, workload='fixture', result_dir=str(result))
        return validator.run_case(case, args)

    def test_native_capable_user_only_window_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.rescore(directory)
        self.assertEqual(report['status'], 'passed', report['errors'])
        self.assertEqual(report['trace_counts']['kernel_uops'], 0)
        self.assertEqual(report['trace_counts']['kernel_memory_uops'], 0)
        self.assertEqual(report['pmu_validation']['status'], 'scored')

    def test_kernel_execution_without_memory_accesses_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.rescore(directory, reference_kernel=1, replay_kernel=1)
        self.assertEqual(report['status'], 'passed', report['errors'])
        self.assertEqual(report['trace_counts']['kernel_uops'], 1)
        self.assertEqual(report['trace_counts']['kernel_memory_uops'], 0)

    def test_lost_kernel_records_still_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.rescore(directory, reference_kernel=1)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('trace_kernel_uops_match', report['errors'])
        self.assertEqual(report['pmu_validation']['status'], 'source-gate-failed')

    def test_missing_native_capture_declaration_still_fails(self):
        for changes in ({'include_kernel': False}, {'native_mode': False}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                report = self.rescore(directory, **changes)
            self.assertEqual(report['status'], 'failed')

    def test_invalid_kernel_memory_counts_still_fail(self):
        for memory in (-1, 2):
            with self.subTest(memory=memory), tempfile.TemporaryDirectory() as directory:
                report = self.rescore(directory, kernel_memory=memory)
            self.assertEqual(report['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
