#include "test_spr_store_group_runtime.cpp"

static fastsim::SimulationStats run_legacy_prepare(std::uint32_t credits,
        std::uint32_t chunk=64, std::uint32_t quantum=4096, bool fast=false) {
    auto config=group_config(false,chunk,32);
    config.interval_max_cycles=quantum;
    config.response_materialized_uop_fast_kernel=fast;
    if (fast) {
        config.cpi_attribution=false;
        config.response_frontier_audit_stride_uops=0;
        config.response_block_summary=true;
        config.response_memory_descriptor=true;
        config.response_monotone_iq_calendar=true;
    }
    config.legacy_prepare_timing=true;
    config.legacy_prepare_credits=credits;
    config.validate();
    std::vector<Step> steps;
    for(unsigned i=0;i<12;++i) steps.push_back({Kind::kStore,0x40000+64*i});
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(steps)));
    fastsim::Simulator simulator(config,std::move(traces));
    return simulator.run();
}

int main() {
    try {
        auto automatic=group_config(false,64,32);
        check(automatic.legacy_prepare_timing_enabled(),
              "compatible legacy buffer TSO enables prepare timing by default");
        automatic.legacy_prepare_timing=false;
        check(!automatic.legacy_prepare_timing_enabled(), "explicit false retains the old path");
        automatic.legacy_prepare_timing.reset();
        automatic.tso_store=fastsim::TsoStoreMode::kSerial;
        check(!automatic.legacy_prepare_timing_enabled(), "other TSO modes keep their own path");
        const auto one=run_legacy_prepare(1);
        const auto many=run_legacy_prepare(16);
        check(one.total_core().retired_uops==12 && one.total_core().perf_retired_stores==12,
              "all mandatory stores must retire when prepare credit is full");
        check(one.total_core().legacy_prepare_accepted>0 && one.total_core().legacy_prepare_dropped>0,
              "one prepare credit must accept and drop hints");
        check(many.total_core().legacy_prepare_dropped<one.total_core().legacy_prepare_dropped,
              "additional prepare credits reduce dropped hints");
        check(one.total_core().legacy_prepare_direct+one.total_core().legacy_prepare_accepted+
              one.total_core().legacy_prepare_dropped<=12,
              "timing roles cannot exceed the retired store population");
        const auto across=run_legacy_prepare(1,1,16);
        check(across.total_core().retired_uops==12 && across.total_core().legacy_prepare_dropped>0,
              "in-flight prepare credit must survive across short batches");
        const auto fast=run_legacy_prepare(1,64,4096,true);
        check(fast.total_core().retired_uops==12 && fast.total_core().legacy_prepare_dropped>0,
              "fast kernel must execute the timing approximation");
        std::cout << "legacy prepare runtime passed\n";
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
