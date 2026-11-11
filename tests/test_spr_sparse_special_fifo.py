"""Special stores share credits; completion edges are modeled, never native."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SparseSpecialFifoTest(unittest.TestCase):
    def test_special_fifo_and_future_events(self):
        code = r'''
#include "fastsim/spr_sparse_store_credits.hpp"
#include <cassert>
#include <optional>
#include <stdexcept>
using P=fastsim::SprSparseStoreCredits;
using S=fastsim::SprCommittedStoreBuffer::Store;
S s(unsigned sequence,unsigned line){return {sequence,line,0,0,0,1};}
int main() {
 P p(3,2,2,1,4);
 assert(p.admit_sq(1,0));auto x=p.admit_special(2,0);assert(x);
 assert(p.admit_sq(3,0));assert(!p.admit_special(4,0));
 assert(p.schedule_offload(s(1,5),2));assert(p.schedule_offload(s(3,5),2));
 p.advance(2);assert(p.budget_used()==3 && p.queued_offloads()==1);
 assert(!p.begin_special(2));assert(!p.next_transition()); // no invented edge
 auto first=p.send_next(2,10);assert(first);assert(p.next_transition()==11);
 p.advance(11);assert(p.budget_used()==2 && !p.next_transition());
 assert(p.begin_special(11)==x);assert(!p.next_transition());
 assert(!p.schedule_special_completion(*x,std::nullopt));
 assert(p.budget_used()==2 && !p.next_transition());
 assert(p.schedule_special_completion(*x,20));
 auto snapshot=p;p.advance(19);assert(p.budget_used()==2);
 p.advance(20);assert(p.budget_used()==1 && p.queued_offloads()==0);
 assert(!p.send_next(20,20));auto after=p.send_next(21,21);
 assert(after && after->id!=first->id); // cannot merge across barrier
 assert(snapshot.budget_used()==2 && snapshot.next_transition()==20);
 snapshot.advance(20);assert(snapshot.budget_used()==1);
 bool backwards=false;try{p.advance(19);}catch(const std::logic_error&){backwards=true;}assert(backwards);

 P q(3,1,1,1,1);
 assert(q.admit_sq(1,0));auto a=q.admit_special(2,0);assert(a);
 auto b=q.admit_special(3,0);assert(b);
 assert(q.schedule_offload(s(1,2),5));assert(!q.admit_sq(4,0));
 q.advance(5);auto older=q.send_next(5,9);assert(older);
 assert(!q.begin_special(9));assert(q.begin_special(10)==a);
 bool wrong=false;try{q.schedule_special_completion(*b,10);}catch(const std::logic_error&){wrong=true;}assert(wrong);
 assert(q.schedule_special_completion(*a,12));q.advance(12);
 assert(q.begin_special(12)==b);assert(q.schedule_special_completion(*b,13));
 q.advance(13);assert(q.budget_used()==0);

 P cap(2,1,2,1,1);assert(cap.admit_sq(1,0));assert(cap.admit_sq(2,0));
 assert(cap.schedule_offload(s(1,1),3));assert(!cap.schedule_offload(s(2,2),3));
 cap.advance(3);assert(cap.schedule_offload(s(2,2),3));

 // A blocked future offload still consumes bounded queue capacity; a full
 // queue cannot silently drop its store or advance the special boundary.
 P full(3,2,2,1,1);auto barrier=full.admit_special(1,0);assert(barrier);
 assert(full.admit_sq(2,0));assert(full.admit_sq(3,0));
 assert(full.schedule_offload(s(2,9),1));assert(!full.schedule_offload(s(3,9),1));
 full.advance(1);assert(!full.next_transition() && full.queued_offloads()==1);
 assert(full.begin_special(1)==barrier);assert(full.schedule_special_completion(*barrier,4));
 full.advance(4);assert(full.budget_used()==2 && full.queued_offloads()==0);
 assert(full.schedule_offload(s(3,9),4));full.advance(4);
 assert(full.budget_used()==1); // same-side stores may merge

 // An out-of-order modeled callback cannot complete a later barrier or
 // release the current one, and a failed time check keeps its credit.
 P callbacks(2,1,1,1,2);auto first_special=callbacks.admit_special(1,5);
 auto second_special=callbacks.admit_special(2,5);assert(first_special && second_special);
 assert(callbacks.begin_special(5)==first_special);
 bool late=false;try{callbacks.schedule_special_completion(*second_special,6);}catch(const std::logic_error&){late=true;}
 assert(late && callbacks.budget_used()==2 && !callbacks.next_transition());
 bool past=false;try{callbacks.schedule_special_completion(*first_special,4);}catch(const std::logic_error&){past=true;}
 assert(past && callbacks.budget_used()==2);
 assert(callbacks.schedule_special_completion(*first_special,5));callbacks.advance(5);
 assert(callbacks.budget_used()==1 && callbacks.begin_special(5)==second_special);
 assert(callbacks.schedule_special_completion(*second_special,5));callbacks.advance(5);
 assert(callbacks.budget_used()==0);

 // Ordered pipeline exposes the same FIFO barrier independently of the
 // sparse event adapter and does not permit cross-barrier line merging.
 fastsim::SprOrderedStorePipeline direct(3,2,2,1);
 assert(direct.admit_sq(0));auto d=direct.offload(s(1,7),0);assert(d.accepted);
 auto dx=direct.admit_special(2,0);assert(dx);assert(direct.admit_sq(0));
 assert(!direct.offload(s(3,7),0).accepted);assert(direct.send_next(0,1));
 assert(!direct.begin_special(1));assert(direct.begin_special(2)==dx);
 direct.complete_special(*dx,3);auto after_special=direct.offload(s(3,7),3);
 assert(after_special.accepted && !after_special.merged && after_special.id!=d.id);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

if __name__ == '__main__':
    unittest.main()
