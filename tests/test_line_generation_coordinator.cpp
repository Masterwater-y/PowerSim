#include "fastsim/line_generation_coordinator.hpp"
#include "fastsim/cache.hpp"

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using Coordinator = fastsim::LineGenerationCoordinator;
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
struct Observer {
    std::vector<Coordinator::Admission> admissions;
    std::vector<Coordinator::Callback> callbacks;
    std::vector<std::string> order;
    std::uint64_t allocations = 0;
    void advance(Coordinator& coordinator, std::uint64_t cycle,
                 std::uint64_t latency = 10) {
        coordinator.advance_to(cycle,
            [&](const Coordinator::Request& request, std::uint64_t now) {
                ++allocations;
                return Coordinator::Service{now + latency, now + latency,
                                             request.sequence + 1000};
            },
            [&](const Coordinator::Admission& admission) {
                admissions.push_back(admission);
                order.push_back("a" + std::to_string(admission.request.sequence));
            },
            [&](const Coordinator::Callback& callback) {
                callbacks.push_back(callback);
                order.push_back("c" + std::to_string(callback.leader_sequence));
            });
        require(coordinator.invariants_hold(), "coordinator invariants failed");
    }
};
void queued(Coordinator& coordinator, std::uint64_t line,
            std::uint64_t seq, std::uint64_t ready) {
    require(coordinator.submit({line, seq, ready}) ==
                Coordinator::SubmitResult::kQueued, "request was not queued");
}

