"""Matched per-case CPI endpoints still must not be pooled across policies."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from run_native_kernel_fastsim_validation import CASE_SCHEMA,write_summary

class EndPolicyAggregationTest(unittest.TestCase):
 def test_two_valid_end_policies_keep_separate_mae_without_combined_accuracy(self):
  with tempfile.TemporaryDirectory() as d:
   output=Path(d);discovered=[]
   for i,policy in enumerate(['common-end-v1','retired-stream-completion-v1']):
    case=f'case{i}';directory=output/'cases'/case;directory.mkdir(parents=True)
    report=dict(schema=CASE_SCHEMA,case_id=case,cores=1,workload='probe',result_dir='/'+case,
     status='passed',attempts=1,errors=[],trace_counts={'kernel_uops':1,'kernel_instructions':1},
     cpi={'status':'scored-active','fastsim':1.2+i,'gem5':1.0,'absolute_relative_error':0.2+i,'active_end_policy':policy},
     pmu_validation={'status':'unavailable'},pmu={})
    (directory/'validation.json').write_text(json.dumps(report));discovered.append({'case_id':case,'result_dir':'/'+case})
   summary=write_summary(SimpleNamespace(output_dir=output,expected_cases=2,max_attempts=1),discovered)
   self.assertIsNone(summary['cpi_mae'])
   self.assertIsNone(summary['mean_absolute_cpi_relative_error'])
   self.assertIsNone(summary['max_absolute_cpi_relative_error'])
   self.assertEqual(summary['active_cpi_accuracy']['cases'],0)
   groups=summary['active_cpi_accuracy_by_end_policy']
   self.assertAlmostEqual(groups['common-end-v1']['cpi_mae'],0.2)
   self.assertAlmostEqual(groups['retired-stream-completion-v1']['cpi_mae'],1.2)
   self.assertIn('mixed',summary['cpi_accuracy_status'])
