"""Bounded store group: fixed model eligibility and joint SQ release."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class PendingGroupTest(unittest.TestCase):
 def test_group_model_and_boundaries(self):
  code=r'''
#include "fastsim/spr_pending_store_group.hpp"
#include <cassert>
using namespace fastsim;
int main(){
 using G=SprPendingStoreGroup;
 G g(2,4,100,0,8,1,1);
 assert(g.hold(0,0,3,10));
 assert(!g.ready());
 // Existing SQ occupant is not available at dispatch=20: no state change.
 assert(!g.hold(1,25,20,30));assert(g.held()==1);
 assert(g.hold(1,25,25,30));assert(g.ready());
 auto done=g.complete();
 assert(done.offload==31 && done.send==31 && done.callback==100);
 assert(done.slots[0]==0 && done.releases[0]==100 && done.slots[1]==1 && done.releases[1]==31);
 bool repeat=false;try{g.complete();}catch(const std::logic_error&){repeat=true;}assert(repeat);
 bool too_large=false;try{G invalid(3,2,0,0,0,1,1);}catch(const std::invalid_argument&){too_large=true;}assert(too_large);
 G not_ready(2,2,0,0,0,1,1);assert(not_ready.hold(0,0,1,3));
 bool early=false;try{not_ready.complete();}catch(const std::logic_error&){early=true;}assert(early);
 G previous_head(2,4,10,100,100,1,1);
 assert(previous_head.hold(0,0,1,20) && previous_head.hold(1,0,2,30));
 auto prior=previous_head.complete();
 assert(prior.offload==100 && prior.releases[1]==100 && prior.callback==101);
 SprStoreMergeCandidate a{},b{};
 a.valid_plain=b.valid_plain=true;a.core=b.core=0;a.chunk=b.chunk=9;
 a.batch_rank=2;b.batch_rank=3;a.uop=10;b.uop=11;a.retire_q16=b.retire_q16=10*65536ull;
 a.facts=SprStoreInput{3,1,0,1,1};b.facts=SprStoreInput{3,2,0,1,1};
 assert(spr_store_merge_pair(a,b,65536));
 // Memory-less NoAccess or fence UOP between them makes UOP indices nonadjacent.
 auto c=b;c.uop=12;assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.valid_plain=false;assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.facts.reset();assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.batch_rank=4;assert(!spr_store_merge_pair(a,c,65536)); // intervening load/other core
 c=b;c.core=1;assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.chunk=10;assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.facts->flags=4;assert(!spr_store_merge_pair(a,c,65536));
 c=b;c.retire_q16+=65536;assert(!spr_store_merge_pair(a,c,65536));
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(binary)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
