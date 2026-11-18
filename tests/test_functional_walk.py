import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class FunctionalWalkTest(unittest.TestCase):
    def test_paths_reuse_pending_and_source_latency(self):
        code = r'''
#include "fastsim/functional_page_walk.hpp"
#include "fastsim/spr_stlb.hpp"
#include <cassert>
int main() {
 fastsim::SprStlb timed;
 timed.insert(1,0,0x4000,12,false,std::nullopt,100);
 assert(!timed.lookup(1,0,0x4000,true,99));
 assert(timed.lookup(1,0,0x4000,true,100));
 timed.insert(1,0,0x4000,12,false,std::nullopt,50);
 assert(timed.lookup(1,0,0x4000,true,60));
 assert(!timed.lookup(1,1,0x4000,true,100));
 fastsim::SimulatorConfig c;
 c.l1d.hit_latency=2; c.l2.hit_latency=8; c.llc.hit_latency=20;
 c.dram.t_rp=10; c.dram.t_rcd=10; c.dram.t_cl=10; c.dram.burst_cycles=4;
 fastsim::FunctionalPageWalk model(c);
 fastsim::fst::TranslationMapping m;
 m.walk_root=0x1000; m.address_space_id=0x1000; m.page_bits=12; m.levels=4;
 m.flags=fastsim::fst::kTranslationLeafKnown;
 auto first=model.walk(m,0x400000,0);
 assert(first.structural && first.levels==4 && first.ready>0);
 auto shared=model.walk(m,0x401000,first.ready+10);
 assert(shared.ready-(first.ready+10)<first.ready); // adjacent leaf PTE shares cache line
 auto concurrent=model.walk(m,0x400000,0);
 assert(concurrent.ready>=first.ready); // cannot use future fills as ready hits
 m.flags|=fastsim::fst::kTranslationPathKnown;
 for(unsigned i=0;i<4;++i)m.pte_addresses[i]=0x8000+i*4096;
 auto physical=model.walk(m,0x400000,10000);
 assert(!physical.structural && physical.levels==4);
 m.page_bits=21;m.levels=3;
 auto huge=model.walk(m,0x400000,20000);
 assert(huge.levels==3);
 auto slower=c;slower.dram.t_cl=100;
 fastsim::FunctionalPageWalk cold(slower);
 assert(cold.walk(m,0x400000,0).ready>physical.ready-10000);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            src=Path(directory)/'test.cc';exe=Path(directory)/'test';src.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__': unittest.main()
