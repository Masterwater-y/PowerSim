#include "fastsim/recorded_scheduler.hpp"
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template <class Action> void rejects(Action action, const char* message) {
    bool rejected = false;
    try { action(); } catch (const std::exception&) { rejected = true; }
    require(rejected, message);
}

void sleep_and_queue_keep_the_physical_core_running() {
    fastsim::RecordedScheduler scheduler({101, 202}, {101, 202});
    scheduler.begin_measurement(100);
    scheduler.switch_owner(0, 101, 303, 2, 150);
    rejects([&] { scheduler.switch_owner(0, 303, 101, 1, 160); },
            "sleeping task must not resume without a wake");
    scheduler.wakeup(101);
    require(scheduler.owner(0) == 303, "wake makes task ready without running it");
    scheduler.switch_owner(0, 303, 101, 1, 250);
    scheduler.finish_core(0, 300);
    scheduler.finish_core(1, 300);
    scheduler.finish_common(350, {true, true});
    require(scheduler.active_q16()[0] == 100, "sleep and ready queue excluded from task active");
    require(scheduler.other_q16()[0] == 100, "other contexts execute throughout task sleep");
    require(scheduler.active_q16()[1] == 200, "unaffected physical core progresses");
    require(scheduler.unknown_q16()[0] == 0 && scheduler.complete(), "certified ownership closes without extrapolating tails");
}

void warmup_preemption_and_unknown_sleep_reason_preserve_ownership() {
    fastsim::RecordedScheduler scheduler({101}, {101});
    scheduler.switch_owner(0, 101, 303, 1, 50);
    scheduler.begin_measurement(100);
    scheduler.switch_owner(0, 303, 101, 3, 200);
    scheduler.wakeup(101); // A running-task wake does not remove ownership.
    scheduler.finish_core(0, 250);
    scheduler.finish_common(300, {true});
    require(scheduler.active_q16()[0] == 50 && scheduler.other_q16()[0] == 100,
            "warm owner survives seam and runnable preemption needs no wake");
    require(scheduler.complete(), "unknown sleep cause does not invalidate observed ownership");

    fastsim::RecordedScheduler blocked({101}, {101});
    blocked.switch_owner(0, 101, 303, 2, 50);
    blocked.begin_measurement(100);
    rejects([&] { blocked.switch_owner(0, 303, 101, 1, 110); },
            "blocked state survives warmup seam");
    blocked.wakeup(101);
    blocked.switch_owner(0, 303, 101, 1, 120);
    blocked.finish_core(0, 150);
    blocked.finish_common(150, {true});
    require(blocked.active_q16()[0] == 30 && blocked.other_q16()[0] == 20,
            "warmup durations are excluded from measurement");
}

void closure_never_extrapolates_uncertified_owners() {
    fastsim::RecordedScheduler scheduler({101, 303, 202}, {101, 202});
    require(!scheduler.complete(), "scheduler is incomplete before certified closure");
    scheduler.begin_measurement(100);
    scheduler.finish_core(0, 200);
    scheduler.finish_core(1, 250);
    scheduler.finish_core(2, 300);
    scheduler.finish_common(300, {false, true, false});
    require(scheduler.active_q16()[0] == 100 && scheduler.unknown_q16()[0] == 0,
            "uncertified closure does not invent EOF cycles");
    require(scheduler.other_q16()[1] == 150 && scheduler.unknown_q16()[1] == 0,
            "certified other context stops at stream completion");
    require(scheduler.active_q16()[2] == 200 && scheduler.unknown_q16()[2] == 0,
            "zero-length uncertified tail introduces no fake duration");
    for (std::size_t core = 0; core != 3; ++core)
        require(scheduler.active_q16()[core] + scheduler.other_q16()[core] +
                scheduler.unknown_q16()[core] == 100 + 50*core, "each physical core stops at its own completion");
    require(!scheduler.complete(), "uncertified closure remains incomplete even with zero unknown tail");

    fastsim::RecordedScheduler no_tail({101}, {101});
    no_tail.begin_measurement(0);
    no_tail.finish_core(0, 100);
    no_tail.finish_common(100, {false});
    require(no_tail.unknown_q16()[0] == 0 && !no_tail.complete(),
            "absence of unknown duration is insufficient without certification");
}

