#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include "core/MemoryHierarchy.h"
#include "core/InstDecoder.h"
#include "core/BranchPredictor.h"
#include <deque>
#include <unordered_map>

namespace minesim {

class IntervalCore {
public:
    IntervalCore(const MicroArchConfig& config, MemoryHierarchy* mem);

    // Process a single macro-instruction
    void step(const Instruction& inst, InstDecoder* decoder);

    // Flush remaining instructions
    void finish();

    // Stats
    uint64_t get_total_cycles() const { return current_cycle_; }
    void print_stats() const;

private:
    MicroArchConfig config_;
    MemoryHierarchy* mem_;
    std::unique_ptr<BranchPredictor> branch_predictor_;

    // Global cycle tracker (representing the "current" time of the frontend)
    uint64_t current_cycle_ = 1;

    // --- Frontend Trackers ---
    uint64_t last_fetch_cycle_ = 1;
    uint32_t fetch_count_ = 0;
    
    uint64_t last_decode_cycle_ = 1;
    uint32_t decode_count_ = 0;
    
    uint64_t last_dispatch_cycle_ = 1;
    uint32_t dispatch_count_ = 0;

    // --- Backend Trackers ---
    uint64_t last_retire_cycle_ = 1;
    uint32_t retire_count_ = 0;

    // Simulates the Reorder Buffer (ROB) by storing the retire cycles of in-flight uops
    std::deque<uint64_t> rob_retire_cycles_;

    // Load and Store Queues
    struct StoreEntry {
        uint64_t dispatch_cycle;
        uint64_t complete_cycle;
        uint64_t retire_cycle;
        Addr addr;
        uint32_t size;
    };
    std::deque<StoreEntry> sq_;

    struct LoadEntry {
        uint64_t dispatch_cycle;
        uint64_t retire_cycle;
        Addr addr;
        uint32_t size;
    };
    std::deque<LoadEntry> lq_;

    // Instruction Queue / Reservation Station (RS)
    struct IQEntry {
        uint64_t dispatch_cycle;
        uint64_t issue_cycle; // The cycle it leaves the IQ/RS
    };
    std::deque<IQEntry> iq_;

    // To model issue width and Functional Unit ports, we track how many uops are issued in a given cycle.
    struct IssueTracker {
        uint32_t total = 0;
        uint32_t alu = 0;
        uint32_t load = 0;
        uint32_t store = 0;
        uint32_t branch = 0;
    };
    std::unordered_map<uint64_t, IssueTracker> issue_trackers_;

    // Periodically cleanup to prevent memory leak
    void cleanup_issue_counts(uint64_t cycle);
    void cleanup_rat(uint64_t cycle);
    uint64_t get_next_issue_cycle(uint64_t desired_cycle, InstType type);

    // Register Alias Table (RAT): Maps a register ID to the cycle it becomes ready
    std::unordered_map<uint16_t, uint64_t> rat_;

    // Statistics
    uint64_t total_macro_insts_ = 0;
    uint64_t total_uops_ = 0;
    uint64_t total_branch_mispredicts_ = 0;
    uint64_t total_icache_miss_penalties_ = 0;
    uint64_t total_dcache_miss_penalties_ = 0;
};

} // namespace minesim
