#pragma once
#include <cstdint>
#include <cstddef>
#include <memory>
#include <functional>
#include <optional>
#include <vector>
#include "fastsim/spr_transaction_id.hpp"
namespace fastsim {
enum class BoundedUopKind { Alu, Load, Store, LocalStore, Fence, Atomic };
struct BoundedUop {
    std::uint64_t sequence=0, dispatch=0, issue=0, completion=0, retire=0;
    BoundedUopKind kind=BoundedUopKind::Alu;
    std::vector<std::size_t> dependencies;
    bool frontend_barrier=false;
};
struct BoundedCoreProfile {
    std::size_t rob_entries=192, iq_entries=64, lq_entries=32, sq_entries=114;
    std::uint32_t dispatch_width=8, issue_width=8, commit_width=8;
    std::uint64_t commit_to_iew_delay=1;
};
struct BoundedFrame { std::uint64_t dispatch=0, issue=0, completion=0, retire=0, sq_release=0; };
enum class BoundedCursorEventKind { Request, StoreCanWB, Retired };
struct BoundedCursorEvent { BoundedCursorEventKind kind;std::size_t uop;std::uint64_t cycle; };
struct BoundedCursorCounters {
    std::uint64_t node_steps=0, dispatched=0, issued=0, completed=0, retired=0, sq_released=0;
    std::size_t max_live_rob=0, max_live_iq=0, max_live_lq=0, max_live_sq=0;
};
// A bounded response-only prototype. No cache state, oracle input or target
// cycle polling. Future completion timestamps are queued, never applied early.
class BoundedResponseCursor {
public:
    BoundedResponseCursor(BoundedCoreProfile,std::vector<BoundedUop>);
    ~BoundedResponseCursor();
    BoundedResponseCursor(const BoundedResponseCursor&)=delete;
    BoundedResponseCursor& operator=(const BoundedResponseCursor&)=delete;
    std::optional<std::uint64_t> next_cycle()const;
    std::optional<BoundedCursorEvent> step();
    void complete_at(std::size_t uop,std::uint64_t cycle);
    void release_sq_at(std::size_t uop,std::uint64_t cycle);
    bool finished()const;
    const std::vector<BoundedFrame>& frames()const;
    const BoundedCursorCounters& counters()const;
private:
    struct Impl;std::unique_ptr<Impl> p_;
};
enum class BoundedServicePriority { Mandatory, Load, Optional };
struct BoundedServiceRequest {
    std::uint64_t owner=0, arrival=0, resource_mask=1;
    BoundedServicePriority priority=BoundedServicePriority::Load;
};
struct BoundedServiceReply {std::uint64_t resource_release=0, completion=0;};
struct BoundedServiceCompletion {std::uint64_t owner=0, admission=0, completion=0;};
struct BoundedServiceCounters {
    std::uint64_t submitted=0, granted=0, completed=0, events=0;
    std::vector<std::size_t> live,max_live;
};
// One physical-resource stage per request. Multiple resource lifetimes must
// be explicit staged admissions, not a speculative multi-resource reservation.
class BoundedServiceCalendar {
public:
    using Service=std::function<BoundedServiceReply(const BoundedServiceRequest&,std::uint64_t)>;
    BoundedServiceCalendar(std::vector<std::size_t> capacities,Service);
    ~BoundedServiceCalendar();
    void submit(BoundedServiceRequest);
    std::optional<std::uint64_t> next_cycle()const;
    std::optional<BoundedServiceCompletion> step();
    const BoundedServiceCounters& counters()const;
private:
    struct Impl;std::unique_ptr<Impl> p_;
};

struct BoundedStoreProfile {
    std::size_t sq_entries=114,tbe_entries=256,optional_entries=0;
    std::uint32_t cpu_ports=200;
    std::uint64_t commit_delay=1,mandatory_queue=1,optional_queue=1;
};
// Cycles begin at protocol service, after the CPU queue edge. No cache labels
// or oracle times are accepted. permission_ready is a frozen model fact.
struct BoundedStoreShape {
    std::uint64_t line=0,acquire_cycles=0,write_cycles=0;
    bool permission_ready=false,local=false,lease_valid=true;
};
struct BoundedStoreCompletion {std::uint64_t owner=0,cycle=0;bool local=false;};
struct BoundedStoreCounters {
    std::uint64_t events=0,retired=0,writes=0,local_completed=0,acquires=0,prepares=0,takeovers=0,superseded=0,reacquires=0;
    std::size_t max_live_tbe=0,max_live_sq=0;
};
class BoundedStoreTimingPlanner {
public:
    explicit BoundedStoreTimingPlanner(BoundedStoreProfile);
    ~BoundedStoreTimingPlanner();
    void retire(std::uint64_t owner,std::uint64_t at,BoundedStoreShape);
    std::optional<std::uint64_t> next_cycle()const;
    std::optional<BoundedStoreCompletion> step();
    const BoundedStoreCounters& counters()const;
private:
    struct Impl;std::unique_ptr<Impl> p_;
};

enum class BoundedStoreRole { OriginalDemand, Takeover, Publish, Reacquire };
struct BoundedNonbindingStoreInput {
    std::uint64_t retire=0,prior_response=0,scan_ready=0,service_cycles=1,publish_cycles=1,commit_delay=1;
    bool prior_local=false,lease_valid=true;
};
struct BoundedNonbindingStoreResult {
    std::uint64_t eligible=0,prepare=0,permission=0,send=0,response=0;
    BoundedStoreRole role=BoundedStoreRole::OriginalDemand;
};
BoundedNonbindingStoreResult estimate_nonbinding_store(const BoundedNonbindingStoreInput&);

// Standalone resource summary, not yet wired into Simulator or cache visibility.
// Transaction-copyable state; all times use one model clock and callers may
// advance only at a certified global watermark.
struct BoundedStoreIdentity {
    std::uint32_t core=0;
    std::uint64_t sequence=0;
    std::uint32_t ordinal=0;
};
struct BoundedStoreEdges { std::uint64_t send=0,response=0,release=0,permission=0; };
struct BoundedStoreLifecycleStats {
    std::uint64_t prepares=0,prepare_dropped=0,heads=0,invalidations=0;
};
class BoundedStoreLifecycle {
 public:
    BoundedStoreLifecycle(std::size_t cores,std::size_t prepare_credits,
                          std::size_t demand_credits,std::uint32_t store_ports,
                          std::size_t ordered_lines,std::size_t store_entries=112);
    bool prepare(BoundedStoreIdentity,std::uint64_t line,std::uint64_t issue,std::uint64_t response);
    BoundedStoreEdges demand(std::uint32_t core,std::uint64_t issue,std::uint64_t service);
    BoundedStoreEdges head(BoundedStoreIdentity,std::uint64_t line,std::uint64_t eligible,
                           std::uint64_t service,std::uint64_t publish_service=1,
                           std::uint64_t reclaim_delay=0);
    std::optional<std::uint64_t> generation(BoundedStoreIdentity id)const;
    // Cache/directory publication is supplied by the existing outer
    // transaction, and is permitted only at a certified completion watermark.
    bool publish_prepare(BoundedStoreIdentity id,std::uint64_t generation,
        const std::function<bool(std::uint32_t,std::uint64_t,std::uint64_t)>& publish);
    // Process certified completions in response order. The callback owns the
    // actual cache/directory transaction and may decline a stale completion.
    std::size_t complete_prepares(std::uint64_t watermark,
        const std::function<bool(BoundedStoreIdentity,std::uint64_t,
                                 std::uint64_t,std::uint64_t)>& publish);
    std::optional<std::uint64_t> next_visible(std::uint64_t line,std::uint64_t at)const;
    void invalidate(std::uint64_t line,std::uint64_t at);
    void advance(std::uint64_t watermark);
    const BoundedStoreLifecycleStats& stats()const{return stats_;}
 private:
    struct Hint {BoundedStoreIdentity id;std::uint64_t line,issue,response,generation;
                 std::optional<std::uint64_t> invalidated;bool consumed=false,published=false;};
    struct LineSlot {std::uint64_t line=0,ready=0;};
    struct Core {std::vector<std::uint64_t> demand_ready;std::vector<LineSlot> window;
                 std::uint64_t port_cycle=0,prepare_frontier=0,release_frontier=0;
                 std::uint32_t port_used=0;};
    std::size_t prepare_credits_,store_entries_;std::uint32_t store_ports_;std::uint64_t watermark_=0;
    std::vector<Core> cores_;std::vector<Hint> hints_;
    SprTransactionId ids_;
    BoundedStoreLifecycleStats stats_;
};
}
