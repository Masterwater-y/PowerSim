"""Controlled real-hardware probe protocol; no simulator-accuracy inference."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
SPEC=importlib.util.find_spec('run_perf_cache_probe')
runner=__import__('run_perf_cache_probe') if SPEC else None


class PerfCacheProbeTest(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(runner,'maintained three-miss hardware probe is missing')

    def record(self):
        return dict(schema='hardware-perf-cache-probe-v1',case='load',iterations=1000,
                    working_set_bytes=4096,cpu=0,group=3,time_enabled=100,time_running=100,
                    counts=dict(perf_l1d_load_misses=7,perf_l2_demand_misses=2,
                                perf_llc_misses_core=1,instructions=5000))

    def test_retired_three_miss_group_has_distinct_units_and_names(self):
        row=self.record();row['group']=7
        row['counts']=dict(retired_l1_load_misses=7,retired_l2_load_misses=2,
                           retired_l3_load_misses=1,instructions=5000)
        runner.validate_record(row)
        summary=runner.summarize_records([row])
        self.assertEqual(summary['load-4096']['retired_l2_load_misses']['raw_median'],2)
        self.assertNotIn('perf_l2_demand_misses',summary['load-4096'])

    def test_c_retired_groups_bind_the_common_event_dictionary(self):
        from retired_cache_contract import EVENTS
        source=ROOT/'tests/workloads/perf_cache_probe.c'
        code='#define main unused_probe_main\n#include '+json.dumps(str(source))+'\n#undef main\n#include <assert.h>\nint main(void){\n'
        for group in range(6,10):
            for slot,name in enumerate(runner.GROUPS[group]):
                event=EVENTS[name];raw=event['event']|(event['umask']<<8)
                code+=f'assert(codes[{group}][{slot}]=={raw});assert(!strcmp(fields[{group}][{slot}],{json.dumps(name)}));\n'
        code+='return 0;}\n'
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=Path(directory);(path/'check.c').write_text(code)
            subprocess.run(['gcc','-O2','-std=gnu11',str(path/'check.c'),'-o',str(path/'check')],check=True,capture_output=True)
            subprocess.run([str(path/'check')],check=True,capture_output=True)

    def test_rejects_partial_scaled_or_mismatched_counter_records(self):
        for bad in ('running','missing','negative','boolean','iterations','group'):
            with self.subTest(bad=bad):
                row=self.record()
                if bad=='running':row['time_running']=99
                if bad=='missing':del row['counts']['perf_llc_misses_core']
                if bad=='negative':row['counts']['instructions']=-1
                if bad=='boolean':row['counts']['perf_l1d_load_misses']=True
                if bad=='iterations':row['iterations']=0
                if bad=='group':row['group']=99
                with self.assertRaises(ValueError):runner.validate_record(row)

    def test_group_retains_own_macro_denominator_and_raw_counts(self):
        row=self.record();runner.validate_record(row)
        summary=runner.summarize_records([row])
        item=summary['load-4096']['perf_l1d_load_misses']
        self.assertEqual(item['raw_median'],7)
        self.assertEqual(item['per_1k_inst_median'],1.4)
        self.assertEqual(item['per_iteration_median'],.007)
        self.assertEqual(item['samples'],1)
        self.assertFalse(summary['load-4096']['hardware_equivalence_certified'])

    def test_runner_keeps_working_set_and_counter_group_identity(self):
        a=self.record();b=self.record();b['working_set_bytes']=262144
        summary=runner.summarize_records([a,b])
        # Without an explicit label, capacity still prevents pooling.
        self.assertEqual(len(summary),2)

    def test_writeback_direction_control_cannot_replace_the_target_event(self):
        row=self.record();row['group']=5
        row['counts']=dict(perf_l2_writebacks=5,diagnostic_l2_lines_out_non_silent=0,
                           diagnostic_l2_lines_in=10,instructions=5000)
        try:runner.validate_record(row)
        except ValueError as error:self.fail(f'writeback direction control unavailable: {error}')
        del row['counts']['perf_l2_writebacks']
        row['counts']['perf_l2_demand_misses']=5
        with self.assertRaises(ValueError):runner.validate_record(row)

    def test_source_compiles_and_rejects_invalid_probe_without_counting(self):
        source=ROOT/'tests/workloads/perf_cache_probe.c'
        self.assertTrue(source.is_file(),'missing controlled cache request workload')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            binary=Path(directory)/'probe'
            subprocess.run(['gcc','-O2','-std=gnu11','-Wall','-Wextra',str(source),'-o',str(binary)],check=True,capture_output=True)
            result=subprocess.run([str(binary),'unknown','100','4096','3','0'],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,'')


if __name__=='__main__':unittest.main()
