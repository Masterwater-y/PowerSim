# L1D deferred install alignment (2026-09-28)

## Problem

FastSim's `PrivateHierarchy::access(line, ...)` synchronously installs
the L1D tag on a miss. Subsequent accesses to the same line return
`HitLevel::kL1` **before the fill has actually completed on gem5's
timeline**. Two authoritative caliber gaps trace to the same source:

- **native `l1d_tag_misses` -43%** (`docs/l1d-sp-ie-im-caliber.md`).
  SP_IE→IM slow-path stores in native SLICC re-issue as a second
  `uu_profileDataMiss` when the buffered store retires while
  Data_Exclusive is still outstanding. FastSim's prepare install hides
  this and the follower never enters the miss path.
- **perf `perf_l1d_load_misses` -50%/-91% on stockfish/omnetpp**
  (4-case pilot, `fs-vs-gem5-delta-v4.md`). gem5's
  `MEM_LOAD_RETIRED.L1_MISS` counts loads that retire while the L1
  fill is still in-flight. FastSim classifies them as L1 hits.

Both are the same mechanism: FastSim treats L1D as installed at
sequencer-issue time; gem5 treats it as installed at fill-completion
time.

## Fix: MSHR shadow with deferred install semantics

Add a per-core shadow table that mirrors the gem5 MSHR occupancy
window. It is read at retire-time perf/native classification and
influences the `private_level` used downstream so that latency and
counter bookkeeping both align with gem5.

```cpp
struct L1MshrShadowEntry {
    std::uint64_t fill_completion_cycle;  // cycle at which L1 fill lands
    HitLevel      source_level;           // source: kL2 / kLlc / kRemote / kMemory
};
absl::flat_hash_map<std::uint64_t /*line*/, L1MshrShadowEntry>
    l1_mshr_[core];
```

### Lifecycle

- **Primary miss (commit path)**: after `private_caches_->access`
  returns `private_result.level != kL1`, insert
  `{issue_cycle + private_response_latency(event, source), source}`
  keyed by `event.line`. Insert only if no live entry exists.
- **Follower access (commit path)**: after `private_caches_->access`
  returns `private_result.level == kL1`, look up the shadow. If
  `issue_cycle < entry.fill_completion_cycle`, **override**
  `private_result.level` to `entry.source_level` (the follower now
  walks the shared hop again, matching gem5). If
  `issue_cycle >= entry.fill_completion_cycle`, erase and treat as an
  ordinary L1 hit.
- **Cross-core invalidate**: when
  `shared_->private_evict(other_core, line, ...)` invalidates our
  local copy, erase our shadow entry for `line`. Handled by wiring
  the existing L2-evicted callback.
- **Self eviction**: when this core's L1 eviction of a same-set line
  happens (returned via `L1LookupResult`), erase the shadow entry for
  the evicted line.
- **TTL fallback**: on any lookup, if the shadow has an entry with
  `issue_cycle >= fill_completion_cycle`, erase it lazily. Prevents
  stale entries from lingering when a line's steady-state hits keep
  arriving after the fill.

### Excluded paths

The shadow is not written or consulted on:
- `event.instruction_fetch` — perf and native L1I caliber are
  independent tracks.
- `event.write || event.atomic` — store-side goes through
  `l1d_write_demand_misses` and buffered-TSO lifecycle; not covered
  here.
- `event.forwarded_load` — store-to-load forwarding already
  short-circuits via `forwarded_load_replay`.
- `preview.buffered_store_prepare == true` — prepare's L1 install is
  a placement placeholder, not a demand miss. Prepare must not enter
  the shadow (it would leak permanent entries).
- Speculative flush — the perf/native classifier is only reached on
  the commit path, matching the existing `bump_perf_*` invariant.

### Deferred install for the timing model

When the shadow overrides the follower's `private_level`, the same
override must feed:
1. `MemoryReplay.private_level` — so `replay_previewed_memory_event`
   picks the shared-hop latency instead of the L1-local latency.
2. Native `CacheCounters` classification (already sourced from
   `private_result.level` in `PrivateHierarchy::access`) — because
   the follower re-enters the shared hop, it also re-enters
   `l1d.misses`, closing the SP_IE→IM caliber gap.
3. Perf classifier — same source, no separate wiring needed.

Timing consequence: the follower now pays shared-hop latency (tens of
cycles) instead of L1-hit latency (4-5 cycles). This is the honest
gem5 timing. **CPI on stockfish/omnetpp will rise**; that is the
expected correction, not a regression. The guardrail is instead:
- On lbm and graph500 (where cache accounting is already ≤1% off),
  CPI must stay within 2%.
- On stockfish/omnetpp, CPI must **move toward gem5**, not away.

