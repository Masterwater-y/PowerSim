# Perf-aligned PMU catalog and residual-error triage (2026-09-30)

Authoritative reference for FastSim ↔ gem5 accuracy reporting. All accuracy
reports from 2026-09-30 forward MUST score against these ten fields using
per-1000-instruction rates; native Ruby/SLICC oracle stays as internal
diagnostic only.

## Sources

- Design: `docs/perf-pmu-alignment-layer-20260928.md`,
  `docs/perf-fb-hit-alignment-20260928.md`,
  `docs/perf-mshr-hit-mirror-alignment-20260928.md`.
- gem5 emitter: `gem5-collection/overlay/src/cpu/o3/probe/tao_trace.cc`
  (`accountCplDataPmu`, lines ~3183-3252) and `.hh` counter block
  (lines 776-785).
- FastSim emitter: `src/simulator.cpp` classifier at line 22853 (shared-hop) and
  early bump sites at lines 18300, 18954, 21674, 22417, 22436, 22452, 22488,
  22594, 22645, 22711. Counter block: `include/fastsim/types.hpp:2296-2305`.
- Full-matrix report generator: `tmp/validation-perf-rerun-20260930/
  emit_perf_aligned_report.py`.
- Latest matrix result: `tmp/validation-perf-rerun-20260930/
  perf-aligned-report.md` (35 cases, buffer-114 aligned).

## Ten fields

| # | Field | Perf event ~ | gem5 hook | FastSim call site | Full-matrix WAPE | Signed bias | Status |
|--:|---|---|---|---|--:|--:|---|
| 1 | `perf_retired_loads` | `MEM_INST_RETIRED.ALL_LOADS` | `accountCplDataPmu` load branch, line 3226 | shared-hop 22855 + `bump_perf_forwarded_load` 22440 + `bump_perf_sequencer_coalesced` 22424 + `bump_perf_private_hit_classifier` 22462 | 20.68% | -20.60% | Known undercount (gap A/B below) |
| 2 | `perf_retired_stores` | `MEM_INST_RETIRED.ALL_STORES` | line 3237 | shared-hop 22868 + `bump_perf_sequencer_coalesced` 22422 + `bump_perf_private_hit_classifier` 22469 | 4.46% | -3.16% | Aligned |
| 3 | `perf_l1d_load_hits` | `MEM_LOAD_RETIRED.L1_HIT` | line 3231 (mshr_hit=false) | shared-hop 22862 + `bump_perf_private_hit_classifier` 22464 | 36.58% | +18.88% | FB_HIT reclassification off — see fix C |
| 4 | `perf_l1d_load_misses` | `MEM_LOAD_RETIRED.L1_MISS` | line 3234 | shared-hop 22864 + `bump_perf_private_hit_classifier` 22466 | 8.80% | -6.44% | Aligned within contract (deferred-install still off) |
| 5 | `perf_l1d_load_fb_hits` | `MEM_LOAD_RETIRED.FB_HIT` | line 3229 (mshr_hit=true) | `bump_perf_forwarded_load` 22441 + `bump_perf_sequencer_coalesced` 22425 + shadow `try_bump_perf_fb_hit_mirror` 22506 | 92.45% | -92.45% | Gate `core.perf_fb_hit_mirror` off; enabling it is fix C |
| 6 | `perf_l2_accesses` | `L2_RQSTS.REFERENCES` | line 3240 | shared-hop 22872 + `bump_perf_private_hit_classifier` 22473 | 9.14% | -7.11% | Aligned |
| 7 | `perf_l2_demand_misses` | `L2_RQSTS.ALL_DEMAND_MISS` | line 3242 | shared-hop 22876 | 13.28% | -11.41% | Diagnostic; scales with L1-miss coverage (fix D) |
| 8 | `perf_l2_writebacks` | `L2_TRANS.L2_WB` | line 3250 | shared-hop 22884 (`preview.l2_evicted && preview.l2_evicted_dirty`) | n/a | n/a | gem5 hook currently emits 0 — `PerfMemClass::l2_writeback` hardcoded false at construction sites 3117, 3137, 4324, 6179; needs L2 replacement probe wiring |
| 9 | `perf_llc_accesses_core` | `LONGEST_LAT_CACHE.REFERENCE` (core-scoped) | line 3243 | shared-hop 22877 | 13.28% | -11.41% | Aligned; tracks field 7 |
| 10 | `perf_llc_misses_core` | `LONGEST_LAT_CACHE.MISS` (core-scoped) | line 3245 | shared-hop 22879 | 6.90% | +0.74% | Aligned |

