#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace fastsim {

// Stable callback identity; every field participates, including core and generation.
// The caller guarantees generation uniqueness across reuse and discarded snapshots.
struct PendingOwner {
    std::uint32_t core;
    std::uint64_t sequence;
    std::uint32_t fragment;
    std::uint64_t generation;

    friend bool operator==(const PendingOwner& a, const PendingOwner& b) {
        return a.core == b.core && a.sequence == b.sequence &&
               a.fragment == b.fragment && a.generation == b.generation;
    }
    friend bool operator!=(const PendingOwner& a, const PendingOwner& b) { return !(a == b); }
};

// Fixed-capacity, owner-tagged resource reservations. Slot reuse forgets its old
// owner; there is no completed-ID history. Copies are independent rollback values.
class PendingResourcePool {
public:
    struct Admission {
        // Earliest provable admission at/after the query candidate. Empty means
        // unresolved owners might release before the earliest known release.
        std::optional<std::uint64_t> exact;
        std::uint64_t lower_bound;
        std::vector<PendingOwner> blockers;
    };

    explicit PendingResourcePool(std::size_t capacity) : slots_(capacity) {
        if (!capacity) throw std::invalid_argument("pending resource capacity must be positive");
    }
    std::size_t capacity() const { return slots_.size(); }

    // Non-mutating. Candidate and reservations must not precede the most recent
    // published admission; resolve callbacks are not constrained by that clock.
    Admission query(std::uint64_t candidate) const {
        check_clock(candidate);
        std::optional<std::uint64_t> known;
        std::optional<std::uint64_t> lower;
        for (const auto& slot : slots_) {
            if (!slot || (slot->release && *slot->release <= candidate))
                return {candidate, candidate, {}};
            const auto floor = std::max(candidate, slot->release.value_or(slot->lower_bound));
            if (!lower || floor < *lower) lower = floor;
            if (slot->release && (!known || *slot->release < *known)) known = slot->release;
        }
        Admission result{known, *lower, {}};
        for (const auto& slot : slots_) {
            if (!slot->release && (!known || std::max(candidate, slot->lower_bound) < *known))
                result.blockers.push_back(slot->owner);
        }
        if (!result.blockers.empty()) result.exact.reset();
        return result;
    }

    // Publish one exact admission. Resources external to this pool may move it
    // later than query(candidate). The chosen time itself must have a free slot.
    // Duplicate IDs still retained in slots reject, even after their release;
    // callers must mint a fresh generation for every reservation.
    void reserve(PendingOwner owner, std::uint64_t admission) {
        check_clock(admission);
        for (const auto& slot : slots_)
            if (slot && slot->owner == owner)
                throw std::invalid_argument("duplicate pending resource owner");
        for (auto& slot : slots_) {
            if (!slot || (slot->release && *slot->release <= admission)) {
                slot = Slot{owner, admission, admission, std::nullopt};
                last_admission_ = admission;
                return;
            }
        }
        throw std::logic_error("pending resource admission is not exact/free");
    }

    void raise_release_lower_bound(PendingOwner owner, std::uint64_t lower_bound) {
        auto& slot = find(owner);
        if (slot.release || lower_bound < slot.lower_bound)
            throw std::logic_error("invalid pending resource lower bound");
        slot.lower_bound = lower_bound;
    }

    void resolve(PendingOwner owner, std::uint64_t release) {
        auto& slot = find(owner);
        if (release < slot.admission || release < slot.lower_bound ||
            (slot.release && *slot.release != release))
            throw std::logic_error("invalid pending resource exact release");
        slot.release = release;
    }

private:
    struct Slot {
        PendingOwner owner;
        std::uint64_t admission;
        std::uint64_t lower_bound;
        std::optional<std::uint64_t> release;
    };
    std::vector<std::optional<Slot>> slots_;
    std::optional<std::uint64_t> last_admission_;

    void check_clock(std::uint64_t time) const {
        if (last_admission_ && time < *last_admission_)
            throw std::logic_error("backwards pending resource admission clock");
    }
    Slot& find(PendingOwner owner) {
        for (auto& slot : slots_)
            if (slot && slot->owner == owner) return *slot;
        throw std::invalid_argument("stale or missing pending resource owner");
    }
};

struct StoreFragment {
    PendingOwner owner;
    std::uint64_t agu_floor;
    std::uint64_t availability_floor;
};

