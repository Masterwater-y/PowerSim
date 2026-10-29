"""Compile real C gates: no post-admission getenv or repeated XZ call site."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ROOT / 'workloads/spec_roi_integration'


class NativeRoiOnceTest(unittest.TestCase):
    def compile(self, dest, name, source, *flags):
        path = dest / (name + '.c')
        path.write_text(source)
        subprocess.run(['gcc', '-D_GNU_SOURCE', '-O2', '-pthread',
                        '-I' + str(HEADERS), str(path), *flags,
                        '-o', str(dest / name)], check=True, capture_output=True)

    def test_entered_real_c_gate_does_no_environment_lookups_or_second_handshake(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            dest = Path(directory)
            self.compile(dest, 'probe', r'''
#include <pthread.h>
#include "fastsim_native_roi.h"
static __thread unsigned lookups;
extern char *__real_getenv(const char *);
char *__wrap_getenv(const char *key) { ++lookups; return __real_getenv(key); }
static void *worker(void *unused) {
    fastsim_native_roi_ready("first");
    unsigned initial_lookups = lookups;
    lookups = 0;
    for (int i = 0; i < 1000; ++i) fastsim_native_roi_ready("again");
    printf("tid=%ld cpu=%d initial=%u repeated=%u\n",
           syscall(SYS_gettid), sched_getcpu(), initial_lookups, lookups);
    return NULL;
}
int main(void) {
    fastsim_native_pin_worker();
    pthread_t threads[4];
    for (int i = 0; i < 4; ++i) if (pthread_create(&threads[i], NULL, worker, NULL)) return 2;
    worker(NULL);
    for (int i = 0; i < 4; ++i) pthread_join(threads[i], NULL);
    return 0;
}
''', '-Wl,--wrap=getenv')
            subprocess.run(['gcc', '-D_GNU_SOURCE', '-O2',
                            '-I' + str(HEADERS), str(HEADERS / 'fastsim_rate_launcher.c'),
                            '-o', str(dest / 'launcher')], check=True, capture_output=True)
            cpus = sorted(os.sched_getaffinity(0))[:4]
            run = subprocess.run([str(dest / 'launcher'), '--cpus', ','.join(map(str, cpus)),
                                  '--native-workers', '5', '--', str(dest / 'probe')],
                                 cwd=dest, env=dict(os.environ, GEM5_DISABLE_ROI_MARKER='1'),
                                 capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            rows = [dict(item.split('=') for item in line.split())
                    for line in (dest / 'copy-0.stdout').read_text().splitlines()]
            self.assertEqual(len({row['tid'] for row in rows}), 5)
            self.assertEqual({int(row['cpu']) for row in rows}, set(cpus))
            self.assertEqual(sum(int(row['cpu']) == cpus[0] for row in rows), 2)
            self.assertTrue(all(int(row['initial']) > 0 for row in rows))
            self.assertEqual({row['repeated'] for row in rows}, {'0'},
                             'an admitted native task still calls getenv')
            self.assertEqual((dest / 'copy-0.stderr').read_text().count('ready worker='), 5)
            self.assertIn('identities-ready workers=5', run.stdout)

    def test_xz_shipped_call_sites_are_once_per_task_across_blocks_and_keep_gates(self):
        # Compile the added statements from the shipped patch, so changing a
        # test-only facsimile cannot hide a repeated call in production.
        hunks = (ROOT / 'patches/spec2026-xz_s-roi.patch').read_text().split('@@')[2::2]
        added = ['\n'.join(line[1:] for line in h.splitlines()
                           if line.startswith('+') and not line.startswith('+++')) for h in hunks]
        declarations = '\n'.join(line for chunk in added for line in chunk.splitlines()
                                 if line.startswith('static __thread'))
        worker = next(chunk for chunk in added if 'xz-worker-first' in chunk)
        producer = next(chunk for chunk in added if 'xz-producer-final' in chunk)
        source = r'''
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>
static __thread int worker_calls, producer_calls;
static int failures;
static void fastsim_native_roi_ready(const char *name) {
  if (strstr(name, "xz-worker")) ++worker_calls; else ++producer_calls;
}
''' + declarations + '\nstatic void worker_step(size_t in_pos) {\n' + worker + r'''
}
struct coder { unsigned threads_initialized, threads_max; };
static void producer_step(unsigned initialized, size_t thr_in_size) {
  struct coder state = {initialized, 4}, *coder = &state;
''' + producer + r'''
}
static void *task(void *unused) {
  worker_step(0); worker_step(16383);
  producer_step(3, 16384); producer_step(4, 16383);
  if (worker_calls || producer_calls) __sync_fetch_and_add(&failures, 1);
  for (int block = 0; block < 3; ++block) {
    for (int chunk = 1; chunk <= 8; ++chunk) {
      worker_step(chunk * 16384);
      producer_step(4, chunk * 16384);
    }
  }
  if (worker_calls != 1 || producer_calls != 1) {
    fprintf(stderr, "worker=%d producer=%d expected one each\n", worker_calls, producer_calls);
    __sync_fetch_and_add(&failures, 1);
  }
  return NULL;
}
int main(void) {
  pthread_t threads[4];
  for (int i = 0; i < 4; ++i) pthread_create(&threads[i], NULL, task, NULL);
  task(NULL);
  for (int i = 0; i < 4; ++i) pthread_join(threads[i], NULL);
  return failures != 0;
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            dest = Path(directory)
            self.compile(dest, 'call-sites', source)
            run = subprocess.run([str(dest / 'call-sites')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
