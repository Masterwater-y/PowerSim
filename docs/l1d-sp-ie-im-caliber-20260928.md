# L1D SP_IE→IM slow-path miss caliber alignment (20260928)

## Root cause

buffer-114 full-matrix reporting shows `l1d_tag_misses` MAPE 57.3%, WAPE 43.2%,
signed bias −43.1% under the strict two-bucket definition
`l1d_read_tag_misses + l1d_write_demand_misses`. The two-bucket sum is now
exact against SLICC I→IS Load + I→IM Store `uu_profileDataMiss`. The remaining
−43% bias is entirely attributable to a third SLICC miss path that FastSim has
never modeled:

`gem5-collection/patches/0001-fastsim-collection-upstream-hooks.patch` line
1319 introduces the transition

```
transition(SP_IE, Store, IM) {
    sp_completeStorePrepare;
    uu_profileDataMiss;      // -> hierarchy.l1d.tag_misses
    k_popMandatoryQueue;
}
```

Semantics: a buffered store's `Store_Prepare` has already moved the L0Cache
line into `SP_IE` (permission-only in flight, TBE allocated, no data yet).
When the actual mandatory `Store` arrives at the head of SQ before
`Data_Exclusive` returns, the store takes over the in-flight prepare and is
counted as a second `uu_profileDataMiss`. This raises `l1d.tag_misses` by one
per late demand.

The corresponding path in FastSim is `replay_buffered_tso_head_demand`
(`src/simulator.cpp:22459`). Two facts explain why the bucket is currently
empty:

1. Prepare installs L1D immediately. `buffered_tso_permission_prepare(event)`
   sets `permission_only=true` but the private access at
   `src/simulator.cpp:22533` still runs `private_caches_[core]->access(line,
   write=false, ...)`. That fill promotes the line into L1D at prepare time.
2. As a result the demand's `private_result.level == HitLevel::kL1`. Because
   the L1D bucket classification at `src/simulator.cpp:22711` gates on
   `preview.private_level != HitLevel::kL1`, the demand never enters any of
   the five L1D buckets.

Native gem5 counts the demand as a miss even when data has not returned yet
because SLICC accounts by state (SP_IE→IM) rather than by cache residency. In
FastSim the residency test hits and the caliber bucket is skipped. The
timing path for the demand is already correct (`role=2/3` in
`docs/buffer-tso-store-timing-20260923.md` §5), so CPI MAE 0.0599 cyc/macro is
independent of this bucket.

## Per-case shape (before this fix)

Data from the current full-matrix run:

- buffered-TSO sparse workloads (graph500, neutron): APE ≤ 2%.
- buffered-TSO dense workloads (LBM, tealeaf, omnetpp, stockfish): APE −40%
  to −96%.

The dense workloads have long queues of buffered stores whose demand arrival
tends to overlap the prepare's outstanding fill, matching the SP_IE→IM
transition in gem5.

## Scope of this change

**In scope (caliber only, Round-1):**

- Add a sixth `CoreCounters` bucket `l1d_sp_ie_im_misses`.
- Detect the SP_IE→IM condition on the demand side by comparing the demand's
  issue cycle to the prepare's stored shared-response completion.
- Include the new bucket in the exported `l1d_tag_misses` value:
  `l1d_tag_misses = l1d_read_tag_misses + l1d_write_demand_misses +
  l1d_sp_ie_im_misses`.
- No change to `SetAssociativeCache` timing, no change to `l1d.misses` total,
  no change to `replay_memory_event` L1D install semantics, no change to CPI.

**Out of scope (functional model change, Round-2, tracked separately):**

- Delaying the L1D fill on `Store_Prepare` until `Data_Exclusive` returns.
- Rewriting `buffered_tso_permissions_intact` / `replay_buffered_tso_head_demand`
  residence certificates to permit an in-flight prepare with no L1D presence.
- Interaction with same-line loads, SQ forwarding, and rollback certificates.

If Round-2 is required later, it lands under a separate design in
`docs/l1d-prepare-install-alignment-<date>.md` with independent evidence.

## Detection algorithm

