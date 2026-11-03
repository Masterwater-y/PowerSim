"""Actual CLI source binding and explicit hashing cost, using a tiny local trace."""
import json,os,struct,subprocess,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class PerfCacheEstimateCliTest(unittest.TestCase):
    def test_cli_exports_content_identity_without_certifying_roi(self):
        binary=ROOT/'build/fastsim'
        if not binary.exists():self.skipTest('build/fastsim required')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory);fst=p/'a.fst'
            fst.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,4,0,0,0,0,0)+bytes(256))
            manifest=p/'manifest.txt';manifest.write_text('0 binary a.fst\n')
            config=p/'config.cfg';config.write_text('cores = 1\ncore.model = interval_weave\nsim.interval_scheduler = time_epoch\n')
            command=[str(binary),'simulate','--config',str(config),'--manifest',str(manifest),'--cores','1','--measurement-scope','user',
                     '--perf-cache-input-identity','true','--output',str(p/'out.json')]
            proc=subprocess.run(command,capture_output=True,text=True,env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
            self.assertEqual(proc.returncode,0,proc.stderr)
            result=json.loads((p/'out.json').read_text())
            self.assertIn('functional_source_identity',result)
            identity=result['functional_source_identity']
            self.assertEqual(identity['status'],'complete');self.assertFalse(identity['roi_edges'])
            self.assertFalse(identity['macro_boundary_complete'])
            self.assertGreaterEqual(result['input_identity_wall_seconds'],0)
            import sys;sys.path.insert(0,str(ROOT/'tools'))
            from perf_cache_source import collect_source_identity,validate_identity
            validate_identity(identity,collect_source_identity(manifest))

if __name__=='__main__':unittest.main()
