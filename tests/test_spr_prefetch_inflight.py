"""Prefetch completion is visible only when service actually finishes."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchInflightTest(unittest.TestCase):
 def test_completion_merge_and_rollback(self):
  code=r'''
#include "fastsim/spr_prefetch_inflight.hpp"
#include <cassert>
#include <vector>
int main() {
 fastsim::SprPrefetchInflight q(2);std::vector<unsigned long> visible;
 assert(q.start(1,10,30));assert(q.start(2,12,25));
 assert(!q.start(3,13,29));assert(!q.start(1,13,29));
 assert(!q.pending_until(1,9)); // a future request cannot capture an earlier demand
 assert(!q.pending_until(1,30)); // completion is not an in-flight interval
 assert(q.pending_until(1,10)==30 && q.pending_until(2,12)==25);
 q.complete_through(24,[&](auto line,auto){visible.push_back(line);});assert(visible.empty());
 auto snap=q;
 q.complete_through(25,[&](auto line,auto){visible.push_back(line);});assert(visible.size()==1 && visible[0]==2);
 assert(q.start(3,26,35));assert(!q.pending_until(2,26));
 q=snap;assert(q.pending_until(2,12)==25 && !q.pending_until(3,26));
 q.complete_through(40,[&](auto line,auto){visible.push_back(line);});
 assert(visible.size()==3 && visible[1]==2 && visible[2]==1);
 bool bad=false;try{q.start(4,40,39);}catch(...){bad=true;}assert(bad);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(binary)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
