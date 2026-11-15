#pragma once
#include "fastsim/cache.hpp"
#include "fastsim/spr_prefetch_service.hpp"
#include <stdexcept>

namespace fastsim {
// The outer hierarchy owns the directory generation and checks it at the
// actual completion edge. Unknown means the caller must retain the request.
enum class SprPrefetchPermission { kValid, kRevoked, kUnknown };

// Compose one already-due prefetch with an existing private-cache transaction
// and external shared-directory/dirty-writeback owner. `permission(entry)` and
// `evict(core,line,dirty,completion)` MUST not read or mutate target time from
// native gem5 traces. On any exception the caller must restore the entire
// shared/cache transaction, counters, pending summary and writeback state.
// Call exactly once per pending generation. Prefetch-only counters are separate
// from retired/demand accounting. L2 victims must be reported even if clean so
// the shared owner can remove directory residency.
template<class Permission, class Evict>
SprPrefetchService::Commit publish_spr_prefetch(
    const SprPrefetchService::Entry& entry, std::uint64_t now,
    PrivateHierarchy& cache, CoreCounters& prefetch_counters,
    PrivateTransaction* transaction, Permission&& permission, Evict&& evict) {
    if (!entry.response || *entry.response > now)
        throw std::logic_error("prefetch publication before response");
    const auto decision=permission(entry);
    if (decision==SprPrefetchPermission::kUnknown)
        return SprPrefetchService::Commit::kRetry;
    if (decision==SprPrefetchPermission::kRevoked)
        return SprPrefetchService::Commit::kPermissionLost;
    const auto result=entry.level==SprPrefetchService::Level::kL1
        ? cache.publish_prefetch_fill(entry.line,entry.issue,*entry.response,now,true,
                                      prefetch_counters,transaction)
        : cache.publish_prefetch_fill_l2(entry.line,entry.issue,*entry.response,now,true,
                                         prefetch_counters.l2,transaction);
    if (result.status!=PrefetchPublicationStatus::kPublished)
        throw std::logic_error("valid ready prefetch did not publish");
    if (result.result.l2_evicted)
        evict(entry.core,result.result.l2_evicted_line,
              result.result.l2_evicted_dirty,*entry.response);
    return SprPrefetchService::Commit::kPublished;
}
}
