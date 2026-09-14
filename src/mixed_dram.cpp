#include "fastsim/mixed_dram.hpp"
#include <algorithm>
#include <deque>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

namespace fastsim {
MixedDramConfig make_mixed_dram_config(const DramConfig& dram) {
    MixedDramConfig result;
    result.dram = dram;
    result.t_cwl = dram.t_cwl == 0 ? dram.t_cl : dram.t_cwl;
    result.t_rcd_wr = dram.t_rcd_wr == 0 ? dram.t_rcd : dram.t_rcd_wr;
    result.t_ccd_l_wr = dram.t_ccd_l_wr;
    result.t_rtw = dram.t_rtw;
    result.t_wtr = dram.t_wtr;
    result.t_wtr_l = dram.t_wtr_l;
    result.t_wr = dram.t_wr;
    result.scheduler = dram.scheduler == "fcfs" ? MixedDramScheduler::kFcfs
        : MixedDramScheduler::kRowHitFirstApproximation;
    return result;
}
bool DramServiceId::operator==(const DramServiceId& o) const {
    return std::tie(core, sequence, fragment, kind) ==
           std::tie(o.core, o.sequence, o.fragment, o.kind);
}
bool DramServiceId::operator<(const DramServiceId& o) const {
    return std::tie(core, sequence, fragment, kind) <
           std::tie(o.core, o.sequence, o.fragment, o.kind);
}
bool MixedDramCompletion::operator==(const MixedDramCompletion& o) const {
    return id == o.id && std::tie(arrival, admission, selection, command, dram_ready,
        response, row_hit, auto_precharged, precharge, activation) ==
        std::tie(o.arrival, o.admission, o.selection, o.command, o.dram_ready,
        o.response, o.row_hit, o.auto_precharged, o.precharge, o.activation);
}
namespace {
using Cycle = std::uint64_t;
constexpr Cycle never = std::numeric_limits<Cycle>::max();
Cycle add(Cycle a, Cycle b) {
    if (b > never - a) throw std::overflow_error("mixed DRAM cycle overflow");
    return a + b;
}
bool power_of_two(std::uint64_t x) { return x && !(x & (x - 1)); }
bool is_write(const MixedDramRequest& r) { return r.id.kind == DramServiceKind::kWriteback; }
}
struct MixedDramController::State {
    struct Address { std::uint32_t channel, rank, bank; Cycle row; };
    struct Entry { MixedDramRequest request; Address address; Cycle admission = 0; };
    struct Bank {
        bool open = false;
        Cycle row = 0, read_at = 0, write_at = 0, pre_at = 0, act_at = 0;
        std::uint32_t accesses = 0;
    };
    struct Flight {
        DramServiceId id;
        Cycle dram_ready, response;
        bool write, capacity_released = false;
    };
    struct Channel {
        std::vector<Entry> reads, writes;
        std::vector<Bank> banks;
        std::vector<std::deque<Cycle>> activations;
        std::vector<Flight> flights;
        Cycle next_burst = 0, event = 0;
        bool scheduled = false, write = false, next_write = false;
        std::uint64_t reads_turn = 0, writes_turn = 0;
        std::uint64_t read_responses = 0;
        Cycle projected_now = 0;
    };
    MixedDramConfig config;
    std::uint32_t line_size;
    Cycle frontier = 0, now = 0;
    MixedDramStats counts;
    std::vector<Channel> channels;
    std::vector<Entry> waiting;
    std::set<DramServiceId> active;
    enum class Mode { kUnused, kStrict, kProjected };
    Mode mode = Mode::kUnused;

