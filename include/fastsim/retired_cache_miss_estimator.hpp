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
    struct Result {
        unsigned misses=0; bool merged=false, fallback=false;
        unsigned fallback_reasons=0;
        unsigned estimated_mask=0;
    };
    explicit RetiredCacheMissEstimator(std::size_t capacity=4096):slots_(capacity) {
        if (!capacity) throw std::invalid_argument("empty retired miss shadow");
    }
    Result observe(std::uint64_t line,std::uint64_t issue,std::uint64_t response,
                   unsigned misses,bool forwarded=false,bool alias=false) {
        if (misses>7) throw std::invalid_argument("invalid retired miss mask");
        if (forwarded) {
            auto service=spr_retired::Result{};
            service.service=spr_retired::Service::Forwarded;
            const auto events=spr_retired::convert(service);
            return {events.miss_mask,false,false,0,events.estimated_mask};
        }
        auto& slot=slots_[(line^(line>>17))%slots_.size()];
        const auto converted=spr_retired::convert(spr_retired::ordinary(misses));
        Result result{converted.miss_mask,false,false,0,converted.estimated_mask};
        if (slot.valid && slot.line==line) {
            if (slot.begin<=issue && issue<slot.end) {
                auto service=spr_retired::ordinary(misses);
                service.service=spr_retired::Service::DemandFollower;
                // This table models L1 pending fills, not a parent miss count.
                service.level[0]=spr_retired::Level::PendingFill;
                service.supply=spr_retired::Supply::FillBuffer;
                const auto events=spr_retired::convert(service);
                return {events.miss_mask,true,false,0,events.estimated_mask};
            }
            if (issue<slot.begin) return {misses,false,true,kRetiredFallbackFutureParent}; // future parent
        }
        // Sequencer de-duplication does not prove a nonresident L1 fill.
        // Without a matching interval retain own results and report fallback.
        if (alias) return {result.misses,true,true,kRetiredFallbackUnclassified,7};
        if (!misses) return result;
        if (response<=issue) return {misses,false,true,kRetiredFallbackInvalidInterval};
        auto uncertainty=slot.uncertain_until;
        if(slot.valid && slot.line!=line && issue<slot.end)
            uncertainty=std::max(uncertainty,slot.end);
        result.fallback=issue<uncertainty;
        if (result.fallback) result.fallback_reasons |= kRetiredFallbackCollision;
        slot={line,issue,response,true,uncertainty};
        return result;
    }
 private:
    struct Slot {
        std::uint64_t line=0,begin=0,end=0;
        bool valid=false;
        std::uint64_t uncertain_until=0;
    };
    std::vector<Slot> slots_;
};
}
