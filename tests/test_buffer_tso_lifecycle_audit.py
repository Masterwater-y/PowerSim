import copy
from pathlib import Path
import sys
import json
import struct
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_buffer_tso_lifecycle import (
    store_observation, assert_audit_neutral, verify_store_identity,
    validate_samples, raw_trace_path, validate_native_inputs, verify_prefix,
    validate_stats_binding, sha256, HEADER, RECORD, FLAG_FIELDS,
)


class StoreLifecycleAuditTests(unittest.TestCase):
    def setUp(self):
        self.sample = dict(regular_store=1, store_send_cycle=20,
                           store_service_cycles=2, store_sq_release_cycle=22,
                           actual_retire_cycle=10, buffer_tso_role=3,
                           buffer_tso_permission_ready_cycle=15)
        self.pair = dict(is_store=True, is_load=False, is_atomic=False,
                         memory_event_scope='data-hierarchy', gem5_native=dict(
                             response_timestamps_available=True, line_requests=1,
                             admission_count=1, response_count=1,
                             hierarchy_request_count=1, aliased_admissions=0,
                             coalesced=0, terminal_no_ruby=False,
                             native_first_admission_tick=3330,
                             native_last_admission_tick=3330,
                             native_last_response_tick=3663,
                             l1d=dict(accesses=1, hits=1)))

    def test_compares_single_demand_but_does_not_certify_prepare(self):
        row = store_observation(self.sample, self.pair, 333)
        self.assertEqual(row['gem5_admission_to_response_cycles'], 1)
        self.assertEqual(row['fastsim_send_to_sq_release_cycles'], 2)
        self.assertEqual(row['service_difference_cycles'], 1)
        self.assertIsNone(row['native_prepare'])
        self.assertFalse(row['full_lifecycle_verified'])

    def test_missing_native_timestamps_are_not_zero_latency(self):
        self.pair['gem5_native']['response_timestamps_available'] = False
        self.assertEqual(store_observation(self.sample, self.pair, 333)['excluded'],
                         'native-response-timestamps-unavailable')

    def test_split_or_coalesced_requests_are_excluded(self):
        for field in ('line_requests', 'admission_count', 'response_count',
                      'hierarchy_request_count', 'coalesced', 'aliased_admissions'):
            pair = copy.deepcopy(self.pair)
            pair['gem5_native'][field] = 2
            self.assertEqual(store_observation(self.sample, pair, 333)['excluded'],
                             'not-one-unaliased-mandatory-demand')

    def test_zero_native_duration_is_legal(self):
        self.pair['gem5_native']['native_last_response_tick'] = 3330
        self.assertEqual(store_observation(self.sample, self.pair, 333)
                         ['gem5_admission_to_response_cycles'], 0)

    def test_invalid_native_order_or_clock_fails_closed(self):
        self.pair['gem5_native']['native_last_response_tick'] = 3329
        with self.assertRaises(ValueError):
            store_observation(self.sample, self.pair, 333)
        with self.assertRaises(ValueError):
            store_observation(self.sample, self.pair, 0)

    def test_sq_release_must_be_its_own_callback(self):
        self.sample['store_sq_release_cycle'] = 23
        with self.assertRaises(ValueError):
            store_observation(self.sample, self.pair, 333)

    def test_atomic_is_never_an_ordinary_store_witness(self):
        self.pair['is_atomic'] = True
        self.assertEqual(store_observation(self.sample, self.pair, 333)['excluded'],
                         'not-ordinary-store')

    def test_audit_neutrality_does_not_compare_host_time(self):
        a = dict(scope_metrics=dict(cycles=20, throughput=1), threads=[dict(records=2)])
        b = copy.deepcopy(a)
        b['scope_metrics']['throughput'] = 9
        assert_audit_neutral(a, b)
        b['scope_metrics']['cycles'] += 1
        with self.assertRaises(ValueError):
            assert_audit_neutral(a, b)

    def test_request_counter_change_is_not_audit_neutral(self):
        a = dict(scope_metrics={}, threads=[], cores=[dict(buffer_tso_head_demands=2)])
        b = copy.deepcopy(a)
        b['cores'][0]['buffer_tso_head_demands'] = 3
        with self.assertRaises(ValueError):
            assert_audit_neutral(a, b)

    def test_same_pc_wrong_line_or_thread_is_rejected(self):
        sample = dict(pc=100, memory_events=[dict(line=4, write=1, instruction_fetch=0)])
        record = dict(macro_pc=100, thread_id=0, core_id=0, paddr=256, size=8)
        fact = dict(thread_id=0, core_id=0)
        verify_store_identity(sample, record, fact, 64)
        with self.assertRaises(ValueError):
            verify_store_identity(sample, dict(record, paddr=320), fact, 64)
        with self.assertRaises(ValueError):
            verify_store_identity(sample, record, dict(fact, thread_id=1), 64)

    def test_duplicate_and_unordered_samples_are_rejected(self):
        validate_samples([dict(sequence=1), dict(sequence=2)])
        for seqs in ([], [1, 1], [2, 1]):
            with self.assertRaises(ValueError):
                validate_samples([dict(sequence=s) for s in seqs])

    def test_padded_core_name_and_missing_trace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            expected = root / 'board.processor.switch00.core.records.micro.jsonl'
            expected.touch()
            self.assertEqual(raw_trace_path(root, 0, 16), expected)
            with self.assertRaises(ValueError):
                raw_trace_path(root, 1, 16)

    def test_failed_native_conservation_stops_before_pairing(self):
        with patch('audit_buffer_tso_lifecycle.audit_result', return_value=dict(
                native_hierarchy_semantic_comparable=False, target_drain_complete=True)):
            with self.assertRaises(ValueError):
                validate_native_inputs(Path('unused'))

    def test_replaced_stats_cannot_reuse_old_passed_validation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'tao_trace').mkdir()
            manifest = root / 'tao_trace/manifest.txt'
            manifest.write_text('frozen manifest')
            stats = root / 'fastsim.json'
            stats.write_text('{"cycles": 2}')
            report = dict(status='passed', fastsim_output=str(stats), result_dir=str(root),
                          validation_fingerprint=dict(inputs=dict(
                              manifest=dict(path=str(manifest), sha256=sha256(manifest)),
                              archived_fastsim_output=dict(path=str(stats), sha256=sha256(stats)))))
            (root / 'validation.json').write_text(json.dumps(report))
            validate_stats_binding(stats, root)
            stats.write_text('{"cycles": 3}')
            with self.assertRaises(ValueError):
                validate_stats_binding(stats, root)

    def test_prefix_checks_auxiliary_syscall_without_shifting_ordinals(self):
        with tempfile.TemporaryDirectory() as d:
            fst, raw = Path(d) / 'core0.fst', Path(d) / 'records.jsonl'
            syscall = [100, 202, 0, 102, 0, 0, 0, 0, 0, 8193, -1, 0, 0, 7, 7, 7, 7, 0]
            store = [104, 256, 0, 108, 0, 0, 0, 0, 8, 5, 0, 0, 0, 7, 7, 7, 7, 0]
            fst.write_bytes(HEADER.pack(b'FSTRC01\0', 7, HEADER.size, RECORD.size, 0, 2, 0, 0, 0, 0, 0)
                            + RECORD.pack(*syscall) + RECORD.pack(*store))
            Path(str(fst) + '.asmap').write_bytes(struct.pack('<8sIIIIQQQ', b'FSTASM1\0', 1, 48, 16, 0, 2, 1, 0)
                                                + struct.pack('<QQ', 0, 44))
            marker = dict(core_id=0, macro_pc=100, is_syscall=1, syscall_number=202, address_space_id=44)
            row = dict(core_id=0, micro_seq=1, macro_pc=104, paddr=256, size=8, cpl=3, address_space_id=44)
            row.update({name: int(bool(5 & (1 << bit))) for bit, name in FLAG_FIELDS.items()})
            raw.write_text(json.dumps(marker) + '\n' + json.dumps(row) + '\n')
            self.assertEqual(verify_prefix(fst, raw, 0, 2)['matched_records'], 2)
            row['paddr'] = 320
            raw.write_text(json.dumps(marker) + '\n' + json.dumps(row) + '\n')
            with self.assertRaises(ValueError):
                verify_prefix(fst, raw, 0, 2)


if __name__ == '__main__':
    unittest.main()
