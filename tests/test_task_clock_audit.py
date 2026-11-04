"""Independent native task counter arithmetic and evidence tampering."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))

def ledger():
    def update(seq,cycle,previous,nxt,counter,active,sleeping=-1,bootstrap=False):
        return dict(first_tick=cycle*10,last_tick=cycle*10,first_cycle=cycle,last_cycle=cycle,
                    first_sequence=seq,last_sequence=seq,count=1,delta=1,active_delta=active,period=10,
                    first_active_counter=counter,last_active_counter=counter+active,sleeping=sleeping,
                    previous_state=previous,next_state=nxt,bootstrap=bootstrap)
    def snap(seq,kind,cycle,counter,pid,enabled,ordinal):
        return dict(sequence=seq,kind=kind,tick=cycle*10,cycle=cycle,active_counter=counter,
                    pid=pid,enabled=enabled,known=True,record_ordinal=ordinal)
    snapshots=[snap(2,'roi_begin',2,0,101,True,2),snap(4,'account_out',3,1,101,False,3),
               snap(6,'account_in',4,2,0,True,6),snap(8,'roi_end',6,4,0,True,10)]
    intervals=[]
    for a,b in zip(snapshots,snapshots[1:]):
        intervals.append(dict(start_tick=a['tick'],end_tick=b['tick'],start_cycle=a['cycle'],end_cycle=b['cycle'],
            active_cycles=b['active_counter']-a['active_counter'],start_sequence=a['sequence'],end_sequence=b['sequence'],
            pid=a['pid'],enabled=a['enabled'],known=a['known']))
    return dict(updates=[update(0,1,0,1,0,0,0,True),update(1,2,1,2,0,0),
                         update(3,3,2,2,0,1),update(5,4,2,2,1,1),update(7,5,2,2,2,1)],
                snapshots=snapshots,intervals=intervals,halted_pids=[],rejections=[],valid=True)

class TaskClockAuditTest(unittest.TestCase):
    def validate(self,data):
        from audit_task_clock import validate_native_counter
        return validate_native_counter(data,10)
    def test_independent_counter_and_unsampled_tail(self):
        result=self.validate(ledger())
        self.assertEqual(result['active_counter_delta'],4)
        self.assertEqual(result['sleep_cycles'],0)
    def test_snapshot_cannot_precede_an_earlier_clock_update_in_sequence(self):
        data=ledger()
        data['snapshots'][1].update(cycle=5,tick=50,active_counter=3)
        data['snapshots'][2].update(cycle=5,tick=50,active_counter=3)
        for actual,a,b in zip(data['intervals'],data['snapshots'],data['snapshots'][1:]):
            actual.update(start_tick=a['tick'],end_tick=b['tick'],start_cycle=a['cycle'],end_cycle=b['cycle'],
                active_cycles=b['active_counter']-a['active_counter'])
        with self.assertRaises(ValueError):self.validate(data)

    def test_tampered_totals_missing_updates_reorder_and_frequency_refuse(self):
        mutations=[lambda d:d['snapshots'][-1].update(active_counter=5),
                   lambda d:d['updates'].pop(3),lambda d:d['updates'].reverse(),
                   lambda d:d['updates'][2].update(active_delta=0),
                   lambda d:d['updates'][2].update(period=11),
                   lambda d:d['intervals'][0].update(active_cycles=100),
                   lambda d:d['snapshots'][1].update(sequence=3)]
        for mutation in mutations:
            data=copy.deepcopy(ledger());mutation(data)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.validate(data)
    def test_strict_cpi_refuses_equal_total_with_different_per_core_populations(self):
        from time_accounting import build_time_comparison
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_dual_time_accounting import documents
        simulation,oracle=documents()
        for row in (simulation['time_accounting'],oracle['time_accounting']):
            row.update(active_scope='workload-task',ownership_status='complete',task_ids=[101,202],
                accounting_contract='task-lifecycle-basecpu-active-v1',counter_domain='gem5-basecpu-active-cycles-v1',
                task_macro_instructions=10,per_core=[dict(core_id=0,task_macro_instructions=4),dict(core_id=1,task_macro_instructions=6)])
        self.assertEqual(build_time_comparison(simulation,oracle,oracle['time_accounting'])['cpi']['status'],'scored-active')
        original=copy.deepcopy(oracle['time_accounting']['per_core'])
        for bad in (None,[],[dict(core_id=0,task_macro_instructions=10),dict(core_id=0,task_macro_instructions=0)],
                    [dict(core_id=0,task_macro_instructions=True),dict(core_id=1,task_macro_instructions=9)],
                    [dict(core_id=1,task_macro_instructions=4),dict(core_id=2,task_macro_instructions=6)]):
            oracle['time_accounting']['per_core']=bad
            self.assertEqual(build_time_comparison(simulation,oracle,oracle['time_accounting'])['cpi']['status'],'unavailable')
        oracle['time_accounting']['per_core']=original
        oracle['time_accounting']['per_core'][0]['task_macro_instructions']=5
        oracle['time_accounting']['per_core'][1]['task_macro_instructions']=5
        comparison=build_time_comparison(simulation,oracle,oracle['time_accounting'])['cpi']
        self.assertEqual(comparison['status'],'unavailable')
        self.assertFalse(comparison['instruction_population_match'])

    def test_legacy_cpi_contract_is_rejected_and_denominator_mismatch_refuses(self):
        from time_accounting import _active_reason
        row=dict(schema='gem5-time-accounting-v1',scope='user-plus-kernel',active_scope='workload-task',
            active_status='available',active_cycles=4,unknown_cycles=0,task_ids=[101],ownership_status='complete',
            active_source='native',accounting_contract='task-oncpu-switch-entry-v1',task_macro_instructions=2)
        self.assertIsNotNone(_active_reason(row,row['schema'],row['scope'],2))

def capture_fixture(trace):
    import json,struct
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from test_task_clock_contract import clock_meta,rows
    from audit_task_clock import KERNEL_SHA256
    from time_accounting import TASK_ACCOUNTING_CONTRACT,TASK_COUNTER_DOMAIN
    events=rows();meta=clock_meta();meta.update(event_count=len(events),kernel_sha256=KERNEL_SHA256,kernel_profile_id='sha256:'+'a'*64)
    native=ledger();native['snapshots'][-1]['record_ordinal']=12
    native.update(schema='gem5-task-clock-native-v1',oracle_only=True,contract=TASK_ACCOUNTING_CONTRACT,
                  counter_domain=TASK_COUNTER_DOMAIN,kernel_sha256=KERNEL_SHA256,profile_id=meta['kernel_profile_id'],
                  **{k:meta[k] for k in ('core_id','source_file','source_record_count','warmup_records','measurement_records')})
    native['per_pid_macros']=[dict(pid=101,macro_instructions=2),dict(pid=0,macro_instructions=4)]
    native.update(active_cycles=1,other_active_cycles=2,transition_active_cycles=1,unknown_active_cycles=0,
                  task_macro_instructions=2,task_ids=[101],initial_known=True,initial_enabled=True,initial_pid=101,
                  common_cut_complete=True,common_start_tick=20,common_end_tick=60,clock_period_ticks=10,ticks_per_second=1000)
    raw=bytearray(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,12,128,0,0,0,0));record=struct.Struct('<QQQQ4IHHhBB4BI')
    for ordinal in range(12):
        e=next((e for e in events if e['record_ordinal']==ordinal),None)
        pc=e['pc'] if e else 4096+ordinal*4
        raw.extend(record.pack(pc,0,0,0,0,0,0,0,0,1|1024|2048 if ordinal==7 else 1,-2 if ordinal==7 else 0,0,0,255,255,255,255,0))
    value=14695981039346656037
    for byte in raw[72:]:value=((value^byte)*1099511628211)&0xffffffffffffffff
    native['source_stream_hash']=value
    hot=raw[72+7*64:72+8*64];bound=2166136261
    for byte in hot:bound=((bound^byte)*16777619)&0xffffffff
    fsem=struct.pack('<8sIIIIQQQII',b'FSTRETV1',2,56,0,1,12,1,value,16,0)+struct.pack('<QII',7,bound,1)
    (trace/meta['source_file']).write_bytes(raw);(trace/(meta['source_file']+'.fsem')).write_bytes(fsem)
    (trace/'thread-state-metadata-core0.json').write_text(json.dumps(meta))
    (trace/'thread-state-core0.jsonl').write_text('\n'.join(map(json.dumps,events)))
    oracle=trace/'oracle';oracle.mkdir()
    observations=[]
    for e,tick in zip(events,[0,10,30,35,40]):
        observations.append(dict(schema='fastsim-thread-state-observation-v1',oracle_only=True,
            **{k:e[k] for k in ('event_id','event_kind','core_id','record_ordinal')},commit_tick=tick))
    (oracle/'thread-state-observation.jsonl').write_text('\n'.join(map(json.dumps,observations)))
    clock=dict(schema='gem5-time-accounting-v1',oracle_only=True,core_id=0,scope='user-plus-kernel',active_scope='workload-task',
        active_status='proxy-scheduler-boundary',active_source='kernel-hook-observations',active_cycles=None,inactive_cycles=None,unknown_cycles=None,
        elapsed_status='available',elapsed_source='common-serial-marker-and-first-core-target',common_start_tick=20,common_end_tick=60,
        clock_period_ticks=10,ticks_per_second=1000,elapsed_reference_cycles=4,elapsed_time_seconds=0.04,reference_frequency_hz=100)
    (oracle/'time-accounting-core0.json').write_text(json.dumps(clock))
    (oracle/'task-clock-native-core0.json').write_text(json.dumps(native))
    return meta,native

class TaskClockCaptureAuditTest(unittest.TestCase):
    def test_full_capture_recounts_fsem_and_lifecycle_denominator(self):
        import tempfile
        from audit_thread_state_accounting import audit
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);capture_fixture(trace);result=audit(trace)
            self.assertEqual(result['active_status'],'available')
            self.assertEqual(result['active_cycles'],1)
            self.assertEqual(result['transition_cycles'],1)
            self.assertEqual(result['inactive_cycles'],2)
            self.assertEqual(result['task_macro_instructions'],2)
            self.assertEqual(result['active_cpi'],0.5)
            self.assertEqual(sum(row['macro_instructions'] for row in result['per_task']),2)
            self.assertEqual(result['recorded_owner_proxy']['task_macro_instructions'],3)
    def test_kernel_first_record_cannot_prove_initial_enabled_eligibility(self):
        import tempfile,struct
        from audit_thread_state_accounting import audit
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);meta,_=capture_fixture(trace)
            source=trace/meta['source_file'];raw=bytearray(source.read_bytes())
            struct.pack_into('<h',raw,72+52,-2);source.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'initial.*kernel|kernel.*initial'):
                audit(trace)
            with self.assertRaisesRegex(ValueError,'initial.*kernel|kernel.*initial'):
                compile_plan(trace,trace/'state.plan',require_sources=True)

    def test_full_capture_hash_count_and_lifecycle_tampering_refuse(self):
        import tempfile,json
        from audit_thread_state_accounting import audit
        mutations=[lambda d:d.update(active_cycles=123),lambda d:d.update(task_ids=[202]),
                   lambda d:d.update(source_stream_hash=1),
                   lambda d:d['per_pid_macros'][0].update(macro_instructions=3),
                   lambda d:d['snapshots'][1].update(pid=202),
                   lambda d:d['snapshots'][1].update(record_ordinal=4)]
        for mutate in mutations:
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);_,native=capture_fixture(trace);mutate(native)
                (trace/'oracle/task-clock-native-core0.json').write_text(json.dumps(native))
                with self.assertRaises(ValueError):audit(trace)
    def test_workload_already_asleep_at_roi_start_is_unavailable(self):
        import tempfile,json
        from audit_thread_state_accounting import audit
        from test_task_clock_contract import initial,identity
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);meta,native=capture_fixture(trace)
            events=[initial(0,101),identity(0,1,101)]
            meta['event_count']=2
            (trace/'thread-state-metadata-core0.json').write_text(json.dumps(meta))
            (trace/'thread-state-core0.jsonl').write_text('\n'.join(map(json.dumps,events)))
            observations=[dict(schema='fastsim-thread-state-observation-v1',oracle_only=True,
                **{k:e[k] for k in ('event_id','event_kind','core_id','record_ordinal')},commit_tick=tick)
                for e,tick in zip(events,[0,10])]
            (trace/'oracle/thread-state-observation.jsonl').write_text('\n'.join(map(json.dumps,observations)))
            update=native['updates'][0];update.update(sleeping=1,next_state=0)
            native['updates']=[update]
            begin=native['snapshots'][0];begin.update(sequence=1)
            end=native['snapshots'][-1];end.update(sequence=2,active_counter=0,pid=101)
            native['snapshots']=[begin,end]
            native['intervals']=[dict(start_tick=20,end_tick=60,start_cycle=2,end_cycle=6,
                start_sequence=1,end_sequence=2,active_cycles=0,pid=101,enabled=True,known=True)]
            native.update(active_cycles=0,other_active_cycles=0,transition_active_cycles=0,
                task_macro_instructions=9,per_pid_macros=[dict(pid=101,macro_instructions=9)],
                halted_pids=[101],rejections=['unsupported-workload-active-halt'],valid=False)
            (trace/'oracle/task-clock-native-core0.json').write_text(json.dumps(native))
            self.assertNotEqual(audit(trace)['active_status'],'available')
            # The first WAKEUP tick is also excluded by BaseCPU and unsupported
            # for a workload eligibility window spanning ROI begin.
            update.update(sleeping=0,next_state=1)
            (trace/'oracle/task-clock-native-core0.json').write_text(json.dumps(native))
            self.assertNotEqual(audit(trace)['active_status'],'available')

    def test_embedded_available_object_cannot_replace_current_native_evidence(self):
        import tempfile,json
        from audit_thread_state_accounting import audit
        from time_accounting import load_gem5_time_accounting,build_time_comparison
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);capture_fixture(trace);audited=audit(trace)
            (trace/'oracle/thread-state-accounting.json').write_text(json.dumps(audited))
            simulation=dict(measurement_scope='user-plus-kernel',scope_metrics={},
                time_accounting={**audited,'schema':'fastsim-time-accounting-v1'})
            for oracle in ({'time_accounting':audited},{'aggregate':{'time_accounting':audited}}):
                # The supplied current clock outranks an embedded availability claim.
                unavailable={**audited,'active_status':'unavailable-native-counter-ledger','active_cycles':None}
                self.assertEqual(build_time_comparison(simulation,oracle,unavailable)['cpi']['status'],'unavailable')
                self.assertEqual(build_time_comparison(simulation,oracle)['cpi']['status'],'unavailable')
                current=load_gem5_time_accounting(trace,oracle,cores=1)
                self.assertEqual(build_time_comparison(simulation,oracle,current)['cpi']['status'],'scored-active')
            copied={**audited,'active_scope':'cpu-unhalted'}
            current=load_gem5_time_accounting(trace,{'time_accounting':copied},cores=1)
            self.assertEqual(current['active_scope'],'workload-task')
            (trace/'oracle/task-clock-native-core0.json').unlink()
            for oracle in ({'time_accounting':audited},{'aggregate':{'time_accounting':audited}}):
                current=load_gem5_time_accounting(trace,oracle,cores=1)
                self.assertNotEqual(current['active_status'],'available')
                self.assertEqual(build_time_comparison(simulation,oracle,current)['cpi']['status'],'unavailable')

    def test_cached_audit_never_upgrades_changed_counter_ledger(self):
        import tempfile,json
        from audit_thread_state_accounting import audit
        from time_accounting import load_gem5_time_accounting
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);_,native=capture_fixture(trace);result=audit(trace)
            (trace/'oracle/thread-state-accounting.json').write_text(json.dumps(result))
            self.assertEqual(load_gem5_time_accounting(trace,{},cores=1)['active_status'],'available')
            native['snapshots'][-1]['active_counter']+=1
            (trace/'oracle/task-clock-native-core0.json').write_text(json.dumps(native))
            self.assertNotEqual(load_gem5_time_accounting(trace,{},cores=1)['active_status'],'available')

class TaskClockClosureTest(unittest.TestCase):
    def test_real_native_observer_roundtrip(self):
        import json,subprocess,tempfile
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_native_task_clock_producer import DRIVER,HEADER
        from audit_task_clock import validate_native_counter
        code=DRIVER.replace('#include <cassert>','#include <cassert>\n#include <iostream>')
        code=code.replace('std::ostringstream json;l.write_json(json);',
            'std::ostringstream json;l.write_json(json);std::cout<<"{"<<json.str()<<"}\\n";')
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'tmp') as directory:
            directory=Path(directory);source=directory/'clock.cpp';source.write_text(code);binary=directory/'clock'
            compiled=subprocess.run(['g++','-std=c++17','-static-libstdc++','-I'+str(HEADER.parent),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            emitted=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(emitted.returncode,0,emitted.stderr)
            result=validate_native_counter(json.loads(emitted.stdout),10)
            self.assertEqual(result['active_counter_delta'],4)

    def test_offedge_roi_uses_native_clocked_object_next_edge(self):
        from audit_task_clock import validate_native_counter
        data=ledger();data['snapshots'][-1]['tick']=51;data['intervals'][-1]['end_tick']=51
        self.assertEqual(validate_native_counter(data,10)['active_counter_delta'],4)

    def test_unsampled_sleep_tail_is_separate(self):
        from audit_task_clock import validate_native_counter
        data=ledger();update=data['updates'][-1]
        update.update(sleeping=1,next_state=0)
        data['snapshots'][-1]['active_counter']=3
        data['intervals'][-1]['active_cycles']=1
        result=validate_native_counter(data,10)
        self.assertEqual(result['sleep_cycles'],1)
        self.assertEqual(result['active_counter_delta'],3)

if __name__=='__main__':unittest.main()
