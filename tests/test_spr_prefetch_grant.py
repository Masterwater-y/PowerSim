"""Tracked directory-generation and permission semantics for prefetch."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchGrantTest(unittest.TestCase):
 def test_aba_rollback_and_bounded_ownership(self):
  code=r'''
#include "fastsim/spr_prefetch_grant.hpp"
#include <array>
#include <cassert>
using namespace fastsim;
int main(){
 using T=SprPrefetchGrantTable;using B=T::Bitmap;using V=T::Validation;
 T table(1);B grants{};grants[0]=1;
 assert(table.track(42,1,grants)==T::Track::kAdded);
 assert(table.validate(42,0,1)==V::kValid && table.validate(42,1,1)==V::kRevoked);
 assert(table.track(42,2,grants)==T::Track::kAlreadyTracked);
 assert(table.track(43,2,grants)==T::Track::kCapacity);
 auto before=table;uint64_t externally_allocated=1;
 B none{};assert(table.transition(42,1,++externally_allocated,none)==T::Transition::kChanged);
 assert(table.validate(42,0,1)==V::kRevoked);
 assert(table.transition(42,2,++externally_allocated,grants)==T::Transition::kChanged);
 assert(table.validate(42,0,1)==V::kRevoked); // invalidate then regrant is not the original grant
 assert(table.validate(42,0,3)==V::kValid);
 assert(table.transition(42,1,4,none)==T::Transition::kStale);
 // Outer rollback restores tracked generation, but NEVER rewinds ticket source.
 table=before;
 assert(table.validate(42,0,1)==V::kValid);
 assert(table.transition(42,1,++externally_allocated,grants)==T::Transition::kChanged);
 assert(table.validate(42,0,3)==V::kRevoked && table.validate(42,0,4)==V::kValid);
 assert(table.forget(42));assert(table.validate(42,0,4)==V::kUnknown);
 assert(table.transition(42,4,5,none)==T::Transition::kUntracked);
 bool bad=false;try{table.validate(1,256,1);}catch(...){bad=true;}assert(bad);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
