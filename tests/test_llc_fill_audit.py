"""Observer output must preserve real completion, sampling and truncation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class FillAuditTests(unittest.TestCase):
    def test_native_physical_fill_member_and_cap(self):
        # A lost completion hook or sampling on byte rather than line address
        # must fail this literal sequence; no mock simulator is involved.
        code=r'''
#include <sstream>
#include <iostream>
#include "mem/llc_fill_audit.hh"
int main(){
 std::ostringstream out;
 {
  gem5::llc_fill_audit::Audit a(out,16,6);
  a.begin(0,5);
  a.fill(64,6,0,false,-1,0,10); // unsampled line1
  a.fill(1024,6,0,false,-1,0,20); // actual completed prepare fill
  a.native(0,40,{128,12,25,true});
  a.member(0,0,40,123,4096,1024,8,3,true,30);
  a.evict(1024,31);
  a.squash(0,40,50,32);
  a.endCore(0,100,124,33);
  a.fill(2048,1,0,true,0,50,34); // capped, cannot look complete
 }
 std::cout<<out.str();
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            p=Path(d);(p/'x.cc').write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-I',str(ROOT/'gem5-fs/src'),
                '-I',str(ROOT/'gem5-fs/taogen/shared'),str(p/'x.cc'),'-o',str(p/'x')],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(p/'x')],capture_output=True,text=True,
                env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
            self.assertEqual(run.returncode,0,run.stderr)
            rows=[json.loads(s) for s in run.stdout.splitlines()]
        self.assertEqual([r['type'] for r in rows],['begin','core_begin','fill','member','evict','squash','core_end','end'])
        self.assertEqual(rows[2]['role'],6)
        self.assertEqual((rows[3]['ordinal'],rows[3]['line'],rows[3]['response_tick']),(123,16,25))
        self.assertTrue(rows[-1]['truncated'])
        self.assertEqual(rows[-1]['omitted'],1)

    def test_model_writer_is_off_without_opt_in(self):
        code=r'''
#include "fastsim/llc_fill_audit.hpp"
int main(){fastsim::ModelLlcFillAudit a; return a.enabled()?1:0;}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            p=Path(d);(p/'x.cc').write_text(code)
            b=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-I',str(ROOT/'include'),str(p/'x.cc'),'-o',str(p/'x')],capture_output=True,text=True)
            self.assertEqual(b.returncode,0,b.stderr)
            env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64');env.pop('FASTSIM_LLC_AUDIT',None)
            self.assertEqual(subprocess.run([str(p/'x')],env=env).returncode,0)

if __name__=='__main__':unittest.main()
