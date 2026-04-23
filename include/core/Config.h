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
    uint32_t page_size_kb;
    uint32_t page_walk_latency;
};

struct MicroArchConfig {
    // Branch Predictor configurations
    std::string bp_type;       // "one_bit", "bimodal", or "none"
    uint32_t bp_size;          // Number of entries in the predictor table

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
    TLBConfig dtlb;
    TLBConfig stlb;

    // Memory configurations
    MemoryConfig mem;

    // Load from a cfg file (sniper style)
    static MicroArchConfig load_from_file(const std::string& filename);
    static MicroArchConfig get_host_preset();
};

} // namespace minesim