#!/usr/bin/env python3
"""Exercise recovery edges through the real two-stage simulator.

These tests catch a missing resolution-to-redirect edge, repeated charging of
that edge, and loss at an accepted-checkpoint/chunk boundary. Synthetic timing
is a mechanism check, never a gem5 accuracy result.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('FASTSIM_TEST_BINARY', ROOT / 'build/fastsim'))


class BranchRecoveryTailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        cls.workspace = tempfile.TemporaryDirectory(prefix='branch-tail-test-', dir=ROOT / 'tmp')
        cls.directory = Path(cls.workspace.name)
        cls.serial = 0

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def simulate(self, *, penalty=2, recovery=True, dependent=True,
                 taken=True, chunk=4096, quantum=1024, fast=False,
                 second_branch=False):
        type(self).serial += 1
        directory = self.directory / str(self.serial)
        directory.mkdir()
        common = dict(core_id=0, thread_id=0, cpl=3)
        rows = [
            dict(common, pc=4096, paddr=65536, vaddr=65536, size=8,
                 is_load=True, n_src=0, op_class=84),
            dict(common, pc=4100, is_branch=True, is_branch_cond=True,
                 branch_taken=taken, branch_target=8192,
                 branch_next_pc=8192 if taken else 4104,
                 producer_dists=[1 if dependent else 0, 0, 0, 0],
                 n_src=int(dependent), op_class=1),
            dict(common, pc=8192 if taken else 4104, op_class=1),
        ]
        if second_branch:
            rows[2].update(is_load=True, paddr=131136, vaddr=131136,
                           size=8, op_class=84)
            rows.extend([
                dict(common, pc=8196, is_branch=True, is_branch_cond=True,
                     branch_taken=True, branch_target=12288,
                     branch_next_pc=12288, producer_dists=[1, 0, 0, 0],
                     n_src=1, op_class=1),
                dict(common, pc=12288, op_class=1),
                dict(common, pc=12292, op_class=1),
            ])
        trace = directory / 'trace.jsonl'
        trace.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        manifest = directory / 'manifest.txt'
        manifest.write_text(f'0 gem5-jsonl {trace}\n')
        config = directory / 'test.cfg'
        config.write_text(
            f'config.include = {ROOT}/configs/gem5-fs-native-kernel.cfg\n'
            'measurement.native_kernel_trace = false\n'
            'measurement.scope = user\nsim.cores = 1\n'
            f'sim.interval_max_cycles = {quantum}\n'
            f'sim.chunk_instructions = {chunk}\n'
            f'sim.interval_target_uops = {min(chunk, 256)}\n'
            'core.store_wait_relief = false\n'
            f'branch.mispredict_penalty = {penalty}\n'
            f'core.response_branch_recovery = {str(recovery).lower()}\n'
            f'core.response_materialized_uop_fast_kernel = {str(fast).lower()}\n'
            f'sim.cpi_attribution = {str(not fast).lower()}\n'
            f'core.response_frontier_audit_stride_uops = {0 if fast else 1}\n')
        output = directory / 'stats.json'
        run = subprocess.run(
            [str(BINARY), 'simulate', '--config', str(config), '--manifest',
             str(manifest), '--output', str(output)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stdout)
        return json.loads(output.read_text())

    def assert_recovery_edge(self, document, penalty, count):
        samples = document['cores'][0]['response_frontier_audit']
        by_sequence = {row['sequence']: row for row in samples}
        paired = 0
        for branch in samples:
            if not branch['branch_miss']:
                continue
            successor = by_sequence[branch['sequence'] + 1]
            paired += 1
            self.assertGreaterEqual(
                successor['actual_fetch_cycle'],
                branch['actual_completion_cycle'] + penalty,
                f"missing recovery edge after branch {branch['sequence']}")
        self.assertEqual(paired, count)

    def test_delayed_resolution_preserves_two_cycle_recovery_edge(self):
        self.assert_recovery_edge(self.simulate(), 2, 1)

    def test_recovery_edge_uses_configured_latency(self):
        self.assert_recovery_edge(self.simulate(penalty=7), 7, 1)

    def test_delayed_resolution_does_not_charge_tail_twice(self):
        document = self.simulate()
        samples = document['cores'][0]['response_frontier_audit']
        # This isolated successor has no later frontend gate: one existing
        # two-cycle edge should be exposed after the delayed resolution.
        self.assertEqual(samples[2]['actual_fetch_cycle'],
                         samples[1]['actual_completion_cycle'] + 2)

    def test_recovery_survives_checkpoint_boundary(self):
        document = self.simulate(chunk=2, quantum=1)
        self.assert_recovery_edge(document, 2, 1)
        samples = document['cores'][0]['response_frontier_audit']
        self.assertNotEqual(samples[1]['checkpoint_begin_sequence'],
                            samples[2]['checkpoint_begin_sequence'])

    def test_consecutive_misses_preserve_each_recovery_edge(self):
        self.assert_recovery_edge(self.simulate(second_branch=True), 2, 2)

    def test_correct_prediction_has_no_added_recovery_delay(self):
        off = self.simulate(taken=False, recovery=False)
        on = self.simulate(taken=False)
        self.assertEqual(on['cores'][0]['response_frontier_audit'],
                         off['cores'][0]['response_frontier_audit'])

    def test_already_ready_frontend_does_not_charge_penalty_twice(self):
        off = self.simulate(dependent=False, recovery=False)
        on = self.simulate(dependent=False)
        self.assertEqual(on['cores'][0]['response_frontier_audit'],
                         off['cores'][0]['response_frontier_audit'])

    def test_zero_tail_preserves_resolution_order(self):
        self.assert_recovery_edge(self.simulate(penalty=0), 0, 1)

    def test_fast_kernel_matches_audited_target_cycles_and_pmu(self):
        audit = self.simulate(second_branch=True)
        fast = self.simulate(second_branch=True, fast=True)
        for key in ['sum_core_cycles', 'pmu', 'perf_like_cpi', 'cycles_per_user_uop']:
            self.assertEqual(audit['scope_metrics'][key], fast['scope_metrics'][key])


if __name__ == '__main__':
    unittest.main()
