import json
import struct
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from test_raw_syscall_microops import raw_trace
from audit_fst_syscall_metadata import audit_file


def fixture(source):
    trace = source / 'tao_trace'; trace.mkdir()
    oracle = source / 'oracle'; oracle.mkdir()
    (source / 'board.pc.com_1.device').write_text(
        '[fastsim-rate] post-switch-census begin\n'
        'copy=0 pid=101 Threads: 2\n'
        'copy=0 task_tid=101 task_tgid=101\n'
        'copy=0 task_tid=202 task_tgid=101\n'
        '[fastsim-rate] post-switch-census end\n')
    def write(path, value): path.write_text(json.dumps(value))
    write(trace / 'functional-boundary-core0.json', dict(warmup_records=1, total_records=19))
    write(trace / 'thread-state-metadata-core0.json', dict(source_file='core0.fst'))
    (trace / 'thread-state-core0.jsonl').write_text(json.dumps(dict(
        event_kind='identity', os_tid=202, os_tgid=101, phase='warmup', record_ordinal=0)) + '\n')
    write(trace / 'trace.json', dict(functional_boundaries={'0': dict(warmup_records=1)}))
    write(oracle / 'thread-state-accounting.json', dict(valid=True, ownership_status='complete',
        unknown_cycles=0, task_macro_instructions=0, per_core=[dict(owner_spans=[
            dict(owner_pid=202, start_tick=100, end_tick=120)])]))
    write(oracle / 'task-pmu-core0.json', dict(owners=[dict(os_tid=202, retired_uops=18)]))
    raw_trace(trace / 'core0.fst')
    return dict(cores=1, native_workers=1, rate_copies=1)


