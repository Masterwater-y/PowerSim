/*
 * Copyright (c) 2010-2020 ARM Limited
 * All rights reserved
 *
 * The license below extends only to copyright in the software and shall
 * not be construed as granting a license to any other intellectual
 * property including but not limited to intellectual property relating
 * to a hardware implementation of the functionality of the software
 * licensed hereunder.  You may use the software subject to the license
 * terms below provided that you ensure that this notice is replicated
 * unmodified and in its entirety in all distributions of the software,
 * modified or unmodified, in source code or in binary form.
 *
 * Copyright (c) 2013 Amin Farmahini-Farahani
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "fastsim/dram_address.hpp"
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
    MixedDramFrontendStats frontend_counts;
    bool native_frontend=false;
    std::vector<Channel> channels;
    std::vector<Entry> waiting;
    std::set<DramServiceId> active;
    enum class Mode { kUnused, kStrict, kProjected };
    Mode mode = Mode::kUnused;

    State(const MixedDramConfig& c, std::uint32_t ls) : config(c), line_size(ls) {
        const auto& d = config.dram;
        validate_dram_layout(d, ls);
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
             c.scheduler != MixedDramScheduler::kFcfs &&
             c.scheduler != MixedDramScheduler::kBankPrepFrfcfs) ||
            (c.scheduler == MixedDramScheduler::kBankPrepFrfcfs &&
             (d.frfcfs_selection_window || d.refresh_enabled)))
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
        const auto mapped = dram_decode_line(d, line, line_size);
        const auto channel = mapped.channel;
        auto above_column = mapped.local_line / (d.row_bytes / line_size);
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
    void admit(Cycle t, std::vector<MixedDramAdmission>* admissions) {
        auto it = waiting.begin();
        while (it != waiting.end() && it->request.arrival <= t) {
            if (!capacity(*it)) { ++it; continue; }
            auto& ch = channels[it->address.channel];
            it->admission = t;
            MixedDramAdmission receipt;receipt.id=it->request.id;receipt.line=it->request.line;
            receipt.arrival=it->request.arrival;receipt.admission=t;
            bool physical_entry=true;
            if(native_frontend) {
                const auto same_line=std::find_if(ch.writes.begin(),ch.writes.end(),[&](const Entry& e) {
                    return e.request.line==it->request.line;
                });
                if(is_write(it->request)) {
                    receipt.frontend_response=add(t,config.dram.frontend_latency);
                    ++frontend_counts.write_responses_scheduled;
                    if(same_line!=ch.writes.end()) {
                        receipt.merged_write=true;receipt.queued_write=same_line->request.id;physical_entry=false;
                        ++frontend_counts.merged_write_requests;
                    }
                } else if(same_line!=ch.writes.end()) {
                    receipt.frontend_response=add(t,config.dram.frontend_latency);
                    receipt.forwarded_read=true;receipt.queued_write=same_line->request.id;physical_entry=false;
                    ++frontend_counts.forwarded_read_requests;
                }
            }
            if(admissions) admissions->push_back(receipt);
            ++counts.admitted;
            if (t > it->request.arrival) {
                ++counts.admission_delayed;
                counts.admission_delay_cycles = add(counts.admission_delay_cycles, t - it->request.arrival);
            }
            if(physical_entry) (is_write(it->request) ? ch.writes : ch.reads).push_back(*it);
            else {
                // Merged writes and WQ-forwarded reads retain identity only
                // through their frontend response, with no DRAM/read-buffer lease.
                ch.flights.push_back({it->request.id,*receipt.frontend_response,
                    *receipt.frontend_response,is_write(it->request),true});
            }
            if ((physical_entry || is_write(it->request)) && !ch.scheduled) {
                ch.scheduled = true; ch.event = t;
            }
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
    // Candidate policy from DRAMInterface::chooseNextFRFCFS/minBankPrep.
    // All ranks here are available: refresh/power states remain outside this
    // controller's supported subset. Uses model event time and bank calendars.
    std::size_t bank_prep_frfcfs(const Channel& ch,const std::vector<Entry>& queue,
                                Cycle min_col_at) const {
        const auto& d=config.dram;
        std::vector<bool> earliest(ch.banks.size(),false),waiting_bank(ch.banks.size(),false);
        bool filled=false,hidden=false,found_hidden=false,prepped=false,found_earliest=false;
        std::optional<std::size_t> selected;
        for(std::size_t i=0;i<queue.size();++i) {
            const auto index=bank_index(queue[i].address);const auto& bank=ch.banks[index];
            const Cycle allowed=ch.write?bank.write_at:bank.read_at;
            if(bank.open && bank.row==queue[i].address.row) {
                if(allowed<=min_col_at){selected=i;break;}
                if(!found_hidden && !prepped){selected=i;prepped=true;}
            } else if(!found_earliest) {
                if(!filled) {
                    for(const auto& request:queue)waiting_bank[bank_index(request.address)]=true;
                    Cycle min_act=never;bool seamless=false;
                    const Cycle rcd=ch.write?config.t_rcd_wr:d.t_rcd;
                    // Translate absolute native ticks to a relative model
                    // origin: max(min_col_at-tRCD,event) cannot precede event.
                    const Cycle hidden_max=min_col_at>=rcd?std::max(min_col_at-rcd,ch.event):ch.event;
                    for(std::size_t b=0;b<ch.banks.size();++b)if(waiting_bank[b]) {
                        const auto& candidate=ch.banks[b];
                        const Cycle act=candidate.open?add(std::max(candidate.pre_at,ch.event),d.t_rp):
                            std::max(candidate.act_at,ch.event);
                        const Cycle col=std::max(ch.write?candidate.write_at:candidate.read_at,add(act,rcd));
                        const bool new_seamless=col<=min_col_at;
                        if(new_seamless || (!seamless && act<=min_act)) {
                            if(!seamless && (new_seamless || act<min_act))std::fill(earliest.begin(),earliest.end(),false);
                            seamless|=new_seamless;
                            // Source assigns this flag for each included bank;
                            // do not replace it with an OR across candidates.
                            hidden=act<=hidden_max;earliest[b]=true;min_act=act;
                        }
                    }
                    filled=true;
                }
                if(earliest[index]) {
                    found_earliest=true;found_hidden=hidden;
                    if(hidden || !prepped)selected=i;
                }
            }
        }
        if(!selected)throw std::logic_error("FRFCFS has no available bank in qualified all-ranks-idle subset");
        return *selected;
    }
    void select(Channel& ch, std::vector<MixedDramCompletion>& out) {
        const auto& d = config.dram;
        const bool switched=ch.write!=ch.next_write;
        if (switched) {
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
        if(config.scheduler==MixedDramScheduler::kBankPrepFrfcfs) {
            const Cycle extra=switched?std::min(ch.write?config.t_rtw:config.t_wtr,d.t_cs):0;
            selected=bank_prep_frfcfs(ch,queue,std::max(add(ch.next_burst,extra),ch.event));
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
void MixedDramController::enable_native_frontend() {
    auto& s=*state_;
    if(s.mode!=State::Mode::kUnused || s.counts.submitted || !s.waiting.empty() || !s.active.empty())
        throw std::invalid_argument("native DRAM frontend must be selected before submission");
    s.native_frontend=true;
}
MixedDramFrontendStats MixedDramController::frontend_stats() const { return state_->frontend_counts; }
void MixedDramController::submit(const MixedDramRequest& r) {
    auto& s = *state_;
    if (s.mode == State::Mode::kProjected ||
        r.arrival < s.frontier || r.arrival >= max_frontier ||
        !dram_contains_line(s.config.dram, r.line, s.line_size) ||
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
    return advance_impl(frontier,nullptr);
}
MixedDramProgress MixedDramController::advance_with_admissions(Cycle frontier) {
    MixedDramProgress progress;
    progress.completions=advance_impl(frontier,&progress.admissions);
    return progress;
}
std::vector<MixedDramCompletion> MixedDramController::advance_impl(
    Cycle frontier, std::vector<MixedDramAdmission>* admissions) {
    auto& s = *state_;
    if ((s.native_frontend && !admissions) || s.mode == State::Mode::kProjected ||
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
        s.admit(t,admissions);
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
    if (s.native_frontend || s.mode == State::Mode::kStrict ||
        s.config.scheduler==MixedDramScheduler::kBankPrepFrfcfs)
        throw std::invalid_argument("cannot mix native/strict and projected DRAM APIs");
    std::set<DramServiceId> ids;
    // Validate the complete input before taking ownership. Runtime time-range
    // failures require the caller's transaction to restore this controller.
    for (const auto& r : requests) {
        if (r.arrival >= max_frontier ||
            !dram_contains_line(s.config.dram, r.line, s.line_size) ||
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
