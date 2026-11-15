"""Transactional, per-line L1 prefetch marks do not alias set activity."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CachePrefetchMark(unittest.TestCase):
    def test_lifetime_and_rollback(self):
        code = r'''
#include "fastsim/cache.hpp"
#include <cassert>
using namespace fastsim;
int main(){
 static_assert(sizeof(CacheLine)==16,"prefetch bit must fit existing line padding");
 CacheConfig cfg;cfg.size_bytes=128;cfg.associativity=2;
 SetAssociativeCache cache(cfg);CacheCounters stats;
 assert(!cache.mark_prefetched(1)); // absent line cannot acquire a mark
 cache.complete_fill(1,false,stats);assert(cache.mark_prefetched(1));
 cache.complete_fill(2,false,stats);cache.probe(2,false,stats); // same set, other way
 auto tx=cache.begin_transaction();
 assert(cache.consume_prefetched(1,&tx));assert(!cache.consume_prefetched(1,&tx));
 cache.restore(tx);assert(cache.consume_prefetched(1));
 assert(!cache.consume_prefetched(1)); // exactly once
 assert(cache.mark_prefetched(1));cache.invalidate(1);cache.complete_fill(1,false,stats);
 assert(!cache.consume_prefetched(1)); // invalidate/reinsert cannot inherit
 assert(cache.mark_prefetched(1));
 cache.probe(2,false,stats);cache.complete_fill(3,false,stats); // LRU replaces 1
 cache.complete_fill(1,false,stats);
 assert(!cache.consume_prefetched(1)); // eviction/reinsert cannot inherit
 auto tx2=cache.begin_transaction();assert(cache.mark_prefetched(1,&tx2));
 cache.restore(tx2);assert(!cache.consume_prefetched(1));
 assert(cache.mark_prefetched(1));
 auto tx3=cache.begin_transaction();cache.mark_dirty(1,&tx3);
 assert(!cache.consume_prefetched(1,&tx3)); // buffered write publication
 cache.restore(tx3);assert(cache.consume_prefetched(1)); // publication rollback
 assert(cache.mark_prefetched(1));assert(cache.mark_prefetched(3));
 cache.mark_dirty(1);assert(!cache.consume_prefetched(1));
 assert(cache.consume_prefetched(3)); // a different way keeps its mark
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            src, exe = Path(d) / 'test.cc', Path(d) / 'test'
            src.write_text(code)
            c = subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17', '-O2', '-static-libstdc++', '-static-libgcc', '-I', str(ROOT / 'include'), str(ROOT / 'src/cache.cpp'), str(src), '-o', str(exe)], capture_output=True, text=True)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
