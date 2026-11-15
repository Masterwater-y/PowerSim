"""LLC-resident L2 prefetch: no early fill, no DRAM and rollback safety."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class LlcResidentPrefetchTest(unittest.TestCase):
 def test_residency_completion_cancellation_and_restore(self):
  code=r'''
#include "fastsim/spr_llc_resident_prefetch.hpp"
#include <cassert>
using namespace fastsim;
CacheConfig cfg(unsigned lines,unsigned ways){CacheConfig c;c.size_bytes=64*lines;c.associativity=ways;return c;}
int main(){
 SetAssociativeCache llc(cfg(8,2));CacheCounters scratch;
 PrivateHierarchy cache(cfg(1,1),cfg(2,2));CoreCounters seed;llc.complete_fill(7,false,scratch);
 SprLlcResidentPrefetch p(1,2);std::vector<PrivateHierarchy*> caches{&cache};std::vector<PrivateTransaction*> tx{nullptr};
 auto evict=[](uint32_t,uint64_t,bool dirty,uint64_t){assert(!dirty);};
 auto permission=[](uint32_t,uint64_t){return true;};
 unsigned publications=0; auto publish=[&](uint32_t,uint64_t){++publications;};
 using R=SprLlcResidentPrefetch::Begin;
 assert(p.begin(0,8,2,8,true,llc,cache)==R::kLlcMissing);
 assert(p.begin(0,7,2,8,false,llc,cache)==R::kUncertified);
 assert(p.begin(0,7,2,8,true,llc,cache)==R::kIssued);
 p.advance(7,llc,caches,tx,permission,publish,evict);assert(!cache.contains(7) && publications==0);
 auto snapshot=p;auto c_tx=cache.begin_transaction();std::vector<PrivateTransaction*> txn{&c_tx};
 auto warm=snapshot;warm.reset_counters();assert(warm.initial_pending()==1 && warm.stats().issued==0);
 p.advance(8,llc,caches,txn,permission,publish,evict);assert(cache.contains(7) && publications==1);
 cache.restore(c_tx);p=snapshot;assert(!cache.contains(7) && p.pending()==1);
 p.cancel_before_write(7,8);p.advance(8,llc,caches,tx,permission,publish,evict);assert(!cache.contains(7));
 assert(p.stats().cancelled_write==1 && p.stats().completed==0);
 // A source evicted before response cannot be used for completion.
 SprLlcResidentPrefetch stale(1,2);assert(stale.begin(0,7,9,12,true,llc,cache)==R::kIssued);
 bool was_dirty=false;llc.invalidate(7,&was_dirty);stale.advance(12,llc,caches,tx,permission,publish,evict);
 assert(!cache.contains(7) && stale.stats().cancelled_source==1);
 llc.complete_fill(7,false,scratch);
 // A dirty inclusive victim cannot trigger an unmodelled writeback.
 PrivateHierarchy dirty(cfg(2,2),cfg(2,2));dirty.complete_fill(1,false,seed);dirty.complete_fill(2,false,seed);
 dirty.access(1,true,seed);dirty.access(2,false,seed);
 SprLlcResidentPrefetch blocked(1,2);std::vector<PrivateHierarchy*> d{&dirty};
 assert(blocked.begin(0,7,13,14,true,llc,dirty)==R::kIssued);
 blocked.advance(14,llc,d,tx,permission,publish,evict);assert(!dirty.contains(7));
 assert(blocked.stats().skipped_dirty_victim==1);
 SprLlcResidentPrefetch revoked(1,2);assert(revoked.begin(0,7,15,16,true,llc,cache)==R::kIssued);
 revoked.advance(16,llc,caches,tx,[](uint32_t,uint64_t){return false;},publish,evict);
 assert(!cache.contains(7) && revoked.stats().cancelled_permission==1);
 SprLlcResidentPrefetch demand(1,2);assert(demand.begin(0,7,17,20,true,llc,cache)==R::kIssued);
 demand.cancel_line(7,18,false);demand.advance(20,llc,caches,tx,permission,publish,evict);
 assert(!cache.contains(7) && demand.stats().cancelled_demand==1 && demand.stats().cancelled_write==0);
 assert(p.stats().skipped_llc_missing==1 && p.stats().skipped_uncertified==1);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cpp';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
