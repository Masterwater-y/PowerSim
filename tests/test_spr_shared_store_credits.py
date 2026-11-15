"""Shared credits are not the FIFO SQ ring; tests cover delayed releases."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SharedStoreCreditsTest(unittest.TestCase):
 def test_preallocated_capacity_and_reuse(self):
  code=r'''
#include "fastsim/spr_shared_store_credits.hpp"
#include <array>
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>
std::atomic<unsigned long> allocations{0};
void* operator new(std::size_t n) {++allocations;if(auto p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p) noexcept {std::free(p);}
void operator delete(void* p,std::size_t) noexcept {std::free(p);}
int main(){
 using C=fastsim::SprSharedStoreCredits;
 bool rejected=false;try{C empty(0);}catch(const std::invalid_argument&){rejected=true;}assert(rejected);
 fastsim::SprStoreCreditTicketCodec codec(112);assert(codec.slot_bits()==7);
 assert(codec.max_serial()==((1ull<<57)-1));
 auto max_ticket=codec.encode(codec.max_serial(),111);assert(codec.candidate_slot(max_ticket)==111);
 bool generation_overflow=false;try{codec.encode(codec.max_serial()+1,0);}catch(const std::overflow_error&){generation_overflow=true;}assert(generation_overflow);
 bool invalid_slot=false;try{codec.encode(1,112);}catch(const std::out_of_range&){invalid_slot=true;}assert(invalid_slot);
 bool zero_serial=false;try{codec.encode(0,0);}catch(const std::overflow_error&){zero_serial=true;}assert(zero_serial);
 fastsim::SprStoreCreditTicketCodec tiny(1);assert(tiny.slot_bits()==0 && tiny.candidate_slot(tiny.encode(1,0))==0);
 fastsim::SprStoreCreditTicketCodec huge(UINT32_MAX);assert(huge.slot_bits()==32 && huge.candidate_slot(huge.encode(1,UINT32_MAX-1))==UINT32_MAX-1);
 C c(112);const auto baseline=allocations.load();
 std::array<std::uint64_t,112> ids{};
 for(std::uint64_t round=0;round<1000;++round) {
  const auto t=round*2;
  for(auto& id:ids) {auto r=c.admit(t,t,C::Kind::kOrdinary);assert(r.status==C::Admission::kAccepted);id=r.id;}
  assert(c.used()==112);
  for(auto id:ids)assert(c.schedule_single(id,t+1));
  c.advance(t+1);assert(c.used()==0);
 }
 assert(allocations.load()==baseline); // no heap allocation per request/event
 C one(1);auto a=one.admit(0,0,C::Kind::kSpecial);auto old=a.id;
 assert(one.schedule_single(a.id,1));one.advance(1);
 auto next=one.admit(1,1,C::Kind::kUnknown);assert(next.id!=old);
 bool stale=false;try{one.schedule_single(old,2);}catch(const std::logic_error&){stale=true;}assert(stale);
 assert(one.schedule_single(next.id,2));one.advance(2);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
 def test_group_credit_future_and_rollback(self):
  code=r'''
#include "fastsim/spr_shared_store_credits.hpp"
#include "fastsim/spr_pending_store_group.hpp"
#include <cassert>
#include <vector>
using namespace fastsim;
int main(){
 using C=SprSharedStoreCredits;
 C c(2);
 auto future=c.admit(0,5,C::Kind::kOrdinary);
 assert(future.status==C::Admission::kFuture && c.used()==0);
 auto a=c.admit(0,0,C::Kind::kOrdinary),b=c.admit(0,0,C::Kind::kOrdinary);
 assert(a.status==C::Admission::kAccepted && b.status==C::Admission::kAccepted && c.used()==2);
 assert(c.schedule_group({a.id,b.id},10,20));
 auto peek=c.probe(9);assert(peek.status==C::Admission::kFull && *peek.next_release==10 && c.used()==2);
 auto blocked=c.admit(9,9,C::Kind::kUnknown);
 assert(blocked.status==C::Admission::kFull && blocked.next_release && *blocked.next_release==10);
 // The younger follower credit at 10 is usable although the older owner is
 // retained to callback 20; a FIFO sq_next_slot test would wait until 20.
 auto available=c.probe(10);assert(available.status==C::Admission::kAccepted && c.used()==1);
 auto unknown=c.admit(10,10,C::Kind::kUnknown);assert(unknown.status==C::Admission::kAccepted && c.used()==2);
 auto next=c.admit(10,10,C::Kind::kSpecial);assert(next.status==C::Admission::kFull && *next.next_release==20);
 assert(c.schedule_single(unknown.id,15));
 auto special=c.admit(15,15,C::Kind::kSpecial);assert(special.status==C::Admission::kAccepted);
 bool invalid_group=false;try{c.schedule_group({a.id,special.id},16,22);}catch(const std::logic_error&){invalid_group=true;}assert(invalid_group);
 assert(c.schedule_single(special.id,1000000000000ull));
 c.advance(20);assert(c.used()==1);
 bool backwards=false;try{c.advance(19);}catch(const std::logic_error&){backwards=true;}assert(backwards);
 c.advance(1000000000000ull);assert(c.used()==0);
 // Full unknown owners cannot promise a guessed next release.
 C d(1);auto u=d.admit(0,0,C::Kind::kUnknown);assert(u.status==C::Admission::kAccepted);
 auto full=d.admit(0,0,C::Kind::kOrdinary);assert(full.status==C::Admission::kFull && !full.next_release);
 auto snapshot=d;assert(d.schedule_single(u.id,30));d.advance(30);assert(d.used()==0);
 d=snapshot;assert(d.used()==1);assert(d.schedule_single(u.id,40));
 d.advance(40);assert(d.used()==0);
 // An ID created in a rolled-back transaction cannot match a later owner.
 auto before=d;auto stale=d.admit(40,40,C::Kind::kOrdinary);d=before;
 auto live=d.admit(40,40,C::Kind::kOrdinary);assert(stale.id!=live.id);
 bool stale_callback=false;try{d.schedule_single(stale.id,100);}catch(const std::logic_error&){stale_callback=true;}assert(stale_callback);
 assert(d.schedule_single(live.id,50));d.advance(50);
 C wide(112);auto w0=wide.admit(0,0,C::Kind::kOrdinary);auto wide_checkpoint=wide;
 auto erased=wide.admit(0,0,C::Kind::kOrdinary);wide=wide_checkpoint;
 auto replacement=wide.admit(0,0,C::Kind::kOrdinary);assert(erased.id!=replacement.id);
 bool wide_stale=false;try{wide.schedule_single(erased.id,10);}catch(const std::logic_error&){wide_stale=true;}assert(wide_stale);
 assert(wide.schedule_single(w0.id,10) && wide.schedule_single(replacement.id,11));wide.advance(11);assert(wide.used()==0);
 // Rejected groups are transactional: no half-scheduled release exists.
 C e(2);auto x=e.admit(0,0,C::Kind::kOrdinary),y=e.admit(0,0,C::Kind::kOrdinary);
 bool bad=false;try{e.schedule_group({x.id,x.id},3,4);}catch(const std::logic_error&){bad=true;}assert(bad);
 assert(!e.next_release());assert(e.schedule_group({x.id,y.id},3,4));
 e.advance(3);assert(e.used()==1);e.advance(4);assert(e.used()==0);
 // Integrate the held-SQ group result with an independent shared budget.
 C credits(2);auto lead=credits.admit(0,0,C::Kind::kOrdinary);
 auto follower=credits.admit(1,1,C::Kind::kOrdinary);
 SprPendingStoreGroup group(2,2,100,0,0,1,1);
 assert(group.hold(0,0,0,10) && group.hold(1,0,1,30));
 auto edges=group.complete();assert(edges.offload==31 && edges.callback==100);
 assert(credits.schedule_group({lead.id,follower.id},edges.offload,edges.callback));
 auto shared=credits.admit(31,31,C::Kind::kSpecial);
 assert(shared.status==C::Admission::kAccepted && credits.used()==2);
 assert(credits.schedule_single(shared.id,33));credits.advance(33);assert(credits.used()==1);
 credits.advance(100);assert(credits.used()==0);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
