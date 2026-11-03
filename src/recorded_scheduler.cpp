#include "fastsim/recorded_scheduler.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>

namespace fastsim {
namespace {
void check_add(std::uint64_t count, std::uint64_t delta) {
    if (delta > std::numeric_limits<std::uint64_t>::max() - count)
        throw std::overflow_error("recorded scheduler duration overflow");
}
} // namespace

RecordedScheduler::RecordedScheduler(std::vector<std::uint64_t> initial_owners,
                                     std::vector<std::uint64_t> task_ids)
    : owners_(std::move(initial_owners)), tasks_(task_ids.begin(), task_ids.end()),
      last_(owners_.size()), active_(owners_.size()), other_(owners_.size()),
      unknown_(owners_.size()), finished_(owners_.size(), false) {
    if (owners_.empty()) throw std::invalid_argument("recorded scheduler needs a physical core");
    if (tasks_.count(0)) throw std::invalid_argument("recorded scheduler zero workload task ID");
    for (const auto pid : owners_) {
        // Zero denotes the recorded idle context, not an unknown owner. Idle
        // contexts may occur on multiple physical CPUs and count as other time.
        if (pid && !states_.emplace(pid, TaskState::Running).second)
            throw std::runtime_error("recorded scheduler duplicate initial CPU owner");
    }
}

void RecordedScheduler::require_open() const {
    if (closed_) throw std::logic_error("recorded scheduler already closed");
}

void RecordedScheduler::validate_time(std::size_t core, std::uint64_t time) const {
    if (time < last_.at(core))
        throw std::runtime_error("recorded scheduler boundary time regressed");
}

void RecordedScheduler::begin_measurement(std::uint64_t origin_reference_q16) {
    require_open();
    if (measuring_) throw std::logic_error("recorded scheduler measurement already started");
    for (std::size_t core = 0; core < owners_.size(); ++core)
        validate_time(core, origin_reference_q16);
    std::fill(last_.begin(), last_.end(), origin_reference_q16);
    std::fill(active_.begin(), active_.end(), 0);
    std::fill(other_.begin(), other_.end(), 0);
    std::fill(unknown_.begin(), unknown_.end(), 0);
    measuring_ = true;
}

void RecordedScheduler::accrue_owner(std::size_t core, std::uint64_t time) {
    validate_time(core, time);
    if (measuring_) {
        auto& count = tasks_.count(owners_[core]) ? active_[core] : other_[core];
        const auto delta = time - last_[core];
        check_add(count, delta);
        count += delta;
    }
    last_[core] = time;
}

void RecordedScheduler::switch_owner(std::size_t core, std::uint64_t prev,
                                     std::uint64_t next, std::uint64_t normalized_prev_state,
                                     std::uint64_t time_reference_q16) {
    require_open();
    if (finished_.at(core)) throw std::logic_error("recorded scheduler physical core already finished");
    if (owners_[core] != prev || normalized_prev_state < 1 || normalized_prev_state > 3)
        throw std::runtime_error("recorded scheduler owner/state mismatch");
    if (prev && prev == next && normalized_prev_state == 2)
        throw std::runtime_error("recorded scheduler blocked switch retained its running owner");
    const auto next_state = states_.find(next);
    if (next && next_state != states_.end() && next_state->second == TaskState::Blocked)
        throw std::runtime_error("recorded scheduler blocked task resumed without wake evidence");
    for (std::size_t other = 0; next && other < owners_.size(); ++other)
        if (other != core && owners_[other] == next)
            throw std::runtime_error("recorded scheduler task already running on another CPU");

    accrue_owner(core, time_reference_q16);
    if (prev) {
        states_[prev] = normalized_prev_state == 1 ? TaskState::Ready :
                        normalized_prev_state == 2 ? TaskState::Blocked : TaskState::Unknown;
    }
    if (next) states_[next] = TaskState::Running;
    owners_[core] = next;
}

void RecordedScheduler::wakeup(std::uint64_t pid) {
    require_open();
    if (!pid) return;
    const auto found = states_.find(pid);
    // A wake of a currently running task is an observation, not a sched-out.
    if (found == states_.end() || found->second != TaskState::Running)
        states_[pid] = TaskState::Ready;
}

void RecordedScheduler::finish_core(std::size_t core, std::uint64_t time_reference_q16) {
    require_open();
    if (!measuring_) throw std::logic_error("recorded scheduler finish before measurement");
    if (finished_.at(core)) throw std::logic_error("recorded scheduler physical core already finished");
    accrue_owner(core, time_reference_q16);
    finished_[core] = true;
}

void RecordedScheduler::finish_common(std::uint64_t common_end_reference_q16,
                                     const std::vector<bool>& certified_owner_continuity) {
    require_open();
    if (!measuring_) throw std::logic_error("recorded scheduler finish before measurement");
    if (certified_owner_continuity.size() != owners_.size())
        throw std::invalid_argument("recorded scheduler common closure core count mismatch");
    // Validate every core before changing any count, so malformed closure never
    // partially certifies or extends otherwise valid cores.
    for (std::size_t core = 0; core < owners_.size(); ++core) {
        if (!finished_[core]) throw std::logic_error("recorded scheduler core completion missing");
        validate_time(core, common_end_reference_q16);
        const auto delta = common_end_reference_q16 - last_[core];
        const auto count = !certified_owner_continuity[core] ? unknown_[core] :
                           tasks_.count(owners_[core]) ? active_[core] : other_[core];
        check_add(count, delta);
    }
    complete_ = true;
    for (std::size_t core = 0; core < owners_.size(); ++core) {
        if (certified_owner_continuity[core]) {
            accrue_owner(core, common_end_reference_q16);
        } else {
            unknown_[core] += common_end_reference_q16 - last_[core];
            last_[core] = common_end_reference_q16;
            complete_ = false;
        }
        if (unknown_[core]) complete_ = false;
    }
    closed_ = true;
}

std::uint64_t RecordedScheduler::owner(std::size_t core) const {
    return owners_.at(core);
}

} // namespace fastsim
