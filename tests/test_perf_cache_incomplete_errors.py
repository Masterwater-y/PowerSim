"""Incomplete evidence permits labeled errors, never an implicit source substitution."""
from copy import deepcopy
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import audit_perf_cache as adapter
import perf_cache_contract as contract
from test_perf_cache_audit import fixture
from test_perf_cache_estimates import estimated
from test_perf_cache_contract import metric


class IncompleteCacheErrorsTest(unittest.TestCase):
    def audit(self, trace):
        self.assertIn('allow_incomplete',inspect.signature(adapter.audit_perf_cache).parameters)
        return adapter.audit_perf_cache(trace,allow_incomplete=True)

    def compare(self, prediction, reference):
        self.assertIn('allow_incomplete_errors',inspect.signature(contract.compare_estimates).parameters)
        return contract.compare_estimates(prediction,reference,allow_incomplete_errors=True)

    def test_request_counts_survive_unavailable_retirement_population(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);path,row=fixture(trace)
            retpath=trace/'oracle/perf-retirement-core0.json';ret=json.loads(retpath.read_text())
            ret['coverage']['observed_leading_incomplete_macros']=1
            for scope in ret['scopes'].values():
                scope['available']=False
                for key in ('retired_macro_instructions','retired_uops','perf_retired_loads','perf_retired_stores'):
                    scope[key]=None
            retpath.write_text(json.dumps(ret))
            row['scopes']['physical']['request_primitives']['private_l2']['data_read']['tag_miss']=7
            row['scopes']['physical']['request_events'].update(perf_l2_accesses=7,perf_l2_demand_misses=7)
            path.write_text(json.dumps(row))
            with self.assertRaises(ValueError):adapter.audit_perf_cache(trace)
            audit=self.audit(trace)
            self.assertTrue(audit['valid'])
            self.assertFalse(audit['retirement_qualification_complete'])
            self.assertIn('leading_incomplete_macros',str(audit['evidence_gaps']))
            event=audit['scopes']['physical']['events']['perf_l2_demand_misses']
            self.assertEqual(event['diagnostic_value'],7)
            self.assertIsNone(event['value'])

    def test_abandoned_warmup_mismatch_is_retained_as_evidence_gap(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);fixture(trace)
            path=trace/'oracle/perf-retirement-core0.json';ret=json.loads(path.read_text())
            for scope in ('physical','user','workload_task'):
                ret['scopes'][scope]['coverage']['incomplete_macros']=2
                ret['scopes'][scope]['coverage']['incomplete_uops']=2
            path.write_text(json.dumps(ret))
            audit=self.audit(trace)
            self.assertIn('incomplete_macros',str(audit['evidence_gaps']))
            self.assertFalse(audit['retirement_qualification_complete'])

    def test_malformed_native_counts_bounds_or_source_still_fail(self):
        for fault in ('request_count','source','boundary','participant'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                trace=Path(directory);path,row=fixture(trace)
                if fault=='request_count':row['scopes']['physical']['request_events']['perf_l2_demand_misses']=9
                if fault=='source':row['evidence_source']='taotrace-path-class-v3'
                if fault=='boundary':row['measurement_begin_ordinal']+=1
                if fault=='participant':row['core_id']=8
                path.write_text(json.dumps(row))
                self.assertIn('allow_incomplete',inspect.signature(adapter.audit_perf_cache).parameters)
                with self.assertRaises(ValueError):adapter.audit_perf_cache(trace,allow_incomplete=True)

    def test_incomplete_reference_exposes_errors_and_separate_aggregate(self):
        f=estimated();g=metric('gem5',complete=False);name='perf_l1d_load_misses'
        f['events'][name].update(estimated_value=3,diagnostic_value=3)
        comparison=self.compare(f,g);row=comparison['events'][name]
        self.assertEqual(row['status'],'diagnostic_only')
        self.assertEqual(row['error_evidence'],'incomplete')
        self.assertEqual(row['absolute_percentage_error'],200)
        self.assertEqual(row['relative_error'],2)
        self.assertIn('reference_incomplete',row['reasons'])
        self.assertTrue(row['missing_reference_capabilities'])
        summary=contract.aggregate_estimates([comparison])['events'][name]
        self.assertEqual(summary['accuracy_cases'],0)
        self.assertIsNone(summary['accuracy']['raw']['mape_percent'])
        self.assertEqual(summary['incomplete_evidence_errors']['raw']['mape_percent'],200)
        self.assertEqual(summary['incomplete_evidence_errors']['raw']['wape_percent'],200)

    def test_zero_reference_is_explicit_not_infinite_or_dropped(self):
        f=estimated();g=metric('gem5',complete=False);name='perf_l1d_load_misses'
        g['events'][name].update(diagnostic_value=0)
        comparison=self.compare(f,g);row=comparison['events'][name]
        self.assertIsNone(row['absolute_percentage_error'])
        self.assertEqual(row['absolute_error'],1)
        summary=contract.aggregate_estimates([comparison])['events'][name]['incomplete_evidence_errors']['raw']
        self.assertEqual(summary['zero_reference_nonzero_prediction_cases'],1)

    def test_unknown_task_request_context_never_reports_zero_error(self):
        f=estimated();g=metric('gem5',complete=False)
        for m in (f,g):m['measurement']['scope']='workload_task'
        row=self.compare(f,g)['events']['perf_l2_demand_misses']
        self.assertEqual(row['status'],'unavailable')
        self.assertNotIn('absolute_percentage_error',row)


    def test_maintained_fingerprint_records_partial_policy_and_coverage(self):
        from types import SimpleNamespace
        import run_native_kernel_fastsim_validation as validator
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            root=Path(directory);case=root/'case';trace=case/'tao_trace';trace.mkdir(parents=True)
            fixture(trace);(case/'oracle').symlink_to(trace/'oracle',target_is_directory=True)
            for f in (case/'request.json',trace/'trace.json',trace/'manifest.txt',trace/'oracle/kernel_events.json',trace/'oracle/native-summary-core0.json'):
                if not f.exists():f.write_text('{}')
            (root/'binary').write_text('binary');(root/'config').write_text('config')
            args=SimpleNamespace(fastsim=root/'binary',config=root/'config',event_dictionary=validator.EVENT_DICTIONARY,
                                 perf_cache=True,perf_cache_incomplete_errors=True)
            retpath=trace/'oracle/perf-retirement-core0.json';ret=json.loads(retpath.read_text())
            ret['coverage']['observed_leading_incomplete_macros']=1;retpath.write_text(json.dumps(ret))
            try:value=validator.validation_fingerprint(dict(result_dir=str(case),cores=1),args)
            except ValueError as error:self.fail('maintained partial request path was aborted: '+str(error))
            self.assertTrue(value['perf_cache_incomplete_errors_requested'])
            self.assertFalse(value['current_perf_cache_evidence']['retirement_qualification_complete'])

    def test_maintained_summary_publishes_partial_error_table_and_gaps(self):
        from types import SimpleNamespace
        import run_native_kernel_fastsim_validation as validator
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            output=Path(directory);case=output/'cases/fixture';case.mkdir(parents=True)
            f=estimated();g=metric('gem5',complete=False);comparison=self.compare(f,g)
            comparison['events']['perf_l1d_load_misses']['evidence_gaps']=[dict(field='test-missing-boundary',observed=1,expected=0)]
            report=dict(schema=validator.CASE_SCHEMA,case_id='fixture',result_dir=str(output),cores=1,
                        workload='fixture',status='passed',errors=[],
                        perf_cache_validation=dict(estimated_scopes={'physical':comparison}))
            (case/'validation.json').write_text(json.dumps(report))
            args=SimpleNamespace(output_dir=output,expected_cases=1,max_attempts=3,perf_cache=True,
                                 perf_cache_estimates=True,perf_cache_incomplete_errors=True)
            validator.write_summary(args,[report])
            text=(output/'perf-cache-estimates.md').read_text()
            self.assertIn('Incomplete-evidence errors',text)
            self.assertIn('test-missing-boundary',text)
            self.assertIn('MAPE',text)


    def test_macro_denominator_mismatch_never_enters_complete_accuracy(self):
        f=estimated();g=metric('gem5');f['retired_macro_instructions']+=1
        for m in (f,g):m['measurement']['scope']='physical'
        row=self.compare(f,g)['events']['perf_l2_demand_misses']
        self.assertEqual(row['error_evidence'],'incomplete')
        self.assertEqual(row['status'],'diagnostic_only')
        self.assertIn('model_native_macro_population',str(row['evidence_gaps']))

    def test_request_evidence_gaps_prevent_strict_scoring(self):
        f=metric();g=metric('gem5');name='perf_l2_demand_misses'
        g['events'][name]['evidence_gaps']=[dict(field='cache_macro_population',observed=200,expected=100)]
        row=contract.compare_metrics(f,g)['events'][name]
        self.assertEqual(row['status'],'unavailable')

    def test_per_core_mismatches_cannot_cancel_in_aggregate(self):
        from test_perf_cache_validation import wrapper
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            trace=Path(directory);fixture(trace);audit=adapter.audit_perf_cache(trace)
            second=deepcopy(audit['per_core'][0]);second['core_id']=1
            for m in second['scopes'].values():m['measurement']['core_ids']=[1]
            audit['per_core'].append(second)
            dictionary=contract.load_dictionary()
            for scope in audit['scopes']:
                audit['scopes'][scope]=adapter.sum_metrics([c['scopes'][scope] for c in audit['per_core']],scope,dictionary)
            prediction=wrapper(audit)
            for out in (prediction,audit):
                for scopes in [out['scopes']]+[c['scopes'] for c in out['per_core']]:
                    e=scopes['physical']['events']['perf_l2_demand_misses']
                    e['coverage']=dict.fromkeys(e['coverage'],True);e['value']=e['diagnostic_value']
            prediction['per_core'][0]['scopes']['physical']['retired_macro_instructions']+=1
            prediction['per_core'][1]['scopes']['physical']['retired_macro_instructions']-=1
            row=adapter.compare_report(prediction,audit,allow_incomplete_errors=True)['scopes']['physical']['events']['perf_l2_demand_misses']
            self.assertEqual(row['status'],'unavailable')
            self.assertEqual(len(row['missing_reference_evidence']),2)

    def test_per_case_rate_error_and_unknown_request_evidence_are_exported(self):
        f=estimated();g=metric('gem5',complete=False);name='perf_l2_demand_misses'
        for m in (f,g):m['measurement']['scope']='physical'
        f['events'][name].update(estimated_value=3,diagnostic_value=3)
        g['events'][name]['request_unknown_diagnostics']=dict(observer_unknown_requests=7,unknown_primitive_lookups=4)
        row=self.compare(f,g)['events'][name]
        self.assertEqual(row['per_1k_inst_errors']['absolute_error'],20)
        self.assertEqual(row['per_1k_inst_errors']['absolute_percentage_error'],200)
        self.assertEqual(row['reference_request_unknown_diagnostics']['observer_unknown_requests'],7)


if __name__=='__main__':unittest.main()
