"""Ruby sequencer demand/prepare capacity and same-line alias behavior."""
import subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SequencerCreditsTest(unittest.TestCase):
 def test_capacity_alias_prepare_and_callbacks(self):
  code=r'''
#include "fastsim/spr_sequencer_credits.hpp"
#include <cassert>
int main(){
 using C=fastsim::SprSequencerCredits;
 C c(2,1);
 auto a=c.admit_demand(5);assert(a && !a->alias);
 auto b=c.admit_demand(5);assert(b && b->alias && c.demand_count()==2 && c.line_count()==1);
 assert(!c.admit_demand(5)); // capacity checked before same-line alias
 bool ordered=false;try {c.complete_demand(b->id);}catch(const std::logic_error&){ordered=true;}assert(ordered);
 auto p=c.admit_prepare();assert(p && !c.admit_prepare());
 c.complete_demand(a->id);assert(c.demand_count()==1 && c.line_count()==1);
 auto d=c.admit_demand(6);assert(d && !d->alias && c.line_count()==2);
 c.complete_prepare(*p);assert(c.prepare_count()==0);
 c.complete_demand(b->id);assert(c.line_count()==1);
 c.complete_demand(d->id);assert(c.demand_count()==0 && c.line_count()==0);
 auto snap=c;auto x=c.admit_demand(7);assert(x && snap.demand_count()==0);
 c=snap;auto y=c.admit_demand(7);assert(y && x->id!=y->id);
 bool stale=false;try {c.complete_demand(x->id);} catch(const std::logic_error&) {stale=true;}assert(stale && c.demand_count()==1);
 C unlimited(1,0);auto u=unlimited.admit_prepare();auto v=unlimited.admit_prepare();assert(u && v);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   s=Path(d)/'t.cc';e=Path(d)/'t';s.write_text(code)
   b=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(s),'-o',str(e)],capture_output=True,text=True)
   self.assertEqual(b.returncode,0,b.stderr)
   p=subprocess.run([str(e)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)
if __name__=='__main__':unittest.main()
