"""Local retired I-fetch certificates are not a global publication watermark."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class IfetchFrontierTest(unittest.TestCase):
 def test_ledger_and_pending_suffix(self):
  code=r'''
#include "fastsim/spr_ifetch_frontier.hpp"
#include <cassert>
#include <limits>
using namespace fastsim;
int main(){
 using F=SprRetiredIfetchFrontier;
 // No ledger response yet: a zero-time unseen fetch is possible.
 auto t=F::tail(true,true,false,0,false);
 assert(t && t->unseen_floor==0);
 auto a=F::combine(*t,true,std::nullopt,0);
 assert(a.status==F::Status::Bounded && a.cycle==0);
 // After zero-latency response at 4, next ledger request is >=5.
 t=F::tail(true,true,true,4,false);assert(t && t->unseen_floor==5);
 a=F::combine(*t,true,2,0);assert(a.status==F::Status::Bounded && a.cycle==2);
 a=F::combine(*t,true,std::nullopt,3);assert(a.status==F::Status::Bounded && a.cycle==8);
 // Resident suffix unknown must not be treated as empty, even when the
 // producer tail is later, or source production is finished.
 assert(F::combine(*t,false,std::nullopt,0).status==F::Status::Unknown);
 auto final=F::tail(true,true,true,4,true);assert(final);
 assert(F::combine(*final,false,std::nullopt,0).status==F::Status::Unknown);
 a=F::combine(*final,true,2,0);assert(a.status==F::Status::Bounded && a.cycle==2);
 assert(F::combine(*final,true,std::nullopt,0).status==F::Status::Finished);
 // Disabled ledger / wrong-path traffic cannot use this certificate.
 assert(!F::tail(false,true,true,4,false));
 assert(!F::tail(true,false,true,4,false));
 assert(!F::tail(true,true,true,std::numeric_limits<uint64_t>::max(),false));
 assert(F::combine(*t,true,std::nullopt,std::numeric_limits<uint64_t>::max()).status==F::Status::Unknown);
 // Immutable segment tail can be copied across chunk handoff/rollback;
 // generated-but-unconsumed earlier fetch always beats the unseen bound.
 auto saved=*t;auto later=F::tail(true,true,true,40,false);assert(later);
 a=F::combine(saved,true,3,0);assert(a.cycle==3);
 a=F::combine(*later,true,3,0);assert(a.cycle==3);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   result=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   result=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
if __name__=='__main__':unittest.main()
