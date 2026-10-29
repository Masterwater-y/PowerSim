"""Execute runner callbacks with the real phase policy, without importing gem5."""
import ast
import argparse
import base64
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from roi_capture_policy import BoundedWarmup
from spec_rate_checkpoint import RateCheckpointFlow


def functions(*names, **values):
    tree = ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(argparse=argparse, Path=Path, os=os, base64=base64, shlex=shlex, **values)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<runner>', 'exec'), namespace)
    return namespace


class NativeWarmupAfterIdentitiesTest(unittest.TestCase):
    def test_flag_requires_native_tasks_and_positive_budget(self):
        parse = functions('parse_args')['parse_args']
        with patch.object(sys, 'argv', ['runner', '--workload-binary', 'binary',
                          '--participant-cores', '4', '--native-warmup-after-identities']):
            args = parse()
        args.rate_launcher = Path('launcher'); args.rate_copies = 1
        args.native_workers = 4; args.warmup_insts = 500000
        validate = functions('validate_rate_options')['validate_rate_options']
        validate(args)
        for workers, budget in ((0, 500000), (4, 0)):
            args.native_workers = workers; args.warmup_insts = budget
            with self.assertRaises(ValueError): validate(args)

    def test_runner_arms_once_at_identity_checkpoint_and_legacy_arms_at_switch(self):
        for deferred in (True, False):
            with self.subTest(deferred=deferred):
                arms = []; phases = []
                policy = BoundedWarmup(500000, defer_until_identities=deferred)
                cpu = SimpleNamespace(scheduleInstStopAnyThread=arms.append)
                args = SimpleNamespace(rate_copies=1, checkpoint_phase='staged',
                    save_boot_checkpoint=None, warmup_insts=500000,
                    native_warmup_after_identities=deferred)
                callback = functions('on_checkpoint', args=args,
                    checkpoint_flow=RateCheckpointFlow('staged', restored=True),
                    milestone=phases.append,
                    simulator=SimpleNamespace(switch_processor=lambda: None),
                    state=dict(switched=False), warmup_policy=policy,
                    traced=[SimpleNamespace(get_simobject=lambda: cpu)])['on_checkpoint']
                callback()
                self.assertEqual(arms, [] if deferred else [500000])
                callback()
                self.assertEqual(arms, [500000])
                if deferred:
                    self.assertEqual(phases[-2:], ['native-identities-ready', 'warmup-armed'])
                    with self.assertRaises(ValueError): callback()
                else: callback()
                self.assertEqual(arms, [500000])

    def test_guest_exports_opt_in_only_when_requested(self):
        ns = functions('_b64_heredoc', 'boot_script')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            binary = Path(directory)/'binary'; binary.write_bytes(b'probe')
            args = SimpleNamespace(workload_binary=str(binary), input_file=[],
                rate_copies=1, native_workers=4, omp_threads=4, rate_launcher=str(binary),
                omp_proc_bind='', omp_places='', omp_wait_policy='',
                skip_workload_validation=False, warmup_steps=None, workload_args='',
                native_warmup_after_identities=False)
            self.assertNotIn('FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES', ns['boot_script'](args))
            args.native_warmup_after_identities = True
            self.assertIn('FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES=1', ns['boot_script'](args))


if __name__ == '__main__': unittest.main()
