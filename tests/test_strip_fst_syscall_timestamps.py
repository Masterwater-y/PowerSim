import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_raw_syscall_microops import raw_trace
from strip_fst_syscall_timestamps import strip_file


class StripTimestampTest(unittest.TestCase):
    def test_only_timestamp_values_and_validity_bits_change(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d) / 'in.fst', Path(d) / 'out.fst'
            raw_trace(source)
            b = bytearray(source.read_bytes())
            row = 72 + 19 * 64
            struct.pack_into('<QQ', b, row + 88, 123, 456)
            struct.pack_into('<H', b, row + 116, 0x30)
            source.write_bytes(b)
            strip_file(source, target)
            got = bytearray(target.read_bytes())
            self.assertEqual(source.read_bytes(), b)
            self.assertEqual(struct.unpack_from('<QQ', got, row + 88), (0, 0))
            self.assertEqual(struct.unpack_from('<H', got, row + 116)[0], 0)
            got[row + 88:row + 104] = b[row + 88:row + 104]
            got[row + 116:row + 118] = b[row + 116:row + 118]
            self.assertEqual(got, b)

    def test_malformed_and_existing_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d) / 'in.fst', Path(d) / 'out.fst'
            raw_trace(source)
            target.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                strip_file(source, target)
            self.assertEqual(target.read_bytes(), b'keep')
            target.unlink()
            source.write_bytes(source.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                strip_file(source, target)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
