// End-to-end regression for the legacy two-stage FR-FCFS forecast. This
// fixture deliberately contains no gem5 timing input or native outcomes.
#include "fastsim/simulator.hpp"

#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace fastsim;
void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
class Records final : public TraceSource {
 public:
    explicit Records(std::vector<TraceRecord> records) : records_(std::move(records)) {}
    bool next(TraceRecord& record) override {
        if (cursor_ == records_.size()) return false;
        record = records_[cursor_++];
        return true;
    }
    std::string description() const override { return "single-pass-frfcfs-fixture"; }
 private:
    std::vector<TraceRecord> records_;
    std::size_t cursor_ = 0;
};

SimulationStats run(const char* scheduler, std::uint32_t mshrs, std::uint32_t workers,
                    std::uint32_t legacy_passes = 4) {
    SimulatorConfig c;
    c.cores = 4;
    c.core_model = "interval_weave";
    c.interval_scheduler = "time_epoch";
    c.interval_reweave_passes = 1;
    c.interval_private_preview = false;
    c.response_queue_feedback = c.response_sparse_scoreboard = true;
    c.chunk_instructions = c.interval_target_uops = 128;
    c.interval_max_cycles = 100000;
    c.domain_workers = workers;
    c.cpi_attribution = true;
    c.response_frontier_audit_stride_uops = 1;
    c.l1d.size_bytes = 4096;
    c.l2.size_bytes = 16384;
    c.llc.size_bytes = 1ull << 20;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    c.llc_mshrs = mshrs;
    c.dram.scheduler = scheduler;
    c.dram.frfcfs_selection_window = 8;
    c.dram.read_buffer_size = 8;
    c.dram.min_reads_per_switch = 8;
    c.dram.frfcfs_passes = legacy_passes;
    c.dram.frfcfs_causal_selection = true;
    c.dram.frfcfs_topology_scaled_window = false;
    c.store_timing_model = "legacy";
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    for (std::uint32_t core = 0; core < c.cores; ++core) {
        std::vector<TraceRecord> records;
        for (std::uint64_t i = 0; i < 32; ++i) {
            TraceRecord r;
            r.pc = 0x4000 + 4 * i;
            r.address = 0x100000 + core * 0x1000000ull + i * 64;
            r.size = 8;
            r.op_class = 56;
            r.n_dst = 1;
            r.flags = kRetires | kLoad | kPhysicalAddress;
            records.push_back(r);
        }
        // Dependent arithmetic must consume the final load's modeled result;
        // the audit compares the actual issue and producer completion.
        TraceRecord dependent;
        dependent.pc = 0x8000;
        dependent.flags = kRetires;
        dependent.op_class = 1;
        dependent.n_src = 1;
        dependent.producer_dists[0] = 1;
        records.push_back(dependent);
        traces.push_back(std::make_unique<Records>(std::move(records)));
    }
    return Simulator(c, std::move(traces)).run();
}

void check_population(const SimulationStats& a, const SimulationStats& b) {
    const auto x = a.total_core(), y = b.total_core();
    check(x.retired_uops == y.retired_uops && x.retired_instructions == y.retired_instructions &&
          x.memory_accesses == y.memory_accesses && x.l1d.accesses == y.l1d.accesses &&
          x.l1d.misses == y.l1d.misses && x.l2.accesses == y.l2.accesses &&
          x.l2.misses == y.l2.misses && a.llc.accesses == b.llc.accesses &&
          a.llc.misses == b.llc.misses,
          "single-pass FR-FCFS changed functional/cache request population");
}

void check_dependency(const SimulationStats& result) {
    for (std::uint32_t core = 0; core < 4; ++core) {
        const ResponseFrontierAuditSample* load = nullptr;
        const ResponseFrontierAuditSample* dependent = nullptr;
        for (const auto& s : result.response_frontier_audit.at(core)) {
            if (s.sequence == 31) load = &s;
            if (s.sequence == 32) dependent = &s;
        }
        check(load && dependent && dependent->actual_issue_cycle >= load->actual_completion_cycle,
              "response feedback did not propagate to the dependent UOP");
    }
}
} // namespace

int main() {
    const auto fcfs = run("fcfs", 128, 1);
    const auto forecast = run("frfcfs", 128, 1);
    const auto parallel = run("frfcfs", 128, 4);
    const auto compatibility_one = run("frfcfs", 128, 1, 1);
    const auto compatibility_max = run("frfcfs", 128, 1, 32);
    check(forecast.dram_frfcfs_candidate_epochs > 0 && forecast.dram_frfcfs_stable_epochs > 0,
          "fixture must exercise at least one accepted FR-FCFS forecast");
    check(forecast.dram_frfcfs_single_pass_epochs == forecast.dram_frfcfs_stable_epochs,
          "accepted forecasts must be distinguished from convergence");
    check(forecast.dram_frfcfs_passes <= forecast.dram_frfcfs_candidate_epochs,
          "legacy FR-FCFS scheduled a candidate more than once");
    check(forecast.dram_frfcfs_stable_epochs + forecast.dram_frfcfs_fallback_epochs ==
          forecast.dram_frfcfs_candidate_epochs,
          "FR-FCFS candidates were neither accepted nor explicitly rejected");
    check(forecast.dram_frfcfs_fallback_epochs ==
          forecast.dram_frfcfs_fallback_credit_epochs +
          forecast.dram_frfcfs_fallback_visibility_epochs +
          forecast.dram_frfcfs_fallback_nonreplayable_epochs,
          "FR-FCFS fallback reason population is not conserved");
    check(forecast.dram_frfcfs_max_admitted_pending <= 8,
          "forecast exceeded configured DRAM read queue capacity");
    check_population(fcfs, forecast);
    check_population(forecast, parallel);
    check(forecast.total_core().cycles == parallel.total_core().cycles &&
          forecast.dram_frfcfs_reordered_requests == parallel.dram_frfcfs_reordered_requests,
          "host worker count changed the model result");
    check_population(forecast, compatibility_one);
    check_population(forecast, compatibility_max);
    check(forecast.total_core().cycles == compatibility_one.total_core().cycles &&
          forecast.total_core().cycles == compatibility_max.total_core().cycles &&
          forecast.dram_frfcfs_passes == compatibility_one.dram_frfcfs_passes &&
          forecast.dram_frfcfs_passes == compatibility_max.dram_frfcfs_passes &&
          forecast.dram_frfcfs_reordered_requests == compatibility_one.dram_frfcfs_reordered_requests &&
          forecast.dram_frfcfs_reordered_requests == compatibility_max.dram_frfcfs_reordered_requests,
          "compatibility passes key changed the single-pass controller result");
    check_dependency(forecast);

    const auto tight_fcfs = run("fcfs", 1, 1);
    const auto tight = run("frfcfs", 1, 1);
    check(tight.dram_frfcfs_candidate_epochs > 0 && tight.dram_frfcfs_fallback_epochs > 0,
          "binding real LLC MSHR must exercise explicit fallback");
    check(tight.dram_frfcfs_fallback_credit_epochs > 0,
          "the tight configured LLC MSHR must cause a credit fallback");
    check(tight.dram_frfcfs_passes <= tight.dram_frfcfs_candidate_epochs,
          "a rejected tight-credit forecast was rescheduled");
    check_population(tight_fcfs, tight);
    if (tight.dram_frfcfs_fallback_epochs == tight.dram_frfcfs_candidate_epochs)
        check(tight.total_core().cycles == tight_fcfs.total_core().cycles,
              "rejected forecast did not preserve canonical feedback");
}
