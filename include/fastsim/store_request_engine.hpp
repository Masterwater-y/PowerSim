#pragma once

#include "fastsim/pending_resources.hpp"
#include <deque>
#include <functional>
#include <optional>
#include <vector>

namespace fastsim {

// A reference lifecycle coordinator, NOT a cache backend or an O3 core.
// The backend supplies events that have happened at now(); no predicted future
// callback may be passed as a completed permission or write response.
struct StoreRequestProfile {
    std::uint32_t core = 0;
    std::size_t sq_entries = 114;
    std::size_t store_ports = 200; // model interface limit, not physical SPR ports
    std::size_t optional_queue_entries = 0; // zero: no configured queue bound
    std::size_t tbe_entries = 256;
    std::uint64_t commit_to_iew_delay = 1;
    std::uint64_t optional_queue_latency = 1;
    std::uint64_t mandatory_queue_latency = 1;
};

enum class StoreDescriptorKind { Ordinary, Atomic, Split, NoAccess };
struct StoreDescriptor {
    PendingOwner owner;
    std::uint64_t line;
    bool prepare_eligible = true;
    StoreDescriptorKind kind = StoreDescriptorKind::Ordinary;
};

// Separate from architectural store identity. IDs are never reused in this
// engine, and the engine is non-copyable so rollback cannot reuse a generation.
struct StoreRequestId {
    std::uint32_t core;
    std::uint64_t generation;
    friend bool operator==(StoreRequestId a, StoreRequestId b) {
        return a.core==b.core && a.generation==b.generation;
    }
    friend bool operator!=(StoreRequestId a, StoreRequestId b) { return !(a==b); }
};

enum class StoreEngineActionKind {
    LocalComplete, DemandQueued, DemandAcquire, DemandHit, DemandTakeover, PrepareAcquire, PrepareSkipped,
    ProtocolRetry
};
struct StoreEngineAction {
    StoreEngineActionKind kind;
    PendingOwner store;
    StoreRequestId request;
    std::optional<StoreRequestId> transaction;
    std::uint64_t line;
};
enum class StoreEngineEventKind {
    LocalCompleted, SQAllocated, AddressReady, Retired, CanWB, PrepareEnqueued,
    PrepareStarted, PrepareSkipped, PermissionReturned, PermissionStale,
    DemandAccepted, DemandStarted, DemandTakeover, ProtocolRetry, WriteResponse, SQReleased,
    Invalidated, PermissionObserved
};
struct StoreEngineEvent {
    StoreEngineEventKind kind;
    std::uint64_t cycle;
    std::optional<PendingOwner> store; // coherence events have no single SQ owner
    std::uint64_t line;
    std::optional<StoreRequestId> request;
    std::optional<StoreRequestId> transaction;
};
struct StoreEngineCounters {
    std::uint64_t sq_allocated=0, sq_released=0, local_completed=0;
    std::uint64_t prepares_enqueued=0, prepares_dequeued=0, prepares_skipped=0;
    std::uint64_t permission_started=0, permission_returned=0, stale_returns=0;
    std::uint64_t demands_accepted=0, demands_started=0, takeovers=0, protocol_retries=0;
};

class StoreRequestEngine {
public:
    // Observational only: getters are allowed, mutation reentry is rejected.
    // A throwing sink poisons further mutation; the caller must abort this run.
    // AdmissionGate is observational and shares the sink non-reentry contract.
    using AdmissionGate=std::function<bool(std::uint64_t)>;
    using EventSink=std::function<void(const StoreEngineEvent&)>;
    explicit StoreRequestEngine(StoreRequestProfile profile, EventSink sink={});
    StoreRequestEngine(const StoreRequestEngine&)=delete;
    StoreRequestEngine& operator=(const StoreRequestEngine&)=delete;

    void advance_to(std::uint64_t cycle);
    std::uint64_t now() const { return now_; }
    bool allocate(StoreDescriptor description);
    void address_ready(PendingOwner owner);
    void retire(PendingOwner owner);
    void can_writeback(PendingOwner owner);
    bool enqueue_prepare(PendingOwner owner);
    std::optional<StoreEngineAction> start_prepare(const AdmissionGate& gate={});
    std::optional<StoreEngineAction> send_head();
    std::optional<StoreEngineAction> start_head(const AdmissionGate& gate={});
    void permission_response(StoreRequestId transaction);
    void write_response(StoreRequestId request);

    // Backend hooks for real cache events. These do not implement cache
    // replacement/coherence themselves. Only tracked lines are retained.
    void observe_permission(std::uint64_t line);
    void invalidate(std::uint64_t line);
    std::optional<StoreEngineAction> retry_head(const AdmissionGate& gate={});

    std::size_t sq_size() const { return stores_.size(); }
    std::size_t optional_queue_size() const { return optional_.size(); }
    std::size_t transactions_inflight() const { return transactions_.size(); }
    std::size_t tracked_lines() const { return lines_.size(); }
    const StoreEngineCounters& counters() const { return counters_; }

private:
    struct Store {
        StoreDescriptor description;
        bool address=false, can_wb=false, prepare_issued=false;
        std::optional<std::uint64_t> retirement;
    };
    struct Optional { StoreDescriptor description; std::uint64_t ready; StoreRequestId id; };
    struct Line {
        std::uint64_t line, version=0;
        bool valid=false;
        std::optional<StoreRequestId> pending;
    };
    struct Transaction {
        StoreRequestId id;
        StoreDescriptor description;
        std::uint64_t version;
    };
    struct Demand {
        StoreRequestId id;
        std::optional<StoreRequestId> waiting;
        std::uint64_t ready;
        bool started=false;
    };
    StoreRequestProfile profile_;
    EventSink sink_;
    bool in_sink_=false, sink_failed_=false;
    StoreEngineCounters counters_;
    std::uint64_t now_=0, generation_=0;
    std::size_t ports_used_=0;
    bool last_response_local_=false;
    std::optional<std::uint64_t> last_sequence_, last_write_response_;
    std::deque<Store> stores_;
    std::deque<Optional> optional_;
    std::vector<Line> lines_;
    std::vector<Transaction> transactions_;
    std::optional<Demand> demand_;
    Store& store(PendingOwner owner);
    Line& line(std::uint64_t address);
    bool ready(const Store& entry) const;
    void prune_line(std::uint64_t address);
    StoreRequestId next_id();
    void require_ids(std::uint64_t count) const;
    StoreRequestId start_transaction(const StoreDescriptor& description);
    void check_mutation() const;
    bool admitted(const AdmissionGate& gate,std::uint64_t line);
    void emit_event(const StoreEngineEvent& event);
    void emit(StoreEngineEventKind kind, const StoreDescriptor& description,
              std::optional<StoreRequestId> request=std::nullopt,
              std::optional<StoreRequestId> transaction=std::nullopt);
    static std::uint64_t add(std::uint64_t a,std::uint64_t b);
};
} // namespace fastsim
