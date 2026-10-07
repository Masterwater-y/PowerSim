# Perf-aligned PMU alignment layer (2026-09-28)

Status: design landed; FastSim + gem5-collection overlay wired; unit tests
passing; overlay rebuilt against upstream `c8222cc`; 1-case smoke pilot
completed (lbm_r c04, 200k records/core); 4-case perf-truth alignment pilot
pending (needs `perf stat` collection on Xeon 8457C).

Implementation log:

- 2026-09-28: `CoreTotals` grew 10 `perf_*` fields (`include/fastsim/types.hpp`);
  `replay_previewed_memory_event` and the buffered-store publish site now
  bump the perf counters at retire-time; JSON reporters emit the new fields
  from `write_user_functional_pmu`, `write_user_plus_kernel_pmu`, and
  `dump_totals`; unit tests in `tests/test_main.cpp` verify the invariants
  (`perf_retired_stores` matches committed store demands, no counter fires
  for speculative flushes). `fastsim_tests` PASS.
- 2026-09-28: `gem5-collection/overlay/src/cpu/o3/probe/tao_trace.{hh,cc}`
  grew the same 10 fields on `PmuScope`, plus `PerfMemClass` and
  `SharedAttr::mshr_same_line_outstanding`; `accountCplDataPmu` now accepts
  a `PerfMemClass` at every call site and updates the perf counters at
  commit; `write_pmu` serializer emits the new fields; summary header
  publishes `perf_pmu_schema=perf-aligned-v1` next to the legacy
  `pmu_source=taotrace-path-class-v3`. Files synced into `gem5-fs/`; scons
  rebuild against upstream `c8222cc` under `yinhaolang` conda env
  (gem5.opt sha256 `97e2340a5417bdb19718934ad9dbb5cd672557036c6694ef2bfd350c963f83d3`).
- 2026-09-28: smoke pilot on `lbm_r c04, 200000 user records/core` produced
  perf_* fields for all 4 cores; L1D load balance invariant holds exactly on
  every core; LLC subset and L2→LLC invariants hold. Section 3 invariant #2
  amended (FB_HIT does not add to L2 refs). Report:
  `tmp/perf-pmu-alignment-pilot-20260928/smoke/report.md`.

## 0. Motivation

The current native reference for `l1d/l2/llc_tag_misses` in
`tools/run_native_kernel_fastsim_validation.py` reads
`native_ruby_pmu_by_scope.<scope>.hierarchy.<level>.tag_misses` from
`native-summary-core*.json`. These fields are emitted by the gem5-collection
overlay in `tao_trace.cc` and are populated from **SLICC controller
transitions** (`uu_profileDataMiss` per `state + event → new_state`
transition).

Consequences of the SLICC caliber:

- One retired store that touches `SP_IE` on its way to `M` is counted **twice**
  as a miss in the L1D bucket (roles I→IS + SP_IE→IM), because SLICC profiles
  transitions, not line fills. See Round-1 report in
  [`docs/l1d-sp-ie-im-caliber-20260928.md`](l1d-sp-ie-im-caliber-20260928.md).
- Store misses (which do not correspond to a retired load) are lumped into
  `l1d_tag_misses`; on real hardware `MEM_LOAD_RETIRED.L1_MISS` only counts
  retired loads.
- Miss profiling triggers at Sequencer / mandatory-queue enqueue
  (issue-time), whereas real-machine `MEM_LOAD_RETIRED.*` events trigger at
  retirement.

Because the downstream ML power model consumes real-perf-shaped features
(retired load L1/L2/L3 miss counts, retired store counts, LLC references,
etc.), the accuracy contract must expose a **perf-semantic authoritative
layer** and demote the SLICC transition counters to diagnostic.

## 1. Scope

- Only core-side cache miss related PMUs (uncore CHA / IMC deferred to a later
  document).
- Ten authoritative fields covering L1D load hit/miss/FB_HIT, L2 references,
  L2 demand miss, L2 → LLC writeback, LLC references, LLC miss, and retired
  load/store denominators.
