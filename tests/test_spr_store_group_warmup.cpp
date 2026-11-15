// Pending integration fixture: compile as a separate executable (this file
// intentionally reuses the ordinary runtime fixture in one translation unit).
// The warmup-only stats fields below are proposed API; they certify that the
// actual pre-boundary candidate committed or took the future-watermark retry.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats warm_run(std::vector<Step> steps, bool enabled,
                         unsigned warm_count, unsigned budget, unsigned quantum) {
    auto c = group_config(enabled, 16, budget);
    c.interval_max_cycles = quantum;
    c.functional_warmup_interval_max_cycles = quantum;
    c.validate();
    const auto measured = steps.size() - warm_count;
    auto raw = std::make_unique<FunctionalStoreSource>(std::move(steps));
    auto wrapped = std::make_unique<WarmupInstructionTraceSource>(
        std::move(raw), warm_count, measured, warm_count, measured, true);
    std::vector<std::unique_ptr<TraceSource>> sources;
    sources.push_back(std::move(wrapped));
    Simulator simulator(c, std::move(sources));
    return simulator.run();
}

void warm_group_boundary(bool late) {
    const std::vector<Step> steps{{Kind::kStore,0x40000},
        {Kind::kStore,0x40008}, {Kind::kLoad,0x40000}};
    const auto quantum = late ? 32u : 4096u;
    const auto control = warm_run(steps,false,2,16,quantum);
    const auto candidate = warm_run(steps,true,2,16,quantum);
    check(candidate.functional_warmup_enabled &&
          candidate.functional_warmup_instructions == 2 &&
          candidate.total_core().retired_instructions == 1,
          "warmup and ROI instruction populations must stay distinct");
    check(candidate.functional_warmup_spr_store_group_candidates == 1,
          "warmup fixture must form an actual store pair");
    if (late) {
        check(candidate.functional_warmup_spr_store_group_future_fallbacks == 1 &&
              candidate.functional_warmup_spr_store_group_committed == 0,
              "a group callback beyond the boundary must retry the ungrouped path");
        check(candidate.total_core().l1d.accesses == control.total_core().l1d.accesses &&
              candidate.total_core().perf_l1d_load_fb_hits == control.total_core().perf_l1d_load_fb_hits &&
              candidate.elapsed_reference_q16 == control.elapsed_reference_q16,
              "rejected warmup pair must preserve ROI timing and PMU populations");
    } else {
        check(candidate.functional_warmup_spr_store_group_committed == 1 &&
              candidate.functional_warmup_spr_store_group_future_fallbacks == 0,
              "group callback before warmup horizon should remain usable");
    }
}

void warm_credit_boundary() {
    const std::vector<Step> steps{{Kind::kUnknownStore,0x40000},
                                  {Kind::kStore,0x40100}};
    const auto result = warm_run(steps,true,1,1,32);
    check(result.functional_warmup_store_credits_live == 1 &&
          result.functional_warmup_store_credit_next_release >
              result.functional_warmup_barrier_cycles,
          "fixture must carry an occupied budget past the measurement boundary");
    check(frame(result,1).actual_dispatch_cycle >=
              result.functional_warmup_store_credit_next_release,
          "the ROI store must respect the warmup store's carried credit");
    check(result.total_core().retired_instructions == 1 &&
          result.total_core().perf_retired_stores == 1,
          "warmup store must not be recounted in measured retirement PMU");
}
}

int main() {
    try { warm_group_boundary(false); warm_group_boundary(true); warm_credit_boundary(); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR store group warmup boundary tests passed\n";
    return 0;
}
