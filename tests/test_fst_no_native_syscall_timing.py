import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_raw_syscall_microops import raw_trace


class NoNativeSyscallTiming(unittest.TestCase):
    def test_rejects_values_and_validity_independently(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.fst'
            raw_trace(path)
            row = 72 + 19 * 64
            original = path.read_bytes()
            command = [sys.executable, str(ROOT/'tools/audit_fst_syscall_metadata.py'),
                       '--fst', str(path), '--require-no-timing']
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            for field in ('valid', 'value'):
                data = bytearray(original)
                if field == 'valid': struct.pack_into('<H', data, row + 116, 1 << 4)
                else: struct.pack_into('<Q', data, row + 88, 100)
                path.write_bytes(data)
                self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0, field)


if __name__ == '__main__':
    unittest.main()
