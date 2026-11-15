"""Production Ruby predictor candidate queues, dedup and snapshot."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprRubyPrefetcherTest(unittest.TestCase):
 def test_candidate_queue_and_state(self):
  code=r'''
#include "fastsim/spr_ruby_prefetcher.hpp"
#include <cassert>
int main() {
 using fastsim::SprRubyPrefetcher;
 SprRubyPrefetcher capped(true,1);
 capped.observe_adjacent(0x1000);capped.observe_adjacent(0x2000);
 assert(capped.queued()==1 && capped.overflow_candidates()==1);
 SprRubyPrefetcher p(true);
 for(unsigned i=0;i<4;++i) p.observe_data_access(0x100,0,false,0x1000+i*64,0x8000+i*64);
 assert(p.queued()==1 && p.duplicate_candidates()==0);
 assert(p.peek()==0x8100 && p.queued()==1); // capacity checks must not remove the candidate
 p.observe_adjacent(0x8140); // adjacent = 0x8100, already enqueued
 assert(p.queued()==1 && p.duplicate_candidates()==1);
 auto snapshot=p;
 assert(p.dequeue()==0x8100 && !p.dequeue());
 assert(snapshot.dequeue()==0x8100);
 p.observe_adjacent(0x8140);assert(p.dequeue()==0x8100); // after dequeue it may re-enter
 SprRubyPrefetcher l2(false);
 for(unsigned i=0;i<8;++i) l2.observe_data_access(0x100,0,false,0x1000+i*64,0x8000+i*64);
 assert(l2.queued()==0);
 for(unsigned i=0;i<5;++i) l2.observe_data_miss(0x1000+i*64,i);
 assert(l2.dequeue()==0x1140);
 l2.observe_prefetch_access(0x1140,5);assert(l2.dequeue()==0x1180);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(binary)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
