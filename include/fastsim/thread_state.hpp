#pragma once
#include <algorithm>
#include <cstdint>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <vector>
#include <utility>

namespace fastsim {
// Scheduler-boundary proxy. Durations come exclusively from target retirement
// clocks. A blocked logical task never stops the physical CPU trace producer.
class ThreadStateAccounting {
  public:
    ThreadStateAccounting(std::vector<std::uint64_t> owners,
                          const std::vector<std::uint64_t>& tasks)
        : owners_(std::move(owners)), tasks_(tasks.begin(),tasks.end()),
          last_(owners_.size()),active_(owners_.size()),other_(owners_.size()) {
        std::set<std::uint64_t> live;
        for(auto pid:owners_) if(pid) {
            if(!live.insert(pid).second) throw std::runtime_error("thread-state duplicate initial CPU owner");
            states_[pid]=1;
        }
    }
    void begin_measurement(std::uint64_t origin) {
        measuring_=true;
        std::fill(last_.begin(),last_.end(),origin);
        std::fill(active_.begin(),active_.end(),0);
        std::fill(other_.begin(),other_.end(),0);
    }
    void switch_owner(std::size_t core,std::uint64_t prev,std::uint64_t next,
                      std::uint64_t prev_state,std::uint64_t time) {
        if(owners_.at(core)!=prev || prev_state<1 || prev_state>3)
            throw std::runtime_error("thread-state owner/state mismatch");
        if(next && states_[next]==2)
            throw std::runtime_error("thread-state blocked task resumed without wake evidence");
        for(std::size_t other=0;next && other<owners_.size();++other)
            if(other!=core && owners_[other]==next)
                throw std::runtime_error("thread-state task already running on another CPU");
        accrue(core,time);
        if(prev) states_[prev]=prev_state;
        if(next) states_[next]=1;
        owners_[core]=next;
    }
    void wakeup(std::uint64_t pid) {
        // A wake of an already-running/runnable task does not replace its owner.
        if(pid && states_[pid]!=1) states_[pid]=1;
    }
    void finish_core(std::size_t core,std::uint64_t time) { accrue(core,time); }
    std::uint64_t owner(std::size_t core) const { return owners_.at(core); }
    const std::vector<std::uint64_t>& active_q16() const { return active_; }
    const std::vector<std::uint64_t>& other_q16() const { return other_; }
  private:
    void accrue(std::size_t core,std::uint64_t time) {
        if(!measuring_) return;
        if(time<last_.at(core)) throw std::runtime_error("thread-state boundary time regressed");
        auto& count=tasks_.count(owners_.at(core)) ? active_.at(core) : other_.at(core);
        const auto delta=time-last_[core];
        if(delta>UINT64_MAX-count) throw std::overflow_error("thread-state duration overflow");
        count+=delta; last_[core]=time;
    }
    bool measuring_=false;
    std::vector<std::uint64_t> owners_;
    std::set<std::uint64_t> tasks_;
    std::unordered_map<std::uint64_t,std::uint64_t> states_;
    std::vector<std::uint64_t> last_,active_,other_;
};
}
