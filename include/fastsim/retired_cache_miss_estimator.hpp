#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>
#include "fastsim/retired_cache_fallback.hpp"
#include "fastsim/spr_retired_load_miss.hpp"

namespace fastsim {
// Observer-only estimate. A slot describes one modeled data-fill interval;
// it never installs a tag, delays a request, or counts an FB-hit event.
class RetiredCacheMissEstimator {
 public:
    enum class Decision : unsigned { Forwarded, Pending, Future, Alias, Hit, Invalid, Install };
    struct Audit {
        Decision decision=Decision::Hit;
        std::uint64_t begin=0,end=0,prior_line=0,install_begin=0;
        std::uint64_t owner_ordinal=UINT64_MAX,shadow_generation=0;
        bool valid=false;
    };
    struct Result {
        unsigned misses=0; bool merged=false, fallback=false;
        unsigned fallback_reasons=0;
        unsigned estimated_mask=0;
    };
    explicit RetiredCacheMissEstimator(std::size_t capacity=4096):slots_(capacity) {
        if (!capacity) throw std::invalid_argument("empty retired miss shadow");
    }
    Result observe(std::uint64_t line,std::uint64_t issue,std::uint64_t response,
                   unsigned misses,bool forwarded=false,bool alias=false,Audit* audit=nullptr,std::uint64_t service_begin=UINT64_MAX,
                   std::uint64_t owner_ordinal=UINT64_MAX) {
        if(audit)*audit=Audit{};
        if (misses>7) throw std::invalid_argument("invalid retired miss mask");
        if (forwarded) {
            if(audit)audit->decision=Decision::Forwarded;
            auto service=spr_retired::Result{};
            service.service=spr_retired::Service::Forwarded;
            const auto events=spr_retired::convert(service);
            return {events.miss_mask,false,false,0,events.estimated_mask};
        }
        const auto index=(line^(line>>17))%slots_.size();
        auto& slot=slots_[index];
        if((audit || owner_ordinal!=UINT64_MAX) && owners_.empty()) owners_.resize(slots_.size());
        if(audit){audit->begin=slot.begin;audit->end=slot.end;
            audit->prior_line=slot.line;audit->valid=slot.valid;
            if(!owners_.empty()){audit->owner_ordinal=owners_[index].ordinal;
                audit->shadow_generation=owners_[index].generation;}}
        const auto converted=spr_retired::convert(spr_retired::ordinary(misses));
        Result result{converted.miss_mask,false,false,0,converted.estimated_mask};
        if (slot.valid && slot.line==line) {
            if (slot.begin<=issue && issue<slot.end) {
                if(audit)audit->decision=Decision::Pending;
                auto service=spr_retired::ordinary(misses);
                service.service=spr_retired::Service::DemandFollower;
                // This table models L1 pending fills, not a parent miss count.
                service.level[0]=spr_retired::Level::PendingFill;
                service.supply=spr_retired::Supply::FillBuffer;
                const auto events=spr_retired::convert(service);
                return {events.miss_mask,true,false,0,events.estimated_mask};
            }
            if (issue<slot.begin) {if(audit)audit->decision=Decision::Future;
                return {misses,false,true,kRetiredFallbackFutureParent};} // future parent
        }
        // Sequencer de-duplication does not prove a nonresident L1 fill.
        // Without a matching interval retain own results and report fallback.
        if (alias) {if(audit)audit->decision=Decision::Alias;
            return {result.misses,true,true,kRetiredFallbackUnclassified,7};}
        if (!misses) return result;
        const auto fill_begin=service_begin==UINT64_MAX?issue:service_begin;
        if (response<=fill_begin) {if(audit)audit->decision=Decision::Invalid;
            return {misses,false,true,kRetiredFallbackInvalidInterval};}
        auto uncertainty=slot.uncertain_until;
        if(slot.valid && slot.line!=line && issue<slot.end)
            uncertainty=std::max(uncertainty,slot.end);
        result.fallback=issue<uncertainty;
        if (result.fallback) result.fallback_reasons |= kRetiredFallbackCollision;
        slot={line,fill_begin,response,true,uncertainty};
        if(!owners_.empty())owners_[index]={owner_ordinal,++generation_};
        if(audit){audit->decision=Decision::Install;audit->install_begin=fill_begin;}
        return result;
    }
 private:
    struct Slot {
        std::uint64_t line=0,begin=0,end=0;
        bool valid=false;
        std::uint64_t uncertain_until=0;
    };
    struct Owner {std::uint64_t ordinal=UINT64_MAX,generation=0;};
    std::vector<Slot> slots_;
    std::vector<Owner> owners_;
    std::uint64_t generation_=0;
};
}
