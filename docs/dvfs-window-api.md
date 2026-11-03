# DVFS and windowed simulation API

FastSim now has a first DVFS vertical slice for the
`interval_weave`/`time_epoch` engine. It provides independently clocked cores,
safe pause boundaries, per-window CPI/PMU results, and both target-time and
retired-instruction windows. Existing configurations remain equivalent to a
fixed 3 GHz run.

## Time domains

`sim.reference_frequency_hz` defines the common target-time calendar used by
the shared cache, CHA, NoC, and DRAM. Its default is 3,000,000,000 Hz, matching
the historical configs whose uncore delays were expressed in 3 GHz cycles.

Each pipeline keeps its existing absolute core-local cycle descriptors. A
piecewise rational clock maps those descriptors onto reference time. Calling
`set_core_frequencies()` at a pause boundary appends a new clock segment, so
already decoded lookahead is not discarded or regenerated and all future
pipeline edges immediately use the new slope.

Configuration keys are numeric Hz:

```ini
sim.reference_frequency_hz = 3000000000
core.frequency_hz = 2400000000
# Optional ordered per-core override; it must have sim.cores entries.
core.frequencies_hz = 3000000000,2400000000,1800000000,1500000000
```

## Controller interface

```cpp
#include "fastsim/simulator.hpp"

fastsim::Simulator simulator(config, std::move(traces));
simulator.set_core_frequencies({3'000'000'000, 2'000'000'000});

while (!simulator.finished()) {
    // Exactly 100 ns of simulated target time (unless the trace ends).
    auto result = simulator.advance(
        fastsim::SimulationWindow::simulated_time_ns(100));

    // A governor can consume result.cores[*].cpi and PMU fields here.
    std::vector<std::uint64_t> next_frequency = decide(result);
    simulator.set_core_frequencies(next_frequency);
}
```

An instruction-controlled governor instead uses:

```cpp
auto result = simulator.advance(
    fastsim::SimulationWindow::retired_instructions(100000));
```

The optional pybind11 module exposes the same stateful loop to Python:

```python
import fastsim_py

sim = fastsim_py.DvfsSession(
    config_path="profile.cfg",
    manifest_path="manifest.txt",
    measurement_scope="user",
)

while not sim.finished():
    result = sim.advance_time_ns(100_000)
    if result.finished:
        break
    sim.set_core_frequencies(decide(result))
```

Configure with `-DFASTSIM_BUILD_PYTHON=ON`; the module is emitted under the
build tree's `python/` directory. The selected Python must be at least 3.8 and
have pybind11 installed. `advance*()` releases the GIL while C++ simulation is
running. See
[the external integration guide](fastsim-external-integration.md#82-pythonfastsim_py)
for construction overrides, result fields, `to_dict()`, and threading rules.

The instruction budget is system-wide across active cores. FastSim pauses at
the first committed time-epoch boundary that reaches the budget;
`instruction_overshoot` reports the deterministic extra instructions. A time
window caps the final reference-time horizon at the requested duration and
does not round it to `sim.interval_max_cycles`. If trace input is exhausted
while corrected retire edges are still in flight, later calls drain that tail
without allowing any nonterminal time window to overrun its target.

`set_core_frequencies()` requires exactly `sim.cores` nonzero entries and is
atomic at the current pause boundary. Zero Hz/clock gating is deliberately not
encoded as a frequency because the current static scheduler has no runnable,
idle, block, or wakeup state.

## Window metrics

For each core, a result contains the active frequency, the additive local
cycle-counter delta,
retired macro instructions and UOPs, memory UOP/line counts, branch and DTLB
counters, L1D/L2 counters, macro-instruction CPI, and UOP CPI. CPI is unavailable
when its corresponding retirement denominator is zero. Per-core cycle values
are core-local; shared queue and merged-wait cycle counters remain in the
reference-time domain.

Retirement counters come from a coordinator-side corrected-retire ledger, not
producer lookahead counters. L1D/L2/LLC/CHA/DRAM fields are target event deltas:
an access belongs to the window in which its cache/shared request commits.
Consequently, a memory request may appear one window before its owner retires;
all deltas conserve over the complete run.

The command line can emit a `fastsim-windows-v1` JSON document for a fixed
frequency assignment. The document records the reference frequency, initial
per-core frequencies, and the active frequency again in every core window:

```bash
./build/fastsim simulate \
  --measurement-scope user \
  --config configs/gem5-v28_1-time-epoch.cfg \
  --manifest traces/manifest.txt \
  --core-frequencies-hz 3000000000,2400000000,1800000000,1500000000 \
  --window-time-ns 100000 \
  --output windows.json
```

Use `--window-instructions N` instead of `--window-time-ns N` for instruction
windows. Runtime frequency decisions use the C++ controller interface above.

## V1 compatibility boundary

DVFS currently requires `core.model=interval_weave` and
`sim.interval_scheduler=time_epoch`. The v1 frequency path requires a single
weave pass and rejects the experimental causal/response retime, ROB-head
suffix replay, corrected-suffix carry, and post-commit-store options. The
canonical FR-FCFS shared-state path remains active, while its
optional post-pass timing repair is skipped after DVFS has been activated.

Functional warmup still runs at the reference frequency. A non-reference
frequency cannot be selected before that warmup boundary in v1; change it after
the first measurement window. The static one-thread-per-core binding also means
DVFS changes timing/progress but cannot create OS scheduling, migration, or
frequency-dependent functional traces.

Shared responses are committed at an epoch boundary before control returns.
V1 converts their response feedback into local cycles at issue time; it does
not yet preserve an explicit physical completion deadline for a response that
would remain outstanding across a later DVFS boundary. That is the next
accuracy refinement for sub-epoch/outstanding-request DVFS.
