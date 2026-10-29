import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from compile_functional_sync import compile_plan
from test_functional_sync import fixture

class CompilerTests(unittest.TestCase):
    def test_deleted_and_corrupted_oracle_do_not_change_plan(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); fixture(root)
            a=root/'a.plan'; b=root/'b.plan'; c=root/'c.plan'
            self.assertTrue(compile_plan(root,a)['valid'])
            oracle=root/'oracle/functional-sync-observation.jsonl'
            oracle.write_text('not json and not ordering\n')
            compile_plan(root,b)
            oracle.unlink()
            compile_plan(root,c)
            self.assertEqual(a.read_bytes(),b.read_bytes())
            self.assertEqual(a.read_bytes(),c.read_bytes())

    def test_declared_team_larger_than_core_capacity_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            fixture(root,[(1,0),(3,0),(3,1)],end_reason='first_core_target_common_end')
            for p in root.glob('functional-sync-core*.jsonl'):
                rows=[json.loads(line) for line in p.read_text().splitlines()]
                for e in rows: e['team_size']=3
                p.write_text(''.join(json.dumps(e)+'\n' for e in rows))
            with self.assertRaisesRegex(ValueError,'capacity'):
                compile_plan(root,root/'bad.plan',allow_truncated=True)

    def test_default_observation_validator_still_rejects_corruption(self):
        from validate_functional_sync import validate
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); fixture(root)
            (root/'oracle/functional-sync-observation.jsonl').write_text('bad\n')
            self.assertFalse(validate(root)['valid'])

if __name__=='__main__': unittest.main()