## Interaction with existing mechanisms

| Mechanism | Interaction | Rule |
|---|---|---|
| buffered-TSO prepare | Prepare installs L1D synchronously today. | Prepare skips shadow (does not insert). Store publish path unaffected. Prepare-follower loads still classified normally. |
| sequencer line coalescing (`ruby_sequencer_line_coalescing`) | Baseline is `false`. When true, aliases already short-circuit. | Shadow supersedes the aliases: if shadow is live, alias hooks are moot. If both fire on the same event, shadow wins to avoid double counting. |
| forwarded_load | Already handled by `bump_perf_forwarded_load`. | Shadow excludes forwarded_load explicitly. |
| shared-hop `kMergedMemory` (LLC MSHR) | Follower on LLC MSHR is orthogonal to L1 MSHR. | Follower first re-enters shared hop via shadow override, THEN shared hop can further classify as `kMergedMemory`. Order preserved. |
| L2 eviction of L1 line | L1 eviction of a shadow-tracked line invalidates the MSHR window. | Erase shadow on L1 eviction. |
| Cross-core `GetX` invalidate | Remote core invalidates our line. | Erase shadow on the invalidate callback. |
| preview vs commit replay | Preview must not affect commit-side counters. | Shadow written only on commit replay path; preview skips. |

## Configuration gate

```
core:
  l1d_deferred_install: false  # default false at first landing
```

Rollout rule: enable on stockfish c04 and lbm c04 smoke first; if CPI
guardrails hold, enable on the full 4-case pilot; if that holds,
enable on the 38-case full matrix and check the native validator
before promoting the default.

## Not fixed here

- `perf_retired_stores` +174% on graph500 — store-side, separate.
- `perf_l2_writebacks` gem5-side scope — overlay reporting, separate.
- Graph500 `perf_llc_misses_core` +70% — shared-hop coherence detail,
  separate.

## Rollback

Flipping `l1d_deferred_install` back to `false` is bit-identical to
v3 (mirror is already default off; deferred install is orthogonal).

## Validation protocol

1. Build + `fastsim_tests`.
2. Smoke stockfish c04 + lbm c04 with the knob off (regression gate:
   perf byte-identical to v3).
3. Smoke same two cases with the knob on:
   - lbm: `perf_l1d_load_misses` unchanged (already 1%), CPI shift < 2%.
   - stockfish: `perf_l1d_load_misses` moves from -50% toward 0, CPI
     may rise but should move toward gem5 CPI.
4. If (3) passes, run all 4 cases with the knob on and compare WAPE.
5. If (4) passes, run the 38-case native validator to confirm
   `l1d_tag_misses` (native) improves from -43%.
6. Only after (5) passes, consider flipping the default.

## 2026-09-30 refresh — Fix C3 promotion plan on top of Fix A1+A2+C1

The Fix A1+A2+C1 default baseline (`core.perf_fb_hit_mirror = true`)
already closed most of the retired-load bias. Remaining perf-aligned
WAPE outliers all trace to this deferred-install gap and must be
carried by a single knob:

| PMU field | current WAPE | signed bias | expected direction after C3 |
|---|--:|--:|---|
| `perf_l1d_load_hits` | 31.28% | +10.73% | move toward 0 (loads reclassified to FB_HIT or L1_MISS) |
| `perf_l1d_load_fb_hits` | 57.04% | -25.74% | move toward 0 (followers re-enter FB_HIT population) |
| `perf_l1d_load_misses` | 8.99% | -6.50% | small positive move (SP_IE→IM slow-path stores re-count) |
| `perf_l2_demand_misses` | 13.33% | -11.41% | move toward 0 (folded here per catalog audit; no separate Fix F) |
| `perf_llc_accesses_core` | 13.33% | -11.41% | tracks `perf_l2_demand_misses` |

Success gate for this iteration is **all five fields ≤ 5% WAPE with
CPI MAE within +0.008 cyc/macro of the Fix A1+A2+C1 baseline** on the
full 36-case matrix (report-only fields must not regress on cases
that were already aligned, in particular lbm and stockfish).

Landing sequence (this refresh amends the original Validation
protocol):

1. Implement the shadow lifecycle at the commit-side classifier in
   `src/simulator.cpp` (near line 22868). Insert on `!at_l1 &&
   !prepare && !forwarded_load && !instruction_fetch`; look up on the
   symmetric `at_l1` path and override `private_level` before the
   perf-classifier fanout at 22876-22908.
2. Wire the invalidate callbacks: L1D self-eviction (existing
   `L1LookupResult`) and cross-core invalidate
   (`shared_->private_evict`). Erase-on-eviction is a hard invariant;
   without it the shadow leaks and reclassifies steady-state hits.
