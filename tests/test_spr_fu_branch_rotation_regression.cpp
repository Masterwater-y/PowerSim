// Standalone regression against IntervalCoreModel's actual shared-port path.
// Expected queue semantics apply to generic IntAlu/Jmprel using getUnit;
// SPR managed integer families use a different ExecutionPorts arbitration.
// The direct state setup describes an earlier reservation on port 6 at t=0;
// it avoids fabricating a target-time trace or depending on frontend timing.
#include <bits/stdc++.h>
#define private public
#include "fastsim/interval_core.hpp"
#undef private

int main() {
    using namespace fastsim;
    SimulatorConfig config;
    config.spr_shared_fu = true;
    config.spr_shared_fu_round_robin = true;
    config.spr_shared_fu_gap_aware = true;
    config.issue_width = 8;
    IntervalCoreModel core(config);
    const TargetOpTraits op{IntervalFuPool::kInteger,1,true};
    TraceRecord alu;
    alu.pc=0x1000;alu.flags=kRetires;alu.op_class=1;
    // A previously modeled operation occupies p6 in this cycle. The next
    // ordinary IntAlu selects p0, so the capability queue's next item is p1.
    core.spr_port_calendar_[6].reserve(0,1);
    assert(core.allocate_issue(0,op,alu,0)==0);
    TraceRecord branch=alu;branch.flags|=kBranch;
    assert(core.allocate_issue(0,op,branch,0)==1);
    // For generic IntAlu, gem5 getUnit examines the complete
    // {0,1,5,6,11} capability queue even
    // for a branch: at t=0 its failed scan starts at p1 and leaves p5 next;
    // at t=1 it skips non-branch p5 and issues the branch on p6.
    // A p0-only operation must consequently be able to issue at t=1.
    TraceRecord restricted=alu;restricted.op_class=9;
    const auto issued=core.allocate_issue(1,op,restricted,0);
    if(issued!=1) {
        std::fprintf(stderr,"branch queue mismatch: p0-only issue=%llu, expected 1\n",
                     static_cast<unsigned long long>(issued));
        return 1;
    }
}