void invalid_events_do_not_mutate_ownership_or_time() {
    rejects([] { fastsim::RecordedScheduler bad({101, 101}, {101}); }, "duplicate initial owners reject");
    fastsim::RecordedScheduler scheduler({101, 202}, {101, 202});
    scheduler.begin_measurement(100);
    rejects([&] { scheduler.switch_owner(0, 404, 303, 1, 150); }, "previous owner must match");
    rejects([&] { scheduler.switch_owner(0, 101, 202, 1, 150); }, "duplicate live owners reject");
    rejects([&] { scheduler.switch_owner(0, 101, 303, 0, 150); }, "zero normalized state rejects");
    rejects([&] { scheduler.switch_owner(0, 101, 303, 4, 150); }, "invalid normalized state rejects");
    rejects([&] { scheduler.switch_owner(0, 101, 101, 2, 150); }, "blocked switch cannot retain the same running task");
    rejects([&] { scheduler.switch_owner(0, 101, 303, 1, 99); }, "target boundary time regression rejects");
    rejects([&] { scheduler.switch_owner(2, 101, 303, 1, 150); }, "core index checked");
    scheduler.finish_core(0, 200);
    scheduler.finish_core(1, 200);
    scheduler.finish_common(200, {true, true});
    require(scheduler.owner(0) == 101 && scheduler.active_q16()[0] == 100,
            "rejected events leave original owner and clock intact");
}

void measurement_and_closure_lifecycle_fail_closed() {
    fastsim::RecordedScheduler warm({101}, {101});
    warm.switch_owner(0, 101, 303, 1, 100);
    rejects([&] { warm.switch_owner(0, 303, 101, 1, 99); }, "warmup times cannot regress");
    rejects([&] { warm.begin_measurement(99); }, "measurement cannot precede warmup frontier");
    rejects([&] { warm.finish_core(0, 100); }, "finish requires measurement origin");
    rejects([&] { warm.finish_common(100, {true}); }, "common finish requires measurement origin");
    warm.begin_measurement(100);
    rejects([&] { warm.begin_measurement(110); }, "repeated measurement start rejects");
    rejects([&] { warm.finish_common(150, {true}); }, "common finish needs every core completion");
    warm.finish_core(0, 125);
    rejects([&] { warm.finish_core(0, 150); }, "repeated core completion rejects");
    rejects([&] { warm.switch_owner(0, 303, 101, 1, 130); }, "closed core cannot accept later switches");
    rejects([&] { warm.finish_common(150, {}); }, "common certification needs one entry per core");
    rejects([&] { warm.finish_common(124, {true}); }, "common end cannot precede core completion");
    warm.finish_common(150, {true});
    rejects([&] { warm.finish_common(150, {true}); }, "repeated common completion rejects");
    rejects([&] { warm.wakeup(101); }, "events after common closure reject");
    require(warm.other_q16()[0] == 25 && warm.complete(), "invalid closures are transactional");
}

void q16_fractional_and_extreme_clocks_conserve_without_wrapping() {
    const auto maximum = std::numeric_limits<std::uint64_t>::max();
    fastsim::RecordedScheduler scheduler({101}, {101});
    scheduler.begin_measurement(maximum - 100);
    scheduler.switch_owner(0, 101, 303, 1, maximum - 75);
    scheduler.finish_core(0, maximum - 25);
    scheduler.finish_common(maximum, {true});
    require(scheduler.active_q16()[0] == 25 && scheduler.other_q16()[0] == 50,
            "Q16 fractions retained and absolute clock does not overflow counters");

    fastsim::RecordedScheduler widest({101}, {101});
    widest.begin_measurement(0);
    widest.finish_core(0, maximum);
    widest.finish_common(maximum, {true});
    require(widest.active_q16()[0] == maximum && widest.complete(), "maximum representable duration remains exact");
    rejects([&] { widest.finish_core(0, 0); }, "wrapped absolute time never accepted");
}
}

void test_recorded_scheduler() {
    sleep_and_queue_keep_the_physical_core_running();
    warmup_preemption_and_unknown_sleep_reason_preserve_ownership();
    closure_never_extrapolates_uncertified_owners();
    invalid_events_do_not_mutate_ownership_or_time();
    measurement_and_closure_lifecycle_fail_closed();
    q16_fractional_and_extreme_clocks_conserve_without_wrapping();
}