    State(const MixedDramConfig& c, std::uint32_t ls) : config(c), line_size(ls) {
        const auto& d = config.dram;
        if (!power_of_two(ls) || !power_of_two(d.channels) ||
            !power_of_two(d.ranks_per_channel) || !power_of_two(d.banks_per_channel) ||
            !power_of_two(d.bank_groups_per_rank) || d.bank_groups_per_rank > d.banks_per_channel ||
            !power_of_two(d.row_bytes) || d.row_bytes < ls ||
            d.size_bytes < ls || d.size_bytes % ls || !d.read_buffer_size ||
            !d.write_buffer_size || !d.burst_cycles || !d.t_cl || !d.t_rcd ||
            !d.t_rp || !c.t_cwl || !c.t_rcd_wr ||
            d.write_low_threshold_percent > d.write_high_threshold_percent ||
            d.write_high_threshold_percent > 100 || !d.min_reads_per_switch ||
            !d.min_writes_per_switch ||
            (c.page_policy != "open" && c.page_policy != "close" &&
             c.page_policy != "open_adaptive" && c.page_policy != "close_adaptive") ||
            (c.scheduler != MixedDramScheduler::kRowHitFirstApproximation &&
             c.scheduler != MixedDramScheduler::kFcfs))
            throw std::invalid_argument("invalid mixed DRAM geometry, timing, or policy");
        const auto bank_count = static_cast<Cycle>(d.ranks_per_channel) * d.banks_per_channel;
        if (bank_count > std::numeric_limits<std::size_t>::max() / sizeof(Bank) / d.channels)
            throw std::invalid_argument("mixed DRAM geometry overflows allocation");
        channels.resize(d.channels);
        for (auto& ch : channels) {
            ch.banks.resize(static_cast<std::size_t>(bank_count));
            ch.activations.resize(d.ranks_per_channel);
        }
    }
    Address decode(Cycle line) const {
        const auto& d = config.dram;
        const auto channel = static_cast<std::uint32_t>(line % d.channels);
        auto above_column = line / d.channels / (d.row_bytes / line_size);
        const auto bank = static_cast<std::uint32_t>(above_column % d.banks_per_channel);
        above_column /= d.banks_per_channel;
        const auto rank = static_cast<std::uint32_t>(above_column % d.ranks_per_channel);
        return {channel, rank, bank, above_column / d.ranks_per_channel};
    }
    std::size_t bank_index(const Address& a) const {
        return static_cast<std::size_t>(a.rank) * config.dram.banks_per_channel + a.bank;
    }
    bool capacity(const Entry& e) const {
        const auto& ch = channels[e.address.channel];
        return is_write(e.request) ? ch.writes.size() < config.dram.write_buffer_size :
            ch.reads.size() + ch.read_responses < config.dram.read_buffer_size;
    }
    void release(Cycle t) {
        for (auto& ch : channels) {
            auto it = ch.flights.begin();
            while (it != ch.flights.end()) {
                if (!it->write && !it->capacity_released && it->dram_ready <= t) {
                    --ch.read_responses;
                    it->capacity_released = true;
                }
                if (it->response > t) { ++it; continue; }
                active.erase(it->id);
                it = ch.flights.erase(it);
            }
        }
    }
    void admit(Cycle t) {
        auto it = waiting.begin();
        while (it != waiting.end() && it->request.arrival <= t) {
            if (!capacity(*it)) { ++it; continue; }
            auto& ch = channels[it->address.channel];
            it->admission = t;
            ++counts.admitted;
            if (t > it->request.arrival) {
                ++counts.admission_delayed;
                counts.admission_delay_cycles = add(counts.admission_delay_cycles, t - it->request.arrival);
            }
            (is_write(it->request) ? ch.writes : ch.reads).push_back(*it);
            if (!ch.scheduled) { ch.scheduled = true; ch.event = t; }
            it = waiting.erase(it);
        }
    }
    void precharge(Bank& b, Cycle t) {
        b.open = false;
        b.act_at = std::max(b.act_at, add(t, config.dram.t_rp));
        ++counts.precharges;
    }
    MixedDramCompletion reserve(Channel& ch, Entry e, Cycle event) {
        // Overflow proof, U=UINT32_MAX: the previous column P satisfies
        // P <= event + commandOffset <= event+2U, including wake from idle.
        // Existing PRE floors are <=P+3U; the next ACT is <=max(event,P+4U)
        // including tRP, tRRD and tXAW. The next column is <=event+7U.
        // Updated response/calendar timestamps add at most4U, so all checked
        // sums are <=event+11U. advance's16U headroom covers these reservations
        // even when commands lie beyond its exclusive frontier. No saturation.
        const auto& d = config.dram;
        const bool write = is_write(e.request);
        auto& b = ch.banks[bank_index(e.address)];
        MixedDramCompletion out;
        out.id = e.request.id; out.arrival = e.request.arrival;
        out.admission = e.admission; out.selection = event;
        out.row_hit = b.open && b.row == e.address.row;
        if (!out.row_hit) {
            if (b.open) {
                out.precharge = std::max(event, b.pre_at);
                precharge(b, out.precharge);
            }
            Cycle act = std::max(event, b.act_at);
            auto& history = ch.activations[e.address.rank];
            if (d.activation_limit && history.size() >= d.activation_limit)
                act = std::max(act, add(history.front(), d.t_xaw));
            out.activation = act;
            b.open = true; b.row = e.address.row; b.accesses = 0;
            b.pre_at = std::max(b.pre_at, add(act, d.t_ras));
            b.read_at = std::max(b.read_at, add(act, d.t_rcd));
            b.write_at = std::max(b.write_at, add(act, config.t_rcd_wr));
            for (std::uint32_t bank = 0; bank < d.banks_per_channel; ++bank) {
                auto& peer = ch.banks[static_cast<std::size_t>(e.address.rank) * d.banks_per_channel + bank];
                const bool same_bg = bank % d.bank_groups_per_rank == e.address.bank % d.bank_groups_per_rank;
                const Cycle spacing = same_bg ? std::max(d.t_rrd, d.t_rrd_l) : d.t_rrd;
                peer.act_at = std::max(peer.act_at, add(act, spacing));
            }
            if (d.activation_limit) {
                history.push_back(act);
                while (history.size() > d.activation_limit) history.pop_front();
            }
        } else { ++counts.row_hits; }
        out.command = std::max({event, write ? b.write_at : b.read_at, ch.next_burst});
        const Cycle ready = add(add(out.command, write ? config.t_cwl : d.t_cl), d.burst_cycles);
        out.dram_ready = ready;
        // A WB result is DRAM-data completion, not architectural store completion
        // or the gem5 frontend acceptance response.
        out.response = write ? ready : add(add(ready, d.frontend_latency), d.backend_latency);
        b.pre_at = std::max(b.pre_at, write ? add(ready, config.t_wr) : add(out.command, d.t_rtp));
        for (std::uint32_t rank = 0; rank < d.ranks_per_channel; ++rank) {
            for (std::uint32_t bank = 0; bank < d.banks_per_channel; ++bank) {
                const bool same_rank = rank == e.address.rank;
                const bool same_bg = bank % d.bank_groups_per_rank == e.address.bank % d.bank_groups_per_rank;
                Cycle rd_gap, wr_gap;
                if (!same_rank) { rd_gap = wr_gap = add(d.burst_cycles, d.t_cs); }
                else if (write) {
                    rd_gap = add(add(config.t_cwl, d.burst_cycles), same_bg ? config.t_wtr_l : config.t_wtr);
                    if (same_bg) rd_gap = std::max<Cycle>(rd_gap, d.t_ccd_l);
                    wr_gap = same_bg ? std::max(d.burst_cycles, config.t_ccd_l_wr) : d.burst_cycles;
                } else {
                    rd_gap = same_bg ? std::max(d.burst_cycles, d.t_ccd_l) : d.burst_cycles;
                    wr_gap = add(d.burst_cycles, config.t_rtw);
                    if (same_bg) wr_gap = std::max<Cycle>(wr_gap, d.t_ccd_l);
                }
                auto& peer = ch.banks[static_cast<std::size_t>(rank) * d.banks_per_channel + bank];
                peer.read_at = std::max(peer.read_at, add(out.command, rd_gap));
                peer.write_at = std::max(peer.write_at, add(out.command, wr_gap));
            }
        }
        ++b.accesses;
        const bool cap = d.max_accesses_per_row && b.accesses >= d.max_accesses_per_row;
        bool hit_waits = false, conflict_waits = false;
        const auto& queue = write ? ch.writes : ch.reads;
        for (const auto& pending : queue) {
            if (bank_index(pending.address) != bank_index(e.address)) continue;
            if (pending.address.row == e.address.row) hit_waits = true;
            else conflict_waits = true;
        }
        const bool adaptive = config.page_policy == "close" ||
            (config.page_policy == "close_adaptive" && !hit_waits) ||
            (config.page_policy == "open_adaptive" && conflict_waits && !hit_waits);
        if (cap || adaptive) {
            out.auto_precharged = true; out.precharge = b.pre_at;
            precharge(b, out.precharge);
            if (cap) ++counts.row_cap_precharges;
            else ++counts.adaptive_precharges;
        }
        ch.next_burst = add(out.command, d.burst_cycles);
        const Cycle offset = add(d.t_rp, std::max(d.t_rcd, config.t_rcd_wr));
        ch.event = std::max(event, ch.next_burst > offset ? ch.next_burst - offset : 0);
        ch.flights.push_back({out.id, out.dram_ready, out.response, write, false});
        if (write) ++counts.writes_serviced;
        else { ++counts.reads_serviced; ++ch.read_responses; }
        return out;
    }
    void select(Channel& ch, std::vector<MixedDramCompletion>& out) {
        const auto& d = config.dram;
        if (ch.write != ch.next_write) {
            ch.write = ch.next_write;
            ch.reads_turn = ch.writes_turn = 0;
            ++counts.direction_switches;
        }
        const Cycle low = static_cast<Cycle>(d.write_buffer_size) * d.write_low_threshold_percent / 100;
        const Cycle high = static_cast<Cycle>(d.write_buffer_size) * d.write_high_threshold_percent / 100;
        if (!ch.write && ch.reads.empty()) {
            if (!ch.writes.empty() && ch.writes.size() > low) ch.next_write = true;
            else ch.scheduled = false;
            return;
        }
        auto& queue = ch.write ? ch.writes : ch.reads;
        if (queue.empty()) { ch.next_write = false; return; }
        std::size_t selected = 0;
        const auto limit = d.frfcfs_selection_window ?
            std::min<std::size_t>(queue.size(), d.frfcfs_selection_window) : queue.size();
        if (config.scheduler == MixedDramScheduler::kRowHitFirstApproximation) {
            for (std::size_t i = 0; i < limit; ++i) {
                const auto& bank = ch.banks[bank_index(queue[i].address)];
                if (bank.open && bank.row == queue[i].address.row) { selected = i; break; }
            }
        }
        Entry e = queue[selected];
        queue.erase(queue.begin() + selected);
        out.push_back(reserve(ch, e, ch.event));
        if (ch.write) {
            ++ch.writes_turn;
            if (ch.writes.empty() || ch.writes.size() + d.min_writes_per_switch < low ||
                (!ch.reads.empty() && ch.writes_turn >= d.min_writes_per_switch))
                ch.next_write = false;
        } else {
            ++ch.reads_turn;
            if (ch.writes.size() > high && (ch.reads_turn >= d.min_reads_per_switch || ch.reads.empty()))
                ch.next_write = true;
        }
    }
};
MixedDramController::MixedDramController(const MixedDramConfig& c, std::uint32_t ls)
    : state_(new State(c, ls)) {}
MixedDramController::~MixedDramController() = default;
MixedDramController::MixedDramController(const MixedDramController& o)
    : state_(new State(*o.state_)) {}
MixedDramController& MixedDramController::operator=(const MixedDramController& o) {
    if (this != &o) state_.reset(new State(*o.state_));
    return *this;
}
MixedDramController::MixedDramController(MixedDramController&&) noexcept = default;
MixedDramController& MixedDramController::operator=(MixedDramController&&) noexcept = default;
void MixedDramController::submit(const MixedDramRequest& r) {
    auto& s = *state_;
    if (s.mode == State::Mode::kProjected ||
        r.arrival < s.frontier || r.arrival >= max_frontier ||
        r.line >= s.config.dram.size_bytes / s.line_size ||
        (r.id.kind != DramServiceKind::kRead && r.id.kind != DramServiceKind::kWriteback) ||
        s.active.count(r.id)) throw std::invalid_argument("invalid or active duplicate mixed DRAM request");
    State::Entry e{r, s.decode(r.line), 0};
    auto pos = std::lower_bound(s.waiting.begin(), s.waiting.end(), e,
        [](const State::Entry& a, const State::Entry& b) {
            return std::tie(a.request.arrival, a.request.id) < std::tie(b.request.arrival, b.request.id);
        });
    s.waiting.insert(pos, e);
    s.active.insert(r.id);
    ++s.counts.submitted;
    s.mode = State::Mode::kStrict;
}
std::vector<MixedDramCompletion> MixedDramController::advance(Cycle frontier) {
    auto& s = *state_;
    if (s.mode == State::Mode::kProjected ||
        frontier < s.frontier || frontier > max_frontier)
        throw std::invalid_argument("unsupported mixed DRAM frontier");
    // Preflight the only checked sum that accumulates across requests. All
    // future admissions in this call come from waiting, at t < frontier; this
    // conservative sum therefore bounds their total added delay. Reject before
    // release/admit/select, preserving both owners and all undelivered outputs.
    Cycle delay_budget = never - s.counts.admission_delay_cycles;
    for (const auto& e : s.waiting) {
        if (e.request.arrival >= frontier) break;
        const Cycle bound = frontier - e.request.arrival;
        if (bound > delay_budget)
            throw std::invalid_argument("unsupported mixed DRAM admission-delay bound");
        delay_budget -= bound;
    }
    s.mode = State::Mode::kStrict;
    std::vector<MixedDramCompletion> out;
    for (;;) {
        Cycle t = never;
        for (const auto& e : s.waiting)
            if (s.capacity(e)) t = std::min(t, std::max(s.now, e.request.arrival));
        for (const auto& ch : s.channels) {
            if (ch.scheduled) t = std::min(t, ch.event);
            for (const auto& f : ch.flights) {
                t = std::min(t, f.response);
                if (!f.write && !f.capacity_released) t = std::min(t, f.dram_ready);
            }
        }
        if (t >= frontier) break;
        s.now = t;
        s.release(t);
        s.admit(t);
        // Exactly one channel event, then reconsider admissions made possible
        // by a write selection. Channel number breaks simultaneous event ties.
        for (auto& ch : s.channels) {
            if (ch.scheduled && ch.event == t) { s.select(ch, out); break; }
        }
    }
    s.frontier = s.now = frontier;
    return out;
}
std::vector<MixedDramCompletion> MixedDramController::reserve_projected_batch(
    const std::vector<MixedDramRequest>& requests) {
    auto& s = *state_;
    if (s.mode == State::Mode::kStrict)
        throw std::invalid_argument("cannot mix strict and projected DRAM APIs");
    std::set<DramServiceId> ids;
    // Validate the complete input before taking ownership. Runtime time-range
    // failures require the caller's transaction to restore this controller.
    for (const auto& r : requests) {
        if (r.arrival >= max_frontier ||
            r.line >= s.config.dram.size_bytes / s.line_size ||
            (r.id.kind != DramServiceKind::kRead && r.id.kind != DramServiceKind::kWriteback) ||
            s.active.count(r.id) || !ids.insert(r.id).second)
            throw std::invalid_argument("invalid projected DRAM request");
    }
    s.mode = State::Mode::kProjected;
    ++s.counts.projected_batches;
    std::vector<std::vector<State::Entry>> channels(s.channels.size());
    for (auto r : requests) {
        const auto address = s.decode(r.line);
        const auto now = s.channels[address.channel].projected_now;
        if (r.arrival < now) {
            ++s.counts.projected_late_arrivals;
            s.counts.projected_late_cycles = add(s.counts.projected_late_cycles, now - r.arrival);
            r.arrival = now;
        }
        channels[address.channel].push_back({r, address, 0});
        s.active.insert(r.id);
        ++s.counts.submitted;
    }
    std::vector<MixedDramCompletion> out;
    for (std::size_t channel = 0; channel < channels.size(); ++channel) {
        auto& future = channels[channel];
        auto& ch = s.channels[channel];
        std::sort(future.begin(), future.end(), [](const auto& a, const auto& b) {
            return std::tie(a.request.arrival, a.request.id) <
                   std::tie(b.request.arrival, b.request.id);
        });
        std::size_t consumed = 0;
        while (consumed != future.size() || !ch.reads.empty() || ch.scheduled) {
            Cycle t = ch.scheduled ? std::max(ch.projected_now, ch.event) : never;
            for (std::size_t i = consumed; i < future.size(); ++i) {
                ++s.counts.projected_scanned_entries;
                if (s.capacity(future[i]) ||
                    (!ch.scheduled && is_write(future[i].request) &&
                     ch.writes.size() == s.config.dram.write_buffer_size))
                    t = std::min(t, std::max(ch.projected_now, future[i].request.arrival));
            }
            if (consumed != future.size()) {
                for (const auto& flight : ch.flights)
                    if (!flight.write && !flight.capacity_released)
                        t = std::min(t, std::max(ch.projected_now, flight.dram_ready));
            }
            if (t == never)
                throw std::logic_error("projected DRAM made no admission progress");
            if (t >= max_frontier)
                throw std::overflow_error("projected DRAM exceeds supported time range");
            ch.projected_now = t;
            ++s.counts.projected_events;
            auto flight = ch.flights.begin();
            while (flight != ch.flights.end()) {
                if (!flight->write && !flight->capacity_released && flight->dram_ready <= t) {
                    --ch.read_responses;
                    flight->capacity_released = true;
                }
                if (flight->response <= t) {
                    s.active.erase(flight->id);
                    flight = ch.flights.erase(flight);
                } else ++flight;
            }
            // Compact the unadmitted suffix once per event, preserving its
            // order. A blocked read does not prevent an independent WB.
            std::size_t keep = consumed;
            for (std::size_t i = consumed; i < future.size(); ++i) {
                auto entry = future[i];
                if (entry.request.arrival <= t && s.capacity(entry)) {
                    entry.admission = t;
                    ++s.counts.admitted;
                    if (t > entry.request.arrival) {
                        ++s.counts.admission_delayed;
                        s.counts.admission_delay_cycles =
                            add(s.counts.admission_delay_cycles, t - entry.request.arrival);
                    }
                    (is_write(entry.request) ? ch.writes : ch.reads).push_back(entry);
                    if (!ch.scheduled) { ch.scheduled = true; ch.event = t; }
                } else future[keep++] = entry;
            }
            future.resize(keep);
            // Even low=100 must permit a full buffer to accept pending WB.
            if (!ch.scheduled &&
                ch.writes.size() == s.config.dram.write_buffer_size &&
                std::any_of(future.begin(), future.end(), [t](const auto& e) {
                    return is_write(e.request) && e.request.arrival <= t;
                })) {
                ch.scheduled = true; ch.event = t; ch.next_write = true;
                ++s.counts.projected_capacity_drains;
            }
            if (ch.scheduled && ch.event <= t) {
                ch.event = t;
                s.select(ch, out);
            }
        }
    }
    return out;
}
MixedDramStats MixedDramController::stats() const {
    auto result = state_->counts;
    result.waiting_admission = state_->waiting.size();
    for (const auto& ch : state_->channels) {
        result.pending_reads += ch.reads.size(); result.pending_writes += ch.writes.size();
        result.read_responses += ch.read_responses;
    }
    return result;
}
} // namespace fastsim