Excluded on purpose (rejected earlier this session):

- All `UNC_CHA_TOR_*` uncore events. gem5 Ruby directory ≠ Intel mesh CHA.
- `MEMORY_ACTIVITY.STALLS_L*_MISS`. Perf topdown/cycles-activity family is
  latency-window, not populations we emit.
- `L2_TRANS.L2_WB` is retained but read-only until the gem5 wiring is fixed.

## Residual-error triage

### Fix A — `perf_retired_loads` under-count (bias -20.6%)

**Where the deficit sits.** LBM/omnetpp/zstd (heavy buffered stores) lose 40-46
% of loads; stockfish/graph500/neutron/tealeaf are within 6%. On
`uarch-baseline-c04-782.lbm_r`, FastSim `perf_retired_loads=127.07/1000 inst`
vs gem5 `239.42/1000 inst`; the store rate is 147.6 vs 152.0 (matches). The
missing 112 loads/1K land in FastSim's `memory_uops` (~384/1K) but not in any
retired-load bucket. Stores are unaffected because the buffered-store demand
replay always reaches `replay_memory_event -> replay.shared_stage`.

**Coverage gap A1 — functional-carried loads with no shared hop.** In
`src/simulator.cpp:21660-21676` the preview-epoch filter routes an event into
`materialized` (which later calls a `replay_shared` path that bumps at 22855)
OR into `bump_perf_private_hit_classifier` for the private-preview branch. The
`(preview.functional_carried && !preview.shared_timing.uncore_request)` branch
falls through both. `replay_carried_memory_event` (invoked later at 21691 only
if the event WAS materialized) contains no perf bump either. On working-set
lines the same core sees repeated hits and the earlier chunk's carried
private_previewed feedback is reused without re-visiting the classifier.

Fix A1: add a third `else if` in the filter for the carried-and-not-uncore
case:

```cpp
} else if (preview.functional_carried &&
           !preview.shared_timing.uncore_request) {
    bump_perf_private_hit_classifier(pending.core, event, preview);
}
```

or, equivalently, call the classifier inside `replay_carried_memory_event` at
its non-materialized entry. Timing-neutral. Expected to eliminate most of the
LBM/omnetpp deficit (verify: LBM c04 loads should climb from 127 to ~230/1K).

**Coverage gap A2 — private-preview classifier gated on
`buffered_store_prepare`.** `bump_perf_private_hit_classifier` at line 22460
skips both load and store bumps when `preview.buffered_store_prepare` is true.
That is only ever true for stores at prepare-time, so live loads are not
affected. Retained here because prior debugging confused the two paths — no
action needed.

**Coverage gap A3 — sph_exa window drift.** sph_exa's baseline40 window ends
2.5× earlier than the perf-rerun window, so absolute counts diverge by design.
Even per-1K normalization does not fully recover this because the sub-region
executed by FastSim has a different load-to-instruction mix than the full
perf-rerun region. Not fixable by counter surgery; either extend baseline40
sph_exa or drop it from the miss-gate check.

### Fix B — small pull-through into other fields

After A1 lands, `perf_l1d_load_hits` and `perf_l1d_load_misses` will each grow
proportional to their share of the newly-classified loads. Expected direction:

- `perf_l1d_load_hits`: pushes further positive (worse bias) if
  `core.perf_fb_hit_mirror` stays off, because most newly-classified loads on
  LBM are L1-hits from buffered fills. This is why Fix C is a prerequisite.
- `perf_l1d_load_misses`: stays within -10% (already at -6.4%).
- `perf_retired_stores`: unchanged.

### Fix C — `perf_l1d_load_hits` inflation (bias +18.9%)

**Root cause.** gem5 splits L1_HIT vs FB_HIT by
`completed->second.mshr_same_line_outstanding` at demand-time (line 3228). On
memory-bound workloads gem5 emits 50-150 FB_HITs/1K inst — see stockfish 52,
tealeaf 99, namd 146, nab 116. FastSim's `perf_fb_hit_mirror` shadow is coded
but gated behind `core.perf_fb_hit_mirror` which defaults to `false`
(`include/fastsim/config.hpp:739`). With the gate off, all sequencer-coalesced
and forwarded loads still land in FB_HIT (via `bump_perf_sequencer_coalesced` /
`bump_perf_forwarded_load`), but the much larger class of "second load to a
line whose fill is still in flight and did NOT go through the L1-MSHR
alias-coalesce path" is silently classified as L1_HIT.

**Fix C1 — enable the shadow.** Flip `core.perf_fb_hit_mirror=true` in the
canonical baseline config (`configs/gem5-fs-native-kernel.cfg` and any
override that inherits from it, including
`tmp/validation-perf-rerun-20260930/buffer114.cfg`). The mirror at
`src/simulator.cpp:22488-22516` reclassifies a load into FB_HIT if its line
still has a live shadow entry (primary_fill_cycle > issue). Timing-neutral,
never touches `private_level`, latency, MSHR, dispatch, replay.

**Fix C2 — expand the shadow horizon.** The current shadow inserts an entry
only when `!at_l1` (the load itself missed L1 and issued a fill). That
matches primary misses but not the gem5 `mshr_same_line_outstanding`
population, which also covers the case where an earlier store's prefetch is
still in flight. Amend the insert path to also seed a shadow entry when a
non-buffered store issues an L2/LLC/memory request whose line has no prior
shadow. This still bumps zero timing state. Expected effect on stockfish c04:
`perf_l1d_load_fb_hits` should move from 7 to roughly 40-50 per 1K, and
`perf_l1d_load_hits` should drop from 215 to ~170-180 per 1K, converging on
gem5's 179.

**Fix C3 (bigger, later) — deferred L1D install.** `core.l1d_deferred_install`
already has a config knob but no implementation
(`include/fastsim/config.hpp:748`, doc
`docs/l1d-deferred-install-alignment-20260928.md`). This is the structural
solution: keep buffered-store prepares out of `private_caches_[core]` until
the shared fill returns. It would push `perf_l1d_load_misses` from -6.4% to
near zero on LBM and also give the shadow a longer horizon to see followers.
Not required for the immediate ≤10% miss gate.

### Fix D — L2 demand-miss and LLC-access small-count noise

`perf_l2_demand_misses` WAPE 13.28% is amplified by low-density workloads
(nab, sph_exa, stockfish) where absolute counts are 0.06-0.9 per 1K inst.
Their APE looks large but the delta is < 0.7 per 1K. Full-matrix bias
(-11.41%) is dominated by heavy-miss cases (graph500 -18%, omnetpp -35%) and
will move within contract once Fix A lands (more retired loads → more
downstream misses). No separate fix required; recheck after A.

## Recommended sequence

1. Apply Fix A1 (add functional-carried classifier branch).
2. Enable Fix C1 (flip `core.perf_fb_hit_mirror` on in the baseline).
3. Rerun the 35-case matrix through
   `tmp/validation-perf-rerun-20260930/emit_perf_aligned_report.py`.
