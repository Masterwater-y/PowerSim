#pragma once

#include <algorithm>
#include <cstdint>
#include <functional>
#include <limits>
#include <queue>
#include <stdexcept>
#include <tuple>
#include <unordered_map>
#include <vector>

namespace fastsim {

// Core-local response availability, distinct from the functional tag array.
// Entries contain FINAL response times from the accepted core timing pass.
// Copies are transaction-local; rejecting a timing pass cannot publish fills.
class PendingFillTable {
  public:
    struct Entry {
        std::uint64_t admission = 0;
        std::uint64_t response = 0;
        // A write permission upgrade need not block reads of resident data.
        std::uint64_t read_response = 0;
        std::uint64_t sequence = 0;
        std::uint64_t generation = 0;
        bool write = false;
    };

    const Entry* find(std::uint64_t line) const {
        const auto it = entries_.find(line);
        return it == entries_.end() ? nullptr : &it->second;
    }

    // The watermark must bound EVERY later request, not just the request
    // currently visited in program order. Dispatch is monotone; OOO issue is
    // not. Expiring at the most recently visited issue loses live parents.
    void expire_before(std::uint64_t watermark) {
        if (watermark < watermark_) {
            throw std::logic_error("pending-fill watermark moved backwards");
        }
        watermark_ = watermark;
        while (!expiry_.empty() && expiry_.top().response <= watermark) {
            const auto item = expiry_.top();
            expiry_.pop();
            const auto it = entries_.find(item.line);
            if (it != entries_.end() &&
                it->second.generation == item.generation) {
                entries_.erase(it);
            }
        }
    }

    void publish(std::uint64_t line, std::uint64_t admission,
                 std::uint64_t response, std::uint64_t sequence,
                 bool write, std::uint64_t read_response) {
        if (response < admission) {
            throw std::logic_error("pending fill responds before admission");
        }
        if (response <= watermark_) return;
        const auto generation = ++generation_;
        if (read_response > response) {
            throw std::logic_error("data fill exceeds its parent response");
        }
        entries_[line] = Entry{
            admission, response, read_response, sequence, generation, write};
        expiry_.push(Expiry{response, line, generation});
    }

    std::size_t size() const { return entries_.size(); }

  private:
    struct Expiry {
        std::uint64_t response;
        std::uint64_t line;
        std::uint64_t generation;
        bool operator>(const Expiry& other) const {
            return std::tie(response, line, generation) >
                std::tie(other.response, other.line, other.generation);
        }
    };
    std::unordered_map<std::uint64_t, Entry> entries_;
    std::priority_queue<Expiry, std::vector<Expiry>, std::greater<>> expiry_;
    std::uint64_t watermark_ = 0;
    std::uint64_t generation_ = 0;
};

// A bounded, core-local ledger for cache-line transactions.  It is kept
// separate from PendingFillTable while the unified request lifecycle is being
// validated.  Time is advanced explicitly so a request whose real admission
// is in the future cannot reserve capacity while an earlier hole is still
// available.
//
// Active intervals are half open: [admission, callback).  advance_to(t)
// applies callbacks at t before a request at t is classified, so such a
// request starts a new generation rather than attaching to the completed one.
class LineGenerationLedger {
  public:
    enum class Access : std::uint8_t {
        kRead,
        kWrite,
    };

    enum class ReadVisibilityUpdate : std::uint8_t {
        // Extend only transaction completion and, for writes, permission.
        kKeep,
        // Data was also waiting for the old callback and moves with it.
        kExtendToCallback,
    };

    enum class Outcome : std::uint8_t {
        kNewGeneration,
        kAttached,
        kAdmissionDeferred,
        kCapacityBlocked,
        // The active generation supplies data but not write permission.
        kPermissionBlocked,
    };

    struct Request {
        std::uint64_t line = 0;
        std::uint64_t sequence = 0;
        Access access = Access::kRead;
        // A future admission is reported as deferred and never reserves a
        // slot.  A past admission is invalid because event time is monotone.
        std::uint64_t admission_cycle = 0;
        // These two fields describe service only if a new generation is
        // allocated.  Followers inherit the active leader's visibility.
        std::uint64_t callback_cycle = 0;
        std::uint64_t read_visible_cycle = 0;

        static Request read(std::uint64_t line,
                            std::uint64_t sequence,
                            std::uint64_t admission_cycle,
                            std::uint64_t callback_cycle,
                            std::uint64_t read_visible_cycle) {
            Request request;
            request.line = line;
            request.sequence = sequence;
            request.access = Access::kRead;
            request.admission_cycle = admission_cycle;
            request.callback_cycle = callback_cycle;
            request.read_visible_cycle = read_visible_cycle;
            return request;
        }

