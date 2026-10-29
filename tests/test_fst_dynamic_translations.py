"""Independent wire fixtures exercise the dynamic translation audit contract."""
import gzip
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))


def uleb(value):
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output + bytes([value]))


def fnv(data, bits=64):
    value, prime = ((14695981039346656037, 1099511628211) if bits == 64
                    else (2166136261, 16777619))
    for byte in data:
        value = ((value ^ byte) * prime) & ((1 << bits) - 1)
    return value


def record(token=7, physical=0x400020, size=8, memory=True):
    data = bytearray(64)
    struct.pack_into('<Q', data, 8, physical)
    struct.pack_into('<HH', data, 48, size, 1 | (2 | 4096 | 32768 if memory else 0))
    struct.pack_into('<I', data, 60, token)
    return bytes(data)


def definition(mapping_id, bits=12, asid=1, ptes=(0x1000, 0x2000, 0x3000, 0x4000),
               virtual=0x200000, physical=0x400000, flags=3, walk_root=None):
    if walk_root is None:
        walk_root = 0x1000 if flags & 2 else 0
    values = (mapping_id, asid, walk_root, virtual, physical, 0)
    return b'\x01' + b''.join(map(uleb, values)) + bytes([bits, len(ptes), flags]) + b''.join(map(uleb, ptes))


def binding(delta, row, mapping_id, token=7, va=0x200020, size=8):
    return (b'\x02' + uleb(delta) + struct.pack('<I', fnv(row, 32))
            + b''.join(map(uleb, (token, va, size, mapping_id))))


def sidecar(body, records, definitions, bindings, unknown=0, memories=None, stream_hash=None):
    if memories is None:
        memories = sum(bool(struct.unpack_from('<H', row, 50)[0] & 14) for row in records)
    footer = (b'\xff' + b''.join(map(uleb, (len(records), memories, definitions, bindings, unknown)))
              + struct.pack('<Q', fnv(b''.join(records)) if stream_hash is None else stream_hash))
    return struct.pack('<8sIIIIQ', b'FSTXLT1\0', 1, 32, 0, 1, 0) + body + footer


class DynamicTranslationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global read_sidecar
        try:
            from fst_dynamic_translations import read_sidecar
        except ImportError:
            read_sidecar = None

    def setUp(self):
        self.assertIsNotNone(read_sidecar, 'dynamic translation decoder must exist')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def read(self, data, records=None, **kwargs):
        path = self.root / 'core.fst.xlat.gz'
        path.write_bytes(gzip.compress(data))
        fst = None
        if records is not None:
            fst = self.root / 'core.fst'
            fst.write_bytes(struct.pack('<8sIIIIQQ4Q', b'FSTRC01\0', 7, 72, 64, 0,
                                        len(records), 512, 0, 0, 0, 0) + b''.join(records))
        return read_sidecar(path, fst_path=fst, **kwargs)

    def test_page_size_path_and_asid_changes_preserve_order(self):
        rows = [record()] * 4 + [record(token=8)]
        body = (definition(1) + definition(2, bits=21, ptes=(0x1000, 0x2000, 0x3000))
                + definition(3, ptes=(0x1000, 0x2000, 0x3000, 0x5000)) + definition(4, asid=2)
                + b''.join(binding(0 if i == 0 else 1, row, mid, token=8 if i == 4 else 7)
                           for i, (row, mid) in enumerate(zip(rows, [1, 2, 1, 3, 4]))))
        result = self.read(sidecar(body, rows, 4, 5), rows)
        self.assertEqual([row['mapping_id'] for row in result['bindings']], [1, 2, 1, 3, 4])
        self.assertEqual(result['definitions'][1]['page_bits'], 21)
        self.assertEqual(result['definitions'][3]['address_space_id'], 2)
        self.assertTrue(result['fst_verified'])
        self.assertEqual(result['coverage']['unknown_records'], 0)

    def test_nonzero_token_cannot_change_asid_virtual_or_physical_page(self):
        for kwargs, va, pa in [({'asid': 2}, 0x200020, 0x400020),
                               ({'virtual': 0x201000}, 0x201020, 0x400020),
                               ({'physical': 0x401000}, 0x200020, 0x401020)]:
            with self.subTest(mapping=kwargs):
                rows = [record(), record(physical=pa)]
                body = (definition(1) + definition(2, **kwargs)
                        + binding(0, rows[0], 1) + binding(1, rows[1], 2, va=va))
                with self.assertRaisesRegex(ValueError, 'token.*identity'):
                    self.read(sidecar(body, rows, 2, 2), rows)

    def test_token_identity_survives_unknown_and_multifragment_updates(self):
        for intermediate in ('unknown', 'split'):
            with self.subTest(intermediate=intermediate):
                rows = [record()] * 3
                body = definition(1) + definition(2, asid=2) + binding(0, rows[0], 1)
                if intermediate == 'unknown':
                    body += binding(1, rows[1], 0, va=0)
                    count, unknown = 3, 1
                else:
                    body += (binding(1, rows[1], 1, size=4)
                             + binding(0, rows[1], 1, va=0x200024, size=4))
                    count, unknown = 4, 0
                body += binding(1, rows[2], 2)
                with self.assertRaisesRegex(ValueError, 'token.*identity'):
                    self.read(sidecar(body, rows, 2, count, unknown=unknown), rows)

    def test_unknown_first_binding_does_not_invent_token_asid_or_page_identity(self):
        rows = [record(), record()]
        body = (definition(1, asid=2) + binding(0, rows[0], 0, va=0)
                + binding(1, rows[1], 1))
        result = self.read(sidecar(body, rows, 1, 2, unknown=1), rows)
        self.assertEqual(result['coverage']['known_records'], 1)
        self.assertEqual(result['coverage']['unknown_records'], 1)

    def test_mapping_path_root_and_levels_match_declared_path_knowledge(self):
        row = record()
        for kwargs in [{'walk_root': 0}, {'ptes': ()},
                       {'flags': 1, 'ptes': (), 'walk_root': 0x1000},
                       {'flags': 1, 'ptes': (0x1000,), 'walk_root': 0}]:
            with self.subTest(mapping=kwargs):
                body = definition(1, **kwargs) + binding(0, row, 1)
                with self.assertRaisesRegex(ValueError, 'path|PTE'):
                    self.read(sidecar(body, [row], 1, 1, unknown=int(kwargs.get('flags') == 1)))

    def test_stable_token_sparse_binding_covers_later_records(self):
        rows = [record(), record(physical=0x400028)]
        result = self.read(sidecar(definition(1) + binding(0, rows[0], 1), rows, 1, 1), rows)
        self.assertEqual(result['coverage']['known_records'], 2)

    def test_unknown_clears_prior_token_state_and_require_complete_rejects(self):
        rows = [record()] * 3
        data = sidecar(definition(1) + binding(0, rows[0], 1) + binding(1, rows[1], 0), rows, 1, 2, unknown=2)
        result = self.read(data, rows)
        self.assertEqual(result['coverage']['unknown_records'], 2)
        with self.assertRaisesRegex(ValueError, 'incomplete|unknown'):
            self.read(data, rows, require_complete=True)

    def test_split_fragments_and_token_zero_need_explicit_full_coverage(self):
        row = record(token=0, physical=0x400ffc, size=8)
        body = (definition(1) + definition(2, virtual=0x201000, physical=0x800000)
                + binding(0, row, 1, token=0, va=0x200ffc, size=4)
                + binding(0, row, 2, token=0, va=0x201000, size=4))
        result = self.read(sidecar(body, [row], 2, 2), [row])
        self.assertEqual(result['coverage']['known_records'], 1)
        data = sidecar(body, [row, row], 2, 2, unknown=1)
        with self.assertRaisesRegex(ValueError, 'binding|coverage'):
            self.read(data, [row, row])

    def test_leaf_only_definition_retains_mapping_but_is_incomplete(self):
        row = record()
        data = sidecar(definition(1, flags=1, ptes=()) + binding(0, row, 1),
                       [row], 1, 1, unknown=1)
        result = self.read(data, [row])
        self.assertEqual(result['definitions'][0]['flags'], 1)
        self.assertEqual(result['coverage']['unknown_records'], 1)
        with self.assertRaisesRegex(ValueError, 'incomplete|unknown'):
            self.read(data, [row], require_complete=True)

    def test_zero_size_memory_flag_does_not_enter_memory_population(self):
        rows = [record(size=0), record(memory=False)]
        result = self.read(sidecar(b'', rows, 0, 0, memories=0), rows)
        self.assertEqual(result['coverage']['memory_records'], 0)

    def test_footer_cannot_understate_explicit_unknown_population_without_fst(self):
        row = record()
        with self.assertRaisesRegex(ValueError, 'population|count'):
            self.read(sidecar(binding(0, row, 0), [row], 0, 1, unknown=0))

    def test_fst_header_requires_declared_companion_matching_core_and_count(self):
        row = record()
        data = sidecar(definition(1) + binding(0, row, 1), [row], 1, 1)
        self.read(data, [row])
        fst = self.root / 'core.fst'
        path = self.root / 'core.fst.xlat.gz'
        original = fst.read_bytes()
        for offset, fmt, value in [(32, '<Q', 0), (20, '<I', 1), (24, '<Q', 2)]:
            broken = bytearray(original)
            struct.pack_into(fmt, broken, offset, value)
            fst.write_bytes(broken)
            with self.assertRaisesRegex(ValueError, 'header|count|feature'):
                read_sidecar(path, fst_path=fst)

    def test_rejects_corrupt_hash_token_and_footer_counts(self):
        row = record()
        base = definition(1)
        for body, kw in [(base + binding(0, record(physical=0x400030), 1), {}),
                         (base + binding(0, row, 1, token=8), {}),
                         (base + binding(0, row, 1), {'memories': 0}),
                         (base + binding(0, row, 1), {'stream_hash': 1})]:
            with self.subTest(kw=kw, body=body):
                with self.assertRaises(ValueError):
                    self.read(sidecar(body, [row], 1, 1, **kw), [row])

    def test_rejects_definition_alignment_undefined_id_and_leaf_bits(self):
        row = record()
        for body in [definition(1, virtual=0x200001), definition(1, bits=13),
                     definition(2), definition(1, ptes=(0x1002,)),
                     binding(0, row, 1)]:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.read(sidecar(body, [row], 1, 1))

    def test_legacy_32bit_pte_four_byte_alignment_is_accepted(self):
        row = record()
        result = self.read(sidecar(definition(1, ptes=(0x1004, 0x2004)) + binding(0, row, 1), [row], 1, 1))
        self.assertFalse(result['fst_verified'])
        self.assertIsNone(result['coverage'])

    def test_rejects_overflow_truncated_varints_and_missing_footer(self):
        header = struct.pack('<8sIIIIQ', b'FSTXLT1\0', 1, 32, 0, 1, 0)
        for tail in [b'\x01' + b'\xff' * 9 + b'\x02', b'\x01\x80', b'']:
            with self.subTest(tail=tail):
                with self.assertRaises(ValueError):
                    self.read(header + tail)

    def test_rejects_trailing_decompressed_bytes_and_second_gzip_member_and_bad_crc(self):
        valid = sidecar(b'', [], 0, 0)
        with self.assertRaises(ValueError):
            self.read(valid + b'\x00')
        path = self.root / 'broken.gz'
        compressed = gzip.compress(valid)
        for wire in [compressed + gzip.compress(b''), compressed[:-1], compressed[:-8] + bytes(8)]:
            path.write_bytes(wire)
            with self.assertRaises(ValueError):
                read_sidecar(path)

    def test_require_complete_without_fst_fails_closed(self):
        row = record()
        data = sidecar(definition(1) + binding(0, row, 1), [row], 1, 1)
        with self.assertRaisesRegex(ValueError, 'FST|fst'):
            self.read(data, require_complete=True)

    def test_cli_reports_diagnostic_unknown_and_fails_require_complete(self):
        path = self.root / 'unknown.gz'
        row = record()
        path.write_bytes(gzip.compress(sidecar(binding(0, row, 0), [row], 0, 1, unknown=1)))
        cmd = [sys.executable, str(TOOLS / 'audit_fst_dynamic_translations.py'), str(path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['footer']['unknown_records'], 1)
        result = subprocess.run(cmd + ['--require-complete'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
