#pragma once
#include "fastsim/config.hpp"
#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace fastsim {
inline std::uint64_t refresh_add(std::uint64_t a,std::uint64_t b) {
    if (a>std::numeric_limits<std::uint64_t>::max()-b)
        throw std::overflow_error("DRAM refresh timestamp overflow");
    return a+b;
}
inline std::uint64_t refresh_multiply(std::uint64_t a,std::uint64_t b) {
    if (b && a>std::numeric_limits<std::uint64_t>::max()/b)
        throw std::overflow_error("DRAM refresh tick conversion overflow");
    return a*b;
}
inline std::uint64_t refresh_ceil(std::uint64_t ticks,std::uint64_t scale) {
    return ticks/scale+(ticks%scale!=0);
}

// One rank's timer. Candidate probes never mutate it. Powerdown is outside
// the compact controller's support. The recurrence intentionally follows the
// captured gem5 processRefreshEvent preparation event, not a fitted period.
struct DramRankRefresh {
    std::uint64_t next_prepare=0,available=0,data_done=0;
    std::uint64_t preparations=0,precharged_ranks=0,idle_skipped=0;
    std::uint64_t observation_start=0;

    void initialize(const DramConfig& config) {
        next_prepare=refresh_add(config.refresh_start_tick,
            config.t_refi_ticks-refresh_multiply(config.t_rp,config.refresh_ticks_per_cycle));
    }
    void reset_observation(std::uint64_t tick) {
        preparations=precharged_ranks=idle_skipped=0;observation_start=tick;
    }
    bool advance(const DramConfig& c,std::uint64_t through,
                 std::uint64_t pre_allowed,std::uint64_t closed_ready,bool open) {
        if (next_prepare>through) return false;
        const auto period=c.t_refi_ticks-refresh_multiply(c.t_rp,c.refresh_ticks_per_cycle);
        const auto first=next_prepare;
        auto start=std::max(first,closed_ready);
        if (open) start=std::max(start,refresh_add(std::max(first,pre_allowed),
            refresh_multiply(c.t_rp,c.refresh_ticks_per_cycle)));
        start=std::max(start,data_done);
        available=refresh_add(start,c.t_rfc_ticks);
        next_prepare=refresh_add(first,period);
        if (available>next_prepare)
            throw std::runtime_error("DRAM rank refresh overrun: outstanding service exceeds the next preparation event");
        if (first>=observation_start) {++preparations;precharged_ranks+=open;}
        // With no intervening issue every bank is now closed. Subsequent
        // idle windows start immediately; skip all of them arithmetically.
        if (next_prepare<=through) {
            const auto count=(through-next_prepare)/period+1;
            const auto last=refresh_add(next_prepare,refresh_multiply(count-1,period));
            const auto first_scored=observation_start<=next_prepare?0:
                std::min(count,refresh_ceil(observation_start-next_prepare,period));
            preparations+=count-first_scored;idle_skipped+=count-first_scored;
            available=refresh_add(last,c.t_rfc_ticks);
            next_prepare=refresh_add(last,period);
        }
        return true;
    }
};
} // namespace fastsim
