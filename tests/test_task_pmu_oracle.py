import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from audit_task_pmu import audit_task_pmu
from audit_p1_native_response_sideband import empty_native_population
from test_thread_state_accounting_oracle import fixture
from test_thread_state_graph import certify


def task_fixture(trace):
    fixture(trace)
    meta_path = trace / 'thread-state-metadata-core0.json'
    meta_path.write_text(json.dumps(certify(json.loads(meta_path.read_text()))))
    owners = []
    for pid, macros in ((101, 2), (0, 3)):
        native = empty_native_population()
        native['ruby_aliased_admission_fragments'] = 0
        owners.append(dict(os_tid=pid, retired_instructions=macros, retired_uops=3,
                           memory_uops=0, branches=0, branch_misses=0, native=native))
    sidecar = dict(schema='fastsim-task-pmu-v1', oracle_only=True, fst_input=False,
                   population_contract='task-oncpu-switch-entry-v1', scope='workload-task',
                   hierarchy_source='ruby-slicc-controller-actions',
                   branch_miss_source='taotrace-retired-bpred-v1',
                   common_cut_id='first-core-target-common-end-v1', core_id=0,
                   common_start_tick=100, common_end_tick=160, source_record_count=8,
                   measurement_begin_ordinal=2, measurement_end_ordinal_exclusive=8,
                   task_ids=[101], owners=owners,
                   coverage=dict(initial_owner=True, common_cut_complete=True,
                                 kernel_stream_complete=True, target_drain_complete=True,
                                 read_failures=0, identity_failures=0, unretained_hook_events=0,
                                 owner_unknown=0, pending_native=0))
    path = trace / 'oracle/task-pmu-core0.json'
    path.write_text(json.dumps(sidecar))
    return path, sidecar


