"""Certificate-only publication fence; not a Simulator integration test."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class SprPrefetchPublicationFenceTest(unittest.TestCase):
 def test_fail_closed_equal_timestamp_and_rollback(self):
  code=r'''
#include "fastsim/spr_prefetch_publication_fence.hpp"
#include <cassert>
#include <limits>
#include <stdexcept>
using fastsim::SprPrefetchPublicationFence;
int main() {
 SprPrefetchPublicationFence fence(2);
 assert(!fence.can_publish(5,10,true)); // no proof for either stream
 fence.certify_future_lower_bound(0,10);
 assert(!fence.can_publish(5,10,true)); // core 1 remains unknown
 fence.certify_future_lower_bound(1,20);
 assert(fence.can_publish(9,10,true));
 assert(!fence.can_publish(10,10,true)); // same-time order unknown
 assert(!fence.can_publish(9,8,true));   // modeled response not ready
 assert(!fence.can_publish(9,10,false));  // cache transaction not committed
 auto snapshot=fence;
 fence.certify_future_lower_bound(0,15);
 assert(fence.can_publish(10,11,true));
 assert(!snapshot.can_publish(10,11,true)); // preview rollback
 bool backwards=false;
 try{fence.certify_future_lower_bound(0,14);}catch(const std::logic_error&){backwards=true;}
 assert(backwards && fence.can_publish(10,11,true));
 fence.finish(0);fence.finish(1);
 assert(fence.can_publish(UINT64_MAX,UINT64_MAX,true));
 bool after_end=false;
 try{fence.certify_future_lower_bound(0,99);}catch(const std::logic_error&){after_end=true;}
 assert(after_end);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(p.returncode,0,p.stderr)
   p=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
