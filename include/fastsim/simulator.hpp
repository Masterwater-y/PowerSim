#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

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

DramScheduleProbeResult run_dram_schedule_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramScheduleRequest>& requests,
    std::uint32_t selection_window);

DramControllerProbeResult run_dram_controller_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramControllerProbeEvent>& events);

}  // namespace testing
#endif

}  // namespace fastsim
