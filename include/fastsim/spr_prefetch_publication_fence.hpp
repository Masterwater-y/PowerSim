#pragma once
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace fastsim {
// A certificate checker, NOT a source of timestamps. A bound must cover all
// future cache-relevant requests of the core, including resident and unseen
// instruction fetches, explicit control effects and uncommitted fragments.
// Each bound is irrevocable outside transaction rollback and cannot decrease.
// Copy this state with the cache, directory and prefetch transaction.
class SprPrefetchPublicationFence {
 public:
    explicit SprPrefetchPublicationFence(std::size_t cores):cores_(cores) {
        if (!cores) throw std::invalid_argument("publication fence needs a core");
    }
    void certify_future_lower_bound(std::size_t core,std::uint64_t lower) {
        auto& c=cores_.at(core);
        if (c.finished || (c.known && lower<c.lower))
            throw std::logic_error("future request certificate moved backwards");
        c.known=true;c.lower=lower;
    }
    // Only after the owner proves this core has no more cache-relevant effects.
    void finish(std::size_t core) {cores_.at(core).finished=true;}
    bool can_publish(std::uint64_t response,std::uint64_t now,bool transaction_committed) const {
        if (!transaction_committed || response>now) return false;
        for (const auto& c:cores_)
            if (!c.finished && (!c.known || c.lower<=response)) return false;
        return true;
    }
 private:
    struct Core {std::uint64_t lower=0;bool known=false,finished=false;};
    std::vector<Core> cores_;
};
} // namespace fastsim