- SLICC-transition counters (`l1d_read_tag_misses`,
  `l1d_write_demand_misses`, `l1d_sp_ie_im_misses`, `l1d_tag_misses`, the
  `l2_*` and `llc_*` SLICC buckets) are **preserved but demoted to
  diagnostic**; they are not deleted, they simply stop being authoritative.

## 2. Authoritative mapping table

The following 10 fields become the authoritative core-side cache-miss layer.
All three sides (perf, gem5-collection overlay, FastSim) must yield
numerically consistent values on identical workloads under identical
configuration.

| # | Perf event | Authoritative semantic | gem5-collection overlay counter | FastSim `CoreTotals` field | gem5 hook point |
|---|---|---|---|---|---|
| 1 | `MEM_INST_RETIRED.ALL_LOADS` (`0xD0/0x81`) | Retired load uops (denominator) | `perf_retired_loads` | `perf_retired_loads` | `TaoTrace::onCommit` + `inst->isLoad()` |
| 2 | `MEM_INST_RETIRED.ALL_STORES` (`0xD0/0x82`) | Retired store uops (denominator) | `perf_retired_stores` | `perf_retired_stores` | `TaoTrace::onCommit` + `inst->isStore()` |
| 3 | `MEM_LOAD_RETIRED.L1_HIT` (`0xD1/0x01`) | Retired demand load, L1D hit | `perf_l1d_load_hits` | `perf_l1d_load_hits` | commit + `hit_level == kL1` |
| 4 | `MEM_LOAD_RETIRED.L1_MISS` (`0xD1/0x08`) | Retired demand load, L1D miss (excludes FB_HIT) | `perf_l1d_load_misses` | `perf_l1d_load_misses` | commit + `hit_level >= kL2` and MSHR line not in-flight |
| 5 | `MEM_LOAD_RETIRED.FB_HIT` (`0xD1/0x40`) | Retired demand load, hit pending fill-buffer (L1 miss, MSHR has same-line in-flight) | `perf_l1d_load_fb_hits` | `perf_l1d_load_fb_hits` | commit + `hit_level >= kL2` and MSHR same-line in-flight |
| 6 | `L2_RQSTS.REFERENCES` (`0x24/0xFF`) | All requests reaching L2 (demand load + RFO + code + prefetch, line-side) | `perf_l2_accesses` | `perf_l2_accesses` | L1 controller → L2 request event |
| 7 | `L2_RQSTS.ALL_DEMAND_MISS` (`0x24/0x27`) | L2 demand miss (load + RFO + code; excludes prefetch) | `perf_l2_demand_misses` | `perf_l2_demand_misses` | L2 request event + demand type + L2 miss |
| 8 | `L2_TRANS.L2_WB` (`0xF0/0x40`) | L2 → LLC dirty writeback | `perf_l2_writebacks` | `perf_l2_writebacks` | L2 controller `PUTX` action |
| 9 | `LONGEST_LAT_CACHE.REFERENCE` (`0x2E/0x4F`) | All core-side requests reaching LLC (demand + prefetch) | `perf_llc_accesses_core` | `perf_llc_accesses_core` | L2 controller → LLC request event |
| 10 | `LONGEST_LAT_CACHE.MISS` (`0x2E/0x41`) | Core-side LLC miss | `perf_llc_misses_core` | `perf_llc_misses_core` | LLC controller miss event |

Every field uses the same identifier across the three sides. No field name is
shared with a SLICC-transition counter.

## 3. Semantic invariants

Any implementation that satisfies the three-way alignment must respect these
identities on every workload:

1. L1D load balance:
   ```
   perf_retired_loads = perf_l1d_load_hits
                      + perf_l1d_load_misses
                      + perf_l1d_load_fb_hits
   ```

