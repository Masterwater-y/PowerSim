#include "fastsim/spr_store_buffer.hpp"
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace fastsim::spr;
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)

static StoreToken add(StoreBuffer& b, std::uint64_t addr, std::uint64_t cycle = 1,
                      std::uint64_t flags = 0, std::uint64_t context = 1,
                      std::uint64_t requestor = 2, std::size_t bytes = 4) {
    auto t = b.allocate({addr, context, requestor, flags, bytes, StoreKind::Ordinary}, cycle);
    CHECK(t.has_value());
    b.markAddressReady(*t, addr, cycle);
    b.markDataReady(*t, cycle);
    b.publish(*t, cycle);
    b.commit(*t, cycle);
    b.allowWriteback(*t, cycle);
    return *t;
}
static BufferWriteToken send(StoreBuffer& b, std::uint64_t cycle) {
    auto c = b.peekWrite(cycle); CHECK(c.has_value());
    CHECK(b.trySendWrite(*c, cycle, [](const WriteCandidate&) { return true; }));
    return c->token;
}
static void unsupported(const std::function<void()>& f) {
    bool threw = false; try { f(); } catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw);
}
// Catches incorrectly returning shared capacity when SQ transfers to the buffer.
static void shared_credit_and_readiness() {
    BufferProfile p; p.shared_store_entries = 2;
    StoreBuffer b(p);
    auto t = b.allocate({1, 1, 2, 0, 4, StoreKind::Ordinary}, 1); CHECK(t);
    b.markAddressReady(*t, 64, 1); b.publish(*t, 1); b.commit(*t, 1);
    b.allowWriteback(*t, 1);
    CHECK(b.offload(1) == 0); // data is a separate prerequisite
    b.markDataReady(*t, 1); CHECK(b.offload(1) == 1);
    CHECK(b.used() == 1 && b.sqSize() == 0 && b.committedSize() == 1);
    add(b, 128); CHECK(b.used() == 2);
    CHECK(!b.allocate({3, 1, 2, 0, 4, StoreKind::Ordinary}, 1));
    CHECK(b.used() == 2 && b.sqSize() == 1 && b.committedSize() == 1);
}
// Catches arbitrary same-line merge, masked-byte overwrite, or changed flag matching.
static void tail_merge_and_last_writer() {
    StoreBuffer b;
    auto a = add(b, 64, 1, 9, 1, 2, 8);
    auto c = add(b, 68, 1, 9, 1, 2, 8);
    CHECK(b.offload(1) == 2);
    CHECK(b.used() == 1 && b.counters().merged == 1);
    auto w = b.peekWrite(1); CHECK(w);
    CHECK(w->mask[0] && w->mask[11] && !w->mask[12]);
    CHECK(w->last_writer[0] == a.id && w->last_writer[3] == a.id);
    CHECK(w->last_writer[4] == c.id && w->last_writer[11] == c.id);
    add(b, 72, 1, 10); add(b, 72, 1, 10, 3);
    CHECK(b.offload(1) == 2 && b.committedSize() == 3);
    add(b, 72, 2, 10, 3, 4); CHECK(b.offload(2) == 1);
    CHECK(b.committedSize() == 4);
    CHECK(b.counters().architectural_combined == 5);
}
// Catches admitting a fifth distinct line or rejecting an already in-flight line.
static void line_window_and_ports() {
    StoreBuffer b;
    for (auto addr : {64, 128, 192, 256}) add(b, addr);
    CHECK(b.offload(1) == 4);
    add(b, 64); add(b, 320);
    CHECK(b.offload(1) == 0); // one model grant per cycle
    CHECK(b.offload(2) == 2);
    auto first = send(b, 2); send(b, 2); CHECK(!b.peekWrite(2));
    send(b, 3); send(b, 3); CHECK(b.inflightLines() == 4);
    send(b, 4); CHECK(b.inflightLines() == 4);
    CHECK(!b.peekWrite(4));
    CHECK(b.completeWrite(first, 5));
    CHECK(!b.peekWrite(5)); // another same-line reference remains
}
// Catches freeing a completed non-head buffer entry or losing cross-chunk identity.
static void callback_prefix_and_generation() {
    StoreBuffer b; add(b, 64); add(b, 128); CHECK(b.offload(1) == 2);
    auto first = send(b, 2), second = send(b, 2);
    CHECK(b.completeWrite(second, 1000));
    CHECK(b.used() == 2 && b.inflightLines() == 1);
    CHECK(!b.completeWrite(second, 1001));
    b.invalidateLine(64, 1001);
    CHECK(b.completeWrite(first, 1002));
    CHECK(b.used() == 0 && b.counters().buffer_credits_released == 2);
    b.advanceGeneration(); add(b, 192, 1003); b.offload(1003);
    auto third = send(b, 1003);
    CHECK(!b.completeWrite(first, 1004));
    CHECK(!b.completeWrite({third.id, first.generation}, 1004));
    CHECK(b.used() == 1 && b.completeWrite(third, 1004));
}
// Catches speculative mutation by peek/rejection and stale candidate use after merge.
static void admission_refusal_and_stale_candidate() {
    StoreBuffer b; add(b, 64); b.offload(1);
    auto old = b.peekWrite(1); CHECK(old);
    CHECK(!b.trySendWrite(*old, 1, [](const auto&) { return false; }));
    CHECK(b.inflightLines() == 0 && b.counters().writes_accepted == 0);
    add(b, 68); b.offload(1);
    bool called = false;
    CHECK(!b.trySendWrite(*old, 1, [&](const auto&) { called = true; return true; }));
    CHECK(!called);
    auto fresh = b.peekWrite(1); CHECK(fresh);
    CHECK(b.trySendWrite(*fresh, 1, [](const auto&) { return true; }));
    CHECK(b.inflightLines() == 1);
    add(b, 72); b.offload(2); CHECK(b.committedSize() == 2); // issued tail cannot merge
}
// Catches hint retirement/data gating, cap becoming demand blockage, or leaked hint credit.
static void early_prepare_drop_and_termination() {
    BufferProfile p; p.prepare_outstanding = 1;
    std::vector<StoreEvent> events;
    StoreBuffer b(p, [&](const StoreEvent& e) { events.push_back(e); });
    auto t = b.allocate({1,1,2,0,4, StoreKind::Ordinary}, 1); CHECK(t); b.markAddressReady(*t, 64, 1);
    auto c = b.peekPrepare(1); CHECK(c);
    auto result = b.trySendPrepare(*c, 1, [](const auto&, auto) { return true; });
    CHECK(result.outcome == PrepareOutcome::Accepted && b.outstandingPrepares() == 1);
    auto younger = b.allocate({2,1,2,0,4, StoreKind::Ordinary}, 1); CHECK(younger);
    b.markAddressReady(*younger, 128, 1);
    auto c2 = b.peekPrepare(1); CHECK(c2);
    CHECK(b.trySendPrepare(*c2, 1, [](const auto&, auto) { return true; }).outcome == PrepareOutcome::Dropped);
    b.markDataReady(*younger, 2); b.publish(*younger, 2); b.commit(*younger, 2); b.allowWriteback(*younger, 2);
    b.markDataReady(*t, 2); b.publish(*t, 2); b.commit(*t, 2); b.allowWriteback(*t, 2);
    CHECK(b.offload(2) == 2); send(b, 2); send(b, 2);
    CHECK(b.outstandingPrepares() == 1); // demand acceptance is not hint termination
    CHECK(b.completePrepare(result.token, PrepareTerminal::TakenOver, 3));
    CHECK(!b.completePrepare(result.token, PrepareTerminal::Fill, 3));
    CHECK(b.outstandingPrepares() == 0 && b.counters().prepares_dropped == 1);
    CHECK(b.counters().architectural_combined == 2 && b.counters().writes_accepted == 2);
    CHECK(events.back().kind == StoreEventKind::PrepareComplete);
    CHECK(events.back().prepare_terminal == PrepareTerminal::TakenOver);
}
// Catches local NO_ACCESS incorrectly generating Ruby traffic and unsupported coercion.
static void no_access_and_unsupported() {
    StoreBuffer b;
    for (auto kind : {StoreKind::Atomic, StoreKind::Split, StoreKind::Uncacheable,
                      StoreKind::StrictlyOrdered, StoreKind::Unsupported})
        unsupported([&] { b.allocate({1,1,2,0,4,kind}, 1); });
    unsupported([&] { b.allocate({1,1,2,0,0, StoreKind::Ordinary}, 1); });
    CHECK(b.used() == 0);
    auto t = b.allocate({1,1,2,0,0,StoreKind::NoAccess}, 1); CHECK(t);
    b.publish(*t, 1); b.commit(*t, 1); b.allowWriteback(*t, 1);
    CHECK(b.offload(1) == 1 && b.used() == 0);
    CHECK(!b.peekWrite(1) && !b.peekPrepare(1));
    CHECK(b.counters().no_access_complete == 1 && b.counters().writes_created == 0);
    auto s = b.allocate({2,1,2,0,8, StoreKind::Ordinary}, 2); CHECK(s);
    unsupported([&] { b.markAddressReady(*s, 60, 2); });
}

