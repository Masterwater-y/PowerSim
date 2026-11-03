import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('collect_tail_timing', ROOT / 'tools/collect_tail_timing.py')
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class FrozenCommandTests(unittest.TestCase):
    def test_modern_argv_keeps_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            argv = ['/path with spaces/gem5', '-d', '/output', '/config.py']
            (p / 'command.json').write_text(json.dumps(argv))
            self.assertEqual(MOD.load_frozen_command(p), argv)

    def test_legacy_log_still_supported(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'run.log').write_text('header\ncommand line: gem5 -d out "config with space.py"\n')
            self.assertEqual(MOD.load_frozen_command(p), ['gem5', '-d', 'out', 'config with space.py'])

    def test_relative_manifest_resolves_against_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'tao_trace').mkdir()
            manifest = p / 'tao_trace/manifest.txt'
            manifest.write_text('# comment\n\n0 fastsim-binary-warmup-slice core0.fst 0 1 2 3 4\n')
            self.assertEqual(MOD.source_from_manifest(manifest), p)

    def test_malformed_argv_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'command.json').write_text('{"shell": "gem5"}')
            with self.assertRaises(ValueError):
                MOD.load_frozen_command(p)

    def test_missing_command_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                MOD.load_frozen_command(Path(d))


if __name__ == '__main__':
    unittest.main()
