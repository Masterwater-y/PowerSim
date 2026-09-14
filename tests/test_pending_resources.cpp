#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>

#include "fastsim/pending_resources.hpp"

namespace {
using fastsim::PendingOwner;
using fastsim::PendingResourcePool;
using fastsim::DetachedStoreGroup;
using fastsim::StoreFragment;

void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
template<class F> void rejects(F action, const char* message) {
    bool rejected = false;
    try { action(); } catch (const std::logic_error&) { rejected = true; }
    catch (const std::overflow_error&) { rejected = true; }
    require(rejected, message);
}
PendingOwner owner(std::uint64_t seq, std::uint32_t frag = 0,
                   std::uint64_t gen = 1, std::uint32_t core = 0) {
    return {core, seq, frag, gen};
}

// Catch treating an unresolved release as zero or a certified known minimum.
void pending_minimum() {
    PendingResourcePool pool(2);
    pool.reserve(owner(1), 3);
    pool.raise_release_lower_bound(owner(1), 7);
    pool.reserve(owner(2), 3);
    pool.resolve(owner(2), 12);
    auto q = pool.query(4);
    require(!q.exact && q.lower_bound == 7 && q.blockers.size() == 1 &&
                q.blockers[0] == owner(1), "pending earlier release uncertifies known minimum");
    pool.raise_release_lower_bound(owner(1), 12);
    q = pool.query(4);
    require(q.exact == 12 && q.lower_bound == 12 && q.blockers.empty(),
            "equal pending lower bound certifies known minimum");
    pool.raise_release_lower_bound(owner(1), 14);
    require(pool.query(4).exact == 12, "later pending floor certifies known minimum");
    rejects([&] { pool.reserve(owner(3), 11); }, "full pool rejects early admission");
    pool.reserve(owner(3), 12);
    require(!pool.query(12).exact, "capacity remains full after one slot reuse");
    rejects([&] { pool.resolve(owner(2), 12); }, "stale callback rejected after reuse");
    require(!pool.query(12).exact, "stale callback cannot free current owner");
}

// Catch whole-pool pending poison, sentinel IDs, mutable snapshots, and unbounded slots.
void free_capacity_and_snapshots() {
    rejects([] { PendingResourcePool pool(0); }, "zero capacity rejected");
    PendingResourcePool pool(2);
    const PendingOwner zero{0, 0, 0, 0};
    pool.reserve(zero, 0);
    rejects([&] { pool.reserve(zero, 0); }, "duplicate must reject even with a free slot");
    require(pool.query(0).exact == 0, "empty slot remains usable beside pending owner zero");
    pool.reserve(owner(0, 0, 0, 1), 0);
    require(pool.capacity() == 2 && pool.query(0).blockers.size() == 2,
            "core identity separates owners at exact capacity");
    rejects([&] { pool.reserve(owner(3), 0); }, "capacity cannot grow");
    rejects([&] { pool.reserve(zero, 0); }, "duplicate owner rejected");
    auto saved = pool;
    pool.resolve(zero, 8);
    require(pool.query(8).exact == 8 && !saved.query(8).exact,
            "snapshot must not observe resolution");
    pool.reserve(owner(4), 8);
    rejects([&] { pool.resolve(zero, 8); }, "reused zero owner callback rejected");
    rejects([&] { pool.raise_release_lower_bound(zero, 9); }, "stale bound cannot modify reused slot");
    pool = saved;
    require(!pool.query(8).exact && pool.query(8).blockers.size() == 2,
            "rollback restores unresolved slots");
    pool.resolve(zero, 8);
    pool.resolve(zero, 8);
    rejects([&] { pool.resolve(zero, 9); }, "resolved release immutable");

    PendingResourcePool fragments(2);
    fragments.reserve(owner(9, 0), 0);
    fragments.reserve(owner(9, 1), 0);
    fragments.raise_release_lower_bound(owner(9, 0), 7);
    fragments.raise_release_lower_bound(owner(9, 1), 9);
    require(fragments.query(0).lower_bound == 7 && fragments.query(0).blockers.size() == 2,
            "all unknown slots preserve individual fragment blockers and minimum bound");
    fragments.resolve(owner(9, 0), 7);
    require(fragments.query(7).exact == 7, "released slot usable while another slot remains unknown");
    fragments.reserve(owner(10), 7);
    require(fragments.query(7).blockers.size() == 2, "repeated queries did not consume free slot");
}

// Catch backwards mutation and accidental UINT64_MAX sentinel/arithmetic.
void resource_clocks() {
    PendingResourcePool pool(1);
    pool.reserve(owner(1), 10);
    rejects([&] { pool.query(9); }, "backwards query rejected");
    rejects([&] { pool.reserve(owner(2), 9); }, "backwards reservation rejected");
    rejects([&] { pool.resolve(owner(1), 9); }, "response before admission rejected");
    pool.raise_release_lower_bound(owner(1), 15);
    rejects([&] { pool.raise_release_lower_bound(owner(1), 14); }, "backwards floor rejected");
    rejects([&] { pool.resolve(owner(1), 14); }, "response before floor rejected");
    rejects([&] { pool.resolve(owner(1, 0, 2), 15); }, "wrong generation rejected");
    require(pool.query(10).lower_bound == 15, "rejections leave lower bound intact");
    const auto max = std::numeric_limits<std::uint64_t>::max();
    pool.resolve(owner(1), max);
    rejects([&] { pool.raise_release_lower_bound(owner(1), 16); },
            "resolved owner cannot acquire a new pending bound");
    require(pool.query(10).exact == max, "MAX is a legitimate exact time");
    pool.reserve(owner(2), max);
    require(!pool.query(max).exact, "pending at MAX remains unknown");
    pool.resolve(owner(2), max);
    require(pool.query(max).exact == max, "exact at MAX does not overflow");
}

// Catch first-fragment SQ release and false response dependencies inside a group.
void detached_split_store() {
    const auto a = owner(5, 0), b = owner(5, 1);
    DetachedStoreGroup group(0, 5, {{a, 5, 7}, {b, 6, 20}});
    require(!group.send_floor(a) && !group.release(), "unretired store cannot send or release");
    rejects([&] { group.admit(a, 20); }, "missing retirement blocks admission");
    group.retire(10);
    require(group.retirement() == 10 && !group.release(), "retired store retains SQ pending response");
    require(group.send_floor(a) == 12, "retirement-to-send literal plus two");
    require(!group.send_floor(b), "prior fragment must publish admission first");
    rejects([&] { group.admit(a, 11); }, "retire plus one is too early");
    group.admit(a, 24); // External capacity moves the exact publication.
    require(group.admission(a) == 24 && !group.admission(b), "admitted flags track fragments independently");
    require(group.send_floor(b) == 24, "prior admission dominates availability, without response");
    group.admit(b, 24);
    group.respond(b, 29);
    require(group.response(b) == 29 && !group.response(a), "responses track fragments independently");
    require(!group.release(), "one split response cannot release group SQ");
    auto snapshot = group;
    group.respond(a, 30);
    require(group.release() == 30 && !snapshot.release(), "group releases at max response; copy independent");
    snapshot.respond(a, 35);
    require(snapshot.release() == 35 && group.release() == 30, "snapshot writes cannot mutate original");
    group.respond(a, 30);
    group.admit(a, 24);
    group.retire(10);
    rejects([&] { group.respond(a, 31); }, "published response immutable");
    rejects([&] { group.admit(a, 25); }, "published admission immutable");
    rejects([&] { group.retire(11); }, "retirement immutable");
    rejects([&] { group.respond(owner(5, 0, 2), 30); }, "stale fragment generation rejected");
    rejects([&] { group.admit(owner(5, 0, 2), 24); }, "stale fragment admission rejected");
    require(group.release() == 30, "invalid installs preserve completed group");
}

// Catch conflation of predecessor response with retirement and missing exact +1 edge.
void tso_and_fragment_floors() {
    const auto pred = owner(4), a = owner(5), b = owner(5, 1);
    DetachedStoreGroup group(0, 5, {{a, 4, 7}, {b, 6, 22}}, pred);
    group.retire(10);
    require(group.retirement() == 10 && !group.send_floor(a),
            "pending predecessor blocks sends but not retirement");
    rejects([&] { group.respond(a, 20); }, "response without admission rejected");
    rejects([&] { group.admit(a, 20); }, "pending predecessor blocks publication");
    rejects([&] { group.resolve_predecessor(owner(4, 0, 2), 17); }, "stale predecessor rejected");
    group.resolve_predecessor(pred, 17);
    group.resolve_predecessor(pred, 17);
    require(group.send_floor(a) == 18, "predecessor exact plus one");
    rejects([&] { group.admit(a, 17); }, "predecessor response same cycle is too early");
    group.admit(a, 18);
    require(group.send_floor(b) == 22, "own availability dominates prior admission");
    group.admit(b, 22);
    rejects([&] { group.respond(a, 17); }, "response cannot precede admission");
    group.respond(a, 50);
    group.respond(b, 24);
    require(group.release() == 50, "fragment response order independent of admission order");
    rejects([&] { group.resolve_predecessor(pred, 19); }, "predecessor exact immutable");
}

void malformed_and_overflow() {
    const auto max = std::numeric_limits<std::uint64_t>::max();
    const auto a = owner(5), b = owner(5, 1);
    rejects([] { DetachedStoreGroup group(0, 5, {}); }, "empty group rejected");
    rejects([&] { DetachedStoreGroup group(0, 5, {{a, 0, 0}, {a, 0, 0}}); }, "duplicate fragment rejected");
    rejects([&] { DetachedStoreGroup group(1, 5, {{a, 0, 0}}); }, "wrong fragment core rejected");
    rejects([&] { DetachedStoreGroup group(0, 6, {{a, 0, 0}}); }, "wrong fragment sequence rejected");
    rejects([&] { DetachedStoreGroup group(0, 5, {{a, 0, 0}}, owner(5)); },
            "predecessor sequence must be earlier");
    rejects([&] { DetachedStoreGroup group(0, 5, {{a, 0, 0}}, owner(4, 0, 1, 1)); },
            "predecessor must belong to same core");
    DetachedStoreGroup group(0, 5, {{a, 6, 7}, {b, 8, 9}});
    rejects([&] { group.retire(7); }, "retirement before AGU completion rejected");
    rejects([&] { group.retire(max - 1); }, "retire plus two overflow rejected before mutation");
    require(!group.retirement(), "overflow cannot publish retirement");
    group.retire(max - 2);
    rejects([&] { group.resolve_predecessor(owner(4), 0); }, "absent predecessor callback rejected");
    require(group.send_floor(a) == max, "retire plus two upper literal boundary");
    group.admit(a, max);
    group.admit(b, max);
    group.respond(a, max);
    group.respond(b, max);
    require(group.release() == max, "MAX response remains valid");
    DetachedStoreGroup next(0, 6, {{owner(6), 0, 0}}, a);
    next.retire(0);
    rejects([&] { next.resolve_predecessor(a, max); }, "predecessor plus one overflow rejected");
    require(!next.send_floor(owner(6)), "overflow preserves pending predecessor");
    next.resolve_predecessor(a, max - 1);
    require(next.send_floor(owner(6)) == max, "predecessor plus one upper literal boundary");
}
} // namespace

void test_pending_resources() {
    pending_minimum();
    free_capacity_and_snapshots();
    resource_clocks();
    detached_split_store();
    tso_and_fragment_floors();
    malformed_and_overflow();
}
#ifdef FASTSIM_PENDING_RESOURCES_STANDALONE
int main() {
    try {
        test_pending_resources();
        std::cout << "pending resource/store owner tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
#endif
