#pragma once

#include "core/Cache.h"
#include "core/TLB.h"
#include "core/PageTable.h"
#include <memory>
#include <deque>
#include <vector>

namespace minesim {

// Sub-component breakdown of a memory access latency.  Each field is the
// number of cycles contributed by that level or mechanism.  The sum of all
// fields equals the total access latency (excluding TLB translation).
struct LatencyBreakdown {
    uint32_t l1_latency = 0;       // l1d_->get_latency()
    uint32_t l2_latency = 0;       // l2_->get_latency()  (0 if L1 hit)
    uint32_t l3_latency = 0;       // l3_->get_latency()  (0 if L2 hit)
    uint32_t dram_latency = 0;     // main_memory_latency_ (0 if L3 hit)
    uint32_t l2_bw_stall = 0;
    uint32_t l3_bw_stall = 0;
    uint32_t dram_bw_stall = 0;
    uint32_t mshr_stall = 0;       // filled by read_data_mshr when MSHR full
    uint32_t tlb_latency = 0;      // TLB translation (hits + page walk)
};

// Returned by read_data_mshr() so the caller (IntervalCore) can both update
// its issue cycle (when the MSHR/LFB pool is exhausted) and bill the
// resulting access latency.
struct LoadAccessResult {
    uint32_t latency = 0;          // total memory-access latency for this load
    uint64_t blocked_until = 0;    // if > issue_cycle, MSHR is full -> stall issue
    LatencyBreakdown breakdown;    // sub-component attribution
};

class MemoryHierarchy {
public:
    MemoryHierarchy(const MicroArchConfig& config);

    // Interface for Instruction Fetch (ITLB -> STLB -> PT -> L1I -> L2 -> L3)
    // Returns the total latency in cycles for the access
    uint32_t fetch_instruction(Addr vaddr, uint64_t current_cycle);

    // Interface for Data Read (DTLB -> STLB -> PT -> L1D -> L2 -> L3)
    // Returns the total latency in cycles for the access. Handles
    // cache-line and page-crossing splits internally and accumulates the
    // appropriate access/miss counters.
    uint32_t read_data(Addr vaddr, uint32_t size, uint64_t current_cycle);

    // Same as read_data() but routes through the MSHR/LFB pool. If the LFB
    // is full, the result's `blocked_until` is set to the cycle when the
    // oldest entry frees, so the caller can stall the load issue.
    LoadAccessResult read_data_mshr(Addr vaddr, uint32_t size, uint64_t current_cycle);

    // Interface for Data Write (DTLB -> STLB -> PT -> L1D -> L2 -> L3)
    uint32_t write_data(Addr vaddr, uint32_t size, uint64_t current_cycle);

    // Print statistics for all caches and TLBs in the hierarchy
    void reset_stats();
    void print_stats() const;

    // Getters for raw backend counters (used by IntervalCore for
    // visible-vs-hidden projection).
    uint64_t get_total_l2_bw_stall() const { return total_l2_bw_stall_; }
    uint64_t get_total_l3_bw_stall() const { return total_l3_bw_stall_; }
    uint64_t get_total_dram_bw_stall() const { return total_dram_bw_stall_; }
    uint64_t get_total_next_line_prefetches() const { return total_next_line_prefetches_; }
    uint64_t get_total_prefetch_mshr_reserved() const { return total_prefetch_mshr_reserved_; }
    uint64_t get_total_prefetch_mshr_dropped() const { return total_prefetch_mshr_dropped_; }
    uint64_t get_total_mshr_stall_cycles() const { return total_mshr_stall_cycles_; }
    uint64_t get_l2_prefetch_installs() const { return l2_->get_stats().prefetch_installs; }
    uint64_t get_l2_prefetch_redundant() const { return l2_->get_stats().prefetch_redundant; }
    uint64_t get_l3_prefetch_installs() const { return l3_->get_stats().prefetch_installs; }
    uint64_t get_l3_prefetch_redundant() const { return l3_->get_stats().prefetch_redundant; }

private:
    std::unique_ptr<Cache> l1i_;
    std::unique_ptr<Cache> l1d_;
    std::unique_ptr<Cache> l2_;
    std::unique_ptr<Cache> l3_;

    std::unique_ptr<TLB> itlb_;
    std::unique_ptr<TLB> dtlb_4k_;
    std::unique_ptr<TLB> dtlb_2m_;
    std::unique_ptr<TLB> dtlb_1g_;
    std::unique_ptr<TLB> stlb_;

    std::unique_ptr<PageTable> page_table_;

