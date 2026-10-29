"""Reject a setup-dominated ROI using real ELF symbols and bounded FST input."""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/validate_roi_phase.py'
HEADER = struct.Struct('<8sIIIIQQ4Q')
RECORD = struct.Struct('<QQQQ4IHHhBB4BI')


class RoiPhaseValidationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / 'probe'
        source = self.root / 'probe.c'
        source.write_text('''
#line 1 "lib/compress/zstd_fixture.c"
__attribute__((noinline)) void ZSTD_compressBlock_fixture(volatile int *p) { *p += 1; }
__attribute__((noinline)) void ZSTD_resetCCtx_internal(volatile int *p) { *p = 1; }
#line 1 "programs/bench_fixture.c"
__attribute__((noinline)) void __memset_fixture(volatile int *p) { *p = 0; }
int main(void) { volatile int p=0; __memset_fixture(&p); ZSTD_compressBlock_fixture(&p); return 0; }
''')
        subprocess.run(['gcc', '-g', '-O0', '-fno-pie', '-no-pie',
                        str(source), '-o', str(self.binary)], check=True,
                       capture_output=True)
        symbols = subprocess.run(['nm', '-n', str(self.binary)], check=True,
                                 capture_output=True, text=True).stdout
        self.pc = {line.split()[-1]: int(line.split()[0], 16)
                   for line in symbols.splitlines()
                   if len(line.split()) == 3 and line.split()[-1] in
                   ('ZSTD_compressBlock_fixture', '__memset_fixture', 'ZSTD_resetCCtx_internal')}
        self.trace = self.root / 'tao_trace'
        self.trace.mkdir()
        self.request = dict(binary_identity=dict(
            binary_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest()))
        (self.root / 'sync-run-request.json').write_text(json.dumps(self.request))

    def stream(self, core, warmup, measured):
        pcs = warmup + measured
        path = self.trace / f'core{core}.fst'
        with path.open('wb') as handle:
            handle.write(HEADER.pack(b'FSTRC01\0', 7, 72, 64, core, len(pcs), 0, 0, 0, 0, 1))
            for pc in pcs:
                handle.write(RECORD.pack(pc, 0, 0, 0, 0, 0, 0, 0,
                                         0, 1, 0, 0, 0, 0, 0, 0, 0, 0))
        (self.trace / f'thread-state-metadata-core{core}.json').write_text(json.dumps(
            dict(core_id=core, source_file=path.name, source_record_count=len(pcs))))
        (self.trace / f'functional-boundary-core{core}.json').write_text(json.dumps(
            dict(core_id=core, warmup_records=len(warmup), measurement_records=len(measured),
                 total_records=len(pcs), measurement_closed=True)))

    def run_gate(self, cores=1):
        self.assertTrue(TOOL.exists(), 'missing ROI phase gate')
        output = self.root / 'phase.json'
        result = subprocess.run([sys.executable, str(TOOL), '--trace-dir', str(self.trace),
                                 '--binary', str(self.binary), '--cores', str(cores),
                                 '--output', str(output)], text=True, capture_output=True)
        self.assertTrue(output.exists(), result.stderr)
        return result.returncode, json.loads(output.read_text())

    def test_rejects_memset_measurement_even_with_compression_in_warmup(self):
        self.stream(0, [self.pc['ZSTD_compressBlock_fixture']] * 20,
                    [self.pc['__memset_fixture']] * 100)
        code, report = self.run_gate()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['valid'])
        self.assertEqual(report['per_core'][0]['measurement']['compression_user_uops'], 0)

    def test_accepts_compression_after_setup_warmup(self):
        self.stream(0, [self.pc['__memset_fixture']] * 20,
                    [self.pc['ZSTD_compressBlock_fixture']] * 100)
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertTrue(report['valid'])
        self.assertEqual(report['per_core'][0]['measurement']['compression_user_uops'], 100)

    def test_checks_every_participant(self):
        self.stream(0, [self.pc['__memset_fixture']],
                    [self.pc['ZSTD_compressBlock_fixture']] * 100)
        self.stream(1, [self.pc['__memset_fixture']], [self.pc['__memset_fixture']] * 100)
        code, report = self.run_gate(2)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(report['per_core']), 2)

    def test_rejects_binary_identity_mismatch(self):
        self.stream(0, [self.pc['__memset_fixture']],
                    [self.pc['ZSTD_compressBlock_fixture']] * 100)
        self.request['binary_identity']['binary_sha256'] = '0' * 64
        (self.root / 'sync-run-request.json').write_text(json.dumps(self.request))
        code, report = self.run_gate()
        self.assertNotEqual(code, 0)
        self.assertIn('identity', ' '.join(report['errors']))

    def test_reads_hot_records_before_embedded_syscall_table(self):
        self.stream(0, [self.pc['__memset_fixture']] * 20,
                    [self.pc['ZSTD_compressBlock_fixture']] * 100)
        path = self.trace / 'core0.fst'
        with path.open('r+b') as handle:
            handle.write(HEADER.pack(b'FSTRC01\0', 7, 72, 64, 0, 120,
                                     10, 72 + 120 * 64, 1, 128, 1))
            handle.seek(0, 2)
            handle.write(bytes(128))
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['per_core'][0]['measurement']['user_uops'], 100)

    def test_rejects_context_initialization_inside_compression_source_tree(self):
        self.stream(0, [self.pc['__memset_fixture']] * 20,
                    [self.pc['ZSTD_resetCCtx_internal']] * 100)
        code, report = self.run_gate()
        self.assertNotEqual(code, 0)
        self.assertEqual(report['per_core'][0]['measurement']['setup_user_uops'], 100)


if __name__ == '__main__':
    unittest.main()
