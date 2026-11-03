"""Opt-in policy without importing gem5's embedded Python runtime."""
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/run_gem5_sync_probe.py'


def runner_functions(*names):
    tree = ast.parse(RUNNER.read_text())
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(json=json, os=__import__('os'))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER), 'exec'), namespace)
    return namespace


class LargeMemoryOptionsTest(unittest.TestCase):
    def policy(self, size='3GiB', enabled=False, backing=False):
        ns = runner_functions('parse_size_bytes', 'validate_x86_memory_options')
        self.assertIn('validate_x86_memory_options', ns, 'missing explicit memory policy')
        return ns['validate_x86_memory_options'](SimpleNamespace(
            mem_size=size, x86_large_memory=enabled,
            ruby_functional_backing_store=backing))

    def test_cli_defaults_to_legacy_and_accepts_explicit_flag(self):
        import argparse
        import sys
        from unittest.mock import patch
        ns = runner_functions('parse_args')
        ns.update(argparse=argparse, Path=Path)
        argv = ['runner', '--workload-binary', '/unused', '--participant-cores', '8']
        with patch.object(sys, 'argv', argv):
            args = ns['parse_args']()
        self.assertFalse(getattr(args, 'x86_large_memory', None),
                         'default must be an explicit false option')
        self.assertTrue(hasattr(args, 'x86_large_memory'), 'missing CLI option')
        with patch.object(sys, 'argv', argv + ['--x86-large-memory', '--mem-size', '32GiB']):
            self.assertTrue(ns['parse_args']().x86_large_memory)

    def test_small_memory_preserves_legacy_with_or_without_flag(self):
        for size in ('1GiB', '3GiB', '3072MiB'):
            for enabled in (False, True):
                self.assertFalse(self.policy(size, enabled))
        self.assertFalse(self.policy('3GiB', True, True))

    def test_large_memory_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, '--x86-large-memory'):
            self.policy('32GiB')
        self.assertTrue(self.policy('32GiB', True))

    def test_active_large_memory_rejects_partial_backing_store(self):
        with self.assertRaisesRegex(ValueError, 'backing.store'):
            self.policy('32GiB', True, True)

    def test_nonpositive_capacity_is_rejected(self):
        for size in ('0GiB', '-1GiB'):
            with self.assertRaisesRegex(ValueError, 'positive'):
                self.policy(size, True)

    def test_checkpoint_identity_distinguishes_requested_and_active_modes(self):
        ns = runner_functions('parse_size_bytes', 'validate_x86_memory_options',
                              'x86_memory_identity')
        self.assertIn('x86_memory_identity', ns, 'missing memory identity boundary')
        args = SimpleNamespace(mem_size='3GiB', x86_large_memory=False,
                               ruby_functional_backing_store=False)
        self.assertEqual(ns['x86_memory_identity'](args),
                         dict(x86_large_memory=False, x86_large_memory_active=False))
        args.x86_large_memory = True
        self.assertEqual(ns['x86_memory_identity'](args),
                         dict(x86_large_memory=True, x86_large_memory_active=False))
        args.mem_size = '32GiB'
        self.assertEqual(ns['x86_memory_identity'](args),
                         dict(x86_large_memory=True, x86_large_memory_active=True))

    def test_legacy_selection_does_not_import_optional_components(self):
        ns = runner_functions('select_x86_memory_components')
        self.assertIn('select_x86_memory_components', ns, 'missing component selection')
        class OriginalBoard:
            pass
        class OriginalMemory:
            pass
        ns.update(X86Board=OriginalBoard, ChanneledMemory=OriginalMemory)
        # gem5 is unavailable in normal Python. Any accidental optional import
        # on this path fails instead of being swallowed by a fake gem5 module.
        self.assertEqual(ns['select_x86_memory_components'](False),
                         (OriginalBoard, OriginalMemory))

    def test_profile_discloses_split_ranges_and_controller_count(self):
        ns = runner_functions('parse_size_bytes', 'validate_x86_memory_options',
                              'effective_sq', 'write_uarch_profile')
        args = SimpleNamespace(omp_threads=8, extra_cores=0, clk='3GHz',
            tso_store='buffer', tso_store_buffer_size=114, sq_entries=32,
            l1d_size='32KiB', l1i_size='32KiB', l2_size='1MiB', l3_size='8MiB',
            l1d_assoc=8, l1i_assoc=8, l2_assoc=8, l3_assoc=16, num_l3_banks=8,
            dtlb_entries=64, mem_channels=8, mem_size='32GiB',
            x86_large_memory=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path = ns['write_uarch_profile'](args, str(Path(directory)/'tao_trace'))
            dram = json.loads(Path(path).read_text())['dram']
        self.assertEqual(dram['num_channels'], 16)
        self.assertEqual(dram['channels_per_range'], 8)
        self.assertEqual(dram['physical_ranges'], [
            {'start': 0, 'size': 3 * 2**30},
            {'start': 4 * 2**30, 'size': 29 * 2**30},
        ])


class LargeMemoryPackagingTest(unittest.TestCase):
    def test_manifest_feature_fingerprints_match_installed_sources_and_patch(self):
        import hashlib
        package = ROOT / 'gem5-collection'
        manifest = json.loads((package / 'manifest.json').read_text())
        self.assertIn('features', manifest, 'missing packaged large-memory feature')
        feature = manifest['features']['x86_large_memory']
        self.assertEqual(feature['runtime_flag'], '--x86-large-memory')
        self.assertFalse(feature['default_enabled'])
        for relative, expected in feature['overlay_files'].items():
            packaged = package / 'overlay' / relative
            self.assertEqual(hashlib.sha256(packaged.read_bytes()).hexdigest(), expected)
            self.assertEqual(packaged.read_bytes(), (ROOT / 'gem5-fs' / relative).read_bytes())
        patch = next(entry for entry in manifest['apply']['patches']
                     if entry['file'] == feature['registration_patch'])
        self.assertEqual(hashlib.sha256((package / patch['file']).read_bytes()).hexdigest(),
                         patch['sha256'])


if __name__ == '__main__':
    unittest.main()
