#include "fastsim/legacy_prepare_timing.hpp"
#include <iostream>
#include <stdexcept>

using namespace fastsim;
static void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
int main() {
    try {
        LegacyPrepareTiming state(1, 8);
        const auto first=state.store({1,0,100},10,50,110,100,4);
        check(first.role==LegacyPrepareRole::Takeover && first.response==110,
              "head must wait for its in-flight prepare");
        auto checkpoint=state;
        const auto dropped=state.store({2,0,101},11,60,111,100,4);
        check(dropped.role==LegacyPrepareRole::Dropped && dropped.response==160,
              "full prepare credit must retain a full-service head");
        state=checkpoint;
        check(state.stats().dropped==0, "copy restores candidate state and counters");
        const auto after=state.store({3,0,102},110,150,130,20,4);
        check(after.role==LegacyPrepareRole::Prepared && after.response==154,
              "completed prepare returns credit and serves a later head");
        LegacyPrepareTiming direct(1, 8);
        const auto head=direct.store({4,0,103},20,20,120,100,4);
        check(head.role==LegacyPrepareRole::Direct && head.response==120,
              "head at the eligible edge does not consume optional credit");
        direct.observe_direct(head,10,20,50,4,1);
        check(direct.stats().diagnostics.direct_scan_blocked==1 &&
              direct.stats().diagnostics.direct_scan_wait_cycles==10 &&
              direct.stats().diagnostics.direct_extra_events==1 &&
              direct.stats().diagnostics.direct_extra_cycles==70,
              "audit separates scan barrier from direct full-service effect");
        std::cout << "legacy prepare timing passed\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
