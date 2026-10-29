"""Independent cache estimates survive a failed native task-population gate."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import run_native_kernel_fastsim_validation as validator
from test_native_kernel_pmu_validation import prediction
import test_p1_native_response_sideband as native_fixture
from test_p0_contract import row
from merge_kernel_events_oracle_v3 import merge


class CacheReportingOrderTest(unittest.TestCase):
    def test_independent_retired_cache_kept_and_task_gate_stays_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=native_fixture.NativeResponseSidebandTest().make_result_summary(root)
            reference=row();reference['user_plus_kernel_retired_instructions']=3
            reference['perf_like_cpi_user_plus_kernel']=10/3
            reference['pmu_user_plus_kernel'].update(retired_instructions=3,retired_uops=5)
            reference['pmu_kernel_by_class']['scheduler'].update(retired_instructions=1,retired_uops=1)
            (result/'oracle/kernel_events.json').write_text(json.dumps(merge([reference])))
            request=json.loads((result/'request.json').read_text())
            request.update(sampling={'functional_include_kernel':True},profile={'cores':1})
            (result/'request.json').write_text(json.dumps(request))
            trace=result/'tao_trace';trace.mkdir();(trace/'manifest.txt').write_text('fixture\n')
            (trace/'trace.json').write_text(json.dumps(dict(trace_scope='user-plus-kernel',
                functional_boundaries={'0':dict(measurement_user_records=4,measurement_records=5,
                    measurement_user_instructions=2,measurement_instructions=3)})))
            pmu=prediction(branch=0,l1d=0,l2=0,llc=0)
            pmu.update(retired_uops=5,retired_instructions=3,llc_tag_accesses=0)
            simulation=dict(configuration=dict(cores=1,native_kernel_trace=True),
                functional_source_identity={'source_identity':'same-input'},
                scope_metrics=dict(pmu_source='fastsim-functional-native-user-plus-kernel-v1',
                    pmu_contract_id=validator.PMU_CONTRACT_ID,pmu=pmu,
                    user_trace_uops=4,native_kernel_trace_uops=1,user_trace_instructions=2,
                    native_kernel_trace_instructions=1,perf_like_cpi=4.0,
                    task_pmu={'status':'available'},retired_cache={}),
                totals=dict(native_kernel_trace=dict(records=1,retired_instructions=1,memory_uops=1),
                    syscall_uops=0,llc_outcomes_conserved=True,
                    **{name:{'events':0} for name in ['synthetic_syscall_kernel',
                        'synthetic_page_fault_kernel','synthetic_irq_kernel','synthetic_kernel_total']}),cha=[])
            stats=root/'stats';stats.mkdir();(stats/'fixture.json').write_text(json.dumps(simulation))
            args=SimpleNamespace(output_dir=root/'reports',stats_dir=stats,fastsim=None,config=None,
                event_dictionary=validator.EVENT_DICTIONARY,repo_root=ROOT,retired_cache=True)
            case=dict(case_id='fixture',cores=1,workload='fixture',result_dir=str(result))
            comparison={'scopes':{'workload_task':{'events':{'retired_l1_load_misses':
                dict(reference=10,predicted=12,status='incomplete-evidence')}}}}
            fingerprint={'sha256':'fixture','current_retired_cache_evidence':{'scopes':{}}}
            with patch.object(validator,'validation_fingerprint',return_value=fingerprint), \
                 patch.object(validator,'audit_task_pmu',side_effect=ValueError('task population mismatch')), \
                 patch('perf_cache_source.collect_source_identity',return_value={'source_identity':'same-input'}), \
                 patch('perf_cache_source.validate_identity'), \
                 patch.object(validator,'compare_retired_cache',return_value=comparison):
                report=validator.run_case(case,args)
            self.assertEqual(report['status'],'failed')
            self.assertIn('task population mismatch',' '.join(report['errors']))
            self.assertEqual(report['pmu_validation']['status'],'source-gate-failed')
            self.assertEqual(report.get('retired_cache_validation'),comparison)
            self.assertNotIn('pmu',report)

            # Preserving an independent namespace never permits a wrong input.
            with patch.object(validator,'validation_fingerprint',return_value=fingerprint), \
                 patch('perf_cache_source.collect_source_identity',return_value={'source_identity':'different'}), \
                 patch('perf_cache_source.validate_identity',side_effect=ValueError('cache input mismatch')), \
                 patch.object(validator,'compare_retired_cache') as compare:
                wrong=validator.run_case(case,args)
            self.assertEqual(wrong['status'],'failed')
            self.assertIn('cache input mismatch',' '.join(wrong['errors']))
            self.assertNotIn('retired_cache_validation',wrong)
            compare.assert_not_called()

            args.retired_cache=False
            args.perf_cache=args.perf_cache_estimates=args.perf_cache_incomplete_errors=True
            with patch.object(validator,'validation_fingerprint',return_value=fingerprint), \
                 patch.object(validator,'audit_task_pmu',side_effect=ValueError('task population mismatch')), \
                 patch.object(validator,'audit_perf_cache',return_value={'retirement_qualification_complete':False}), \
                 patch.object(validator,'compare_perf_cache_report',return_value=comparison):
                requests=validator.run_case(case,args)
            self.assertEqual(requests['status'],'failed')
            self.assertEqual(requests['pmu_validation']['status'],'source-gate-failed')
            self.assertEqual(requests.get('perf_cache_validation'),comparison)


if __name__=='__main__':
    unittest.main()
