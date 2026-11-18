import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import run_native_kernel_fastsim_validation as validator


class QuickValidationTest(unittest.TestCase):
    def test_default_and_explicit_mode(self):
        args = ['validator', '--source-root', '.', '--stats-dir', '.', '--output-dir', '.']
        with patch.object(sys, 'argv', args):
            self.assertEqual(validator.parse_args().validation_mode, 'quick')
        with patch.object(sys, 'argv', args + ['--validation-mode', 'strict']):
            self.assertEqual(validator.parse_args().validation_mode, 'strict')

    def test_quick_source_rejects_missing_or_foreign_identity(self):
        with self.assertRaises(ValueError):
            validator.quick_check_source_identity({}, Path('.'), 1)
        with self.assertRaises(ValueError):
            validator.quick_check_source_identity({'status': 'complete', 'participants': [1],
                'sources': []}, Path('.'), 1)

    def test_quick_summary_never_claims_strict_pass(self):
        summary = validator.quick_summary([], 1)
        self.assertEqual(summary['validation_mode'], 'quick')
        self.assertFalse(summary['formal_validation'])
        self.assertEqual(summary['passed_cases'], 0)
        self.assertEqual(summary['pending_cases'], 1)

    def test_source_paths_and_sizes_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fst = root / 'trace.fst'
            fst.write_bytes(b'abc')
            identity = {'status': 'complete', 'source_identity': 'sha256:test',
                        'participants': [0], 'sources': [{'core_id': 0, 'artifacts': [
                            {'role': 'fst', 'path': str(fst), 'size_bytes': 3}]}]}
            validator.quick_check_source_identity(identity, root, 1)
            fst.write_bytes(b'changed-size')
            with self.assertRaises(ValueError):
                validator.quick_check_source_identity(identity, root, 1)

    def test_mixed_end_policies_are_not_aggregated(self):
        def row(policy):
            return {'cpi': {'status': 'scored-active', 'active_scope': 'workload-task',
                'active_start_policy': 'common-start-v1', 'active_end_policy': policy,
                'absolute_error': 1, 'absolute_relative_error': 1}}
        summary = validator.quick_summary([row('one'), row('two')], 2)
        self.assertEqual(summary['cpi_scored_cases'], 2)
        self.assertIsNone(summary['cpi_mae'])
        self.assertIsNone(summary['cpi_mape_percent'])

if __name__ == '__main__':
    unittest.main()
