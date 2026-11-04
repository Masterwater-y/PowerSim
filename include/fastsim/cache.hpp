#pragma once

#include <cstdint>
#include <limits>
#include <optional>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

struct CacheLine {
    std::uint64_t tag = 0;
    std::uint8_t age = 0;
    bool valid = false;
    bool dirty = false;
    bool reserved = false; // native request-owned transient tag, not readable data
};

struct CacheSetSnapshot {
    std::uint32_t set = 0;
    std::uint64_t replacement_state = 0;
    std::uint64_t mutation_generation = 0;
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

// A side-effect-free observation of one cache lookup.  The per-set mutation
// generation makes the proposal valid only while the observed set remains
// unchanged.  This lets timing/admission logic decide when an access becomes
// visible without touching replacement state or demand counters early.
struct PreparedCacheLookup {
    std::uint64_t index_line = 0;
    std::uint64_t tag_line = 0;
    std::uint64_t tag = 0;
    std::uint64_t mutation_generation = 0;
    std::uint32_t set = 0;
    std::uint32_t way = 0;
    bool hit = false;
};

struct NativeCacheAllocation {
    std::uint64_t line=0, revision=0;
    std::uint32_t set=0,way=0;
    bool available=false;
    CacheAccessResult victim;
};

class SetAssociativeCache {
  public:
    explicit SetAssociativeCache(const CacheConfig& config);

    const CacheConfig& config() const { return config_; }
    std::uint32_t set_count() const { return set_count_; }

    CacheTransaction begin_transaction(std::size_t event_hint = 0);
    void restore(const CacheTransaction& transaction);

    PreparedCacheLookup prepare_lookup(std::uint64_t line) const;
    PreparedCacheLookup prepare_lookup_indexed(
        std::uint64_t index_line, std::uint64_t tag_line) const;
    bool can_commit(const PreparedCacheLookup& prepared) const;
    // Returns nullopt when another committed access changed the observed set.
    // A rejected commit has no cache or counter side effects.
    std::optional<CacheAccessResult> commit_probe(
        const PreparedCacheLookup& prepared, bool write,
        CacheCounters& counters,
        CacheTransaction* transaction = nullptr);

    // Demand lookup: count the access and touch/dirty a hit, but do not
    // allocate or select a victim on a miss. Counters, as with access(), are
    // owned by the caller and are not rolled back by restore().
    CacheAccessResult probe(std::uint64_t line, bool write,
                            CacheCounters& counters,
                            CacheTransaction* transaction = nullptr);
    CacheAccessResult probe_indexed(
        std::uint64_t index_line, std::uint64_t tag_line, bool write,
        CacheCounters& counters, CacheTransaction* transaction = nullptr);
    // Install at completion using the then-current replacement state. A line
    // installed in the meantime is touched/merged instead of duplicated.
    // Only eviction/writeback counters are charged here; hit reports whether
    // the line was already resident at completion, not the demand outcome.
    // The caller owns callback timing, permissions and generation validity.
    CacheAccessResult complete_fill(std::uint64_t line, bool write,
                                    CacheCounters& counters,
                                    CacheTransaction* transaction = nullptr);
    CacheAccessResult complete_fill_indexed(
        std::uint64_t index_line, std::uint64_t tag_line, bool write,
        CacheCounters& counters, CacheTransaction* transaction = nullptr);

    CacheAccessResult access(std::uint64_t line, bool write,
                             CacheCounters& counters,
                             CacheTransaction* transaction = nullptr);
    // VIPT-style access: `index_line` selects the set while `tag_line`
    // supplies the physical identity. Ordinary physically indexed caches use
    // the same line for both through access().
    CacheAccessResult access_indexed(
        std::uint64_t index_line, std::uint64_t tag_line, bool write,
        CacheCounters& counters,
        CacheTransaction* transaction = nullptr);
    NativeCacheAllocation prepare_native_allocation(std::uint64_t line) const;
    std::optional<CacheAccessResult> reserve_native(const NativeCacheAllocation&, CacheCounters&, std::uint64_t cycle);
    bool complete_native(std::uint64_t line, std::uint64_t cycle, bool write=false, bool touch_data=false);
    bool touch_native(std::uint64_t line, std::uint64_t cycle);
    std::size_t native_reservations() const;
    bool cancel_native(std::uint64_t line);
    bool has_tag(std::uint64_t line) const;
    bool reserved(std::uint64_t line) const;
    bool contains(std::uint64_t line) const;
    bool dirty(std::uint64_t line) const;
    bool invalidate(std::uint64_t line, bool* dirty = nullptr,
                    CacheTransaction* transaction = nullptr);
    void mark_dirty(std::uint64_t line,
                    CacheTransaction* transaction = nullptr);
    // Transfer dirty ownership without removing or touching the resident line.
    bool clear_dirty(std::uint64_t line);

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
    void note_mutation(std::uint32_t set);
    CacheAccessResult install(std::uint32_t set, std::uint64_t tag,
                              bool write, CacheCounters& counters,
                              CacheTransaction* transaction);

    CacheConfig config_;
    std::uint32_t set_count_ = 0;
    std::vector<CacheLine> lines_;
    // Allocated only by the native adapter. Legacy replacement state is unchanged.
    std::vector<std::uint64_t> native_touch_cycles_;
    std::vector<std::uint64_t> replacement_state_;
    std::vector<std::uint64_t> mutation_generation_;
    std::vector<std::uint32_t> snapshot_generation_;
    mutable bool mutation_tracking_ = false;
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

struct PreparedPrivateLookup {
    PreparedCacheLookup l1;
    PreparedCacheLookup l2;
    bool has_l2 = false;
};

class PrivateHierarchy {
  public:
    PrivateHierarchy(const CacheConfig& l1, const CacheConfig& l2);