// Catches event consumers observing a transient double charge or premature release.
static void event_ledger_is_conserved() {
    StoreBuffer* owner = nullptr;
    StoreBuffer b({}, [&](const StoreEvent&) {
        if (!owner) return;
        const auto& c = owner->counters();
        CHECK(owner->used() == c.allocated - c.merged - c.buffer_credits_released - c.no_access_complete);
        CHECK(owner->sqSize() == c.allocated - c.architectural_combined - c.no_access_complete);
    });
    owner = &b;
    add(b, 64); add(b, 68); add(b, 128); b.offload(1);
    auto first = send(b, 2), second = send(b, 2);
    CHECK(b.completeWrite(second, 3)); CHECK(b.completeWrite(first, 4));
    CHECK(b.used() == 0);
}

// Native Sequencer consumes capped hints as Issued: CPU send port is still used.
static void dropped_prepare_consumes_port() {
    BufferProfile p; p.prepare_outstanding = 1;
    StoreBuffer b(p);
    for (unsigned i = 1; i <= 3; ++i) {
        auto t = b.allocate({i,1,2,0,4, StoreKind::Ordinary}, 1); CHECK(t);
        b.markAddressReady(*t, i * 64, 1);
    }
    auto first = b.peekPrepare(1); CHECK(first);
    CHECK(b.trySendPrepare(*first, 1, [](const auto&, auto) { return true; }).outcome == PrepareOutcome::Accepted);
    auto second = b.peekPrepare(1); CHECK(second);
    CHECK(b.trySendPrepare(*second, 1, [](const auto&, auto) { return true; }).outcome == PrepareOutcome::Dropped);
    CHECK(!b.peekPrepare(1));
    CHECK(b.peekPrepare(2));
}

