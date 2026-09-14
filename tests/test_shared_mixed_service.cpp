#include "fastsim/simulator.hpp"
#include <stdexcept>

void test_shared_mixed_service() {
    const auto p = fastsim::testing::run_shared_mixed_service_probe();
    const auto require = [](bool ok, const char* reason) {
        if (!ok) throw std::runtime_error(reason);
    };
    require(p.pending_before_selection,
            "shared demand fabricated a response before controller selection");
    require(p.bounded_submission, "shared pending services exceeded upstream L2 MSHR owners");
    require(p.instruction_queue_pmu_isolated, "instruction service leaked queue PMU into data scope");
    require(p.selected_beyond_frontier && p.invisible_before_fill && p.visible_at_fill,
            "selected future response leaked cache visibility across frontier");
    require(p.response_and_fill_distinct,
            "shared fill and external response lost their separate boundaries");
    require(p.mshr_blocks_without_fake_release,
            "pending MSHR owner did not retain a capacity-blocked demand");
    require(p.actual_dirty_writebacks != 0 &&
                p.serviced_dirty_writebacks == p.actual_dirty_writebacks,
            "real LLC dirty evictions did not enter and leave the mixed controller");
    require(p.rollback_conserved, "shared RD/WB transaction leaked state or PMU");
    require(p.partition_invariant, "shared service results depended on frontier partitions");
    require(p.late_submission_rejected, "shared service accepted arrival behind its frontier");
}

#ifdef FASTSIM_SHARED_MIXED_SERVICE_STANDALONE
#include <iostream>
int main() {
    try {
        test_shared_mixed_service();
        std::cout << "shared mixed service tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
#endif
