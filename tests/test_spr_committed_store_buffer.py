"""Independent committed store buffer summary, no target time stepping."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CommittedStoreBufferTest(unittest.TestCase):
 def test_shared_budget_merge_and_ordered_reclaim(self):
  code=r'''
#include "fastsim/spr_committed_store_buffer.hpp"
#include <cassert>
int main() {
 using namespace fastsim;
 using B=SprCommittedStoreBuffer;
 B q(4,4,4);
 auto s=[](uint64_t seq,uint64_t line,uint64_t context,uint64_t mask=1) {
   return B::Store{seq,line,context,0,0,mask};
 };
 assert(q.admit_sq());assert(q.admit_sq());
 auto a=q.offload(s(1,10,1,1),0); assert(!a.merged && a.cycle==0);
 auto b=q.offload(s(2,10,1,2),0); assert(b.merged && a.id==b.id);
 assert(q.sq_entries()==0 && q.buffer_entries()==1 && q.budget_used()==1);
 assert(q.byte_mask(a.id)==3);
 assert(q.admit_sq()); auto c=q.offload(s(3,10,2),0);assert(!c.merged);
 assert(q.send_next(0)==a.id); assert(q.send_next(0)==c.id);
 assert(q.admit_sq()); auto d=q.offload(s(4,10,2),0);assert(!d.merged);
 assert(q.send_next(0)==d.id);
 q.complete(c.id,0);assert(q.buffer_entries()==3); // completed middle retained
 q.complete(a.id,0);assert(q.buffer_entries()==1); // reclaim contiguous prefix
 q.complete(d.id,0);assert(q.budget_used()==0);
 for(unsigned i=0;i<4;++i) {assert(q.admit_sq());}
 assert(!q.admit_sq());
 for(unsigned i=0;i<4;++i)assert(q.offload(s(10+i,20+i,1),10).cycle==10);
 assert(!q.admit_sq());
 assert(q.send_next(10)); assert(q.send_next(10)); assert(q.send_next(10)); assert(q.send_next(10));
 B p(8,4,1);assert(p.admit_sq());auto x=p.offload(s(20,1,1),0);assert(p.send_next(0)==x.id);
 assert(p.admit_sq());auto y=p.offload(s(21,2,1),0);assert(!p.send_next(0));
 p.complete(x.id,0);assert(p.send_next(0)==y.id);
 B same(4,4,1);assert(same.admit_sq());auto s1=same.offload(s(1,1,1),0);assert(same.send_next(0)==s1.id);
 assert(same.admit_sq());auto s2=same.offload(s(2,1,1),0);assert(same.send_next(0)==s2.id);
 assert(same.admit_sq());auto s3=same.offload(s(3,2,1),0);assert(!same.send_next(0));
 same.complete(s1.id,0);assert(!same.send_next(0));same.complete(s2.id,0);assert(same.send_next(0)==s3.id);
 B guards(5,4,4);
 auto g0=s(1,4,1,1); assert(guards.admit_sq());auto first=guards.offload(g0,0);
 auto other=g0;other.sequence=2;other.flags=1;assert(guards.admit_sq());auto different=guards.offload(other,0);assert(!different.merged && different.id!=first.id);
 other.sequence=3;other.requestor=1;assert(guards.admit_sq());assert(!guards.offload(other,0).merged);
 bool threw=false;try {guards.complete(first.id,0);} catch(const std::logic_error&) {threw=true;} assert(threw);
 auto snapshot=guards;assert(snapshot.send_next(0)==first.id);assert(snapshot.buffer_entries()==guards.buffer_entries());assert(guards.send_next(0)==first.id);
 B aba(2,4,4);assert(aba.admit_sq());auto before=aba;
 auto old=aba.offload(s(70,7,1),0);aba=before;auto current=aba.offload(s(70,7,1),0);
 assert(old.id!=current.id);assert(aba.send_next(0)==current.id);
 bool stale=false;try {aba.complete(old.id,100);}catch(const std::logic_error&){stale=true;}assert(stale);aba.complete(current.id,0);
 B time(2,4,4);assert(time.admit_sq());auto timed=time.offload(s(90,9,1),10);assert(timed.accepted);
 bool too_early=false;try {time.send_next(9);}catch(const std::logic_error&) {too_early=true;}assert(too_early);
 assert(time.send_next(10)==timed.id);time.complete(timed.id,11);
 B width(8,4,4);for(unsigned i=0;i<5;++i)assert(width.admit_sq());
 for(unsigned i=0;i<4;++i)assert(width.offload(s(30+i,30+i,1),0).cycle==0);
 auto deferred=width.offload(s(34,34,1),0);
 bool early=false;try {width.send_next(0);}catch(const std::logic_error&) {early=true;}assert(!early);
 assert(!deferred.accepted && deferred.cycle==1 && width.sq_entries()==1 && width.buffer_entries()==4);
 auto fifth=width.offload(s(34,34,1),1);assert(fifth.accepted && fifth.cycle==1 && width.buffer_entries()==5);
 // A special/unknown store remains an SQ credit owner and a FIFO barrier;
 // a later same-line normal store cannot pass it or merge into an old tail.
 B barrier(3,4,4);assert(barrier.admit_sq());
 auto old_line=barrier.offload(s(1,5,1),0);assert(old_line.accepted);
 auto special=barrier.admit_special(2);assert(special);assert(barrier.admit_sq());assert(!barrier.admit_sq());
 auto blocked=barrier.offload(s(3,5,1),0);assert(!blocked.accepted && barrier.sq_entries()==2 && barrier.budget_used()==3);
 assert(!barrier.begin_special(0)); // earlier buffered line must drain first
 assert(barrier.send_next(0)==old_line.id);barrier.complete(old_line.id,2);
 assert(barrier.begin_special(2)==special);assert(barrier.budget_used()==2);
 assert(!barrier.offload(s(3,5,1),2).accepted && barrier.budget_used()==2);
 barrier.complete_special(*special,3);assert(barrier.budget_used()==1);
 auto after=barrier.offload(s(3,5,1),3);assert(after.accepted && !after.merged && after.id!=old_line.id);
 assert(barrier.send_next(3)==after.id);barrier.complete(after.id,4);assert(barrier.budget_used()==0);
 // Special remains live across snapshot boundaries and does not permit an
 // out-of-order callback or a stale callback after rollback.
 B special_aba(2,4,4);auto base=special_aba;
 auto stale_id=special_aba.admit_special(4);assert(stale_id);special_aba=base;
 auto live_id=special_aba.admit_special(4);assert(live_id && live_id!=stale_id);
 assert(special_aba.begin_special(0)==live_id);
 bool stale_special=false;try{special_aba.complete_special(*stale_id,100);}catch(const std::logic_error&){stale_special=true;}assert(stale_special);
 auto next_chunk=special_aba;next_chunk.complete_special(*live_id,0);assert(next_chunk.budget_used()==0);
 assert(special_aba.budget_used()==1);special_aba.complete_special(*live_id,0);
 // Two consecutive barriers cannot overtake a still-unoffloaded SQ head or
 // one another; full-budget rejection does not consume a slot.
 B sequence(3,4,4);assert(sequence.admit_sq());
 auto sp1=sequence.admit_special(10),sp2=sequence.admit_special(11);
 assert(sp1 && sp2 && !sequence.admit_special(12) && !sequence.begin_special(0));
 auto head=sequence.offload(s(9,4,1),0);assert(head.accepted);
 assert(!sequence.begin_special(0));assert(sequence.send_next(0)==head.id);
 sequence.complete(head.id,1);assert(sequence.begin_special(1)==sp1);
 bool wrong_order=false;try{sequence.complete_special(*sp2,1);}catch(const std::logic_error&){wrong_order=true;}assert(wrong_order);
 sequence.complete_special(*sp1,1);assert(sequence.begin_special(1)==sp2);
 sequence.complete_special(*sp2,1);assert(sequence.budget_used()==0);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
   build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
   self.assertEqual(build.returncode,0,build.stderr)
   run=subprocess.run([str(binary)],capture_output=True,text=True)
   self.assertEqual(run.returncode,0,run.stderr)
if __name__=='__main__':unittest.main()
