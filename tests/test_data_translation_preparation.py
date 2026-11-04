"""Source-bound data origin transport and preparation gates, without timing input."""
import gzip
import json
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from fst_dynamic_translations import read_sidecar, _fnv
import prepare_spr_capture as prepare


def policy():
    return runpy.run_path(str(ROOT / 'gem5-spr/src/cpu/o3/probe/generate_static_uop_policy.py'))['policy_digest'](ROOT / 'gem5-spr').hex()


def bindings(body):
    """Offsets from real writer output, independent of chosen varint widths."""
    position = 64
    rows = []
    def uv():
        nonlocal position
        start = position
        while body[position] & 128:
            position += 1
        position += 1
        return start, position
    while body[position] != 255:
        start = position
        tag = body[position]
        position += 1
        if tag == 1:
            for _ in range(6): uv()
            levels = body[position + 1]
            position += 3
            for _ in range(levels): uv()
            position += 1
            uv(); uv()
        elif tag == 2:
            uv()
            hash_at = position
            position += 4
            fields = [uv() for _ in range(4)]
            asid_start, asid_end = uv()
            rows.append((start, position, asid_start, asid_end, hash_at, fields[1]))
        else:
            raise AssertionError('unexpected writer tag')
    return rows


class DataQualificationTests(unittest.TestCase):
    def audit(self):
        return dict(static_uop_facts=dict(policy_identities=[policy()]), data_translation_facts=dict(
            files_declared=1, wire_version_1_files=0, wire_version_2_files=0, wire_version_3_files=1,
            memory_records=3, memory_bytes=24, byte_mapping_records=2, observed_path_records=1,
            leaf_only_records=1, unknown_mapping_records=1, byte_mapping_bytes=16,
            observed_path_bytes=8, leaf_only_bytes=8, unknown_mapping_bytes=8, special_request_bytes=0,
            origin_retirement_asid_mismatch_records=2, mixed_origin_asid_records=1, historical_walk_root_records=1))

    def test_valid_source_preserves_missing_evidence_and_context_counts(self):
        result = prepare.data_translation_qualification(self.audit(), 1, ROOT / 'gem5-spr', True)
        self.assertEqual(result['status'], 'transport-validated')
        self.assertEqual(result['byte_mapping_status'], 'incomplete')
        self.assertEqual(result['actual_path_status'], 'incomplete')
        self.assertFalse(result['logical_mmu_controls_complete'])
        self.assertFalse(result['full_spr_profile_qualified'])

    def test_missing_wrong_policy_and_population_fail_closed(self):
        for field in ('memory_records', 'memory_bytes', 'byte_mapping_records', 'observed_path_bytes'):
            audit = self.audit()
            audit['data_translation_facts'][field] += 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'conserve'):
                prepare.data_translation_qualification(audit, 1, ROOT / 'gem5-spr', True)
        audit = self.audit();audit['static_uop_facts']['policy_identities'] = ['00' * 32]
        with self.assertRaisesRegex(ValueError, 'policy'):
            prepare.data_translation_qualification(audit, 1, ROOT / 'gem5-spr', True)
        with self.assertRaisesRegex(ValueError, 'root missing'):
            prepare.data_translation_qualification(self.audit(), 1, None, True)

    def test_switch_mismatch_rejects_and_legacy_is_not_upgraded(self):
        with self.assertRaisesRegex(ValueError, 'declaration'):
            prepare.data_translation_qualification(self.audit(), 1, ROOT / 'gem5-spr', False)
        legacy = self.audit();legacy['data_translation_facts']['wire_version_3_files'] = 0
        legacy['data_translation_facts']['wire_version_2_files'] = 1
        result = prepare.data_translation_qualification(legacy, 1, ROOT / 'gem5-spr', False)
        self.assertEqual(result['status'], 'legacy')
        self.assertEqual(result['origin_context_binding'], 'unavailable')
        with self.assertRaisesRegex(ValueError, 'declaration'):
            prepare.data_translation_qualification(legacy, 1, ROOT / 'gem5-spr', True)

    def test_shared_writer_source_matches_native(self):
        native = (ROOT / 'gem5-spr/taogen/shared/fst_dynamic_translation_writer.hh').read_text()
        parent = (ROOT / 'include/fastsim/fst_dynamic_translation_writer.hpp').read_text()
        self.assertEqual(native.replace('"fst_translation_facts.hh"', '"fastsim/fst_translation_facts.hpp"'), parent)


class ActualTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transport = Path(os.environ.get('FASTSIM_DATA_ORIGIN_TEST', ROOT / 'build/fastsim_data_origin_tests')).resolve()
        cls.auditor = Path(os.environ.get('FASTSIM_RETIRED_FRONTEND_AUDITOR', ROOT / 'build/fastsim_audit_retired_frontend')).resolve()
        if not cls.transport.is_file() or not cls.auditor.is_file():
            raise unittest.SkipTest('build data-origin transport and maintained frontend auditor first')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='data-origin-tests-', dir=ROOT / 'tmp')
        self.addCleanup(self.directory.cleanup)
        self.fst = Path(self.directory.name) / 'trace.fst'
        self.policy = policy()
        checked = subprocess.run([str(self.transport), self.policy, str(self.fst)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.sidecar = Path(str(self.fst) + '.xlat.gz')
        self.body = bytearray(gzip.decompress(self.sidecar.read_bytes()))

    def audit(self):
        return subprocess.run([str(self.auditor), str(self.fst)], capture_output=True, text=True)

    def test_writer_reader_auditor_and_python_source_conserve(self):
        result = read_sidecar(self.sidecar, self.fst)
        self.assertEqual(result['header']['version'], 3)
        self.assertTrue(result['fst_verified'])
        run = self.audit();self.assertEqual(run.returncode, 0, run.stderr)
        audit = json.loads(run.stdout)
        data = audit['data_translation_facts']
        self.assertEqual(data['memory_bytes'], 128)
        self.assertEqual(data['byte_mapping_bytes'], 104)
        self.assertEqual(data['unknown_mapping_bytes'], 24)
        self.assertEqual(data['mixed_origin_asid_records'], 1)
        self.assertEqual(data['historical_walk_root_records'], 6)
        self.assertEqual(data['special_request_bytes'], 48)
        self.assertEqual(audit['functional_memory_facts']['ordinary_loads_proven'], 0)
        result = prepare.data_translation_qualification(audit, 1, ROOT / 'gem5-spr', True)
        self.assertFalse(result['logical_mmu_controls_complete'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            read_sidecar(self.sidecar, self.fst, require_complete=True)

    def test_missing_or_altered_context_and_row_hash_reject(self):
        _, _, start, end, hash_at, _ = bindings(self.body)[0]
        edits = []
        changed = self.body.copy();changed[end - 1] ^= 0x20;edits.append(changed)
        missing = self.body.copy();del missing[start:end];edits.append(missing)
        hashed = self.body.copy();hashed[hash_at] ^= 1;edits.append(hashed)
        for body in edits:
            self.sidecar.write_bytes(gzip.compress(body))
            with self.subTest(body=body[:8]), self.assertRaises((ValueError, OSError)):
                read_sidecar(self.sidecar, self.fst)
            run = self.audit();self.assertNotEqual(run.returncode, 0, run.stdout)

    def test_overlapping_fragment_bytes_and_footer_counts_reject(self):
        rows = bindings(self.body)
        # The mixed-origin 8-byte row has two explicit 4-byte fragments.
        first, second = rows[3], rows[4]
        a, b = first[-1]; c, d = second[-1]
        self.assertEqual(b - a, d - c)
        body = self.body.copy();body[c:d] = body[a:b]
        self.sidecar.write_bytes(gzip.compress(body))
        with self.assertRaises(ValueError):read_sidecar(self.sidecar, self.fst)
        self.assertNotEqual(self.audit().returncode, 0)
        body = self.body.copy();body[-9] ^= 1
        self.sidecar.write_bytes(gzip.compress(body))
        with self.assertRaises(ValueError):read_sidecar(self.sidecar, self.fst)
        self.assertNotEqual(self.audit().returncode, 0)

    def test_context_validation_precedes_replay_slice(self):
        rows = bindings(self.body)
        start, end, _, _, _, _ = rows[-1]
        body = self.body.copy();del body[start:end]
        self.sidecar.write_bytes(gzip.compress(body))
        self.assertNotEqual(self.audit().returncode, 0)

    def test_missing_declared_vmap_and_source_policy_reject(self):
        for suffix in ('.vmap', '.uops.gz', '.asmap'):
            path = Path(str(self.fst) + suffix);saved = path.read_bytes();path.unlink()
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, OSError)):
                read_sidecar(self.sidecar, self.fst)
            self.assertNotEqual(self.audit().returncode, 0)
            path.write_bytes(saved)

    def test_undeclared_hot_tokens_without_vmap_reject(self):
        hot = bytearray(self.fst.read_bytes())
        hot[32] &= ~1
        self.fst.write_bytes(hot)
        Path(str(self.fst) + '.vmap').unlink()
        with self.assertRaisesRegex(ValueError, 'requires declared virtual-page map'):
            read_sidecar(self.sidecar, self.fst)
        result = self.audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires declared virtual-page map', result.stderr)

    def test_hot_hash_and_footer_corruption_reject(self):
        hot = bytearray(self.fst.read_bytes());hot[-1] ^= 1;self.fst.write_bytes(hot)
        with self.assertRaisesRegex(ValueError, 'hot-stream hash'):
            read_sidecar(self.sidecar, self.fst)
        self.assertNotEqual(self.audit().returncode, 0)


if __name__ == '__main__': unittest.main()
