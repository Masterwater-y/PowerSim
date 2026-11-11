"""A consumed completed L1 prefetch continues the trained Ruby stream."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SprL1StreamHit(unittest.TestCase):
    def test_stream_hit_continues(self):
        code = r'''
#include "fastsim/spr_l2_resident_prefetch.hpp"
#include <cassert>
using namespace fastsim;
CacheConfig cfg(unsigned n){CacheConfig c;c.size_bytes=64*n;c.associativity=1;return c;}
int main(){
 PrivateHierarchy cache(cfg(1),cfg(128)); CoreCounters counters;
 cache.complete_fill(0x8140/64,false,counters);
 cache.complete_fill(0x8180/64,false,counters);
 cache.complete_fill(0xa000/64,false,counters);
 SprL2ResidentPrefetch engine(1,2,8,true,1);
 std::vector<PrivateHierarchy*> caches{&cache};
 std::vector<PrivateTransaction*> tx{nullptr};
 auto evict=[](uint32_t,uint64_t,bool,uint64_t){assert(false);};
 for(unsigned i=0;i<5;++i){
  SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100+i,0,0x1000+i*64,0x8000+i*64};
  engine.advance(i,caches,tx,evict);engine.observe(0,x,HitLevel::kL2,i,10,cache,nullptr);
 }
 assert(engine.stats().issued==1);
 engine.advance(13,caches,tx,evict);
 assert(!engine.consume_stream_hit(0,0x8140/64,cache));
 engine.advance(14,caches,tx,evict);
 auto snapshot=engine; auto transaction=cache.begin_transaction();
 assert(engine.consume_stream_hit(0,0x8140/64,cache));
 assert(!engine.consume_stream_hit(0,0x8140/64,cache));
 cache.probe(0x8140/64,false,counters,&transaction);
 cache.restore(transaction);engine=snapshot;
 assert(engine.consume_stream_hit(0,0x8140/64,cache));
 assert(cache.probe(0x8140/64,false,counters).level==HitLevel::kL1);
 SprPrefetchInput hit{SprPrefetchInputStatus::kEligible,0x200,0,0x1140,0x8140};
 engine.observe(0,hit,HitLevel::kL1,15,10,cache,nullptr,true);
 assert(engine.stats().issued==2);
 engine.advance(25,caches,tx,evict);
 cache.complete_fill(0xa000/64,false,counters); // evict marked line
 cache.complete_fill(0x8180/64,false,counters); // reinsert same line (ABA)
 assert(!engine.consume_stream_hit(0,0x8180/64,cache));
 assert(engine.stats().stream_stale==1);
 engine.note_stream_consumed_not_used();
 assert(engine.stats().stream_consumed_not_used==1);
 // New opt-in line-local marks survive another way in the same L1 set.
 CacheConfig c2=cfg(2);c2.associativity=2;
 PrivateHierarchy local(c2,cfg(128));CoreCounters x;
 local.complete_fill(0x8140/64,false,x);local.complete_fill(0x8180/64,false,x);
 local.complete_fill(0xa000/64,false,x);local.complete_fill(0xa040/64,false,x);
 SprL2ResidentPrefetch line_engine(1,2,8,true,1,true);
 std::vector<PrivateHierarchy*> lc{&local};
 for(unsigned i=0;i<5;++i){
  SprPrefetchInput input{SprPrefetchInputStatus::kEligible,0x300+i,0,0x1000+i*64,0x8000+i*64};
  line_engine.advance(i,lc,tx,evict);line_engine.observe(0,input,HitLevel::kL2,i,10,local,nullptr);
 }
 line_engine.advance(14,lc,tx,evict);
 local.probe(0xa040/64,false,x); // same set, different resident line
 auto local_snapshot=line_engine;auto local_tx=local.begin_transaction();
 assert(line_engine.consume_stream_hit(0,0x8140/64,local,&local_tx));
 local.restore(local_tx);line_engine=local_snapshot;
 assert(line_engine.consume_stream_hit(0,0x8140/64,local));
 assert(!line_engine.consume_stream_hit(0,0x8140/64,local));
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            src, exe = Path(d) / 'test.cc', Path(d) / 'test'
            src.write_text(code)
            c = subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17', '-O2', '-static-libstdc++', '-static-libgcc', '-I', str(ROOT / 'include'), str(ROOT / 'src/cache.cpp'), str(src), '-o', str(exe)], capture_output=True, text=True)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
