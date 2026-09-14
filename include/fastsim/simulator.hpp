#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

// A runtime control window.  Simulated-time windows use target time, never
// host wall time.  Instruction windows count retired macro instructions over
// all active cores and stop at the first committed time-epoch boundary whose
// total reaches the requested budget; `instruction_overshoot` reports the
// deterministic boundary granularity.
enum class SimulationWindowKind {
    kSimulatedTime,
    kRetiredInstructions,
};

struct SimulationWindow {
    SimulationWindowKind kind = SimulationWindowKind::kSimulatedTime;
    std::uint64_t value = 0;

    static SimulationWindow simulated_time_ns(std::uint64_t nanoseconds) {
        return SimulationWindow{
            SimulationWindowKind::kSimulatedTime, nanoseconds};
    }

    static SimulationWindow retired_instructions(
        std::uint64_t instructions) {
        return SimulationWindow{
            SimulationWindowKind::kRetiredInstructions, instructions};
    }
};

// Window PMU fields are deliberately limited to additive, externally useful
// counters.  Internal maxima and host-throughput diagnostics remain in the
// cumulative SimulationStats report because subtracting them at an arbitrary
// boundary would not produce a meaningful window value.
struct CoreWindowStats {
    std::uint32_t core = 0;
    std::uint64_t frequency_hz = 0;
    std::uint64_t cycles = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t memory_uops = 0;
    std::uint64_t memory_accesses = 0;
    std::uint64_t retired_branches = 0;
    std::uint64_t retired_branch_misses = 0;
    std::uint64_t dtlb_accesses = 0;
    std::uint64_t dtlb_misses = 0;
    CacheCounters l1d;
    CacheCounters l2;
    double cpi = 0.0;
    double uop_cpi = 0.0;
    bool cpi_available = false;
    bool uop_cpi_available = false;
};

struct SharedWindowStats {
    CacheCounters llc;
    std::uint64_t cha_requests = 0;
    std::uint64_t cha_reads = 0;
    std::uint64_t cha_writes = 0;
    std::uint64_t llc_hits = 0;
    std::uint64_t llc_misses = 0;
    std::uint64_t permission_upgrades = 0;
    std::uint64_t invalidations = 0;
    std::uint64_t remote_supplies = 0;
    std::uint64_t llc_unique_fills = 0;
    std::uint64_t llc_merged_misses = 0;
    std::uint64_t llc_merged_wait_cycles = 0;
    std::uint64_t dram_reads = 0;
    std::uint64_t dram_writes = 0;
    std::uint64_t queue_cycles = 0;
};

struct SimulationWindowResult {
    std::uint64_t window_id = 0;
    std::uint64_t start_time_fs = 0;
    std::uint64_t end_time_fs = 0;
    std::uint64_t requested_value = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t instruction_overshoot = 0;
    bool finished = false;
    std::vector<CoreWindowStats> cores;
    SharedWindowStats shared;
};

// A functional instruction stream is a software-thread property; the timing
// model, predictor, TLBs, and private caches are hardware-core properties.
// This binding makes that ownership explicit while the first scheduling stage
// remains deliberately static (one thread per core, no migration or time
// slicing).  `address_space_id` is carried now so later TLB/scheduler work does
// not have to change the input API.
struct ThreadTraceBinding {
    std::uint32_t thread_id = 0;
    std::uint32_t initial_core = 0;
    std::uint64_t address_space_id = 0;
    std::unique_ptr<TraceSource> trace;
};

class Simulator {
  public:
    Simulator(SimulatorConfig config,
              std::vector<std::unique_ptr<TraceSource>> traces);
    Simulator(SimulatorConfig config,
              std::vector<ThreadTraceBinding> threads);
    ~Simulator();
    Simulator(const Simulator&) = delete;
    Simulator& operator=(const Simulator&) = delete;

