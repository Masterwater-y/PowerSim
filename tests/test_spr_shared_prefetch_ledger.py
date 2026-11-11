"""Shared-prefetch ledger with a real LLC and transactional fake directory."""
import subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SharedPrefetchLedgerTest(unittest.TestCase):
 def test_visibility_demand_cancel_and_rollback(self):
  code=r'''
#include "fastsim/spr_shared_prefetch_ledger.hpp"
#include "fastsim/spr_shared_prefetch_publish.hpp"
#include "fastsim/spr_prefetch_grant.hpp"
#include <cassert>
#include <vector>
using namespace fastsim;
int main() {
 using L=SprSharedPrefetchLedger;
 CacheConfig cfg;cfg.size_bytes=128;cfg.associativity=2;
 SetAssociativeCache llc(cfg);CacheCounters counters;
 llc.complete_fill(1,true,counters);llc.complete_fill(2,false,counters);
 L ledger(2);auto snap=ledger;auto tx=llc.begin_transaction();
 auto future=ledger.issue(10,0,3,11,5);assert(future.status==L::Issue::kFuture && ledger.size()==0 && !llc.contains(3));
 auto a=ledger.issue(10,0,3,10,5);assert(a.status==L::Issue::kAccepted && !llc.contains(3));
 auto duplicate=ledger.issue(10,1,3,10,5);assert(duplicate.status==L::Issue::kDuplicate && duplicate.ticket==a.ticket);
 assert(ledger.demand(10,3).value()==a.ticket);assert(!ledger.demand(10,4));
 assert(ledger.select(a.ticket,30)==L::Select::kSelected);
 assert(ledger.select(a.ticket,30)==L::Select::kDuplicate);
 std::vector<uint64_t> victim;
 auto commit=[&](const L::Entry& e,uint64_t publication){
   auto p=publish_spr_shared_prefetch(llc,e.line,e.issue,publication,publication,counters,&tx,
       []{return SprPrefetchPermission::kValid;},[&](uint64_t l,bool d,uint64_t at){assert(at==30);if(d)victim.push_back(l);});
   assert(p==SprSharedPrefetchPublication::kPublished);return L::Commit::kPublished;
 };
 assert(ledger.complete(29,commit).published==0 && !llc.contains(3));
 assert(ledger.demand(29,3).value()==a.ticket);
 assert(ledger.complete(30,commit).published==1 && llc.contains(3) && victim.size()==1 && victim[0]==1);
 llc.restore(tx);ledger=snap;victim.clear();assert(llc.contains(1) && !llc.contains(3));
 auto replacement=ledger.issue(10,0,3,10,6);assert(replacement.ticket!=a.ticket);
 assert(ledger.select(a.ticket,50)==L::Select::kStale);
 assert(ledger.cancel_line(10,3).value()==replacement.ticket);
 assert(ledger.select(replacement.ticket,50)==L::Select::kStale && !ledger.demand(10,3));
 auto b=ledger.issue(10,0,4,10,7),c=ledger.issue(10,0,5,10,8);
 assert(b.status==L::Issue::kAccepted && c.status==L::Issue::kAccepted);
 assert(ledger.issue(10,0,6,10,9).status==L::Issue::kCapacity);
 ledger.select(b.ticket,12);ledger.select(c.ticket,12);
 unsigned calls=0;auto x=ledger.complete(12,[&](const L::Entry& e,uint64_t now){assert(now==12);++calls;return e.ticket==b.ticket?L::Commit::kRetry:L::Commit::kPermissionLost;});
 assert(x.deferred==1 && x.cancelled==1 && calls==2 && ledger.size()==1);
 bool backwards=false;try{ledger.demand(11,4);}catch(const std::invalid_argument&){backwards=true;}assert(backwards);
 auto before_exception=ledger;bool failed=false;
 try { ledger.complete(15,[&](const L::Entry& e,uint64_t now)->L::Commit{
   assert(*e.response==12 && now==15);throw std::runtime_error("external transaction failed");
 }); } catch(const std::runtime_error&) {failed=true;}
 assert(failed);ledger=before_exception;
 assert(ledger.demand(12,4).value()==b.ticket);
 assert(ledger.complete(15,[&](const L::Entry& e,uint64_t now){
   assert(*e.response==12 && now==15);return L::Commit::kPermissionLost;
 }).cancelled==1);
 SprPrefetchGrantTable grants(2);SprPrefetchGrantTable::Bitmap sharers{};sharers[0]=1;
 assert(grants.track(9,100,sharers)==SprPrefetchGrantTable::Track::kAdded);
 auto old=ledger.issue(15,0,9,15,100);ledger.select(old.ticket,20);
 auto empty=SprPrefetchGrantTable::Bitmap{};
 grants.transition(9,100,101,empty);grants.transition(9,101,102,sharers);
 assert(ledger.complete(20,[&](const L::Entry& e,uint64_t){
    assert(grants.validate(e.line,e.core,e.grant_generation)==SprPrefetchGrantTable::Validation::kRevoked);
    return L::Commit::kPermissionLost;
 }).cancelled==1);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