4. If `perf_l1d_load_hits` bias is still > 10%, layer Fix C2 (expand shadow
   seeding at store-issued fills).
5. Fix C3 (`l1d_deferred_install`) remains a separate proposal; do NOT bundle
   it into this change because it changes timing.

## Landed status (2026-09-30, Fix A1 + A2 + C1)

**Baseline is now Fix C1**. `core.perf_fb_hit_mirror` C++ default flipped to
`true` (`include/fastsim/config.hpp:739`), and the canonical alias
`configs/gem5-fs-native-kernel.cfg` sets it explicitly for reproducibility.
Disable with `core.perf_fb_hit_mirror = false` for A/B tests.

Full-matrix result on 36 cases (32 with gem5 kernel-events scoring):

| PMU field | WAPE | signed bias | Δ vs pre-fix |
|---|--:|--:|--:|
| perf_retired_loads | 3.17% | -2.45% | −17.5 pp |
| perf_retired_stores | 5.11% | -3.70% | −0.8 pp |
| perf_l1d_load_hits | 31.28% | +10.73% | −5.3 pp |
| perf_l1d_load_misses | 8.99% | -6.50% | ≈0 |
| perf_l1d_load_fb_hits | 57.04% | -25.74% | −35.4 pp |
| perf_l2_accesses | 9.46% | -7.24% | ≈0 |
| perf_l2_demand_misses | 13.33% | -11.41% | ≈0 |
| perf_llc_accesses_core | 13.33% | -11.41% | ≈0 |
| perf_llc_misses_core | 6.92% | +0.47% | ≈0 |

CPI: bit-identical to pre-fix on all 35 common cases (Δ < 1e-9). Aggregate
CPI MAE 0.06176 cyc/macro (MAPE 7.47%) on the common 35-case set;
0.06184 cyc/macro on Fix C1's 36-case set.

Landed pieces:
- Fix A1 — `interval_corrected_suffix_carry` classifier branch at
  `src/simulator.cpp:~21676`. Currently a no-op because the baseline chain
  keeps `sim.interval_corrected_suffix_carry = false`; retained as a
  guarded safety net for callers that enable suffix-carry.
- Fix A2 — forwarded-load handling in `bump_perf_private_hit_classifier`
  (`src/simulator.cpp:~22462`). Removes the early-return on
  `event.forwarded_load` and bumps `perf_retired_loads +
  perf_l1d_load_fb_hits` for loads that resolve entirely within the store
  buffer. Primary contributor to the `perf_retired_loads` collapse from
  bias −20.60% to −2.45%.
- Fix C1 — MSHR-hit shadow enabled by default. See
  `docs/perf-mshr-hit-mirror-alignment-20260928.md` for the shadow-window
  contract (timing-neutral, no free parameters, invariant preserved:
  `retired_loads == l1_hits + l1_misses + fb_hits`).

Artifacts:
- Validator output: `tmp/validation-perf-rerun-20260930/output-fixC1/`
- Report: `tmp/validation-perf-rerun-20260930/perf-aligned-report-fixC1.md`
- Overlay used: `tmp/validation-perf-rerun-20260930/buffer114.cfg`

Remaining above-10% gaps and their honest scope:
- `perf_l1d_load_hits` +10.73% and `perf_l1d_load_fb_hits` -25.74% —
  functional gap in FS `Store_Prepare` L1D install. Fix C3
  (`core.l1d_deferred_install`) is the correct next step; it is a
  **timing** change and must be validated as an independent proposal.
- `perf_l2_demand_misses` / `perf_llc_accesses_core` -11.41% —
  bucket-audit on 2026-09-30 (see `## L2 demand-miss bucket audit`)
  showed FS five-bucket L2 miss classification is already conserved
  and per-class-name equivalent to gem5. The residual is not a report-
  only classifier gap; it is the downstream tail of the same
  `Store_Prepare` L1D-install artefact and is folded into **Fix C3**.
  There is no separate report-only Fix F.

