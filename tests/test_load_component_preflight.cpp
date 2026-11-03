#include "fastsim/load_component_preflight.hpp"

#include <stdexcept>
#include <vector>

namespace {
using Preflight = fastsim::LoadComponentPreflight;
using Event = Preflight::Event;
using Reason = Preflight::Reason;
using Kind = Preflight::Kind;

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
Event load(std::uint32_t core, std::uint64_t line, std::uint64_t component,
           std::uint64_t cycle = 0) {
    Event result;
    result.core = core;
    result.line = line;
    result.private_component = component;
    result.admission_cycle = cycle;
    return result;
}
void conserved(const Preflight::Result& result) {
    require(result.event_stats.conserved() && result.active_event_stats.conserved() &&
                result.component_stats.conserved(), "preflight reason counts must conserve");
    require(result.event_stats.total == result.events.size() &&
                result.active_event_stats.total == result.active_events.size() &&
                result.component_stats.total == result.components.size(),
            "preflight statistics must count each result exactly once");
}
void same_line_and_private_set() {
    auto result = Preflight::analyze({load(0, 10, 1, 0), load(0, 10, 1, 9),
                                      load(0, 20, 2, 12)});
    require(result.events.size() == 3 && result.components.size() == 2 &&
                result.events[0].certified() && result.events[1].certified() &&
                result.events[2].certified() &&
                result.events[0].component == result.events[1].component,
            "same-line loads and unrelated private set must be certified");
    require(result.event_stats.by_reason[static_cast<std::size_t>(Reason::kCertified)] == 3,
            "certified population count must include every batch load");
    conserved(result);
    result = Preflight::analyze({load(0, 10, 1), load(0, 20, 1), load(0, 30, 2)});
    require(result.events[0].has(Reason::kPrivateLineConflict) &&
                result.events[1].has(Reason::kPrivateLineConflict) &&
                result.events[2].certified(),
            "other physical line in same private set must reject whole component only");
    conserved(result);
}
void core_namespaces_and_shared_lines() {
    auto result = Preflight::analyze({load(0, 10, 1), load(1, 20, 1)});
    require(result.components.size() == 2 && result.events[0].certified() &&
                result.events[1].certified(), "private IDs must be namespaced by core");
    result = Preflight::analyze({load(0, 10, 1), load(1, 10, 2), load(2, 20, 3)});
    require(result.components.size() == 2 && result.events[0].has(Reason::kCrossCoreLine) &&
                result.events[1].has(Reason::kCrossCoreLine) && result.events[2].certified(),
            "same physical line on different cores must reject both endpoints");
    conserved(result);
}
void unsupported_events_and_transitive_resources() {
    for (const auto kind : {Kind::kStore, Kind::kAtomic, Kind::kIfetch,
                            Kind::kPageWalk, Kind::kPageSeed}) {
        Event unsupported = load(0, 10, 1);
        unsupported.kind = kind;
        const auto result = Preflight::analyze({load(0, 10, 1), unsupported, load(0, 20, 2)});
        require(result.events[0].has(Reason::kUnsupportedKind) &&
                    result.events[1].has(Reason::kUnsupportedKind) &&
                    result.events[2].certified(),
                "non-plain loads must reject associated loads without global poisoning");
        conserved(result);
    }
    Event first = load(0, 10, 1);
    first.resource_component_valid = true;
    first.resource_component = 0;
    Event second = load(1, 20, 1);
    second.resource_component_valid = true;
    second.resource_component = 0;
    Event third = second;
    third.resource_component_valid = false;
    third.kind = Kind::kStore;
    const auto result = Preflight::analyze({first, second, third, load(2, 30, 1)});
    require(result.components.size() == 2 &&
                result.events[0].has(Reason::kUnsupportedKind) &&
                result.events[1].has(Reason::kUnsupportedKind) &&
                result.events[2].has(Reason::kUnsupportedKind) &&
                result.events[3].certified(),
            "valid shared ID zero must close transitive interference; absent IDs must not join");
    conserved(result);
}
void carried_and_active_conflicts() {
    Event carried = load(0, 10, 1);
    carried.functional_carried = true;
    auto result = Preflight::analyze({load(0, 10, 1), carried});
    require(result.events[0].primary_reason == Reason::kFunctionalCarried &&
                result.events[1].has(Reason::kFunctionalCarried),
            "functional carried state must reject associated ordinary loads");
    Event active = load(0, 10, 1, 5);
    result = Preflight::analyze({load(0, 10, 1, 6)}, {active});
    require(result.events[0].certified() && result.active_events[0].certified() &&
                result.components[0].has_active_generation,
            "compatible active generation must allow same-line read follower");
    result = Preflight::analyze({load(0, 20, 1, 6), load(0, 30, 2, 7)}, {active});
    require(result.events[0].has(Reason::kActiveConflict) &&
                result.events[0].has(Reason::kPrivateLineConflict) &&
                result.active_events[0].primary_reason == Reason::kActiveConflict &&
                result.events[1].certified(),
            "unsupported same-set activity must flag live generation and associated event");
    conserved(result);
    carried.active_generation = true;
    result = Preflight::analyze({carried});
    require(result.events[0].has(Reason::kActiveConflict),
            "inline active generation marker must propagate active conflict");
    conserved(result);
}
void chronological_admissions() {
    auto result = Preflight::analyze({load(0, 10, 1, 9), load(1, 20, 1, 1),
                                      load(0, 10, 1, 8)});
    require(result.events[0].has(Reason::kNonmonotonicAdmission) &&
                result.events[2].has(Reason::kNonmonotonicAdmission) &&
                result.events[1].certified(),
            "per-core backwards admission must reject its component, intercore order is free");
    result = Preflight::analyze({load(0, 10, 1, 9)},
                               {load(0, 10, 1, 10), load(0, 10, 1, 8)});
    require(result.events[0].has(Reason::kNonmonotonicAdmission) &&
                result.events[0].has(Reason::kActiveConflict),
            "batch may not precede most recent active admission");
    result = Preflight::analyze({load(0, 10, 1, 10), load(0, 10, 1, 10)},
                               {load(0, 10, 1, 10), load(0, 10, 1, 8)});
    require(result.events[0].certified() && result.events[1].certified(),
            "unordered active snapshot and equal-time batch admissions are legal");
    conserved(result);
}
void inclusive_empty_and_copy() {
    const auto result = Preflight::analyze({load(0, 10, 1), load(1, 20, 2)}, {}, true);
    require(result.events[0].primary_reason == Reason::kInclusiveLlc &&
                result.events[1].primary_reason == Reason::kInclusiveLlc,
            "inclusive LLC must reject all components");
    conserved(result);
    auto copy = result;
    copy.events[0].reasons = 0;
    require(result.events[0].has(Reason::kInclusiveLlc), "result copies must be isolated");
    const auto empty = Preflight::analyze({}, {}, true);
    require(empty.events.empty() && empty.active_events.empty() && empty.components.empty(),
            "empty preflight must produce no phantom components");
    conserved(empty);
    const auto only_active = Preflight::analyze({}, {load(0, 10, 1)});
    require(only_active.events.empty() && only_active.active_events[0].certified() &&
                only_active.components[0].event_count == 0 &&
                only_active.components[0].active_event_count == 1,
            "active-only snapshot must retain its own separate conserved population");
    conserved(only_active);
    const auto next_batch = Preflight::analyze({load(0, 20, 1)});
    require(next_batch.events[0].certified(), "preflight must not retain historical seen lines");
}
} // namespace

void test_load_component_preflight() {
    same_line_and_private_set();
    core_namespaces_and_shared_lines();
    unsupported_events_and_transitive_resources();
    carried_and_active_conflicts();
    chronological_admissions();
    inclusive_empty_and_copy();
}
