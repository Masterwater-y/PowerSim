#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <numeric>
#include <unordered_map>
#include <utility>
#include <vector>

namespace fastsim {

// Conservative, read-only certification over one batch and its active snapshot.
// Component IDs describe interference domains, not cache line identities.
// No historical seen-set survives this call. Callers must describe every
// relevant interference domain, including functional-only accesses.
class LoadComponentPreflight {
public:
    enum class Kind { kLoad, kStore, kAtomic, kIfetch, kPageWalk, kPageSeed };
    enum class Reason : std::uint8_t {
        kCertified, kInclusiveLlc, kActiveConflict, kUnsupportedKind,
        kFunctionalCarried, kPrivateLineConflict, kCrossCoreLine,
        kNonmonotonicAdmission, kCount
    };
    using ReasonMask = std::uint32_t;
    struct Event {
        std::uint32_t core = 0;
        std::uint64_t line = 0;
        std::uint64_t private_component = 0;
        std::uint64_t resource_component = 0;
        Kind kind = Kind::kLoad;
        bool functional_carried = false;
        bool active_generation = false;
        std::uint64_t admission_cycle = 0;
        bool resource_component_valid = false;
    };
    struct Decision {
        std::size_t component = 0;
        Reason primary_reason = Reason::kCertified;
        ReasonMask reasons = 0;
        bool certified() const { return reasons == 0; }
        bool has(Reason reason) const {
            return reason == Reason::kCertified ? certified() :
                (reasons & mask(reason)) != 0;
        }
    };
    struct Component : Decision {
        std::size_t event_count = 0;
        std::size_t active_event_count = 0;
        bool has_active_generation = false;
    };
    struct Statistics {
        std::size_t total = 0;
        std::array<std::size_t, static_cast<std::size_t>(Reason::kCount)> by_reason{};
        bool conserved() const {
            return total == std::accumulate(by_reason.begin(), by_reason.end(),
                                             std::size_t{0});
        }
    };
    struct Result {
        std::vector<Decision> events;
        std::vector<Decision> active_events;
        std::vector<Component> components;
        Statistics event_stats;
        Statistics active_event_stats;
        Statistics component_stats;
    };
    static constexpr ReasonMask mask(Reason reason) {
        return reason == Reason::kCertified ? 0 :
            ReasonMask{1} << static_cast<unsigned>(reason);
    }
    static const char* reason_name(Reason reason) {
        switch (reason) {
        case Reason::kCertified: return "certified";
        case Reason::kInclusiveLlc: return "inclusive_llc";
        case Reason::kActiveConflict: return "active_conflict";
        case Reason::kUnsupportedKind: return "unsupported_kind";
        case Reason::kFunctionalCarried: return "functional_carried";
        case Reason::kPrivateLineConflict: return "private_line_conflict";
        case Reason::kCrossCoreLine: return "cross_core_line";
        case Reason::kNonmonotonicAdmission: return "nonmonotonic_admission";
        case Reason::kCount: break;
        }
        return "invalid";
    }

