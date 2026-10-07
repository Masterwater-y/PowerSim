# Perf MSHR-hit mirror alignment (2026-09-28)

## Context

`docs/perf-fb-hit-alignment-20260928.md` closed the store-forwarding
class of `MEM_LOAD_RETIRED.FB_HIT` by counting FastSim's
`forwarded_load` path. On the 4-case pilot the residual gap is:

| workload | fb_hit APE (v3) |
|---|---:|
| lbm_r c04 | +5.16% |
| stockfish_r c04 | -92.90% |
| omnetpp_r c04 | -93.72% |
| graph500_s c04 | -65.94% |

stockfish/omnetpp/graph500's FB_HIT truth is dominated by
**MSHR-coalesced true demand loads** — a second load to a line whose
L1 miss request is still outstanding. On real hardware `perf`
classifies these loads as FB_HIT (data returned via the fill buffer
path, never entering the L1 data array). FastSim's baseline runs with
`ruby_sequencer_line_coalescing = false`, so the three v2 alias hooks
never fire. Enabling that toggle in the baseline is a *timing*
change: it makes the follower's `latency_cycles` equal the residual
fill time instead of the full L2 or shared-hop path.

## Approach: reporting-only shadow classifier

Rather than change the timing model, introduce a **reporting-only**
mirror that reclassifies followers into the FB_HIT bucket after retire
without touching any timing decision.

### Contract

- **Timing-neutral**: only writes to `CoreTotals` scalar fields.
  Never participates in `private_caches_->access`, `shared_->access`,
  MSHR admission, `MemoryReplay.latency_cycles`, dispatch, or replay.
- **Report-only**: the mirror is invisible to every callee of
  `replay_memory_event` and `replay_previewed_memory_event`; the
  `MemoryReplay` returned to the caller is byte-identical.
- **Retire-time only**: the shadow table is written at the same
  point where perf counters are bumped (committed retire path). No
  speculative-flush path writes it, matching the existing perf-counter
  invariant.
- **No feedback**: shadow entries are never consumed by sequencer,
  fill, eviction, dispatch, TLB, or any timing component.

### Shadow table

Per-core:

```
struct PerfFbHitShadowEntry {
    std::uint64_t primary_fill_cycle;  // cycle at which the outstanding
                                       // L1 fill completes
};
absl::flat_hash_map<std::uint64_t /*line*/, PerfFbHitShadowEntry>
    perf_fb_hit_shadow_[core];
```

### Lifecycle rules

- On a load that FastSim classifies as `perf_l1d_load_misses`
  (i.e. the primary miss): if the shadow has no entry for `line`,
  insert `{primary_fill_cycle}`. `primary_fill_cycle` is the retire-
  reported fill completion, which is available as the sum of the retire
  cycle plus `replay.latency_cycles` (or the equivalent
  `replay.shared_response_cycle` for shared-hop retirements).
- On a subsequent load to the same `line` while the shadow entry is
  live (i.e. current retire cycle `<` `primary_fill_cycle`): treat
  this as a mirror FB_HIT. Two effects on the reporting counters
  only:
  - Subtract 1 from the bucket the load was previously classified
    into (typically `perf_l1d_load_hits` if FS thinks it is an L1 hit
    because the line has been installed at prepare-time, or
    `perf_l1d_load_misses` if it entered the shared hop).
  - Add 1 to `perf_l1d_load_fb_hits`.
  - `perf_retired_loads` is unchanged (already counted).
- Shadow entry is expired lazily: on any read to `line` when
  `now >= primary_fill_cycle`, drop the entry before classification.
- Instruction fetches never touch the shadow (perf FB_HIT is data-only).
- Stores never touch the shadow.
- Store-forwarded loads never touch the shadow (they go through
  `bump_perf_forwarded_load` and are already counted as FB_HIT).

### Speculative-flush safety

The mirror sits inside the perf-classification block, which is only
entered on committed retire. Speculative loads never reach
`bump_perf_*` and therefore never write the shadow.

## What this does and does not fix

| | Fixed? |
|---|---|
| stockfish/omnetpp/graph500 `perf_l1d_load_fb_hits ≈ 0` reporting | Yes |
| `perf_retired_loads == hits + miss + fb_hit` balance | Yes |
| Perf-language semantic parity with `MEM_LOAD_RETIRED.FB_HIT` | Yes |
| CPI parity with gem5 on cases where FB_HIT latency < merged-L2 | **No** — timing model still uses the shared-hop path |

The last row is the honest scope boundary: **the mirror closes the
report-layer gap only**. If a workload's CPI depends on FB_HIT
latency being the residual-fill time (as it is in gem5), the correct
fix is to enable `ruby_sequencer_line_coalescing = true` in the
baseline config and revalidate the full matrix — an independent
timing change with its own guardrail.

## Config gate

New knob: `core.perf_fb_hit_mirror` (default: **true** once validated;
default **false** at first landing).

```
core:
  perf_fb_hit_mirror: true
```

## Interaction with existing hooks

- `bump_perf_forwarded_load` fires first (on `event.forwarded_load`),
  so store-forwarded loads bypass the shadow.
- `bump_perf_sequencer_coalesced` fires only when
  `ruby_sequencer_line_coalescing = true` — mutually exclusive with the
  mirror in practice. If both fire on the same event, the mirror
  short-circuits when the event's `sequencer_coalesced` bit is set to
  avoid double counting.
- Shared-hop `kMergedMemory` (LLC-merged) is orthogonal: the shadow
  keys off L1-line identity plus L1-fill cycle, not shared-transaction
  identity.

## Rollout

1. Land this doc.
2. Add the `perf_fb_hit_mirror` config knob (default false at first).
3. Implement `bump_perf_fb_hit_mirror` and the shadow table in the
   committed replay paths.
4. Build + unit tests.
5. Smoke on the 4-case pilot with the knob **off**: byte-identical
   perf and CPI vs current baseline (regression gate).
6. Smoke on the 4-case pilot with the knob **on**: perf CPI must be
   byte-identical to knob-off; only `perf_l1d_load_hits`,
   `perf_l1d_load_misses`, `perf_l1d_load_fb_hits` reshuffle.
   Success criterion: fb_hit APE drops below 30% on
   stockfish/omnetpp/graph500 (subject to shadow-window vs gem5-mshr
   alignment residuals).
7. If CPI is bit-identical and PMU improves, flip the default to true
   and land a full-matrix guardrail entry.

## Not fixed here

- Graph500 `perf_retired_stores` +174% — unrelated store-side issue.
- Gem5 `perf_l2_writebacks == 0` — overlay scope handling.
- Any CPI-side dependence on FB_HIT latency semantics.
