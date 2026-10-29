"""Real accepted-prefix replay binds raw ordinals across a warmup reset."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class FillAuditCliTests(unittest.TestCase):
    def test_accepted_raw_ordinals_phases_and_target_neutrality(self):
        binary=Path(os.environ.get('FASTSIM_LLC_TEST_BINARY',ROOT/'build/fastsim'))
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory);records=[]
            for i,address in enumerate([0x8000,0,0x8400,0x8000]):
                row=bytearray(64);struct.pack_into('<QQ',row,0,4096+4*i,address)
                struct.pack_into('<HHh',row,48,8 if address else 0,4099 if address else 1,0)
                records.append(row)
            (p/'a.fst').write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,4,0,0,0,0,0)+b''.join(records))
            (p/'manifest.txt').write_text('0 binary-warmup-slice a.fst 0 1 3 1 3\n')
            (p/'config.cfg').write_text('cores = 1\ncore.model = interval_weave\nsim.interval_scheduler = time_epoch\n')
            env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64',FASTSIM_LLC_AUDIT_STRIDE='16')
            cmd=[str(binary),'simulate','--config',str(p/'config.cfg'),'--manifest',str(p/'manifest.txt'),
                 '--cores','1','--measurement-scope','user','--perf-cache-input-identity','true','--output',str(p/'on.json')]
            env['FASTSIM_LLC_AUDIT']=str(p/'audit.jsonl')
            on=subprocess.run(cmd,capture_output=True,text=True,env=env)
            self.assertEqual(on.returncode,0,on.stderr)
            self.assertTrue((p/'audit.jsonl').exists(),'accepted native-line audit is missing')
            rows=[json.loads(s) for s in (p/'audit.jsonl').read_text().splitlines()]
            members=[r for r in rows if r['type']=='member']
            self.assertEqual([r['ordinal'] for r in members],[0,2,3])
            self.assertEqual([r['measurement'] for r in members],[False,True,True])
            self.assertEqual([r['line'] for r in members],[512,528,512])
            self.assertEqual([bool(r['estimate']&4) for r in members],[True,True,False])
            self.assertFalse(rows[-1]['truncated'])
            self.assertTrue(rows[-1]['completed'])
            closure=[r for r in rows if r['type']=='core_end']
            self.assertEqual([(r['records'],r['warmup_records']) for r in closure],[(4,1)])
            a=json.loads((p/'on.json').read_text())
            import hashlib
            self.assertEqual(a['llc_fill_audit']['sha256'],hashlib.sha256((p/'audit.jsonl').read_bytes()).hexdigest())
            self.assertEqual(rows[0]['source_identity'],a['functional_source_identity']['source_identity'])
            env.pop('FASTSIM_LLC_AUDIT');cmd[-1]=str(p/'off.json')
            off=subprocess.run(cmd,capture_output=True,text=True,env=env)
            self.assertEqual(off.returncode,0,off.stderr);b=json.loads((p/'off.json').read_text())
            for key in ['totals','time_accounting','cores','cha','recorded_scheduler','thread_state']:
                self.assertEqual(a[key],b[key],key)
            for key in ['retired_cache','pmu','perf_retirement','perf_cache']:
                self.assertEqual(a['scope_metrics'][key],b['scope_metrics'][key],key)

if __name__=='__main__':unittest.main()
