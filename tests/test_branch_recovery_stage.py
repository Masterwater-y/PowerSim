#!/usr/bin/env python3
"""Functional Decode target correction through the real two-stage engine."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('FASTSIM_TEST_BINARY', ROOT / 'build/fastsim'))


class BranchRecoveryStageTest(unittest.TestCase):
    def simulate(self, *, conditional=False, target_alias=False, static_map=True,
                 static_target=None, target_pc=8192, indirect=False, chunk=4096, quantum=1024,
                 fast=False, recovery=True, actual_taken=True, alias_pc=4102):
        with tempfile.TemporaryDirectory(prefix='branch-stage-', dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            common = dict(core_id=0, thread_id=0, cpl=3)
            rows = []
            if static_target is None:
                static_target = target_pc
            # Tiny predictor tables make training deterministic. A different
            # direct PC aliases the BTB tag, supplying a wrong taken target.
            if target_alias:
                for _ in range(8):
                    rows.extend([
                        dict(common, pc=4100, is_branch=True, is_branch_cond=True,
                             branch_taken=True, branch_target=target_pc,
                             branch_next_pc=target_pc, op_class=1),
                        dict(common, pc=target_pc, op_class=1),
                    ])
                rows.append(dict(common, pc=alias_pc, is_branch=True,
                                 branch_taken=True, branch_target=12288,
                                 branch_next_pc=12288, op_class=1))
            rows.extend([
                dict(common, pc=4096, paddr=65536, vaddr=65536, size=8,
                     is_load=True, n_src=0, op_class=84),
                dict(common, pc=4100, is_branch=True,
                     is_branch_cond=conditional, is_branch_indirect=indirect,
                     branch_taken=actual_taken, branch_target=target_pc,
                     branch_next_pc=target_pc if actual_taken else 4102,
                     producer_dists=[1, 0, 0, 0], n_src=1, op_class=1),
                dict(common, pc=target_pc, paddr=131072, vaddr=131072, size=8,
                     is_load=True, op_class=84),
            ])
            source = directory / 'source.jsonl'
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            trace = directory / 'trace.fst'
            converted = subprocess.run([str(BINARY), 'convert-gem5', '--input', str(source),
                                        '--output', str(trace)], capture_output=True, text=True)
            self.assertEqual(converted.returncode, 0, converted.stderr)
            if static_map:
                flags = 1 | (2 if conditional else 0) | (4 if indirect else 32)
                entries = [(4100, 4102, 0 if indirect else static_target, flags, 2)]
                if target_alias:
                    entries.append((alias_pc, alias_pc + 2, 12288, 33, 2))
                header = struct.pack('<8sIIIIQQII', b'FSTIMP1\0', 1, 48, 32, 0,
                                     len(rows), len(entries), 0, 0)
                Path(str(trace) + '.imap').write_bytes(header + b''.join(
                    struct.pack('<QQQHB5x', *entry) for entry in entries))
            manifest = directory / 'manifest.txt'
            manifest.write_text(f'0 binary {trace}\n')
            config = directory / 'test.cfg'
            config.write_text(
                f'config.include = {ROOT}/configs/gem5-fs-native-kernel.cfg\n'
                'measurement.native_kernel_trace = false\nmeasurement.scope = user\n'
                'sim.cores = 1\ncore.store_wait_relief = false\n'
                'branch.speculative_history = false\nbranch.local_entries = 1\n'
                'branch.local_history_entries = 1\nbranch.global_entries = 1\n'
                'branch.choice_entries = 1\nbranch.btb_entries = 1\n'
                'branch.btb_tag_bits = 1\nbranch.btb_set_shift = 0\n'
                'branch.mispredict_penalty = 2\n'
                f'sim.chunk_instructions = {chunk}\nsim.interval_max_cycles = {quantum}\n'
                f'sim.interval_target_uops = {min(chunk, 256)}\n'
                f'core.response_branch_recovery = {str(recovery).lower()}\n'
                f'core.response_materialized_uop_fast_kernel = {str(fast).lower()}\n'
                f'sim.cpi_attribution = {str(not fast).lower()}\n'
                f'core.response_frontier_audit_stride_uops = {0 if fast else 1}\n')
            output = directory / 'stats.json'
            result = subprocess.run([str(BINARY), 'simulate', '--config', str(config),
                                     '--manifest', str(manifest), '--output', str(output)],
                                    capture_output=True, text=True, timeout=30, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(output.read_text())

    def assert_decode_redirect(self, document):
        rows = document['cores'][0]['response_frontier_audit']
        branch, successor = rows[-2:]
        self.assertEqual(branch['branch_miss'], 1)
        self.assertLess(successor['actual_fetch_cycle'], branch['actual_completion_cycle'],
                        'Decode-correctable direct target must not wait for backend completion')
        self.assertGreaterEqual(successor['actual_issue_cycle'], successor['actual_fetch_cycle'])
        return rows

    def test_cold_direct_target_corrects_at_decode(self):
        self.assert_decode_redirect(self.simulate())

    def test_predicted_taken_conditional_wrong_target_corrects_at_decode(self):
        self.assert_decode_redirect(self.simulate(conditional=True, target_alias=True))

    def test_conditional_direction_error_still_waits_for_backend(self):
        document = self.simulate(conditional=True)
        rows = document['cores'][0]['response_frontier_audit']
        self.assertGreaterEqual(rows[-1]['actual_fetch_cycle'],
                                rows[-2]['actual_completion_cycle'] + 2)

    def test_zero_displacement_target_correction_already_reaches_fallthrough(self):
        # Native Decode repairs the target; DynInst::mispredicted compares
        # the full advanced PC, not PredTaken. A zero-displacement target
        # equals fallthrough, so there is no later backend PC redirect.
        self.assert_decode_redirect(self.simulate(
            conditional=True, target_alias=True, target_pc=4102,
            alias_pc=4106, actual_taken=False))

    def test_missing_static_target_preserves_existing_timing(self):
        absent = self.simulate(static_map=False)
        inconsistent = self.simulate(static_target=12288)
        self.assertEqual(absent['cores'][0]['response_frontier_audit'],
                         inconsistent['cores'][0]['response_frontier_audit'])

    def test_indirect_target_still_waits_for_backend(self):
        document = self.simulate(indirect=True)
        rows = document['cores'][0]['response_frontier_audit']
        self.assertGreaterEqual(rows[-1]['actual_fetch_cycle'],
                                rows[-2]['actual_completion_cycle'] + 2)

    def test_checkpointed_decode_redirect_keeps_frontend_discovery(self):
        document = self.simulate(chunk=2, quantum=1)
        rows = document['cores'][0]['response_frontier_audit']
        self.assertNotEqual(rows[-2]['checkpoint_begin_sequence'],
                            rows[-1]['checkpoint_begin_sequence'])
        # Closing an interval may advance global time beyond this branch's
        # execution. It must still retain Decode ownership rather than turn
        # a delayed load operand into a branch-resolution response delay.
        totals = document['totals']
        self.assertEqual(totals['branch_recovery_decode_redirects'], 1)
        self.assertEqual(totals['branch_recovery_backend_redirects'], 0)
        self.assertEqual(totals['branch_recovery_response_delayed_mispredictions'], 0)

    def test_decode_tail_is_applied_once_with_resident_target(self):
        document = self.simulate(target_pc=4104)
        rows = self.assert_decode_redirect(document)
        self.assertEqual(rows[-1]['actual_fetch_cycle'],
                         rows[-2]['actual_fetch_cycle'] + 3)

    def test_fast_kernel_preserves_decode_target_timing(self):
        audited = self.simulate(target_alias=True, conditional=True)
        fast = self.simulate(target_alias=True, conditional=True, fast=True)
        for key in ['sum_core_cycles', 'pmu', 'perf_like_cpi']:
            self.assertEqual(audited['scope_metrics'][key], fast['scope_metrics'][key])


if __name__ == '__main__':
    unittest.main()
