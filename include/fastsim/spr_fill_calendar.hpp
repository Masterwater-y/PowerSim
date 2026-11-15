#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

namespace fastsim {

// Per-core, per-level host-side interval reservations. The finite slot count
// models a resource capacity; max_records is only a host memory bound. No
// global event queue, cycle polling, or iterative whole-window replay.
class SprFillCalendar {
  public:
    struct Ticket {
        std::uint64_t admission = 0;
        std::uint64_t id = 0;
        std::size_t slot = 0;
        bool valid = false;
    };

    SprFillCalendar() = default;
    SprFillCalendar(std::size_t capacity, std::size_t max_records)
        : slots_(capacity), max_records_(max_records) {
        if (!capacity || max_records < capacity)
            throw std::invalid_argument("invalid SPR fill calendar bounds");
    }

    void prune(std::uint64_t watermark) {
        for (auto& slot : slots_) {
            const auto old = slot.size();
            slot.erase(std::remove_if(slot.begin(), slot.end(),
                [watermark](const Interval& i) { return i.end <= watermark; }),
                slot.end());
            records_ -= old - slot.size();
        }
    }

    Ticket reserve(std::uint64_t proposed, std::uint64_t duration) {
        if (slots_.empty()) throw std::logic_error("uninitialized SPR fill calendar");
        if (records_ >= max_records_) return Ticket{proposed, 0, 0, false};
        duration = std::max<std::uint64_t>(1, duration);
        std::size_t selected = 0;
        auto best = std::numeric_limits<std::uint64_t>::max();
        for (std::size_t i = 0; i < slots_.size(); ++i) {
            auto start = proposed;
            for (const auto& entry : slots_[i]) {
                if (entry.end <= start) continue;
                if (add(start, duration) <= entry.start) break;
                start = entry.end;
            }
            if (start < best) { best = start; selected = i; }
            if (best == proposed) break;
        }
        auto& slot = slots_[selected];
        const auto id = ++next_id_;
        const Interval interval{best, add(best, duration), id};
        const auto pos = std::lower_bound(slot.begin(), slot.end(), best,
            [](const Interval& entry, std::uint64_t value) { return entry.start < value; });
        slot.insert(pos, interval);
        ++records_;
        return Ticket{best, id, selected, true};
    }

    // Returns true when a corrected response extends into an already accepted
    // future reservation. No past decision is replayed; callers report this
    // approximation and the overlapping intervals remain visible to later work.
    bool finish(const Ticket& ticket, std::uint64_t response) {
        if (!ticket.valid) return false;
        auto& slot = slots_.at(ticket.slot);
        auto it = std::find_if(slot.begin(), slot.end(),
            [&ticket](const Interval& entry) { return entry.id == ticket.id; });
        if (it == slot.end() || response < it->start)
            throw std::logic_error("invalid SPR fill completion");
        bool conflict = false;
        for (auto next = it + 1; next != slot.end() && next->start < response; ++next)
            conflict = conflict || next->end > it->start;
        it->end = response;
        return conflict;
    }

  private:
    struct Interval { std::uint64_t start, end, id; };
    static std::uint64_t add(std::uint64_t a, std::uint64_t b) {
        return b > std::numeric_limits<std::uint64_t>::max() - a
            ? std::numeric_limits<std::uint64_t>::max() : a + b;
    }
    std::vector<std::vector<Interval>> slots_;
    std::size_t max_records_ = 0;
    std::size_t records_ = 0;
    std::uint64_t next_id_ = 0;
};
} // namespace fastsim
