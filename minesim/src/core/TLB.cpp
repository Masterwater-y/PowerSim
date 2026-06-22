#include "core/TLB.h"
#include <iostream>
#include <iomanip>
#include <cmath>

namespace minesim {

TLB::TLB(const std::string& name, const TLBConfig& config)
    : name_(name),
      entries_(config.entries),
      associativity_(config.associativity),
      latency_(config.latency),
      stats_() {

    if (associativity_ == 0) {
        std::cerr << "Warning: TLB associativity is 0, defaulting to 1 for " << name << "\n";
        associativity_ = 1;
    }

    if (entries_ == 0) {
        // Just simulate a perfect TLB or disable it by allocating 1 entry
        std::cerr << "Warning: TLB entries is 0, defaulting to 1 for " << name << "\n";
        entries_ = 1;
    }

    num_sets_ = entries_ / associativity_;
    if (num_sets_ == 0) num_sets_ = 1;

    set_index_mask_ = num_sets_ - 1;

    sets_.reserve(num_sets_);
    for (uint32_t i = 0; i < num_sets_; ++i) {
        sets_.emplace_back(associativity_);
    }

    std::cout << "Initialized TLB: " << name 
              << " | Entries: " << entries_ 
              << " | Assoc: " << associativity_ 
              << " | Sets: " << num_sets_ 
              << " | Latency: " << latency_ << " cycles\n";
}

bool TLB::lookup(Addr vpn, Addr& out_ppn, uint64_t current_cycle) {
    stats_.accesses++;

    uint32_t set_idx = extract_set_index(vpn);
    int entry_idx = find_entry(set_idx, vpn);

    if (entry_idx != -1) {
        stats_.hits++;
        sets_[set_idx].entries[entry_idx].last_access_time = current_cycle;
        out_ppn = sets_[set_idx].entries[entry_idx].ppn;
        return true;
    }

    stats_.misses++;
    return false;
}

void TLB::insert(Addr vpn, Addr ppn, uint64_t current_cycle) {
    uint32_t set_idx = extract_set_index(vpn);
    int lru_idx = find_lru_entry(set_idx);

    TLBEntry& replaced_entry = sets_[set_idx].entries[lru_idx];
    replaced_entry.valid = true;
    replaced_entry.vpn = vpn;
    replaced_entry.ppn = ppn;
    replaced_entry.last_access_time = current_cycle;
}

void TLB::reset_stats() {
    stats_ = Stats{};
}

void TLB::print_stats() const {
    double hit_rate = stats_.accesses > 0 ? (double)stats_.hits / stats_.accesses * 100.0 : 0.0;
    double miss_rate = stats_.accesses > 0 ? (double)stats_.misses / stats_.accesses * 100.0 : 0.0;

    std::cout << "\n--- TLB Stats: " << name_ << " ---\n"
              << "Accesses: " << stats_.accesses << "\n"
              << "Hits:     " << stats_.hits << " (" << std::fixed << std::setprecision(2) << hit_rate << "%)\n"
              << "Misses:   " << stats_.misses << " (" << std::fixed << std::setprecision(2) << miss_rate << "%)\n"
              << "---------------------------\n";
}

uint32_t TLB::extract_set_index(Addr vpn) const {
    return static_cast<uint32_t>(vpn % num_sets_);
}

int TLB::find_entry(uint32_t set_idx, Addr vpn) const {
    const TLBSet& set = sets_[set_idx];
    for (uint32_t i = 0; i < associativity_; ++i) {
        if (set.entries[i].valid && set.entries[i].vpn == vpn) {
            return i;
        }
    }
    return -1;
}

int TLB::find_lru_entry(uint32_t set_idx) const {
    const TLBSet& set = sets_[set_idx];
    int lru_idx = 0;
    uint64_t min_time = set.entries[0].last_access_time;

    for (uint32_t i = 0; i < associativity_; ++i) {
        if (!set.entries[i].valid) {
            return i;
        }
        if (set.entries[i].last_access_time < min_time) {
            min_time = set.entries[i].last_access_time;
            lru_idx = i;
        }
    }
    return lru_idx;
}

} // namespace minesim