2. L1D → L2 boundary. On real hardware `MEM_LOAD_RETIRED.L1_MISS` and
   `MEM_LOAD_RETIRED.FB_HIT` both count retire-time load uops that missed the
   L1 tag; only the *L1_MISS* subset issues a new L1→L2 request. FB_HIT loads
   are followers on an already-outstanding MSHR request and do **not** add to
   `L2_RQSTS.REFERENCES`. The correct invariant is therefore:
   ```
   perf_l1d_load_misses <= perf_l2_accesses    (each L1 miss triggers one L2 ref;
                                                inequality reflects code fetch,
                                                store RFO, and prefetch that
                                                also reach L2)
   ```
   The stronger `l1_misses + fb_hits <= l2_accesses` shape from the 2026-09-28
   draft was rejected by the smoke pilot (see
   `tmp/perf-pmu-alignment-pilot-20260928/smoke/report.md`) because heavy
   MSHR/fill-buffer coalescing means the FB_HIT count typically dominates
   L2 references.

3. L2 → LLC boundary:
   ```
   perf_l2_demand_misses <= perf_llc_accesses_core
   ```

4. Writeback stream is independent of the demand/prefetch request stream:
   ```
   perf_l2_writebacks is not counted in perf_l2_accesses.
   ```

5. LLC miss ⊆ LLC references:
   ```
   perf_llc_misses_core <= perf_llc_accesses_core
   ```

Pilot validation must include a check for each of these identities against
each side independently (perf tallies, gem5 overlay, FastSim replay).

## 4. Structural gaps (documented, not fixed here)

The perf-aligned layer explicitly does not eliminate the following
model-level differences; instead they are called out and expected to appear
as bounded systematic offsets in the pilot report.

### 4.1 Issue-time vs retire-time

- Perf `MEM_LOAD_RETIRED.*` only fires when a load uop reaches architectural
  retirement.
- SLICC `uu_profileDataMiss` fires at Sequencer enqueue (issue-time).
- Consequence: on branch-mispredict-heavy workloads (omnetpp, stockfish),
  SLICC over-counts by the wrong-path fraction. gem5 overlay hooks and
  FastSim writes for these fields must be placed at commit-time only.

### 4.2 Prefetch inclusion

- `L2_RQSTS.REFERENCES` and `LONGEST_LAT_CACHE.REFERENCE` include HW
  prefetch on real hardware.
- gem5 Ruby and FastSim currently model **no** HW prefetch in the default
  buffer-114 profile.
- Consequence: in the current pilot the prefetch contribution is zero on all
  three sides; if a future config enables Ruby prefetcher, the overlay hook
  must include `RubyPrefetcherProxy::ppFill` and the FastSim path must add
  a corresponding synthetic prefetch counter.

### 4.3 Store side has no L1D perf event

- Real perf provides no L1D-side store miss counter. Store misses appear
  first at L2 as `L2_RQSTS.ALL_RFO` / `L2_RQSTS.RFO_MISS`.
- FastSim `l1d_write_demand_misses` (SLICC) captures L1D store miss but has
  no perf-side authoritative counterpart. It stays as diagnostic only.
- Downstream models that need "L1D store miss" should read `perf_l2_accesses
  − perf_l2_demand_misses` (RFO issued, hit at L2) plus the RFO-miss subset,
  not any L1D field.

### 4.4 FB_HIT versus L1 miss separation

- Real perf treats `MEM_LOAD_RETIRED.L1_MISS` and
  `MEM_LOAD_RETIRED.FB_HIT` as mutually exclusive.
- In the gem5 overlay this requires reading MSHR state at commit: if the
  retiring load's line is currently in an outstanding fill-request bucket,
  count as FB_HIT; otherwise count as L1_MISS.
- The overlay already tracks outstanding MSHR entries (see
  `tao_trace.cc:6071` "MSHR coalescing 视图"); the FB_HIT logic reuses this
  view, no new bookkeeping needed.

### 4.5 Core-side LLC view versus CHA-side

