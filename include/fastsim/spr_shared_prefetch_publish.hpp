#pragma once
#include "fastsim/cache.hpp"
#include "fastsim/spr_prefetch_publish.hpp"
#include <cstdint>
#include <stdexcept>

namespace fastsim {
enum class SprSharedPrefetchPublication { kNotReady, kRetry, kPermissionLost, kPublished };

// Completion half of an LLC prefetch. At issue, use the existing LLC probe
// (which does not allocate on a miss), reserve independent credit and choose
// a response through the existing shared service. This function must receive
// the real model-selected data response, not an optimistic lower bound.
// `permission()` validates the *current* directory/line generation. `evict`
// must update the directory for clean and dirty victims and submit dirty WB at
// the passed response edge. Caller owns the outer transaction: any exception
// requires restoring LLC, private caches, directory, counters, timing/credit,
// outstanding summary and dirty-writeback state together. This helper does not
// publish private data or claim a target-native PMU event.
template<class Permission, class Evict>
SprSharedPrefetchPublication publish_spr_shared_prefetch(
    SetAssociativeCache& llc, std::uint64_t line, std::uint64_t issue,
    std::uint64_t completion, std::uint64_t now, CacheCounters& prefetch_counters,
    CacheTransaction* transaction, Permission&& permission, Evict&& evict) {
    if (completion<issue)throw std::invalid_argument("prefetch completion precedes issue");
    if (now<completion)return SprSharedPrefetchPublication::kNotReady;
    const auto decision=permission();
    if(decision==SprPrefetchPermission::kUnknown)return SprSharedPrefetchPublication::kRetry;
    if(decision==SprPrefetchPermission::kRevoked)return SprSharedPrefetchPublication::kPermissionLost;
    const auto fill=llc.complete_fill(line,false,prefetch_counters,transaction);
    if(fill.evicted)evict(fill.evicted_line,fill.evicted_dirty,completion);
    return SprSharedPrefetchPublication::kPublished;
}
}