// Omitting functional qualification must not silently certify an ordinary store.
static void omitted_qualification_rejected() {
    StoreBuffer b;
    StoreFacts facts{};
    facts.source_id = 1; facts.context = 1; facts.requestor = 2; facts.bytes = 4;
    unsupported([&] { b.allocate(facts, 1); });
    unsupported([&] { b.allocate({2, 1, 2, 0, 4}, 1); });
    CHECK(b.used() == 0 && b.counters().allocated == 0);
    CHECK(!b.peekPrepare(1) && !b.peekWrite(1));
    facts.kind = StoreKind::Ordinary;
    CHECK(b.allocate(facts, 1));
    CHECK(b.used() == 1 && b.counters().allocated == 1);
}

// Catches lost functional flag bits and tampered prepare candidates reaching backend.
static void prepare_preserves_functional_flags() {
    StoreBuffer b;
    constexpr std::uint64_t flags = UINT64_C(0x100000000800);
    auto t = b.allocate({1, 3, 5, flags, 4, StoreKind::Ordinary}, 1); CHECK(t);
    b.markAddressReady(*t, 68, 1);
    auto c = b.peekPrepare(1); CHECK(c);
    CHECK(c->flags == flags && c->context == 3 && c->requestor == 5 && c->line == 64);
    auto altered = *c; altered.flags ^= 1;
    bool called = false;
    CHECK(b.trySendPrepare(altered, 1, [&](const auto&, auto) {
        called = true; return true;
    }).outcome == PrepareOutcome::Stale);
    CHECK(!called && b.outstandingPrepares() == 0);
    auto result = b.trySendPrepare(*c, 1, [&](const PrepareCandidate& delivered, auto) {
        CHECK(delivered.flags == flags); return true;
    });
    CHECK(result.outcome == PrepareOutcome::Accepted && b.outstandingPrepares() == 1);
    CHECK(b.completePrepare(result.token, PrepareTerminal::Fill, 2));
}

// Native zero is unlimited: neither drop all hints nor substitute the default 16.
static void zero_prepare_bound_is_unlimited() {
    BufferProfile p; p.prepare_outstanding = 0;
    StoreBuffer b(p);
    std::vector<PrepareToken> accepted;
    for (unsigned i = 1; i <= 17; ++i) {
        auto t = b.allocate({i, 1, 2, 0, 4, StoreKind::Ordinary}, i); CHECK(t);
        b.markAddressReady(*t, i * 64, i);
        auto candidate = b.peekPrepare(i); CHECK(candidate);
        auto result = b.trySendPrepare(*candidate, i, [](const auto&, auto) { return true; });
        CHECK(result.outcome == PrepareOutcome::Accepted);
        accepted.push_back(result.token);
    }
    CHECK(b.outstandingPrepares() == 17 && b.counters().prepares_dropped == 0);
    for (auto token : accepted) CHECK(b.completePrepare(token, PrepareTerminal::Ignored, 18));
    CHECK(b.outstandingPrepares() == 0 && b.counters().prepares_completed == 17);
}

int main() {
    const std::vector<std::pair<const char*, std::function<void()>>> tests = {
        {"shared_credit_and_readiness", shared_credit_and_readiness},
        {"tail_merge_and_last_writer", tail_merge_and_last_writer},
        {"line_window_and_ports", line_window_and_ports},
        {"callback_prefix_and_generation", callback_prefix_and_generation},
        {"admission_refusal_and_stale_candidate", admission_refusal_and_stale_candidate},
        {"early_prepare_drop_and_termination", early_prepare_drop_and_termination},
        {"no_access_and_unsupported", no_access_and_unsupported},
        {"event_ledger_is_conserved", event_ledger_is_conserved},
        {"dropped_prepare_consumes_port", dropped_prepare_consumes_port},
        {"omitted_qualification_rejected", omitted_qualification_rejected},
        {"prepare_preserves_functional_flags", prepare_preserves_functional_flags},
        {"zero_prepare_bound_is_unlimited", zero_prepare_bound_is_unlimited}};
    unsigned failed = 0;
    for (const auto& test : tests) {
        try { test.second(); std::cout << "PASS " << test.first << '\n'; }
        catch (const std::exception& e) { ++failed; std::cerr << "FAIL " << test.first << ": " << e.what() << '\n'; }
    }
    return failed ? 1 : 0;
}
