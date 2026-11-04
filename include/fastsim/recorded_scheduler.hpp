#pragma once

#include <cstddef>
#include <cstdint>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace fastsim {

// Coordinator-owned replay of recorded logical-task ownership. Inputs are
// corrected target Q16 reference cycles, never source observations. Blocking a
// task changes its readiness; it does not stop the physical CPU trace stream.
class RecordedScheduler {
public:
    RecordedScheduler(std::vector<std::uint64_t> initial_owners,
                      std::vector<std::uint64_t> task_ids);

    // One warmup-to-measurement seam; owners and readiness survive, counters reset.
    void begin_measurement(std::uint64_t origin_reference_q16);
    // normalized_prev_state: 1 runnable, 2 proven blocked, 3 unknown.
    void switch_owner(std::size_t core, std::uint64_t prev, std::uint64_t next,
                      std::uint64_t normalized_prev_state,
                      std::uint64_t time_reference_q16);
    void wakeup(std::uint64_t pid);
    void finish_core(std::size_t core, std::uint64_t time_reference_q16);
    // Every core must first finish at its known completion. Common closure
    // validates continuity without extending any per-core counter.
    void finish_common(std::uint64_t common_end_reference_q16,
                       const std::vector<bool>& certified_owner_continuity);

    std::uint64_t owner(std::size_t core) const;
    const std::vector<std::uint64_t>& active_q16() const { return active_; }
    const std::vector<std::uint64_t>& other_q16() const { return other_; }
    const std::vector<std::uint64_t>& unknown_q16() const { return unknown_; }
    bool complete() const { return complete_; }

private:
    enum class TaskState { Running, Ready, Blocked, Unknown };

    void require_open() const;
    void validate_time(std::size_t core, std::uint64_t time) const;
    void accrue_owner(std::size_t core, std::uint64_t time);

    std::vector<std::uint64_t> owners_;
    std::unordered_set<std::uint64_t> tasks_;
    std::unordered_map<std::uint64_t, TaskState> states_;
    std::vector<std::uint64_t> last_, active_, other_, unknown_;
    std::vector<bool> finished_;
    bool measuring_ = false;
    bool closed_ = false;
    bool complete_ = false;
};

} // namespace fastsim