        static Request write(std::uint64_t line,
                             std::uint64_t sequence,
                             std::uint64_t admission_cycle,
                             std::uint64_t callback_cycle,
                             std::uint64_t read_visible_cycle) {
            Request request;
            request.line = line;
            request.sequence = sequence;
            request.access = Access::kWrite;
            request.admission_cycle = admission_cycle;
            request.callback_cycle = callback_cycle;
            request.read_visible_cycle = read_visible_cycle;
            return request;
        }
    };

    struct Entry {
        std::uint64_t line = 0;
        std::uint64_t generation = 0;
        std::uint64_t leader_sequence = 0;
        std::uint64_t admission_cycle = 0;
        std::uint64_t callback_cycle = 0;
        std::uint64_t read_visible_cycle = 0;
        std::uint64_t write_visible_cycle = 0;
        std::uint64_t read_followers = 0;
        std::uint64_t write_followers = 0;
        bool grants_write = false;
    };

    struct Decision {
        Outcome outcome = Outcome::kCapacityBlocked;
        std::uint64_t generation = 0;
        std::uint64_t leader_sequence = 0;
        std::uint64_t response_cycle = 0;
        // Earliest useful retry for a blocked request.  It is advisory only;
        // the ledger never inserts a reservation for that future time.
        std::uint64_t retry_cycle = 0;
        bool read_visible_at_admission = false;
        bool write_visible_at_admission = false;

        bool admitted() const {
            return outcome == Outcome::kNewGeneration ||
                outcome == Outcome::kAttached;
        }
        bool attached() const { return outcome == Outcome::kAttached; }
    };

    struct Counters {
        // Counts valid admission attempts. A deferred or blocked request is
        // counted again when the coordinator retries it; unique requests are
        // an owner-level statistic outside this bounded ledger.
        std::uint64_t requests = 0;
        std::uint64_t new_generations = 0;
        std::uint64_t immediate_callbacks = 0;
        std::uint64_t attachments = 0;
        std::uint64_t read_attachments = 0;
        std::uint64_t write_attachments = 0;
        std::uint64_t future_admission_deferrals = 0;
        std::uint64_t capacity_blocks = 0;
        std::uint64_t permission_blocks = 0;
        std::uint64_t expired_generations = 0;
        std::uint64_t callback_extensions = 0;
        std::uint64_t expiry_records_scheduled = 0;
        // Records discarded without completing a generation, whether by
        // advance_to(), retry-front cleanup, or bounded heap compaction.
        std::uint64_t stale_expiry_events = 0;
        std::uint64_t expiry_compactions = 0;
        std::size_t max_active_generations = 0;
        std::size_t max_expiry_entries = 0;
    };

    explicit LineGenerationLedger(std::size_t capacity,
                                  std::uint64_t initial_cycle = 0)
        : capacity_(capacity), now_(initial_cycle) {
        if (capacity == 0) {
            throw std::invalid_argument(
                "line-generation capacity must be nonzero");
        }
        expiry_limit_ = capacity_ >
                std::numeric_limits<std::size_t>::max() / 2
            ? std::numeric_limits<std::size_t>::max()
            : capacity_ * 2;
    }

    std::size_t capacity() const { return capacity_; }
    std::size_t size() const { return entries_.size(); }
    std::uint64_t now() const { return now_; }
    const Counters& counters() const { return counters_; }
    std::size_t expiry_entries() const { return expiry_.size(); }

    bool expiry_accounting_conserved() const {
        const auto limit = std::numeric_limits<std::uint64_t>::max();
        auto accounted = counters_.expired_generations;
        if (counters_.stale_expiry_events > limit - accounted) return false;
        accounted += counters_.stale_expiry_events;
        if (expiry_.size() > limit - accounted) return false;
        accounted += static_cast<std::uint64_t>(expiry_.size());
        return accounted == counters_.expiry_records_scheduled;
    }

    const Entry* find(std::uint64_t line) const {
        const auto found = entries_.find(line);
        return found == entries_.end() ? nullptr : &found->second;
    }

    // Commits callbacks in timestamp order.  Equal-time callbacks are applied
    // before any subsequent try_admit() at that time.
    void advance_to(std::uint64_t cycle) {
        if (cycle < now_) {
            throw std::logic_error(
                "line-generation time moved backwards");
        }
        now_ = cycle;
        while (!expiry_.empty() && expiry_.top().callback_cycle <= now_) {
            const auto item = expiry_.top();
            expiry_.pop();
            const auto found = entries_.find(item.line);
            if (found == entries_.end() ||
                found->second.generation != item.generation ||
                found->second.callback_cycle != item.callback_cycle) {
                bump(counters_.stale_expiry_events);
                continue;
            }
            entries_.erase(found);
            bump(counters_.expired_generations);
        }
    }

