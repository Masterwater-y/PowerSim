# Perf FB_HIT alignment — FastSim sequencer-coalesced classification (2026-09-28)

## Context

`docs/perf-pmu-alignment-layer-20260928.md` established the 10-field
retire-time perf-aligned PMU layer. The 4-case pilot
(`tmp/perf-pmu-alignment-pilot-20260928/fs-vs-gem5-delta.md`)
uncovered two related gaps in FastSim's classification:

- `perf_l1d_load_fb_hits` is 0 for every case (100% WAPE).
- `perf_retired_loads` under-counts by 43-96% on stockfish / omnetpp / lbm.

Both stem from the same cause: the retire-time perf classifier in the
shared-replay hop is bypassed for sequencer-coalesced loads.

## Root cause

FastSim's per-core Ruby sequencer coalesces demand loads that hit an
already-outstanding L1D MSHR request. Three call sites publish an
early-return `MemoryReplay` with `sequencer_coalesced = true` and skip
the shared-replay hop:

- `replay_sequencer_line_alias` @ `src/simulator.cpp:22399`
- Fixed-read follower branch in the sequencer batch pipeline @
  `src/simulator.cpp:18278-18298`
- Batch sequencer alias @ `src/simulator.cpp:18879-18925`

The perf classifier lives after these returns, in the shared-replay
body at `src/simulator.cpp:22711-22755`. Its `follower` gate uses
`SharedTimingPath::kMergedMemory`, which is an **LLC-side** MSHR
coalescing signal (two independent LLC requests hit the same in-flight
LLC transaction), not the L1-side sequencer coalescing. The two events
correspond to different perf populations:

| perf event | FastSim signal | Meaning |
|---|---|---|
| `MEM_LOAD_RETIRED.FB_HIT` | `sequencer_coalesced == true` | L1 MSHR already outstanding for the line |
| `MEM_LOAD_RETIRED.L1_MISS` | `path == kMergedMemory` or plain private miss | Line not in L1 and not already outstanding |
| `MEM_LOAD_RETIRED.L1_HIT` | `preview.private_level == kL1` | Line already resident in L1 |

Because the sequencer-coalesced loads never reach the classifier, both
`perf_retired_loads` and `perf_l1d_load_fb_hits` are silently dropped;
the balance identity
`loads == l1_hits + l1_miss + fb_hit`
holds only because both sides are zero for that population.

## Fix

Three changes, all timing-neutral, all in `src/simulator.cpp`:

1. **New helper `bump_perf_fb_hit(core, event)`** — writes to
   `cores_[core]->total`. Instruction fetches are skipped (perf counts
   data loads). Stores go to `perf_retired_stores`; loads and atomics
   go to `perf_retired_loads` and `perf_l1d_load_fb_hits`.
2. **Call the helper at each early-return site**: line 22399 (before
   the `return replay`), lines 18283 and 18915 (right where the alias
   replay is constructed).
3. **Reclassify the shared-hop `follower` in the perf bucket**:
   `SharedTimingPath::kMergedMemory` now falls under
   `perf_l1d_load_misses` instead of `perf_l1d_load_fb_hits`. The
   `follower` variable is removed from the load-bucketing branch; the
   L2/LLC branch keeps its existing behavior because LLC-merged misses
   still generate one L2 request but not a distinct LLC access
   (`llc_merged_misses` is a subclass of `perf_l2_demand_misses`, not a
   new access).

## Invariants after the fix

- `perf_retired_loads == perf_l1d_load_hits + perf_l1d_load_misses + perf_l1d_load_fb_hits`
- `perf_l1d_load_misses <= perf_l2_accesses`
- `perf_l2_demand_misses <= perf_llc_accesses_core`
- `perf_llc_misses_core <= perf_llc_accesses_core`
- Speculative flush must NEVER increment `perf_*` counters (unchanged).

The alias sites already run only on committed retirement paths, so the
speculative-flush invariant is preserved by construction.

## Timing neutrality

The helper writes only to `CoreTotals` scalar counters. No cycle,
latency, MSHR state, transient-ready table, or `MemoryReplay` timing
field is touched. The three alias functions still return their
original `MemoryReplay` verbatim.

## Rollback plan

If this fix regresses CPI or non-perf PMU on the full matrix, revert
the four hunks in `src/simulator.cpp` and restore the previous
`follower` gate (or gate the helper behind
`config_.perf_fb_hit_classification`).