    static Result analyze(const std::vector<Event>& batch,
                          const std::vector<Event>& active = {},
                          bool inclusive_llc = false) {
        const std::size_t count = batch.size() + active.size();
        auto event = [&](std::size_t index) -> const Event& {
            return index < batch.size() ? batch[index] : active[index - batch.size()];
        };
        std::vector<std::size_t> parents(count), ranks(count, 0);
        std::iota(parents.begin(), parents.end(), std::size_t{0});
        auto root = [&](std::size_t index) {
            while (parents[index] != index) {
                parents[index] = parents[parents[index]];
                index = parents[index];
            }
            return index;
        };
        auto join = [&](std::size_t first, std::size_t second) {
            first = root(first);
            second = root(second);
            if (first == second) return;
            if (ranks[first] < ranks[second]) std::swap(first, second);
            parents[second] = first;
            if (ranks[first] == ranks[second]) ++ranks[first];
        };
        std::vector<ReasonMask> reasons(count, inclusive_llc ? mask(Reason::kInclusiveLlc) : 0);
        std::unordered_map<PrivateKey, std::size_t, PrivateHash> private_first;
        std::unordered_map<std::uint64_t, std::size_t> resource_first, line_first;
        for (std::size_t index = 0; index < count; ++index) {
            const auto& current = event(index);
            if (current.kind != Kind::kLoad)
                reasons[index] |= mask(Reason::kUnsupportedKind);
            if (current.functional_carried)
                reasons[index] |= mask(Reason::kFunctionalCarried);
            const auto private_pair = private_first.emplace(
                PrivateKey{current.core, current.private_component}, index);
            if (!private_pair.second) {
                const auto prior = private_pair.first->second;
                join(index, prior);
                if (event(prior).line != current.line)
                    reasons[index] |= mask(Reason::kPrivateLineConflict);
            }
            if (current.resource_component_valid) {
                const auto shared = resource_first.emplace(current.resource_component, index);
                if (!shared.second) join(index, shared.first->second);
            }
            // Same physical line is a relation even if the caller's private
            // component IDs differ (e.g. I-cache versus D-cache).
            const auto physical = line_first.emplace(current.line, index);
            if (!physical.second) {
                const auto prior = physical.first->second;
                join(index, prior);
                if (event(prior).core != current.core)
                    reasons[index] |= mask(Reason::kCrossCoreLine);
            }
        }
        // Active snapshots need not be sorted. Their latest admission is the
        // earliest legal next admission for each core's chronological batch.
        std::unordered_map<std::uint32_t, std::uint64_t> latest_admission;
        for (const auto& current : active) {
            auto inserted = latest_admission.emplace(current.core, current.admission_cycle);
            if (!inserted.second && inserted.first->second < current.admission_cycle)
                inserted.first->second = current.admission_cycle;
        }
        for (std::size_t index = 0; index < batch.size(); ++index) {
            const auto& current = batch[index];
            const auto inserted = latest_admission.emplace(current.core, current.admission_cycle);
            if (!inserted.second) {
                if (current.admission_cycle < inserted.first->second)
                    reasons[index] |= mask(Reason::kNonmonotonicAdmission);
                else inserted.first->second = current.admission_cycle;
            }
        }
        Result result;
        std::unordered_map<std::size_t, std::size_t> component_ids;
        std::vector<std::size_t> component_for_event(count);
        for (std::size_t index = 0; index < count; ++index) {
            const auto inserted = component_ids.emplace(root(index), result.components.size());
            if (inserted.second) {
                result.components.emplace_back();
                result.components.back().component = inserted.first->second;
            }
            const auto id = inserted.first->second;
            component_for_event[index] = id;
            auto& component = result.components[id];
            component.reasons |= reasons[index];
            component.has_active_generation |=
                index >= batch.size() || event(index).active_generation;
            if (index < batch.size()) ++component.event_count;
            else ++component.active_event_count;
        }
        for (auto& component : result.components) {
            if (component.has_active_generation && component.reasons != 0)
                component.reasons |= mask(Reason::kActiveConflict);
            component.primary_reason = primary(component.reasons);
            record(result.component_stats, component.primary_reason);
        }
        result.events.reserve(batch.size());
        result.active_events.reserve(active.size());
        for (std::size_t index = 0; index < count; ++index) {
            const Decision decision = result.components[component_for_event[index]];
            if (index < batch.size()) {
                result.events.push_back(decision);
                record(result.event_stats, decision.primary_reason);
            } else {
                result.active_events.push_back(decision);
                record(result.active_event_stats, decision.primary_reason);
            }
        }
        return result;
    }

private:
    struct PrivateKey {
        std::uint32_t core;
        std::uint64_t component;
        bool operator==(const PrivateKey& other) const {
            return core == other.core && component == other.component;
        }
    };
    struct PrivateHash {
        std::size_t operator()(const PrivateKey& key) const {
            const auto first = std::hash<std::uint64_t>{}(key.component);
            return first ^ (std::hash<std::uint32_t>{}(key.core) +
                            std::size_t{0x9e3779b9} + (first << 6) + (first >> 2));
        }
    };
    static Reason primary(ReasonMask reasons) {
        for (unsigned value = 1; value < static_cast<unsigned>(Reason::kCount); ++value)
            if (reasons & mask(static_cast<Reason>(value))) return static_cast<Reason>(value);
        return Reason::kCertified;
    }
    static void record(Statistics& stats, Reason reason) {
        ++stats.total;
        ++stats.by_reason[static_cast<std::size_t>(reason)];
    }
};

} // namespace fastsim
