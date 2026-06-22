#pragma once
#include <cstdint>
#include <string>

namespace minesim {

struct CacheConfig {
    uint32_t size_kb;
    uint32_t associativity;
    uint32_t line_size;
    uint32_t latency;
};

struct TLBConfig {
    uint32_t entries;
    uint32_t associativity;
    uint32_t latency;
};

struct MemoryConfig {
    uint32_t size_mb;
    uint32_t latency;
    uint32_t default_page_size_kb; // baseline (e.g. 4)
    uint32_t thp_page_size_kb;     // hugepage size (e.g. 2048 = 2MB; 1048576 = 1GB)
    uint64_t thp_min_vaddr;        // vaddrs >= this are mapped using thp_page_size_kb
    uint32_t page_walk_latency;
};

// Experimental switches and parameters for the cycle-bias backend extensions
// (MSHR/LFB limit, DRAM bandwidth queueing, partial STLF, SQ drain blocking).
struct ExperimentalConfig {
    bool enable_mshr = true;
    bool enable_dram_bw = true;
    bool enable_l2_bw = true;
    bool enable_l3_bw = true;
    bool enable_partial_stlf = true;
    bool enable_sq_drain_block = true;

    uint32_t mshr_capacity = 10;            // SPR LFB ~ 10
    uint32_t num_dram_channels = 6;
    uint32_t l2_burst_cycles = 1;           // ~ 1 cache line / cycle sustained
    uint32_t l3_burst_cycles = 1;           // ~ 1 cache line / cycle sustained
    uint32_t dram_burst_cycles = 4;          // ~ 1 transfer / 4 cycles per channel
    uint32_t partial_stlf_penalty = 11;      // SPR LD_BLOCKS.STORE_FORWARD ~ 11

    // Next-line prefetcher (covers the L2/L3 stream/stride family at the
    // simplest level). When a demand miss reaches L2, install the next
    // `next_line_prefetch_distance` lines into L2/L3 directly, *without*
    // billing demand miss counters. This brings simulated LLC demand-miss
    // counts much closer to perf, which observes prefetched lines as already
    // resident on the next demand access.
    bool enable_next_line_prefetcher = true;
    uint32_t next_line_prefetch_distance = 1;  // 1 = +1 line; 2 = +1, +2; etc.

    // DynamoRIO's trace stream used here does not preserve enough untaken
    // conditional-branch information to reproduce PMU branch-misses directly.
    // Treat taken-conditional target BTB misses as a proxy, but expose only a
    // configurable fraction as PMU-visible branch misses.
    uint32_t direct_target_miss_visibility_pct = 58;

    // IntervalCore currently overlaps almost all LFB/MSHR blocked time with
    // independent backend work. Real cores still expose a tail of this pressure
    // as elapsed cycles through ROB/RS occupancy and replay/queue backpressure.
    uint32_t backend_stall_visibility_pct = 10;

    // Branch recovery and long-latency memory pressure are not fully additive:
    // when older loads already block retirement/ROB progress, part of the
    // frontend recovery bubble is hidden behind backend waiting time.
    uint32_t branch_flush_memory_overlap_pct = 35;

    // MineSim Criticality Window (MCW) observation mode. This tracks which
    // dependency, memory, and branch intervals actually extend a lightweight
    // critical tail. In observation mode it only reports decomposition stats.
    bool enable_mcw_stats = true;
    bool enable_mcw_timing = false;
    uint32_t mcw_window_size = 512;
};

struct MicroArchConfig {
    // Branch Predictor configurations
    std::string bp_type;       // "one_bit", "bimodal", or "none"
    uint32_t bp_size;          // Number of entries in the predictor table
    uint32_t bp_history_bits;  // Explicit GHR width for gshare; 0 = derive from size

    // Pipeline widths
    uint32_t fetch_width;
    uint32_t decode_width;
    uint32_t rename_width;
    uint32_t dispatch_width;
    uint32_t issue_width;
    uint32_t retire_width;

    // Execution Ports (Functional Units)
    uint32_t num_alu_ports;
    uint32_t num_load_ports;
    uint32_t num_store_ports;
    uint32_t num_branch_ports;

    // Queue and Buffer sizes
    uint32_t rob_size;         // Reorder Buffer
    uint32_t lq_size;          // Load Queue
    uint32_t sq_size;          // Store Queue
    uint32_t iq_size;          // Instruction Queue / Reservation Station
    
    // Penalties
    uint32_t branch_mispredict_penalty;

    // Cache configurations
    CacheConfig l1i;
    CacheConfig l1d;
    CacheConfig l2;
    CacheConfig l3;

    // TLB configurations
    TLBConfig itlb;
    TLBConfig dtlb_4k;
    TLBConfig dtlb_2m;
    TLBConfig dtlb_1g;
    TLBConfig stlb;

    // Memory configurations
    MemoryConfig mem;

    // Experimental cycle-bias extensions
    ExperimentalConfig experimental;

    // Load from a cfg file (sniper style)
    static MicroArchConfig load_from_file(const std::string& filename);
    static MicroArchConfig get_host_preset();
};

} // namespace minesim
