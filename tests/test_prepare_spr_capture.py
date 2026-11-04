"""Native SPR preparation preserves capture bytes and fails closed on populations."""
import gzip
import os
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from test_fs_effective_target import native_spr_ini
from test_p0_contract import row as kernel_row
from test_fst_dynamic_translations import fnv, sidecar

ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct('<8sIIIIQQ4Q')


def dump(path, value):
    path.write_text(json.dumps(value) + '\n')


class PrepareSprCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        self.trace = self.source / 'tao_trace'
        self.oracle = self.trace / 'oracle'
        self.oracle.mkdir(parents=True)
        self.binary = self.source / 'probe-binary'
        self.binary.write_bytes(b'fixture binary identity')
        self.request = dict(schema='fastsim-spr-collection-request-v1', participant_core_ids=[0],
            arguments=dict(workload_binary=str(self.binary), workload_args='--fixture', omp_threads=1,
                extra_cores=0, functional_user_target=4, warmup_insts=0, trace_format='fst',
                functional_include_kernel=True, functional_syscall_microops=True,
                functional_sync=False, thread_state=False),
            binary_identity=dict(binary=str(self.binary), binary_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest()))
        dump(self.source / 'sync-run-request.json', self.request)
        dump(self.source / 'sync-run-result.json', dict(common_end=True, measurement_open=True, switched=True))
        with (self.source / 'config.ini').open('w') as output:
            native_spr_ini(1).write(output)
        (self.source / 'uarch_profile.json').write_text('original producer profile\n')
        self.fst = self.trace / 'native-original-name.records.micro.fst'
        records = []
        for ordinal in range(6):
            hot = bytearray(64)
            struct.pack_into('<Q', hot, 0, 4096 + ordinal // 2)
            struct.pack_into('<H', hot, 50, 1 | 1024 | (2048 if ordinal % 2 else 0))
            records.append(bytes(hot))
        data = b''.join(records)
        self.fst.write_bytes(HEADER.pack(b'FSTRC01\0', 7, 72, 64, 0, 6, 0xff5, 0, 0, 0, 1) + data)
        Path(str(self.fst) + '.deps').write_bytes(struct.pack('<8sIIIIQQQ', b'FSTDEP1\0', 1, 48, 0, 0, 6, 0, 0))
        for suffix, magic, row_bytes in (('.mreq', b'FSTMREQ1', 24), ('.fsem', b'FSTRETV1', 16)):
            Path(str(self.fst) + suffix).write_bytes(struct.pack('<8sIIIIQQQII', magic, 1, 56, 0, 1, 6, 0, fnv(data), row_bytes, 0))
        Path(str(self.fst) + '.xlat.gz').write_bytes(gzip.compress(sidecar(b'', records, 0, 0)))
        frontend = struct.pack('<8sIIIIQ', b'FSTFFE1\0', 1, 32, 0, 80, 0)
        for macro in range(3):
            frontend += b'\x01' + struct.pack('<6QIIB15sII', macro * 2, macro * 2 + 2,
                4096 + macro, 1, 100 + macro, 200 + macro, 8 | 256 | 512, 1023,
                1, b'\x90' + bytes(14), 2, 0)
        frontend += b'\xff' + struct.pack('<QQQ', 6, 3, fnv(data))
        Path(str(self.fst) + '.ffe.gz').write_bytes(gzip.compress(frontend))
        Path(str(self.fst) + '.asmap').write_bytes(struct.pack('<8sIIIIQQQ', b'FSTASM1\0', 1, 48, 16, 0, 6, 1, 0) + struct.pack('<QQ', 0, 1))
        Path(str(self.fst) + '.vmap').write_bytes(struct.pack('<8sIIIIQQII', b'FSTVMP1\0', 1, 48, 32, 0, 6, 1, 12, 0) + struct.pack('<IIQQQ', 1, 0, 0, 2, 0))
        Path(str(self.fst) + '.imap').write_bytes(struct.pack('<8sIIIIQQII', b'FSTIMP3\0', 3, 48, 64, 0, 6, 3, 3, 1) + b''.join(
            struct.pack('<QQQHBB4xQQQQ', 4096 + macro, 4097 + macro, 0, 0, 1, 1, 0, 0, 0, 0) for macro in range(3)))
        Path(str(self.fst) + '.future-unknown-companion').write_bytes(b'preserved future companion')
        self.boundary = dict(schema='tcsim-functional-boundary-v1', core_id=0, measurement_policy='first-core-target-common-end-v1',
            measurement_started=True, measurement_closed=True, stop_reason='first-core-user-target',
            target_records=4, participant_count=1, common_end_tick=100, trigger_core=0,
            target_reached=True, warmup_records=2, measurement_records=4, total_records=6,
            warmup_instructions=1, measurement_instructions=2, total_instructions=3,
            measurement_user_records=4, trace_scope='user-plus-kernel')
        dump(self.trace / 'functional-boundary-core0.json', self.boundary)
        dump(self.oracle / 'cpi-core0.json', dict(core_id=0, n_user=4))
        (self.oracle / 'cpl_class.jsonl').write_text(json.dumps(dict(core_id=0, last_tick=100)) + '\n')
        dump(self.oracle / 'kernel-events-core0.json', kernel_row())

    def run_prepare(self, extra_args=()):
        return subprocess.run([sys.executable, str(ROOT / 'tools/prepare_spr_capture.py'),
            '--source', str(self.source), '--workload-label', 'fixture-probe', *extra_args], capture_output=True, text=True,
            env=dict(os.environ, LD_LIBRARY_PATH='/opt/gcc-11/lib64'))

    def test_prepares_preserves_all_originals_and_uses_actual_native_identity(self):
        before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        run = self.run_prepare()
        self.assertEqual(run.returncode, 0, run.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        request = json.loads((self.source / 'request.json').read_text())
        self.assertEqual(request['profile']['cores'], 1)
        self.assertEqual(request['workload_selection'], {'workload': 'fixture-probe', 'workload_args': '--fixture'})
        self.assertNotIn('tso_store', request['sampling'])
        fields = (self.trace / 'manifest.txt').read_text().split()
        self.assertEqual(fields, ['0', 'binary-warmup-slice', self.fst.name, '0', '1', '2', '2', '4'])
        self.assertTrue((self.source / 'oracle').is_symlink())
        self.assertEqual((self.source / 'oracle').resolve(), self.oracle.resolve())
        target = json.loads((self.source / 'effective-target.json').read_text())
        self.assertEqual(target['core']['pipeline']['instructionQueues']['aggregateEntries'], 248)
        report = json.loads((self.source / 'spr-capture-preparation.json').read_text())
        self.assertEqual(report['status'], 'prepared')
        self.assertEqual(report['thread_state']['status'], 'not-requested')
        self.assertFalse(report['accuracy_evaluated'])
        repeated = self.run_prepare()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)

    def test_rejects_bad_contract_population_feature_or_dependency_before_publication(self):
        for defect in ('schema', 'participants', 'common-end', 'raw-feature', 'deps', 'boolean-core-count', 'string-thread-state'):
            with self.subTest(defect=defect):
                original_req = (self.source / 'sync-run-request.json').read_bytes()
                original_fst = self.fst.read_bytes()
                original_dep = Path(str(self.fst) + '.deps').read_bytes()
                original_cpl = (self.oracle / 'cpl_class.jsonl').read_bytes()
                if defect in ('schema', 'participants'):
                    req = dict(self.request)
                    req['schema' if defect == 'schema' else 'participant_core_ids'] = 'unknown' if defect == 'schema' else [0, 0]
                    dump(self.source / 'sync-run-request.json', req)
                elif defect in ('boolean-core-count', 'string-thread-state'):
                    req = json.loads(json.dumps(self.request))
                    req['arguments']['omp_threads' if defect == 'boolean-core-count' else 'thread_state'] = True if defect == 'boolean-core-count' else 'false'
                    dump(self.source / 'sync-run-request.json', req)
                elif defect == 'common-end':
                    dump(self.oracle / 'cpl_class.jsonl', dict(core_id=0, last_tick=99))
                elif defect == 'raw-feature':
                    h = list(HEADER.unpack(original_fst[:72])); h[6] &= ~256
                    self.fst.write_bytes(HEADER.pack(*h) + original_fst[72:])
                else:
                    Path(str(self.fst) + '.deps').write_bytes(b'truncated')
                run = self.run_prepare()
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse((self.source / 'request.json').exists())
                (self.source / 'sync-run-request.json').write_bytes(original_req)
                self.fst.write_bytes(original_fst)
                Path(str(self.fst) + '.deps').write_bytes(original_dep)
                (self.oracle / 'cpl_class.jsonl').write_bytes(original_cpl)

    def test_declared_memory_facts_require_companion_before_audit(self):
        raw=bytearray(self.fst.read_bytes())
        header=list(HEADER.unpack(raw[:HEADER.size]));header[6] |= 1 << 13
        raw[:HEADER.size]=HEADER.pack(*header);self.fst.write_bytes(raw)
        run=self.run_prepare()
        self.assertNotEqual(run.returncode,0)
        self.assertIn('missing functional companion .sreq',run.stderr)
        self.assertFalse((self.source/'request.json').exists())

    def test_declared_static_uops_require_companion_before_audit(self):
        raw=bytearray(self.fst.read_bytes())
        header=list(HEADER.unpack(raw[:HEADER.size]));header[6] |= 1 << 14
        raw[:HEADER.size]=HEADER.pack(*header);self.fst.write_bytes(raw)
        run=self.run_prepare()
        self.assertNotEqual(run.returncode,0)
        self.assertIn('missing functional companion .uops.gz',run.stderr)
        self.assertFalse((self.source/'request.json').exists())

    def test_requested_instruction_translation_requires_declared_companion(self):
        request = json.loads(json.dumps(self.request))
        request['arguments']['functional_instruction_translations'] = True
        dump(self.source / 'sync-run-request.json', request)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('instruction translation request/feature mismatch', run.stderr)
        raw = bytearray(self.fst.read_bytes())
        header = list(HEADER.unpack(raw[:HEADER.size])); header[6] |= 1 << 15
        raw[:HEADER.size] = HEADER.pack(*header); self.fst.write_bytes(raw)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('missing functional companion .ixlat.gz', run.stderr)
        self.assertFalse((self.source / 'request.json').exists())

    def test_task_clock_requires_explicit_thread_state_capture(self):
        request = json.loads(json.dumps(self.request))
        request['arguments']['task_clock'] = True
        dump(self.source / 'sync-run-request.json', request)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('task clock requires thread-state capture', run.stderr)
        self.assertFalse((self.source / 'request.json').exists())

    def test_requested_asid_map_cannot_silently_fall_back_to_legacy(self):
        request = json.loads(json.dumps(self.request))
        request['arguments']['functional_asid_static_map'] = True
        dump(self.source / 'sync-run-request.json', request)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('requested ASID static map capability', run.stderr)
        self.assertFalse((self.source / 'request.json').exists())

    def test_strict_functional_reader_rejects_corrupted_and_missing_companions(self):
        for suffix in ('.deps', '.mreq', '.fsem', '.xlat.gz', '.ffe.gz', '.imap', '.vmap', '.asmap'):
            for defect in ('missing', 'corrupt'):
                with self.subTest(suffix=suffix, defect=defect):
                    path = Path(str(self.fst) + suffix)
                    original = path.read_bytes()
                    if defect == 'missing':
                        path.unlink()
                    else:
                        path.write_bytes(b'garbage companion')
                    run = self.run_prepare()
                    self.assertNotEqual(run.returncode, 0, suffix)
                    self.assertFalse((self.source / 'request.json').exists())
                    path.write_bytes(original)

    def test_missing_native_auditor_refuses_functional_readiness(self):
        root = self.source / 'unbuilt-fastsim-root'
        root.mkdir()
        run = self.run_prepare(('--fastsim-root', str(root)))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('functional auditor', run.stderr)
        self.assertFalse((self.source / 'request.json').exists())

    def test_whole_stream_bindings_detect_changed_ordinary_records(self):
        original = self.fst.read_bytes()
        # No sparse memory/retirement/dependency row names this ordinary UOP.
        data = bytearray(original)
        data[72 + 5 * 64 + 16] ^= 1
        self.fst.write_bytes(data)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.source / 'request.json').exists())

    def test_untraced_spare_cores_are_explicitly_unsupported(self):
        self.request['arguments']['extra_cores'] = 1
        dump(self.source / 'sync-run-request.json', self.request)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('unsupported untraced spares', run.stderr)

    def add_truncated_sync_region(self, *, invalid=False):
        self.request['arguments']['functional_sync'] = True
        dump(self.source / 'sync-run-request.json', self.request)
        events = [dict(schema='fastsim-functional-sync-v1', boundary='after_record', core_id=0,
            record_ordinal=ordinal, pc=4096, address_space_id=1, thread_id=1,
            region_instance=1, sync_object_id=77, event_kind=kind, generation=0,
            team_size=1, member_index=0, phase='warmup') for ordinal, kind in ((0, 1), (1, 3))]
        if invalid:
            events[1]['thread_id'] = 2  # A truncated prefix cannot excuse wrong master ownership.
        (self.trace / 'functional-sync-core0.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        dump(self.trace / 'functional-sync-metadata-core0.json', dict(schema='fastsim-functional-sync-metadata-v1',
            core_id=0, source_file=self.fst.name, source_record_count=6, event_count=2,
            marker_pc=4096, initial_context='empty', end_reason='first_core_target_common_end'))

    def test_closed_common_end_allows_legitimate_truncated_sync_prefix(self):
        self.add_truncated_sync_region()
        run = self.run_prepare()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads((self.source / 'spr-capture-preparation.json').read_text())
        compiler = next(step for step in report['tools'] if step['tool'] == 'compile_functional_sync.py')
        self.assertIn('--allow-truncated', compiler['options'])
        self.assertEqual(report['functional_sync']['status'], 'available')
        self.assertIn('CORES 1 EVENTS 2', (self.source / 'functional-sync.plan').read_text())

    def test_truncated_flag_does_not_accept_invalid_graph_or_unproven_end(self):
        for defect in ('graph', 'closure'):
            with self.subTest(defect=defect):
                self.add_truncated_sync_region(invalid=defect == 'graph')
                if defect == 'closure':
                    dump(self.source / 'sync-run-result.json', dict(common_end=False, measurement_open=True))
                run = self.run_prepare()
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse((self.source / 'functional-sync.plan').exists())
                self.assertFalse((self.source / 'request.json').exists())

    def test_missing_native_kernel_evidence_keeps_valid_functional_input(self):
        (self.oracle / 'kernel-events-core0.json').unlink()
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        report = json.loads((self.source / 'spr-capture-preparation.json').read_text())
        self.assertTrue(report['functional_input_ready'])
        self.assertFalse(report['native_evidence']['available'])
        self.assertIn('participants', report['native_evidence']['error'])
        self.assertFalse((self.oracle / 'kernel_events.json').exists())

    def test_wrong_warmup_macro_count_refuses_manifest_publication(self):
        self.boundary['warmup_instructions'] = 2
        dump(self.trace / 'functional-boundary-core0.json', self.boundary)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.trace / 'manifest.txt').exists())

    def test_wrong_measurement_macro_count_refuses_manifest_publication(self):
        self.boundary['measurement_instructions'] = 3
        dump(self.trace / 'functional-boundary-core0.json', self.boundary)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.trace / 'manifest.txt').exists())

    def test_zero_measurement_macros_refuses_unusable_slice(self):
        self.boundary['measurement_instructions'] = 0
        dump(self.trace / 'functional-boundary-core0.json', self.boundary)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.trace / 'manifest.txt').exists())

    def test_hardware_config_count_must_match_traced_participants(self):
        with (self.source / 'config.ini').open('w') as output:
            native_spr_ini(4).write(output)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('hardware/source core count mismatch', run.stderr)
        self.assertFalse((self.source / 'effective-target.json').exists())

    def test_functional_companion_full_stream_hashes_are_enforced(self):
        for suffix in ('.mreq', '.fsem', '.xlat.gz', '.ffe.gz'):
            with self.subTest(suffix=suffix):
                path = Path(str(self.fst) + suffix)
                original = path.read_bytes()
                try:
                    raw = bytearray(gzip.decompress(original) if suffix.endswith('.gz') else original)
                    raw[-1 if suffix.endswith('.gz') else 40] ^= 1
                    path.write_bytes(gzip.compress(raw) if suffix.endswith('.gz') else raw)
                    run = self.run_prepare()
                    self.assertNotEqual(run.returncode, 0)
                    self.assertFalse((self.trace / 'manifest.txt').exists())
                finally:
                    path.write_bytes(original)

    def test_requested_thread_state_requires_evidence(self):
        self.request['arguments']['thread_state'] = True
        dump(self.source / 'sync-run-request.json', self.request)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('thread-state', run.stderr)
        self.assertFalse((self.source / 'request.json').exists())

    def test_empty_requested_sync_is_unavailable_without_enabled_empty_plan(self):
        self.request['arguments']['functional_sync'] = True
        dump(self.source / 'sync-run-request.json', self.request)
        dump(self.trace / 'functional-sync-metadata-core0.json', dict(core_id=0,
            source_file=self.fst.name, source_record_count=6, event_count=0))
        (self.trace / 'functional-sync-core0.jsonl').write_text('')
        run = self.run_prepare()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads((self.source / 'spr-capture-preparation.json').read_text())
        self.assertEqual(report['functional_sync']['status'], 'unavailable')
        self.assertFalse((self.source / 'functional-sync.plan').exists())

    def test_native_merge_failure_keeps_functional_input_and_identity_ready(self):
        native = kernel_row()
        native['pmu_source'] = 'unsupported-native-evidence'
        dump(self.oracle / 'kernel-events-core0.json', native)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertTrue((self.trace / 'manifest.txt').is_file(), run.stderr)
        self.assertFalse((self.oracle / 'kernel_events.json').exists())
        report = json.loads((self.source / 'spr-capture-preparation.json').read_text())
        self.assertEqual(report['status'], 'input-prepared-native-gate-failed')
        self.assertTrue(report['functional_input_ready'])
        self.assertTrue(report['target_identity_valid'])
        self.assertFalse(report['native_evidence']['available'])
        self.assertIn('pmu_source', report['native_evidence']['error'])

    def test_zero_observed_warmup_is_preserved(self):
        self.boundary.update(warmup_records=0, warmup_instructions=0,
                             measurement_records=6, measurement_instructions=3)
        dump(self.trace / 'functional-boundary-core0.json', self.boundary)
        run = self.run_prepare()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.trace / 'manifest.txt').read_text().split()[4:], ['0', '3', '0', '6'])

    def test_broken_oracle_symlink_is_rejected_without_publication(self):
        (self.source / 'oracle').symlink_to('missing-native-oracle', target_is_directory=True)
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.source / 'request.json').exists())
        self.assertEqual((self.source / 'oracle').readlink(), Path('missing-native-oracle'))

    def test_conflicting_existing_output_is_preserved_and_reported(self):
        output = self.source / 'request.json'
        output.write_text('pre-existing unrelated output\n')
        run = self.run_prepare()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('conflicting existing output', run.stderr)
        self.assertEqual(output.read_text(), 'pre-existing unrelated output\n')


if __name__ == '__main__':
    unittest.main()
