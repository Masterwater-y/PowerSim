#pragma once
#include "fastsim/spr_ip_stride.hpp"
#include "fastsim/spr_stream_prefetch.hpp"
#include <cstdint>
#include <deque>
#include <optional>
#include <unordered_set>

namespace fastsim {
// Predictor and controller-input queue only. No cache state or request timing
// is mutated here: the simulation must service dequeued lines through its
// existing hierarchy. Copying the object snapshots predictor and queue state.
class SprRubyPrefetcher {
  public:
    // queue_limit=0 preserves the target's unbounded MessageBuffer semantics;
    // any nonzero host cap is an explicit approximation with drop accounting.
    explicit SprRubyPrefetcher(bool ip_stride,std::size_t queue_limit=0)
        :ip_enabled_(ip_stride),queue_limit_(queue_limit) {}
    void observe_data_access(std::uint64_t pc,std::uint64_t context,bool secure,
                             std::uint64_t vaddr,std::uint64_t paddr) {
        if(ip_enabled_) add(ip_.observe(pc,context,secure,vaddr,paddr));
    }
    void observe_data_miss(std::uint64_t address,std::uint64_t cycle) {
        add(stream_.observe_miss(address,cycle));
    }
    void observe_prefetch_access(std::uint64_t address,std::uint64_t cycle) {
        add(stream_.observe_prefetch_access(address,cycle));
    }
    void observe_adjacent(std::uint64_t address) {
        const auto line=address & ~std::uint64_t{63};
        const auto adjacent=line^std::uint64_t{64};
        if ((line>>12)==(adjacent>>12)) add(adjacent);
    }
    // Examine the oldest candidate before acquiring capacity; rejected
    // service must leave it queued and deduplicated.
    std::optional<std::uint64_t> peek() const {
        return queue_.empty() ? std::nullopt : std::optional<std::uint64_t>(queue_.front());
    }
    std::optional<std::uint64_t> dequeue() {
        if(queue_.empty()) return std::nullopt;
        const auto line=queue_.front();queue_.pop_front();queued_.erase(line);return line;
    }
    std::size_t queued() const {return queue_.size();}
    std::uint64_t duplicate_candidates() const {return duplicates_;}
    std::uint64_t overflow_candidates() const {return overflow_;}
  private:
    void add(std::optional<std::uint64_t> line) {
        if(!line)return;
        if(queued_.count(*line)){++duplicates_;return;}
        if(queue_limit_ && queue_.size()>=queue_limit_){++overflow_;return;}
        queued_.insert(*line);queue_.push_back(*line);
    }
    bool ip_enabled_;
    std::size_t queue_limit_;
    SprIpStride ip_;
    SprStreamPrefetch stream_;
    std::deque<std::uint64_t> queue_;
    std::unordered_set<std::uint64_t> queued_;
    std::uint64_t duplicates_=0,overflow_=0;
};
}
