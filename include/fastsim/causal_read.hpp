#pragma once

#include <cstdint>
#include <functional>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

// Experimental event solver. The service callback is
// invoked once per unique LLC miss, at the actual controller admission time.
// It must be causal; data_ready releases the controller slot, while response
// includes the controller's configured return pipeline. No reference labels
// or precomputed instruction issue/completion times enter this interface.
struct CausalReadDramResponse {
    std::uint64_t command = 0;
    std::uint64_t data_ready = 0;
    std::uint64_t response = 0;
    std::uint64_t queue_cycles = 0;
};
using CausalReadDramService = std::function<CausalReadDramResponse(
    std::uint64_t arrival, std::uint64_t line,
    std::uint64_t sequence, std::uint32_t fragment)>;
using CausalMulticoreDramService = std::function<CausalReadDramResponse(
    std::uint64_t arrival, std::uint64_t line, std::uint32_t core,
    std::uint64_t sequence, std::uint32_t fragment)>;
// Full-line dirty LLC evictions enter the controller write path. Architectural
// stores complete at the cache response, independently of this later eviction.
using CausalDramWriteService = std::function<void(
    std::uint64_t arrival, std::uint64_t line)>;

struct CausalReadEvent {
    // ready: operands available; execute: memory FU/address stage;
    // data: one load fragment delivered; completion_ready: all data/FU work
    // complete and eligible for WB; writeback: actual wakeup and IQ release.
    // miss/fill delimit an allocated data-or-permission generation/MSHR;
    // permission_request marks resident-data upgrades among those pairs.
    // cache_response occurs when a cache sends its hit/fill reply, before
    // the LLC network leg. Upper fills happen only at response arrival.
    // coherence_acquire/grant/release describe conservative ordering leases;
    // grant has no network cost. directory_request is the actual shared
    // miss/upgrade arrival; permission_exclusive/shared describe fill state.
    const char* kind = "";
    std::uint64_t cycle = 0;
    // Zero-based per-core functional ordinal, continuous across the warmup
    // boundary and independent of host chunks. Identity is (core, sequence).
    std::uint64_t sequence = 0;
    std::uint32_t fragment = 0;
    std::uint64_t line = 0;
    // -1: core (including local MMIO escape completion), 0/1/2: L1D/L2/LLC,
    // 3: DRAM. Escape completions never represent a device/cache response.
    int level = -1;
    std::uint64_t generation = 0;
    std::uint64_t leader = 0;
    std::uint32_t core = 0;
    std::uint32_t leader_core = 0;
    bool measured = true;
};
using CausalReadObserver = std::function<void(const CausalReadEvent&)>;

void validate_causal_read_config(const SimulatorConfig& config);

// stats must be fresh, with its per-core/per-CHA vectors initialized by Simulator.
// The source is streamed with bounded functional lookahead. Unsupported
// records fail the entire run; partial results must never be published as CPI.
// No old interval scheduler, private preview, shared replay or feedback is run.
void run_causal_read(const SimulatorConfig& config, TraceSource& source,
                     SimulationStats& stats,
                     const CausalReadDramService& dram_service,
                     const CausalReadObserver& observer = {},
                     const CausalDramWriteService& dram_write = {});

void run_causal_multicore(const SimulatorConfig& config,
                         const std::vector<TraceSource*>& sources,
                         SimulationStats& stats,
                         const CausalMulticoreDramService& dram_service,
                         const CausalReadObserver& observer = {},
                         const CausalDramWriteService& dram_write = {});

}  // namespace fastsim
