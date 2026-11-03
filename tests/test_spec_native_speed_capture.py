"""Native speed workers share one process and reach the detailed gate together."""
import ast
import base64
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


def runner_functions(*names):
    tree = ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(os=os, base64=base64, shlex=shlex)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<runner>', 'exec'), ns)
    return ns


class NativeSpeedCaptureTest(unittest.TestCase):
    def test_native_census_and_terminal_drain_finish_before_detailed_handoff(self):
        # Moving the native handoff ahead of /proc or dropping tcdrain must
        # fail: neither census work nor queued UART bytes belong in warmup.
        cpus = sorted(os.sched_getaffinity(0))[:4]
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            (dest/'probe.cpp').write_text(r'''
#include <thread>
#include <vector>
extern "C" {
#include "fastsim_native_roi.h"
}
void work() { fastsim_native_roi_ready("compute"); }
int main() {
  fastsim_native_pin_worker();
  std::vector<std::thread> workers;
  for (int i=0; i<4; ++i) workers.emplace_back(work);
  work();
  for (auto &worker: workers) worker.join();
}
''')
            (dest/'boundary.c').write_text(r'''
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
int __wrap_nanosleep(const struct timespec *req, struct timespec *rem) {
  static int count;
  printf("[test] handoff=%d\n", ++count); return 0;
}
int __wrap_isatty(int fd) { return 1; }
int __wrap_tcdrain(int fd) {
  static int count;
  ++count;
  if (getenv("TEST_DRAIN_ERROR")) { errno=EIO; return -1; }
  if (getenv("TEST_DRAIN_EINTR") && count==1) { errno=EINTR; return -1; }
  printf("[test] serial-drained attempts=%d\n", count); return 0;
}
''')
            include = '-I'+str(ROOT/'workloads/spec_roi_integration')
            subprocess.run(['gcc', '-O2', include,
                str(ROOT/'workloads/spec_roi_integration/fastsim_rate_launcher.c'),
                str(dest/'boundary.c'), '-Wl,--wrap=nanosleep',
                '-Wl,--wrap=isatty', '-Wl,--wrap=tcdrain', '-o', str(dest/'launcher')],
                check=True, capture_output=True)
            subprocess.run(['g++', '-D_GNU_SOURCE', '-O2', '-pthread', include,
                str(dest/'probe.cpp'), '-o', str(dest/'probe')],
                check=True, capture_output=True)
            for mode, attempts in [('', 1), ('TEST_DRAIN_EINTR', 2), ('TEST_DRAIN_ERROR', 0)]:
                with self.subTest(mode=mode):
                    environment = dict(os.environ, GEM5_DISABLE_ROI_MARKER='1')
                    if mode: environment[mode] = '1'
                    result = subprocess.run([str(dest/'launcher'), '--cpus',
                        ','.join(map(str, cpus)), '--native-workers', '5', '--', str(dest/'probe')],
                        cwd=dest, env=environment, capture_output=True, text=True, timeout=15)
                    if not attempts:
                        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                        self.assertNotIn('[test] handoff=2', result.stdout)
                        self.assertNotIn('compute-gate-release', result.stdout)
                        continue
                    self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                    markers = ['post-switch-census end',
                               '[test] serial-drained attempts='+str(attempts),
                               '[test] handoff=2', 'compute-gate-release',
                               'identities-ready workers=5']
                    for marker in markers: self.assertIn(marker, result.stdout)
                    positions = [result.stdout.index(marker) for marker in markers]
                    self.assertEqual(positions, sorted(positions), result.stdout)
                    self.assertEqual(result.stdout.count('task_tid='), 5)

    def test_native_worker_count_is_distinct_from_process_count(self):
        validate = runner_functions('validate_rate_options')['validate_rate_options']
        args = SimpleNamespace(rate_copies=1, native_workers=4, omp_threads=4,
            extra_cores=0, rate_launcher=Path('launcher'), housekeeping_cpu=0,
            functional_sync=False, omp_proc_bind='', omp_places='', omp_wait_policy='',
            warmup_insts=1000000, checkpoint_only=False,
            save_boot_checkpoint=Path('checkpoint'), restore_boot_checkpoint=None)
        validate(args)
        args.native_workers = 5
        validate(args)  # XZ: four compression workers plus the active main.
        for copies, workers in [(2, 4), (1, 3), (1, -1), (1, 1025)]:
            args.rate_copies, args.native_workers = copies, workers
            with self.assertRaises(ValueError):
                validate(args)

    def test_guest_starts_one_native_process_without_openmp_environment(self):
        ns = runner_functions('_b64_heredoc', 'boot_script')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            binary = Path(directory)/'binary'; binary.write_bytes(b'test')
            args = SimpleNamespace(workload_binary=str(binary), input_file=[],
                rate_copies=1, native_workers=4, omp_threads=4, rate_launcher=str(binary),
                omp_proc_bind='', omp_places='', omp_wait_policy='',
                skip_workload_validation=False, warmup_steps=None,
                workload_args='--cpu-threads 4', rate_private_cwd=True)
            script = ns['boot_script'](args)
            self.assertIn('--cpus 0,1,2,3 --native-workers 4 --private-cwd -- /tmp/run/workload --cpu-threads 4', script)
            self.assertNotIn('OMP_NUM_THREADS', script)
            args.native_omp_num_threads = True
            script = ns['boot_script'](args)
            self.assertIn('OMP_NUM_THREADS=4 /tmp/run/rate-launcher', script)
            args.staging_size = '2g'
            script = ns['boot_script'](args)
            self.assertIn('mount -t tmpfs -o size=2g tmpfs /tmp/run', script)
            args.prepare_workload_args = '-d -f "official input.flac" -o output.wav'
            script = ns['boot_script'](args)
            prepared = "/tmp/run/workload -d -f 'official input.flac' -o output.wav"
            self.assertIn(prepared, script)
            self.assertLess(script.index(prepared), script.index('OMP_NUM_THREADS=4 /tmp/run/rate-launcher'))

    def test_active_main_and_four_workers_share_four_cpus_without_deadlock(self):
        cpus = sorted(os.sched_getaffinity(0))[:4]
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            (dest/'probe.cpp').write_text(r'''
#include <thread>
#include <vector>
extern "C" {
#include "fastsim_native_roi.h"
}
void work() {
  fastsim_native_roi_ready("active-compute");
  printf("tid=%ld cpu=%d\n",syscall(SYS_gettid),sched_getcpu());
}
int main() {
  fastsim_native_pin_worker();
  std::vector<std::thread> workers;
  for(int i=0;i<4;++i) workers.emplace_back(work);
  work();
  for(auto &w:workers) w.join();
}
''')
            include = '-I'+str(ROOT/'workloads/spec_roi_integration')
            for compiler, source, name in [('gcc', ROOT/'workloads/spec_roi_integration/fastsim_rate_launcher.c', 'launcher'), ('g++', dest/'probe.cpp', 'probe')]:
                subprocess.run([compiler, '-D_GNU_SOURCE', '-O2', '-pthread', include, str(source), '-o', str(dest/name)], check=True, capture_output=True)
            result = subprocess.run([str(dest/'launcher'), '--cpus', ','.join(map(str,cpus)), '--native-workers', '5', '--', str(dest/'probe')], cwd=dest, env=dict(os.environ,GEM5_DISABLE_ROI_MARKER='1'), capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            rows = [dict(t.split('=') for t in line.split()) for line in (dest/'copy-0.stdout').read_text().splitlines()]
            self.assertEqual(len({r['tid'] for r in rows}), 5)
            self.assertEqual({int(r['cpu']) for r in rows}, set(cpus))
            self.assertEqual(sum(int(r['cpu'])==cpus[0] for r in rows), 2)
            self.assertIn('identities-ready workers=5', result.stdout)

    def test_four_native_workers_in_one_process_reach_gate_and_pin_once(self):
        header = ROOT/'workloads/spec_roi_integration/fastsim_native_roi.h'
        self.assertTrue(header.exists(), 'native worker gate is missing')
        cpus = sorted(os.sched_getaffinity(0))[:4]
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            (dest/'probe.cpp').write_text(r'''
#include <thread>
#include <vector>
extern "C" {
#include "fastsim_native_roi.h"
}
extern int marker_count;
void worker() {
  fastsim_native_pin_worker();
  fastsim_native_roi_ready("probe");
  int first = sched_getcpu();
  fastsim_native_pin_worker();
  fastsim_native_roi_ready("must-not-wait-again");
  printf("worker pid=%d tid=%ld cpu=%d stable=%d registered=%d\n",getpid(),syscall(SYS_gettid),sched_getcpu(),first==sched_getcpu(),__atomic_load_n(&marker_count,__ATOMIC_SEQ_CST));
}
int main() {
  std::vector<std::thread> workers;
  for (int i=0;i<4;++i) workers.emplace_back(worker);
  for (auto &worker:workers) worker.join();
}
''')
            (dest/'marker.cpp').write_text(r'''
#include <unistd.h>
int marker_count;
static int ticket;
extern "C" void fastsim_wait_marker(unsigned long long, unsigned long long,
    unsigned long long, unsigned long long, unsigned long long, unsigned long long) {
  if (__sync_fetch_and_add(&ticket,1)==0) usleep(150000);
  __sync_fetch_and_add(&marker_count,1);
}
''')
            include = '-I'+str(header.parent)
            (dest/'handoff.c').write_text(r'''
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <signal.h>
#include <ucontext.h>
#include <unistd.h>
static void emulate_native_checkpoint(int signal, siginfo_t *info, void *context) {
  ucontext_t *state=context;
  unsigned char *pc=(unsigned char *)state->uc_mcontext.gregs[REG_RIP];
  if (memcmp(pc, "\x0f\x04\x43\x00", 4) ||
      state->uc_mcontext.gregs[REG_RDI] || state->uc_mcontext.gregs[REG_RSI]) _exit(125);
  const char observed[]="[test] instruction-checkpoint delay=0 period=0\n";
  write(STDOUT_FILENO, observed, sizeof(observed)-1);
  state->uc_mcontext.gregs[REG_RIP]+=4;
}
__attribute__((constructor)) static void install_checkpoint_emulator(void) {
  struct sigaction action={0};
  action.sa_sigaction=emulate_native_checkpoint; action.sa_flags=SA_SIGINFO;
  sigemptyset(&action.sa_mask); sigaction(SIGILL, &action, NULL);
}
int __real_open(const char *, int, ...);
int __wrap_open(const char *path, int flags, ...) {
  /* Only replace the unavailable m5 MMIO device with anonymous zero pages.
   * The real marker emitter and its address-mode store still execute. */
  if (!strcmp(path, "/dev/gem5_bridge")) return __real_open("/dev/zero", O_RDWR);
  mode_t mode=0;
  if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode=va_arg(ap, int); va_end(ap); }
  return __real_open(path, flags, mode);
}
int __wrap_nanosleep(const struct timespec *request, struct timespec *remaining) {
  static int count;
  printf("[test] handoff=%d\n", ++count); fflush(stdout); return 0;
}
''')
            subprocess.run(['gcc', '-D_GNU_SOURCE', '-O2', include,
                str(header.parent/'fastsim_rate_launcher.c'), str(dest/'handoff.c'),
                '-Wl,--wrap=nanosleep', '-Wl,--wrap=open', '-o', str(dest/'launcher')],
                check=True, capture_output=True)
            compiled = subprocess.run(['g++', '-D_GNU_SOURCE', '-O2', '-pthread', include,
                str(dest/'probe.cpp'), str(dest/'marker.cpp'), '-o', str(dest/'probe')], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            environment = dict(os.environ, FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES='1')
            environment.pop('GEM5_DISABLE_ROI_MARKER', None)
            result = subprocess.run([str(dest/'launcher'), '--cpus', ','.join(map(str,cpus)),
                '--native-workers', '4', '--', str(dest/'probe')], cwd=dest,
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout)
            rows = (dest/'copy-0.stdout').read_text().splitlines()
            self.assertEqual(len(rows), 4)
            fields = [dict(token.split('=') for token in row.split()[1:]) for row in rows]
            self.assertEqual(len({row['pid'] for row in fields}), 1)
            self.assertEqual(len({row['tid'] for row in fields}), 4)
            self.assertEqual({int(row['cpu']) for row in fields}, set(cpus))
            self.assertEqual({row['stable'] for row in fields}, {'1'})
            self.assertEqual({row['registered'] for row in fields}, {'4'})
            self.assertFalse((dest/'copy-1.stdout').exists())
            self.assertIn('all-ready copies=1 workers=4', result.stdout)
            self.assertEqual((dest/'copy-0.stderr').read_text().count('ready worker='), 4)
            self.assertEqual(result.stdout.count('marker=native-identities-ready operation=checkpoint'), 1)
            self.assertEqual(result.stdout.count('[test] instruction-checkpoint delay=0 period=0'), 1)
            disassembly = subprocess.run(['objdump', '-d', str(dest/'launcher')],
                check=True, capture_output=True, text=True).stdout
            self.assertRegex(disassembly, r'0f 04\s+\(bad\)')
            self.assertIn('43 00', disassembly)
            self.assertLess(result.stdout.index('identities-ready workers=4'),
                            result.stdout.index('marker=native-identities-ready operation=checkpoint'))
            self.assertNotIn('[test] handoff=3', result.stdout,
                             'O3 identity checkpoint must not repeat the KVM handoff sleep')


if __name__ == '__main__':
    unittest.main()
