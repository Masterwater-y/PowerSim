#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Sorted, nonoverlapping half-open reservations. A retained prefix index
// allows cheap expiry; occasional compaction bounds stale host bookkeeping.
class SprPortCalendar {
  public:
    void prune(std::uint64_t lower_bound) {
        while (first_ < intervals_.size() && intervals_[first_].end <= lower_bound)
            ++first_;
        if (first_ >= 128 && first_ >= intervals_.size() / 2) {
            intervals_.erase(intervals_.begin(), intervals_.begin() +
                static_cast<std::ptrdiff_t>(first_));
            first_ = 0;
        }
    }
    bool available(std::uint64_t begin, std::uint64_t duration) const {
        const auto end = checked_end(begin, duration);
        auto first = intervals_.begin() + static_cast<std::ptrdiff_t>(first_);
        auto next = std::lower_bound(first, intervals_.end(), begin,
            [](const Interval& interval, std::uint64_t time) { return interval.begin < time; });
        if (next != first && (next - 1)->end > begin) return false;
        return next == intervals_.end() || next->begin >= end;
    }
    void reserve(std::uint64_t begin, std::uint64_t duration) {
        const auto end = checked_end(begin, duration);
        if (!available(begin, duration)) throw std::logic_error("overlapping SPR port reservation");
        auto first = intervals_.begin() + static_cast<std::ptrdiff_t>(first_);
        auto position = std::lower_bound(first, intervals_.end(), begin,
            [](const Interval& interval, std::uint64_t time) { return interval.begin < time; });
        intervals_.insert(position, Interval{begin, end});
    }
    std::size_t live() const { return intervals_.size() - first_; }
  private:
    struct Interval { std::uint64_t begin, end; };
    static std::uint64_t checked_end(std::uint64_t begin, std::uint64_t duration) {
        if (!duration || begin > std::numeric_limits<std::uint64_t>::max() - duration)
            throw std::overflow_error("invalid SPR port reservation interval");
        return begin + duration;
    }
    std::vector<Interval> intervals_;
    std::size_t first_ = 0;
};
} // namespace fastsim
