"""Source-slot-only fusion reservation with independently charged IDQ/rename."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class FusionSourceReservationTest(unittest.TestCase):
 def test_model_cycle_and_credit_gates(self):
  code=r'''
#include "fastsim/spr_fusion_source_reservation.hpp"
#include <cassert>
using namespace fastsim;
using R=SprFusionSourceReservation;
SprFusionPair pair() {
 SprFusionPair p;p.producer_pc=0x1000;p.branch_pc=0x1002;
 p.producer_asid=1;p.branch_asid=1;p.producer_size=2;p.branch_condition=4;
 p.allowed_jcc_mask=0xf0fc;p.producer_from_user=true;p.branch_from_user=true;
 p.producer_alu=true;p.producer_group_ready=true;p.producer_has_fused_branch=false;
 p.branch_single=true;p.branch_microcode=false;return p;
}
R::Producer parent() {return {20,7,4,SprFrontendSource::Mite,true};}
R::Offer offer() {return {21,7,4,SprFrontendSource::Mite,8,8,8,32,true};}
int main() {
 R r;r.note_producer(parent());auto p=pair();auto o=offer();
 // Source full: the branch can reuse the proved producer slot. IDQ and
 // rename stay separately charged, and preview has no mutation.
 auto preview=r.preview(p,o);assert(preview.decision==R::Decision::Eligible);
 assert(preview.source_slots==0 && preview.idq_slots==1 && preview.rename_slots==1 && preview.uops==1);
 assert(r.preview(p,o).decision==R::Decision::Eligible);
 auto committed=r.reserve(p,o);assert(committed.decision==R::Decision::Eligible);
 assert(r.reserve(p,o).decision==R::Decision::Ineligible);
 assert(r.counters().fused==1);
 // A copied cross-chunk state retains the producer and not shared mutation.
 R chunks;chunks.note_producer(parent());auto snapshot=chunks;
 assert(chunks.reserve(p,o).decision==R::Decision::Eligible);
 assert(snapshot.reserve(p,o).decision==R::Decision::Eligible);
 // Rename/IDQ pressure prevents service, without consuming the candidate.
 R blocked;blocked.note_producer(parent());o.idq_available=false;
 assert(blocked.reserve(p,o).decision==R::Decision::Blocked);
 assert(blocked.counters().eligible==1 && blocked.counters().fused==0);
 o.idq_available=true;assert(blocked.reserve(p,o).decision==R::Decision::Eligible);
 assert(blocked.counters().eligible==2 && blocked.counters().fused==1);
 // Batch identity alone never overrides model service-cycle mismatch.
 R delayed;delayed.note_producer(parent());o=offer();o.service_cycle=5;
 assert(delayed.reserve(p,o).decision==R::Decision::Ineligible);
 o.service_cycle.reset();assert(delayed.reserve(p,o).decision==R::Decision::Unknown);
 o=offer();o.batch_id=0;assert(delayed.reserve(p,o).decision==R::Decision::Unknown);
 o=offer();o.batch_id=8;assert(delayed.reserve(p,o).decision==R::Decision::Ineligible);
 o=offer();o.source=SprFrontendSource::Dsb;assert(delayed.reserve(p,o).decision==R::Decision::Ineligible);
 o=offer();o.ops_used=32;assert(delayed.reserve(p,o).decision==R::Decision::Blocked);
 o=offer();o.source_slots_used=0;assert(delayed.reserve(p,o).decision==R::Decision::Unknown);
 o=offer();o.sequence=22;assert(delayed.reserve(p,o).decision==R::Decision::Ineligible);
 auto missing=p;missing.producer_alu.reset();o=offer();
 assert(delayed.reserve(missing,o).decision==R::Decision::Unknown);
 delayed.clear();assert(delayed.reserve(p,o).decision==R::Decision::Unknown);
 R warm;warm.note_producer(parent());warm.reset_counters();
 assert(warm.reserve(p,o).decision==R::Decision::Eligible);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   result=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   result=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
