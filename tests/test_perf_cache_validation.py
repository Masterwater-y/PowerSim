"""Maintained validator integration for explicitly requested ten-field PMUs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from test_perf_cache_audit import fixture
from test_perf_cache_contract import metric
import audit_perf_cache as adapter
import perf_cache_contract as contract
import run_native_kernel_fastsim_validation as validator


def wrapper(audit):
    out=deepcopy(audit);out['schema']='fastsim-perf-cache-report-v2'
    for scopes in [out['scopes']]+[r['scopes'] for r in out['per_core']]:
        for scope,m in scopes.items():
            m.update(schema='fastsim-perf-cache-v2',evidence_source='model-self-generated-events',oracle_only=False)
    return out


class PerfCacheValidationTest(unittest.TestCase):
    def test_estimates_require_verified_content_and_native_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace)
            manifest=trace/'manifest.txt'
            manifest.write_text('0 binary-warmup-slice core0.records.micro.fst 0 2 5 2 6\n')
            audit=adapter.audit_perf_cache(trace,include_input_identity=True)
            pred=wrapper(audit);pred['input_identity']=deepcopy(audit['input_identity'])
            for scopes in [pred['scopes']]+[r['scopes'] for r in pred['per_core']]:
                for m in scopes.values():
                    for r in m['events'].values():
                        r.update(estimated_value=r['diagnostic_value'],estimate_status='approximate',
                            estimate_policy='accepted-model-events-zero-unmodeled-v1',assumptions=['unmodeled contribution zero'],
                            missing_components=[k for k,v in r['coverage'].items() if not v])
            result=adapter.compare_report(pred,audit,include_estimates=True)
            self.assertEqual(result['estimated_scopes']['physical']['events']['perf_l1d_load_misses']['status'],'diagnostic_only')
            summary=adapter.aggregate_report_estimates([dict(status='passed',perf_cache_validation=result)])
            self.assertEqual(summary['physical']['events']['perf_l1d_load_misses']['diagnostic_cases'],1)
            self.assertEqual(summary['physical']['events']['perf_l1d_load_misses']['accuracy_cases'],0)
            output=trace/'report';report_dir=output/'cases/fixture';report_dir.mkdir(parents=True)
            report=dict(schema=validator.CASE_SCHEMA,case_id='fixture',result_dir=str(trace),cores=1,
                        workload='fixture',status='passed',errors=[],perf_cache_validation=result)
            (report_dir/'validation.json').write_text(json.dumps(report))
            args=SimpleNamespace(output_dir=output,expected_cases=1,max_attempts=3,perf_cache=True,perf_cache_estimates=True)
            validator.write_summary(args,[report])
            self.assertIn('Diagnostic differences',(output/'perf-cache-estimates.md').read_text())
            pred['input_identity']['source_identity']='sha256:'+'0'*64
            with self.assertRaisesRegex(ValueError,'identity|digest'):
                adapter.compare_report(pred,audit,include_estimates=True)
            manifest.write_text('0 binary-warmup-slice core0.records.micro.fst 0 1 6 1 7\n')
            with self.assertRaisesRegex(ValueError,'bound|window|ordinal'):
                adapter.audit_perf_cache(trace,include_input_identity=True)

    def test_per_core_estimate_cannot_hide_aggregate_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace);audit=adapter.audit_perf_cache(trace)
            pred=wrapper(audit)
            for scopes in [pred['scopes']]+[r['scopes'] for r in pred['per_core']]:
                for m in scopes.values():
                    for r in m['events'].values():
                        r.update(estimated_value=r['diagnostic_value'],estimate_status='approximate',
                            estimate_policy='accepted-model-events-zero-unmodeled-v1',assumptions=['unmodeled contribution zero'],
                            missing_components=[k for k,v in r['coverage'].items() if not v])
            pred['per_core'][0]['scopes']['physical']['events']['perf_l2_demand_misses']['estimated_value']=9
            with self.assertRaises(ValueError):adapter.compare_report(pred,audit)

    def test_requested_mode_cannot_reuse_a_legacy_report_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=root/'case';trace=case/'tao_trace';trace.mkdir(parents=True)
            fixture(trace);(case/'oracle').symlink_to(trace/'oracle',target_is_directory=True)
            for f in (case/'request.json',trace/'trace.json',trace/'manifest.txt',trace/'oracle/kernel_events.json',trace/'oracle/native-summary-core0.json'):
                if not f.exists():f.write_text('{}')
            (root/'binary').write_text('binary');(root/'config').write_text('config')
            args=SimpleNamespace(fastsim=root/'binary',config=root/'config',event_dictionary=validator.EVENT_DICTIONARY,perf_cache=False)
            row=dict(result_dir=str(case),cores=1)
            old=validator.validation_fingerprint(row,args)
            args.perf_cache=True;new=validator.validation_fingerprint(row,args)
            self.assertNotEqual(old['sha256'],new['sha256'])
            self.assertTrue(new['perf_cache_requested'])
            self.assertIn('current_perf_cache_evidence',new)
            (trace/'oracle/perf-cache-core0.json').unlink()
            with self.assertRaisesRegex(ValueError,'sidecar|participant'):validator.validation_fingerprint(row,args)

    def test_wrapper_per_core_counts_and_source_participants_are_independent(self):
        self.assertTrue(hasattr(adapter,'compare_report'),'maintained v2 wrapper comparison is missing')
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace);audit=adapter.audit_perf_cache(trace)
            pred=wrapper(audit);result=adapter.compare_report(pred,audit)
            self.assertEqual(result['schema'],'perf-cache-validation-v2')
            self.assertEqual(result['scopes']['user']['events']['perf_l1d_load_misses']['status'],'unavailable')
            pred['scopes']['physical']['events']['perf_l2_accesses']['diagnostic_value']=1
            with self.assertRaisesRegex(ValueError,'sum|conserv|core'):adapter.compare_report(pred,audit)

    def test_each_child_scope_window_task_and_native_macro_population_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace);audit=adapter.audit_perf_cache(trace)
            for key,value in [('scope','kernel'),('window_id','wrong'),('task_ids',[999])]:
                with self.subTest(key=key):
                    pred=wrapper(audit)
                    pred['per_core'][0]['scopes']['user']['measurement'][key]=value
                    with self.assertRaisesRegex(ValueError,'scope|identity|window|task'):
                        adapter.compare_report(pred,audit)
            second=deepcopy(audit['per_core'][0]);second['core_id']=1
            for m in second['scopes'].values():m['measurement']['core_ids']=[1]
            audit['per_core'].append(second)
            audit['scopes']={name:adapter.sum_metrics([c['scopes'][name] for c in audit['per_core']],name,contract.load_dictionary()) for name in audit['scopes']}
            pred=wrapper(audit)
            pred['per_core'][0]['scopes']['user']['retired_macro_instructions']+=1
            pred['per_core'][1]['scopes']['user']['retired_macro_instructions']-=1
            with self.assertRaisesRegex(ValueError,'core|population'):
                adapter.compare_report(pred,audit)

    def test_cache_only_fingerprint_tracks_non_event_fst_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=root/'case';trace=case/'tao_trace';trace.mkdir(parents=True)
            fixture(trace);(case/'oracle').symlink_to(trace/'oracle',target_is_directory=True)
            for f in (case/'request.json',trace/'trace.json',trace/'manifest.txt',trace/'oracle/kernel_events.json',trace/'oracle/native-summary-core0.json'):
                if not f.exists():f.write_text('{}')
            (root/'binary').write_text('binary');(root/'config').write_text('config')
            args=SimpleNamespace(fastsim=root/'binary',config=root/'config',event_dictionary=validator.EVENT_DICTIONARY,perf_cache=True)
            row=dict(result_dir=str(case),cores=1)
            before=validator.validation_fingerprint(row,args)
            source=trace/'core0.records.micro.fst';data=bytearray(source.read_bytes());data[72+64*4+8]^=1;source.write_bytes(data)
            after=validator.validation_fingerprint(row,args)
            self.assertNotEqual(before['sha256'],after['sha256'])

    def test_summary_has_requested_fields_mape_max_ape_and_no_native_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);trace=root/'trace';trace.mkdir();fixture(trace)
            audit=adapter.audit_perf_cache(trace);pred=wrapper(audit)
            comparison=adapter.compare_report(pred,audit)
            output=root/'output';case=output/'cases/fixture';case.mkdir(parents=True)
            report=dict(schema=validator.CASE_SCHEMA,case_id='fixture',result_dir=str(trace),cores=1,
                        workload='fixture',status='passed',errors=[],perf_cache_validation=comparison)
            (case/'validation.json').write_text(json.dumps(report))
            args=SimpleNamespace(output_dir=output,expected_cases=1,max_attempts=3,perf_cache=True)
            summary=validator.write_summary(args,[report])
            md=(output/'perf-cache-summary.md').read_text()
            for f in contract.BINDINGS:self.assertIn(f,md)
            for t in ('MAPE','max APE','unavailable','hardware','raw','per_1k_inst'):self.assertIn(t,md)
            self.assertIn('perf_cache_accuracy_by_scope',summary)


if __name__=='__main__':unittest.main()
