#!/usr/bin/env python3
"""Check the offline head-gap partition against a per-cycle reference."""

import importlib.util
import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'audit_fst_commit_gaps', ROOT / 'tools/audit_fst_commit_gaps.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
sys.modules['audit_fst_commit_gaps'] = audit
paired_spec = importlib.util.spec_from_file_location(
    'analyze_paired_rob_gaps', ROOT / 'tools/analyze_paired_rob_gaps.py')
paired = importlib.util.module_from_spec(paired_spec)
paired_spec.loader.exec_module(paired)


class HeadGapTests(unittest.TestCase):
    def test_interval_partition_matches_cycle_oracle(self):
        rng = random.Random(24)
        for _ in range(100):
            rows, retire = [], 10
            for i in range(50):
                retire += rng.randrange(0, 12)
                issue = rng.randrange(retire + 1)
                fetch = rng.randrange(issue + 1)
                rows.append((fetch, issue, retire, rng.choice(['load', 'store', 'other'])))
            ledger = audit.HeadGaps()
            for i, row in enumerate(rows):
                ledger.add(*row, {'sequence': i})
            actual = ledger.result()
            expected = {k: 0 for k in actual['categories']}
            productive = 0
            for tick in range(rows[0][2] + 1, rows[-1][2] + 1):
                if any(row[2] == tick for row in rows):
                    productive += 1
                    continue
                head = next(row for row in rows if row[2] > tick)
                key = ('not_fetched_cycles' if tick < head[0] else
                       'fetched_not_issued_cycles' if tick < head[1] else
                       'issued_%s_cycles' % head[3])
                expected[key] += 1
            self.assertEqual(actual['categories'], expected)
            self.assertEqual(actual['productive_cycles'], productive)

    def test_auxiliary_record_does_not_shift_micro_seq_join(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            root = Path(directory)
            (root / 'trace').mkdir()
            fst = root / 'test.fst'
            records = []
            for flags, op in [(0, 1), (0, -1), (2, 56), (0, 1)]:
                record = bytearray(64)
                struct.pack_into('<Hh', record, 50, flags, op)
                records.append(record)
            fst.write_bytes(audit.HEADER.pack(b'FSTRC01\0', 7, 72, 64, 0, 4, 31,
                                             328, 0, 0, 0) + b''.join(records))
            labels = root / 'trace/test.switch0.test.labels.micro.jsonl'
            rows = [dict(core_id=0, thread_id=0, micro_seq=i+1,
                         fetch_tick=333, issue_tick=333,
                         commit_tick=333*tick)
                    for i, tick in enumerate([3, 10, 15])]
            labels.write_text('\n'.join(map(json.dumps, rows)))
            entry = ['0', 'fastsim-binary-warmup-slice', str(fst), '0', '0', '0', '2', '2']
            cpl = dict(clock_period_ticks=333, first_tick=3330, last_tick=4995,
                       measured_ticks=1665, idle_ticks=0)
            out = audit.audit_core((str(root), entry, cpl))
            self.assertEqual(out['span_cycles'], 5)
            self.assertEqual(out['rows'], 2)
            self.assertEqual(out['boundary_uncovered_cycles'], 0)
            rows[1]['micro_seq'] = 9
            labels.write_text('\n'.join(map(json.dumps, rows)))
            with self.assertRaisesRegex(ValueError, 'micro_seq'):
                audit.audit_core((str(root), entry, cpl))

    def test_baseline_comparison_rejects_experimental_and_sparse_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            path = Path(directory) / 'pairs.json'
            data = dict(instrumentation_target_metrics_equal=True,
                        experimental_model=True)
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'experimental'):
                paired.compare(path)
            data['experimental_model'] = False
            data['cores'] = [dict(pairs=[dict(record_ordinal=1),
                                        dict(record_ordinal=3)])]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'dense'):
                paired.compare(path)


if __name__ == '__main__':
    unittest.main()
