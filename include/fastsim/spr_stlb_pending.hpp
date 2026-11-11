#pragma once
#include "fastsim/spr_stlb.hpp"
#include "fastsim/spr_transaction_id.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <queue>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Independent per-core modeled-time adapter. Only certified complete MMU
// epochs, non-global 4K translations and PCID 0 are supported. No native
// TLB outcome or timestamp is read. The caller owns proof of every MMU event
// and the final modeled walker completion. Copies snapshot local state while
// sharing a non-rewinding ticket allocator for safe rollback.
class SprStlbPending {
 public:
    struct Key {
        std::uint64_t asid;
        unsigned pcid;
        std::uint64_t va;
        unsigned page_bits;
        bool global;
    };
    enum class Status {Unknown,Unsupported,Miss,HitPending,NotReady,Done,
                       Invalidated,Queued,Capacity,Stale,TimeRegression,InvalidTime};
    struct Result {Status status;std::uint64_t id=0,ready=0;};
    SprStlbPending(std::size_t capacity,std::uint64_t hit_latency)
        :capacity_(capacity),hit_latency_(hit_latency) {
        if(!capacity)throw std::invalid_argument("zero STLB pending capacity");
    }
    // true must be backed by complete functional control and a known empty
    // translation state at this boundary, not inferred from missing events.
    bool start_clean_epoch(std::uint64_t now,bool certified_empty) {
        if(!certified_empty || (ever_started_ && now<frontier_)) {
            poison();return false;
        }
        clear_pending();stlb_.flush_all();frontier_=now;valid_=true;ever_started_=true;
        return true;
    }
    Result lookup(const Key& key,std::uint64_t now) {
        const auto state=advance(now);
        if(state!=Status::Done)return {state};
        if(!supported(key)){poison();return {Status::Unsupported};}
        if(!stlb_.lookup(key.asid,key.pcid,key.va,false))return {Status::Miss};
        if(hits_.size()>=capacity_){poison();return {Status::Capacity};}
        if(now>std::numeric_limits<std::uint64_t>::max()-hit_latency_)
            {poison();return {Status::InvalidTime};}
        const auto id=ids_.allocate(),ready=now+hit_latency_;
        hits_.push_back(Hit{id,ready,stlb_.generation()});
        (void)stlb_.lookup(key.asid,key.pcid,key.va,true);
        return {Status::HitPending,id,ready};
    }
    // An unresolved response is never used as a fill. `ready` is an
    // irrevocable modeled walker completion, not an OoO reference event.
    Status schedule_fill(const Key& key,std::uint64_t now,std::uint64_t ready) {
        const auto state=advance(now);
        if(state!=Status::Done)return state;
        if(!supported(key)){poison();return Status::Unsupported;}
        if(ready<now){poison();return Status::InvalidTime;}
        if(fills_.size()>=capacity_){poison();return Status::Capacity;}
        if(next_fill_==std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("STLB fill identity overflow");
        fills_.push(Fill{ready,next_fill_++,stlb_.generation(),key});
        return Status::Queued;
    }
    Status complete_hit(std::uint64_t id,std::uint64_t now) {
        const auto state=advance(now);
        if(state!=Status::Done)return state;
        auto it=std::find_if(hits_.begin(),hits_.end(),[id](const Hit& h){return h.id==id;});
        if(it==hits_.end())return Status::Stale;
        if(now<it->ready)return Status::NotReady;
        const auto result=it->generation==stlb_.generation()?Status::Done:Status::Invalidated;
        hits_.erase(it);
        return result;
    }
    // Conservative per-core flush for a certified control event. Queued
    // walks are discarded; already-returned hit tokens are invalidated by
    // generation and can never authorize an L1 fill.
    Status invalidate_all(std::uint64_t now) {
        const auto state=advance(now);
        if(state!=Status::Done)return state;
        fills_={};stlb_.flush_all();return Status::Done;
    }
    // The caller must abandon before falling back on an unobserved lookup,
    // fill or control event which could change STLB membership.
    void abandon_unknown(){poison();}
 private:
    struct Fill {std::uint64_t ready,seq,generation;Key key;};
    struct Later {bool operator()(const Fill& a,const Fill& b)const {
        return a.ready!=b.ready ? a.ready>b.ready : a.seq>b.seq;
    }};
    struct Hit {std::uint64_t id,ready,generation;};
    static bool supported(const Key& key) {
        return key.pcid==0 && key.page_bits==12 && !key.global;
    }
    void clear_pending(){fills_={};hits_.clear();}
    void poison(){clear_pending();stlb_.flush_all();valid_=false;}
    Status advance(std::uint64_t now) {
        if(!valid_)return Status::Unknown;
        if(now<frontier_){poison();return Status::TimeRegression;}
        frontier_=now;
        while(!fills_.empty() && fills_.top().ready<=now) {
            const auto fill=fills_.top();fills_.pop();
            if(fill.generation==stlb_.generation())
                stlb_.insert(fill.key.asid,fill.key.pcid,fill.key.va,
                             fill.key.page_bits,fill.key.global);
        }
        return Status::Done;
    }
    SprStlb stlb_;
    SprTransactionId ids_;
    std::priority_queue<Fill,std::vector<Fill>,Later> fills_;
    std::vector<Hit> hits_;
    std::size_t capacity_;
    std::uint64_t hit_latency_,frontier_=0,next_fill_=1;
    bool valid_=false,ever_started_=false;
};
} // namespace fastsim
