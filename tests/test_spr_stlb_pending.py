"""Per-core modeled STLB pending fills and invalidation generations."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class SprStlbPendingTest(unittest.TestCase):
 def test_fill_hit_generation_snapshot_and_time_reversal(self):
  code=r'''
#include "fastsim/spr_stlb_pending.hpp"
#include <cassert>
using P=fastsim::SprStlbPending;
using S=P::Status;
int main(){
 P p(4,6);P::Key a{1,0,0x1000,12,false};
 assert(p.lookup(a,0).status==S::Unknown);
 assert(!p.start_clean_epoch(0,false));
 assert(p.start_clean_epoch(0,true));
 assert(p.schedule_fill(a,0,10)==S::Queued);
 assert(p.lookup(a,9).status==S::Miss);
 auto hit=p.lookup(a,10);assert(hit.status==S::HitPending && hit.ready==16 && hit.id);
 assert(p.complete_hit(hit.id,15)==S::NotReady);
 assert(p.invalidate_all(15)==S::Done);
 assert(p.complete_hit(hit.id,16)==S::Invalidated);
 assert(p.lookup(a,16).status==S::Miss);
 assert(p.schedule_fill(a,16,30)==S::Queued);
 assert(p.invalidate_all(20)==S::Done);
 assert(p.lookup(a,31).status==S::Miss); // invalidated walk never installs

 assert(p.schedule_fill(a,31,40)==S::Queued);
 auto snapshot=p;
 assert(p.invalidate_all(35)==S::Done);
 assert(p.lookup(a,40).status==S::Miss);
 auto saved_hit=snapshot.lookup(a,40);assert(saved_hit.status==S::HitPending);
 assert(snapshot.complete_hit(saved_hit.id,46)==S::Done);
 assert(p.lookup(a,39).status==S::TimeRegression); // cannot see future t=40
 assert(p.lookup(a,41).status==S::Unknown); // remains poisoned until known clean
 assert(!p.start_clean_epoch(41,false));assert(p.lookup(a,41).status==S::Unknown);
 assert(p.start_clean_epoch(41,true));assert(p.lookup(a,41).status==S::Miss);
 P::Key pcid{1,1,0x1000,12,false};assert(p.lookup(pcid,41).status==S::Unsupported);
 assert(p.lookup(a,41).status==S::Unknown);assert(p.start_clean_epoch(41,true));
 P::Key global{1,0,0x1000,12,true};assert(p.lookup(global,41).status==S::Unsupported);
 assert(p.start_clean_epoch(41,true));
 P::Key huge{1,0,0x200000,21,false};assert(p.lookup(huge,41).status==S::Unsupported);
 assert(p.start_clean_epoch(41,true));
 assert(p.schedule_fill(a,41,40)==S::InvalidTime);

 P bound(1,6);assert(bound.start_clean_epoch(0,true));
 assert(bound.schedule_fill(a,0,10)==S::Queued);
 assert(bound.schedule_fill(P::Key{1,0,0x2000,12,false},0,10)==S::Capacity);
 assert(bound.lookup(a,1).status==S::Unknown);assert(bound.start_clean_epoch(1,true));
 assert(bound.schedule_fill(a,1,2)==S::Queued);
 auto h=bound.lookup(a,2);assert(h.status==S::HitPending);
 assert(bound.lookup(a,2).status==S::Capacity);
 assert(bound.complete_hit(h.id,8)==S::Unknown);
 assert(bound.start_clean_epoch(8,true));assert(bound.complete_hit(h.id,8)==S::Stale);

 P tie(2,6);assert(tie.start_clean_epoch(0,true));
 assert(tie.schedule_fill(a,0,10)==S::Queued);
 assert(tie.invalidate_all(10)==S::Done);
 assert(tie.lookup(a,10).status==S::Miss); // invalidation wins after same-time fill
 assert(tie.schedule_fill(a,10,10)==S::Queued);
 auto ht=tie.lookup(a,10);assert(ht.status==S::HitPending);
 assert(tie.invalidate_all(16)==S::Done);
 assert(tie.complete_hit(ht.id,16)==S::Invalidated); // explicit control first

 P rollback(3,6);assert(rollback.start_clean_epoch(0,true));
 assert(rollback.schedule_fill(a,0,0)==S::Queued);
 auto before=rollback.lookup(a,0);auto copy=rollback;
 auto stale=rollback.lookup(a,0);rollback=copy;
 assert(rollback.complete_hit(stale.id,6)==S::Stale);
 assert(rollback.complete_hit(before.id,6)==S::Done);
 auto next=rollback.lookup(a,6);assert(next.id!=stale.id);
 P other(2,6);assert(other.start_clean_epoch(0,true));
 assert(other.lookup(a,0).status==S::Miss); // independent per-core state
 other.abandon_unknown();assert(other.lookup(a,0).status==S::Unknown);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(p.returncode,0,p.stderr)
   p=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
