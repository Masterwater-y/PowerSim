"""Model-owned source-batch identity does not alter frontend service."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class BatchIdentityTest(unittest.TestCase):
    def test_model_batch_and_fragment_identity(self):
        code=r'''
#include "fastsim/spr_frontend.hpp"
#include <cassert>
using namespace fastsim;
SprFrontendMacro m(uint64_t pc,unsigned n=1){SprFrontendMacro x;x.pc=pc;x.code_identity=pc+1;x.instruction_bytes=4;x.uops=n;return x;}
SprFrontendDelivery one(SprFrontend& f,SprFrontendMacro x,uint64_t ack_delay=0){
 f.begin_macro(x,0,0);auto d=f.deliver_next(0);f.accepted(d.cycle+ack_delay);return d;
}
int main(){
 SprFrontend f;
 auto first=one(f,m(0x1000));assert(first.model_batch_id!=0);
 for(unsigned i=1;i<6;++i){auto x=one(f,m(0x1000+4*i));assert(x.model_batch_id==first.model_batch_id && x.cycle==first.cycle);}
 auto seventh=one(f,m(0x1018));assert(seventh.model_batch_id!=first.model_batch_id && seventh.cycle>first.cycle);
 SprFrontend g;auto a=one(g,m(0x2000));g.reset_counters();auto b=one(g,m(0x2004));assert(a.model_batch_id==b.model_batch_id);
 SprFrontend snapshot=g;assert(one(g,m(0x2008)).model_batch_id==one(snapshot,m(0x2008)).model_batch_id);
 auto micro=m(0x200c);micro.microcode=true;assert(one(g,micro).model_batch_id!=a.model_batch_id);
 SprFrontend h;auto longmacro=m(0x3000,65);h.begin_macro(longmacro,0,0);
 while(h.active()){auto x=h.deliver_next(0);assert(x.model_batch_id==0);h.accepted(x.cycle);}
 auto next=one(h,m(0x3004));assert(next.model_batch_id!=0);
 SprFrontend p;one(p,m(0x4000),100);auto delayed=one(p,m(0x4004));
 assert(delayed.cycle>=100);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),str(ROOT/'src/spr_frontend.cpp'),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
    def test_chunk_splitting_keeps_batch_and_service(self):
        code=r'''
#include "fastsim/spr_frontend.hpp"
#include <cassert>
#include <tuple>
#include <vector>
using namespace fastsim;
using Record=std::tuple<uint64_t,uint64_t,uint32_t,unsigned,bool,bool>;
std::vector<Record> run(unsigned chunk) {
 SprFrontend f;std::vector<Record> out;
 for(unsigned i=0;i<96;++i){
  SprFrontendMacro m;m.pc=0x1000+(i%48)*4;m.code_identity=m.pc+1;
  m.instruction_bytes=4;m.uops=i==22?65:1;m.microcode=i==44;
  f.begin_macro(m,(i/20)*20,(i/20)*20);
  while(f.active()){
   const auto x=f.deliver_next(0);
   out.emplace_back(x.model_batch_id,x.cycle,x.slot_cost,unsigned(x.source),x.macro_begin,x.macro_end);
   f.accepted(x.cycle+(i%13==0?9:0));
  }
  if ((i+1)%chunk==0) {SprFrontend snapshot=f;f=snapshot;}
 }
 return out;
}
int main(){assert(run(1)==run(3));assert(run(1)==run(32));}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),str(ROOT/'src/spr_frontend.cpp'),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
