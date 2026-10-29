"""Three miss estimates retain independent native evidence and accepted macros."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))


class RetiredCacheMissEstimates(unittest.TestCase):
    def test_production_shadow_and_macro_retirement(self):
        code = r'''
#include "fastsim/retired_cache_miss_estimator.hpp"
#include "fastsim/perf_retirement.hpp"
#include <cassert>
int main() {
 using S=fastsim::RetiredCacheMissEstimator;
 S s(1);
 auto primary=s.observe(64,10,30,7);
 assert(primary.misses==7 && !primary.merged);
 auto follower=s.observe(64,20,35,7);
 assert(follower.misses==0 && follower.merged);
 assert(s.observe(64,30,50,3).misses==3); // half-open response edge
 assert(s.observe(64,5,15,1).fallback); // no future parent attachment
 assert(s.observe(128,31,60,7).fallback); // bounded collision is explicit
 assert(s.observe(128,32,61,7,true).misses==0); // forwarding issues no request
 assert(s.observe(192,40,80,7,false,true).misses==0); // modeled alias
 assert(s.observe(256,50,50,1).fallback); // invalid/empty window
 S collision(1);
 collision.observe(64,10,100,7);
 assert(collision.observe(128,20,30,7).fallback);
 assert(collision.observe(64,40,100,7).fallback); // displaced parent still live
 fastsim::PerfRetirementLedger ledger;
 using F=fastsim::PerfRetirementLedger;
 ledger.accept(false,true,false,false,true,true,F::Unknown,F::Unknown,primary.misses);
 ledger.begin_measurement();
 ledger.accept(true,true,false,false,true,true,F::L1,F::L1,0);
 auto r=ledger.snapshot().workload_task;
 assert(r.estimated_l1_misses==1 && r.estimated_l2_misses==1 && r.estimated_l3_misses==1);
 assert(r.l1_miss_qualified==0 && r.l1_miss_unknown==1); // evidence unchanged
 ledger.accept(false,true,false,false,true,true,F::Miss,F::Miss,7);
 assert(ledger.snapshot().workload_task.estimated_l3_misses==1); // incomplete tail
 ledger.accept(true,true,false,false,true,true,F::L1,F::L1,0,true,true);
 r=ledger.snapshot().workload_task;
 assert(r.estimated_l3_misses==2 && r.estimated_miss_fallback_loads==1);
 assert(r.estimated_merged_loads==1 && r.perf_retired_loads==2);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            source=Path(directory)/'check.cc';exe=Path(directory)/'check'
            source.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++',
                '-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            result=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_three_estimates_score_without_fb_accuracy(self):
        from test_retired_cache_contract import RetiredCacheContract
        c,model,reference=RetiredCacheContract().fixture()
        names=('retired_l1_load_misses','retired_l2_load_misses','retired_l3_load_misses')
        for block in [model,*model['per_core']]:
            for scope in block['scopes'].values():
                scope['events']['retired_load_fb_hits']['count']=4
                for name in names:
                    scope['events'][name].update(estimated_count=3,estimate_policy='bounded-line-fill-v1',
                        estimate_fallback_loads=1,estimated_merged_loads=1)
        report=c.compare_retired_cache(model,reference,misses_only=True)
        self.assertEqual(set(report['scopes']['workload_task']['events']),set(names))
        row=report['scopes']['workload_task']['events']['retired_l2_load_misses']
        self.assertEqual((row['predicted'],row['model_known_count'],row['reference']),(3,2,2))
        self.assertEqual(row['estimate_fallback_loads'],1)
        self.assertEqual(row['status'],'incomplete-evidence')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            c.write_retired_cache_summary(Path(directory),[dict(case_id='fixture',retired_cache_validation=report)])
            text=(Path(directory)/'retired-cache-summary.md').read_text()
            self.assertNotIn('retired_load_fb_hits',text)
            self.assertIn('retired_l3_load_misses',text)

    def test_estimate_gates_still_reject_population_and_missing_estimates(self):
        from test_retired_cache_contract import RetiredCacheContract
        c,model,reference=RetiredCacheContract().fixture()
        with self.assertRaises(ValueError):c.compare_retired_cache(model,reference,misses_only=True)
        for block in [model,*model['per_core']]:
            for scope in block['scopes'].values():
                for name in ('retired_l1_load_misses','retired_l2_load_misses','retired_l3_load_misses'):
                    scope['events'][name].update(estimated_count=3,estimate_policy='bounded-line-fill-v1',
                        estimate_fallback_loads=0,estimated_merged_loads=0)
        wrong=copy.deepcopy(model);wrong['input_identity']='wrong'
        with self.assertRaises(ValueError):c.compare_retired_cache(wrong,reference,misses_only=True)
        wrong=copy.deepcopy(model);wrong['per_core'][0]['scopes']['physical']['events']['retired_l1_load_misses']['estimated_count']=4
        with self.assertRaises(ValueError):c.compare_retired_cache(wrong,reference,misses_only=True)
        wrong=copy.deepcopy(reference)
        for block in [wrong,*wrong['per_core']]:block['scopes']['physical']['retired_macro_instructions']+=1
        with self.assertRaises(ValueError):c.compare_retired_cache(model,wrong,misses_only=True)

    def test_cli_selects_three_misses_and_native_retired_audit(self):
        from unittest.mock import patch
        import run_native_kernel_fastsim_validation as validator
        with patch.object(sys,'argv',['validator','--source-root','tmp/source',
            '--stats-dir','tmp/stats','--output-dir','tmp/output','--retired-cache-misses']):
            args=validator.parse_args()
        self.assertTrue(args.retired_cache and args.retired_cache_misses)

    def test_primary_misses_require_zero_native_unknown(self):
        from test_retired_cache_contract import RetiredCacheContract
        c,model,reference=RetiredCacheContract().fixture()
        for block in [model,*model['per_core']]:
            for scope in block['scopes'].values():
                for name in c.MISS_EVENTS:
                    scope['events'][name].update(estimated_count=0,estimate_policy='bounded-line-fill-v1',
                        estimate_fallback_loads=0,estimated_merged_loads=0)
        for field in (*c.MISS_EVENTS,'retired_loads'):
            bad=copy.deepcopy(reference)
            for block in [bad,*bad['per_core']]:
                block['scopes']['workload_task']['events'][field]['unknown']=1
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'native.*unknown'):
                c.compare_retired_cache(model,bad,misses_only=True)
        bad=copy.deepcopy(reference);bad['evidence_gaps']=[{'field':'retirement_population_available','observed':False}]
        with self.assertRaisesRegex(ValueError,'native.*coverage'):
            c.compare_retired_cache(model,bad,misses_only=True)

    def test_primary_requires_same_eligible_load_population(self):
        from test_retired_cache_contract import RetiredCacheContract
        c,model,reference=RetiredCacheContract().fixture()
        for block in [model,*model['per_core']]:
            for row in block['scopes'].values():
                for name in c.MISS_EVENTS:
                    row['events'][name].update(estimated_count=0,estimate_policy='bounded-line-fill-v1',
                        estimate_fallback_loads=0,estimated_merged_loads=0)
        for block in [model,*model['per_core']]:
            block['scopes']['workload_task']['events']['retired_loads']['count']=6
        with self.assertRaisesRegex(ValueError,'eligible.*load.*population'):
            c.compare_retired_cache(model,reference,misses_only=True)


if __name__=='__main__':unittest.main()