    Decision try_admit(const Request& request) {
        if (request.admission_cycle > now_) {
            bump(counters_.requests);
            bump(counters_.future_admission_deferrals);
            return Decision{
                Outcome::kAdmissionDeferred, 0, 0, 0,
                request.admission_cycle, false, false};
        }
        if (request.admission_cycle < now_) {
            throw std::logic_error(
                "line-generation admission is older than current time");
        }
        if (const auto found = entries_.find(request.line);
            found != entries_.end()) {
            bump(counters_.requests);
            auto& entry = found->second;
            if (request.access == Access::kWrite && !entry.grants_write) {
                bump(counters_.permission_blocks);
                return Decision{
                    Outcome::kPermissionBlocked,
                    entry.generation,
                    entry.leader_sequence,
                    0,
                    entry.callback_cycle,
                    entry.read_visible_cycle <= now_,
                    false};
            }

            bump(counters_.attachments);
            if (request.access == Access::kRead) {
                bump(counters_.read_attachments);
                bump(entry.read_followers);
            } else {
                bump(counters_.write_attachments);
                bump(entry.write_followers);
            }
            const auto visibility = request.access == Access::kRead
                ? entry.read_visible_cycle
                : entry.write_visible_cycle;
            return Decision{
                Outcome::kAttached,
                entry.generation,
                entry.leader_sequence,
                std::max(now_, visibility),
                0,
                entry.read_visible_cycle <= now_,
                entry.grants_write &&
                    entry.write_visible_cycle <= now_};
        }

        if (request.callback_cycle < request.admission_cycle) {
            throw std::logic_error(
                "line generation responds before admission");
        }
        if (request.read_visible_cycle < request.admission_cycle ||
            request.read_visible_cycle > request.callback_cycle) {
            throw std::logic_error(
                "line data visibility is outside its generation");
        }
        bump(counters_.requests);
        const bool write = request.access == Access::kWrite;

        // A zero-duration transaction is observable to the caller but owns no
        // active line or capacity after this call.  It therefore bypasses a
        // full ledger rather than waiting for an unrelated callback.
        if (request.callback_cycle == now_) {
            const auto generation = allocate_generation();
            bump(counters_.immediate_callbacks);
            return Decision{
                Outcome::kNewGeneration,
                generation,
                request.sequence,
                now_,
                0,
                true,
                write};
        }

        discard_stale_expiry_front();
        if (entries_.size() == capacity_) {
            if (expiry_.empty()) {
                throw std::logic_error(
                    "full line-generation ledger has no callback");
            }
            bump(counters_.capacity_blocks);
            return Decision{
                Outcome::kCapacityBlocked, 0, 0, 0,
                expiry_.top().callback_cycle, false, false};
        }

        const auto generation = allocate_generation();
        const auto write_visible = write ? request.callback_cycle : 0;
        const auto inserted = entries_.emplace(
            request.line,
            Entry{request.line, generation, request.sequence,
                  request.admission_cycle,
                  request.callback_cycle, request.read_visible_cycle,
                  write_visible, 0, 0, write});
        if (!inserted.second) {
            throw std::logic_error(
                "line-generation insertion raced with active entry");
        }
        push_expiry(Expiry{
            request.callback_cycle, request.line, generation});
        counters_.max_active_generations = std::max(
            counters_.max_active_generations, entries_.size());
        return Decision{
            Outcome::kNewGeneration,
            generation,
            request.sequence,
            request.access == Access::kRead
                ? request.read_visible_cycle
                : request.callback_cycle,
            0,
            request.read_visible_cycle <= now_,
            write && request.callback_cycle <= now_};
    }

    // Split responses or a late service decision may extend the callback.  An
    // old heap record then becomes stale; generation and callback matching
    // keep it from deleting the live entry.
    void extend_callback(std::uint64_t line, std::uint64_t generation,
                         std::uint64_t callback_cycle,
                         ReadVisibilityUpdate read_visibility_update) {
        const auto found = entries_.find(line);
        if (found == entries_.end() ||
            found->second.generation != generation) {
            throw std::logic_error(
                "cannot extend an inactive line generation");
        }
        auto& entry = found->second;
        if (callback_cycle < entry.callback_cycle) {
            throw std::logic_error(
                "line-generation callback moved backwards");
        }
        if (callback_cycle == entry.callback_cycle) return;
        if (read_visibility_update ==
            ReadVisibilityUpdate::kExtendToCallback) {
            if (entry.read_visible_cycle != entry.callback_cycle) {
                throw std::logic_error(
                    "cannot extend data that was independently visible");
            }
            entry.read_visible_cycle = callback_cycle;
        }
        if (entry.grants_write) {
            entry.write_visible_cycle = callback_cycle;
        }
        entry.callback_cycle = callback_cycle;
        bump(counters_.callback_extensions);
        push_expiry(Expiry{callback_cycle, line, generation});
    }