The role-2 head-demand case already exists in the timing model:
`src/simulator.cpp:15182-15189` sets
`timing.buffered_store_original_demands[core][position] = 2` when
`store_send < buffered_permission_ready`, i.e. the SQ head demand for a
buffered store issues before the prepare's shared response is available. That
condition is exactly SLICC `SP_IE + Store → IM`; the code comment already
cites `MESI_Three_Level L0 SP_IE + Store → IM`. The mandatory request takes
over the in-flight prepare and completes at its response.

At publish time (`src/simulator.cpp:7217-7223`) the two role-1 / role-2 cases
are already dispatched: role 1 decrements `buffer_tso_prepares`, role 2
increments `buffer_tso_merged_demands`. Adding one `l1d_sp_ie_im_misses`
increment in the role-2 branch is sufficient and correct:

- Role 2 count is finalized at publish, i.e. after timing feedback / reweave
  has committed. No rollback can un-do the increment.
- Role 1 (store already at SQ head at canWB) has no prepare in flight and
  maps to a normal `uu_profileDataMiss` on an I state, which is already
  covered by `l1d_write_demand_misses` when the store first enters the
  simulator.
- Role 3 (`store_send >= buffered_permission_ready`) means gem5 saw
  `Data_Exclusive` before the mandatory `Store`, transitioning `SP_IE → E`;
  the subsequent store on E is an upgrade path (`uu_profileDataUpgrade`), not
  a miss.

No auxiliary map is required. No change to `replay_memory_event`. No change
to `replay_buffered_tso_head_demand`. The classification predicate is already
computed and stored in `timing.buffered_store_original_demands`.

## Conservation and reporting

- `l1d.misses` (from `SetAssociativeCache::probe_indexed`) is unchanged.
- Existing five-bucket L1D breakdown (`l1d_read_tag_misses`,
  `l1d_write_demand_misses`, `l1d_permission_upgrades`, `l1d_follower_misses`,
  `l1d_prepare_misses`) is unchanged. Their sum is still bounded by the raw
  `l1d.misses` population; `l1d_sp_ie_im_misses` is a separate accounting
  slice keyed off the demand path and adds to the exported tag-miss field.
- JSON `l1d_tag_misses` = read + write demand + SP_IE→IM.
- New diagnostic field `l1d_sp_ie_im_misses` exposed alongside the other five
  buckets in `write_user_functional_pmu`, `write_user_plus_kernel_pmu`, and
  `dump_totals`.

The native validator (`tools/run_native_kernel_fastsim_validation.py`,
`PMU_EVENT_NAMES`) already uses the field name `l1d_tag_misses`, so no
validator change is needed.

## Verification targets

- LBM C4 (`uarch-baseline-c04-782.lbm_r`) baseline signed bias ≈ −40%.
- LBM C32, tealeaf, omnetpp, stockfish (buffered-TSO dense) baselines with
  APE ≥ 40%.
- graph500, neutron (buffered-TSO sparse) baselines APE ≤ 2% — must not
  regress.
- CPI MAE full matrix must remain 0.0599 cyc/macro (0.001 cyc/macro
  tolerance).

## Guardrails

- Bit-identical `l1d.misses` and `SetAssociativeCache` counters vs. current
  release build.
- `fastsim_tests` full pass.
- Full 38-case `first-core-target-common-end-v1` native validation with
  `--jobs 16`; the run reuses `validation_fingerprint` so unrelated cases
  short-circuit.

## References

- `docs/buffer-tso-l2-caliber-fix-20260928.md` — L2/LLC five-bucket caliber
  fix (same pattern, one round earlier).
- `docs/buffer-tso-store-timing-20260923.md` §5 — SQ head demand roles
  1/2/3, which drove the current timing correctness of the demand path.
- `gem5-collection/patches/0001-fastsim-collection-upstream-hooks.patch`
  lines 1298–1339 — SLICC transitions defining SP_IE, `Store_Prepare`,
  `SP_IE Store → IM` (`uu_profileDataMiss`), and `SP_IE Data_Exclusive → E`
  (`u_writeDataToCache`).
