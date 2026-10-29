"""Ready checkpoints resume at a new marker, without repeating initialization."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))


class ReadyCheckpointTest(unittest.TestCase):
    def test_fresh_and_restored_marker_sequences(self):
        from spec_rate_checkpoint import RateCheckpointFlow
        for phase, fresh, restored in (
            ('staged', ['save', 'switch', 'ignore'], ['switch', 'ignore']),
            ('compute-ready', ['continue', 'save', 'switch', 'ignore'], ['switch', 'ignore']),
        ):
            flow = RateCheckpointFlow(phase, restored=False)
            self.assertEqual([flow.checkpoint() for _ in fresh], fresh)
            flow = RateCheckpointFlow(phase, restored=True)
            self.assertEqual([flow.checkpoint() for _ in restored], restored)
        with self.assertRaises(ValueError):
            RateCheckpointFlow('unknown', restored=False)

    def test_serial_initialization_and_census_precede_ready_checkpoint(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            dest = Path(directory)
            (dest / 'probe.c').write_text(r'''
#include "fastsim_rate_roi.h"
#include <sys/file.h>
int main(void) {
  int fd=open("init.lock",O_CREAT|O_RDWR,0600);
  if(fd<0 || flock(fd,LOCK_EX|LOCK_NB)) return 7;
  usleep(20000); flock(fd,LOCK_UN);close(fd);
  fastsim_rate_roi_ready("serial-ready-probe"); return 0;
}
''')
            for source, output in (
                (ROOT / 'workloads/spec_roi_integration/fastsim_rate_launcher.c', dest / 'launcher'),
                (dest / 'probe.c', dest / 'probe'),
            ):
                subprocess.run(['gcc', '-O2', '-I'+str(ROOT / 'workloads/spec_roi_integration'),
                                str(source), '-o', str(output)], check=True, capture_output=True)
            # Force a terminal in this harness and observe the real launcher's
            # drain call. Otherwise a pipe would skip precisely this operation.
            (dest/'terminal.c').write_text(
                '#include <stdio.h>\nint __wrap_isatty(int fd){return 1;}\n'
                'int __wrap_tcdrain(int fd){puts("[test] serial-drained");return 0;}\n')
            subprocess.run(['gcc','-O2','-I'+str(ROOT/'workloads/spec_roi_integration'),
                str(ROOT/'workloads/spec_roi_integration/fastsim_rate_launcher.c'),
                str(dest/'terminal.c'),'-Wl,--wrap=isatty','-Wl,--wrap=tcdrain',
                '-o',str(dest/'launcher')],check=True,capture_output=True)
            cpus = sorted(os.sched_getaffinity(0))[:4]
            result = subprocess.run([str(dest / 'launcher'), '--cpus', ','.join(map(str, cpus)),
                                     '--', str(dest / 'probe')], cwd=dest,
                env=dict(os.environ, GEM5_DISABLE_ROI_MARKER='1', FASTSIM_RATE_SERIAL_INIT='1',
                         FASTSIM_RATE_READY_CHECKPOINT='1'),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            markers = ['pre-checkpoint-census end', 'compute-ready-checkpoint', '[test] serial-drained',
                       'compute-ready-resume', 'post-switch-census begin', 'post-switch-census end']
            positions = [result.stdout.index(marker) for marker in markers]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(result.stdout.count('task_tid='), 8)


if __name__ == '__main__':
    unittest.main()