- `LONGEST_LAT_CACHE.*` on Golden Cove reports **core-side** LLC lookups
  (from L2 to LLC via the mesh). Uncore CHA counters
  (`UNC_CHA_TOR_INSERTS.IA*`) report the CHA-side view and include IO/DMA.
- This document only covers the core-side view; the two are related by
  `perf_llc_accesses_core (per-core sum) ≈ UNC_CHA_TOR_INSERTS.IA
  (socket sum) − UNC_CHA_TOR_INSERTS.IO`, which is outside this scope.

## 5. gem5-collection overlay implementation

### 5.1 File touch list

- `gem5-collection/overlay/src/cpu/o3/probe/tao_trace.hh`: add
  `struct PerfPmuLedger` with the 10 counter members and per-scope buckets
  (`user`, `syscall`, `page_fault`, `irq`).
- `gem5-collection/overlay/src/cpu/o3/probe/tao_trace.cc`:
  - `onCommit(DynInstPtr &inst)`: read `inst->isLoad()` / `isStore()`,
    fetch the recorded MSHR / hit-level state, increment the corresponding
    ledger bucket by scope.
  - L1 → L2 request emit path: increment `perf_l2_accesses` and, on demand
    + L2 miss, increment `perf_l2_demand_misses`.
  - L2 → LLC request emit path: increment `perf_llc_accesses_core` and, on
    LLC miss, `perf_llc_misses_core`.
  - L2 controller `PUTX` path: increment `perf_l2_writebacks`.
- `native-summary-core*.json` schema bump to `taotrace-native-summary-v2`:
  new top-level key `perf_pmu_by_scope` alongside the existing
  `native_ruby_pmu_by_scope`.

### 5.2 Scope assignment

The scope key uses the existing overlay convention (`user`, `syscall`,
`page_fault`, `irq`). The perf `:u` / `:k` mapping is:
```
perf_scope :u  = user
perf_scope :k  = syscall + page_fault + irq
```
Pilot reports must produce both `:u` and `:k` sums.

## 6. FastSim implementation

### 6.1 Field additions in `include/fastsim/types.hpp`

Add to `struct CoreTotals`:
```
std::uint64_t perf_retired_loads = 0;
std::uint64_t perf_retired_stores = 0;
std::uint64_t perf_l1d_load_hits = 0;
std::uint64_t perf_l1d_load_misses = 0;
std::uint64_t perf_l1d_load_fb_hits = 0;
std::uint64_t perf_l2_accesses = 0;
std::uint64_t perf_l2_demand_misses = 0;
std::uint64_t perf_l2_writebacks = 0;
std::uint64_t perf_llc_accesses_core = 0;
std::uint64_t perf_llc_misses_core = 0;
```
Extend `operator+=` correspondingly.

### 6.2 Write points in `src/simulator.cpp`

- `retire_load()`: read `HitLevel` and MSHR-pending state for the retiring
  load; bump exactly one of `perf_l1d_load_hits`, `perf_l1d_load_misses`,
  `perf_l1d_load_fb_hits`, and also bump `perf_retired_loads`.
- `retire_store()`: bump `perf_retired_stores`.
- L1 → L2 request emit paths (the same points that currently update
  `l2_accesses`): bump `perf_l2_accesses`; on demand-type + L2 miss, bump
  `perf_l2_demand_misses`.
- L2 → LLC request emit: bump `perf_llc_accesses_core`; on LLC miss, bump
  `perf_llc_misses_core`.
- L2 writeback path: bump `perf_l2_writebacks`.

Speculative flush must not increment any `perf_*_retired_*` counter. Where
the current SLICC counters update at issue-time, the new `perf_*` counters
update at retire-time only.

### 6.3 JSON reporter changes in `src/main.cpp`

Extend the three writers (`write_user_functional_pmu`,
`write_user_plus_kernel_pmu`, `dump_totals`) to emit all 10 `perf_*` fields
under the same JSON path as the existing SLICC counters.

### 6.4 Reference-source switch