// Detached ordinary-store ownership: one architectural sequence, with fragments
// in send order. Not a core scheduler. The caller owns the group lifetime and
// supplies the exact predecessor group response (after ALL its fragments).
class DetachedStoreGroup {
public:
    DetachedStoreGroup(std::uint32_t core, std::uint64_t sequence,
                       std::vector<StoreFragment> fragments,
                       std::optional<PendingOwner> predecessor = std::nullopt)
        : core_(core), sequence_(sequence), predecessor_(predecessor) {
        if (fragments.empty()) throw std::invalid_argument("empty detached store group");
        if (predecessor && (predecessor->core != core || predecessor->sequence >= sequence))
            throw std::invalid_argument("invalid detached store predecessor");
        fragments_.reserve(fragments.size());
        for (const auto& fragment : fragments) {
            if (fragment.owner.core != core || fragment.owner.sequence != sequence)
                throw std::invalid_argument("fragment outside detached store group");
            for (const auto& prior : fragments_)
                if (prior.description.owner == fragment.owner)
                    throw std::invalid_argument("duplicate detached store fragment");
            fragments_.push_back({fragment, std::nullopt, std::nullopt});
        }
    }

    std::uint32_t core() const { return core_; }
    std::uint64_t sequence() const { return sequence_; }
    std::optional<std::uint64_t> retirement() const { return retirement_; }

    // Retirement depends on AGU completion but never on fragment responses or
    // predecessor response. Preflight the exact +2 edge before publishing.
    void retire(std::uint64_t time) {
        checked_add(time, 2);
        if (retirement_ && *retirement_ != time)
            throw std::logic_error("detached store retirement already published");
        for (const auto& fragment : fragments_)
            if (time < fragment.description.agu_floor)
                throw std::logic_error("detached store retirement precedes AGU");
        retirement_ = time;
    }

    void resolve_predecessor(PendingOwner owner, std::uint64_t response) {
        if (!predecessor_ || *predecessor_ != owner)
            throw std::invalid_argument("stale or missing detached store predecessor");
        checked_add(response, 1);
        if (predecessor_response_ && *predecessor_response_ != response)
            throw std::logic_error("detached store predecessor response already published");
        predecessor_response_ = response;
    }

    // Empty means pending retirement, predecessor, or prior fragment admission.
    // Within a group there is NO previous-fragment response dependency. The
    // effective own availability is max(AGU floor, availability floor).
    std::optional<std::uint64_t> send_floor(PendingOwner owner) const {
        const auto index = find_index(owner);
        if (!retirement_ || (predecessor_ && !predecessor_response_) ||
            (index && !fragments_[index - 1].admission)) return std::nullopt;
        const auto& description = fragments_[index].description;
        auto floor = std::max({checked_add(*retirement_, 2), description.agu_floor,
                               description.availability_floor});
        if (predecessor_response_) floor = std::max(floor, checked_add(*predecessor_response_, 1));
        if (index) floor = std::max(floor, *fragments_[index - 1].admission);
        return floor;
    }

    // A nonempty admission is also the admitted flag. External port/resource
    // capacity may delay time above send_floor, but publication is immutable.
    void admit(PendingOwner owner, std::uint64_t time) {
        auto& fragment = fragments_[find_index(owner)];
        const auto floor = send_floor(owner);
        if (!floor || time < *floor || (fragment.admission && *fragment.admission != time))
            throw std::logic_error("invalid detached store admission");
        fragment.admission = time;
    }
    std::optional<std::uint64_t> admission(PendingOwner owner) const {
        return fragments_[find_index(owner)].admission;
    }

    void respond(PendingOwner owner, std::uint64_t time) {
        auto& fragment = fragments_[find_index(owner)];
        if (!fragment.admission || time < *fragment.admission ||
            (fragment.response && *fragment.response != time))
            throw std::logic_error("invalid detached store response");
        fragment.response = time;
    }
    std::optional<std::uint64_t> response(PendingOwner owner) const {
        return fragments_[find_index(owner)].response;
    }

    // SQ ownership survives retirement and every partial split response.
    std::optional<std::uint64_t> release() const {
        if (!retirement_) return std::nullopt;
        auto time = *retirement_;
        for (const auto& fragment : fragments_) {
            if (!fragment.response) return std::nullopt;
            time = std::max(time, *fragment.response);
        }
        return time;
    }

private:
    struct FragmentState {
        StoreFragment description;
        std::optional<std::uint64_t> admission;
        std::optional<std::uint64_t> response;
    };
    std::uint32_t core_;
    std::uint64_t sequence_;
    std::vector<FragmentState> fragments_;
    std::optional<PendingOwner> predecessor_;
    std::optional<std::uint64_t> predecessor_response_;
    std::optional<std::uint64_t> retirement_;

    static std::uint64_t checked_add(std::uint64_t time, std::uint64_t delta) {
        if (time > std::numeric_limits<std::uint64_t>::max() - delta)
            throw std::overflow_error("detached store exact time overflow");
        return time + delta;
    }
    std::size_t find_index(PendingOwner owner) const {
        for (std::size_t i = 0; i < fragments_.size(); ++i)
            if (fragments_[i].description.owner == owner) return i;
        throw std::invalid_argument("stale or missing detached store fragment");
    }
};

} // namespace fastsim