    // Configuration
    uint32_t main_memory_latency_;
    uint32_t page_walk_latency_;

    // Page-walk counters (== STLB miss count, the perf-event semantics for
    // dTLB-load-misses / iTLB-load-misses on Intel: WALK_COMPLETED).
    uint64_t data_page_walks_ = 0;
    uint64_t data_page_walks_load_ = 0;
    uint64_t data_page_walks_store_ = 0;
    uint64_t inst_page_walks_ = 0;

    // ---- Experimental cycle-bias extensions ----
    bool enable_mshr_ = true;
    bool enable_dram_bw_ = true;
    bool enable_l2_bw_ = true;
    bool enable_l3_bw_ = true;
    uint32_t mshr_capacity_ = 10;

    // Next-line prefetcher (optional). On every demand load that misses L1D,
    // install the next `next_line_prefetch_distance_` cache lines into the
    // L2 (and refresh L3 / DRAM accounting) so the *next* demand on the
    // sequential successor line is no longer counted as a demand miss.
    bool enable_next_line_prefetcher_ = true;
    uint32_t next_line_prefetch_distance_ = 1;
    uint64_t total_next_line_prefetches_ = 0;

    struct MSHREntry {
        Addr line_addr;
        uint64_t free_cycle;
        LatencyBreakdown breakdown;
    };
    std::deque<MSHREntry> mshr_;
    uint64_t total_mshr_stall_cycles_ = 0;
    uint64_t total_mshr_coalesced_ = 0;
    uint64_t total_mshr_misses_allocated_ = 0;
    uint64_t total_prefetch_mshr_reserved_ = 0;
    uint64_t total_prefetch_mshr_dropped_ = 0;

    // DRAM channel queueing
    uint64_t l2_next_free_ = 0;
    uint64_t l3_next_free_ = 0;
    uint32_t l2_burst_cycles_ = 1;
    uint32_t l3_burst_cycles_ = 1;
    uint64_t total_l2_bw_stall_ = 0;
    uint64_t total_l3_bw_stall_ = 0;
    uint64_t total_l2_accesses_ = 0;
    uint64_t total_l3_accesses_ = 0;
    std::vector<uint64_t> dram_channel_next_free_;
    uint32_t num_dram_channels_ = 6;
    uint32_t dram_burst_cycles_ = 4;
    uint64_t total_dram_bw_stall_ = 0;
    uint64_t total_dram_accesses_ = 0;

    void propagate_l1d_writeback(Addr paddr, uint64_t current_cycle);
    void propagate_l2_writeback(Addr paddr, uint64_t current_cycle);
    void propagate_l3_writeback(Addr paddr, uint64_t current_cycle);
    void account_l2_access(uint64_t current_cycle, uint32_t* total_latency);
    void account_l3_access(uint64_t current_cycle, uint32_t* total_latency);
    void account_dram_access(Addr paddr, uint64_t current_cycle, uint32_t* total_latency);
    void cleanup_mshr(uint64_t current_cycle);
    uint64_t reserve_mshr_fill(Addr line_addr, uint64_t issue_cycle, uint32_t latency, bool allow_drop,
                               const LatencyBreakdown& breakdown = LatencyBreakdown{});

    // Install the next `next_line_prefetch_distance_` 64-byte cache lines
    // following `paddr` into L2 (and L3 if they would otherwise miss). This
    // is the next-line / +1 stride approximation of the SPR L2 stream
    // prefetcher and only affects future demand accesses' hit/miss outcome.
    void prefetch_next_lines_into_l2(Addr paddr, uint64_t current_cycle);

    // Single-access primitives for the data hierarchy. Splits into multiple
    // accesses are orchestrated by read_data/write_data.
    uint32_t access_data_unit(Addr vaddr, uint32_t size, bool is_write, uint64_t current_cycle);

    // Single-access primitive that also reports whether L1D hit and whether
    // the access ultimately went out to DRAM (so the MSHR free-cycle can
    // reflect bandwidth queueing).  Optionally populates a LatencyBreakdown
    // for sub-component attribution.
    uint32_t access_data_unit_with_dram(Addr vaddr, uint32_t size, bool is_write,
                                        uint64_t current_cycle,
                                        bool& l1d_hit, bool& went_to_dram,
                                        LatencyBreakdown* breakdown = nullptr);

    // Translates a virtual address to a physical address.
    // Returns the translation latency (TLB hits/misses + Page Walk).
    uint32_t translate_address(Addr vaddr, Addr& paddr, bool is_instruction, bool is_write, uint64_t current_cycle);
};

} // namespace minesim
