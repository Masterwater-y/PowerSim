#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Observer-only estimate. A slot describes one modeled data-fill interval;
// it never installs a tag, delays a request, or counts an FB-hit event.
class RetiredCacheMissEstimator {
 public:
    struct Result { unsigned misses=0; bool merged=false, fallback=false; };
    explicit RetiredCacheMissEstimator(std::size_t capacity=4096):slots_(capacity) {
        if (!capacity) throw std::invalid_argument("empty retired miss shadow");
    }
    Result observe(std::uint64_t line,std::uint64_t issue,std::uint64_t response,
                   unsigned misses,bool forwarded=false,bool alias=false) {
        if (misses>7) throw std::invalid_argument("invalid retired miss mask");
        if (forwarded) return {0,false,false}; // complete local forwarding, no data request
        if (alias) return {0,true,false};
        auto& slot=slots_[(line^(line>>17))%slots_.size()];
        Result result{misses,false,false};
        if (slot.valid && slot.line==line) {
            if (slot.begin<=issue && issue<slot.end) return {0,true,false};
            if (issue<slot.begin) return {misses,false,true}; // future parent
        }
        if (!misses) return result;
        if (response<=issue) return {misses,false,true};
        auto uncertainty=slot.uncertain_until;
        if(slot.valid && slot.line!=line && issue<slot.end)
            uncertainty=std::max(uncertainty,slot.end);
        result.fallback=issue<uncertainty;
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