## L2 demand-miss bucket audit (2026-09-30)

Aggregate four-core snapshot on the c04 tier, buffer-114 overlay:

| case | L2 accesses FS vs gem5 (per 1K inst) | L2 demand miss FS vs gem5 (per 1K inst) | miss/access ratio FS vs gem5 |
|---|---|---|---|
| omnetpp_r | 13.44 vs 19.86 (-32%) | 0.67 vs 1.04 (-36%) | 4.96% vs 5.23% |
| graph500_s | 56.76 vs 63.01 (-10%) | 30.07 vs 36.99 (-19%) | 52.98% vs 58.71% |
| neutron_s | 34.49 vs 36.66 (-6%) | 20.72 vs 23.48 (-12%) | 60.06% vs 64.03% |
| lbm_r | 34.73 vs 36.83 (-6%) | 21.13 vs 22.39 (-6%) | 60.85% vs 60.79% |

FS five-bucket `perf_l2_demand_misses` split (aggregate, c04):

| case | read_tag | write_demand | remote_supply | permission_upgrade | follower |
|---|--:|--:|--:|--:|--:|
| omnetpp_r | 11,508 | 23 | 40 | 686 | 0 |
| graph500_s | 633,108 | 1,471 | 35,718 | 74,689 | 0 |
| neutron_s | 454,542 | 58 | 69 | 336 | 17 |
| lbm_r | 142,224 | 5,368 | 13 | 418,893 | 0 |

Findings:

1. **FS classification is conserved and correct.** For every case,
   `sum(read_tag + write_demand + remote_supply + permission_upgrade +
   follower) == totals.perf_l2_demand_misses`, and `l2_follower_misses`
   is effectively zero, so the `kMergedMemory` exclusion at
   `src/simulator.cpp:22873-22894` is not silently dropping traffic.
2. **`permission_upgrades` name conflict is not a bias source.** gem5
   `permission_upgrades` (`CoherenceAction::WB_REQUIRED`) and FS
   `l2_permission_upgrades` (buffered `Store_Prepare` reaching
   `private_level != kL1`) both land in each simulator's
   `perf_l2_demand_misses` by construction (gem5 via `path_class != 0,1`
   in `tao_trace.cc:3239-3247`; FS via the `private_level ∈ {kLlc,
   kRemote, kMemory}` branch at `src/simulator.cpp:22896-22903`), so the
   caliber matches even though the names diverge.
3. **The residual is L1D upstream, not L2 classification.** FS
   `perf_l2_accesses` is uniformly below gem5, but the FS L2
   miss-over-access ratio matches gem5 within a few percent per case.
   That means gem5 is issuing L2 requests that FS never generates — the
   same population is being satisfied earlier in FS's L1D because
   buffered `Store_Prepare` installs the line into `private_caches_`
   immediately, so follower loads on the same line hit L1D instead of
   going through the fill buffer / L2. This is the same mechanism that
   produces the +10.73% inflation on `perf_l1d_load_hits` and the
   -25.74% deficit on `perf_l1d_load_fb_hits`.
4. **No independent Fix F exists.** No timing-neutral classifier change
   can move `perf_l2_demand_misses` toward zero bias while conserving
   the five-bucket population. The correct next step is **Fix C3
   (`core.l1d_deferred_install`)**, which keeps buffered
   `Store_Prepare` out of `private_caches_[core]` until the shared fill
   returns; it is a timing change and must go through an independent
   proposal + full 36-case validation.

Artifact: bucket-audit was computed directly from
`tmp/validation-perf-rerun-20260930/output-fixC1/cases/<case>/fastsim.json`
(`totals.l2_*` + `totals.perf_l2_*`) versus
`tmp/perf-rerun-20260929/formal/source/<case>/trace-scratch/oracle/kernel-events-core*.json`
(`pmu_user_plus_kernel.perf_l2_*` + `l2_*` + `permission_upgrades`).
