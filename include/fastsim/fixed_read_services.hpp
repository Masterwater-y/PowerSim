#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <unordered_map>

namespace fastsim {

// Per-core, experimental fixed-path read identities. Times are reference
// cycles; current_index is an epoch-local index, never a persistent pointer.
class FixedReadServices {
  public:
    static constexpr std::size_t incoming = std::numeric_limits<std::size_t>::max();
    struct Entry {
        std::uint64_t generation = 0;
        std::uint64_t sequence = 0;
        std::uint32_t ordinal = 0;
        std::size_t current_index = incoming;
        std::uint64_t admission = 0, callback = 0, response = 0;
        std::uint32_t level = 0;
    };

    void begin_epoch(std::uint64_t earliest_request) {
        for (auto it = entries_.begin(); it != entries_.end();) {
            if (it->second.callback <= earliest_request) it = entries_.erase(it);
            else {
                it->second.current_index = incoming;
                ++it;
            }
        }
    }
    const Entry* find(std::uint64_t line, std::uint64_t arrival) const {
        const auto it = entries_.find(line);
        if (it == entries_.end() || arrival >= it->second.callback) return nullptr;
        // Program order selects the owner. Arrival reversal is an explicit
        // approximation, diagnosed by the caller, not a reason to iterate.
        return &it->second;
    }
    Entry& insert(std::uint64_t line, Entry entry) {
        if (next_generation_ == std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("fixed read generation exhausted");
        entry.generation = ++next_generation_;
        return entries_.insert_or_assign(line, entry).first->second;
    }
    std::size_t invalidate(std::uint64_t line) { return entries_.erase(line); }
    std::size_t invalidate_range(std::uint64_t first, std::uint64_t count) {
        std::size_t removed = 0;
        for (auto it = entries_.begin(); it != entries_.end();) {
            if (it->first >= first && it->first - first < count) {
                it = entries_.erase(it);
                ++removed;
            } else ++it;
        }
        return removed;
    }
    std::size_t size() const { return entries_.size(); }
    const std::unordered_map<std::uint64_t, Entry>& entries() const { return entries_; }
    void publish(std::uint64_t line, std::uint64_t generation,
                 std::uint64_t admission, std::uint64_t response,
                 std::uint64_t callback_edge) {
        auto it = entries_.find(line);
        if (it == entries_.end() || it->second.generation != generation) return;
        it->second.admission = admission;
        it->second.response = response;
        it->second.callback = response > callback_edge ? response - callback_edge : 0;
        it->second.current_index = incoming;
    }

  private:
    std::unordered_map<std::uint64_t, Entry> entries_;
    std::uint64_t next_generation_ = 0;
};

} // namespace fastsim
