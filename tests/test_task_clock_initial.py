"""Initial idle eligibility requires a complete, bound first-account-out proof."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(Path(__file__).resolve().parent)]
from test_task_clock_audit import capture_fixture
from test_task_clock_contract import cut
from test_thread_state_graph import event,initial,identity,switch


def fnv64(data):
    value=14695981039346656037
    for byte in data:value=((value^byte)*1099511628211)&0xffffffffffffffff
    return value


def rebind_source(trace,meta,native):
    source=trace/meta['source_file'];raw=source.read_bytes();value=fnv64(raw[72:])
    companion=Path(str(source)+'.fsem');data=bytearray(companion.read_bytes());struct.pack_into('<Q',data,40,value)
    for offset in range(56,len(data),16):
        ordinal=struct.unpack_from('<Q',data,offset)[0];bound=2166136261
        for byte in raw[72+64*ordinal:72+64*(ordinal+1)]:bound=((bound^byte)*16777619)&0xffffffff
        struct.pack_into('<I',data,offset+8,bound)
    companion.write_bytes(data);native['source_stream_hash']=value
    if 'task_clock_initial_resolution' in meta:meta['task_clock_initial_resolution']['source_stream_hash']=value
    if 'initial_resolution' in native:native['initial_resolution']['source_stream_hash']=value
    return value


def save_fixture(trace,meta,native,rows):
    meta['event_count']=len(rows)
    (trace/'thread-state-metadata-core0.json').write_text(json.dumps(meta))
    (trace/'thread-state-core0.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    (trace/'oracle/task-clock-native-core0.json').write_text(json.dumps(native))
    ticks={'initial_owner':0,'account_out':30,'sched_switch':35,'account_in':40,'identity':45}
    observations=[dict(schema='fastsim-thread-state-observation-v1',oracle_only=True,
        **{k:e[k] for k in ('event_id','event_kind','core_id','record_ordinal')},commit_tick=10 if e['event_kind']=='identity' and e['phase']=='warmup' else ticks[e['event_kind']]) for e in rows]
    (trace/'oracle/thread-state-observation.jsonl').write_text('\n'.join(map(json.dumps,observations)))


def idle_prefix_fixture(trace):
    meta,native=capture_fixture(trace)
    rows=[initial(0,0),cut(3,'account_out',0,101),switch(0,4,0,101,raw_state=0),
          cut(6,'account_in',0,101),identity(0,8,101)]
    meta.update(task_clock_initial_known=False,task_clock_initial_enabled=False,task_clock_initial_pid=0,
                task_clock_continuous_capture=True)
    source=trace/meta['source_file'];raw=bytearray(source.read_bytes())
    struct.pack_into('<Q',raw,32,128|256|16)
    for ordinal in (0,3,6):struct.pack_into('<h',raw,72+64*ordinal+52,-2)
    source.write_bytes(raw)
    resolution=dict(kind='first-account-out-idle-prefix-v1',record_ordinal=3,prev_pid=0,source_stream_hash=0)
    meta['task_clock_initial_resolution']=copy.deepcopy(resolution);native['initial_resolution']=copy.deepcopy(resolution)
    native.update(initial_known=False,initial_enabled=False,initial_pid=0,active_cycles=2,other_active_cycles=1,
                  transition_active_cycles=1,unknown_active_cycles=0,task_macro_instructions=4,
                  per_pid_macros=[dict(pid=0,macro_instructions=2),dict(pid=101,macro_instructions=4)])
    for snapshot in native['snapshots']:
        if snapshot['kind']=='roi_begin':snapshot.update(pid=0,known=False,enabled=False)
        elif snapshot['kind']=='account_out':snapshot.update(pid=0)
        else:snapshot.update(pid=101)
    for interval,start,end in zip(native['intervals'],native['snapshots'],native['snapshots'][1:]):
        interval.update(pid=start['pid'],known=start['known'],enabled=start['enabled'])
    rebind_source(trace,meta,native);save_fixture(trace,meta,native,rows)
    return meta,native,rows


def rebuild_intervals(native):
    native['intervals']=[dict(start_tick=a['tick'],end_tick=b['tick'],start_cycle=a['cycle'],end_cycle=b['cycle'],
        active_cycles=b['active_counter']-a['active_counter'],start_sequence=a['sequence'],end_sequence=b['sequence'],
        pid=a['pid'],known=a['known'],enabled=a['enabled']) for a,b in zip(native['snapshots'],native['snapshots'][1:])]


def task_prefix_fixture(trace):
    meta,native,_=idle_prefix_fixture(trace)
    rows=[initial(0,101),identity(0,1,101),cut(3,'account_out',101,202),switch(0,4,101,202,raw_state=0),
          cut(6,'account_in',101,202),identity(0,8,202,2)]
    meta['task_clock_initial_pid']=101
    meta['task_clock_initial_resolution'].update(kind='first-account-out-task-prefix-v1',prev_pid=101)
    native.update(initial_pid=101,active_cycles=3,other_active_cycles=0,task_macro_instructions=6,task_ids=[101,202],
        per_pid_macros=[dict(pid=101,macro_instructions=2),dict(pid=202,macro_instructions=4)])
    native['initial_resolution']=copy.deepcopy(meta['task_clock_initial_resolution'])
    for snapshot in native['snapshots']:
        snapshot['pid']=101 if snapshot['kind'] in ('roi_begin','account_out') else 202
    rebuild_intervals(native);save_fixture(trace,meta,native,rows)
    return meta,native,rows


class TaskClockInitialTest(unittest.TestCase):
    def test_positive_kernel_initial_same_incarnation_is_resolved_by_complete_first_out(self):
        from task_clock_initial import effective_initial_state,validate_initial_resolution
        from compile_thread_state import compile_plan
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=task_prefix_fixture(trace)
            self.assertEqual(effective_initial_state(meta,rows),dict(known=True,enabled=True,pid=101))
            self.assertEqual(validate_initial_resolution(trace/meta['source_file'],meta,rows)['kind'],
                'first-account-out-task-prefix-v1')
            plan=trace/'state.plan';compile_plan(trace,plan,require_sources=True)
            self.assertIn('CLOCK_STATE 0 1 1 101 1',plan.read_text())
            report=audit(trace)
            self.assertEqual(report['active_status'],'available')
            self.assertEqual(report['active_cycles'],3)
            self.assertEqual(report['task_macro_instructions'],6)
            self.assertEqual(sum(row['active_cycles'] for row in report['per_task']),3)
            self.assertEqual(sum(row['macro_instructions'] for row in report['per_task']),6)
            self.assertFalse(meta['task_clock_initial_known'])
            self.assertFalse(native['snapshots'][0]['known'])

    def test_positive_nonworkload_prefix_remains_other_context(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=task_prefix_fixture(trace)
            rows=[e for e in rows if not(e['event_kind']=='identity' and e['os_tid']==101)]
            native.update(task_ids=[202],task_macro_instructions=4,active_cycles=2,other_active_cycles=1)
            save_fixture(trace,meta,native,rows)
            report=audit(trace)
            self.assertEqual(report['active_status'],'available')
            self.assertEqual(report['active_cycles'],2)
            self.assertEqual(report['inactive_cycles'],1)
            self.assertEqual(report['task_macro_instructions'],4)
            self.assertEqual(report['per_task'][0]['os_tid'],202)

    def test_positive_prefix_mismatched_pid_incarnation_phase_and_cut_refuse(self):
        from task_clock_initial import effective_initial_state
        for mutation in ('pid','incarnation','phase','kind','first-in'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                meta,_,rows=task_prefix_fixture(Path(directory))
                if mutation=='pid':meta['task_clock_initial_resolution']['prev_pid']=202
                elif mutation=='incarnation':rows[2]['prev_task_id']+=1
                elif mutation=='phase':rows[2]['phase']='warmup'
                elif mutation=='kind':meta['task_clock_initial_resolution']['kind']='first-account-out-idle-prefix-v1'
                else:rows[2]['event_kind']='account_in';rows[2]['pc']=0xffffffff81067eef
                with self.assertRaises(ValueError):effective_initial_state(meta,rows)

    def test_positive_prefix_sleep_notification_refuses_forged_resolution(self):
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=task_prefix_fixture(trace)
            native['updates'][2].update(sleeping=1,next_state=0)
            native['updates'][3].update(previous_state=0,active_delta=0,first_active_counter=1,last_active_counter=1)
            native['updates'][4].update(first_active_counter=1,last_active_counter=2)
            native['snapshots'][2]['active_counter']=1;native['snapshots'][3]['active_counter']=3
            native.update(active_cycles=3,transition_active_cycles=0)
            rebuild_intervals(native);save_fixture(trace,meta,native,rows)
            report=audit(trace)
            self.assertNotEqual(report['active_status'],'available')
            self.assertTrue(any('initial-resolution' in reason for reason in report['limitations']))

    def test_positive_prefix_sleep_or_wakeup_at_roi_refuses_forged_resolution(self):
        from audit_thread_state_accounting import audit
        for state in (0,1):
            with self.subTest(state=state),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                trace=Path(directory);meta,native,rows=task_prefix_fixture(trace)
                native['counter_epoch']=dict(schema='gem5-basecpu-counter-epoch-v1',source='BaseCPU::previousState/previousCycle',
                    sequence=0,tick=20,cycle=2,previous_cycle=2,previous_state=state,period=10)
                native['updates']=copy.deepcopy(native['updates'][2:])
                counter=0;prior=state
                for index,update in enumerate(native['updates']):
                    sleeping=0 if index==0 and state==0 else -1
                    active=1 if prior==2 else 0
                    update.update(first_sequence=2+2*index,last_sequence=2+2*index,bootstrap=False,
                        previous_state=prior,next_state=1 if sleeping==0 else 2,sleeping=sleeping,
                        active_delta=active,first_active_counter=counter,last_active_counter=counter+active)
                    counter+=active;prior=update['next_state']
                counters=(0,0,0,2) if state==0 else (0,0,1,3)
                for index,snapshot in enumerate(native['snapshots']):snapshot.update(sequence=1+2*index,active_counter=counters[index])
                native.update(active_cycles=2,transition_active_cycles=0 if state==0 else 1)
                rebuild_intervals(native);save_fixture(trace,meta,native,rows)
                report=audit(trace)
                self.assertNotEqual(report['active_status'],'available')
                self.assertTrue(any('initial-resolution' in reason for reason in report['limitations']))

    def test_positive_prefix_without_new_resolution_stays_unknown(self):
        from task_clock_initial import effective_initial_state
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            meta,_,rows=task_prefix_fixture(Path(directory));meta.pop('task_clock_initial_resolution')
            self.assertEqual(effective_initial_state(meta,rows),dict(known=False,enabled=False,pid=101))

    def test_first_true_idle_out_proves_effective_initial_without_changing_raw_facts(self):
        from task_clock_initial import effective_initial_state,validate_initial_resolution
        from thread_state_graph import load_capture_evidence
        from compile_thread_state import compile_plan
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace)
            proof=validate_initial_resolution(trace/meta['source_file'],meta,rows)
            self.assertEqual(proof['cut_count'],2)
            self.assertEqual(effective_initial_state(meta,rows),dict(known=True,enabled=True,pid=0))
            evidence=load_capture_evidence(trace)
            self.assertTrue(evidence['graph']['cores'][0]['task_clock_initial_known'])
            self.assertFalse(evidence['metadata'][0]['task_clock_initial_known'])
            plan=trace/'state.plan';compile_plan(trace,plan,require_sources=True)
            self.assertIn('CLOCK_STATE 0 1 1 0 1',plan.read_text())
            self.assertNotIn('source_stream_hash',plan.read_text())
            original=(trace/'oracle/task-clock-native-core0.json').read_bytes()
            report=audit(trace)
            self.assertEqual(report['active_status'],'available')
            self.assertEqual(report['active_cycles'],2)
            self.assertEqual(report['inactive_cycles'],1)
            self.assertEqual(report['unknown_cycles'],0)
            self.assertEqual(report['task_macro_instructions'],4)
            self.assertEqual((trace/'oracle/task-clock-native-core0.json').read_bytes(),original)
            self.assertFalse(native['snapshots'][0]['known'])

    def test_structural_forgery_incomplete_capture_and_wrong_identity_refuse(self):
        from task_clock_initial import effective_initial_state
        mutations=[lambda m:m.update(task_clock_continuous_capture=False),lambda m:m.pop('task_clock_continuous_capture'),
            lambda m:m.update(task_clock_initial_pid=101),lambda m:m.update(task_clock_initial_known=True),
            lambda m:m.update(kernel_sha256='0'*64),lambda m:m.update(kernel_profile_id='sha256:bad'),
            lambda m:m.update(read_failures=1),lambda m:m.update(kernel_stream_complete=False),
            lambda m:m['task_clock_initial_resolution'].update(prev_pid=101),
            lambda m:m['task_clock_initial_resolution'].update(record_ordinal=6),
            lambda m:m['task_clock_initial_resolution'].update(kind='pid-zero-is-idle'),
            lambda m:m.update(task_clock_initial_pid=False)]
        for mutate in mutations:
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                meta,_,rows=idle_prefix_fixture(Path(directory));mutate(meta)
                with self.assertRaises(ValueError):effective_initial_state(meta,rows)

    def test_full_stream_hidden_or_missing_cut_and_wrong_hash_refuse(self):
        from task_clock_initial import validate_initial_resolution
        for mutation in ('hidden-in','missing-event','duplicate-event','wrong-hash','wrong-pc','event-pc'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace)
                source=trace/meta['source_file'];raw=bytearray(source.read_bytes())
                if mutation=='hidden-in':
                    struct.pack_into('<Q',raw,72+64,0xffffffff81067eef);struct.pack_into('<h',raw,72+64+52,-2)
                    source.write_bytes(raw);rebind_source(trace,meta,native)
                elif mutation=='missing-event':rows=[e for e in rows if e['event_kind']!='account_in']
                elif mutation=='duplicate-event':rows.append(copy.deepcopy(rows[1]))
                elif mutation=='wrong-hash':meta['task_clock_initial_resolution']['source_stream_hash']+=1
                elif mutation=='event-pc':rows[3]['pc']=4096
                else:struct.pack_into('<Q',raw,72+3*64,4096);source.write_bytes(raw);rebind_source(trace,meta,native)
                with self.assertRaises(ValueError):validate_initial_resolution(source,meta,rows)

    def test_no_new_proof_preserves_unknown_and_native_resolution_must_match(self):
        from task_clock_initial import effective_initial_state
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace)
            meta.pop('task_clock_initial_resolution');native.pop('initial_resolution')
            self.assertEqual(effective_initial_state(meta,rows),dict(known=False,enabled=False,pid=0))
            native.update(other_active_cycles=0,unknown_active_cycles=1,valid=False,
                rejections=['native-roi-initial-eligibility-unknown','unknown-native-task-clock-coverage'],
                per_pid_macros=[dict(pid=101,macro_instructions=4)])
            save_fixture(trace,meta,native,rows)
            self.assertNotEqual(audit(trace)['active_status'],'available')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace);native.pop('initial_resolution')
            save_fixture(trace,meta,native,rows)
            with self.assertRaisesRegex(ValueError,'resolution'):audit(trace)

    def test_hardware_entry_at_cut_pc_is_excluded_from_true_cut_population(self):
        from task_clock_initial import validate_initial_resolution
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace)
            source=trace/meta['source_file'];raw=bytearray(source.read_bytes())
            # The bound functional companion, rather than PC alone, excludes
            # hardware-entry pseudo records from the architectural cut scan.
            struct.pack_into('<Q',raw,72+7*64,0xffffffff81067eef)
            source.write_bytes(raw);rebind_source(trace,meta,native)
            self.assertEqual(validate_initial_resolution(source,meta,rows)['cut_count'],2)

    def test_idle_incarnation_must_match_first_out(self):
        from task_clock_initial import effective_initial_state
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            meta,_,rows=idle_prefix_fixture(Path(directory));rows[1]['prev_task_id']+=1
            with self.assertRaises(ValueError):effective_initial_state(meta,rows)

    def test_compiler_and_capture_loader_reject_missing_nonanchor_cut(self):
        from compile_thread_state import compile_plan
        from thread_state_graph import load_capture_evidence
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);meta,native,rows=idle_prefix_fixture(trace)
            source=trace/meta['source_file'];raw=bytearray(source.read_bytes())
            struct.pack_into('<Q',raw,72+2*64,0xffffffff81067eef);struct.pack_into('<h',raw,72+2*64+52,-2)
            source.write_bytes(raw);rebind_source(trace,meta,native);save_fixture(trace,meta,native,rows)
            with self.assertRaisesRegex(ValueError,'cut'):compile_plan(trace,trace/'state.plan',require_sources=True)
            with self.assertRaisesRegex(ValueError,'cut'):load_capture_evidence(trace)


if __name__=='__main__':unittest.main()
