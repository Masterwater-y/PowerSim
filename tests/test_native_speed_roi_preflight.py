"""Host admission exercises real native gates, without running a benchmark."""
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT/'tools/preflight_native_speed_roi.py'
HEADERS = ROOT/'workloads/spec_roi_integration'


class NativeSpeedPreflightTest(unittest.TestCase):
    def test_real_native_process_admits_all_tasks_and_is_stopped(self):
        self.assertTrue(TOOL.exists(), 'host native admission checker is missing')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            cpus = sorted(os.sched_getaffinity(0))[:2]
            (dest/'probe.cpp').write_text(r'''
#include <thread>
#include <vector>
extern "C" {
#include "fastsim_native_roi.h"
}
void work() { fastsim_native_roi_ready("real-compute"); sleep(20); }
int main() {
  if (access("seed.txt", R_OK) || getenv("OMP_NUM_THREADS")) return 9;
  fastsim_native_pin_worker();
  std::vector<std::thread> workers;
  for (int i=0; i<2; ++i) workers.emplace_back(work);
  work();
  for (auto &worker:workers) worker.join();
}
''')
            for compiler, source, output in (
                ('gcc', HEADERS/'fastsim_rate_launcher.c', dest/'launcher'),
                ('g++', dest/'probe.cpp', dest/'probe'),
            ):
                subprocess.run([compiler, '-D_GNU_SOURCE', '-O2', '-pthread',
                    '-I'+str(HEADERS), str(source), '-o', str(output)],
                    check=True, capture_output=True)
            with tarfile.open(dest/'input.tar', 'w') as archive:
                info = tarfile.TarInfo('seed.txt'); info.size = 5
                archive.addfile(info, io.BytesIO(b'input'))
            case = dict(name='probe', case_id='c02-probe', cores=len(cpus),
                native_workers=3, binary=str(dest/'probe'), args='',
                input_archive=str(dest/'input.tar'), launcher=str(dest/'launcher'),
                host_cpus=sorted(os.sched_getaffinity(0))[:3], native_omp=False,
                native_warmup_after_identities=True)
            (dest/'case.json').write_text(json.dumps(case))
            result = subprocess.run([sys.executable, str(TOOL), '--case-json',
                str(dest/'case.json'), '--output', str(dest/'output'),
                '--wall-seconds', '10'], capture_output=True, text=True, timeout=15,
                env=dict(os.environ, OMP_NUM_THREADS='99'))
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            receipt = json.loads((dest/'output/admission.json').read_text())
            self.assertTrue(receipt['valid'])
            self.assertEqual(receipt['environment']['FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES'], '1')
            self.assertEqual(receipt['effective_host_cpus'], cpus)
            self.assertEqual(len(receipt['ready_workers']), 3)
            self.assertEqual(len(receipt['census_task_ids']), 3)
            self.assertEqual(receipt['peak_swap_bytes'], 0)
            self.assertGreater(receipt['peak_rss_bytes'], 0)
            self.assertLess(receipt['wall_seconds'], 10)
            self.assertFalse(Path('/proc/'+str(receipt['pid'])).exists())
            self.assertEqual(set(receipt['input_hashes']), {'binary', 'launcher', 'input_archive'})
            spec = importlib.util.spec_from_file_location('native_preflight', TOOL)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            case['native_warmup_after_identities'] = False
            (dest/'case.json').write_text(json.dumps(case))
            with patch.dict(os.environ, FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES='stale'):
                rejected = module.run(dest/'case.json', dest/'rss-rejected', 10, rss_limit=1)
            self.assertIsNone(rejected['environment']['FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES'])
            self.assertFalse(rejected['valid'])
            self.assertIn('RSS limit exceeded', rejected['reason'])
            self.assertFalse(Path('/proc/'+str(rejected['pid'])).exists())

    def test_identity_ack_does_not_hide_missing_ready_worker(self):
        self.assertTrue(TOOL.exists(), 'host native admission checker is missing')
        spec = importlib.util.spec_from_file_location('native_preflight', TOOL)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        text = ('[fastsim-rate] post-switch-census begin\n'
                '[fastsim-rate] copy=0 pid=100 cpu=0 VmRSS: 100 kB VmSwap: 0 kB Threads: 2\n'
                '[fastsim-rate] copy=0 task_tid=100 task_tgid=100\n'
                '[fastsim-rate] copy=0 task_tid=101 task_tgid=100\n'
                '[fastsim-rate] post-switch-census end\n'
                '[fastsim-native] identities-ready workers=2\n')
        with self.assertRaisesRegex(ValueError, 'ready worker identity'):
            module.admitted_tasks(text,
                '[fastsim-native] ready worker=0 tid=100 cpu=0 phase=compute\n',
                dict(native_workers=2, cores=2, host_cpus=[0, 1]))

    def test_escaping_archive_is_rejected_before_start(self):
        self.assertTrue(TOOL.exists(), 'host native admission checker is missing')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            with tarfile.open(dest/'input.tar', 'w') as archive:
                info = tarfile.TarInfo('../escaped.txt'); info.size = 4
                archive.addfile(info, io.BytesIO(b'evil'))
            case = dict(name='bad', case_id='c01-bad', cores=1, native_workers=1,
                binary='/bin/true', launcher='/bin/true', args='',
                input_archive=str(dest/'input.tar'),
                host_cpus=sorted(os.sched_getaffinity(0))[:1], native_omp=False)
            (dest/'case.json').write_text(json.dumps(case))
            result = subprocess.run([sys.executable, str(TOOL), '--case-json',
                str(dest/'case.json'), '--output', str(dest/'output')],
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((dest/'escaped.txt').exists())
            receipt = json.loads((dest/'output/admission.json').read_text())
            self.assertFalse(receipt['valid'])
            self.assertIsNone(receipt['pid'])


if __name__ == '__main__':
    unittest.main()
