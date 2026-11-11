"""Native sampled diagnostics retain the SPR policy, separate from legacy bits."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
class SprLoadServiceAuditTest(unittest.TestCase):
    def test_native_writer_preserves_policy_masks_at_source_ordinal(self):
        code=r'''
#include "mem/llc_fill_audit.hh"
#include <iostream>
int main(){std::ostringstream out;{
 gem5::llc_fill_audit::Audit a(out,16,32);
 a.begin(0,1);gem5::llc_fill_audit::Native n{256,10,20,true};
 n.spr_miss_mask=1;n.spr_unknown_mask=0;n.spr_estimated_mask=7;n.spr_path_mask=1u<<7;
 a.native(0,42,n);a.member(0,0,42,123,0x400000,1024,8,3,true,25);
 a.endCore(0,100,124,30);
}std::cout<<out.str();}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory);(p/'test.cc').write_text(code)
            b=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-I'+str(ROOT/'gem5-spr/src'),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
            self.assertEqual(b.returncode,0,b.stderr)
            r=subprocess.run([str(p/'test')],capture_output=True,text=True,env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
            self.assertEqual(r.returncode,0,r.stderr)
            rows=[json.loads(x) for x in r.stdout.splitlines()]
        member=next(x for x in rows if x['type']=='member')
        self.assertEqual((member['core'],member['ordinal'],member['line']),(0,123,16))
        self.assertEqual(member['source'],256)
        self.assertEqual([member[k] for k in ['spr_miss_mask','spr_unknown_mask','spr_estimated_mask','spr_path_mask']],[1,0,7,128])
        self.assertTrue(rows[-1]['completed']);self.assertFalse(rows[-1]['truncated'])
if __name__=='__main__':unittest.main()
