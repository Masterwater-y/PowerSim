#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

namespace fastsim {

struct PrivateReadServiceCounters {
    std::uint64_t observed_events = 0;
    std::uint64_t candidate_loads = 0;
    std::uint64_t guarded_events = 0;
    std::uint64_t owners = 0;
    std::uint64_t followers = 0;
    std::uint64_t completed_hits = 0;
    std::uint64_t admission_rejected_loads = 0;
    std::uint64_t entry_rejected_loads = 0;
    std::uint64_t boundary_rejected_loads = 0;
    std::uint64_t response_added_cycles = 0;
    std::uint64_t response_removed_cycles = 0;

    PrivateReadServiceCounters& operator+=(const PrivateReadServiceCounters& v) {
        observed_events += v.observed_events;
        candidate_loads += v.candidate_loads;
        guarded_events += v.guarded_events;
        owners += v.owners;
        followers += v.followers;
        completed_hits += v.completed_hits;
        admission_rejected_loads += v.admission_rejected_loads;
        entry_rejected_loads += v.entry_rejected_loads;
        boundary_rejected_loads += v.boundary_rejected_loads;
        response_added_cycles += v.response_added_cycles;
        response_removed_cycles += v.response_removed_cycles;
        return *this;
    }
};

// A batch-local proof for read-only private-cache components. A component is
// the common L1D/L2 set index; exactly one physical line may touch it. Its first
// access owns a miss and every later access is a local hit. No cache mutation
// needs replay in this case: all replacement touches name the same line.
// Callers reject cross-core access, unsupported requests and carried effects.
// No time-based map/heap, historical line identity or cross-batch pointer exists.
class PrivateReadServices {
  public:
    struct Slot {
        std::uint64_t line = 0;
        std::size_t first_event = 0;
        std::uint64_t first_follower_floor = std::numeric_limits<std::uint64_t>::max();
        std::uint64_t count = 0;
        std::uint64_t admission = 0;
        std::uint64_t callback = 0;
        std::uint64_t response = 0;
        bool first_miss = false;
        bool blocked = false;
        bool candidate = false;
        bool published = false;
    };
    struct Result {
        std::uint64_t response;
        std::uint64_t callback;
        bool certified = false;
        bool follower = false;
    };

    void reset(std::size_t sets) {
        slots_.assign(sets, Slot{});
        counters = {};
        active_ = false;
    }

    void observe(std::size_t event, std::uint64_t line,
                 std::uint64_t request_floor, bool ordinary_read, bool l1_hit) {
        auto& s = slots_[line & (slots_.size() - 1)];
        ++counters.observed_events;
        if (s.count++ == 0) {
            s.line = line;
            s.first_event = event;
            s.first_miss = !l1_hit;
        } else {
            s.blocked |= s.line != line || !l1_hit;
            s.first_follower_floor = std::min(s.first_follower_floor, request_floor);
        }
        s.blocked |= !ordinary_read;
    }

    void seal(std::uint64_t future_floor, std::uint64_t entry_floor = 0) {
        future_floor_ = future_floor;
        entry_floor_ = entry_floor;
        for (auto& s : slots_) {
            s.candidate = s.count > 1 && s.first_miss && !s.blocked;
            if (s.candidate) {
                active_ = true;
                counters.candidate_loads += s.count;
            } else {
                counters.guarded_events += s.count;
            }
        }
    }

    bool active() const { return active_; }
    std::vector<Slot>& slots() { return slots_; }
    bool affects(std::uint64_t line) const {
        return active_ && slots_[line & (slots_.size() - 1)].candidate;
    }

    Result resolve(std::size_t event, std::uint64_t line,
                   std::uint64_t request, std::uint64_t local_response) {
        Result result{local_response, local_response, false, false};
        if (!active_) return result;
        auto& s = slots_[line & (slots_.size() - 1)];
        if (!s.candidate || s.line != line) return result;
        if (event == s.first_event) {
            if (request < entry_floor_) {
                counters.entry_rejected_loads += s.count;
                s.candidate = false;
                return result;
            }
            // The floor is taken before feedback. Every later request is at
            // least this late even if independent of this owner. Reject the
            // whole component before publishing if that cannot prove order.
            if (request > s.first_follower_floor) {
                counters.admission_rejected_loads += s.count;
                s.candidate = false;
                return result;
            }
            const auto callback = local_response > request ? local_response - 1 : request;
            // This first slice does not carry an owner over a batch boundary.
            // Prove completion precedes every unexamined request instead.
            if (callback > future_floor_) {
                counters.boundary_rejected_loads += s.count;
                s.candidate = false;
                return result;
            }
            s.admission = request;
            s.callback = callback;
            s.response = local_response;
            s.published = true;
            ++counters.owners;
        } else if (s.published) {
            if (request < s.admission) return result;
            if (request < s.callback) {
                result.response = s.response;
                result.follower = true;
                ++counters.followers;
                if (s.response > local_response)
                    counters.response_added_cycles += s.response - local_response;
                else
                    counters.response_removed_cycles += local_response - s.response;
            } else {
                ++counters.completed_hits;
            }
        } else {
            return result;
        }
        result.certified = true;
        result.callback = result.response > request ? result.response - 1 : request;
        return result;
    }

    PrivateReadServiceCounters counters;

  private:
    std::vector<Slot> slots_;
    std::uint64_t future_floor_ = 0;
    std::uint64_t entry_floor_ = 0;
    bool active_ = false;
};

} // namespace fastsim