## Expected pilot delta

| case | before loads |Δ| | before fb_hit |Δ| | expected loads |Δ| after | expected fb_hit |Δ| after |
|---|---:|---:|---:|---:|
| stockfish_r c04 | 61.4% | 100% | <10% | <10% |
| omnetpp_r c04 | 95.6% | 100% | <10% | <10% |
| lbm_r c04 | 43.5% | 100% | <5% | <5% |
| graph500_s c04 | 13.4% | 100% | still moderate | still moderate |

graph500's `fb_hit` truth is close to its `l1_miss`, so its residual
error is expected to be dominated by other sources (indirect-load
mispredict, coherence downgrade) — those are outside this fix.

## Post-implementation amendment (2026-09-28, v3)

The initial hypothesis (sequencer-coalesced L1 MSHR hits are the
missing population) was **partially wrong**. After landing the three
`bump_perf_sequencer_coalesced` alias hooks, the 4-case pilot showed
**no change** in the perf numbers. Diagnosis via `jq
.configuration` on the FastSim output revealed that
`ruby_sequencer_line_coalescing = false` in the baseline configuration
chain — the three alias functions all early-exit at
`sequencer_line_table_enabled()`, so the hooks are inert.

Re-scanning `.totals` uncovered the real root cause:
**`forwarded_loads` on lbm is 2,742,182 ≈ gem5 `fb_hit` = 2,288,424**.
The FastSim `core.store_forwarding` optimization has its own dedicated
functional path `forwarded_load_replay` (`src/simulator.cpp:7691`),
which is entered via three call sites (`7718`, `22544`, `22609`).
All three early-return with a synthesized `MemoryReplay` and skip both
the shared-hop perf classifier *and* the L1-MSHR alias paths — so
store-forwarded loads were never counted anywhere in the perf layer.

On real hardware, `perf` classifies store-forwarded loads as
`MEM_LOAD_RETIRED.FB_HIT`: they are retired loads whose data is
resolved out of the store queue / fill-buffer path, not the L1 data
array. Intel's counter definition treats them as fill-buffer hits.

### Real fix landed (v3)

Added helper `bump_perf_forwarded_load(core, event)` next to
`bump_perf_sequencer_coalesced` at `src/simulator.cpp:22406`. Called
at the two committed replay entries:

- `replay_memory_event` @ `src/simulator.cpp:22544`
- `replay_previewed_memory_event` @ `src/simulator.cpp:22609`

The `preview_memory_event` call site (`7718`) is preview-only and
never publishes final retirement, so it is intentionally *not*
instrumented — otherwise the preview would double-count. Stores
never take the `forwarded_load` path, so `perf_retired_stores` is
unaffected; instruction fetches are skipped (`event.instruction_fetch`
is checked at helper entry).

The `bump_perf_sequencer_coalesced` helper and its three call sites
are **kept in place** but currently inert. They activate when
`ruby_sequencer_line_coalescing = true` is enabled in a future config
sweep. Removing them would just churn the tree.

### Pilot result (v3, vs v2)

| workload | WAPE v2 | WAPE v3 |
|---|---:|---:|
| stockfish_r c04 | 50.29% | 49.04% (only marginal — its FB_HIT is real MSHR-coalesced, not forwarded) |
| omnetpp_r c04 | 75.87% | 74.09% |
| **lbm_r c04** | **28.44%** | **8.66%** |
| graph500_s c04 | 40.75% | 40.75% |

lbm's FB_HIT gap closed from -100% to +5.16%. Retired loads gap closed
from -43.5% to -11.6%. Full detail:
`tmp/perf-pmu-alignment-pilot-20260928/fs-vs-gem5-delta-v3.md`.

### Follow-up gap (still open)

stockfish/omnetpp still under-count FB_HIT by ~93%. Their FB_HIT truth
is dominated by *MSHR-coalesced true demand loads*, not
store-forwarding. Two paths to close this:

1. Enable `ruby_sequencer_line_coalescing = true` in the baseline
   config. The three v2 hooks then fire and this closes automatically.
   Risk: enabling changes CPI/timing on the full matrix, needs
   guardrail validation.
2. Add a functional-only mirror counter that classifies "would have
   been MSHR-coalesced" loads without changing timing.

Either path is out of scope for this doc; will spin a separate
`docs/perf-mshr-hit-alignment-*.md` when the timing/config decision is
made.


