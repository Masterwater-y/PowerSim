#include "fastsim/cache.hpp"

#include <algorithm>
#include <cassert>
#include <stdexcept>

namespace fastsim {

SetAssociativeCache::SetAssociativeCache(const CacheConfig& config)
    : config_(config) {
    const auto denominator =
        static_cast<std::uint64_t>(config.associativity) * config.line_size;
    set_count_ = static_cast<std::uint32_t>(config.size_bytes / denominator);
    lines_.resize(static_cast<std::size_t>(set_count_) *
                  config.associativity);
    replacement_state_.resize(set_count_);
    snapshot_generation_.resize(set_count_);
}

std::uint32_t SetAssociativeCache::set_of(std::uint64_t line) const {
    return static_cast<std::uint32_t>(line & (set_count_ - 1));
}

std::uint64_t SetAssociativeCache::tag_of(std::uint64_t line) const {
    return line / set_count_;
}

std::uint64_t SetAssociativeCache::line_of(std::uint32_t set,
                                           std::uint64_t tag) const {
    return tag * set_count_ + set;
}

CacheLine* SetAssociativeCache::set_base(std::uint32_t set) {
    return lines_.data() +
           static_cast<std::size_t>(set) * config_.associativity;
}

const CacheLine* SetAssociativeCache::set_base(std::uint32_t set) const {
    return lines_.data() +
           static_cast<std::size_t>(set) * config_.associativity;
}

CacheTransaction SetAssociativeCache::begin_transaction(
    std::size_t event_hint) {
    if (++next_generation_ == 0) {
        std::fill(snapshot_generation_.begin(), snapshot_generation_.end(), 0);
        next_generation_ = 1;
    }
    CacheTransaction transaction;
    transaction.generation = next_generation_;
    const auto expected_sets = std::min<std::size_t>(
        event_hint, set_count_);
    transaction.snapshots.reserve(expected_sets);
    transaction.ways_before.reserve(
        expected_sets * config_.associativity);
    return transaction;
}

void SetAssociativeCache::snapshot_if_needed(
    std::uint32_t set, CacheTransaction* transaction) {
    if (transaction == nullptr) return;
    if (transaction->generation == 0) {
        throw std::logic_error("cache transaction is not initialized");
    }
    if (snapshot_generation_[set] == transaction->generation) return;
    snapshot_generation_[set] = transaction->generation;
    CacheSetSnapshot snapshot;
    snapshot.set = set;
    snapshot.replacement_state = replacement_state_[set];
    snapshot.ways_offset = transaction->ways_before.size();
    const auto* base = set_base(set);
    transaction->ways_before.insert(
        transaction->ways_before.end(), base,
        base + config_.associativity);
    transaction->snapshots.push_back(std::move(snapshot));
}

void SetAssociativeCache::restore(const CacheTransaction& transaction) {
    for (const auto& snapshot : transaction.snapshots) {
        assert(snapshot.ways_offset + config_.associativity <=
               transaction.ways_before.size());
        auto* base = set_base(snapshot.set);
        const auto first = transaction.ways_before.begin() +
            static_cast<std::ptrdiff_t>(snapshot.ways_offset);
        std::copy(first, first + config_.associativity, base);
        replacement_state_[snapshot.set] = snapshot.replacement_state;
    }
}

std::uint32_t SetAssociativeCache::choose_victim(std::uint32_t set) const {
    const auto* base = set_base(set);
    for (std::uint32_t way = 0; way < config_.associativity; ++way) {
        if (!base[way].valid) return way;
    }
    if (config_.replacement == ReplacementPolicy::kLru) {
        std::uint32_t victim = 0;
        for (std::uint32_t way = 1; way < config_.associativity; ++way) {
            if (base[way].age > base[victim].age) victim = way;
        }
        return victim;
    }

    std::uint32_t node = 0;
    std::uint32_t first_way = 0;
    std::uint32_t ways = config_.associativity;
    const auto state = replacement_state_[set];
    while (ways > 1) {
        const bool choose_right = ((state >> node) & 1u) != 0;
        const auto half = ways / 2;
        node = choose_right ? (node * 2 + 2) : (node * 2 + 1);
        if (choose_right) first_way += half;
        ways = half;
    }
    return first_way;
}

void SetAssociativeCache::touch(std::uint32_t set, std::uint32_t way) {
    auto* base = set_base(set);
    if (config_.replacement == ReplacementPolicy::kLru) {
        const auto old_age = base[way].age;
        for (std::uint32_t candidate = 0;
             candidate < config_.associativity; ++candidate) {
            if (candidate != way && base[candidate].valid &&
                base[candidate].age < old_age &&
                base[candidate].age <
                    std::numeric_limits<std::uint8_t>::max()) {
                ++base[candidate].age;
            }
        }
        base[way].age = 0;
        return;
    }

    std::uint32_t node = 0;
    std::uint32_t first_way = 0;
    std::uint32_t ways = config_.associativity;
    auto& state = replacement_state_[set];
    while (ways > 1) {
        const auto half = ways / 2;
        const bool went_right = way >= first_way + half;
        if (went_right) {
            state &= ~(1ull << node);  // left subtree is now LRU
            first_way += half;
            node = node * 2 + 2;
        } else {
            state |= 1ull << node;  // right subtree is now LRU
            node = node * 2 + 1;
        }
        ways = half;
    }
}

CacheAccessResult SetAssociativeCache::access(
    std::uint64_t line, bool write, CacheCounters& counters,
    CacheTransaction* transaction) {
    ++counters.accesses;
    const auto set = set_of(line);
    const auto tag = tag_of(line);
    auto* base = set_base(set);
    for (std::uint32_t way = 0; way < config_.associativity; ++way) {
        if (base[way].valid && base[way].tag == tag) {
            snapshot_if_needed(set, transaction);
            ++counters.hits;
            base[way].dirty = base[way].dirty || write;
            touch(set, way);
            return CacheAccessResult{true, false, false, 0};
        }
    }

    snapshot_if_needed(set, transaction);
    ++counters.misses;
    const auto victim = choose_victim(set);
    CacheAccessResult result;
    if (base[victim].valid) {
        result.evicted = true;
        result.evicted_dirty = base[victim].dirty;
        result.evicted_line = line_of(set, base[victim].tag);
        ++counters.evictions;
        if (base[victim].dirty) ++counters.writebacks;
    }
    base[victim].valid = true;
    base[victim].dirty = write;
    base[victim].tag = tag;
    base[victim].age =
        static_cast<std::uint8_t>(config_.associativity - 1);
    touch(set, victim);
    return result;
}

bool SetAssociativeCache::contains(std::uint64_t line) const {
    const auto set = set_of(line);
    const auto tag = tag_of(line);
    const auto* base = set_base(set);
    for (std::uint32_t way = 0; way < config_.associativity; ++way) {
        if (base[way].valid && base[way].tag == tag) return true;
    }
    return false;
}

bool SetAssociativeCache::invalidate(std::uint64_t line, bool* dirty,
                                     CacheTransaction* transaction) {
    const auto set = set_of(line);
    const auto tag = tag_of(line);
    auto* base = set_base(set);
    for (std::uint32_t way = 0; way < config_.associativity; ++way) {
        if (base[way].valid && base[way].tag == tag) {
            snapshot_if_needed(set, transaction);
            if (dirty != nullptr) *dirty = base[way].dirty;
            base[way].valid = false;
            base[way].dirty = false;
            return true;
        }
    }
    if (dirty != nullptr) *dirty = false;
    return false;
}

void SetAssociativeCache::mark_dirty(std::uint64_t line,
                                     CacheTransaction* transaction) {
    const auto set = set_of(line);
    const auto tag = tag_of(line);
    auto* base = set_base(set);
    for (std::uint32_t way = 0; way < config_.associativity; ++way) {
        if (base[way].valid && base[way].tag == tag) {
            snapshot_if_needed(set, transaction);
            base[way].dirty = true;
            return;
        }
    }
}

PrivateHierarchy::PrivateHierarchy(const CacheConfig& l1,
                                   const CacheConfig& l2)
    : l1_(l1), l2_(l2) {}

PrivateTransaction PrivateHierarchy::begin_transaction(
    std::size_t event_hint) {
    return PrivateTransaction{l1_.begin_transaction(event_hint),
                              l2_.begin_transaction(event_hint)};
}

void PrivateHierarchy::restore(const PrivateTransaction& transaction) {
    l1_.restore(transaction.l1);
    l2_.restore(transaction.l2);
}

PrivateAccessResult PrivateHierarchy::access(
    std::uint64_t line, bool write, CoreCounters& counters,
    PrivateTransaction* transaction) {
    auto* l1_txn = transaction == nullptr ? nullptr : &transaction->l1;
    auto* l2_txn = transaction == nullptr ? nullptr : &transaction->l2;

    const auto l1_result =
        l1_.access(line, write, counters.l1d, l1_txn);
    if (l1_result.hit) {
        return PrivateAccessResult{HitLevel::kL1, false, false, 0};
    }

    PrivateAccessResult result;
    const auto l2_result =
        l2_.access(line, false, counters.l2, l2_txn);
    if (l2_result.hit) {
        result.level = HitLevel::kL2;
    } else {
        result.level = HitLevel::kLlc;
        result.l2_evicted = l2_result.evicted;
        result.l2_evicted_dirty = l2_result.evicted_dirty;
        result.l2_evicted_line = l2_result.evicted_line;
        if (l2_result.evicted) {
            bool l1_dirty = false;
            if (l1_.invalidate(l2_result.evicted_line, &l1_dirty, l1_txn) &&
                l1_dirty) {
                result.l2_evicted_dirty = true;
            }
        }
    }

    // A dirty L1 victim is written back into the inclusive private L2.
    if (l1_result.evicted && l1_result.evicted_dirty) {
        if (result.l2_evicted &&
            result.l2_evicted_line == l1_result.evicted_line) {
            // The demand fill selected the same line that L1 just evicted.
            // Carry the dirty data with that eviction instead of refilling a
            // line that has already left the private hierarchy.
            result.l2_evicted_dirty = true;
        } else if (l2_.contains(l1_result.evicted_line)) {
            l2_.mark_dirty(l1_result.evicted_line, l2_txn);
        } else {
            const auto writeback = l2_.access(
                l1_result.evicted_line, true, counters.l2, l2_txn);
            if (writeback.evicted && result.l2_evicted) {
                throw std::logic_error(
                    "inclusive private hierarchy produced two L2 victims");
            }
            if (writeback.evicted) {
                result.l2_evicted = true;
                result.l2_evicted_dirty = writeback.evicted_dirty;
                result.l2_evicted_line = writeback.evicted_line;
            }
        }
    }
    return result;
}

bool PrivateHierarchy::invalidate(std::uint64_t line,
                                  PrivateTransaction* transaction) {
    auto* l1_txn = transaction == nullptr ? nullptr : &transaction->l1;
    auto* l2_txn = transaction == nullptr ? nullptr : &transaction->l2;
    bool l1_dirty = false;
    bool l2_dirty = false;
    const bool l1_found = l1_.invalidate(line, &l1_dirty, l1_txn);
    const bool l2_found = l2_.invalidate(line, &l2_dirty, l2_txn);
    return l1_found || l2_found || l1_dirty || l2_dirty;
}

bool PrivateHierarchy::contains(std::uint64_t line) const {
    return l1_.contains(line) || l2_.contains(line);
}

}  // namespace fastsim
