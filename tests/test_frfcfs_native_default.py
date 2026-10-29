"""Exercise effective native profile policy and exact legacy rollback via CLI."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
R=Path(__file__).resolve().parents[1]
class NativeFrfcfsDefaultTest(unittest.TestCase):
    def effective(self,profile):
        with tempfile.TemporaryDirectory(dir=R/'tmp')as td:
            out=Path(td)/'result.json';env=dict(os.environ);env['LD_LIBRARY_PATH']='/opt/gcc-11/lib64'+(':'+env['LD_LIBRARY_PATH']if env.get('LD_LIBRARY_PATH')else'')
            p=subprocess.run([str(R/'build/fastsim'),'benchmark','--config',str(profile),'--native-kernel-trace','false','--measurement-scope','user','--instructions-per-core','128','--output',str(out)],env=env,text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            return json.loads(out.read_text())
    def test_maintained_profile_selects_arrived_queue_candidate(self):
        d=self.effective(R/'configs/gem5-fs-native-kernel.cfg')['configuration']['dram']
        self.assertEqual(d['scheduler'],'frfcfs')
        self.assertFalse(d['frfcfs_topology_scaled_window'])
        self.assertEqual(d['frfcfs_selection_window'],0)
        self.assertTrue(d['frfcfs_causal_selection'])
    def test_legacy_overlay_restores_old_dram_and_keeps_other_defaults(self):
        old=self.effective(R/'configs/gem5-v28_9-fs-load-ready.cfg')
        rollback=self.effective(R/'configs/gem5-fs-native-kernel-legacy-dram.cfg')
        # Maintained alias also contains unrelated promoted settings; compare
        # the DRAM policy itself to the immutable versioned old profile.
        self.assertEqual(rollback['configuration']['dram'],old['configuration']['dram'])
        # Rollback leaves non-DRAM maintained defaults unchanged.
        current=self.effective(R/'configs/gem5-fs-native-kernel.cfg')
        a=dict(current['configuration']);b=dict(rollback['configuration']);a.pop('dram');b.pop('dram')
        self.assertEqual(a,b)
    def test_canonical_only_shared_service_experiment_remains_runnable(self):
        d=self.effective(R/'configs/gem5-exp-shared-service-constraints.cfg')['configuration']
        self.assertTrue(d['response_shared_service_constraints'])
        self.assertTrue(d['dram']['frfcfs_topology_scaled_window'])
        self.assertEqual(d['dram']['frfcfs_selection_window'],8)

if __name__=='__main__':unittest.main()
