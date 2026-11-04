"""Freeze local accounting at retained retirement; keep team elapsed separate."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(Path(__file__).resolve().parent)]
from test_task_clock_audit import ledger
from test_task_clock_initial import idle_prefix_fixture,save_fixture,rebuild_intervals

STOP_POLICY='retired-stream-completion-v1'
COMMON_POLICY='common-end-v1'


def stop_anchor(ordinal=12,tick=50,cycle=5,counter=3,next_sequence=8):
    return dict(schema='gem5-retirement-stop-v1',record_ordinal=ordinal,tick=tick,cycle=cycle,
                active_counter=counter,next_sequence=next_sequence)


def stop_fixture(trace):
    meta,native,rows=idle_prefix_fixture(trace)
    native['retirement_stop']=stop_anchor()
    save_fixture(trace,meta,native,rows)
    return meta,native,rows


def rle_ledger():
    native=ledger();native['snapshots'][-1].update(sequence=11,tick=90,cycle=9,active_counter=7,record_ordinal=12)
    native['updates'][-1].update(count=4,last_tick=80,last_cycle=8,last_sequence=10,last_active_counter=6)
    rebuild_intervals(native)
    native['retirement_stop']=stop_anchor(tick=60,cycle=6,counter=4,next_sequence=9)
    return native


def add_second_core(trace):
    local=trace/'second-participant';local.mkdir();meta,native,rows=stop_fixture(local)
    source=local/meta['source_file'];raw=bytearray(source.read_bytes());struct.pack_into('<I',raw,20,1)
    companion=bytearray(Path(str(source)+'.fsem').read_bytes());struct.pack_into('<I',companion,16,1)
    (trace/'core1.fst').write_bytes(raw);(trace/'core1.fst.fsem').write_bytes(companion)
    meta.update(core_id=1,source_file='core1.fst');native.update(core_id=1,source_file='core1.fst',task_ids=[101,202])
    for event in rows:
        event.update(core_id=1,event_id=event['event_id'].replace('core0:','core1:'))
        for field in ('prev_pid','next_pid','os_tid','os_tgid'):
            if event.get(field)==101:event[field]=202
        for field in ('prev_task_id','next_task_id','task_id'):
            if event.get(field)==10101:event[field]=10202
        if event.get('event_kind')=='identity':event['runtime_thread_id']=2
    for row in [*native['snapshots'],*native['intervals'],*native['per_pid_macros']]:
        if row.get('pid')==101:row['pid']=202
    (trace/'thread-state-metadata-core1.json').write_text(json.dumps(meta))
    (trace/'thread-state-core1.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    (trace/'oracle/task-clock-native-core1.json').write_text(json.dumps(native))
    clock=json.loads((local/'oracle/time-accounting-core0.json').read_text());clock['core_id']=1
    (trace/'oracle/time-accounting-core1.json').write_text(json.dumps(clock))
    observations=[json.loads(line) for line in (local/'oracle/thread-state-observation.jsonl').read_text().splitlines()]
    for row in observations:row.update(core_id=1,event_id=row['event_id'].replace('core0:','core1:'))
    old=(trace/'oracle/thread-state-observation.jsonl').read_text()
    (trace/'oracle/thread-state-observation.jsonl').write_text(old+'\n'+'\n'.join(map(json.dumps,observations)))
    first=trace/'oracle/task-clock-native-core0.json';row=json.loads(first.read_text());row['task_ids']=[101,202];first.write_text(json.dumps(row))
    return native


class RetirementStopCounterTest(unittest.TestCase):
    def test_stop_inside_future_rle_uses_only_prefix_updates(self):
        from audit_task_clock import validate_retirement_stop
        data=rle_ledger();result=validate_retirement_stop(data,10,12)
        self.assertEqual(result['active_counter'],4)
        self.assertEqual(result['completion_reference_cycles'],4)
        self.assertEqual(result['next_sequence'],9)

    def test_native_counter_span_keeps_offedge_roi_rounding_separate(self):
        from audit_task_clock import validate_retirement_stop
        native=rle_ledger();native['snapshots'][0].update(tick=21,cycle=3,active_counter=1)
        rebuild_intervals(native)
        result=validate_retirement_stop(native,10,12)
        self.assertEqual(result['native_cycle_span'],3)
        self.assertEqual(result['active_counter_delta'],3)
        self.assertAlmostEqual(result['completion_reference_cycles'],3.9)

    def test_stop_after_common_end_snapshot_is_valid_only_at_same_cut(self):
        from audit_task_clock import validate_retirement_stop
        data=rle_ledger();data['retirement_stop']=stop_anchor(tick=90,cycle=9,counter=7,next_sequence=12)
        self.assertEqual(validate_retirement_stop(data,10,12)['active_counter'],7)
        data['retirement_stop']['tick']=91;data['retirement_stop']['cycle']=10;data['retirement_stop']['active_counter']=8
        with self.assertRaises(ValueError):validate_retirement_stop(data,10,12)

    def test_stop_record_count_requires_all_retained_cuts_before_anchor(self):
        from audit_task_clock import validate_retirement_stop
        for anchor in (stop_anchor(tick=30,cycle=3,counter=1,next_sequence=5),
                       stop_anchor(tick=40,cycle=4,counter=2,next_sequence=6)):
            with self.subTest(anchor=anchor):
                native=rle_ledger();native['retirement_stop']=anchor
                with self.assertRaisesRegex(ValueError,'retained.*cut'):
                    validate_retirement_stop(native,10,12)

    def test_absent_legacy_anchor_is_not_invented_and_malformed_present_anchor_refuses(self):
        from audit_task_clock import validate_retirement_stop
        data=rle_ledger();data.pop('retirement_stop')
        self.assertIsNone(validate_retirement_stop(data,10,12))
        data['retirement_stop']=None
        with self.assertRaises(ValueError):validate_retirement_stop(data,10,12)

    def test_anchor_tampering_cannot_move_across_ledger_order_or_source_population(self):
        from audit_task_clock import validate_retirement_stop
        mutations=[dict(active_counter=5),dict(next_sequence=7),dict(next_sequence=10),dict(record_ordinal=11),
                   dict(tick=61),dict(cycle=7),dict(tick=10,cycle=1,active_counter=0,next_sequence=1)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data=rle_ledger();data['retirement_stop'].update(mutation)
                with self.assertRaises(ValueError):validate_retirement_stop(data,10,12)


class RetirementStopAuditTest(unittest.TestCase):
    def test_reference_clips_native_buckets_and_preserves_common_end_diagnostics(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);_,native,_=stop_fixture(trace);before=(trace/'oracle/task-clock-native-core0.json').read_bytes()
            result=audit(trace)
            self.assertEqual(result['active_end_policy'],STOP_POLICY)
            self.assertEqual(result['active_status'],'available')
            self.assertEqual(result['active_cycles'],1)
            self.assertEqual(result['inactive_cycles'],1)
            self.assertEqual(result['transition_cycles'],1)
            self.assertEqual(result['task_macro_instructions'],4)
            self.assertEqual(result['elapsed_reference_cycles'],4)
            self.assertEqual(result['common_end_tick'],60)
            self.assertEqual(result['per_core'][0]['completion_reference_cycles'],3)
            self.assertEqual(result['common_end_diagnostic']['active_cycles'],native['active_cycles'])
            self.assertEqual(result['common_end_diagnostic']['active_cycles'],2)
            self.assertEqual(result['common_end_diagnostic']['transition_cycles'],1)
            self.assertEqual(sum(row['active_cycles'] for row in result['per_task']),1)
            self.assertEqual((trace/'oracle/task-clock-native-core0.json').read_bytes(),before)

    def test_post_stop_workload_halt_stays_common_end_diagnostic(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=stop_fixture(trace)
            sleep=copy.deepcopy(native['updates'][-1]);sleep.update(first_sequence=8,last_sequence=8,
                first_tick=60,last_tick=60,first_cycle=6,last_cycle=6,first_active_counter=3,last_active_counter=4,
                sleeping=1,next_state=0)
            native['updates'].append(sleep);native['snapshots'][-1]['sequence']=9
            native.update(valid=False,rejections=['unsupported-workload-active-halt'],halted_pids=[101])
            rebuild_intervals(native);save_fixture(trace,meta,native,rows)
            result=audit(trace)
            self.assertEqual(result['active_status'],'available')
            self.assertEqual(result['active_cycles'],1)
            self.assertNotEqual(result['common_end_diagnostic']['active_status'],'available')
            self.assertEqual(result['common_end_diagnostic']['active_cycles'],2)
            native['retirement_stop']=stop_anchor(tick=60,cycle=6,counter=4,next_sequence=9)
            save_fixture(trace,meta,native,rows)
            self.assertNotEqual(audit(trace)['active_status'],'available')

    def test_mixed_anchor_coverage_does_not_fall_back_to_common_end(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);stop_fixture(trace);native=add_second_core(trace)
            self.assertEqual(audit(trace)['active_cycles'],2)
            native.pop('retirement_stop');path=trace/'oracle/task-clock-native-core1.json';path.write_text(json.dumps(native))
            result=audit(trace)
            self.assertEqual(result['active_end_policy'],STOP_POLICY)
            self.assertNotEqual(result['active_status'],'available')
            self.assertIsNone(result['active_cycles'])
            self.assertIsNone(result['unknown_cycles'])
            self.assertEqual(result['common_end_diagnostic']['active_cycles'],4)
            native['retirement_stop']=None;path.write_text(json.dumps(native))
            with self.assertRaises(ValueError):audit(trace)

    def test_old_saved_audit_gets_explicit_common_policy_without_source_rewrite(self):
        from audit_thread_state_accounting import audit
        from time_accounting import load_gem5_time_accounting
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);idle_prefix_fixture(trace);old=audit(trace)
            old.pop('active_end_policy');old.pop('common_end_diagnostic')
            path=trace/'oracle/thread-state-accounting.json';path.write_text(json.dumps(old));before=path.read_bytes()
            current=load_gem5_time_accounting(trace,{},cores=1)
            self.assertEqual(current['active_status'],'available')
            self.assertEqual(current['active_end_policy'],COMMON_POLICY)
            self.assertEqual(path.read_bytes(),before)

    def test_old_reference_declares_common_end_policy_without_upgrading(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);idle_prefix_fixture(trace);result=audit(trace)
            self.assertEqual(result['active_end_policy'],COMMON_POLICY)
            self.assertEqual(result['active_cycles'],2)

    def test_anchor_must_follow_all_retained_functional_retirement_observations(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=stop_fixture(trace)
            native['retirement_stop']=stop_anchor(tick=40,cycle=4,counter=2,next_sequence=7)
            # The retained identity at ordinal8 retired at45. A final source
            # completion at40 cannot close all twelve retained records.
            save_fixture(trace,meta,native,rows)
            with self.assertRaises(ValueError):audit(trace)

    def test_new_policy_mismatch_refuses_cpi_but_keeps_elapsed(self):
        from audit_thread_state_accounting import audit
        from time_accounting import build_time_comparison
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);stop_fixture(trace);stopped=audit(trace)
            model=copy.deepcopy(stopped);model.update(schema='fastsim-time-accounting-v1',active_end_policy=STOP_POLICY)
            for row in model['per_core']:row['core_frequency_hz']=model['reference_frequency_hz']
            simulation=dict(measurement_scope='user-plus-kernel',scope_metrics={},time_accounting=model)
            old=copy.deepcopy(stopped);old.update(active_end_policy=COMMON_POLICY)
            comparison=build_time_comparison(simulation,{},old)
            self.assertEqual(comparison['cpi']['status'],'unavailable')
            self.assertIn('active-end-policy-mismatch',comparison['cpi']['reasons'])
            self.assertIsNone(comparison['cpi']['absolute_error'])
            self.assertEqual(comparison['elapsed']['status'],'scored')
            old.pop('active_end_policy')
            self.assertEqual(build_time_comparison(simulation,{},old)['cpi']['status'],'unavailable')
            self.assertEqual(build_time_comparison(simulation,{},stopped)['cpi']['status'],'scored-active')

    def test_model_components_must_equal_completion_at_each_core_frequency(self):
        from audit_thread_state_accounting import audit
        from time_accounting import build_time_comparison
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);stop_fixture(trace);reference=audit(trace)
            model=copy.deepcopy(reference);model['schema']='fastsim-time-accounting-v1'
            for row in model['per_core']:row['core_frequency_hz']=model['reference_frequency_hz']
            simulation=dict(measurement_scope='user-plus-kernel',scope_metrics={},time_accounting=model)
            self.assertEqual(build_time_comparison(simulation,{},reference)['cpi']['status'],'scored-active')
            for mutation in ('short-completion','padded-both','missing-frequency'):
                changed=copy.deepcopy(model);row=changed['per_core'][0]
                if mutation=='short-completion':row['completion_reference_cycles']=0
                elif mutation=='padded-both':row['active_cycles']+=1;changed['active_cycles']+=1
                else:row.pop('core_frequency_hz')
                simulation['time_accounting']=changed
                self.assertEqual(build_time_comparison(simulation,{},reference)['cpi']['status'],'unavailable')
            doubled=copy.deepcopy(model);doubled['per_core'][0]['core_frequency_hz']*=2
            doubled['per_core'][0]['completion_reference_cycles']/=2
            doubled['elapsed_reference_cycles']=1.5;doubled['elapsed_time_seconds']=1.5/doubled['reference_frequency_hz']
            simulation['time_accounting']=doubled
            self.assertEqual(build_time_comparison(simulation,{},reference)['cpi']['status'],'scored-active')

    def test_native_components_counter_and_excluded_states_must_conserve_cycle_span(self):
        from audit_thread_state_accounting import audit
        from time_accounting import _active_reason
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);stop_fixture(trace);reference=audit(trace)
            self.assertEqual(reference['per_core'][0]['native_cycle_span'],3)
            for mutation in ('counter','sleep','span','padding-both'):
                changed=copy.deepcopy(reference);row=changed['per_core'][0]
                if mutation=='counter':row['native_active_counter_delta']+=1
                elif mutation=='sleep':row['sleep_cycles']+=1
                elif mutation=='span':row['native_cycle_span']+=2
                else:row['active_cycles']+=1;changed['active_cycles']+=1
                self.assertIsNotNone(_active_reason(changed,changed['schema'],'user-plus-kernel',changed['task_macro_instructions']))

    def test_stopped_per_core_components_are_required_and_must_sum_to_aggregate(self):
        from audit_thread_state_accounting import audit
        from time_accounting import build_time_comparison
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);stop_fixture(trace);reference=audit(trace)
            model=copy.deepcopy(reference);model['schema']='fastsim-time-accounting-v1'
            for row in model['per_core']:row['core_frequency_hz']=model['reference_frequency_hz']
            simulation=dict(measurement_scope='user-plus-kernel',scope_metrics={},time_accounting=model)
            for mutation in ('missing-completion','active-mismatch','negative-transition','past-global-end'):
                changed=copy.deepcopy(model);row=changed['per_core'][0]
                if mutation=='missing-completion':row.pop('completion_reference_cycles',None)
                elif mutation=='active-mismatch':row['active_cycles']+=1
                elif mutation=='negative-transition':row['transition_cycles']=-1
                else:row['completion_reference_cycles']=100
                simulation['time_accounting']=changed
                self.assertEqual(build_time_comparison(simulation,{},reference)['cpi']['status'],'unavailable')


if __name__=='__main__':unittest.main()