void test_future_request_hole() {
    Coordinator coordinator(1, 4);
    Observer observer;
    queued(coordinator, 10, 1, 0);   // A: [0, 10)
    queued(coordinator, 20, 2, 468); // B must not reserve the idle hole.
    queued(coordinator, 30, 3, 12);  // C: [12, 22), despite later submission.
    observer.advance(coordinator, 22);
    require(observer.admissions.size() == 2 &&
                observer.admissions[1].request.sequence == 3 &&
                observer.admissions[1].admission_cycle == 12 &&
                coordinator.ready_size() == 1 && coordinator.active_size() == 0,
            "future B must not reserve capacity before C");
    observer.advance(coordinator, 478);
    require(observer.admissions[2].request.sequence == 2 &&
                observer.admissions[2].admission_cycle == 468 &&
                observer.allocations == 3, "B must admit at its real time");
}
void test_follower_and_callback_boundary() {
    Coordinator coordinator(2, 4);
    Observer observer;
    queued(coordinator, 10, 1, 0);
    queued(coordinator, 10, 2, 9);
    queued(coordinator, 10, 3, 10);
    observer.advance(coordinator, 20);
    require(observer.allocations == 2 && observer.callbacks.size() == 2,
            "same-line follower must not allocate service or callback");
    require(observer.admissions[1].follower &&
                observer.admissions[1].response_cycle == 10 &&
                observer.admissions[1].leader_sequence == 1 &&
                observer.admissions[1].generation == observer.admissions[0].generation,
            "follower must inherit leader response and generation");
    require(!observer.admissions[2].follower &&
                observer.admissions[2].generation != observer.admissions[0].generation &&
                observer.order == std::vector<std::string>{"a1", "a2", "c1", "a3", "c3"},
            "callback must precede same-tick admission and start a new generation");
    const auto& callback = observer.callbacks[0];
    require(callback.line == 10 && callback.leader_sequence == 1 &&
                callback.admission_cycle == 0 && callback.callback_cycle == 10 &&
                callback.read_visible_cycle == 10 && callback.fill_token == 1001,
            "callback must preserve leader identity, visibility and fill token");
}
void test_capacity_retry_and_bounds() {
    Coordinator coordinator(1, 3);
    Observer observer;
    queued(coordinator, 10, 1, 0);
    queued(coordinator, 20, 2, 1);
    queued(coordinator, 20, 3, 2);
    require(coordinator.submit({40, 4, 3}) ==
                Coordinator::SubmitResult::kReadyCapacityBlocked,
            "ready queue must apply bounded backpressure");
    observer.advance(coordinator, 9);
    require(observer.allocations == 1 && coordinator.ready_size() == 2 &&
                coordinator.counters().capacity_retries == 2,
            "capacity retries must not allocate service or duplicate ready entries");
    observer.advance(coordinator, 20);
    require(observer.allocations == 2 && observer.admissions[1].admission_cycle == 10 &&
                observer.admissions[2].follower &&
                coordinator.counters().max_ready == 3 &&
                coordinator.counters().max_active == 1 &&
                coordinator.counters().max_callbacks == 1,
            "bounded retry must admit one leader and one follower");
    require(coordinator.counters().submissions == 3 &&
                coordinator.counters().admissions == 3 &&
                coordinator.counters().callbacks == 2,
            "unique submissions, admissions and callbacks must conserve");
}
void test_unsupported_store_atomic() {
    Coordinator coordinator(1, 2);
    Observer observer;
    queued(coordinator, 10, 1, 0);
    observer.advance(coordinator, 0);
    require(coordinator.submit({10, 2, 0, Coordinator::Access::kStore}) ==
                Coordinator::SubmitResult::kUnsupportedAccess &&
                coordinator.submit({10, 3, 0, Coordinator::Access::kAtomic}) ==
                Coordinator::SubmitResult::kUnsupportedAccess,
            "store/atomic must fail closed instead of attaching to read generation");
    require(coordinator.ready_size() == 0 && coordinator.active_size() == 1 &&
                coordinator.counters().unsupported_accesses == 2 &&
                coordinator.ledger().find(10)->read_followers == 0,
            "unsupported requests must leave active read state untouched");
    observer.advance(coordinator, 10);
}
void test_copy_isolation_and_rollback() {
    Coordinator original(1, 3);
    Observer observer;
    queued(original, 10, 1, 0);
    queued(original, 20, 2, 1);
    observer.advance(original, 1);
    auto snapshot = original;
    const auto rollback_point = snapshot;
    Observer copied_observer;
    observer.advance(original, 20);
    require(snapshot.now() == 1 && snapshot.active_size() == 1 &&
                snapshot.ready_size() == 1 && snapshot.counters().callbacks == 0,
            "advancing original must not mutate copied ledger or heaps");
    copied_observer.advance(snapshot, 20);
    require(copied_observer.callbacks.size() == 2 &&
                copied_observer.callbacks[0].fill_token == 1001 &&
                snapshot.counters().admissions == original.counters().admissions,
            "copy must preserve pending callback payload and deterministic replay");
    original = rollback_point;
    require(original.invariants_hold() && original.now() == 1 &&
                original.active_size() == 1 && original.ready_size() == 1,
            "snapshot assignment must restore earlier active and ready state");
    Observer replay_observer;
    replay_observer.advance(original, 20);
    require(replay_observer.order == copied_observer.order &&
                original.counters().callbacks == snapshot.counters().callbacks,
            "rollback must replay exactly the same event ordering and accounting");
}
void test_zero_service_and_monotone_time() {
    Coordinator coordinator(1, 2);
    Observer observer;
    queued(coordinator, 10, 1, 0);
    queued(coordinator, 10, 2, 0);
    observer.advance(coordinator, 0, 0);
    require(observer.order == std::vector<std::string>{"a1", "c1", "a2", "c2"} &&
                coordinator.active_size() == 0 && observer.allocations == 2,
            "zero service callback must drain before next same-tick admission");
    observer.advance(coordinator, 5);
    bool rejected = false;
    try { queued(coordinator, 20, 3, 4); }
    catch (const std::logic_error&) { rejected = true; }
    require(rejected, "past ready cycle must be rejected");
    rejected = false;
    try { observer.advance(coordinator, 4); }
    catch (const std::logic_error&) { rejected = true; }
    require(rejected && coordinator.invariants_hold(),
            "backward advance must be rejected without state mutation");
}

