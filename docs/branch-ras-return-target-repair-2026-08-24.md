# Branch RAS return-target repair (2026-08-24)

> **Oracle correction:** the absolute branch-error values below used the old
> retirement-time `DynInst::mispredicted()` label, which loses misses after a
> Decode target repair. The RAS implementation and same-oracle A/B delta remain
> useful, but the reported branch MAPE/P99 values are superseded by
> `branch-miss-oracle-repair-2026-08-24.md`.

## Problem

FastSim previously pushed a call's return target only after observing a later
dynamic return.  The first execution of a call site therefore entered the RAS
with an unknown target.  This is not the hardware contract: an x86 call pushes
the architectural fallthrough PC (`call_pc + instruction_length`) when the call
is decoded.  The causal-learning approximation created false cold and nested
return misses, especially in LBM, Tealeaf, SPH, and NAMD.

## Repair

- A call uses an exact `StaticInstructionInfo` row to push its architectural
  fallthrough PC immediately.
- Static metadata is optional.  Missing or partial maps fall back to the old
  causal learner, now keyed by address-space ID and call PC so identical virtual
  PCs in different processes cannot alias.
- The implementation consumes the existing `TraceSource` static-instruction
  interface.  It does not depend on gem5 as a producer and does not require an
  `.imap` to run.  Any trace frontend can provide equivalent exact instruction
  metadata; without it, results remain explicitly labeled as learned/unknown.
- The output reports RAS pushes, pops, predictions, hits, and mutually exclusive
  static/learned/unknown source buckets with a conservation check.
- `branch.ras_static_return_target=false` provides a same-binary causal
  ablation.

The PMU validator fingerprint was also repaired to hash the full transitive
`config.include` chain.  Changing an included base configuration can no longer
reuse a stale validation report.

## Historical 40-case causal result

Both variants replayed and scored all 40 C4/C8/C16/C32 native-kernel cases.
The only changed branch setting was the static return-target switch.

| metric | causal fallback | static fallthrough | delta |
|---|---:|---:|---:|
| branch-miss MAPE | 17.4671% | 13.6518% | -3.8153 pp |
| branch-miss P99 APE | 61.8425% | 48.8563% | -12.9862 pp |
| branch-miss WAPE | 7.8424% | 7.0202% | -0.8223 pp |
| predicted branch misses | 12,543,083 | 12,447,446 | -95,637 |
| CPI MAPE | 7.3620% | 7.4569% | +0.0950 pp |
| CPI P99 APE | 13.1929% | 13.5242% | +0.3313 pp |

The CPI non-improvement is intentional evidence against compensating-error
calibration: removing false branch penalties slightly lowers predicted CPI in
some already-underpredicted cases.  Cache-miss WAPE is effectively unchanged
(L1D 2.0218% to 2.0195%, private L2 2.7337% to 2.7356%, LLC 2.2106% to
2.2163%).

Across all cases, 13,928,992 of 14,661,065 call pushes (95.0%) used exact static
fallthroughs.  In the active-kernel subset, 2,797,310 of 3,107,318 pushes
(90.0%) did so.  All source and prediction ledgers conserved.

## Remaining boundary

This repair removes one proven source of false branch misses. The apparent
48.86% P99 and LBM C4 52.24% tail, however, are not valid current accuracy
numbers because their reference omitted decode-corrected BPred misses. CPI P99
remains a separate timing-model problem and must not be reduced by reintroducing
false PMU events.

The subsequent checkpoint/squash experiment is documented in
`branch-speculative-history-checkpoint-2026-08-24.md`.  It implemented Fetch-time
speculative histories and retire-time table training, but slightly worsened the
legacy 40-case branch gate and remains disabled. The later source audit, not
that attribution, determines the next priority.

The full candidate and ablation reports are in:

- `tmp/ras-static-return-target-full40/summary.md`
- `tmp/ras-static-return-target-fallback-full40/summary.md`