    PrivateTransaction begin_transaction(std::size_t event_hint = 0);
    void restore(const PrivateTransaction& transaction);
    PreparedPrivateLookup prepare_probe(std::uint64_t line) const;
    PreparedCacheLookup prepare_probe_l1(std::uint64_t line) const;
    std::optional<CacheAccessResult> commit_probe_l1(
        const PreparedCacheLookup&, bool write, CoreCounters&,
        PrivateTransaction* transaction = nullptr);
    PreparedCacheLookup prepare_probe_l2(std::uint64_t line) const;
    bool can_commit(const PreparedPrivateLookup& prepared) const;
    std::optional<PrivateAccessResult> commit_probe(
        const PreparedPrivateLookup& prepared, bool write,
        CoreCounters& counters,
        PrivateTransaction* transaction = nullptr);
    std::optional<PrivateAccessResult> commit_probe_l2(
        const PreparedCacheLookup& prepared, CacheCounters& counters,
        PrivateTransaction* transaction = nullptr);
    PrivateAccessResult probe(std::uint64_t line, bool write,
                              CoreCounters& counters,
                              PrivateTransaction* transaction = nullptr);
    PrivateAccessResult probe_l2(
        std::uint64_t line, CacheCounters& counters,
        PrivateTransaction* transaction = nullptr);
    // Complete a data demand (L1+L2) or instruction demand (L2 only).
    // These maintain inclusion and dirty-victim propagation, without charging
    // another demand lookup. level describes residency at completion; callers
    // retain the original probe result for demand-path classification.
    PrivateAccessResult complete_fill(
        std::uint64_t line, bool write, CoreCounters& counters,
        PrivateTransaction* transaction = nullptr);
    PrivateAccessResult complete_fill_l2(
        std::uint64_t line, CacheCounters& counters,
        PrivateTransaction* transaction = nullptr);
    // L1 response installation after an independently committed L2 fill/hit.
    // A parent evicted during transport cancels installation, not the owned
    // read response; it must not resurrect a private-inclusion victim.
    // Explicit partial runtimes can reject a dirty victim before the legacy
    // immediate L1-to-L2 writeback path. Rejection aborts the owning run.
    bool complete_fill_l1(std::uint64_t line, bool write, CoreCounters&,
                         PrivateTransaction* transaction = nullptr,
                         bool reject_dirty_eviction = false);
    // Native requests reserve transient tags before launching a lower request.
    // Fill completion never chooses a new victim; caller owns service/TBE IDs.
    NativeCacheAllocation prepare_native_l1(std::uint64_t line) const { return l1_.prepare_native_allocation(line); }
    NativeCacheAllocation prepare_native_l2(std::uint64_t line) const { return l2_.prepare_native_allocation(line); }
    std::optional<CacheAccessResult> reserve_native_l1(const NativeCacheAllocation& p, CoreCounters& c,std::uint64_t cycle) {return l1_.reserve_native(p,c.l1d,cycle);}
    std::optional<CacheAccessResult> reserve_native_l2(const NativeCacheAllocation& p, CoreCounters& c,std::uint64_t cycle) {return l2_.reserve_native(p,c.l2,cycle);}
    bool complete_native_l1(std::uint64_t line,bool cancel,std::uint64_t cycle,bool touch_data=true) {return cancel ? l1_.cancel_native(line) : l1_.complete_native(line,cycle,false,touch_data);}
    bool complete_native_l2(std::uint64_t line,std::uint64_t cycle) {return l2_.complete_native(line,cycle,false,true);}
    bool touch_native_l1(std::uint64_t line,std::uint64_t cycle) {return l1_.touch_native(line,cycle);}
    bool touch_native_l2(std::uint64_t line,std::uint64_t cycle) {return l2_.touch_native(line,cycle);}
    bool write_native_l1(std::uint64_t line,std::uint64_t cycle) {if(!l1_.touch_native(line,cycle))return false;l1_.mark_dirty(line);return true;}
    std::size_t native_reservations() const {return l1_.native_reservations()+l2_.native_reservations();}
    bool has_native_tag(std::uint64_t line) const {return l1_.has_tag(line) || l2_.has_tag(line);}
    bool evict_native_l2(std::uint64_t line, CoreCounters&);
    bool accept_writeback_l2(std::uint64_t line, bool dirty = true);
    bool contains_l1(std::uint64_t line) const;
    bool invalidate_l1(std::uint64_t line, bool* dirty);

    PrivateAccessResult access(std::uint64_t line, bool write,
                               CoreCounters& counters,
                               PrivateTransaction* transaction = nullptr);
    // Instruction misses bypass L1D and enter the target's unified private
    // L2. L2 evictions still maintain the existing L1D inclusion contract.
    PrivateAccessResult access_l2(
        std::uint64_t line, CacheCounters& counters,
        PrivateTransaction* transaction = nullptr);
    bool invalidate(std::uint64_t line,
                    PrivateTransaction* transaction = nullptr);
    bool contains(std::uint64_t line) const;
    bool dirty(std::uint64_t line) const;
    void mark_dirty(std::uint64_t line,
                    PrivateTransaction* transaction = nullptr);

  private:
    void finish_l1_eviction(const CacheAccessResult& l1_result,
                            PrivateAccessResult& result,
                            CoreCounters& counters,
                            PrivateTransaction* transaction);
    SetAssociativeCache l1_;
    SetAssociativeCache l2_;
};

}  // namespace fastsim