    // Intended for directed validation and debug gates, not the hot path.
    bool invariants_hold() const {
        if (capacity_ == 0 || entries_.size() > capacity_ ||
            expiry_.size() > expiry_limit_ ||
            counters_.max_active_generations > capacity_ ||
            counters_.max_expiry_entries > expiry_limit_) {
            return false;
        }
        for (const auto& [line, entry] : entries_) {
            if (line != entry.line || entry.generation == 0 ||
                entry.admission_cycle > now_ ||
                entry.callback_cycle <= now_ ||
                entry.read_visible_cycle < entry.admission_cycle ||
                entry.read_visible_cycle > entry.callback_cycle ||
                (entry.grants_write &&
                 entry.write_visible_cycle != entry.callback_cycle) ||
                (!entry.grants_write && entry.write_visible_cycle != 0)) {
                return false;
            }
            bool has_live_expiry = false;
            auto expiry = expiry_;
            while (!expiry.empty()) {
                const auto item = expiry.top();
                expiry.pop();
                if (item.line == line &&
                    item.generation == entry.generation &&
                    item.callback_cycle == entry.callback_cycle) {
                    has_live_expiry = true;
                    break;
                }
            }
            if (!has_live_expiry) return false;
        }
        return true;
    }

  private:
    struct Expiry {
        std::uint64_t callback_cycle;
        std::uint64_t line;
        std::uint64_t generation;
        bool operator>(const Expiry& other) const {
            return std::tie(callback_cycle, line, generation) >
                std::tie(other.callback_cycle, other.line,
                         other.generation);
        }
    };

    static void bump(std::uint64_t& value) {
        if (value != std::numeric_limits<std::uint64_t>::max()) ++value;
    }

    static void bump_by(std::uint64_t& value, std::size_t amount) {
        const auto limit = std::numeric_limits<std::uint64_t>::max();
        if (amount > limit - value) {
            value = limit;
        } else {
            value += static_cast<std::uint64_t>(amount);
        }
    }

    std::uint64_t allocate_generation() {
        if (generation_ == std::numeric_limits<std::uint64_t>::max()) {
            throw std::overflow_error(
                "line-generation identifier exhausted");
        }
        bump(counters_.new_generations);
        return ++generation_;
    }

    bool live_expiry(const Expiry& item) const {
        const auto found = entries_.find(item.line);
        return found != entries_.end() &&
            found->second.generation == item.generation &&
            found->second.callback_cycle == item.callback_cycle;
    }

    void discard_stale_expiry_front() {
        while (!expiry_.empty() && !live_expiry(expiry_.top())) {
            expiry_.pop();
            bump(counters_.stale_expiry_events);
        }
    }

    void compact_expiry() {
        auto old = expiry_;
        std::unordered_map<std::uint64_t, bool> retained_lines;
        retained_lines.reserve(entries_.size());
        std::size_t stale = 0;
        while (!old.empty()) {
            const auto item = old.top();
            old.pop();
            if (!live_expiry(item) ||
                !retained_lines.emplace(item.line, true).second) {
                ++stale;
            }
        }
        bump_by(counters_.stale_expiry_events, stale);
        decltype(expiry_) fresh;
        for (const auto& [line, entry] : entries_) {
            fresh.push(Expiry{
                entry.callback_cycle, line, entry.generation});
        }
        expiry_ = std::move(fresh);
        bump(counters_.expiry_compactions);
    }

    void push_expiry(const Expiry& item) {
        bump(counters_.expiry_records_scheduled);
        if (expiry_.size() >= expiry_limit_) {
            compact_expiry();
            // The entry is updated before this method is called, so a rebuild
            // already inserted its current callback.
            if (live_expiry(item)) {
                counters_.max_expiry_entries = std::max(
                    counters_.max_expiry_entries, expiry_.size());
                return;
            }
        }
        expiry_.push(item);
        if (expiry_.size() > expiry_limit_) {
            throw std::logic_error(
                "line-generation expiry state exceeded its bound");
        }
        counters_.max_expiry_entries = std::max(
            counters_.max_expiry_entries, expiry_.size());
    }

    std::size_t capacity_ = 0;
    std::size_t expiry_limit_ = 0;
    std::uint64_t now_ = 0;
    std::uint64_t generation_ = 0;
    std::unordered_map<std::uint64_t, Entry> entries_;
    std::priority_queue<Expiry, std::vector<Expiry>, std::greater<>> expiry_;
    Counters counters_;
};

}  // namespace fastsim
