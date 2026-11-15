// Functional-only Simulator integration fixture for independent prepare credits.
// The SFINAE adapter allows this test to expose the old behavior before the
// proposed opt-in configuration field exists.
#include "test_spr_store_group_runtime.cpp"

namespace {
template <typename T>
auto enable_prepare_credit(T& c, int)
    -> decltype(c.ruby_store_prepare_credit = true, void()) {
    c.ruby_store_prepare_credit = true;
}
template <typename T> void enable_prepare_credit(T&, long) {}

template <typename T>
auto prepare_accepted(const T& s, int) -> decltype(s.spr_prepare_credit_accepted) {
    return s.spr_prepare_credit_accepted;
}
template <typename T> std::uint64_t prepare_accepted(const T&, long) { return 0; }
template <typename T>
auto prepare_dropped(const T& s, int) -> decltype(s.spr_prepare_credit_dropped) {
    return s.spr_prepare_credit_dropped;
}
template <typename T> std::uint64_t prepare_dropped(const T&, long) { return 0; }
template <typename T>
auto head_demands(const T& s, int) -> decltype(s.spr_prepare_credit_head_demands) {
    return s.spr_prepare_credit_head_demands;
}
template <typename T> std::uint64_t head_demands(const T&, long) { return 0; }

void saturated_prepare_preserves_head_demand() {
    std::vector<Step> steps;
    for (unsigned i = 0; i < 17; ++i)
        steps.push_back({Kind::kStore, 0x40000 + 64 * i});
    auto config = group_config(false, 128, 32);
    config.interval_max_cycles = 100000;
    // Keep head sends beyond the hint issue window. The test implementation
    // must still verify its modeled hint population and callback timing;
    // this parameter alone is not proof that all hints overlap.
    config.store_commit_to_iew_delay = 1000;
    config.llc_fill_response_latency = 1000;
    config.ruby_sequencer_max_outstanding = 16;
    enable_prepare_credit(config, 0);
    config.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(steps)));
    Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    check(stats.total_core().retired_uops == 17 &&
          stats.total_core().perf_retired_stores == 17,
          "all 17 stores must retire exactly once");
    check(prepare_accepted(stats, 0) == 16 && prepare_dropped(stats, 0) == 1,
          "17 simultaneously outstanding hints must accept 16 and drop one; accepted=" +
              std::to_string(prepare_accepted(stats, 0)) + " dropped=" +
              std::to_string(prepare_dropped(stats, 0)));
    check(head_demands(stats, 0) == 17 && stats.total_sequencer().requests == 17,
          "all stores, including the dropped hint, must issue one real head demand");
}
}

int main() {
    try { saturated_prepare_preserves_head_demand(); }
    catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
    std::cout << "SPR prepare credit runtime tests passed\n";
}