class TaskPmuOracleTest(unittest.TestCase):
    def test_report_does_not_pool_task_and_physical_pmu_populations(self):
        from run_native_kernel_fastsim_validation import write_summary, CASE_SCHEMA
        from test_native_kernel_pmu_validation import prediction, oracle, native
        from run_native_kernel_fastsim_validation import (
            build_pmu_comparison, load_pmu_event_contract, EVENT_DICTIONARY,
        )
        comparison=build_pmu_comparison(prediction(),oracle(),native(),load_pmu_event_contract(EVENT_DICTIONARY))
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory);cases=[]
            for i,scope in enumerate(('workload-task','user-plus-kernel')):
                case=dict(case_id=str(i),result_dir='/fixture/'+str(i))
                cases.append(case);path=output/'cases'/str(i);path.mkdir(parents=True)
                report=dict(case,schema=CASE_SCHEMA,cores=1,workload='fixture',status='passed',
                            errors=[],attempts=1,pmu_validation=dict(status='scored',scope=scope),pmu=comparison)
                (path/'validation.json').write_text(json.dumps(report))
            summary=write_summary(SimpleNamespace(output_dir=output,expected_cases=2,max_attempts=3),cases)
            self.assertEqual(summary['pmu_scope'],'mixed')
            self.assertEqual(summary['pmu_accuracy']['l1d_tag_misses']['cases'],0)
            for scope in ('workload-task','user-plus-kernel'):
                self.assertEqual(summary['pmu_accuracy_by_scope'][scope]['l1d_tag_misses']['cases'],1)

    def test_syscall_marker_expansion_conserves_native_uops_and_control_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=task_fixture(trace)
            meta=json.loads((trace/'thread-state-metadata-core0.json').read_text())
            source=trace/meta['source_file'];data=bytearray(source.read_bytes())
            struct.pack_into('<h',data,72+64*7+52,-1)
            source.write_bytes(data)
            row['owners'][0]['retired_uops']+=24
            row['owners'][0]['branches']+=1
            path.write_text(json.dumps(row))
            result=audit_task_pmu(trace)
            self.assertEqual(result['pmu']['retired_uops'],27)
            self.assertEqual(result['pmu']['branches'],1)
            row['owners'][0]['retired_uops']=14  # common cut within collapsed syscall
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError,'population'): audit_task_pmu(trace)

    def test_maintained_scorer_accepts_only_matching_task_population_and_conservation(self):
        from run_native_kernel_fastsim_validation import (
            build_task_pmu_comparison, load_pmu_event_contract, EVENT_DICTIONARY,
        )
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);task_fixture(trace);audited=audit_task_pmu(trace)
        metric=dict(status='available',scope='workload-task',
                    population_contract='task-oncpu-switch-entry-v1',task_ids=[101],
                    pmu=dict(audited['pmu']),other_pmu=dict(audited['other']),
                    full_pmu={k:audited['pmu'][k]+audited['other'][k] for k in audited['pmu']})
        metric['per_core']=[dict(core=0,pmu=dict(metric['pmu']),other_pmu=dict(metric['other_pmu']),
                                 full_pmu=dict(metric['full_pmu']))]
        contract=load_pmu_event_contract(EVENT_DICTIONARY)
        compared=build_task_pmu_comparison(metric,audited,contract)
        self.assertEqual(compared['l1d_tag_misses']['reference'],0)
        metric['task_ids']=[102]
        with self.assertRaisesRegex(ValueError,'task'): build_task_pmu_comparison(metric,audited,contract)
        metric['task_ids']=[101];metric['full_pmu']['branch_misses']=1
        with self.assertRaisesRegex(ValueError,'conserv'): build_task_pmu_comparison(metric,audited,contract)
        metric['full_pmu']['branch_misses']=0
        metric['per_core'][0]['pmu']['retired_instructions']=1
        with self.assertRaisesRegex(ValueError,'population|conserv'): build_task_pmu_comparison(metric,audited,contract)

    def test_complete_owner_population_includes_switch_anchor_before_owner_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); task_fixture(trace)
            result = audit_task_pmu(trace)
        self.assertEqual(result['task_ids'], [101])
        self.assertEqual(result['pmu']['retired_instructions'], 2)
        self.assertEqual(result['pmu']['retired_uops'], 3)
        self.assertEqual(result['pmu']['l1d_tag_misses'], 0)
        self.assertEqual(result['other']['retired_instructions'], 3)

    def test_missing_native_or_incomplete_drain_fails_closed(self):
        for change in ('missing', 'drain', 'proxy', 'unknown', 'bounds'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);path,row=task_fixture(trace)
                if change=='missing': del row['owners'][0]['native']
                if change=='drain': row['coverage']['target_drain_complete']=False
                if change=='proxy': row['hierarchy_source']='taotrace-path-class-v3'
                if change=='unknown': row['coverage']['owner_unknown']=1
                if change=='bounds': row['measurement_begin_ordinal']=3
                path.write_text(json.dumps(row))
                with self.assertRaises(ValueError): audit_task_pmu(trace)

    def test_wrong_owner_native_attribution_cannot_pass_aggregate_conservation(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=task_fixture(trace)
            meta=json.loads((trace/'thread-state-metadata-core0.json').read_text())
            source=trace/meta['source_file'];data=bytearray(source.read_bytes())
            # Last micro-op at switch-out still belongs to TASK, not next owner.
            struct.pack_into('<H',data,72+64*3+50,1|2|1024|2048)
            source.write_bytes(data)
            row['owners'][1]['memory_uops']=1
            native=row['owners'][1]['native']
            native.update(memory_uops=1, no_ruby_uops=1, hierarchy_complete_uops=1,
                          architectural_line_requests=1)
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError,'owner|population'): audit_task_pmu(trace)

    def test_changed_non_event_macro_cannot_reuse_saved_oracle_population(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);task_fixture(trace)
            meta=json.loads((trace/'thread-state-metadata-core0.json').read_text())
            source=trace/meta['source_file'];data=bytearray(source.read_bytes())
            struct.pack_into('<H',data,72+64*7+50,0)
            source.write_bytes(data)
            with self.assertRaisesRegex(ValueError,'population'): audit_task_pmu(trace)
