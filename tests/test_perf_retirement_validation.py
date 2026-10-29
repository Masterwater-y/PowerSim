"""Independent FST population and fail-closed retirement report regressions."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from test_task_pmu_oracle import task_fixture
from audit_perf_retirement import (
    aggregate_perf_retirement_accuracy, audit_perf_retirement,
    compare_perf_retirement, scan_functional_retirement, sum_scopes,
)


FIELDS = ('retired_macro_instructions', 'retired_uops', 'perf_retired_loads',
          'perf_retired_stores', 'perf_l1d_load_hits', 'perf_l1d_load_misses',
          'perf_l1d_load_fb_hits')


def population(macros, uops):
    row = dict.fromkeys(FIELDS, 0)
    row.update(available=True, retired_macro_instructions=macros, retired_uops=uops)
    row['coverage'] = dict(unit_contract='functional-membership-not-ISA-certification',
                           classified_loads=0, unclassified_loads=0,
                           incomplete_macros=0, incomplete_uops=0,
                           mixed_source_loads=0, forwarded_loads=0,
                           scope_crossing_macros=0, warmup_spanning_macros=0)
    return row


def retirement_fixture(trace):
    task_fixture(trace)
    scopes = {name: population(*values) for name, values in {
        'physical': (5, 6), 'user': (5, 6), 'kernel': (0, 0),
        'workload_task': (2, 3), 'other_task': (3, 3)}.items()}
    row = dict(schema='gem5-perf-retirement-v1', oracle_only=True, fst_input=False,
               unit='architectural-retired-macro-instruction',
               population_source='gem5-o3-committed-DynInst',
               population_contract='task-oncpu-switch-entry-v1',
               core_id=0, source_file='core0.records.micro.fst', source_record_count=8,
               measurement_begin_ordinal=2, measurement_end_ordinal_exclusive=8,
               common_start_tick=100, common_end_tick=160,
               common_cut_id='first-core-target-common-end-v1', task_ids=[101],
               source_classification='native-ruby-service-facts',
               perf_equivalence='unverified', scopes=scopes,
               coverage=dict(initial_owner=True, common_cut_complete=True,
                             kernel_stream_complete=True, target_drain_complete=True,
                             functional_records_match=True, functional_macros_match=True,
                             observed_leading_incomplete_macros=0,
                             observed_boundary_discontinuities=0,
                             read_failures=0, identity_failures=0,
                             unretained_hook_events=0, owner_unknown=0, pending_native=0))
    path = trace / 'oracle/perf-retirement-core0.json'
    path.write_text(json.dumps(row))
    metric = dict(schema='fastsim-perf-retirement-v1',
                  unit='architectural-retired-macro-instruction',
                  population_source='accepted-functional-prefix',
                  source_classification='model-estimate-not-native-cache-truth',
                  perf_equivalence='unverified', task_ids=[101], scopes=deepcopy(scopes),
                  per_core=[dict(core_id=0, scopes=deepcopy(scopes))])
    return path, row, metric


class PerfRetirementValidationTest(unittest.TestCase):
    def test_maintained_markdown_includes_finite_cases_percentiles_and_bias(self):
        from types import SimpleNamespace
        from run_native_kernel_fastsim_validation import write_summary, CASE_SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); trace=root/'source'; trace.mkdir()
            _, _, metric=retirement_fixture(trace)
            comparison=compare_perf_retirement(metric,audit_perf_retirement(trace))
            output=root/'output'; report_dir=output/'cases'/'fixture'; report_dir.mkdir(parents=True)
            report=dict(schema=CASE_SCHEMA, case_id='fixture', result_dir=str(trace),
                        cores=1, workload='fixture', status='passed', errors=[],
                        perf_retirement_validation=comparison)
            (report_dir/'validation.json').write_text(json.dumps(report))
            write_summary(SimpleNamespace(output_dir=output,expected_cases=1,
                                          max_attempts=3,perf_retirement=True),[report])
            markdown=(output/'perf-retirement-summary.md').read_text()
            for required in ('finite APE', 'P50', 'P90', 'P99', 'bias', 'per_1k_inst', 'raw'):
                self.assertIn(required, markdown)

    def test_all_source_coverage_and_native_uop_diagnostics_sum_across_cores(self):
        with tempfile.TemporaryDirectory() as directory:
            _, row, _ = retirement_fixture(Path(directory))
            a, b = deepcopy(row['scopes']), deepcopy(row['scopes'])
            for scopes, value in ((a, 2), (b, 7)):
                for name in ('physical', 'user', 'workload_task'):
                    scopes[name]['native_retired_uops'] = value
                    scopes[name]['coverage'].update(forwarded_loads=value,
                                                   rom_entry_macros=value)
                for name in ('kernel', 'other_task'):
                    scopes[name]['native_retired_uops'] = 0
                    scopes[name]['coverage']['rom_entry_macros'] = 0
            result = sum_scopes([a, b])
            self.assertEqual(result['physical']['native_retired_uops'], 9)
            self.assertEqual(result['physical']['coverage']['rom_entry_macros'], 9)
            self.assertEqual(result['user']['coverage']['forwarded_loads'], 9)
    def test_raw_rate_and_scope_aggregates_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); _, row, metric = retirement_fixture(trace)
            for scopes in (row['scopes'], metric['scopes'], metric['per_core'][0]['scopes']):
                for name in ('physical', 'user', 'workload_task'):
                    scopes[name]['perf_retired_loads'] = 1
                    scopes[name]['perf_l1d_load_hits'] = 1
                    scopes[name]['coverage']['classified_loads'] = 1
            audited = dict(valid=True, task_ids=[101], scopes=row['scopes'],
                           per_core=[dict(core_id=0, scopes=row['scopes'])])
            for scopes in (metric['scopes'], metric['per_core'][0]['scopes']):
                for name in ('physical', 'user', 'workload_task'):
                    scopes[name]['perf_l1d_load_hits'] = 0
                    scopes[name]['perf_l1d_load_misses'] = 1
            comparison = compare_perf_retirement(metric, audited)
            report = dict(status='passed', perf_retirement_validation=comparison)
            result = aggregate_perf_retirement_accuracy([report, dict(status='failed')])
            hit = result['user']['perf_l1d_load_hits']
            self.assertEqual(hit['raw']['mape_percent'], 100)
            self.assertEqual(hit['per_1k_inst']['wape_percent'], 100)
            self.assertEqual(result['kernel']['perf_l1d_load_hits']['raw']['finite_ape_cases'], 1)
            self.assertEqual(result['kernel']['perf_l1d_load_hits']['raw']['mape_percent'], 0)
            self.assertEqual(result['user']['perf_l1d_load_misses']['raw']['zero_reference_nonzero_prediction_cases'], 1)

    def test_fingerprint_reaudits_non_event_source_and_missing_sidecar(self):
        from types import SimpleNamespace
        from run_native_kernel_fastsim_validation import validation_fingerprint, EVENT_DICTIONARY
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); result = root/'case'; trace = result/'tao_trace'
            trace.mkdir(parents=True); sidecar, _, _ = retirement_fixture(trace)
            (result/'oracle').symlink_to(trace/'oracle', target_is_directory=True)
            for path in (result/'request.json', trace/'trace.json', trace/'manifest.txt',
                         trace/'oracle/kernel_events.json', trace/'oracle/native-summary-core0.json'):
                path.write_text('{}')
            (root/'binary').write_text('binary'); (root/'config').write_text('config')
            args = SimpleNamespace(fastsim=root/'binary', config=root/'config',
                                   event_dictionary=EVENT_DICTIONARY, perf_retirement=True)
            case = dict(result_dir=str(result), cores=1)
            validation_fingerprint(case, args)
            source=trace/'core0.records.micro.fst'; original=source.read_bytes(); data=bytearray(original)
            struct.pack_into('<H', data, 72+64*4+50, 1|2); source.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'population'):
                validation_fingerprint(case, args)
            source.write_bytes(original); sidecar.unlink()
            with self.assertRaisesRegex(ValueError, 'sidecar|participant'):
                validation_fingerprint(case, args)

    def test_scans_macro_members_through_warmup_and_switch_anchor(self):
        # Production break: resetting membership at start, line/UOP counting,
        # or applying switch-before-anchor changes these literal populations.
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory)
            task_fixture(trace)
            path = trace / 'thread-state-metadata-core0.json'
            meta = json.loads(path.read_text())
            meta.update(warmup_records=3, measurement_records=5)
            source = trace / meta['source_file']
            data = bytearray(source.read_bytes())
            struct.pack_into('<H', data, 72 + 64 * 2 + 50, 1 | 1024 | 4)
            # One atomic macro after returning to TASK has read AND write.
            struct.pack_into('<H', data, 72 + 64 * 7 + 50, 1 | 8)
            source.write_bytes(data)
            rows = [json.loads(s) for s in (trace / 'thread-state-core0.jsonl').read_text().splitlines()]
            scopes = scan_functional_retirement(trace, meta, rows, [101])
            self.assertEqual(scopes['physical']['retired_macro_instructions'], 5)
            self.assertEqual(scopes['physical']['retired_uops'], 5)
            self.assertEqual(scopes['physical']['perf_retired_stores'], 2)
            self.assertEqual(scopes['physical']['perf_retired_loads'], 1)
            self.assertEqual(scopes['workload_task']['perf_retired_stores'], 2)
            self.assertEqual(scopes['other_task']['perf_retired_stores'], 0)
            self.assertEqual(scopes['physical']['warmup_spanning_macros'], 1)

    def test_split_load_counts_once_and_partial_tail_is_not_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory)
            task_fixture(trace)
            meta = json.loads((trace / 'thread-state-metadata-core0.json').read_text())
            source = trace / meta['source_file']; data = bytearray(source.read_bytes())
            struct.pack_into('<Q', data, 72 + 64 * 4 + 8, 60)
            struct.pack_into('<HH', data, 72 + 64 * 4 + 48, 8, 1 | 2)
            struct.pack_into('<H', data, 72 + 64 * 7 + 50, 1 | 2 | 1024)
            source.write_bytes(data)
            rows = [json.loads(s) for s in (trace / 'thread-state-core0.jsonl').read_text().splitlines()]
            got = scan_functional_retirement(trace, meta, rows, [101])['physical']
            self.assertEqual(got['perf_retired_loads'], 1)
            self.assertEqual(got['retired_macro_instructions'], 4)
            self.assertEqual(got['incomplete_macros'], 1)

    def test_warmup_only_tail_remains_diagnostic_without_retiring_an_event(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory); task_fixture(trace)
            meta=json.loads((trace/'thread-state-metadata-core0.json').read_text())
            meta.update(warmup_records=8, measurement_records=0)
            source=trace/meta['source_file']; data=bytearray(source.read_bytes())
            struct.pack_into('<H', data, 72+64*7+50, 1|2|1024)
            source.write_bytes(data)
            rows=[json.loads(s) for s in (trace/'thread-state-core0.jsonl').read_text().splitlines()]
            got=scan_functional_retirement(trace, meta, rows, [101])['physical']
            self.assertEqual(got['retired_macro_instructions'], 0)
            self.assertEqual(got['perf_retired_loads'], 0)
            self.assertEqual(got['retired_uops'], 0)
            self.assertEqual(got['incomplete_macros'], 1)

    def test_audit_matches_independent_populations_and_rejects_missing_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory)
            path, row, metric = retirement_fixture(trace)
            audited = audit_perf_retirement(trace)
            self.assertTrue(audited['valid'])
            result = compare_perf_retirement(metric, audited)
            self.assertEqual(result['scopes']['workload_task']['fields']['perf_retired_stores']['absolute_error'], 0)
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'sidecar|participant|missing'):
                audit_perf_retirement(trace)

    def test_rejects_bounds_owner_source_and_population_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); path, row, metric = retirement_fixture(trace)
            for key, bad in [('measurement_begin_ordinal', 1), ('task_ids', [999]),
                             ('source_classification', 'taotrace-path-class-v3')]:
                changed = deepcopy(row); changed[key] = bad
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): audit_perf_retirement(trace)
            path.write_text(json.dumps(row))
            source = trace / row['source_file']; data = bytearray(source.read_bytes())
            struct.pack_into('<H', data, 72 + 64 * 4 + 50, 1 | 2)
            source.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'population'):
                audit_perf_retirement(trace)

    def test_no_legacy_fallback_and_per_core_conservation_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); _, _, metric = retirement_fixture(trace)
            audited = audit_perf_retirement(trace)
            with self.assertRaises(ValueError): compare_perf_retirement({}, audited)
            bad = deepcopy(metric)
            bad['per_core'][0]['scopes']['user']['perf_retired_loads'] = 1
            with self.assertRaises(ValueError): compare_perf_retirement(bad, audited)
            for field in ('incomplete_macros', 'incomplete_uops', 'warmup_spanning_macros'):
                bad = deepcopy(metric)
                for scopes in (bad['scopes'], bad['per_core'][0]['scopes']):
                    for name in ('physical', 'user', 'workload_task'):
                        scopes[name]['coverage'][field] = 1
                with self.assertRaisesRegex(ValueError, 'functional coverage'):
                    compare_perf_retirement(bad, audited)

    def test_unclassified_cache_population_does_not_score_subset_as_whole(self):
        # Raw retired-load population remains comparable; source gaps forbid
        # computing a cache error from two different classified subsets.
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); path, row, metric = retirement_fixture(trace)
            for scopes in (row['scopes'], metric['scopes'], metric['per_core'][0]['scopes']):
                for name in ('physical', 'user', 'workload_task'):
                    scopes[name]['perf_retired_loads'] = 1
                    scopes[name]['coverage']['unclassified_loads'] = 1
            # Comparison receives an already independently audited fixture;
            # this test isolates event coverage, not functional scanning.
            audited = dict(valid=True, task_ids=[101], scopes=row['scopes'],
                           per_core=[dict(core_id=0, scopes=row['scopes'])])
            result = compare_perf_retirement(metric, audited)
            fields = result['scopes']['user']['fields']
            self.assertEqual(fields['perf_retired_loads']['absolute_error'], 0)
            self.assertEqual(fields['perf_l1d_load_hits']['status'], 'unavailable-source-coverage')
            self.assertIsNone(fields['perf_l1d_load_hits']['absolute_percentage_error'])


if __name__ == '__main__':
    unittest.main()
