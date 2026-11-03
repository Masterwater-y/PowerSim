#pragma once

#include "fastsim/pending_fill.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <queue>
#include <stdexcept>
#include <tuple>
#include <vector>

namespace fastsim {

// Core-local, read-only lifecycle coordinator. The caller supplies unique
// request identities and owns rollback of effects emitted by the hooks; copying
// this object snapshots all internal scheduling state. Hooks must not reenter
// the coordinator. Exceptions from hooks invalidate the attempted advance;
// restore both the coordinator and hook-owned state before retrying.
//
// This first slice admits reads only and publishes data at the final callback.
// It deliberately does not model write permission or split/early responses.
// A full ledger defers a new line before invoking service (including a possible
// cache hit); integration must account for this conservative admission policy.
class LineGenerationCoordinator {
  public:
    enum class Access : std::uint8_t { kRead, kStore, kAtomic };
    enum class SubmitResult : std::uint8_t {
        kQueued, kReadyCapacityBlocked, kUnsupportedAccess
    };
    struct Request {
        std::uint64_t line = 0;
        std::uint64_t sequence = 0;
        std::uint64_t ready_cycle = 0;
        Access access = Access::kRead;
    };
    struct Service {
        std::uint64_t callback_cycle = 0;
        std::uint64_t read_visible_cycle = 0;
        // Opaque value, never an external pointer: snapshots remain independent.
        std::optional<std::uint64_t> fill_token;
    };
    struct Admission {
        Request request;
        std::uint64_t admission_cycle = 0;
        std::uint64_t generation = 0;
        std::uint64_t leader_sequence = 0;
        std::uint64_t response_cycle = 0;
        bool follower = false;
    };
    struct Callback {
        std::uint64_t line = 0;
        std::uint64_t generation = 0;
        std::uint64_t leader_sequence = 0;
        std::uint64_t admission_cycle = 0;
        std::uint64_t callback_cycle = 0;
        std::uint64_t read_visible_cycle = 0;
        std::optional<std::uint64_t> fill_token;
        bool operator>(const Callback& other) const {
            return std::tie(callback_cycle, generation) >
                std::tie(other.callback_cycle, other.generation);
        }
    };
    struct Counters {
        std::uint64_t submissions = 0;
        std::uint64_t ready_capacity_blocks = 0;
        std::uint64_t unsupported_accesses = 0;
        std::uint64_t admissions = 0;
        std::uint64_t leaders = 0;
        std::uint64_t followers = 0;
        std::uint64_t service_allocations = 0;
        std::uint64_t capacity_retries = 0;
        std::uint64_t callbacks = 0;
        std::size_t max_ready = 0;
        std::size_t max_callbacks = 0;
        std::size_t max_active = 0;
    };

    LineGenerationCoordinator(std::size_t active_capacity,
                              std::size_t ready_capacity,
                              std::uint64_t initial_cycle = 0)
        : ledger_(active_capacity, initial_cycle),
          ready_capacity_(ready_capacity), now_(initial_cycle) {
        if (ready_capacity == 0)
            throw std::invalid_argument("coordinator ready capacity is zero");
    }

