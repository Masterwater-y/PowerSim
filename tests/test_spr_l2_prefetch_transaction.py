"""Transactional state for the shared L2 prefetch replay."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class PrefetchTransactionTest(unittest.TestCase):
    def test_rollback_restores_pending_predictor_frontier_and_counters(self):
        code = r'''
#include "fastsim/spr_l2_shared_prefetch_state.hpp"
#include <cassert>
using namespace fastsim;
int main() {
 SprL2SharedPrefetchState state(2, 8, 4);
 using P = SprL2RubyPrefetcher;
 state.predictors[0].observe({P::Action::kMiss,P::Request::kGetS,0x8000,3,true,std::nullopt});
 state.pending.push_back({0,0x200,3,20});
 state.note_source_response(3,100);
 state.note_source_response(3+4096,80);
 assert(state.source_ready[3]==100); // collision retains the later callback
 state.frontier=4; state.issued=1; state.trained=1;
 const auto snapshot = state;
 const auto abandoned_service=state.ids.allocate();
 state.predictors[0].observe({P::Action::kMiss,P::Request::kGetS,0xa000,7,true,std::nullopt});
 state.pending.clear(); state.frontier=20; state.completed=1; state.cancelled_write=1;
 state = snapshot;
 assert(state.ids.allocate()>abandoned_service); // rollback cannot reuse a service ID
 assert(state.pending.size()==1 && state.pending[0].line==0x200);
 assert(state.frontier==4 && state.issued==1 && state.completed==0 && state.cancelled_write==0);
 assert(state.predictors[0].queued()==snapshot.predictors[0].queued());
 auto head=state.predictors[0].peek(4); assert(head);
 assert(state.predictors[0].consume(4,head->handle));
 assert(state.predictors[0].queued()+1==snapshot.predictors[0].queued());
 state.predictors[0].observe({P::Action::kMiss,P::Request::kGetS,0xa000,7,true,std::nullopt});
 auto discarded=state.predictors[0].peek(8); assert(discarded);
 state=snapshot;
 head=state.predictors[0].peek(4); assert(head);
 assert(state.predictors[0].consume(4,head->handle));
 state.predictors[0].observe({P::Action::kMiss,P::Request::kGetS,0xa000,7,true,std::nullopt});
 auto replacement=state.predictors[0].peek(8); assert(replacement);
 assert(replacement->handle.ticket>discarded->handle.ticket);
 assert(!state.predictors[0].consume(8,discarded->handle));
 state.pending.push_back({0,0x300,7,9});
 state.pending.push_back({1,0x300,7,9});
 state.pending.push_back({1,0x400,7,9});
 state.cancel_before_write(0x300,0);
 assert(state.pending.size()==2 && state.pending.back().line==0x400);
 assert(state.cancelled_demand==2 && state.cancelled_write==2 && state.cancelled_cross_core==1);
 assert(state.issued==1); // cancellation never removes already-issued traffic
 state.demand_fills={{0,0x500,30},{1,0x500,30},{0,0x600,30}};
 auto fills=state;
 state.invalidate_demand_fill(0,0x500);
 assert(state.demand_fills.size()==2 && state.demand_fill_dropped==1);
 state=fills;
 state.cancel_before_write(0x500,1);
 assert(state.demand_fills.size()==1 && state.demand_fills[0].line==0x600);
 assert(state.demand_fill_dropped==2);
 // A demand-only controller repair cannot replace the DRAM state of any
 // epoch that begins with, issues, or ends with a shared prefetch in flight.
 SprL2SharedPrefetchState epoch(1,8,4);
 auto mark=epoch.frfcfs_checkpoint();
 assert(!epoch.frfcfs_requires_fallback(mark));
 ++epoch.issued; // even an issued-and-cancelled request consumes DRAM
 assert(epoch.frfcfs_requires_fallback(mark));
 mark=epoch.frfcfs_checkpoint();
 epoch.pending.push_back({0,5,1,5});
 assert(epoch.frfcfs_requires_fallback(mark));
 mark=epoch.frfcfs_checkpoint();
 epoch.pending.clear(); ++epoch.completed;
 assert(epoch.frfcfs_requires_fallback(mark));
 mark=epoch.frfcfs_checkpoint();
 assert(!epoch.frfcfs_requires_fallback(mark));
 epoch.pending.push_back({0,9,4,30}); epoch.issued++;
 epoch.note_store_publication(9,0,23);
 assert(epoch.frontier==23 && epoch.pending.empty() && epoch.cancelled_write==1);
 epoch.note_store_publication(10,0,19);
 assert(epoch.frontier==23); // publication never rewinds accepted order
 epoch.note_local_store_publication(11,0,40);
 assert(epoch.frontier==23 && epoch.store_line_until.at(11)==40);
 assert(epoch.store_line_until.count(12)==0);
 epoch.expire_store_edges(39); assert(epoch.store_line_until.count(11)==1);
 epoch.expire_store_edges(40); assert(epoch.store_line_until.count(11)==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            src=Path(d)/'test.cpp'; exe=Path(d)/'test'; src.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__ == '__main__': unittest.main()
