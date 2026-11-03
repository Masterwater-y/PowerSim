"""Approximate predictions are useful without pretending the reference is complete."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from test_perf_cache_contract import metric
import perf_cache_contract as contract


def estimated():
    m=metric(complete=False)
    for row in m['events'].values():
        row.update(estimated_value=row['diagnostic_value'],estimate_status='approximate',
                   estimate_policy='accepted-model-events-zero-unmodeled-v1',
                   assumptions=['unmodeled requests contribute zero; no claim of a bound'],
                   missing_components=list(row['coverage']))
    return m


class PerfCacheEstimateTest(unittest.TestCase):
    def test_incomplete_model_can_be_scored_against_complete_reference(self):
        f=estimated();g=metric('gem5');name='perf_l2_demand_misses'
        for m in (f,g):m['measurement']['scope']='physical'
        f['events'][name].update(estimated_value=3,diagnostic_value=3)
        compared=contract.compare_estimates(f,g)
        self.assertEqual(compared['events'][name]['status'],'scored_estimate')
        self.assertEqual(compared['events'][name]['predicted'],3)
        self.assertEqual(compared['events'][name]['reference'],1)
        self.assertEqual(compared['hardware_equivalence'],'unverified')
        self.assertIsNone(contract.validate_metric(f,'fastsim')['events'][name]['value'])

    def test_incomplete_reference_is_diagnostic_difference_not_accuracy(self):
        f=estimated();g=metric('gem5',complete=False);name='perf_l1d_load_misses'
        f['events'][name].update(estimated_value=3,diagnostic_value=3)
        row=contract.compare_estimates(f,g)['events'][name]
        self.assertEqual(row['status'],'diagnostic_only')
        self.assertEqual(row['signed_difference'],2)
        self.assertNotIn('absolute_percentage_error',row)
        result=contract.aggregate_estimates([contract.compare_estimates(f,g)])['events'][name]
        self.assertEqual(result['accuracy_cases'],0)
        self.assertIsNone(result['accuracy']['raw']['mape_percent'])
        self.assertEqual(result['diagnostic_cases'],1)
        self.assertEqual(result['diagnostic_differences']['raw']['mean_absolute_percentage_difference'],200)

    def test_estimate_metadata_is_validated_independently(self):
        for fault in ('boolean','missing_assumption','missing_components','wrong_policy','over_macro','unknown_component'):
            with self.subTest(fault=fault):
                f=estimated();r=f['events']['perf_l1d_load_misses']
                if fault=='boolean':r['estimated_value']=True
                if fault=='missing_assumption':r['assumptions']=[]
                if fault=='missing_components':r['missing_components']=[]
                if fault=='wrong_policy':r['estimate_policy']='copy-native-count'
                if fault=='over_macro':r['estimated_value']=101
                if fault=='unknown_component':r['missing_components'].append('pretend')
                with self.assertRaises(ValueError):contract.validate_metric(f,'fastsim')

    def test_source_scope_context_and_window_cannot_be_waived_by_estimates(self):
        for fault in ('source','scope','context','window'):
            with self.subTest(fault=fault):
                f=estimated();g=metric('gem5')
                if fault=='source':g['evidence_source']='taotrace-path-class-v3'
                else:g['measurement'][{'scope':'scope','context':'context_policy','window':'window_id'}[fault]]='wrong'
                with self.assertRaises(ValueError):contract.compare_estimates(f,g)

    def test_source_identity_allows_only_explicit_window_approximation(self):
        f=estimated();g=metric('gem5');f['measurement']['window_id']='accepted-model-window-v1'
        for m in (f,g):m['measurement']['scope']='physical'
        c=contract.compare_estimates(f,g,input_identity_verified=True)
        self.assertEqual(c['events']['perf_l2_demand_misses']['status'],'diagnostic_only')
        self.assertEqual(c['window_status'],'source-record-window-approximation')
        f['measurement']['task_ids']=[999]
        with self.assertRaises(ValueError):contract.compare_estimates(f,g,input_identity_verified=True)

    def test_unknown_request_context_is_not_a_zero_task_result(self):
        f=estimated();g=metric('gem5',complete=False)
        for m in (f,g):m['measurement']['scope']='workload_task'
        r=contract.compare_estimates(f,g)['events']['perf_l2_demand_misses']
        self.assertEqual(r['status'],'unavailable')
        self.assertIn('event_context',r['reasons'])

    def test_legacy_output_without_estimates_remains_unavailable(self):
        r=contract.compare_estimates(metric(complete=False),metric('gem5'))
        self.assertEqual(r['events']['perf_l2_demand_misses']['status'],'unavailable')

    def test_aggregate_does_not_mix_scopes_or_reference_types(self):
        a=contract.compare_estimates(estimated(),metric('gem5'))
        b=deepcopy(a);b['reference_producer']='hardware'
        with self.assertRaises(ValueError):contract.aggregate_estimates([a,b])


if __name__=='__main__':unittest.main()
