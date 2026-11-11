"""Typed coordinator between pending prefetch, cache and directory hooks."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchPublishTest(unittest.TestCase):
 def test_permission_retry_dirty_victim_and_rollback(self):
  code=r'''
#include "fastsim/spr_prefetch_publish.hpp"
#include <cassert>
#include <vector>
using namespace fastsim;
CacheConfig cfg(){CacheConfig c;c.size_bytes=64;c.associativity=1;return c;}
int main() {
 using S=SprPrefetchService;
 PrivateHierarchy cache(cfg(),cfg());CoreCounters demand,prefetch;
 cache.complete_fill(1,true,demand);
 S services(2);assert(services.begin_at(10,1,0,3,10,S::Level::kL1,77)==S::Begin::kAccepted);
 assert(services.select_response(1,30)==S::Select::kSelected);
 bool grant=false,unknown=true;std::vector<uint64_t> writes;
 auto tx=cache.begin_transaction();auto snap=services;auto pref_before=prefetch;
 auto lookup=[&](const S::Entry& e){assert(e.grant_generation==77);return unknown?SprPrefetchPermission::kUnknown:grant?SprPrefetchPermission::kValid:SprPrefetchPermission::kRevoked;};
 auto evict=[&](uint32_t core,uint64_t line,bool dirty,uint64_t cycle){assert(core==0 && (cycle==30 || cycle==35));if(dirty)writes.push_back(line);};
 auto publish=[&](const S::Entry& e){return publish_spr_prefetch(e,30,cache,prefetch,&tx,lookup,evict);};
 auto before=services.complete_through(29,publish);assert(before.published==0 && !cache.contains(3));
 auto retry=services.complete_through(30,publish);assert(retry.deferred==1 && services.size()==1 && cache.contains(1) && writes.empty());
 unknown=false;grant=true;
 auto done=services.complete_through(30,publish);assert(done.published==1 && cache.contains(3) && writes.size()==1 && writes[0]==1);
 assert(prefetch.l1d.accesses==0 && prefetch.l2.accesses==0);
 // Restoring all external and pending state makes abandoned trial disappear.
 cache.restore(tx);services=snap;prefetch=pref_before;writes.clear();
 assert(cache.contains(1) && !cache.contains(3));
 grant=false;
 auto cancel=services.complete_through(30,publish);assert(cancel.cancelled==1 && cache.contains(1) && !cache.contains(3) && writes.empty());
 // Private L2 prefetch must not install in L1.
 assert(services.begin_at(31,2,0,9,31,S::Level::kL2,77)==S::Begin::kAccepted);
 assert(services.select_response(2,35)==S::Select::kSelected);grant=true;
 auto tx2=cache.begin_transaction();
 auto done2=services.complete_through(35,[&](const S::Entry& e){return publish_spr_prefetch(e,35,cache,prefetch,&tx2,lookup,evict);});
 assert(done2.published==1);
 CoreCounters check;assert(cache.probe(9,false,check).level==HitLevel::kL2);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
