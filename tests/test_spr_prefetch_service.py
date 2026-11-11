"""Bounded two-stage prefetch completion and transaction behavior."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchServiceTest(unittest.TestCase):
 def test_service_snapshot_and_writeback(self):
  code=r'''
#include "fastsim/spr_prefetch_service.hpp"
#include "fastsim/cache.hpp"
#include <cassert>
#include <vector>
using namespace fastsim;
CacheConfig config() {CacheConfig x;x.size_bytes=64;x.associativity=1;return x;}
int main() {
 using S=SprPrefetchService;
 // Ticket allocator must live outside copied transactional state.
 S generation(1);auto empty=generation;uint64_t ticket_allocator=0;
 const auto abandoned=++ticket_allocator;
 assert(generation.begin_at(1,abandoned,0,7,1,S::Level::kL1,1)==S::Begin::kAccepted);
 generation=empty;
 const auto replacement=++ticket_allocator;
 assert(generation.begin_at(2,replacement,0,7,2,S::Level::kL1,2)==S::Begin::kAccepted);
 assert(generation.select_response(abandoned,3)==S::Select::kStale);
 assert(generation.select_response(replacement,4)==S::Select::kSelected);
 assert(generation.begin_at(3,++ticket_allocator,0,8,3,S::Level::kL1,2)==S::Begin::kCapacity);
 generation.complete_through(4,[](const S::Entry&){return S::Commit::kRetry;});
 generation.complete_through(5,[](const S::Entry&){return S::Commit::kRetry;});
 assert(generation.select_response(replacement,4)==S::Select::kDuplicate); // delayed duplicate remains idempotent
 bool different_response=false;try{generation.select_response(replacement,5);}catch(...){different_response=true;}assert(different_response);
 PrivateHierarchy cache(config(),config());CoreCounters initial,prefetch;
 cache.complete_fill(1,true,initial); // dirty victim once line 3 completes
 S pending(3);
 assert(pending.begin_at(9,1,0,3,10,S::Level::kL1,77)==S::Begin::kDeferred);
 assert(pending.size()==0); // future issue cannot reserve capacity
 assert(pending.begin_at(10,1,0,3,10,S::Level::kL1,77)==S::Begin::kAccepted);
 assert(pending.begin_at(11,2,0,3,11,S::Level::kL1,77)==S::Begin::kDuplicateLine);
 assert(pending.begin_at(11,2,1,3,11,S::Level::kL2,77)==S::Begin::kAccepted);
 assert(!pending.waiting(0,3,9)); // future prefetch cannot capture demand
 assert(pending.waiting(0,3,10)->ticket==1);
 assert(!pending.waiting(0,3,10)->response.has_value());
 assert(pending.select_response(11,30)==S::Select::kStale);
 assert(pending.select_response(1,30)==S::Select::kSelected);
 assert(pending.select_response(1,30)==S::Select::kDuplicate);
 // Followers retain an absolute service boundary when CPU issue moves.
 // Two demands share one service without consuming or duplicating it.
 assert(pending.follower_response(1,0,3,9,2,3)==std::nullopt);
 assert(pending.follower_response(1,0,3,12,2,3)==33);
 assert(pending.follower_response(1,0,3,20,2,3)==33);
 assert(pending.follower_response(1,0,3,40,2,3)==42);
 // A CPU event retains its own service reference across publication and Qs.
 auto follower=pending.attach_follower(1,0,3,12);
 assert(follower && follower->ticket==1);
 assert(!pending.attach_follower(1,0,3,9));
 assert(!pending.attach_follower(2,1,3,20));
 assert(S::response_for(*follower,20,2,3)==33);
 assert(pending.follower_response(1,1,3,20,2,3)==std::nullopt);
 assert(pending.follower_response(2,1,3,20,2,3)==std::nullopt);
 assert(pending.size()==2);
 bool overflow=false;
 try { pending.follower_response(1,0,3,UINT64_MAX,1,3); }
 catch(const std::overflow_error&) { overflow=true; }
 assert(overflow);
 bool bad=false;try{pending.select_response(2,10);}catch(...){bad=true;}assert(bad);
 assert(pending.select_response(2,25)==S::Select::kSelected);
 auto saved=pending;auto tx=cache.begin_transaction();std::vector<uint64_t> writes;
 unsigned calls=0;
 auto publish=[&](const S::Entry& e){
  ++calls;
  if(e.core==1) return S::Commit::kRetry; // unrelated due prefetch can proceed
  auto r=cache.publish_prefetch_fill(e.line,e.issue,*e.response,*e.response,true,prefetch,&tx);
  assert(r.status==PrefetchPublicationStatus::kPublished);
  if(r.result.l2_evicted_dirty)writes.push_back(r.result.l2_evicted_line);
  return S::Commit::kPublished;
 };
 auto before=pending.complete_through(24,publish);assert(before.published==0 && calls==0 && !cache.contains(3));
 auto mid=pending.complete_through(25,publish);assert(mid.deferred==1 && !cache.contains(3));
 auto done=pending.complete_through(30,publish);assert(done.published==1 && done.deferred==1 && cache.contains(3));
 assert(writes.size()==1 && writes[0]==1 && pending.size()==1);
 assert(prefetch.l1d.accesses==0 && prefetch.l2.accesses==0);
 // Restore all three independently owned pieces of one speculative batch.
 cache.restore(tx);pending=saved;writes.clear();
 assert(cache.contains(1) && !cache.contains(3) && pending.size()==2);
 // A discarded future callback cannot update a replacement generation.
 assert(pending.cancel(1));
 assert(pending.follower_response(1,0,3,20,2,3)==std::nullopt);
 assert(S::response_for(*follower,40,2,3)==42);
 assert(pending.select_response(1,40)==S::Select::kStale);
 // On coherence revocation the publisher reports cancellation: no stale fill.
 auto revoke=pending.complete_through(30,[](const S::Entry&){return S::Commit::kPermissionLost;});
 assert(revoke.cancelled==1 && !cache.contains(3));
 assert(pending.begin_at(31,3,0,3,31,S::Level::kL1,78)==S::Begin::kAccepted);
 bool backwards=false;try { pending.begin_at(30,4,0,4,30,S::Level::kL1,78); } catch(...) { backwards=true; } assert(backwards);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
