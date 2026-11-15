"""Shared prefetch installation and dirty eviction at modeled completion."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchSharedPublishTest(unittest.TestCase):
 def test_shared_fill_visibility_and_outer_rollback(self):
  code=r'''
#include "fastsim/spr_shared_prefetch_publish.hpp"
#include <cassert>
#include <unordered_map>
#include <vector>
using namespace fastsim;
CacheConfig config(){CacheConfig c;c.size_bytes=128;c.associativity=2;return c;}
int main() {
 SetAssociativeCache llc(config());CacheCounters initial,pre;
 llc.complete_fill(1,true,initial);llc.complete_fill(2,false,initial);
 auto tx=llc.begin_transaction();
 const auto probe=llc.probe(3,false,pre,&tx);assert(!probe.hit && !llc.contains(3));
 std::unordered_map<uint64_t,uint64_t> directory{{1,44},{2,55}};auto directory_before=directory;
 std::vector<uint64_t> writebacks;unsigned permission_calls=0;
 auto permission=[&](){++permission_calls;return SprPrefetchPermission::kValid;};
 auto evict=[&](uint64_t line,bool dirty,uint64_t ready){assert(ready==30);directory.erase(line);if(dirty)writebacks.push_back(line);};
 const auto early=publish_spr_shared_prefetch(llc,3,10,30,29,pre,&tx,permission,evict);
 assert(early==SprSharedPrefetchPublication::kNotReady && !llc.contains(3) && permission_calls==0);
 auto unknown=[](){return SprPrefetchPermission::kUnknown;};
 const auto retry=publish_spr_shared_prefetch(llc,3,10,30,30,pre,&tx,unknown,evict);
 assert(retry==SprSharedPrefetchPublication::kRetry && !llc.contains(3) && directory==directory_before && writebacks.empty());
 auto lost=[](){return SprPrefetchPermission::kRevoked;};
 const auto cancel=publish_spr_shared_prefetch(llc,3,10,30,30,pre,&tx,lost,evict);
 assert(cancel==SprSharedPrefetchPublication::kPermissionLost && !llc.contains(3));
 // Another true demand touches line 2 before response; install chooses victim at response.
 llc.probe(2,false,pre,&tx);
 const auto done=publish_spr_shared_prefetch(llc,3,10,30,30,pre,&tx,permission,evict);
 assert(done==SprSharedPrefetchPublication::kPublished && llc.contains(3) && !llc.contains(1));
 assert(writebacks.size()==1 && writebacks[0]==1 && !directory.count(1));
 assert(pre.accesses==2 && pre.hits==1 && pre.misses==1); // publication is not another lookup
 // Restoring LLC alone is insufficient: restore external directory and writeback ledger too.
 llc.restore(tx);directory=directory_before;writebacks.clear();
 assert(!llc.contains(3) && llc.contains(1) && llc.contains(2) && directory.count(1));
 // A rejected shared writeback happens after cache mutation: the outer batch
 // restores LLC, directory, writeback state and caller-owned counters together.
 auto tx2=llc.begin_transaction();const auto pre_before=pre;
 auto failing_evict=[&](uint64_t line,bool dirty,uint64_t){directory.erase(line);if(dirty)writebacks.push_back(line);throw std::runtime_error("writeback rejected");};
 bool failed=false;try{publish_spr_shared_prefetch(llc,3,10,30,30,pre,&tx2,permission,failing_evict);}catch(...){failed=true;}assert(failed);
 llc.restore(tx2);directory=directory_before;writebacks.clear();pre=pre_before;
 assert(llc.contains(1) && llc.contains(2) && !llc.contains(3) && directory.count(1));
 bool bad=false;try{publish_spr_shared_prefetch(llc,3,31,30,40,pre,&tx,permission,evict);}catch(...){bad=true;}assert(bad);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
