"""Lifecycle accounting must not promote ownership proxy spans."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_thread_state_graph import meta,certify,initial,identity,event,switch
from thread_state_graph import build_graph

CONTRACT='task-lifecycle-basecpu-active-v1'
DOMAIN='gem5-basecpu-active-cycles-v1'

def clock_meta():
    return {**certify(meta(0)), 'task_clock_contract':CONTRACT,
            'task_clock_counter_domain':DOMAIN,'task_clock_initial_known':True,
            'task_clock_initial_enabled':True,'task_clock_initial_pid':101,
            'task_clock_common_cut_complete':True}

def cut(ordinal,kind,prev,nxt):
    return {**event(0,ordinal,kind,prev_pid=prev,next_pid=nxt,
                 prev_task_id=prev+10000,next_task_id=nxt+10000),
                 'pc':{'account_out':0xffffffff81517a0a,'account_in':0xffffffff81067eef}[kind]}

def rows():
    return [initial(0,101),identity(0,1,101),cut(3,'account_out',101,0),
            switch(0,4,101,0,raw_state=0),cut(6,'account_in',101,0)]

def graph(events,metadata):
    metadata=copy.deepcopy(metadata);metadata['event_count']=len(events)
    return build_graph(events,[metadata])

class TaskClockContractTest(unittest.TestCase):
    def test_old_certificate_only_proves_owner(self):
        result=graph([initial(0,101),identity(0,1,101)],certify(meta(0)))
        self.assertNotEqual(result['summary']['active_status'],'available')
        self.assertEqual(result['summary']['ownership_status'],'complete')
        self.assertFalse(result['summary']['strict_active_scope'])
    def test_paired_lifecycle_and_initial_facts_are_functional_only(self):
        result=graph(rows(),clock_meta())
        self.assertEqual([e['kind'] for e in result['events']],[1,8,6,9])
        self.assertEqual(result['summary']['task_clock_contract'],CONTRACT)
        self.assertNotEqual(result['summary']['active_status'],'available')
        self.assertEqual(result['events'][1]['args'],[101,0,0])
        self.assertEqual(result['cores'][0]['task_clock_initial_pid'],101)
    def test_duplicate_or_mismatched_cuts_refuse(self):
        for bad in ([initial(0,101),identity(0,1,101),cut(3,'account_in',101,0)],
                    rows()+[cut(8,'account_in',101,0)],
                    [initial(0,101),identity(0,1,101),cut(3,'account_out',202,0)],
                    [initial(0,101),identity(0,1,101),cut(3,'account_out',101,101)]):
            with self.subTest(events=bad),self.assertRaises(ValueError):graph(bad,clock_meta())
    def test_uncertified_open_tail_is_not_synthetic_closure(self):
        metadata=clock_meta();metadata['task_clock_common_cut_complete']=False
        result=graph(rows()[:-1],metadata)
        self.assertFalse(result['cores'][0]['task_clock_common_cut_complete'])
    def test_timing_pollution_refuses(self):
        for field in ('task_clock_active_cycles','cycle','active_counter','clock_period_ticks','retire_tick'):
            metadata=clock_meta();metadata[field]=100
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'pollution'):graph(rows(),metadata)
    def test_initial_eligibility_must_match_initial_functional_owner(self):
        metadata=clock_meta();metadata['task_clock_initial_pid']=202
        with self.assertRaises(ValueError):graph([initial(0,101),identity(0,1,101)],metadata)

    def test_new_contract_requires_source_even_without_legacy_certificate(self):
        import tempfile,json
        from compile_thread_state import compile_plan
        from thread_state_graph import CERTIFICATE_FIELDS
        metadata=clock_meta()
        for field in CERTIFICATE_FIELDS:metadata.pop(field)
        events=rows();metadata['event_count']=len(events)
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory)
            (trace/'thread-state-metadata-core0.json').write_text(json.dumps(metadata))
            (trace/'thread-state-core0.jsonl').write_text('\n'.join(map(json.dumps,events)))
            with self.assertRaisesRegex(ValueError,'missing required FST'):
                compile_plan(trace,trace/'state.plan')

    def test_same_task_switch_needs_no_cuts(self):
        result=graph([initial(0,101),identity(0,1,101),switch(0,3,101,101,raw_state=0)],clock_meta())
        self.assertEqual(result['summary']['task_clock_cut_count'],0)

if __name__=='__main__':unittest.main()
