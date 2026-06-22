#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include <vector>
#include <cstdint>

namespace minesim {

struct CacheAccessResult {
    bool hit = false;
    bool evicted_dirty = false;
    Addr evicted_addr = 0;
};

// Represents a single cache block/line
struct CacheBlock {
    Addr tag = 0;
    bool valid = false;
    bool dirty = false;
    uint64_t last_access_time = 0; // Used for LRU policy
};

// Represents a set in a set-associative cache
struct CacheSet {
    std::vector<CacheBlock> blocks;

    CacheSet(uint32_t associativity) : blocks(associativity) {}
};

class Cache {
public:
    // Statistics for this cache
    struct Stats {
        uint64_t accesses = 0;
        uint64_t hits = 0;
        uint64_t misses = 0;
        uint64_t writebacks = 0;
        uint64_t prefetch_installs = 0;
        uint64_t prefetch_redundant = 0;
    };

    Cache(const std::string& name, const CacheConfig& config);
    virtual ~Cache() = default;

    // Perform a memory access (read or write).
    // On a miss, the result reports whether a dirty victim must be written back.
    CacheAccessResult access(Addr addr, bool is_write, uint64_t current_cycle);

    // Install a line into this cache without billing demand hit/miss counters.
    // - If the line is already present: returns hit=true and refreshes LRU.
    // - Otherwise: allocates a slot (potentially evicting a dirty victim, in
    //   which case evicted_dirty/evicted_addr are populated for the caller
    //   to propagate the writeback) and bumps prefetch_installs.
    // This is the API the next-line / stream prefetcher uses to bring data
    // into L2/L3 ahead of demand without polluting demand miss counters.
    CacheAccessResult prefetch_install(Addr addr, uint64_t current_cycle);

    // Get cache statistics
    const Stats& get_stats() const { return stats_; }
    void reset_stats();

    // Print statistics
    void print_stats() const;

    const std::string& get_name() const { return name_; }
    uint32_t get_latency() const { return latency_; }

protected:
    std::string name_;
    
    // Cache configuration
    uint32_t size_bytes_;
    uint32_t associativity_;
    uint32_t line_size_;
    uint32_t latency_;

    // Derived parameters
    uint32_t num_sets_;
    uint32_t set_index_mask_;
    uint32_t tag_shift_;

    // Storage
    std::vector<CacheSet> sets_;

    // Statistics
    Stats stats_;

    // Internal helper methods
    Addr extract_tag(Addr addr) const;
    uint32_t extract_set_index(Addr addr) const;
    Addr make_line_addr(Addr tag, uint32_t set_idx) const;
    
    // Returns the index of the block if hit, or -1 if miss
    int find_block(uint32_t set_idx, Addr tag) const;
    
    // Find the LRU block in a set
    int find_lru_block(uint32_t set_idx) const;
};

} // namespace minesim