    SimulationStats run();
    SimulationWindowResult advance(const SimulationWindow& window);
    // Frequencies are applied atomically at the current paused simulated-time
    // boundary and affect the next advance().  A zero frequency is rejected;
    // clock gating requires an explicit runnable/idle model and is outside the
    // current static-thread scheduler.
    void set_core_frequencies(
        const std::vector<std::uint64_t>& frequencies_hz);
    bool finished() const;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

std::vector<std::unique_ptr<TraceSource>> make_synthetic_traces(
    std::uint32_t cores, std::uint64_t instructions_per_core,
    std::uint32_t memory_percent = 30,
    std::uint32_t shared_percent = 5,
    std::uint64_t working_set_lines = 1ull << 18,
    std::uint64_t seed = 1);

#ifdef FASTSIM_ENABLE_TEST_HOOKS
namespace testing {

struct DramScheduleRequest {
    std::uint64_t arrival = 0;
    std::uint64_t line = 0;
    std::uint64_t ordinal = 0;
};

struct DramScheduleProbeResult {
    std::vector<std::uint64_t> completions;
    std::vector<std::uint64_t> command_cycles;
    std::vector<std::uint8_t> row_hits;
    std::vector<std::size_t> service_order;
    std::uint64_t max_selection_candidates = 0;
    std::uint64_t max_admitted_pending = 0;
    std::uint64_t page_policy_scanned_requests = 0;
    std::uint64_t outside_window_row_hits = 0;
    std::uint64_t outside_window_bank_conflicts = 0;
    std::uint64_t row_cap_precharges = 0;
    std::uint64_t adaptive_precharges = 0;
    std::uint64_t writes_drained = 0;
    std::uint64_t high_watermark_switches = 0;
    std::uint64_t turnarounds = 0;
    std::uint64_t pending_writes_final = 0;
};

struct DramControllerProbeEvent {
    std::uint64_t arrival = 0;
    std::uint64_t line = 0;
    bool write = false;
};

struct DramControllerProbeResult {
    std::vector<std::uint64_t> completions;
    std::uint64_t write_enqueues = 0;
    std::uint64_t writes_drained = 0;
    std::uint64_t read_bypasses = 0;
    std::uint64_t high_watermark_switches = 0;
    std::uint64_t forced_capacity_drains = 0;
    std::uint64_t turnarounds = 0;
    std::uint64_t write_row_hits = 0;
    std::uint64_t write_row_misses = 0;
    std::uint64_t max_pending = 0;
    std::uint64_t pending_final = 0;
};

struct ResidentBufferProbeResult {
    bool initial_mapping_conserved = false;
    bool rebased_mapping_conserved = false;
    bool descriptor_indices_unchanged = false;
    bool ring_wrap_conserved = false;
    std::uint64_t released_chunks = 0;
};

struct SharedPendingFillProbeResult {
    std::uint64_t generation = 0;
    std::optional<std::uint64_t> pending_ready, selected_ready, restored_ready;
    bool numeric_access_rejected = false;
    bool numeric_replay_pending = false;
    bool stale_publication = false;
    bool duplicate_publication = false;
    bool conflicting_publication_rejected = false;
    std::uint64_t selected_merge_response = 0;
    std::uint64_t requests_after_rejected_access = 0;
    std::uint64_t requests_after_restore = 0;
};

struct SharedMixedServiceProbeResult {
    bool pending_before_selection = false;
    bool selected_beyond_frontier = false;
    bool invisible_before_fill = false;
    bool visible_at_fill = false;
    bool response_and_fill_distinct = false;
    bool mshr_blocks_without_fake_release = false;
    bool rollback_conserved = false;
    bool partition_invariant = false;
    bool late_submission_rejected = false;
    bool bounded_submission = false;
    bool instruction_queue_pmu_isolated = false;
    std::uint64_t actual_dirty_writebacks = 0;
    std::uint64_t serviced_dirty_writebacks = 0;
};

DramScheduleProbeResult run_dram_schedule_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramScheduleRequest>& requests,
    std::uint32_t selection_window,
    const std::vector<std::uint64_t>& buffered_write_lines = {},
    bool parallel_channels = false);

DramControllerProbeResult run_dram_controller_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramControllerProbeEvent>& events);

ResidentBufferProbeResult run_resident_buffer_probe();
SharedPendingFillProbeResult run_shared_pending_fill_probe();
SharedMixedServiceProbeResult run_shared_mixed_service_probe();

}  // namespace testing
#endif

}  // namespace fastsim
