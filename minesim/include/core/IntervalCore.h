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
    void reset_stats();

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

    // Track the most recent 64-byte fetch block so we only consult the
    // L1I/ITLB once per block, matching the perf event semantics.
    Addr last_fetched_block_paddr_ = static_cast<Addr>(-1);
    
    uint64_t last_decode_cycle_ = 1;
    uint32_t decode_count_ = 0;
    
    uint64_t last_dispatch_cycle_ = 1;
    uint32_t dispatch_count_ = 0;

    // --- Backend Trackers ---
    uint64_t last_retire_cycle_ = 1;
    uint32_t retire_count_ = 0;
    uint64_t last_uop_complete_cycle_ = 1;  // execute-completion of most recent uop
    uint64_t last_branch_complete_cycle_ = 1;  // execute-completion of most recent branch uop

    // Simulates the Reorder Buffer (ROB) by storing the retire cycles of in-flight uops
    std::deque<uint64_t> rob_retire_cycles_;

    // Load and Store Queues
    struct StoreEntry {
        uint64_t dispatch_cycle;
        uint64_t complete_cycle;
        uint64_t data_ready_cycle;
        uint64_t retire_cycle;
        uint64_t free_cycle;
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
    uint64_t total_raw_branch_mispredicts_ = 0;
    uint64_t total_direct_target_proxy_misses_ = 0;
    uint64_t total_direct_target_visible_misses_ = 0;
    uint64_t direct_target_visibility_accum_ = 0;

    // Branch-type breakdown (observation-only, PMU semantic audit).
    uint64_t branch_total_cond_ = 0;
    uint64_t branch_total_return_ = 0;
    uint64_t branch_total_indirect_ = 0;
    uint64_t branch_total_direct_call_ = 0;
    uint64_t branch_total_direct_jump_ = 0;
    uint64_t branch_miss_cond_ = 0;
    uint64_t branch_miss_return_ = 0;
    uint64_t branch_miss_indirect_ = 0;
    uint64_t branch_miss_direct_target_ = 0;
    uint64_t total_icache_miss_penalties_ = 0;
    uint64_t total_dcache_miss_penalties_ = 0;
    uint64_t total_partial_stlf_count_ = 0;
    uint64_t total_partial_stlf_stall_ = 0;
    uint64_t total_sq_drain_stall_ = 0;
    uint64_t total_sq_drain_visible_cycles_ = 0;
    uint64_t total_mshr_issue_stall_ = 0;
    uint64_t total_backend_stall_visible_cycles_ = 0;
    uint64_t total_branch_memory_overlap_cycles_ = 0;
    uint64_t total_mcw_timing_cycles_ = 0;
    bool finish_applied_ = false;

    // Cycle breakdown: how many cycles dispatch was held back by each cause.
    uint64_t stall_rob_full_ = 0;
    uint64_t stall_iq_full_ = 0;
    uint64_t stall_lq_full_ = 0;
    uint64_t stall_sq_full_ = 0;
    uint64_t stall_raw_dep_ = 0;
    uint64_t stall_port_busy_ = 0;
    uint64_t stall_branch_flush_ = 0;
    uint64_t stall_serialize_ = 0;

    // MineSim Criticality Window (MCW): lightweight critical-tail attribution.
    // These counters are observation-only unless enable_mcw_timing is enabled.
    struct MCWMemoryInterval {
        uint64_t start_cycle;
        uint64_t end_cycle;
    };
    std::deque<MCWMemoryInterval> mcw_memory_intervals_;
    uint64_t mcw_critical_tail_cycle_ = 1;
    uint64_t mcw_memory_busy_until_ = 1;
    uint64_t mcw_visible_dependency_cycles_ = 0;
    uint64_t mcw_hidden_dependency_cycles_ = 0;
    uint64_t mcw_visible_load_miss_cycles_ = 0;
    uint64_t mcw_hidden_load_miss_cycles_ = 0;
    uint64_t mcw_visible_branch_recovery_cycles_ = 0;
    uint64_t mcw_hidden_branch_recovery_cycles_ = 0;
    uint64_t mcw_load_miss_intervals_ = 0;
    uint64_t mcw_branch_recovery_intervals_ = 0;

    // graph_walk memory criticality observation (observation-only).
    uint64_t mcw_dep_stall_with_memory_overlap_ = 0;
    uint64_t mcw_dep_stall_no_memory_overlap_ = 0;
    uint64_t mcw_memory_depth_sum_ = 0;
    uint64_t mcw_memory_depth_samples_ = 0;
    uint64_t mcw_window_full_evictions_ = 0;

    // Visible memory stall sub-component breakdown (observation-only).
    // Each counter tracks the visible portion of the corresponding latency
    // component, attributed proportionally to the visible fraction of each
    // load miss interval.
    uint64_t visible_mem_l1_latency_ = 0;
    uint64_t visible_mem_l2_latency_ = 0;
    uint64_t visible_mem_l3_latency_ = 0;
    uint64_t visible_mem_dram_latency_ = 0;
    uint64_t visible_mem_l2_bw_stall_ = 0;
    uint64_t visible_mem_l3_bw_stall_ = 0;
    uint64_t visible_mem_dram_bw_stall_ = 0;
    uint64_t visible_mem_mshr_stall_ = 0;
    uint64_t visible_mem_tlb_latency_ = 0;

    // Dep stall classification (observation-only).
    uint64_t dep_true_blocking_cycles_ = 0;
    uint64_t dep_apparent_cycles_ = 0;
    uint64_t dep_interval_memory_overlap_cycles_ = 0;
    uint64_t dep_interval_total_cycles_ = 0;

    // Branch recovery overlap diagnostics (observation-only).
    uint64_t br_mispredict_count_ = 0;
    uint64_t br_deque_nonempty_at_resolve_ = 0;
    uint64_t br_window_hit_count_ = 0;
    uint64_t br_window_tail_sum_ = 0;
    uint64_t br_busy_until_fallback_count_ = 0;
    uint64_t br_deque_empty_busy_cover_count_ = 0;
    uint64_t br_true_overlap_cycles_ = 0;
    uint64_t br_visible_nonzero_count_ = 0;
};

} // namespace minesim
