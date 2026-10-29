import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('report',Path(__file__).resolve().parents[1]/'tools/compare_store_demand_latency.py')
report=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

class StoreDemandReportTests(unittest.TestCase):
    def fixture(self,path,duplicate=False,omit=False):
        lines=[]
        for group,count,mean in [('latency',3,10),('hit_latency',2,1),('miss_latency',1,28)]:
            if omit and group=='miss_latency':continue
            key='board.cache_hierarchy.ruby_system.RequestType.ST.'+group+'_hist_seqr'
            lines += [key+'::samples '+str(count),key+'::mean '+str(mean),key+'::bucket_size 4',
                      key+' | '+str(count)+' 100.0% 100.0% | '+' | '.join(['0 0% 100%']*9)]
        if duplicate:lines+=lines
        path.write_text('\n'.join(lines))

    def test_native_means_are_not_bucket_midpoints(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'stats.txt';self.fixture(p)
            result=report.read_gem5_st(p)
            self.assertEqual(result['miss']['mean_cycles'],28)
            self.assertEqual(result['hit']['samples'],2)

    def test_missing_and_duplicate_stats_fail_closed(self):
        for options in [dict(duplicate=True),dict(omit=True)]:
            with self.subTest(options=options),tempfile.TemporaryDirectory() as d:
                p=Path(d)/'stats.txt';self.fixture(p,**options)
                with self.assertRaises(ValueError):report.read_gem5_st(p)

    def test_align_to_common_gem5_bucket_width(self):
        left=dict(bucket_size=1,buckets=[1]*10)
        right=dict(bucket_size=4,buckets=[2]*10)
        aligned=report.align_histograms(left,right)
        self.assertEqual(aligned['bucket_size'],4)
        self.assertEqual(aligned['fastsim'],[4,4,2,0,0,0,0,0,0,0])
        self.assertEqual(aligned['gem5'],[2]*10)

if __name__=='__main__':unittest.main()