def capture_fixture(source):
    from prepare_native_speed_source import sha256
    binary = source / 'workload'; binary.write_bytes(b'fixture-workload')
    case = dict(cores=1, native_workers=1, binary=str(binary), warmup_wall_seconds=0,
                native_warmup_after_identities=True)
    args = dict(omp_threads=1, extra_cores=0, rate_copies=1, native_workers=1,
        functional_user_target=10000000, warmup_insts=500000, mem_size='32GiB', mem_channels=8,
        x86_large_memory=True, ruby_functional_backing_store=False, tso_store='buffer',
        tso_store_buffer_size=114, functional_syscall_microops=True, trace_format='fst',
        thread_state=True, workload_binary=str(binary), native_warmup_after_identities=True)
    request = dict(arguments=args, binary_identity=dict(binary_sha256=sha256(binary)))
    (source / 'sync-run-request.json').write_text(json.dumps(request))
    (source / 'capture-receipt.json').write_text(json.dumps(dict(returncode=0, guard_reason=None,
        measurement_open=True, detailed_warmup_wall_seconds=901)))
    (source / 'capture.log').write_text('CHECKPOINT marker: switch KVM -> O3+Ruby\n'
        'native identities ready: warmup instruction budget armed\n'
        'WARMUP instruction budget reached: measurement window open\n'
        'functional_user_target reached: common-end, finish\n')
    (source / 'board.pc.com_1.device').write_text('[fastsim-native] identities-ready workers=1\n')
    phases = [dict(phase=name, tick=tick, wall_time=100 + i) for i, (name, tick) in enumerate((
        ('switch-complete', 10), ('native-identities-ready', 20), ('warmup-armed', 20),
        ('measurement-open', 30), ('common-end', 40)))]
    (source / 'capture-phases.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in phases))
    return case, request, phases


class NativeSpeedPreparationTest(unittest.TestCase):
    def test_cli_exposes_explicit_source_case_and_frozen_root(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/prepare_native_speed_source.py'), '--help'],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        for flag in ('--source', '--case-json', '--frozen-root'):
            self.assertIn(flag, result.stdout)

    def test_scope_uses_real_raw_metadata_and_preserves_unknown_entry_fields(self):
        from prepare_native_speed_source import audit_process_scope
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory); case = fixture(source)
            result = audit_process_scope(source, case, audit_file)
            self.assertTrue(result['valid'])
            self.assertEqual(result['source_syscall_numbers'], {0: 1})
            metadata = result['syscall_metadata_audits'][0]
            self.assertEqual(metadata['field_coverage'], {})
            self.assertEqual(metadata['missing_return_by_phase'], {'warmup': 1})
            self.assertEqual(result['unmarked_members'][101]['retired_uops'], 0)

    def test_warmup_task_creation_is_rejected_without_any_compact_marker(self):
        from prepare_native_speed_source import audit_process_scope
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory); case = fixture(source)
            path = source / 'tao_trace/core0.fst'
            data = bytearray(path.read_bytes())
            struct.pack_into('<Q', data, 72 + 19 * 64 + 24, 56)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'new task syscall'):
                audit_process_scope(source, case, audit_file)

    def test_retirement_clock_preserves_independent_process_owner_scope(self):
        from prepare_native_speed_source import audit_process_scope
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory); case = fixture(source)
            path = source / 'oracle/thread-state-accounting.json'
            account = json.loads(path.read_text())
            proxy_rows = account['per_core']
            proxy_rows[0]['core_id'] = 0
            account['recorded_owner_proxy'] = dict(per_core=proxy_rows)
            account['per_core'] = [dict(core_id=0, active_cycles=20)]
            account['active_end_policy'] = 'retired-stream-completion-v1'
            path.write_text(json.dumps(account))
            result = audit_process_scope(source, case, audit_file)
            self.assertTrue(result['valid'])
            self.assertEqual(result['owner_span_source'], 'recorded_owner_proxy')
            self.assertEqual(json.loads(path.read_text()), account)
            # A same-process member absent from the marked compute set still
            # invalidates whole-process coverage when it actually owns a core.
            proxy_rows[0]['owner_spans'].append(dict(owner_pid=101, start_tick=120, end_tick=121))
            path.write_text(json.dumps(account))
            with self.assertRaisesRegex(ValueError, 'unmarked same-process'):
                audit_process_scope(source, case, audit_file)

    def test_malformed_retained_owner_proxy_cannot_fallback_to_active_counters(self):
        from prepare_native_speed_source import audit_process_scope
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory); case = fixture(source)
            path = source / 'oracle/thread-state-accounting.json'
            account = json.loads(path.read_text())
            account['recorded_owner_proxy'] = dict(per_core=[])
            path.write_text(json.dumps(account))
            with self.assertRaisesRegex(ValueError, 'owner span participants'):
                audit_process_scope(source, case, audit_file)

    def test_unmarked_process_work_and_late_identity_fail_closed(self):
        from prepare_native_speed_source import audit_process_scope
        for change in ('work', 'identity'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                source = Path(directory); case = fixture(source)
                if change == 'work':
                    path = source / 'oracle/task-pmu-core0.json'
                    value = json.loads(path.read_text())
                    value['owners'].append(dict(os_tid=101, retired_uops=1))
                    path.write_text(json.dumps(value))
                else:
                    path = source / 'tao_trace/thread-state-core0.jsonl'
                    value = json.loads(path.read_text()); value['record_ordinal'] = 1
                    path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'unmarked|identity missed'):
                    audit_process_scope(source, case, audit_file)

    def test_disabled_warmup_wall_guard_allows_observed_long_warmup(self):
        from prepare_native_speed_source import validate_capture, sha256
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            binary = source / 'workload'; binary.write_bytes(b'fixture-workload')
            case = dict(cores=1, native_workers=1, binary=str(binary), warmup_wall_seconds=0)
            args = dict(omp_threads=1, extra_cores=0, rate_copies=1, native_workers=1,
                functional_user_target=10000000, warmup_insts=500000, mem_size='32GiB', mem_channels=8,
                x86_large_memory=True, ruby_functional_backing_store=False, tso_store='buffer',
                tso_store_buffer_size=114, functional_syscall_microops=True, trace_format='fst',
                thread_state=True, workload_binary=str(binary))
            (source / 'sync-run-request.json').write_text(json.dumps(dict(arguments=args,
                binary_identity=dict(binary_sha256=sha256(binary)))))
            receipt = source / 'capture-receipt.json'
            receipt.write_text(json.dumps(dict(returncode=0, guard_reason=None,
                measurement_open=True, detailed_warmup_wall_seconds=901)))
            (source / 'capture.log').write_text('CHECKPOINT marker: switch KVM -> O3+Ruby\n'
                'WARMUP instruction budget reached: measurement window open\n'
                'functional_user_target reached: common-end, finish\n')
            (source / 'board.pc.com_1.device').write_text('[fastsim-native] identities-ready workers=1\n')
            self.assertEqual(validate_capture(source, case)['arguments']['warmup_insts'], 500000)
            case['warmup_wall_seconds'] = 600
            with self.assertRaisesRegex(ValueError, 'warmup wall'):
                validate_capture(source, case)
            case['warmup_wall_seconds'] = 0
            for invalid in (float('nan'), float('inf'), -1):
                receipt.write_text(json.dumps(dict(returncode=0, guard_reason=None,
                    measurement_open=True, detailed_warmup_wall_seconds=invalid)))
                with self.assertRaisesRegex(ValueError, 'warmup wall'):
                    validate_capture(source, case)

    def test_identity_warmup_opt_in_requires_request_and_ordered_phase_proof(self):
        from prepare_native_speed_source import validate_capture
        for change in ('valid', 'argument', 'armed-log', 'missing-phase', 'before-switch',
                       'early-measurement', 'duplicate', 'bad-tick', 'bad-wall'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                source = Path(directory); case, request, phases = capture_fixture(source)
                if change == 'argument':
                    del request['arguments']['native_warmup_after_identities']
                    (source / 'sync-run-request.json').write_text(json.dumps(request))
                elif change == 'armed-log':
                    path = source / 'capture.log'
                    path.write_text(path.read_text().replace('native identities ready: warmup instruction budget armed\n', ''))
                elif change == 'missing-phase':
                    phases.pop(1)
                elif change == 'before-switch':
                    phases[0], phases[1] = phases[1], phases[0]
                elif change == 'early-measurement':
                    phases[2], phases[3] = phases[3], phases[2]
                elif change == 'duplicate':
                    phases.insert(2, dict(phases[1]))
                elif change == 'bad-tick':
                    phases[2]['tick'] = 19
                elif change == 'bad-wall':
                    phases[2]['wall_time'] = 100
                (source / 'capture-phases.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in phases))
                if change == 'valid':
                    self.assertEqual(validate_capture(source, case)['arguments']['warmup_insts'], 500000)
                else:
                    with self.assertRaisesRegex(ValueError, 'native_warmup_after_identities|identities|warmup phase'):
                        validate_capture(source, case)

    def test_failed_preparation_invalidates_existing_ready_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'source-ready.json').write_text('{"valid": true}')
            case = source / 'case.json'; case.write_text('{}')
            result = subprocess.run([sys.executable, str(ROOT / 'tools/prepare_native_speed_source.py'),
                '--source', str(source), '--case-json', str(case), '--frozen-root', str(source / 'missing')],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads((source / 'source-ready.json').read_text())['valid'])



if __name__ == '__main__':
    unittest.main()
