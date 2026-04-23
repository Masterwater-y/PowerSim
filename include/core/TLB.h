#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include <vector>
#include <cstdint>

namespace minesim {

struct TLBEntry {
    Addr vpn = 0;
    Addr ppn = 0;
    bool valid = false;
    uint64_t last_access_time = 0;
};

struct TLBSet {
    std::vector<TLBEntry> entries;

    TLBSet(uint32_t associativity) : entries(associativity) {}
};

class TLB {
public:
    struct Stats {
        uint64_t accesses = 0;
        uint64_t hits = 0;
        uint64_t misses = 0;
    };

    TLB(const std::string& name, const TLBConfig& config);
    virtual ~TLB() = default;

    // Lookup a VPN in the TLB
    // Returns true if hit (and updates hit stats), false if miss
    bool lookup(Addr vpn, Addr& out_ppn, uint64_t current_cycle);

    // Insert a new VPN -> PPN mapping into the TLB
    void insert(Addr vpn, Addr ppn, uint64_t current_cycle);

    // Statistics
    const Stats& get_stats() const { return stats_; }
    void print_stats() const;

    const std::string& get_name() const { return name_; }
    uint32_t get_latency() const { return latency_; }

protected:
    std::string name_;

    uint32_t entries_;
    uint32_t associativity_;
    uint32_t latency_;

    uint32_t num_sets_;
    uint32_t set_index_mask_;

    std::vector<TLBSet> sets_;
    Stats stats_;

    // Internal helper
    uint32_t extract_set_index(Addr vpn) const;
    int find_entry(uint32_t set_idx, Addr vpn) const;
    int find_lru_entry(uint32_t set_idx) const;
};

} // namespace minesim
