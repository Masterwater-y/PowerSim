#include "fastsim/spr_frontend.hpp"
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

using namespace fastsim;
namespace {
void check(bool b, const char *s) { if (!b) throw std::runtime_error(s); }
template<class F> void rejects(F f, const char *s) {
    try { f(); } catch (const std::exception &) { return; }
    throw std::runtime_error(s);
}
SprFrontendMacro macro(std::uint64_t pc = 0x1000, unsigned uops = 1) {
    SprFrontendMacro m; m.pc = pc; m.code_identity = pc + 1;
    m.instruction_bytes = 4; m.uops = uops; return m;
}
std::vector<SprFrontendDelivery> run(SprFrontend &f, SprFrontendMacro m,
        std::uint64_t submit = 0, std::uint64_t bytes = 0) {
    f.begin_macro(m, submit, bytes);
    std::vector<SprFrontendDelivery> out;
    while (f.active()) {
        auto d = f.deliver_next(0); out.push_back(d); f.accepted(d.cycle);
    }
    return out;
}
// Removing source latency or aligned-byte serialization breaks these fixtures.
void cold_bytes_and_width() {
    SprFrontend f;
    auto out = run(f, macro(0x1000, 10));
    check(out[0].cycle == 2 && out[7].cycle == 2 && out[8].cycle == 3,
          "cold response +1 and MITE latency +1, width8");
    check(out.front().macro_begin && out.back().macro_end, "macro boundaries");
    check(f.counters().byte_service_blocks == 1, "one aligned block service");
    SprFrontend crossed;
    auto m = macro(0x101f); m.instruction_bytes = 2;
    check(run(crossed, m)[0].cycle == 3, "two aligned blocks need two cycles");
    SprFrontend delayed;
    check(run(delayed, macro(), 0, 1000000000000ULL)[0].cycle == 1000000000002ULL,
          "jump directly across long cache response wait");
}
// Delaying Rename must retain occupancy and stop source groups atomically.
void pressure_and_ack() {
    SprFrontendConfig c; c.idq_capacity = 4;
    SprFrontend f(c); f.begin_macro(macro(0x1000, 12), 0, 0);
    auto d = f.deliver_next(0);
    check(f.idq_occupancy() == 4, "source group fills IDQ before output");
    rejects([&]{ f.deliver_next(0); }, "unacknowledged delivery must reject");
    rejects([&]{ f.accepted(d.cycle - 1); }, "accept before send must reject");
    f.accepted(100);
    check(f.idq_occupancy() == 3, "sent record retains slot until accepted");
    auto next = f.deliver_next(0);
    check(next.cycle >= 100, "ordered output cannot precede prior acceptance");
    f.accepted(next.cycle);
    while (f.active()) { d = f.deliver_next(0); f.accepted(d.cycle); }
    check(f.counters().idq_high_water_slots == 4 && f.idq_occupancy() == 0,
          "bounded credits drain fully");
}
// Charging fused followers one slot or limiting them by slot width breaks this.
void fusion_and_long_macro() {
    SprFrontendConfig c; c.idq_capacity = 1;
    SprFrontend f(c); auto fused = macro(0x1000, 5); fused.shared_allocation = true;
    auto ds = run(f, fused);
    check(ds[0].slot_cost == 1 && ds[1].slot_cost == 0 && ds[4].slot_cost == 0,
          "one slot per shared allocation group");
    check(ds[0].cycle == ds[3].cycle && ds[4].cycle == ds[0].cycle + 1 && f.counters().allocation_slots == 1,
          "zero-cost followers preserve same-cycle service");
    SprFrontend longf; auto m = macro(0x2000, 200); m.microcode = true;
    auto out = run(longf, m);
    check(out.size() == 200 && out[0].source == SprFrontendSource::Microcode,
          "long microcode expansion has bounded fragments");
    check(out[0].cycle == 2 && out[3].cycle == 2 && out[4].cycle == 3,
          "MS delivers four allocation slots per cycle");
    check(longf.source_for(m) == SprFrontendSource::Microcode, "microcode never DSB");
}
// Caching before final delivery, ignoring identity, or clearing on measurement reset breaks this.
void learning_identity_and_reset() {
    SprFrontend f; auto m = macro();
    f.begin_macro(m, 0, 0);
    check(f.source_for(m) == SprFrontendSource::Mite, "no eager DSB fill");
    auto d = f.deliver_next(0);
    check(f.source_for(m) == SprFrontendSource::Dsb, "learn after completed delivery");
    f.reset_counters();
    check(f.idq_occupancy() == 1, "measurement reset preserves outstanding credit");
    f.accepted(d.cycle);
    check(f.source_for(m) == SprFrontendSource::Dsb, "measurement reset preserves DSB");
    auto changed = m; ++changed.code_identity;
    check(f.source_for(changed) == SprFrontendSource::Mite, "mismatch probe is cold");
    check(f.source_for(m) == SprFrontendSource::Dsb, "probe is read only");
    run(f, changed, 10, 10);
    check(f.source_for(m) == SprFrontendSource::Mite, "code change invalidates stale code");
    auto space = changed; ++space.address_space_id;
    check(f.source_for(space) == SprFrontendSource::Mite, "ASID is part of key");
    f.reset();
    check(f.source_for(changed) == SprFrontendSource::Mite && !f.active(), "full reset cold");
}
// Native functional control placement can forbid DSB without changing source timing inputs.
void functional_dsb_eligibility() {
    SprFrontend f; auto m = macro(); m.dsb_eligible = false;
    run(f,m);
    auto again = run(f,m,10,10);
    check(again[0].source == SprFrontendSource::Mite && f.counters().dsb_fills == 0 &&
          f.counters().dsb_hits == 0, "explicitly ineligible macro must never gain DSB");
    m.dsb_eligible = true; run(f,m,20,20);
    check(f.source_for(m) == SprFrontendSource::Dsb, "eligible complete macro still caches");
    m.dsb_eligible = false;
    check(f.source_for(m) == SprFrontendSource::Mite, "ineligible macro cannot reuse eligible template");
}
// Even uncached long macros must revoke resident bytes on functional code changes.
void uncached_code_change_revokes_bytes() {
    SprFrontend f; auto m = macro(0x1000,9); run(f,m);
    check(f.source_for(m) == SprFrontendSource::Mite, "oversize macro not cached");
    ++m.code_identity;
    auto changed = run(f,m,10,1000);
    check(changed[0].cycle == 1002, "uncached code change waits for new byte response");
}
// Skipping source drain or its configured DSB-to-MITE wait breaks this fixture.
void source_switch_and_batches() {
    SprFrontendConfig c; c.dsb_to_mite_delay = 7;
    SprFrontend f(c); auto m = macro(); run(f, m);
    auto hot = run(f, m, 10, 1000);
    check(hot[0].cycle == 11 && hot[0].source == SprFrontendSource::Dsb,
          "DSB bypasses instruction-byte response");
    auto cold = run(f, macro(0x2000), 11, 11);
    check(cold[0].cycle >= 20 && f.counters().switch_delay_cycles == 7,
          "DSB-to-MITE source-side wait");
    SprFrontend batch;
    for (unsigned i=0; i<6; ++i)
        check(run(batch, macro(0x1000 + i*4))[0].cycle == 2, "six MITE macros per batch");
    check(run(batch, macro(0x1018))[0].cycle == 3, "seventh MITE macro next batch");
}
// Removing cache capacity, whole-line eviction or branch prior breaks these.
void cache_capacity_and_branch_prior() {
    SprFrontendConfig c; c.dsb_sets = 1; c.dsb_ways = 1;
    SprFrontend f(c); auto a = macro(0x1000, 8); auto b = macro(0x2000, 8);
    run(f, a); run(f, b, 10, 10);
    check(f.source_for(a) == SprFrontendSource::Mite && f.source_for(b) == SprFrontendSource::Dsb,
          "one-way set evicts old line");
    SprFrontend branches;
    for (unsigned i=0; i<4; ++i) { auto m = macro(0x1000 + 4*i); m.unconditional_branch = true; run(branches,m); }
    auto first = macro(); first.unconditional_branch = true;
    check(branches.source_for(first) == SprFrontendSource::Mite,
          "four unconditional branch templates reject 32-byte block");
}
// Explicit pseudo and partial facts must preserve observed operations without invented byte traffic.
void pseudo_partial_and_copy() {
    SprFrontend f; auto m = macro(); m.pseudo = true; m.instruction_bytes = 0;
    auto out = run(f,m,0,10000);
    check(out[0].cycle == 1 && f.counters().byte_service_blocks == 0, "pseudo skips byte traffic");
    check(f.source_for(m) == SprFrontendSource::Mite && f.counters().dsb_fills == 0, "pseudo never enters DSB");
    auto partial = macro(0x2000,2); partial.complete = false;
    run(f,partial,10,10);
    check(f.source_for(partial) == SprFrontendSource::Mite, "observed partial macro never DSB");
    auto regular = macro(0x3000); run(f,regular,20,20);
    f.begin_macro(regular,30,30); auto first = f.deliver_next(0);
    SprFrontend snapshot(f);
    SprFrontend assigned; assigned = f;
    assigned.accepted(first.cycle + 200);
    check(!assigned.active() && f.active(), "copy assignment preserves independent active state");
    f.accepted(first.cycle); snapshot.accepted(first.cycle + 100);
    check(f.counters().accepted_uops == snapshot.counters().accepted_uops && !snapshot.active(), "snapshot copies outstanding credit");
    auto a = run(f,regular,31,31); auto b = run(snapshot,regular,31,31);
    check(b[0].cycle >= a[0].cycle + 99, "snapshot schedules diverge independently");
    snapshot.reset();
    check(f.source_for(regular) == SprFrontendSource::Dsb, "snapshot reset independent cache");
}
void invalid_inputs() {
    SprFrontend f; auto m = macro();
    m = macro(); m.instruction_bytes = 0;
    rejects([&]{ f.begin_macro(m,0,0); }, "normal macro must have bytes");
    m = macro(); m.code_identity = 0;
    rejects([&]{ f.begin_macro(m,0,0); }, "missing code identity unsupported");
    m = macro(); m.uops = 0;
    rejects([&]{ f.begin_macro(m,0,0); }, "zero ops unsupported");
    m = macro(); m.shared_allocation = true; m.uops = 65;
    rejects([&]{ f.begin_macro(m,0,0); }, "oversize atomic shared group rejects");
    m = macro(std::numeric_limits<std::uint64_t>::max());
    rejects([&]{ f.begin_macro(m,0,0); }, "instruction address overflow rejects");
    m = macro(); m.instruction_bytes = 16;
    rejects([&]{ f.begin_macro(m,0,0); }, "invalid x86 length unsupported");
    rejects([&]{ f.begin_macro(macro(), std::numeric_limits<std::uint64_t>::max(),0); },
          "cycle overflow rejects");
    SprFrontendConfig c; c.byte_service_bytes = 3;
    rejects([&]{ SprFrontend bad(c); }, "unaligned byte geometry rejects");
}
}
int main() {
    try {
        cold_bytes_and_width(); pressure_and_ack(); fusion_and_long_macro();
        learning_identity_and_reset(); functional_dsb_eligibility(); uncached_code_change_revokes_bytes(); source_switch_and_batches();
        cache_capacity_and_branch_prior(); invalid_inputs(); pseudo_partial_and_copy();
        std::cout << "all SPR frontend tests passed\n"; return 0;
    } catch (const std::exception &e) {
        std::cerr << "SPR frontend test failure: " << e.what() << '\n'; return 1;
    }
}
