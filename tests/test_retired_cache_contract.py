import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))


class RetiredCacheContract(unittest.TestCase):
    def test_native_adapter_reuses_checked_functional_population(self):
        from test_perf_cache_audit import fixture
        from audit_perf_cache import audit_perf_cache,audit_request_population
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'tmp') as directory:
            trace=Path(directory);fixture(trace)
            audited=audit_perf_cache(trace,allow_incomplete=True)
            scanned=audit_request_population(trace)
            populations=audited['functional_retirement_populations']
            for core in scanned['per_core']:
                for scope,row in core['scopes'].items():
                    for key in ('retired_macro_instructions','retired_uops'):
                        self.assertEqual(populations[str(core['core_id'])][scope][key],row[key])

    def fixture(self):
        import retired_cache_contract as c
        row=dict(retired_macro_instructions=10,retired_uops=20,incomplete_macros=0,
                 warmup_spanning_macros=0,scope_crossing_macros=0,
                 events={k:dict(count=0,unknown=0,estimate_available=True,event=v['event'],umask=v['umask']) for k,v in c.EVENTS.items()})
        row['events']['retired_loads']['count']=5
        row['events']['retired_l2_load_misses']['count']=2
        x=dict(schema='retired-load-cache-report-v1',hardware_equivalence='unverified',
               source='accepted-model-retirement-service-facts',unit='architectural-retired-macro-instruction',
               scopes={s:copy.deepcopy(row) for s in c.SCOPES},
               per_core=[dict(core_id=0,scopes={s:copy.deepcopy(row) for s in c.SCOPES})])
        y=copy.deepcopy(x);y.update(schema='retired-load-cache-reference-v1',source='native-retirement-service-facts',
                                    input_identity='sha256:test',task_ids=[7],evidence_gaps=[])
        x['input_identity']=y['input_identity'];x['task_ids']=[7]
        return c,x,y

    def test_retired_miss_raw_and_unknown_remain_visible(self):
        c,x,y=self.fixture()
        for row in [y['scopes']['workload_task'],y['per_core'][0]['scopes']['workload_task']]:
            row['events']['retired_l2_load_misses'].update(count=1,unknown=3)
        v=c.compare_retired_cache(x,y)['scopes']['workload_task']['events']['retired_l2_load_misses']
        self.assertEqual((v['predicted'],v['reference'],v['absolute_error'],v['ape_percent']),(2,1,1,100))
        self.assertEqual(v['reference_unknown'],3)
        self.assertEqual(v['status'],'incomplete-evidence')

    def test_per_core_population_and_sum_are_required(self):
        c,x,y=self.fixture();x['per_core'][0]['scopes']['physical']['retired_macro_instructions']=11
        with self.assertRaises(ValueError):c.compare_retired_cache(x,y)

    def test_missing_new_namespace_does_not_use_old_request_counter(self):
        c,x,y=self.fixture();del x['scopes']['physical']['events']['retired_l2_load_misses']
        with self.assertRaises(ValueError):c.compare_retired_cache(x,y)

    def test_zero_reference_keeps_absolute_difference(self):
        c,x,y=self.fixture()
        for row in [y['scopes']['physical'],y['per_core'][0]['scopes']['physical']]:
            row['events']['retired_l2_load_misses']['count']=0
        v=c.compare_retired_cache(x,y)['scopes']['physical']['events']['retired_l2_load_misses']
        self.assertEqual(v['absolute_error'],2);self.assertIsNone(v['ape_percent'])

    def test_wrong_input_or_tasks_rejected(self):
        c,x,y=self.fixture();x['input_identity']='other'
        with self.assertRaises(ValueError):c.compare_retired_cache(x,y)
        x['input_identity']=y['input_identity'];x['task_ids']=[8]
        with self.assertRaises(ValueError):c.compare_retired_cache(x,y)

    def test_raw_isa_unknown_is_subset_of_functional_load_count(self):
        c,x,y=self.fixture()
        for row in [y['scopes']['physical'],y['per_core'][0]['scopes']['physical']]:
            row['events']['retired_loads']['unknown']=2
        v=c.compare_retired_cache(x,y)['scopes']['physical']['events']['retired_loads']
        self.assertEqual(v['reference'],5)
        self.assertEqual(v['reference_unknown'],2)


if __name__=='__main__':unittest.main()
