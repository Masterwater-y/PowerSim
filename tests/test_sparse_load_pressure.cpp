#include <iostream>
#if __has_include("fastsim/sparse_load_pressure.hpp")
#include "fastsim/sparse_load_pressure.hpp"
#include <limits>
#ifdef FASTSIM_SPARSE_INTEGRATION
#include "fastsim/simulator.hpp"
#include <memory>
#include <vector>
class Records final : public fastsim::TraceSource {
    std::vector<fastsim::TraceRecord> records_;
    std::size_t cursor_ = 0;
  public:
    explicit Records(std::vector<fastsim::TraceRecord> r) : records_(std::move(r)) {}
    bool next(fastsim::TraceRecord& r) override {
        if (cursor_ == records_.size()) return false;
        r = records_[cursor_++]; return true;
    }
    std::string description() const override { return "sparse-load-test"; }
    std::optional<bool> current_ordinary_load_eligible() const override { return true; }
};
static fastsim::SimulatorConfig config(bool enabled, unsigned chunk) {
    using namespace fastsim;
    SimulatorConfig c;
    c.core_model = "interval_weave"; c.interval_scheduler = "time_epoch";
    c.chunk_instructions = c.interval_target_uops = chunk;
    c.interval_max_cycles = 64; c.lookahead_chunks = 2;
    c.spr_shared_fu = c.spr_shared_fu_gap_aware = true;
    c.response_queue_feedback = c.response_sparse_scoreboard = true;
    c.response_memory_descriptor = c.response_block_summary = true;
    c.response_monotone_iq_calendar = c.response_activity_certificate = true;
    c.response_materialized_uop_fast_kernel = true;
    c.ruby_sequencer_max_outstanding = 2;
    c.cache_load_ports = 1; c.cache_store_ports = 2;
    c.sparse_load_pressure = enabled;
    return c;
}
static fastsim::SimulationStats simulate(bool enabled, unsigned chunk) {
    using namespace fastsim;
    auto c = config(enabled, chunk); c.validate();
    std::vector<TraceRecord> records;
    for (unsigned i=0;i<480;++i) {
        TraceRecord r; r.pc=0x1000+i*4; r.flags=kRetires|kLoad|kPhysicalAddress;
        r.op_class=56; r.address=0x10000+(i%20)*64; r.size=8;
        if (i%23==0) r.size=80; // explicit multifragment exclusion
        records.push_back(r);
    }
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<Records>(records));
    return Simulator(c,std::move(traces)).run();
}
#endif
#include <stdexcept>
static void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
int main() {
    fastsim::SparseLoadPressure p;
    check(p.constrain(100, 100, 3).cycle == 100, "first grant");
    check(p.constrain(100, 100, 3).cycle == 100, "second grant");
    check(p.constrain(100, 100, 3).cycle == 100, "third grant");
    check(p.constrain(100, 100, 3).cycle == 101, "fourth grant requires capacity");
    auto copy = p;
    check(copy.constrain(100, 107, 3).cycle == 107, "existing wait absorbs bound");
    check(p.constrain(100, 100, 3).cycle == 101, "copy isolates source state");
    check(p.constrain(200, 200, 3).new_cohort, "new callback resets cohort");
    check(p.constrain(200, 200, 3).cycle == 200, "no inherited debt");
    check(p.constrain(10, 10, 3).new_cohort, "rebased callback starts new cohort");
    bool rejected = false;
    try { p.constrain(10, 10, 0); } catch (const std::invalid_argument&) { rejected = true; }
    check(rejected, "zero ports rejected");
    rejected = false;
    try { p.constrain(20, 19, 3); } catch (const std::invalid_argument&) { rejected = true; }
    check(rejected, "admission before release rejected");
    fastsim::SparseLoadPressure tail;
    const auto end = std::numeric_limits<std::uint64_t>::max();
    check(tail.constrain(end, end, 1).cycle == end, "large target time constant work");
    rejected = false;
    try { tail.constrain(end, end, 1); } catch (const std::overflow_error&) { rejected = true; }
    check(rejected, "cycle overflow rejected");
#ifdef FASTSIM_SPARSE_INTEGRATION
    for (unsigned mode=0; mode<3; ++mode) {
        auto c = config(true, 7);
        if (mode==0) c.response_shared_service_constraints = true;
        if (mode==1) c.dtlb.hierarchy_walk = true;
        if (mode==2) c.software_prefetch_early_ack = true;
        bool refused = false;
        try { c.validate(); } catch (const std::invalid_argument& e) {
            refused = std::string(e.what()).find("sparse_load_pressure") != std::string::npos;
        }
        check(refused, "unsupported mode must explicitly reject sparse pressure");
    }
    const auto off = simulate(false,7), on = simulate(true,7);
    check(off.total_core().retired_uops == on.total_core().retired_uops, "functional population preserved");
    check(off.total_core().l1d.accesses == on.total_core().l1d.accesses, "cache transactions preserved");
    check(off.total_o3().sparse_load_pressure_events == 0, "OFF path inert");
    check(on.total_o3().sparse_load_pressure_events > 0, "blocked load cohorts exercised");
    check(on.total_o3().sparse_load_pressure_repairs > 0 &&
          on.total_o3().sparse_load_pressure_cycles > 0, "positive integration repair exercised");
    const auto across = simulate(true,31);
    check(across.total_o3().sparse_load_pressure_events > 0, "different chunks carry cohorts");
    check(across.total_core().retired_uops == on.total_core().retired_uops, "chunk population conserved");
    std::cout << "cohort events=" << on.total_o3().sparse_load_pressure_events
              << " repairs=" << on.total_o3().sparse_load_pressure_repairs << "\n";
#endif
    std::cout << "sparse load pressure checks passed\n";
}
#else
int main() { std::cerr << "FAIL: sparse load pressure mechanism is absent\n"; return 1; }
#endif