void test_private_hierarchy_callback_and_transaction_replay() {
    fastsim::CacheConfig l1;
    l1.size_bytes = 64;
    fastsim::CacheConfig l2 = l1;
    l2.size_bytes = 128;
    fastsim::PrivateHierarchy cache(l1, l2);
    fastsim::CoreCounters counters;
    Coordinator coordinator(2, 4);
    struct PendingFill {
        std::uint64_t line;
        std::uint64_t sequence;
        std::uint64_t generation = 0;
    };
    std::map<std::uint64_t, PendingFill> pending;
    std::vector<std::string> order;
    auto transaction = cache.begin_transaction();
    const auto advance = [&](std::uint64_t cycle) {
        coordinator.advance_to(cycle,
            [&](const Coordinator::Request& request, std::uint64_t now) {
                const auto probe =
                    cache.probe(request.line, false, counters, &transaction);
                require(probe.level == fastsim::HitLevel::kLlc,
                        "harness leader must miss without installing tags");
                require(!cache.contains(request.line),
                        "leader probe must leave line invisible before callback");
                const auto token = request.sequence + 1000;
                require(pending.emplace(token,
                            PendingFill{request.line, request.sequence}).second,
                        "leader must allocate exactly one unique fill token");
                order.push_back("s" + std::to_string(request.sequence));
                return Coordinator::Service{now + 10, now + 10, token};
            },
            [&](const Coordinator::Admission& admission) {
                if (!admission.follower) {
                    pending.at(admission.request.sequence + 1000).generation =
                        admission.generation;
                } else {
                    require(pending.size() == 1 && counters.l1d.accesses == 1 &&
                                counters.l2.accesses == 1,
                            "follower must reuse service without a cache probe");
                }
                order.push_back("a" + std::to_string(admission.request.sequence));
            },
            [&](const Coordinator::Callback& callback) {
                require(callback.fill_token.has_value(),
                        "miss callback must carry its fill token");
                const auto it = pending.find(*callback.fill_token);
                require(it != pending.end() && it->second.line == callback.line &&
                            it->second.sequence == callback.leader_sequence &&
                            it->second.generation == callback.generation,
                        "callback must validate fill token and generation before fill");
                require(!cache.contains(callback.line),
                        "pending line must remain invisible until its callback");
                cache.complete_fill(callback.line, false, counters, &transaction);
                require(cache.contains(callback.line),
                        "callback completion must make the cache line visible");
                pending.erase(it);
                order.push_back("c" + std::to_string(callback.leader_sequence));
            });
        require(coordinator.invariants_hold(),
                "cache harness coordinator invariants must hold");
    };

    queued(coordinator, 0, 1, 0);
    queued(coordinator, 0, 2, 9);
    queued(coordinator, 1, 3, 12);
    advance(1);
    const auto coordinator_before = coordinator;
    const auto counters_before = counters;
    const auto pending_before = pending;
    const auto order_before = order;
    // Snapshot cache mutations from this point, alongside coordinator and
    // counters. Hook-owned tokens and observable effects also participate.
    transaction = cache.begin_transaction();
    const auto run_suffix = [&]() {
        advance(9);
        require(!cache.contains(0) && counters.l1d.accesses == 1 &&
                    counters.l2.accesses == 1 && pending.size() == 1,
                "same-line follower before callback must not allocate or count");
        advance(10);
        require(cache.contains(0) && !cache.contains(1) && pending.empty(),
                "first callback must publish exactly its completed line");
        advance(22);
        require(cache.contains(0) && cache.contains(1) && !cache.contains(2) &&
                    pending.empty() && coordinator.active_size() == 0 &&
                    counters.l1d.accesses == 2 && counters.l1d.misses == 2 &&
                    counters.l1d.evictions == 1 && counters.l2.accesses == 2 &&
                    counters.l2.misses == 2 && counters.l2.evictions == 0,
                "two leaders and one follower must produce two demand probes");
    };
    run_suffix();
    const auto first_order = order;
    const auto first_counters = counters;
    const auto first_coordinator_counters = coordinator.counters();
    cache.restore(transaction);
    coordinator = coordinator_before;
    counters = counters_before;
    pending = pending_before;
    order = order_before;
    require(!cache.contains(0) && !cache.contains(1) &&
                counters.l1d.accesses == 1 && coordinator.now() == 1,
            "rollback must restore cache, coordinator and demand counters together");
    transaction = cache.begin_transaction();
    run_suffix();
    const auto same_counters = [](const fastsim::CacheCounters& a,
                                  const fastsim::CacheCounters& b) {
        return a.accesses == b.accesses && a.hits == b.hits &&
            a.misses == b.misses && a.evictions == b.evictions &&
            a.writebacks == b.writebacks;
    };
    require(order == first_order &&
                order == std::vector<std::string>{"s1", "a1", "a2", "c1",
                                                  "s3", "a3", "c3"} &&
                same_counters(counters.l1d, first_counters.l1d) &&
                same_counters(counters.l2, first_counters.l2) &&
                coordinator.counters().admissions == first_coordinator_counters.admissions &&
                coordinator.counters().callbacks == first_coordinator_counters.callbacks &&
                coordinator.counters().service_allocations == 2 &&
                coordinator.counters().followers == 1,
            "transaction replay must preserve ordering, cache accounting and lifecycle counts");
}
}  // namespace

void test_line_generation_coordinator() {
    test_future_request_hole();
    test_follower_and_callback_boundary();
    test_capacity_retry_and_bounds();
    test_unsupported_store_atomic();
    test_copy_isolation_and_rollback();
    test_zero_service_and_monotone_time();
    test_private_hierarchy_callback_and_transaction_replay();
}
