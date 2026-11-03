"""Functional CDA adjacency audit: no native cache labels or timing inputs."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'audit', Path(__file__).resolve().parents[1] / 'tools/audit_store_chain.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class StoreChainAuditTests(unittest.TestCase):
    def fixture(self, directory, corrupt=False, end=3):
        p = Path(directory) / 'core.fst'
        records = []
        for address in (0x1008, 0x1000, 0x1008):
            r = bytearray(64)
            struct.pack_into('<QQ', r, 0, 0x4000, address)
            struct.pack_into('<HH', r, 48, 8, 5)
            records.append(r)
        header = bytearray(72)
        struct.pack_into('<8sIIIIQQ', header, 0, b'FSTRC01\0', 7, 72, 64, 0, 3, 64)
        p.write_bytes(header + b''.join(records))
        row = audit.ROW.pack(0, audit.record_hash(records[0]) ^ corrupt, 0, 1, bytes(7))
        Path(str(p) + '.mreq').write_bytes(audit.HEADER.pack(
            b'FSTMREQ1', 2, 56, 0, 1, 3, 1, 0, 24, 0) + row)
        return p, end

    def test_cda_and_two_data_members(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d)
            result = audit.audit_trace(p, 0, 0, end)
            self.assertEqual(result['counts']['cda_two_store_same_pc'], 1)
            self.assertEqual(result['counts']['same_line'], 1)
            self.assertFalse(result['full_stream_hash_verified'])

    def test_bad_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d, corrupt=True)
            with self.assertRaisesRegex(ValueError, 'hash'):
                audit.audit_trace(p, 0, 0, end)

    def test_boundary_is_not_a_successful_triple(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d, end=2)
            result = audit.audit_trace(p, 0, 0, end)
            self.assertEqual(result['counts']['boundary'], 1)
            self.assertEqual(result['counts'].get('cda_two_store_same_pc', 0), 0)

    def test_missing_companion_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d)
            Path(str(p) + '.mreq').unlink()
            with self.assertRaises(FileNotFoundError):
                audit.audit_trace(p, 0, 0, end)

    def test_trailing_fst_metadata_is_not_a_hot_record(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d)
            p.write_bytes(p.read_bytes() + bytes(128))
            self.assertEqual(audit.audit_trace(p, 0, 0, end)['counts']['noaccess'], 1)

    def test_following_noaccess_is_not_a_data_member(self):
        with tempfile.TemporaryDirectory() as d:
            p, end = self.fixture(d)
            content = bytearray(Path(str(p) + '.mreq').read_bytes())
            struct.pack_into('<Q', content, 32, 2)
            record = p.read_bytes()[136:200]
            content.extend(audit.ROW.pack(1, audit.record_hash(record), 0, 1, bytes(7)))
            Path(str(p) + '.mreq').write_bytes(content)
            counts = audit.audit_trace(p, 0, 0, end)['counts']
            self.assertEqual(counts.get('cda_two_store_same_pc', 0), 0)


if __name__ == '__main__':
    unittest.main()