    SubmitResult submit(const Request& request) {
        if (request.ready_cycle < now_)
            throw std::logic_error("coordinator request precedes current time");
        if (request.access != Access::kRead) {
            bump(counters_.unsupported_accesses);
            return SubmitResult::kUnsupportedAccess;
        }
        if (ready_.size() == ready_capacity_) {
            bump(counters_.ready_capacity_blocks);
            return SubmitResult::kReadyCapacityBlocked;
        }
        if (ticket_ == std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("coordinator request ticket exhausted");
        ready_.push(Ready{request.ready_cycle, ++ticket_, request});
        bump(counters_.submissions);
        counters_.max_ready = std::max(counters_.max_ready, ready_.size());
        return SubmitResult::kQueued;
    }

    // allocate_service(request, admission_cycle) is called exactly once per
    // new generation, never for a follower or capacity retry. on_admission
    // owns completion/dependent-ready publication; on_callback owns cache fill.
    // Callback events at t run before every admission at t. Outputs are streamed
    // to hooks, so completed requests never accumulate in this coordinator.
    template <class AllocateService, class OnAdmission, class OnCallback>
    void advance_to(std::uint64_t cycle, AllocateService&& allocate_service,
                    OnAdmission&& on_admission, OnCallback&& on_callback) {
        if (cycle < now_)
            throw std::logic_error("coordinator time moved backwards");
        while (true) {
            const bool callback_due = !callbacks_.empty() &&
                callbacks_.top().callback_cycle <= cycle;
            const bool ready_due = !ready_.empty() && ready_.top().cycle <= cycle;
            if (!callback_due && !ready_due) break;
            if (callback_due && (!ready_due ||
                callbacks_.top().callback_cycle <= ready_.top().cycle)) {
                const auto callback = callbacks_.top();
                callbacks_.pop();
                now_ = callback.callback_cycle;
                ledger_.advance_to(now_);
                bump(counters_.callbacks);
                on_callback(callback);
                continue;
            }
            auto ready = ready_.top();
            now_ = ready.cycle;
            ledger_.advance_to(now_);
            const auto* entry = ledger_.find(ready.request.line);
            if (!entry && ledger_.size() == ledger_.capacity()) {
                // No service side effects and no ledger reservation. There is
                // exactly one ready node for this request throughout retries.
                if (callbacks_.empty() || callbacks_.top().callback_cycle <= now_)
                    throw std::logic_error("full coordinator lacks future callback");
                ready_.pop();
                ready.cycle = callbacks_.top().callback_cycle;
                ready_.push(ready);
                bump(counters_.capacity_retries);
                continue;
            }
            Service service;
            if (!entry) {
                service = allocate_service(ready.request, now_);
                if (service.callback_cycle < now_ ||
                    service.read_visible_cycle != service.callback_cycle)
                    throw std::logic_error("unsupported early/split read response");
                bump(counters_.service_allocations);
            }
            const auto decision = ledger_.try_admit(
                LineGenerationLedger::Request::read(
                    ready.request.line, ready.request.sequence, now_,
                    service.callback_cycle, service.read_visible_cycle));
            if (!decision.admitted())
                throw std::logic_error("eligible coordinator admission was rejected");
            ready_.pop();
            bump(counters_.admissions);
            const bool follower = decision.attached();
            if (follower) {
                bump(counters_.followers);
            } else {
                bump(counters_.leaders);
                callbacks_.push(Callback{
                    ready.request.line, decision.generation,
                    decision.leader_sequence, now_, service.callback_cycle,
                    service.read_visible_cycle, service.fill_token});
                counters_.max_callbacks = std::max(
                    counters_.max_callbacks, callbacks_.size());
                counters_.max_active = std::max(
                    counters_.max_active, ledger_.size());
            }
            on_admission(Admission{ready.request, now_, decision.generation,
                decision.leader_sequence, decision.response_cycle, follower});
        }
        now_ = cycle;
        ledger_.advance_to(cycle);
    }

    std::uint64_t now() const { return now_; }
    std::size_t ready_size() const { return ready_.size(); }
    std::size_t active_size() const { return ledger_.size(); }
    std::size_t callback_size() const { return callbacks_.size(); }
    const Counters& counters() const { return counters_; }
    const LineGenerationLedger& ledger() const { return ledger_; }

    // Debug/validation only: copies bounded heaps; never part of request replay.
    bool invariants_hold() const {
        if (ready_.size() > ready_capacity_ ||
            callbacks_.size() > ledger_.capacity() ||
            ledger_.size() != callbacks_.size() ||
            counters_.submissions != counters_.admissions + ready_.size() ||
            counters_.admissions != counters_.leaders + counters_.followers ||
            counters_.leaders != counters_.callbacks + callbacks_.size() ||
            counters_.service_allocations != counters_.leaders ||
            !ledger_.invariants_hold() || !ledger_.expiry_accounting_conserved())
            return false;
        auto callbacks = callbacks_;
        while (!callbacks.empty()) {
            const auto callback = callbacks.top();
            callbacks.pop();
            const auto* entry = ledger_.find(callback.line);
            if (!entry || entry->generation != callback.generation ||
                entry->callback_cycle != callback.callback_cycle ||
                entry->leader_sequence != callback.leader_sequence ||
                callback.callback_cycle <= now_) return false;
        }
        auto ready = ready_;
        while (!ready.empty()) {
            if (ready.top().cycle < now_) return false;
            ready.pop();
        }
        return true;
    }

  private:
    static void bump(std::uint64_t& value) {
        if (value != std::numeric_limits<std::uint64_t>::max()) ++value;
    }

    struct Ready {
        std::uint64_t cycle;
        std::uint64_t ticket;
        Request request;
        bool operator>(const Ready& other) const {
            return std::tie(cycle, ticket) > std::tie(other.cycle, other.ticket);
        }
    };
    LineGenerationLedger ledger_;
    std::size_t ready_capacity_;
    std::uint64_t now_;
    std::uint64_t ticket_ = 0;
    Counters counters_;
    std::priority_queue<Ready, std::vector<Ready>, std::greater<Ready>> ready_;
    std::priority_queue<Callback, std::vector<Callback>,
                        std::greater<Callback>> callbacks_;
};

}  // namespace fastsim
