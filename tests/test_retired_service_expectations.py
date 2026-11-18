import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from test_retired_cache_contract import RetiredCacheContract

class ServiceExpectations(unittest.TestCase):
    def fixture(self):
        c,model,reference=RetiredCacheContract().fixture();scale=65536
        population=dict(physical=5,user=3,kernel=2,workload_task=4,other_task=1)
        for report in [model,reference]:
            for block in [report,*report['per_core']]:
                for scope,n in population.items():
                    row=block['scopes'][scope];row['retired_macro_instructions']=2*n;row['retired_uops']=3*n
                    row['events']['retired_loads']['count']=n;row['events']['retired_l2_load_misses']['count']=min(2,n)
        reference['membership_policy']=c.SPR_MISS_POLICY
        for report in [model,reference]:
            for block in [report,*report['per_core']]:
                for row in block['scopes'].values():
                    n=row['events']['retired_loads']['count']
                    for name in c.MISS_EVENTS:
                        e=row['events'][name];count=e['count']
                        if report is model:
                            e.update(estimated_count=count,estimate_policy=c.SPR_MISS_POLICY,
                                estimate_fallback_loads=0,estimated_merged_loads=0,
                                prediction_nonmember=n-count,prediction_unknown=0,prediction_exact_count=count,
                                prediction_estimated_count=0,prediction_exact_nonmember=n-count,prediction_estimated_nonmember=0)
                    if report is reference:
                        for name,e in row['events'].items():
                            if name in ('retired_loads','retired_stores'):continue
                            count=e['count'];e.update(nonmember=n-count,exact_count=0,estimated_count=count,
                                exact_nonmember=0,estimated_nonmember=n-count,reasons={'R-MIXED-FRAGMENT':n})
        e=dict(schema='retired-service-expectation-v1',available=True,model='bounded-v1',
               unit='architectural-retired-macro-instruction',hardware_equivalence='unverified',
               scale=scale,input_identity=model['input_identity'],task_ids=[7],timing_feedback=False,
               synthetic_requests=False,owner_coverage='complete',semantic_policy=c.SPR_MISS_POLICY,scopes={})
        for scope,n in population.items():
            e['scopes'][scope]=dict(retired_macro_instructions=2*n,retired_uops=3*n,eligible_loads=n,
                unknown_source_loads=0,clock_unavailable_loads=0,correlation_uncertain_loads=0,fallback_loads=0,
                events={k:dict(expected_q16=n*scale//4,lower_q16=n*scale//4,upper_q16=n*scale//4,
                               expected_count=n/4) for k in c.MISS_EVENTS})
        e['per_core']=[dict(core_id=0,scopes=copy.deepcopy(e['scopes']))]
        return c,model,reference,e
    def test_fractional_counts_keep_native_unit_and_identity_gates(self):
        c,m,r,e=self.fixture();v=c.compare_retired_cache_expectations(m,e,r)
        row=v['scopes']['physical']['events']['retired_l2_load_misses']
        self.assertEqual(row['predicted'],1.25);self.assertEqual(row['relative_error'],-.375)
        wrong=copy.deepcopy(e);wrong['input_identity']='wrong'
        with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,wrong,r)
    def test_population_coverage_and_per_core_sums_fail_closed(self):
        c,m,r,e=self.fixture()
        for target in ['eligible_loads','retired_macro_instructions','unknown_source_loads']:
            bad=copy.deepcopy(e);bad['per_core'][0]['scopes']['workload_task'][target]+=1
            with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,bad,r)
        bad=copy.deepcopy(e);bad['per_core'][0]['scopes']['physical']['events'][c.MISS_EVENTS[0]]['expected_q16']+=1
        with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,bad,r)
    def test_unknown_never_silently_becomes_hit_or_zero(self):
        c,m,r,e=self.fixture()
        for block in [r,*r['per_core']]:
            row=block['scopes']['workload_task']['events'][c.MISS_EVENTS[1]]
            row['unknown']=1;row['nonmember']-=1;row['estimated_nonmember']-=1
            row['reasons']['R-MIXED-FRAGMENT']-=1;row['reasons']['U-BOUNDARY']=1
        with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,e,r)
        v=c.compare_retired_cache_expectations(m,e,r,small_native_unknown_limit=2)
        row=v['scopes']['workload_task']['events'][c.MISS_EVENTS[1]]
        self.assertEqual(row['reference_unknown'],1);self.assertEqual(row['status'],'small-unknown-provisional')
    def test_absent_legacy_or_mixed_semantic_policy_rejects(self):
        c,m,r,e=self.fixture()
        for policy in [None,'retired-load-cache-membership-v1']:
            bad=copy.deepcopy(r)
            if policy is None:bad.pop('membership_policy')
            else:bad['membership_policy']=policy
            with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,e,bad)
        bad=copy.deepcopy(e);bad['semantic_policy']='old-policy'
        with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,bad,r)
    def test_nonfinite_out_of_bounds_or_non_nested_expectation_rejects(self):
        c,m,r,e=self.fixture()
        for key,value in [('expected_count',float('nan')),('expected_q16',2**70),('lower_q16',2**70)]:
            bad=copy.deepcopy(e);bad['scopes']['workload_task']['events'][c.MISS_EVENTS[1]][key]=value
            with self.assertRaises(ValueError):c.compare_retired_cache_expectations(m,bad,r)
    def test_zero_reference_keeps_absolute_error(self):
        c,m,r,e=self.fixture();v=c.compare_retired_cache_expectations(m,e,r)
        row=v['scopes']['physical']['events'][c.MISS_EVENTS[2]]
        self.assertEqual(row['absolute_error'],1.25);self.assertIsNone(row['relative_error'])
    def test_native_validator_explicit_expectation_mode(self):
        from unittest.mock import patch
        import run_native_kernel_fastsim_validation as validator
        argv=['validator','--validation-mode','strict','--source-root','tmp/source','--stats-dir','tmp/stats',
              '--output-dir','tmp/report','--retired-service-expectations','--retired-service-native-unknown-limit','2']
        with patch.object(sys,'argv',argv):args=validator.parse_args()
        self.assertTrue(args.retired_cache and args.retired_service_expectations)
        self.assertEqual(args.retired_service_native_unknown_limit,2)
        self.assertFalse(args.retired_cache_misses)

if __name__=='__main__':unittest.main()
