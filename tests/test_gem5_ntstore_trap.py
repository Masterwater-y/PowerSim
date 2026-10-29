"""Opt-in integration regression against the actual compiled gem5 O3 decoder."""
import json
import os
from pathlib import Path
import resource
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('GEM5_NTSTORE_TEST_BINARY'),
                     'set GEM5_NTSTORE_TEST_BINARY to run O3 integration')
class NTStoreTrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gem5 = Path(os.environ['GEM5_NTSTORE_TEST_BINARY']).resolve()
        cls.out = Path(os.environ.get('GEM5_NTSTORE_TEST_OUT',
                                     ROOT / 'tmp/ntstore-regression')).resolve()
        cls.out.mkdir(parents=True, exist_ok=True)
        for name, flags in [('normal', []), ('fault', ['-DFAULT_PROBE'])]:
            subprocess.run(['/opt/gcc-11/bin/gcc', '-nostdlib', '-static', '-no-pie',
                            *flags, str(ROOT / 'tests/workloads/ntstore_no_debug_trap.S'),
                            '-o', str(cls.out / name)], check=True)

    def run_guest(self, name):
        directory = self.out / (name + '-gem5')
        directory.mkdir(exist_ok=True)
        result = subprocess.run([str(self.gem5), '--outdir=' + str(directory),
                                 str(ROOT / 'tools/gem5/run_ntstore_regression.py'),
                                 '--binary', str(self.out / name)],
                                capture_output=True, text=True, timeout=90,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
        (directory / 'driver.log').write_text(result.stdout + result.stderr)
        return result, directory

    def test_nt_stores_preserve_bytes_without_debug_traps(self):
        result, directory = self.run_guest('normal')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"code": 0', result.stdout)
        # Text dumps omit zero vectors. Read the actual VectorInfo instead
        # of treating a missing text row as a measured zero.
        status = json.loads((directory / 'ntstore-result.json').read_text())['commit_status']
        self.assertGreater(status['running'], 0)
        self.assertEqual(status['trapPending'], 0,
                         'valid NT stores must not repeatedly enter the O3 fault/trap path')

    def test_real_bad_address_fault_remains(self):
        result, _ = self.run_guest('fault')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout + result.stderr,
                         r'(page table fault|Page-Fault|BadAddress|write unmapped address)')


if __name__ == '__main__':
    unittest.main()
