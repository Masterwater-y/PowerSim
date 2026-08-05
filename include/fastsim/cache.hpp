#pragma once

#include <cstdint>
#include <limits>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

struct CacheLine {
    std::uint64_t tag = 0;
    std::uint8_t age = 0;
    bool valid = false;
    bool dirty = false;
};

struct CacheSetSnapshot {
    std::uint32_t set = 0;
    std::uint64_t replacement_state = 0;
    std::size_t ways_offset = 0;
};

struct CacheTransaction {
    std::uint32_t generation = 0;
    std::vector<CacheSetSnapshot> snapshots;
    // All set ways share one append-only undo buffer.  A transaction touching
    // thousands of sets therefore grows two vectors geometrically instead of
    // allocating one vector per set.
    std::vector<CacheLine> ways_before;

    void clear() {
        generation = 0;
        snapshots.clear();
        ways_before.clear();
    }
};

struct CacheAccessResult {
    bool hit = false;
    bool evicted = false;
    bool evicted_dirty = false;
    std::uint64_t evicted_line = 0;
};

class SetAssociativeCache {
  public:
    explicit SetAssociativeCache(const CacheConfig& config);

    const CacheConfig& config() const { return config_; }
    std::uint32_t set_count() const { return set_count_; }

    CacheTransaction begin_transaction(std::size_t event_hint = 0);
    void restore(const CacheTransaction& transaction);

    CacheAccessResult access(std::uint64_t line, bool write,
                             CacheCounters& counters,
                             CacheTransaction* transaction = nullptr);
    bool contains(std::uint64_t line) const;
    bool invalidate(std::uint64_t line, bool* dirty = nullptr,
                    CacheTransaction* transaction = nullptr);
    void mark_dirty(std::uint64_t line,
                    CacheTransaction* transaction = nullptr);

  private:
    std::uint32_t set_of(std::uint64_t line) const;
    std::uint64_t tag_of(std::uint64_t line) const;
    std::uint64_t line_of(std::uint32_t set, std::uint64_t tag) const;
    CacheLine* set_base(std::uint32_t set);
    const CacheLine* set_base(std::uint32_t set) const;
    void snapshot_if_needed(std::uint32_t set,
                            CacheTransaction* transaction);
    std::uint32_t choose_victim(std::uint32_t set) const;
    void touch(std::uint32_t set, std::uint32_t way);

    CacheConfig config_;
    std::uint32_t set_count_ = 0;
    std::vector<CacheLine> lines_;
    std::vector<std::uint64_t> replacement_state_;
    std::vector<std::uint32_t> snapshot_generation_;
    std::uint32_t next_generation_ = 1;
};

struct PrivateAccessResult {
    HitLevel level = HitLevel::kUnknown;
    bool l2_evicted = false;
    bool l2_evicted_dirty = false;
    std::uint64_t l2_evicted_line = 0;
};

struct PrivateTransaction {
    CacheTransaction l1;
    CacheTransaction l2;
};

class PrivateHierarchy {
  public:
    PrivateHierarchy(const CacheConfig& l1, const CacheConfig& l2);

    PrivateTransaction begin_transaction(std::size_t event_hint = 0);
    void restore(const PrivateTransaction& transaction);
    PrivateAccessResult access(std::uint64_t line, bool write,
                               CoreCounters& counters,
                               PrivateTransaction* transaction = nullptr);
    bool invalidate(std::uint64_t line,
                    PrivateTransaction* transaction = nullptr);
    bool contains(std::uint64_t line) const;

  private:
    SetAssociativeCache l1_;
    SetAssociativeCache l2_;
};

}  // namespace fastsim
