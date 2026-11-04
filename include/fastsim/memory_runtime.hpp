#pragma once
#include "fastsim/cache.hpp"
#include "fastsim/pending_resources.hpp"
#include <cstdint>
#include <map>
#include <optional>
#include <queue>
#include <vector>

namespace fastsim::spr {
using Cycle = std::uint64_t;
// Position is a caller-certified target causal phase, including any zero-cycle
// successors. It is not a host insertion order, nor an implicit extra cycle.
struct SafeKey {
    Cycle cycle = 0;
    std::uint32_t position = 0;
    friend bool operator==(SafeKey a, SafeKey b) { return a.cycle==b.cycle && a.position==b.position; }
    friend bool operator!=(SafeKey a, SafeKey b) { return !(a==b); }
    friend bool operator<(SafeKey a, SafeKey b) { return a.cycle<b.cycle || (a.cycle==b.cycle && a.position<b.position); }
    friend bool operator<=(SafeKey a, SafeKey b) { return a<b || a==b; }
};
struct FunctionalEarliestReady { SafeKey value; };
struct ProvisionalSchedule { SafeKey value; };
struct CertifiedReservation { SafeKey value; };
struct SourcePublication {
    SafeKey earliest;
    std::vector<PendingOwner> blockers;
    bool done = false;
    static SourcePublication not_before(SafeKey key) { return {key,{},false}; }
    static SourcePublication blocked_on(std::vector<PendingOwner> tokens, SafeKey successor);
    static SourcePublication finished() { return {{},{},true}; }
};
enum class MemoryEventKind { Completion, CreditRelease, Request, Prefetch, Writeback };
struct MemoryEvent {
    PendingOwner owner;
    SafeKey key;
    MemoryEventKind kind;
    std::uint32_t domain = 0;
};
// All possible event producers (including prefetch/translation/writeback) must
// register. Unpublished sources initially block at zero, never at infinity.
// This coordinator owns ordering only: a caller must not mutate a backend until
// its event is returned. Sources must publish the entire unseen-source bound.
class MemoryRuntime {
public:
    explicit MemoryRuntime(std::uint32_t sources);
    void publish(std::uint32_t source, SourcePublication publication);
    void enqueue(MemoryEvent event);
    std::optional<SafeKey> safe_frontier() const; // empty iff all sources finished
    std::optional<SafeKey> next_event_key() const {
        return events_.empty() ? std::nullopt : std::optional<SafeKey>(events_.top().event.key);
    }
    std::optional<MemoryEvent> pop_ready();
    // Close one target phase as a batch BEFORE target-domain arbitration.
    // Domain-specific RR must see every candidate in this batch for its domain.
    std::vector<MemoryEvent> pop_ready_group();
    std::size_t queued() const { return events_.size(); }
private:
    struct Queued { MemoryEvent event; std::uint64_t order; };
    struct Later { bool operator()(const Queued& a, const Queued& b) const {
        return b.event.key<a.event.key || (a.event.key==b.event.key && a.order>b.order);
    }};
    std::vector<SourcePublication> sources_;
    std::priority_queue<Queued,std::vector<Queued>,Later> events_;
    std::uint64_t order_ = 0;
    std::optional<SafeKey> closed_;
    bool ready() const;
};

struct CacheProposal {
    PendingOwner owner;
    PreparedCacheLookup lookup;
    std::uint64_t line;
    std::uint64_t invalidation_generation;
    bool write;
};
// Real SetAssociativeCache adapter. This is a cache-level lifecycle, NOT a
// replacement for SharedSystem's protocol/DRAM/permission adapter. Pending miss
// owners are retained until actual fill/cancellation; proposals do no training,
// replacement, counting, or allocation. One instance per cache state domain.
class CacheEventAdapter {
public:
    CacheEventAdapter(SetAssociativeCache& cache, CacheCounters& counters)
        : cache_(cache), counters_(counters) {}
    CacheProposal prepare(PendingOwner owner, std::uint64_t line, bool write) const;
    std::optional<CacheAccessResult> admit(const CacheProposal& proposal);
    bool complete(PendingOwner owner);
    void invalidate(std::uint64_t line);
    std::size_t pending() const { return pending_.size(); }
private:
    struct OwnerLess { bool operator()(const PendingOwner& a,const PendingOwner& b) const; };
    struct Fill { std::uint64_t line; bool write, valid; };
    SetAssociativeCache& cache_;
    CacheCounters& counters_;
    std::map<PendingOwner,Fill,OwnerLess> pending_;
    // A conservative proposal epoch invalidates external proposals without
    // retaining line tombstones. Actual fills are cancelled only for that line.
    std::uint64_t invalidation_generation_ = 0;
};
} // namespace fastsim::spr
