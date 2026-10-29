"""A proven fault abandons an unretired prefix, preserving its execution cost."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
class PreciseFaultRetirementTest(unittest.TestCase):
 def test_native_and_model_discard_only_proven_fault_prefix(self):
  source=r'''
#include "gem5-fs/src/cpu/o3/probe/perf_retirement.hh"
#include "include/fastsim/perf_retirement.hpp"
#include <cassert>
int main(){
 using namespace gem5::o3::retirement;
 Ledger n;Member m;m.context=3;m.sequence=1;m.owner=7;m.pc=0x41bd04;
 m.first=true;m.last=false;m.measured=true;m.write=true;n.commit(m);
 assert(!n.notePreciseFault(3,0x41bd08));
 assert(n.notePreciseFault(3,0x41bd04));
 Member e;e.context=3;e.sequence=2;e.owner=7;e.pc=m.pc;e.first=false;e.last=false;
 e.measured=true;e.user=false;e.rom=e.rom_entry=e.hardware_entry=true;e.read=e.write=true;
 n.commit(e);assert(n.lastPreciseFaultAbandonment());
 e.sequence=3;e.rom_entry=e.hardware_entry=false;e.last=true;n.commit(e);
 auto r=n.snapshot({7});assert(r.physical.boundary_discontinuities==0);
 assert(r.user.precise_fault_abandoned_macros==1 && r.kernel.precise_fault_abandoned_macros==0);
 assert(r.physical.retired_macro_instructions==1 && r.physical.retired_uops==3);
 assert(r.physical.perf_retired_loads==0 && r.physical.perf_retired_stores==0);
 assert(r.user.incomplete_macros==1 && r.user.incomplete_uops==1);
 // A retry is a new architectural store, counted only when it completes.
 m.first=m.last=true;m.sequence=4;n.commit(m);
 assert(n.snapshot({7}).physical.perf_retired_stores==1);
 Ledger bad;bad.commit(m);m.first=true;m.last=false;m.sequence=5;bad.commit(m);
 e.rom_entry=e.hardware_entry=true;e.sequence=6;bad.commit(e);
 assert(bad.snapshot({7}).physical.boundary_discontinuities==1);
 using F=fastsim::PerfRetirementLedger;F f;
 f.accept(false,false,true,false,true,true,0);
 f.abandonPreciseFaultPrefix();
 f.accept(false,false,false,true,true,true,0);f.accept(true,false,false,true,true,true,0);
 auto p=f.snapshot().physical;
 assert(p.retired_macro_instructions==1 && p.retired_uops==3 && p.perf_retired_stores==0);
 assert(p.incomplete_macros==1 && p.incomplete_uops==1);
 f.accept(true,false,true,false,true,true,0);
 assert(f.snapshot().physical.perf_retired_stores==1);
 F w;w.accept(false,false,true,false,true,true,0);w.begin_measurement();
 w.abandonPreciseFaultPrefix();
 assert(w.snapshot().physical.incomplete_macros==0 && w.snapshot().physical.incomplete_uops==0);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);(p/'test.cc').write_text(source)
   b=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-I',str(ROOT),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
   self.assertEqual(b.returncode,0,b.stderr)
   r=subprocess.run([str(p/'test')],capture_output=True,text=True,env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
