import importlib.util
from pathlib import Path
import unittest
import tempfile
import json

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('campaign', ROOT/'tools/native_speed_campaign.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class CampaignCommandTest(unittest.TestCase):
    def row(self):
        return dict(case_id='c16-801.xz_s',cores=16,native_workers=17,binary='/bin/xz',
                    args='input 2700 hash 0 1 8',input_archive='/data/input.tar',
                    launcher='/bin/launcher',native_omp=True,prepare_args='',
                    target_uops=10000000,warmup_insts=500000,staging_size='512m')

    def test_source_exact_geometry_raw_syscalls_and_fresh_checkpoint(self):
        r=self.row(); argv=mod.build_capture_command(ROOT,ROOT/'tmp/campaign',r,'source',list(range(24)))
        for key,value in [('--mem-size','32GiB'),('--mem-channels','8'),('--participant-cores','16'),
                          ('--native-workers','17'),('--rate-copies','1'),('--warmup-insts','500000'),
                          ('--functional-user-target','10000000'),('--tso-store-buffer-size','114')]:
            self.assertEqual(argv[argv.index(key)+1],value)
        self.assertIn('--functional-syscall-microops',argv)
        self.assertIn('--x86-large-memory',argv)
        self.assertIn('--restore-boot-checkpoint',argv)
        self.assertNotIn('--ruby-functional-backing-store',argv)

    def test_checkpoint_and_source_share_workload_arguments(self):
        r=self.row();a=mod.build_capture_command(ROOT,ROOT/'tmp/campaign',r,'checkpoint',list(range(24)))
        b=mod.build_capture_command(ROOT,ROOT/'tmp/campaign',r,'source',list(range(24)))
        for key in ['--workload-args','--participant-cores','--native-workers','--mem-size']:
            self.assertEqual(a[a.index(key)+1],b[b.index(key)+1])
        self.assertIn('--checkpoint-only',a)
        self.assertIn('--save-boot-checkpoint',a)

    def test_native_identity_boundary_is_explicitly_forwarded(self):
        r=self.row();r['native_warmup_after_identities']=True
        a=mod.build_capture_command(ROOT,ROOT/'tmp/campaign',r,'source',list(range(24)))
        self.assertIn('--native-warmup-after-identities',a)

    def test_source_gate_requires_successful_raw_native_smoke(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory)/'source-ready.json'
            self.assertFalse(mod.source_gate_ready(p))
            p.write_text(json.dumps(dict(valid=False,status='preparing')))
            self.assertFalse(mod.source_gate_ready(p))
            p.write_text(json.dumps(dict(valid=True,raw_syscall_microops=False)))
            with self.assertRaises(ValueError):mod.source_gate_ready(p)
            p.write_text(json.dumps(dict(valid=True,raw_syscall_microops=True)))
            self.assertTrue(mod.source_gate_ready(p))

    def test_resume_only_accepts_checkpoint_with_no_source_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            r=Path(directory);source=r/'source';checkpoint=r/'checkpoint'
            checkpoint.mkdir();(checkpoint/'fastsim-checkpoint.json').write_text('{}')
            self.assertEqual(mod.capture_phases(source,checkpoint,True),['source'])
            with self.assertRaises(RuntimeError):mod.capture_phases(source,checkpoint,False)
            source.mkdir()
            with self.assertRaises(RuntimeError):mod.capture_phases(source,checkpoint,True)


if __name__=='__main__':unittest.main()