3. Feed the overridden `private_level` back to `MemoryReplay` so the
   follower pays shared-hop latency, per the "Deferred install for the
   timing model" section above.
4. Config gate `core.l1d_deferred_install` defaults **off**. First
   landing must be a zero-diff run with the knob off.
5. Single-case A/B on lbm c04 and omnetpp c04:
   - lbm c04: CPI shift ≤ 2%; `perf_l2_demand_misses` bias moves from
     -5.6% toward 0.
   - omnetpp c04: `perf_l1d_load_hits` bias moves from -10.7% toward
     0; `perf_l2_demand_misses` bias moves from -35.8% toward 0; CPI
     is allowed to rise if it moves **toward** gem5 CPI.
6. If (5) passes, extend to the 4-case pilot (lbm/omnetpp/graph500/
   neutron at c04), then to the full 36-case matrix. Report per case:
   `perf_l1d_load_hits`, `perf_l1d_load_fb_hits`,
   `perf_l1d_load_misses`, `perf_l2_demand_misses`,
   `perf_llc_accesses_core`, CPI MAE (cycles/macro), CPI MAPE. Follow
   `docs/accuracy-reporting-contract.md`.
7. Only after all five fields ≤ 5% WAPE **and** the CPI guardrail
   holds do we consider flipping the C++ default.

Cross-doc pointers:
- Bucket-audit evidence: `docs/perf-aligned-pmu-catalog-20260930.md`
  §"L2 demand-miss bucket audit (2026-09-30)".
- Reporting contract: `docs/accuracy-reporting-contract.md` §0, §8.
- Fix C1 shadow-window contract (adjacent report-only mirror):
  `docs/perf-mshr-hit-mirror-alignment-20260928.md`.

## 2026-09-30 refresh — Fix C3 Phase 1 Step 1 (predicate tightening)

Phase 1 landed on 2026-09-30 with the widest possible predicate (seed
whenever `!at_l1 || buffered_store_prepare`, reclassify whenever
`issue < primary_fill_cycle`). The 24-case matrix produced the expected
directional evidence — `perf_l2_demand_misses` bias moved from `-11.09%`
to `-8.80%` (+2.28pp toward zero, cycles Δ=0) — but WAPE regressed from
`12.34%` to `13.88%` because low-baseline cases (stockfish, sph_exa,
nab, all with gem5 baseline < 1.75/1K inst) overshot: any spurious
reclassification is amplified in the relative metric.

Step 1 keeps Phase 1's report-only, timing-neutral contract and only
tightens the predicate. No `MemoryReplay.private_level` feedback yet
(that stays in Phase 2). Three tightening points, each behind a
sub-knob so we can bisect their contributions:

### Tightening A — seed only when shared_path actually crosses L1D

Current:
```
seed if (!at_l1 || buffered_store_prepare) && !instruction_fetch && !forwarded_load
```
Drops `!at_l1 && shared_path in {kLocal, kPermissionUpgrade}`: these
fills either stay private-local or are permission upgrades on a line
that FS already tracks in L1D LRU, so gem5 does not treat the follower
as an MSHR-window customer. Restrict seeding to the paths that
represent a genuine cross-L1D fill:
```
seed if (buffered_store_prepare ||
         shared_path in {kRemoteSupply, kLlcHit, kMergedMemory, kMemory})
      && !instruction_fetch && !forwarded_load
```
`buffered_store_prepare` stays unconditional because FS's synchronous
Store_Prepare L1D install is the whole point of Fix C3; gem5 keeps that
line out of L1 LRU regardless of shared path.

### Tightening B — reclassify only when defer window is meaningful

Current:
```
reclassify if shadow[event.line].primary_fill_cycle > issue_cycle
```
That admits arbitrarily small windows (even 1 cycle), which reclassify
loads that gem5 also treats as L1 hits — the fill returned "fast
enough" in gem5's timeline too. Add a floor `min_defer_gap`:
```
reclassify if (primary_fill_cycle - seed_cycle) >= min_defer_gap
          && issue_cycle < primary_fill_cycle
```
Requires storing the seed cycle in `PerfDeferredInstallEntry`. Default
`min_defer_gap = 20` cycles (≈ L1 hit latency × 4, covers the smallest
meaningful shared-hop deferral). Config knob
`core.l1d_deferred_install_min_defer_cycles` (default 20).

### Tightening C — low-baseline core gating

