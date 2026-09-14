#include "fastsim/mixed_dram.hpp"

#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace fastsim;
void check(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
MixedDramConfig fixture() {
    MixedDramConfig c;
    auto& d = c.dram;
    d.channels = 1; d.ranks_per_channel = 2; d.banks_per_channel = 8;
    d.bank_groups_per_rank = 4; d.row_bytes = 256;
    d.t_cl = d.t_rcd = d.t_rp = 43; d.burst_cycles = 11;
    d.t_ras = 97; d.t_rtp = 23; d.t_rrd = 11; d.t_rrd_l = 15;
    d.t_xaw = 64; d.activation_limit = 4; d.t_ccd_l = 16; d.t_cs = 6;
    d.frontend_latency = d.backend_latency = 31;
    d.write_buffer_size = 4; d.write_low_threshold_percent = 50;
    d.min_writes_per_switch = 1; d.min_reads_per_switch = 1;
    c.t_cwl = c.t_rcd_wr = 43; c.t_ccd_l_wr = 16;
    c.t_rtw = 6; c.t_wtr = c.t_wtr_l = 16; c.t_wr = 46;
    return c;
}
MixedDramRequest req(std::uint64_t sequence, std::uint64_t arrival,
                     std::uint64_t line, bool write = false) {
    return {{0, sequence, 0, write ? DramServiceKind::kWriteback :
                                  DramServiceKind::kRead}, arrival, line};
}
// Rejects the old read-triggered drain and >= low trigger.
void threshold() {
    auto c = fixture();
    MixedDramController three(c, 64), two(c, 64);
    for (unsigned i = 0; i < 3; ++i) three.submit(req(i, 0, i, true));
    for (unsigned i = 0; i < 2; ++i) two.submit(req(i, 0, i, true));
    const auto done = three.advance(1000);
    two.advance(1000);
    check(three.stats().writes_serviced == 3, "read-empty write progress");
    check(done.size() == 3, "all three write identities complete");
    check(done[0].id.sequence == 0 && done[1].id.sequence == 1 &&
          done[2].id.sequence == 2, "write service identity order");
    check(three.stats().pending_writes == 0, "drained write occupancy");
    check(two.stats().writes_serviced == 0, "at-low writes stay pending");
    check(two.stats().pending_writes == 2, "at-low ownership retained");
}

// Real warmup requests establish open rows; no synthetic controller hooks.
MixedDramController warmed(MixedDramConfig c) {
    MixedDramController m(c, 64);
    m.submit(req(100, 0, 0));
    m.submit(req(101, 0, 4));   // rank 0, bank 1
    m.submit(req(102, 0, 16));  // rank 0, bank 4: same BG as bank 0
    m.submit(req(103, 0, 32));  // rank 1, bank 0
    m.advance(900);
    return m;
}
// Catches omitted CWL, extra turnaround stalls and lost older-rank floors.
void direction_timings() {
    struct Case { bool first_write, next_write; unsigned line, command; };
    const Case cases[] = {
        {true, false, 0, 1070}, {true, false, 4, 1070},
        {true, false, 32, 1017}, {true, true, 16, 1016},
        {true, true, 4, 1011}, {true, true, 32, 1017},
        {false, true, 16, 1017}, {false, true, 4, 1017},
        {false, true, 32, 1017}, {false, false, 16, 1016},
        {false, false, 4, 1011}, {false, false, 32, 1017}
    };
    for (const auto& x : cases) {
        auto c = fixture(); c.dram.write_low_threshold_percent = 0;
        auto m = warmed(c);
        m.submit(req(1, 1000, 0, x.first_write));
        m.submit(req(2, 1001, x.line, x.next_write));
        auto out = m.advance(2000);
        check(out.size() == 2 && out[0].command == 1000, "direction first command");
        check(out[1].command == x.command, "literal direction spacing");
    }
    auto c = fixture(); c.dram.write_low_threshold_percent = 0;
    auto m = warmed(c);
    m.submit(req(1, 1000, 0, true));
    m.submit(req(2, 1001, 32));
    m.submit(req(3, 1002, 0));
    auto out = m.advance(2000);
    check(out[1].command == 1017 && out[2].command == 1070,
          "older same-rank WR floor survives intervening rank");
    c.dram.burst_cycles = 10; // inherited production unit remains literal
    m = warmed(c); m.submit(req(1, 1000, 0, true)); m.submit(req(2, 1001, 0));
    out = m.advance(2000);
    check(out[1].command == 1069, "burst ten is not silently converted to eleven");
}
// Catches WR recovery using read-like PRE timing and opposite-queue scans.
void adaptive_and_recovery() {
    for (bool write : {false, true}) {
        auto c = fixture(); c.dram.write_low_threshold_percent = 0;
        auto m = warmed(c);
        m.submit(req(1, 1000, 0, write)); m.submit(req(2, 1000, 64, write));
        auto out = m.advance(2000);
        check(out[0].auto_precharged, "same-direction conflict closes row");
        check(out[0].precharge == (write ? 1100 : 1023), "literal PRE recovery");
        check(out[1].activation == (write ? 1143 : 1066), "literal conflict ACT");
        check(out[1].command == (write ? 1186 : 1109), "literal conflict column");
    }
    // Keep writes below low here so read scheduling is the selected direction.
    auto c = fixture();
    auto m = warmed(c);
    m.submit(req(1, 1000, 0)); m.submit(req(2, 1000, 64));
    m.submit(req(3, 1000, 0, true));
    auto out = m.advance(2000);
    check(out[0].auto_precharged && out[0].precharge == 1023,
          "opposite-direction hit cannot keep selected read row open");
    m = warmed(c);
    m.submit(req(1, 1000, 0)); m.submit(req(2, 1000, 64, true));
    out = m.advance(2000);
    check(!out[0].auto_precharged, "opposite-direction conflict cannot close row");
    c.dram.frfcfs_selection_window = 1;
    m = warmed(c);
    m.submit(req(1, 1000, 0)); m.submit(req(2, 1000, 64)); m.submit(req(3, 1000, 1));
    out = m.advance(2000);
    check(!out[0].auto_precharged, "same-direction hit outside selection window keeps row open");
    c.dram.max_accesses_per_row = 2;
    m = warmed(c);
    m.submit(req(1, 1000, 0)); m.submit(req(2, 1000, 64)); m.submit(req(3, 1000, 1));
    const auto before = m.stats().precharges;
    out = m.advance(1001);
    check(out[0].auto_precharged && out[0].precharge == 1023, "row cap overrides waiting hit");
    check(m.stats().row_cap_precharges == 1, "row cap counted once");
    check(m.stats().precharges == before + 2 && m.stats().adaptive_precharges == 1,
          "one cap PRE and one later adaptive PRE, never duplicate cap PRE");
}
// Catches command-frontier lookahead and discarded state at batch boundaries.
void frontier_and_rollback() {
    auto c = fixture(); c.dram.write_low_threshold_percent = 0;
    auto full = warmed(c), split = full;
    const auto w = req(1, 990, 0, true), b = req(2, 1000, 64), a = req(3, 1050, 0);
    full.submit(w); full.submit(b); full.submit(a);
    auto all = full.advance(2000);
    split.submit(w); split.submit(b);
    auto prefix = split.advance(1050);
    check(prefix.size() == 2 && prefix[1].id == b.id,
          "earlier conflict irrevocably selected before future hit arrives");
    check(prefix[1].selection == 1000 && prefix[1].command == 1176,
          "selection event reserves a command beyond frontier");
    auto snapshot = split;
    split.submit(a); auto suffix = split.advance(2000);
    check(suffix.size() == 1 && suffix[0].selection == 1101 && suffix[0].command == 1316,
          "reserved row state persists into later event");
    prefix.insert(prefix.end(), suffix.begin(), suffix.end());
    check(prefix == all, "whole stream equals split stream exactly");
    check(snapshot.stats().submitted == 6, "snapshot does not share admissions");
    snapshot.submit(a);
    check(snapshot.advance(2000) == suffix, "snapshot replay exact completion equality");
    check(snapshot.stats().reads_serviced == split.stats().reads_serviced,
          "rollback does not publish counters twice");
    auto tied = warmed(c); tied.submit(w); tied.submit(req(3, 1000, 0)); tied.submit(b);
    auto tie_out = tied.advance(2000);
    check(tie_out[1].id.sequence == 3, "already admitted row hit wins paired variant");
}
// Catches freeing capacity on selection rather than response, and early future admission.
void occupancy() {
    auto c = fixture(); c.dram.read_buffer_size = 1;
    MixedDramController m(c, 64);
    m.submit(req(1, 0, 0)); m.submit(req(2, 1, 1)); m.submit(req(3, 500, 2, true));
    auto out = m.advance(90);
    check(out.size() == 1 && out[0].dram_ready == 97 && out[0].response == 159,
          "literal distinct DRAM-ready and requester-return boundaries");
    check(m.stats().read_responses == 1 && m.stats().admitted == 1,
          "inflight read retains capacity; future write not admitted");
    m.advance(97);
    check(m.stats().admitted == 1, "exclusive response frontier retains occupancy");
    out = m.advance(98);
    check(out.size() == 1 && out[0].arrival == 1 && out[0].admission == 97,
          "response releases capacity at exact completion");
    check(m.stats().admission_delayed == 1 && m.stats().admission_delay_cycles == 96,
          "capacity delay counted once");
    check(m.stats().writes_serviced == 0 && m.stats().pending_writes == 0,
          "future writes above frontier stay outside physical queue");
    bool active_rejected = false;
    try { m.submit(req(1, 98, 0)); }
    catch (const std::invalid_argument&) { active_rejected = true; }
    check(active_rejected, "identity remains active after DRAM ready until requester response");
    c.dram.read_buffer_size = 2;
    MixedDramController pending(c, 64);
    pending.submit(req(1, 0, 0)); pending.submit(req(2, 0, 64)); pending.submit(req(3, 0, 128));
    pending.advance(1);
    check(pending.stats().admitted == 2 && pending.stats().waiting_admission == 1,
          "pending reads consume admission capacity before selection");
}
template<class F> void rejects(F f, const char* message) {
    bool threw = false;
    try { f(); } catch (const std::invalid_argument&) { threw = true; }
    check(threw, message);
}
void validation_and_channels() {
    auto c = fixture();
    rejects([&] { MixedDramController x(c, 0); }, "zero line size rejected");
    auto bad = c; bad.dram.channels = 0;
    rejects([&] { MixedDramController x(bad, 64); }, "zero channels rejected");
    bad = c; bad.dram.bank_groups_per_rank = 16;
    rejects([&] { MixedDramController x(bad, 64); }, "invalid bank groups rejected");
    bad = c; bad.dram.row_bytes = 63;
    rejects([&] { MixedDramController x(bad, 64); }, "invalid row geometry rejected");
    MixedDramController x(c, 64); x.submit(req(1, 0, 0));
    rejects([&] { x.submit(req(1, 0, 1)); }, "pending duplicate identity rejected");
    rejects([&] { x.submit(req(2, 0, c.dram.size_bytes / 64)); }, "out of range line rejected");
    x.advance(1);
    rejects([&] { x.submit(req(1, 1, 0)); }, "inflight duplicate identity rejected");
    rejects([&] { x.submit(req(3, 0, 0)); }, "late submission rejected");
    rejects([&] { x.advance(0); }, "backwards frontier rejected");
    check(x.advance(1).empty(), "repeated frontier does not duplicate completions");
    c.dram.channels = 2;
    MixedDramController multi(c, 64);
    multi.submit(req(2, 0, 1)); multi.submit(req(1, 0, 0));
    auto out = multi.advance(200);
    check(out.size() == 2 && out[0].command == 43 && out[1].command == 43,
          "channels issue independently");
    const auto s = multi.stats();
    check(s.reads_serviced + s.writes_serviced + s.pending_reads + s.pending_writes == s.admitted,
          "admitted service accounting conserved");
    check(s.submitted == s.admitted + s.waiting_admission, "submitted ownership conserved");
}
// Catches reset-on-batch and fixed-16 drain loops. Literal event times follow
// WR commands 1000,1016,...,1240; WR16 selects at 1149, WR17 at 1165.
void interrupted_write_turn() {
    auto c = fixture();
    c.dram.write_buffer_size = 128;
    c.dram.min_writes_per_switch = c.dram.min_reads_per_switch = 16;
    for (const unsigned arrival : {1148u, 1150u}) {
        auto full = warmed(c), split = full;
        for (unsigned i = 0; i < 80; ++i) {
            full.submit(req(i + 1, 1000, i % 4, true));
            split.submit(req(i + 1, 1000, i % 4, true));
        }
        const auto read = req(200, arrival, 0);
        full.submit(read);
        const auto all = full.advance(2000);
        const unsigned count = arrival == 1148 ? 16 : 17;
        check(full.stats().writes_serviced == count, "actual read arrival interrupts persistent write turn");
        check(all.size() == count + 1 && all.back().id == read.id,
              "write minimum turn precedes arriving read");
        check(all.back().command == (arrival == 1148 ? 1310 : 1326),
              "literal read command after interrupted write turn");
        auto first = split.advance(arrival);
        split.submit(read);
        auto last = split.advance(2000);
        first.insert(first.end(), last.begin(), last.end());
        check(first == all, "write-turn counters survive arrival-frontier split");
        check(split.stats().pending_writes == 80 - count, "write-turn pending ownership conserved");
    }
    auto m = warmed(c);
    for (unsigned i = 0; i < 65; ++i) m.submit(req(i + 1, 1000, i % 4, true));
    m.advance(2000);
    check(m.stats().writes_serviced == 18 && m.stats().pending_writes == 47,
          "65 writes drain exactly 18 through low plus minimum hysteresis");
    m = warmed(c);
    for (unsigned i = 0; i < 110; ++i) m.submit(req(i + 1, 1000, i % 4, true));
    m.submit(req(200, 1000, 0));
    auto out = m.advance(1001);
    check(!out.empty() && out[0].id.kind == DramServiceKind::kRead,
          "high watermark does not preempt first read");
}
// Build an ongoing write turn naturally, so opposite reads exist at WR8's
// selection event 1011 without introducing a test-only forced direction API.
void write_adaptive_visibility() {
    auto c = fixture(); c.dram.write_buffer_size = 128;
    c.dram.write_low_threshold_percent = 0; c.dram.min_writes_per_switch = 16;
    for (const bool write_conflict : {false, true}) {
        auto m = warmed(c);
        for (unsigned i = 1; i <= 8; ++i) m.submit(req(i, 990, 0, true));
        if (write_conflict) m.submit(req(9, 990, 64, true));
        m.submit(req(200, 1000, write_conflict ? 0 : 64));
        const auto out = m.advance(2000);
        check(out.size() >= 9 && out[7].id.sequence == 8 && out[7].selection == 1011,
              "eighth write selects with opposite read admitted");
        check(out[7].auto_precharged == write_conflict,
              "write adaptive scan ignores opposite-direction hit or conflict");
        if (write_conflict) check(out[7].precharge == 1202, "write adaptive PRE includes data ready plus recovery");
    }
}
// Catches missing independent ACT spacing and rolling activation history.
void activation_calendars_and_policies() {
    auto c = fixture(); c.scheduler = MixedDramScheduler::kFcfs;
    MixedDramController m(c, 64);
    const unsigned lines[] = {0, 4, 16, 20, 8};
    for (unsigned i = 0; i < 5; ++i) m.submit(req(i, 0, lines[i]));
    auto out = m.advance(500);
    const unsigned acts[] = {0, 11, 22, 33, 64};
    const unsigned commands[] = {43, 54, 65, 76, 107};
    for (unsigned i = 0; i < 5; ++i) {
        check(out[i].activation == acts[i], "literal rank activation window");
        check(out[i].command == commands[i], "literal columns following activation window");
    }
    MixedDramController same_bg(c, 64);
    same_bg.submit(req(1, 0, 0)); same_bg.submit(req(2, 0, 16));
    out = same_bg.advance(500);
    check(out[1].activation == 15 && out[1].command == 59, "same-BG activation spacing");
    c.page_policy = "close_adaptive";
    MixedDramController close(c, 64);
    close.submit(req(1, 0, 0)); close.submit(req(2, 0, 1));
    out = close.advance(500);
    check(!out[0].auto_precharged && out[1].auto_precharged,
          "close adaptive keeps pending hit then closes last request");
    c.page_policy = "open";
    auto open = warmed(c);
    open.submit(req(1, 1000, 0)); open.submit(req(2, 1000, 64));
    out = open.advance(2000);
    check(!out[0].auto_precharged && out[1].precharge == 1023,
          "open policy reserves explicit conflict PRE");
}
// Catches overflow after a pending owner was erased and after earlier
// completions had accumulated in advance's local output vector.
void unsupported_horizon_is_rejected_before_mutation() {
    const auto max_cycle = std::numeric_limits<std::uint64_t>::max();
    const auto max_primitive = std::numeric_limits<std::uint32_t>::max();
    const auto supported = max_cycle - 16ull * max_primitive;
    auto c = fixture();
    MixedDramController m(c, 64);
    m.submit(req(1, 0, 0)); m.submit(req(2, 1000, 64));
    rejects([&] { m.submit(req(3, max_cycle - 10, 128)); },
            "unsupported arrival rejected before taking ownership");
    check(m.stats().submitted == 2 && m.stats().waiting_admission == 2,
          "unsupported arrival leaves both valid owners intact");
    const auto snapshot = m;
    rejects([&] { m.advance(max_cycle); },
            "unsupported frontier rejected before selecting earlier requests");
    check(m.stats().admitted == 0 && m.stats().waiting_admission == 2,
          "unsupported frontier preserves unselected requests and counters");
    auto replay = snapshot;
    const auto expected = replay.advance(2000);
    const auto recovered = m.advance(2000);
    check(recovered == expected && recovered.size() == 2,
          "valid retry returns all earlier completions exactly once");
    check(m.stats().reads_serviced + m.stats().pending_reads == m.stats().admitted,
          "horizon rejection preserves admitted service conservation");
    // The bounded horizon itself is legal, including a column reserved past it.
    MixedDramController edge(c, 64);
    edge.submit(req(1, supported - 1, 0));
    const auto out = edge.advance(supported);
    check(out.size() == 1 && out[0].command == supported + 42 &&
          out[0].response == supported + 158,
          "supported horizon reserves finite exact timestamps beyond frontier");
    rejects([&] { edge.submit(req(2, supported, 0)); },
            "arrival with no legal later exclusive frontier rejected");
    // Cumulative delay is a second checked arithmetic path: conservative
    // preflight must reject an unrepresentable bound before touching state.
    MixedDramController delay(c, 64);
    delay.submit(req(1, 0, 0)); delay.submit(req(2, 0, 64));
    rejects([&] { delay.advance(supported); },
            "unrepresentable admission-delay bound rejected before mutation");
    check(delay.stats().admitted == 0 && delay.stats().waiting_admission == 2,
          "delay-bound rejection preserves owners");
    check(delay.advance(2000).size() == 2,
          "smaller certified frontier recovers after delay-bound rejection");
    // Exercise the bound with maximal legal primitive timings and an auto-PRE
    // write conflict, including reservations well beyond the supported event.
    auto huge = c;
    auto& d = huge.dram;
    d.t_cl = d.t_rcd = d.t_rp = d.t_ras = d.t_rtp = max_primitive;
    d.t_rrd = d.t_rrd_l = d.t_xaw = d.t_ccd_l = d.t_cs = max_primitive;
    d.burst_cycles = d.frontend_latency = d.backend_latency = max_primitive;
    huge.t_cwl = huge.t_rcd_wr = huge.t_ccd_l_wr = huge.t_rtw = max_primitive;
    huge.t_wtr = huge.t_wtr_l = huge.t_wr = max_primitive;
    d.write_low_threshold_percent = 0; d.max_accesses_per_row = 1;
    MixedDramController ceiling(huge, 64);
    ceiling.submit(req(1, supported - 1, 0, true));
    ceiling.submit(req(2, supported - 1, 64, true));
    const auto ceiling_out = ceiling.advance(supported);
    check(ceiling_out.size() == 2 &&
          ceiling_out[0].command == supported - 1 + 1ull * max_primitive &&
          ceiling_out[1].command == supported - 1 + 6ull * max_primitive &&
          ceiling_out[1].precharge == supported - 1 + 9ull * max_primitive,
          "maximum primitive timings remain exact within reserved headroom");
}

void projected_batches() {
    auto c = fixture();
    c.dram.read_buffer_size = 1;
    MixedDramController m(c, 64);
    const auto first = m.reserve_projected_batch({req(1, 0, 0), req(2, 1, 1)});
    check(first.size() == 2 && first[1].admission == first[0].dram_ready,
          "projected read capacity releases at data ready");
    const auto now = first[1].selection;
    const auto snapshot = m;
    const auto second = m.reserve_projected_batch({req(3, 0, 64)});
    check(second.size() == 1 && second[0].arrival == now,
          "late projected arrival clamps to selection, not response");
    check(m.stats().projected_late_arrivals == 1 &&
          m.stats().projected_late_cycles == now,
          "projected approximation measured explicitly");
    auto restored = snapshot;
    check(restored.reserve_projected_batch({req(3, 0, 64)}) == second,
          "projected snapshot reproduces state and selections");
    rejects([&] { m.submit(req(4, 10000, 0)); }, "projected cannot submit strict");
    rejects([&] { m.advance(10000); }, "projected cannot advance strict");
    MixedDramController strict(c, 64);
    strict.advance(1);
    rejects([&] { strict.reserve_projected_batch({req(1, 1, 0)}); },
            "strict cannot reserve projected");

    MixedDramController writes(c, 64);
    check(writes.reserve_projected_batch({req(1, 0, 0, true), req(2, 0, 1, true)}).empty(),
          "projected batch does not flush low-water writes");
    check(writes.stats().pending_writes == 2, "projected write owners survive batch");
    auto out = writes.reserve_projected_batch({req(3, 10, 2, true)});
    check(out.size() == 3 && writes.stats().pending_writes == 0,
          "projected read-empty progress includes prior batch writes");
    c.dram.write_low_threshold_percent = c.dram.write_high_threshold_percent = 100;
    MixedDramController capacity(c, 64);
    std::vector<MixedDramRequest> input;
    for (unsigned i = 0; i < 7; ++i) input.push_back(req(i, 0, i, true));
    capacity.reserve_projected_batch(input);
    const auto counts = capacity.stats();
    check(counts.submitted == 7 && counts.admitted == 7 &&
          counts.writes_serviced + counts.pending_writes == 7 &&
          counts.projected_capacity_drains != 0,
          "projected full write buffer progresses even at low=100");
    c.dram.read_buffer_size = 1;
    MixedDramController full_but_no_due_write(c, 64);
    input.clear();
    for (unsigned i = 0; i < 4; ++i) input.push_back(req(i, 0, i, true));
    input.push_back(req(10, 0, 64));
    input.push_back(req(11, 1, 65));
    full_but_no_due_write.reserve_projected_batch(input);
    check(full_but_no_due_write.stats().writes_serviced == 0 &&
          full_but_no_due_write.stats().projected_capacity_drains == 0,
          "capacity-blocked read cannot force an unrelated low-water WB drain");

    c = fixture(); c.dram.channels = 2;
    MixedDramController channels(c, 64);
    channels.reserve_projected_batch({req(1, 10000, 0)});
    out = channels.reserve_projected_batch({req(2, 0, 1)});
    check(out.size() == 1 && out[0].selection == 0 &&
          channels.stats().projected_late_arrivals == 0,
          "one channel's lookahead never clamps an independent channel");
    const auto before = channels.stats().submitted;
    rejects([&] { channels.reserve_projected_batch({req(3, 0, 0), req(3, 0, 0)}); },
            "duplicate projected batch IDs rejected");
    check(channels.stats().submitted == before, "invalid batch takes no ownership");
}
}

void test_mixed_dram() {
    threshold(); direction_timings(); adaptive_and_recovery();
    frontier_and_rollback(); occupancy(); validation_and_channels();
    interrupted_write_turn(); write_adaptive_visibility();
    activation_calendars_and_policies();
    unsupported_horizon_is_rejected_before_mutation();
    projected_batches();
}

#ifdef FASTSIM_MIXED_DRAM_STANDALONE
int main() {
    try { test_mixed_dram(); std::cout << "all mixed DRAM tests passed\n"; }
    catch (const std::exception& e) {
        std::cerr << "mixed DRAM test failure: " << e.what() << '\n'; return 1;
    }
}
#endif
