"""Architectural load populations use bound functional hint roles."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from audit_perf_retirement import scan_functional_retirement
from test_task_pmu_oracle import task_fixture


def fnv(data, bits):
    value, prime = (2166136261, 16777619) if bits == 32 else (14695981039346656037, 1099511628211)
    for byte in data: value = ((value ^ byte) * prime) & ((1 << bits) - 1)
    return value


class SoftwarePrefetchAuditTest(unittest.TestCase):
    def source(self, trace, *, version=2, role=2):
        task_fixture(trace)
        meta = json.loads((trace / 'thread-state-metadata-core0.json').read_text())
        events = [json.loads(row) for row in (trace / 'thread-state-core0.jsonl').read_text().splitlines()]
        source = trace / meta['source_file']; data = bytearray(source.read_bytes())
        struct.pack_into('<Q', data, 32, 64)
        for ordinal in (4, 5):
            struct.pack_into('<H', data, 72 + ordinal * 64 + 48, 8)
            struct.pack_into('<H', data, 72 + ordinal * 64 + 50, 1 | 2)
        if role == 1:
            struct.pack_into('<H', data, 72 + 4 * 64 + 50, 1 | 4)
        source.write_bytes(data)
        header = struct.pack('<8sIIIIQQQII', b'FSTMREQ1', version, 56, 0, 1,
                             8, 1, fnv(data[72:], 64), 24, 0)
        record = data[72 + 4 * 64:72 + 5 * 64]
        companion = Path(str(source) + '.mreq')
        companion.write_bytes(header + struct.pack('<QIIB7s', 4, fnv(record, 32), 0, role, bytes(7)))
        return source, companion, meta, events

    def test_prefetch_is_an_instruction_but_not_an_architectural_load(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, _, meta, events = self.source(trace)
            result = scan_functional_retirement(trace, meta, events, {101})
            self.assertEqual(result['physical']['retired_macro_instructions'], 5)
            self.assertEqual(result['physical']['retired_uops'], 6)
            self.assertEqual(result['physical']['perf_retired_loads'], 1)

    def test_spr_prefetch_load_membership_is_explicitly_versioned(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace=Path(directory); _, _, meta, events=self.source(trace)
            result=scan_functional_retirement(trace,meta,events,{101},
                load_event_policy='spr-software-prefetch-v1')['physical']
            self.assertEqual(result['perf_retired_loads'],2)
            self.assertEqual(result['perf_retired_stores'],0)
            with self.assertRaisesRegex(ValueError,'policy'):
                scan_functional_retirement(trace,meta,events,{101},load_event_policy='guessed')

    def test_v1_no_access_keeps_historical_population(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, _, meta, events = self.source(trace, version=1, role=1)
            result = scan_functional_retirement(trace, meta, events, {101})['physical']
            self.assertEqual(result['perf_retired_loads'], 1)
            self.assertEqual(result['perf_retired_stores'], 1)

    def test_no_access_cannot_relabel_a_load(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, companion, meta, events = self.source(trace)
            data = bytearray(companion.read_bytes()); data[72] = 1; companion.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'request|NO_ACCESS|store'):
                scan_functional_retirement(trace, meta, events, {101})

    def test_missing_required_companion_rejects(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, companion, meta, events = self.source(trace); companion.unlink()
            with self.assertRaisesRegex(ValueError, 'request|companion'):
                scan_functional_retirement(trace, meta, events, {101})

    def test_wrong_row_binding_rejects(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, companion, meta, events = self.source(trace)
            data = bytearray(companion.read_bytes()); data[64] ^= 1; companion.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'request|binding|hash'):
                scan_functional_retirement(trace, meta, events, {101})

    def test_v1_cannot_claim_new_hint_role(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            trace = Path(directory); _, _, meta, events = self.source(trace, version=1)
            with self.assertRaisesRegex(ValueError, 'request|version|role'):
                scan_functional_retirement(trace, meta, events, {101})


if __name__ == '__main__': unittest.main()
