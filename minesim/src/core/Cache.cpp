#include "core/Cache.h"
#include <iostream>
#include <iomanip>
#include <cmath>

namespace minesim {

Cache::Cache(const std::string& name, const CacheConfig& config)
    : name_(name),
      size_bytes_(config.size_kb * 1024),
      associativity_(config.associativity),
      line_size_(config.line_size),
      latency_(config.latency),
      stats_() {

    // Ensure associativity and line_size are non-zero to avoid division by zero
    if (associativity_ == 0) {
        std::cerr << "Warning: Cache associativity is 0, defaulting to 1 for " << name << "\n";
        associativity_ = 1;
    }
    if (line_size_ == 0) {
        std::cerr << "Warning: Cache line size is 0, defaulting to 64 for " << name << "\n";
        line_size_ = 64;
    }

    num_sets_ = size_bytes_ / (line_size_ * associativity_);
    
    // Check if num_sets_ is a power of 2 for bitmask optimization
    if ((num_sets_ & (num_sets_ - 1)) != 0) {
        std::cerr << "Error: Cache num_sets is not a power of 2 for " << name << "\n";
        exit(1);
    }

    set_index_mask_ = num_sets_ - 1;
    tag_shift_ = static_cast<uint32_t>(std::log2(line_size_)) + static_cast<uint32_t>(std::log2(num_sets_));

    sets_.reserve(num_sets_);
    for (uint32_t i = 0; i < num_sets_; ++i) {
        sets_.emplace_back(associativity_);
    }

    std::cout << "Initialized Cache: " << name 
              << " | Size: " << config.size_kb << " KB"
              << " | Assoc: " << associativity_ 
              << " | Line: " << line_size_ << " B"
              << " | Sets: " << num_sets_ 
              << " | Latency: " << latency_ << " cycles\n";
}

CacheAccessResult Cache::access(Addr addr, bool is_write, uint64_t current_cycle) {
    CacheAccessResult result;
    stats_.accesses++;

    uint32_t set_idx = extract_set_index(addr);
    Addr tag = extract_tag(addr);

    int block_idx = find_block(set_idx, tag);

    if (block_idx != -1) {
        // Cache Hit
        stats_.hits++;
        result.hit = true;
        
        CacheBlock& block = sets_[set_idx].blocks[block_idx];
        block.last_access_time = current_cycle;
        if (is_write) {
            block.dirty = true;
        }
        return result;
    }

    // Cache Miss
    stats_.misses++;
    
    // Allocate new block (Replacement)
    int lru_idx = find_lru_block(set_idx);
    CacheBlock& replaced_block = sets_[set_idx].blocks[lru_idx];

    // Handle Writeback if dirty
    if (replaced_block.valid && replaced_block.dirty) {
        stats_.writebacks++;
        result.evicted_dirty = true;
        result.evicted_addr = make_line_addr(replaced_block.tag, set_idx);
    }

    // Update block info
    replaced_block.valid = true;
    replaced_block.tag = tag;
    replaced_block.dirty = is_write;
    replaced_block.last_access_time = current_cycle;

    return result;
}

void Cache::reset_stats() {
    stats_ = Stats{};
}

void Cache::print_stats() const {
    double hit_rate = stats_.accesses > 0 ? (double)stats_.hits / stats_.accesses * 100.0 : 0.0;
    double miss_rate = stats_.accesses > 0 ? (double)stats_.misses / stats_.accesses * 100.0 : 0.0;

    std::cout << "\n--- Cache Stats: " << name_ << " ---\n"
              << "Accesses:   " << stats_.accesses << "\n"
              << "Hits:       " << stats_.hits << " (" << std::fixed << std::setprecision(2) << hit_rate << "%)\n"
              << "Misses:     " << stats_.misses << " (" << std::fixed << std::setprecision(2) << miss_rate << "%)\n"
              << "Writebacks: " << stats_.writebacks << "\n"
              << "Prefetch installs: " << stats_.prefetch_installs
              << " (redundant: " << stats_.prefetch_redundant << ")\n"
              << "---------------------------\n";
}

CacheAccessResult Cache::prefetch_install(Addr addr, uint64_t current_cycle) {
    CacheAccessResult result;

    uint32_t set_idx = extract_set_index(addr);
    Addr tag = extract_tag(addr);

    int block_idx = find_block(set_idx, tag);
    if (block_idx != -1) {
        // Line is already present: just refresh LRU. Do NOT bill demand
        // hit/miss counters; bump redundant counter for visibility.
        stats_.prefetch_redundant++;
        sets_[set_idx].blocks[block_idx].last_access_time = current_cycle;
        result.hit = true;
        return result;
    }

    // Allocate a new block (LRU replacement) without touching access/miss
    // counters. The caller is responsible for propagating evicted dirty
    // writebacks down the hierarchy.
    int lru_idx = find_lru_block(set_idx);
    CacheBlock& replaced_block = sets_[set_idx].blocks[lru_idx];
    if (replaced_block.valid && replaced_block.dirty) {
        stats_.writebacks++;
        result.evicted_dirty = true;
        result.evicted_addr = make_line_addr(replaced_block.tag, set_idx);
    }
    replaced_block.valid = true;
    replaced_block.tag = tag;
    replaced_block.dirty = false;
    replaced_block.last_access_time = current_cycle;
    stats_.prefetch_installs++;
    return result;
}

Addr Cache::extract_tag(Addr addr) const {
    return addr >> tag_shift_;
}

uint32_t Cache::extract_set_index(Addr addr) const {
    return (addr >> static_cast<uint32_t>(std::log2(line_size_))) & set_index_mask_;
}

Addr Cache::make_line_addr(Addr tag, uint32_t set_idx) const {
    uint32_t line_shift = static_cast<uint32_t>(std::log2(line_size_));
    return (tag << tag_shift_) | (static_cast<Addr>(set_idx) << line_shift);
}

int Cache::find_block(uint32_t set_idx, Addr tag) const {
    const CacheSet& set = sets_[set_idx];
    for (uint32_t i = 0; i < associativity_; ++i) {
        if (set.blocks[i].valid && set.blocks[i].tag == tag) {
            return i;
        }
    }
    return -1;
}

int Cache::find_lru_block(uint32_t set_idx) const {
    const CacheSet& set = sets_[set_idx];
    int lru_idx = 0;
    uint64_t min_time = set.blocks[0].last_access_time;

    for (uint32_t i = 0; i < associativity_; ++i) {
        // Prefer invalid blocks first
        if (!set.blocks[i].valid) {
            return i;
        }
        
        // Find least recently used
        if (set.blocks[i].last_access_time < min_time) {
            min_time = set.blocks[i].last_access_time;
            lru_idx = i;
        }
    }
    return lru_idx;
}

} // namespace minesim
