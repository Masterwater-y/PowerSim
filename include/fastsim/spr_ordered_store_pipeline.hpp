#pragma once

#include "fastsim/spr_committed_store_buffer.hpp"
#include "fastsim/spr_ordered_store_visibility.hpp"
#include <algorithm>
#include <cstdint>
#include <deque>
#include <limits>
#include <optional>
#include <stdexcept>

namespace fastsim {
// Single-pass composition of the experimental buffer and ordered-visibility
// summaries. All times are model estimates, not gem5 event timestamps.
// Callers must supply an irrevocable modeled callback-ready time at acceptance;
// delayed/unknown future service is outside this interface. The resulting
// modeled completion is not a proof that native Ruby has already completed.
class SprOrderedStorePipeline {
 public:
    using Store = SprCommittedStoreBuffer::Store;
    using Offload = SprCommittedStoreBuffer::Offload;
    struct Dispatch {
        std::uint64_t id, model_visible, release;
    };
    SprOrderedStorePipeline(std::size_t budget, unsigned offload_width,
                            std::size_t line_window, std::uint64_t reclaim_delay)
        : buffer_(budget,offload_width,line_window), reclaim_delay_(reclaim_delay) {
        if (!reclaim_delay) throw std::invalid_argument("store reclaim delay must be positive");
    }
    void advance(std::uint64_t now) {
        if (now<frontier_) throw std::logic_error("store pipeline time moved backwards");
        while (!pending_.empty() && pending_.front().release<=now) {
            buffer_.complete(pending_.front().id,pending_.front().release);
            pending_.pop_front();
        }
        frontier_=now;
    }
    bool admit_sq(std::uint64_t now) {
        advance(now);
        return !external_boundary_ && buffer_.admit_sq();
    }
    std::optional<std::uint64_t> admit_special(std::uint64_t sequence,std::uint64_t now) {
        advance(now);
        if(external_boundary_) return std::nullopt;
        return buffer_.admit_special(sequence);
    }
    std::optional<std::uint64_t> begin_special(std::uint64_t now) {
        advance(now);
        if(external_boundary_) return std::nullopt;
        return buffer_.begin_special(now);
    }
    // The caller must pass the externally modeled final completion, not an
    // estimate that may later change. Younger stores use a conservative
    // one-cycle boundary because the special request's line is unknown.
    void complete_special(std::uint64_t id,std::uint64_t now) {
        if(now==std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("special store boundary time overflow");
        advance(now);
        buffer_.complete_special(id,now);
        send_not_before_=std::max(send_not_before_,now+1);
    }
    Offload offload(const Store& store,std::uint64_t now) {
        advance(now);
        if (external_boundary_) throw std::logic_error("external store boundary is active");
        auto result=buffer_.offload(store,now);
        if (result.accepted && !result.merged) unsent_.push_back({result.id,store.line});
        return result;
    }
    std::optional<Dispatch> send_next(std::uint64_t now,std::uint64_t modeled_callback_ready) {
        advance(now);
        if (external_boundary_ || now<send_not_before_) return std::nullopt;
        if (unsent_.empty()) return std::nullopt;
        // Preview the constant-size visibility state so a refused send cannot
        // advance ordering or consume an estimated callback.
        auto candidate=visibility_;
        auto timing=candidate.schedule(unsent_.front().line,
                                       std::max(now,modeled_callback_ready));
        if (timing.visibility_cycle > std::numeric_limits<std::uint64_t>::max()-reclaim_delay_)
            throw std::overflow_error("store reclaim time overflow");
        Dispatch result{unsent_.front().id,timing.visibility_cycle,
                        timing.visibility_cycle+reclaim_delay_};
        pending_.push_back({result.id,result.release});
        auto accepted=buffer_.send_next(now);
        if (!accepted) { pending_.pop_back(); return std::nullopt; }
        if (*accepted!=result.id) throw std::logic_error("store pipeline send order mismatch");
        unsent_.pop_front();
        visibility_=candidate;
        return result;
    }
    std::size_t budget_used() const {return buffer_.budget_used();}
    std::size_t pending_callbacks() const {return pending_.size();}
    std::optional<std::uint64_t> next_release_cycle() const {
        return pending_.empty() ? std::nullopt
                                : std::optional<std::uint64_t>(pending_.front().release);
    }
    // An unmodeled or special store uses an isolated conservative boundary.
    // The caller runs its existing external path only after this succeeds and
    // must end the boundary only at that path's modeled completion. This may
    // serialize more than gem5; it cannot reconstruct the unknown request.
    bool begin_external_boundary(std::uint64_t now) {
        advance(now);
        if (external_boundary_ || buffer_.budget_used()!=0) return false;
        external_boundary_=true;
        return true;
    }
    void end_external_boundary(std::uint64_t now) {
        advance(now);
        if (!external_boundary_) throw std::logic_error("no external store boundary");
        if (now==std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("external store boundary time overflow");
        send_not_before_=now+1; // unknown line: do not share its final cycle
        external_boundary_=false;
    }
 private:
    struct Unsent {std::uint64_t id,line;};
    struct Pending {std::uint64_t id,release;};
    SprCommittedStoreBuffer buffer_;
    SprOrderedStoreVisibility visibility_;
    std::deque<Unsent> unsent_;
    std::deque<Pending> pending_;
    std::uint64_t reclaim_delay_,frontier_=0,send_not_before_=0;
    bool external_boundary_=false;
};
} // namespace fastsim
