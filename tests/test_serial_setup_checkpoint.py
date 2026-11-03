"""Exercise the actual instrumented runtime across a quiescent setup boundary."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / 'workloads/spec_roi_integration/fastsim_serial_setup_checkpoint.h'
RUNTIME = ROOT / 'tmp/thread-state-active-elapsed/runtime/libgomp.a'

PROGRAM = r'''
#include "fastsim_serial_setup_checkpoint.h"
#include <assert.h>
static int crossed, before, after;
static unsigned long long main_runtime, main_tid, main_tgid;
void fastsim_wait_marker(unsigned long long kind, unsigned long long runtime,
                         unsigned long long tid, unsigned long long request,
                         unsigned long long object, unsigned long long tgid) {
  if (kind != 1) return;
  assert(runtime && tid && tgid && !request && !object);
  if (tid == (unsigned long long)getpid()) {
    if (crossed) {
      assert(runtime == main_runtime && tid == main_tid && tgid == main_tgid);
    } else {
      main_runtime = runtime; main_tid = tid; main_tgid = tgid;
    }
  }
  if (crossed) __atomic_add_fetch(&after, 1, __ATOMIC_RELAXED);
  else __atomic_add_fetch(&before, 1, __ATOMIC_RELAXED);
}
static void boundary(const char *name, unsigned id) { crossed = 1; }
int main(int argc, char **argv) {
  int requested = fastsim_serial_setup_begin();
  assert(requested == 4 && omp_get_max_threads() == 1);
  int members = 0;
  #pragma omp parallel reduction(+:members)
  members += 1;
  assert(members == 1);
  if (argc > 1 && argv[1][0] == 'w') {
    omp_set_num_threads(4);
    #pragma omp parallel
    { __asm__ volatile ("" ::: "memory"); }
  }
  if (argc > 1 && argv[1][0] == 'n') {
    #pragma omp parallel num_threads(1)
    fastsim_serial_setup_checkpoint(boundary,"nested-invalid",1,requested);
  } else {
    fastsim_serial_setup_checkpoint(boundary,"safe-boundary",1,requested);
  }
  assert(crossed && before == 1 && after == 1);
  assert(omp_get_max_threads() == 4);
  members = 0;
  #pragma omp parallel reduction(+:members)
  members += 1;
  assert(members == 4);
  assert(after == 4);
}
'''


class SerialSetupCheckpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HEADER.exists():
            raise AssertionError('quiescent serial-setup checkpoint helper missing')
        if not RUNTIME.exists():
            raise unittest.SkipTest('instrumented libgomp not built')
        cls.directory = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        path = Path(cls.directory.name)
        source = path / 'probe.c'
        source.write_text(PROGRAM)
        cls.binary = path / 'probe'
        subprocess.run(['/opt/gcc-11/bin/gcc', '-std=c17', '-D_GNU_SOURCE',
                        '-Werror=implicit-function-declaration', '-O2', '-fopenmp',
                        '-I'+str(HEADER.parent), str(source), '-static', '-no-pie',
                        str(RUNTIME), '-pthread', '-ldl', '-o', str(cls.binary)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_probe(self, *arguments):
        env = dict(os.environ, OMP_NUM_THREADS='4', OMP_DYNAMIC='FALSE',
                   OMP_WAIT_POLICY='PASSIVE')
        env.pop('OMP_PLACES', None)
        env.pop('OMP_PROC_BIND', None)
        return subprocess.run([str(self.binary), *arguments], env=env,
                              capture_output=True, text=True, timeout=15)

    def test_serial_setup_then_new_four_thread_team_and_identity(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('os_tasks=1', result.stdout)

    def test_existing_worker_pool_is_rejected(self):
        result = self.run_probe('workers')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires exactly one OS task', result.stderr)

    def test_serialized_parallel_region_is_rejected(self):
        result = self.run_probe('nested')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside every parallel region', result.stderr)


if __name__ == '__main__':
    unittest.main()
