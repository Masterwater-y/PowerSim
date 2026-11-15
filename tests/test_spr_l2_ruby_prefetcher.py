"""Independent L2 SLICC action order and queue semantics; no native outcomes."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class L2PrefetcherTest(unittest.TestCase):
 def test_controller_action_order_and_queue(self):
  code=r'''
#include "fastsim/spr_l2_ruby_prefetcher.hpp"
#include <cassert>
#include <type_traits>
using namespace fastsim;
using P=SprL2RubyPrefetcher;
int main(){
 auto take=[](P& p,uint64_t t,uint64_t line) {auto h=p.peek(t);assert(h && h->line==line);assert(p.consume(t,h->handle));};
 static_assert(!std::is_default_constructible<P>::value,"queue bound must be explicit");
 bool zero=false;try{P invalid(0);}catch(const std::invalid_argument&){zero=true;}assert(zero);
 bool huge=false;try{P invalid((1u<<20)+1);}catch(const std::invalid_argument&){huge=true;}assert(huge);
 P p(64);
 for(unsigned i=0;i<4;++i) {
  auto t=2*i;assert(p.observe({P::Action::kMiss,P::Request::kGetS,0x1000+i*64,t,true,{}}).observed);
  assert(!p.peek(t));take(p,t+1,(0x1000+i*64)^64);
 }
 assert(p.observe({P::Action::kMiss,P::Request::kGetS,0x1100,8,true,{}}).observed);
 assert(p.duplicates()==1);take(p,9,0x1140);
 auto hit=p.observe({P::Action::kHit,P::Request::kGetS,0x1140,10,true,true});
 assert(hit.observed && hit.clear_prefetched);take(p,11,0x1180);take(p,11,0x1100);
 assert(!p.observe({P::Action::kHit,P::Request::kGetS,0x1180,12,true,{}}).observed);
 assert(!p.peek(12));
 assert(p.observe({P::Action::kHit,P::Request::kOther,0x1180,12,true,{}}).observed);
 assert(p.observe({P::Action::kUpgrade,P::Request::kGetX,0x1180,12,true,true}).observed);
 assert(!p.peek(12));
 assert(p.observe({P::Action::kLateLoad,P::Request::kGetS,0x1180,12,false,{}}).observed);
 take(p,13,0x11c0);
 P q(64);assert(q.observe({P::Action::kMiss,P::Request::kGetS,0x2000,0,true,{}}).observed);
 assert(q.inspect(0,{})==P::Decision::kNotReady);
 auto head=*q.peek(1);
 assert(q.inspect(1,{false,P::LineState::kUnknown,{},{},{}})==P::Decision::kDrop);
 assert(q.consume(1,head.handle) && !q.peek(1));
 q.observe({P::Action::kMiss,P::Request::kGetS,0x2000,2,true,{}});head=*q.peek(3);
 assert(q.inspect(3,{true,P::LineState::kPresent,{},{},{}})==P::Decision::kDrop);
 assert(q.consume(3,head.handle));q.observe({P::Action::kMiss,P::Request::kGetS,0x2000,4,true,{}});
 assert(q.inspect(5,{true,P::LineState::kInvalid,false,{},{}})==P::Decision::kRetry);
 assert(q.inspect(5,{true,P::LineState::kInvalid,true,false,false})==P::Decision::kRetry);
 assert(q.inspect(5,{true,P::LineState::kInvalid,true,false,true})==P::Decision::kNeedEviction);
 assert(q.inspect(5,{true,P::LineState::kInvalid,true,true,{}})==P::Decision::kReady);
 head=*q.peek(5);assert(!q.consume(5,P::Handle{head.handle.owner,123}));assert(q.consume(5,head.handle));
 assert(q.inspect(5,{})==P::Decision::kEmpty);
 P recycle(64);
 recycle.observe({P::Action::kMiss,P::Request::kGetS,0x3000,0,true,{}});
 recycle.observe({P::Action::kMiss,P::Request::kGetS,0x4000,0,true,{}});
 head=*recycle.peek(1);assert(head.line==0x3040);
 assert(!recycle.recycle(1,head.handle,{}));assert(!recycle.recycle(1,head.handle,1));
 assert(recycle.recycle(1,head.handle,2));
 assert(recycle.peek(1)->line==0x4040);
 recycle.observe({P::Action::kMiss,P::Request::kGetS,0x3000,1,true,{}});
 assert(recycle.duplicates()==1);
 take(recycle,1,0x4040);assert(!recycle.peek(1));take(recycle,2,0x3040);
 P tie(64);tie.observe({P::Action::kMiss,P::Request::kGetS,0x5000,0,true,{}});
 head=*tie.peek(1);assert(tie.recycle(1,head.handle,2));
 tie.observe({P::Action::kMiss,P::Request::kGetS,0x6000,1,true,{}});
 assert(tie.peek(2)->line==0x5040); // original message sequence wins tie
 // A distant certified resource event does not cause one-cycle retry loops.
 P longwait(1);longwait.observe({P::Action::kMiss,P::Request::kGetS,0x7000,0,true,{}});
 head=*longwait.peek(1);assert(longwait.recycle(1,head.handle,1000000000000ull));
 longwait.observe({P::Action::kMiss,P::Request::kGetS,0x8000,2,true,{}});
 assert(longwait.overflow()==1 && !longwait.peek(2) && *longwait.next_ready()==1000000000000ull);
 auto snapshot=longwait;take(longwait,1000000000000ull,0x7040);
 assert(snapshot.peek(1000000000000ull)->line==0x7040);
 P bounded(2);bounded.observe({P::Action::kMiss,P::Request::kGetS,0xa000,0,true,{}});
 bounded.observe({P::Action::kMiss,P::Request::kGetS,0xb000,0,true,{}});
 bounded.observe({P::Action::kMiss,P::Request::kGetS,0xc000,0,true,{}});
 assert(bounded.queued()==2 && bounded.overflow()==1);
 auto bounded_copy=bounded;take(bounded,1,0xa040);take(bounded_copy,1,0xa040);
 bounded_copy.observe({P::Action::kMiss,P::Request::kGetS,0xd000,1,true,{}});
 assert(bounded_copy.queued()==2 && bounded.queued()==1);
 // Rollback may restore an empty queue, but cannot reuse an old ticket.
 P aba(64);auto initial=aba;aba.observe({P::Action::kMiss,P::Request::kGetS,0x9000,0,true,{}});
 auto old=*aba.peek(1);aba=initial;aba.observe({P::Action::kMiss,P::Request::kGetS,0x9000,0,true,{}});
 auto current=*aba.peek(1);assert(old.handle.ticket!=current.handle.ticket && !aba.consume(1,old.handle));
 assert(aba.consume(1,current.handle));
 // Fresh owners can generate the same local ticket; cross-owner callback
 // and a callback from a destroyed/recreated owner must still be rejected.
 P owner_a(2),owner_b(2);
 owner_a.observe({P::Action::kMiss,P::Request::kGetS,0xe000,0,true,{}});
 owner_b.observe({P::Action::kMiss,P::Request::kGetS,0xe000,0,true,{}});
 auto ha=*owner_a.peek(1),hb=*owner_b.peek(1);
 assert(ha.handle.ticket==hb.handle.ticket && ha.handle.owner!=hb.handle.owner);
 assert(!owner_a.consume(100,hb.handle) && !owner_a.recycle(100,hb.handle,101));
 assert(owner_a.consume(1,ha.handle) && owner_b.consume(1,hb.handle));
 P replacement(2);replacement.observe({P::Action::kMiss,P::Request::kGetS,0xe000,0,true,{}});
 auto recreated=*replacement.peek(1);
 assert(recreated.handle.ticket==ha.handle.ticket && recreated.handle.owner!=ha.handle.owner);
 assert(!replacement.consume(1,ha.handle));assert(replacement.consume(1,recreated.handle));
 // Recycling for a credit change at 100 must not hide a line becoming
 // resident at 2. The MODEL explicitly notifies that earlier state change.
 P wake(4);wake.observe({P::Action::kMiss,P::Request::kGetS,0xf000,0,true,{}});
 assert(!wake.notify_line_change(0,0xf040) && !wake.peek(0));
 auto waiting=*wake.peek(1);assert(wake.recycle(1,waiting.handle,100));
 assert(wake.notify_line_change(2,0xf040));
 auto awakened=*wake.peek(2);assert(awakened.line==0xf040 && awakened.handle.ticket==waiting.handle.ticket);
 assert(wake.inspect(2,{true,P::LineState::kPresent,{},{},{}})==P::Decision::kDrop);
 assert(wake.consume(2,awakened.handle));
 wake.observe({P::Action::kMiss,P::Request::kGetS,0xf000,2,true,{}});
 take(wake,3,0xf040); // dequeue released dedup at the real model edge
 // A controller-wide change can wake multiple blocked heads without polling.
 P global_wake(4);global_wake.observe({P::Action::kMiss,P::Request::kGetS,0x11000,0,true,{}});
 global_wake.observe({P::Action::kMiss,P::Request::kGetS,0x12000,0,true,{}});
 auto first_wait=*global_wake.peek(1);assert(global_wake.recycle(1,first_wait.handle,100));
 auto second_wait=*global_wake.peek(1);assert(global_wake.recycle(1,second_wait.handle,90));
 assert(global_wake.notify_resource_change(2)==2);
 assert(global_wake.peek(2)->handle.ticket==first_wait.handle.ticket);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
