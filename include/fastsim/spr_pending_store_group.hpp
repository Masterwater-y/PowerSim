#pragma once
#include "fastsim/spr_store_input.hpp"
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>

namespace fastsim {
// Stage-one model facts only. Eligibility must exclude all special operations
// and boundaries. A rank gap covers any other core's memory event; a UOP gap
// also covers fences/NoAccess UOPs with no memory event of their own.
struct SprStoreMergeCandidate {
    std::uint32_t core=0;
    std::uint64_t chunk=0,batch_rank=0,uop=0,retire_q16=0;
    bool valid_plain=false;
    std::optional<SprStoreInput> facts;
};
inline bool spr_store_merge_pair(const SprStoreMergeCandidate& a,
                                 const SprStoreMergeCandidate& b,
                                 std::uint64_t cycle_unit) {
    if(!cycle_unit || !a.valid_plain || !b.valid_plain || !a.facts || !b.facts ||
       a.core!=b.core || a.chunk!=b.chunk ||
       a.batch_rank==std::numeric_limits<std::uint64_t>::max() ||
       a.uop==std::numeric_limits<std::uint64_t>::max() ||
       b.batch_rank!=a.batch_rank+1 || b.uop!=a.uop+1 ||
       a.retire_q16/cycle_unit!=b.retire_q16/cycle_unit)return false;
    return a.facts->line==b.facts->line && a.facts->context==b.facts->context &&
        a.facts->requestor==b.facts->requestor && a.facts->flags==b.facts->flags;
}

// Small, opt-in, phase-two scratch for one already frozen group. The owner
// must hold the corresponding SQ slots in its own transactional timing state
// until complete() returns the release edges. Up to 4 adjacent ordinary source
// stores are processed once in program order. Every time is a FastSim estimate,
// not native OoO information. Caller decides the group's membership before
// canonical replay; no cache/directory state or global clock is changed here.
class SprPendingStoreGroup {
 public:
    static constexpr std::size_t kMaximum=4;
    struct Completion {
        std::size_t count=0;
        std::array<std::uint32_t,kMaximum> slots{};
        std::array<std::uint64_t,kMaximum> releases{};
        std::uint64_t offload=0,send=0,callback=0;
    };
    SprPendingStoreGroup(std::size_t expected,std::size_t sq_slots,
                         std::uint64_t permission_ready,std::uint64_t prior_offload_floor,
                         std::uint64_t prior_send_floor,
                         std::uint64_t commit_to_offload,std::uint64_t local_store_latency)
        :expected_(expected),sq_slots_(sq_slots),permission_ready_(permission_ready),
         prior_offload_floor_(prior_offload_floor),prior_send_floor_(prior_send_floor),commit_to_offload_(commit_to_offload),
         local_store_latency_(local_store_latency) {
        if(expected<2 || expected>kMaximum || expected>sq_slots || !local_store_latency)
            throw std::invalid_argument("invalid bounded store group");
    }
    // An occupied SQ slot must first reach its externally calculated admission
    // edge. A blocked hold changes no group state or future capacity.
    bool hold(std::uint32_t slot,std::uint64_t previous_release,
              std::uint64_t actual_dispatch,std::uint64_t actual_retire) {
        if(completed_ || count_==expected_)throw std::logic_error("store group is closed");
        if(slot>=sq_slots_)throw std::out_of_range("store group SQ slot");
        if(actual_retire<actual_dispatch || (count_ && actual_retire<last_retire_))
            throw std::invalid_argument("store group retirement order invalid");
        for(std::size_t i=0;i<count_;++i)if(slots_[i]==slot)
            throw std::logic_error("duplicate held SQ slot");
        if(previous_release>actual_dispatch)return false;
        slots_[count_++]=slot;last_retire_=actual_retire;return true;
    }
    bool ready()const{return count_==expected_ && !completed_;}
    std::size_t held()const{return count_;}
    Completion complete() {
        if(!ready())throw std::logic_error("store group is not ready");
        Completion result;result.count=count_;result.slots=slots_;
        // The supplied bound must include all older SQ/barrier and offload
        // width restrictions; retirement alone does not prove SQ-head status.
        result.offload=std::max(prior_offload_floor_,add(last_retire_,commit_to_offload_));
        result.send=std::max(result.offload,prior_send_floor_);
        result.callback=std::max(permission_ready_,add(result.send,local_store_latency_));
        result.releases[0]=result.callback;
        for(std::size_t i=1;i<count_;++i)result.releases[i]=result.offload;
        completed_=true;return result;
    }
 private:
    static std::uint64_t add(std::uint64_t a,std::uint64_t b) {
        if(b>std::numeric_limits<std::uint64_t>::max()-a)
            throw std::overflow_error("store group time overflow");
        return a+b;
    }
    std::size_t expected_,sq_slots_,count_=0;
    std::uint64_t permission_ready_,prior_offload_floor_,prior_send_floor_,commit_to_offload_,local_store_latency_;
    std::uint64_t last_retire_=0;
    bool completed_=false;
    std::array<std::uint32_t,kMaximum> slots_{};
};
} // namespace fastsim