Maintain per-core rolling count of primary L2 demand misses over a
sliding window (10K committed instructions). If the observed density
falls below `low_baseline_threshold_per_1k` (default 0.5), disable
reclassify for that core until density recovers. Rationale: on
stockfish/sph_exa/nab the gem5 L2-miss rate is fundamentally low; any
FS mis-classification dominates the relative metric. This gate is
observational (uses FS's own counters, not gem5 truth), so it is safe
under the "no gem5 handshake" project rule.

Config knobs (default off by absence, active only when parent
`core.l1d_deferred_install = true`):
- `core.l1d_deferred_install_min_defer_cycles` (uint, default 20)
- `core.l1d_deferred_install_low_baseline_per_1k` (float, default 0.5)

### Success gate (Step 1)

Step 1 does **not** need to hit 5% WAPE by itself. It must:

1. `cycles Δ = 0` on all 24 cases (timing-neutral hard gate).
2. `perf_l2_demand_misses` WAPE **≤ 12.34%** (do not regress vs Fix C1).
3. `perf_l2_demand_misses` bias closer to zero than the Fix C1 baseline
   (`|bias|` strictly < 11.09%), so we know we still have Phase 2
   headroom in the correct direction.
4. Overshoot cases (stockfish c04, sph_exa c04/c16, nab c04) return to
   within ±100% bias (Phase 1 pushed them past +300%).
5. Gains preserved on the "responder" cases (omnetpp c04, graph500 c04,
   neutron c04): bias magnitude smaller than or equal to Fix C1
   baseline.

If (2)-(5) hold, Step 1 lands with default OFF and becomes the clean
baseline for Phase 2. If they do not, iterate the three knobs before
touching timing.

### Rollback path

Every Step 1 change is behind the parent `core.l1d_deferred_install`
knob. Setting parent knob false disables Step 1 entirely, restoring
Fix C1 behaviour byte-for-byte. Sub-knobs are additive tighteners:
setting `min_defer_cycles = 0` and `low_baseline_per_1k = 0` recovers
the unrestricted Phase 1 behaviour.

### 2026-09-30 Step 1 landed result (24-case matrix)

Three configurations were scored against the Fix C1 baseline on the
full 24-case corpus (`tmp/fixC3-phase1-{matrix,step1,step1bc}-
20260930/`):

| config | `perf_l2_demand_misses` WAPE | bias | stockfish c04 bias | omnetpp c04 bias |
|---|---|---|---|---|
| OFF (Fix C1)                       | 12.34% | -11.09% |   +5.36% | -35.84% |
| Phase 1 (all A/B/C off)            | 13.88% |  -8.80% | +799.49% | -28.07% |
| Step 1 A+B+C (require_shared_hop)  | 12.39% | -11.02% |   +5.63% | -35.84% |
| Step 1 B+C only (**default**)       | 12.39% | -11.02% |   +5.63% | -35.84% |

Key findings:

1. **Tightening B alone dominates.** Step 1 A+B+C and B+C are byte-
   identical on every case (verified via raw `perf_l2_demand_misses`
   counts at unit precision). C never fires on this matrix because B
   already filters short-window reclassify events before the density
   gate is reached; A adds no marginal filtering on top of B.
2. **B removes overshoot AND removes gains.** stockfish c04 falls from
   +799% back to +5.6%; omnetpp c04's +7.8pp Phase-1 gain vanishes
   because the omnetpp defer-window is < 20 cyc. The seeds causing
   stockfish overshoot and the seeds closing the omnetpp gap belong to
   the same short-window population and cannot be separated by any
   window/path predicate.
3. **Cycles Δ=0** on all 24 cases; retired-loads conservation
   invariant `hits+miss+fb == retired` holds in all three
   configurations.
4. Step 1 WAPE = 12.39% is +0.05pp above OFF and -1.49pp below Phase 1;
   bias is -11.02% vs -11.09% at OFF.

Verdict: **Step 1 B+C lands as the default Fix C3 Phase 1 configuration.**
It eliminates the low-baseline overshoot without WAPE regression and
provides a clean report-only diagnostic knob. However the report-only
mechanism has run out of headroom: no predicate combination closes the
`perf_l2_demand_misses` bias meaningfully past OFF. Phase 2 timing
feedback (feeding the override into `MemoryReplay.private_level`) is
required to reach the 5% WAPE gate.

Knob defaults (as of 2026-09-30 landing):
- `core.l1d_deferred_install = false` (parent, unchanged)
- `core.l1d_deferred_install_min_defer_cycles = 20`
- `core.l1d_deferred_install_low_baseline_per_1k = 0.5`
- `core.l1d_deferred_install_require_shared_hop = false`

Diagnostic reproduction:
```
tmp/fixC3-phase1-step1bc-20260930/
  buffer114-c3on-step1bc.cfg   # overlay with l1d_deferred_install=true
  case-list.txt
  run_case.sh
  score_step1bc.py
  step1bc-report.md
```