`tools/run_native_kernel_fastsim_validation.py`:
- Add authoritative mode `perf-aligned-v1` that reads
  `perf_pmu_by_scope.<scope>.<field>` from
  `native-summary-core*.json` schema v2.
- Existing `native-ruby-slicc` mode remains available as
  `--reference-source slicc-diagnostic` for legacy comparisons.
- Default reference source switches to `perf-aligned-v1` once the pilot
  passes.

### 6.5 Unit tests

Add cases in `tests/test_main.cpp`:
- Retire-time hook counts a committed load once and a speculatively flushed
  load zero times.
- FB_HIT path (load retires while same-line MSHR outstanding) increments
  only `perf_l1d_load_fb_hits`, not `perf_l1d_load_misses`.
- Store retire increments only `perf_retired_stores`, no L1D perf counter.

## 7. Pilot validation

### 7.1 Workload selection

Four cases from the existing 38-case matrix:
- `uarch-baseline-c04-782.lbm_r` (buffered-store dense)
- `uarch-baseline-c04-710.omnetpp_r` (branch dense, exposes retire-time gap)
- `uarch-baseline-c04-854.graph500_s` (indirect load dense)
- `uarch-baseline-c04-706.stockfish_r` (control-flow heavy)

### 7.2 Truth collection on the collection host

For each pilot case, run the same user-space workload under `perf stat`
with raw encoding (per section 2 above) to obtain real-hardware counters
for each of the 10 fields. Store under
`tmp/perf-pmu-alignment-pilot-20260928/perf-truth/<case>/perf-stat.txt`.

### 7.3 Alignment check

For each field and each pilot case, emit:
- `perf_truth`: the raw perf counter value
- `gem5_overlay`: the value in `native-summary-core*.json` v2
- `fastsim`: the value in FastSim's JSON reporter
- Two deltas: `overlay − perf`, `fastsim − perf`, as absolute and MAPE

Pass gate for the pilot: every field on every case shows `|delta| ≤ 5 %`
against perf truth. Fields that exceed the gate must be traceable to one of
the structural gaps listed in section 4; unexplained excess fails the pilot.

### 7.4 Reporting

Produce `tmp/perf-pmu-alignment-pilot-20260928/report.md` with a per-field
per-case table and a summary MAPE table across the four cases.

## 8. Backward compatibility and diagnostic layer

- SLICC-transition fields
  (`l1d_read_tag_misses`, `l1d_write_demand_misses`, `l1d_sp_ie_im_misses`,
  `l1d_tag_misses`, `l2_*`, `llc_*` SLICC counters) remain in
  `CoreTotals` and JSON reporters.
- Contract label changes from authoritative to `slicc-diagnostic`. Downstream
  power modelling and weekly / release reporting must consume the `perf_*`
  layer only.
- One release cycle after `perf-aligned-v1` is default, SLICC-transition
  fields may be removed pending explicit user approval.

## 9. Out of scope for this document

- Uncore CHA counters (`UNC_CHA_TOR_INSERTS.*`).
- Uncore IMC counters (`UNC_M_CAS_COUNT.*`).
- L1I load counters (`FRONTEND_RETIRED.*` family).
- Store-side detailed miss accounting via `OFFCORE_RESPONSE.*`.
- HW prefetch modelling (this doc pins pilot to a no-prefetch config).
- PEBS sample-level (per-IP) attribution; only `perf stat` counts are
  targeted.

## 10. Cross references

- Round-1 SLICC caliber fix:
  [`docs/l1d-sp-ie-im-caliber-20260928.md`](l1d-sp-ie-im-caliber-20260928.md)
- Accuracy reporting contract:
  [`docs/accuracy-reporting-contract.md`](accuracy-reporting-contract.md)
- Native-Ruby PMU source repair:
  [`docs/private-l2-pmu-source-repair-20260916.md`](private-l2-pmu-source-repair-20260916.md)
- Native-summary schema v1 audit:
  [`docs/p2-native-ruby-pmu-contract-2026-08-19.md`](p2-native-ruby-pmu-contract-2026-08-19.md)
