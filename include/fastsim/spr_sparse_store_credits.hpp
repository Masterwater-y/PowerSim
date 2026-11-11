#pragma once

#include "fastsim/spr_ordered_store_pipeline.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <optional>
#include <stdexcept>

namespace fastsim {
// Independent single-pass model-clock summary. All admitted ordinary stores
// must be scheduled for offload in sequence/retirement order. Callers must
// provide a FINAL modeled response before sending; no native timestamps.
// Unknown/special stores share the budget and hold a FIFO barrier until the
// caller supplies their irrevocable modeled completion.
class SprSparseStoreCredits {
 public:
    using Store=SprCommittedStoreBuffer::Store;
    using Dispatch=SprOrderedStorePipeline::Dispatch;
    SprSparseStoreCredits(std::size_t budget,unsigned offload_width,
                          std::size_t line_window,std::uint64_t reclaim_delay,
                          std::size_t event_capacity)
        : pipeline_(budget,offload_width,line_window,reclaim_delay),
          width_(offload_width),capacity_(event_capacity) {
        if (!capacity_) throw std::invalid_argument("zero store event capacity");
    }
    bool admit_sq(std::uint64_t sequence,std::uint64_t now) {
        advance(now);
        if (last_admit_valid_ && sequence<=last_admit_)
            throw std::logic_error("store SQ admission sequence is not increasing");
        if (!pipeline_.admit_sq(now)) return false;
        unscheduled_.push_back(sequence);
        last_admit_=sequence;last_admit_valid_=true;
        return true;
    }
    std::optional<std::uint64_t> admit_special(std::uint64_t sequence,std::uint64_t now) {
        advance(now);
        if(last_admit_valid_ && sequence<=last_admit_)
            throw std::logic_error("store SQ admission sequence is not increasing");
        auto id=pipeline_.admit_special(sequence,now);
        if(id) {last_admit_=sequence;last_admit_valid_=true;}
        return id;
    }
    // Schedules once, without advancing the model clock. The offload width is
    // applied algebraically; no per-cycle retry or whole-window iteration.
    bool schedule_offload(const Store& store,std::uint64_t proposed) {
        if (unscheduled_.empty() || unscheduled_.front()!=store.sequence)
            throw std::logic_error("store offloads must follow admitted SQ order");
        if (proposed<frontier_ || (last_proposed_valid_ && proposed<last_proposed_))
            throw std::logic_error("store offload precedes ordered retirement");
        if (events_.size()>=capacity_) return false;
        auto at=std::max(proposed,last_assigned_);
        auto count=(assigned_valid_ && at==last_assigned_) ? assigned_count_ : 0u;
        if (count==width_) {
            if(at==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("store offload time overflow");
            ++at;count=0;
        }
        events_.push_back({store,at});
        unscheduled_.pop_front();
        last_assigned_=at;assigned_count_=count+1;assigned_valid_=true;
        last_proposed_=proposed;last_proposed_valid_=true;
        return true;
    }
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::logic_error("store event frontier moved backwards");
        // Visit only known event boundaries; a special barrier with no known
        // completion is not a same-cycle retry. Releases precede offloads.
        while(true) {
            auto next=next_transition();
            if(!next || *next>now) break;
            const auto at=std::max(frontier_,*next);
            pipeline_.advance(at);
            frontier_=at;
            if(special_completion_ && *special_completion_<=at) {
                pipeline_.complete_special(*active_special_,at);
                special_completion_.reset();active_special_.reset();
                blocked_by_special_=false;
            }
            if(!blocked_by_special_ && !events_.empty() && events_.front().time<=at) {
                auto& event=events_.front();
                const auto result=pipeline_.offload(event.store,at);
                if(result.accepted) events_.pop_front();
                else if(result.cycle>at) event.time=result.cycle;
                else blocked_by_special_=true;
            }
        }
        pipeline_.advance(now);
        frontier_=now;
    }
    std::optional<std::uint64_t> begin_special(std::uint64_t now) {
        advance(now);
        if(active_special_) return std::nullopt;
        auto id=pipeline_.begin_special(now);
        if(id) active_special_=id;
        return id;
    }
    bool schedule_special_completion(std::uint64_t id,std::optional<std::uint64_t> final_model_callback) {
        if(!active_special_ || *active_special_!=id || special_completion_)
            throw std::logic_error("invalid special store completion");
        if(!final_model_callback) return false;
        if(*final_model_callback<frontier_ || *final_model_callback==std::numeric_limits<std::uint64_t>::max())
            throw std::logic_error("invalid special store completion time");
        special_completion_=*final_model_callback;
        return true;
    }
    std::optional<Dispatch> send_next(std::uint64_t now,
                                      std::optional<std::uint64_t> final_model_callback) {
        advance(now);
        if (!final_model_callback) return std::nullopt;
        return pipeline_.send_next(now,*final_model_callback);
    }
    bool begin_external_boundary(std::uint64_t now) {
        advance(now);
        if(!events_.empty() || !unscheduled_.empty())return false;
        return pipeline_.begin_external_boundary(now);
    }
    void end_external_boundary(std::uint64_t now) {
        advance(now);pipeline_.end_external_boundary(now);
    }
    std::optional<std::uint64_t> next_transition() const {
        auto next=pipeline_.next_release_cycle();
        if(special_completion_ && (!next || *special_completion_<*next))next=special_completion_;
        if(!blocked_by_special_ && !events_.empty()) {
            const auto at=std::max(frontier_,events_.front().time);
            if(!next || at<*next)next=at;
        }
        return next;
    }
    std::size_t budget_used() const {return pipeline_.budget_used();}
    std::size_t queued_offloads() const {return events_.size();}
 private:
    struct Event {Store store;std::uint64_t time;};
    SprOrderedStorePipeline pipeline_;
    unsigned width_;
    std::size_t capacity_;
    std::deque<std::uint64_t> unscheduled_;
    std::deque<Event> events_;
    std::optional<std::uint64_t> active_special_,special_completion_;
    std::uint64_t frontier_=0,last_admit_=0,last_proposed_=0,last_assigned_=0;
    unsigned assigned_count_=0;
    bool last_admit_valid_=false,last_proposed_valid_=false,assigned_valid_=false,blocked_by_special_=false;
};
} // namespace fastsim
