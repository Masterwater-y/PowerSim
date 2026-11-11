"""Transaction rollback ordering; arbitrary nested commits are not covered."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CacheUndoSnapshotOrderTest(unittest.TestCase):
    def test_repeated_set_and_untouched_nested_set(self):
        code = r'''
#include "fastsim/cache.hpp"
#include <cassert>
using namespace fastsim;
int main() {
 CacheConfig cfg;cfg.line_size=64;cfg.associativity=1;cfg.size_bytes=128;
 SetAssociativeCache cache(cfg);CacheCounters counters;
 // Lines 0, 2 and 4 all map to set 0. The inner restore must not
 // change which image the outer transaction restores for that set.
 cache.access(0,true,counters);
 auto before=cache.prepare_lookup(0);
 auto outer=cache.begin_transaction();
 cache.access(2,false,counters,&outer);
 auto inner=cache.begin_transaction();
 cache.access(4,false,counters,&inner);
 cache.restore(inner);
 assert(cache.contains(2));
 cache.access(4,true,counters,&outer);
 assert(outer.snapshots.size()==2);
 cache.restore(outer);
 assert(cache.contains(0) && !cache.contains(2) && !cache.contains(4));
 assert(cache.can_commit(before)); // original mutation generation restored
 auto evicted=cache.access(2,false,counters);
 assert(evicted.evicted && evicted.evicted_line==0 && evicted.evicted_dirty);
 cache.access(0,false,counters);

 // Scope boundary: an inner committed change to a set that the outer
 // transaction never touched cannot be undone by the outer transaction.
 auto parent=cache.begin_transaction();
 cache.access(2,false,counters,&parent); // set 0 only
 auto child=cache.begin_transaction();
 cache.access(1,false,counters,&child);  // set 1 only, child is retained
 cache.restore(parent);
 assert(cache.contains(0) && cache.contains(1));
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            source=Path(directory)/'test.cc'; binary=Path(directory)/'test'
            source.write_text(code)
            result=subprocess.run([
                '/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror',
                '-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),
                str(source),str(ROOT/'src/cache.cpp'),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
