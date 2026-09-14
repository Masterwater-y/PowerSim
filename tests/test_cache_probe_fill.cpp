#include "fastsim/cache.hpp"

#include <stdexcept>

namespace {

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

fastsim::CacheConfig cache_config(std::uint64_t lines, std::uint32_t ways) {
    fastsim::CacheConfig config;
    config.size_bytes = lines * 64;
    config.associativity = ways;
    return config;
}

void test_probe_visibility_and_completion_victim() {
    fastsim::SetAssociativeCache cache(cache_config(2, 2));
    fastsim::CacheCounters counters;
    cache.access(0, false, counters);
    cache.access(1, false, counters);
    auto transaction = cache.begin_transaction();
    const auto miss = cache.probe(2, true, counters, &transaction);
    require(!miss.hit && !miss.evicted && !cache.contains(2),
            "probe miss must not expose a pending fill");
    require(transaction.snapshots.empty() && cache.contains(0) &&
                cache.contains(1) && counters.evictions == 0,
            "probe miss must not reserve a victim or mutate replacement");
    cache.probe(0, false, counters, &transaction);
    const auto fill = cache.complete_fill(2, true, counters, &transaction);
    require(fill.evicted && fill.evicted_line == 1 && !fill.evicted_dirty,
            "completion must choose the current victim after intervening hit");
    require(cache.contains(2) && cache.contains(0) && !cache.contains(1),
            "completion installs exactly the requested line");
    require(counters.accesses == 4 && counters.hits == 1 &&
                counters.misses == 3 && counters.evictions == 1,
            "completion must not count a second demand access");
    cache.restore(transaction);
    require(cache.contains(0) && cache.contains(1) && !cache.contains(2),
            "transaction restore must undo delayed allocation");
    const auto restored_victim = cache.complete_fill(3, false, counters);
    require(restored_victim.evicted_line == 0,
            "transaction restore must undo probe-hit replacement touch");
}

void test_probe_dirty_and_duplicate_completion() {
    fastsim::SetAssociativeCache cache(cache_config(2, 2));
    fastsim::CacheCounters counters;
    cache.complete_fill(0, false, counters);
    cache.complete_fill(1, false, counters);
    auto transaction = cache.begin_transaction();
    require(cache.probe(0, true, counters, &transaction).hit,
            "probe must report existing lines");
    bool dirty = false;
    cache.invalidate(0, &dirty, &transaction);
    require(dirty, "write probe hit must mark dirty");
    cache.restore(transaction);
    cache.invalidate(0, &dirty);
    require(!dirty, "transaction restore must undo probe-hit dirty state");
    cache.complete_fill(0, true, counters);
    const auto duplicate = cache.complete_fill(0, false, counters);
    require(duplicate.hit && !duplicate.evicted && counters.accesses == 1 &&
                counters.hits == 1 && counters.misses == 0,
            "duplicate completion merges without demand accounting");
    cache.invalidate(0, &dirty);
    require(dirty, "read completion must preserve existing dirty data");
}

void test_indexed_probe_and_fill() {
    fastsim::SetAssociativeCache cache(cache_config(4, 1));
    fastsim::CacheCounters counters;
    auto transaction = cache.begin_transaction();
    require(!cache.probe_indexed(1, 8, false, counters, &transaction).hit,
            "indexed probe initially misses");
    require(transaction.snapshots.empty(), "indexed miss must not mutate");
    cache.complete_fill_indexed(1, 8, false, counters, &transaction);
    require(cache.probe_indexed(1, 8, false, counters, &transaction).hit,
            "indexed completion must retain virtual index and physical tag");
    require(!cache.probe_indexed(0, 8, false, counters, &transaction).hit,
            "indexed completion must not fill the physical-index set");
    cache.restore(transaction);
    require(!cache.probe_indexed(1, 8, false, counters).hit,
            "indexed completion must be transactional");
}

void test_private_visibility_inclusion_and_restore() {
    fastsim::PrivateHierarchy hierarchy(cache_config(1, 1), cache_config(2, 1));
    fastsim::CoreCounters counters;
    auto transaction = hierarchy.begin_transaction();
    require(hierarchy.probe(0, true, counters, &transaction).level ==
                fastsim::HitLevel::kLlc && !hierarchy.contains(0),
            "private probe miss must not fill either level");
    hierarchy.complete_fill(0, true, counters, &transaction);
    require(hierarchy.probe(0, false, counters, &transaction).level ==
                fastsim::HitLevel::kL1,
            "data completion must expose an L1 hit");
    require(counters.l1d.accesses == 2 && counters.l2.accesses == 1,
            "private completion must not double-count demand lookups");
    hierarchy.complete_fill(1, false, counters, &transaction);
    require(hierarchy.probe(0, false, counters, &transaction).level ==
                fastsim::HitLevel::kL2,
            "dirty L1 victim must remain in inclusive L2");
    require(counters.l1d.writebacks == 1,
            "completion must charge dirty L1 victim writeback");
    const auto eviction =
        hierarchy.complete_fill_l2(2, counters.l2, &transaction);
    require(eviction.l2_evicted && eviction.l2_evicted_line == 0 &&
                eviction.l2_evicted_dirty && counters.l2.writebacks == 1,
            "L1 dirty victim must propagate into later L2 eviction");
    require(!hierarchy.contains(0), "L2 eviction removes inclusive line");
    hierarchy.restore(transaction);
    require(!hierarchy.contains(0) && !hierarchy.contains(1) &&
                !hierarchy.contains(2),
            "private transaction restore must undo both levels");

    hierarchy.complete_fill(0, true, counters);
    const auto instruction_eviction =
        hierarchy.complete_fill_l2(2, counters.l2);
    require(instruction_eviction.l2_evicted_dirty && !hierarchy.contains(0),
            "instruction completion must invalidate dirty inclusive L1 victim");
    require(hierarchy.probe(2, false, counters).level == fastsim::HitLevel::kL2,
            "instruction completion must not install in L1D");
}

void test_private_same_dirty_victim_and_l2_hit_restore() {
    fastsim::PrivateHierarchy hierarchy(cache_config(1, 1), cache_config(1, 1));
    fastsim::CoreCounters counters;
    hierarchy.complete_fill(0, true, counters);
    const auto eviction = hierarchy.complete_fill(1, false, counters);
    require(eviction.l2_evicted && eviction.l2_evicted_line == 0 &&
                eviction.l2_evicted_dirty && !hierarchy.contains(0),
            "same L1/L2 victim must carry dirty data without resurrection");

    fastsim::PrivateHierarchy hits(cache_config(1, 1), cache_config(2, 2));
    fastsim::CoreCounters hit_counters;
    hits.complete_fill_l2(0, hit_counters.l2);
    hits.complete_fill_l2(1, hit_counters.l2);
    auto transaction = hits.begin_transaction();
    require(hits.probe_l2(0, hit_counters.l2, &transaction).level ==
                fastsim::HitLevel::kL2,
            "L2 probe must report the hit without allocating L1");
    require(hits.complete_fill_l2(2, hit_counters.l2, &transaction).
                l2_evicted_line == 1,
            "L2 probe hit must touch replacement state");
    hits.restore(transaction);
    require(hits.complete_fill_l2(2, hit_counters.l2).l2_evicted_line == 0,
            "private restore must undo L2 probe touch and completion victim");
}

void test_prepared_lookup_is_side_effect_free_and_guarded() {
    fastsim::SetAssociativeCache cache(cache_config(2, 2));
    fastsim::CacheCounters counters;
    cache.complete_fill(0, false, counters);
    cache.complete_fill(1, false, counters);

    const auto prepared = cache.prepare_lookup(0);
    require(prepared.hit && counters.accesses == 0 && counters.hits == 0,
            "prepare must observe a hit without demand accounting");
    const auto intervening = cache.complete_fill(2, false, counters);
    require(intervening.evicted && intervening.evicted_line == 0,
            "prepare must not touch replacement state");
    const auto counters_before_reject = counters;
    require(!cache.commit_probe(prepared, false, counters).has_value(),
            "same-set mutation must invalidate a prepared lookup");
    require(counters.accesses == counters_before_reject.accesses &&
                counters.hits == counters_before_reject.hits &&
                counters.misses == counters_before_reject.misses,
            "rejected prepared commit must not change demand counters");
}

void test_prepared_lookup_transaction_restore() {
    fastsim::SetAssociativeCache cache(cache_config(2, 2));
    fastsim::CacheCounters counters;
    cache.complete_fill(0, false, counters);
    cache.complete_fill(1, false, counters);
    const auto prepared = cache.prepare_lookup(0);

    auto transaction = cache.begin_transaction();
    require(cache.probe(1, false, counters, &transaction).hit,
            "transactional hit must mutate replacement state");
    require(!cache.can_commit(prepared),
            "transactional mutation must invalidate earlier proposal");
    cache.restore(transaction);
    require(cache.can_commit(prepared),
            "restore must restore the set mutation generation");
    const auto committed = cache.commit_probe(prepared, true, counters);
    require(committed.has_value() && committed->hit,
            "restored proposal must commit against restored state");
    bool dirty = false;
    cache.invalidate(0, &dirty);
    require(dirty, "prepared write commit must apply hit dirty state");
}

void test_private_prepared_lookup_commits_atomically() {
    fastsim::PrivateHierarchy hierarchy(cache_config(1, 1),
                                        cache_config(2, 1));
    fastsim::CoreCounters counters;
    hierarchy.complete_fill(0, false, counters);
    const auto prepared = hierarchy.prepare_probe(0);
    require(!prepared.has_l2,
            "private L1 hit must not observe an unused L2 set");
    const auto accesses_before = counters.l1d.accesses;
    const auto committed = hierarchy.commit_probe(
        prepared, false, counters);
    require(committed.has_value() &&
                committed->level == fastsim::HitLevel::kL1 &&
                counters.l1d.accesses == accesses_before + 1,
            "private prepared hit must account exactly once at commit");

    const auto stale = hierarchy.prepare_probe(0);
    hierarchy.complete_fill(1, false, counters);
    const auto l1_before_reject = counters.l1d.accesses;
    const auto l2_before_reject = counters.l2.accesses;
    require(!hierarchy.commit_probe(stale, false, counters).has_value(),
            "private commit must reject a changed L1 set before mutation");
    require(counters.l1d.accesses == l1_before_reject &&
                counters.l2.accesses == l2_before_reject,
            "rejected private commit must be counter-atomic");
}

}  // namespace

void test_cache_probe_fill() {
    test_probe_visibility_and_completion_victim();
    test_probe_dirty_and_duplicate_completion();
    test_indexed_probe_and_fill();
    test_private_visibility_inclusion_and_restore();
    test_private_same_dirty_victim_and_l2_hit_restore();
    test_prepared_lookup_is_side_effect_free_and_guarded();
    test_prepared_lookup_transaction_restore();
    test_private_prepared_lookup_commits_atomically();
}
