#pragma once

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

}  // namespace fastsim
