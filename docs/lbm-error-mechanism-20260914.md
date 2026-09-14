# LBM Error Mechanism Analysis and Reduction Boundary

Date: 2026-09-14. Status: measurement + source analysis on the frozen
default-off baseline. No simulator behavior changed. This report explains *why*
the two-stage inference structurally overestimates LBM's memory-bound cycles,
bounds how much of that is legitimately recoverable without abandoning the
throughput-first design, and records the bidirectional regression guardrail that
now protects the underestimating tealeaf/namd cases.

## Methodology discipline

Two-stage inference is a throughput-first approximation. The goal here is *not*
to drive LBM error to zero; it is to decide whether a mechanism-level change can
reduce the LBM overestimate without (a) degrading throughput or (b) worsening
the many cases that already underestimate. Every claim below is grounded in the
committed source path and in per-core measurement against gem5, not in fitted
constants. Acceptance is end-to-end CPI + throughput, enforced by
`tools/run_cpi_guardrail.py`; local service MAE is explicitly not an acceptance
metric.

## The exposure mechanism (source-grounded)

The memory penalty is computed in `src/simulator.cpp`:

- `ChunkMemoryEvent.lower_bound_latency` (`simulator.cpp:2891`) defaults to 0 and
  is only set non-zero for DTLB hierarchy walks (`simulator.cpp:8147`). Ordinary
  LBM data misses therefore carry `lower_bound_latency = 0`.
- Exposed cycles = `ceil((latency - lower_bound_latency) * memory_exposure)`
  (`simulator.cpp:20855`, `:15872`, `:17623`, and peers).
- `core.memory_exposure` defaults to 1.0 (`include/fastsim/config.hpp:852`) and
  is *forced* to 1.0 whenever `needs_tso` is set (`config.cpp:597`), which is the
  case for these x86 runs. So LBM puts the entire post-lower-bound latency on the
  critical path.
- The interval core issues a load at only its lower-bound latency
  (`interval_core.cpp:112`, `ordinary_load_latency`); the miss excess is added
  afterward as exposed penalty.
- In the interval-bound path that excess accumulates into the per-core interval
  gap: `interval_gap_q16_[core] += cycles_to_fixed(exposed)`
  (`simulator.cpp:20925`). In the non-interval path it serializes even harder:
  `ready_q16_[core] = issue_q16 + cycles_to_fixed(exposed)`
  (`simulator.cpp:20928`).

The structural consequence: overlap between two independent outstanding misses is
credited only to the extent the interval scheduler's dependency/FU model already
placed their issue edges close together. Any residual exposed latency is summed,
not max-ed. The explicit comment at `simulator.cpp:6597` confirms the design
intent — "capacity resources release at the full response latency even when
memory_exposure puts only part of it on the critical path" — i.e. the model
knowingly trades exact MLP for throughput.

## Where LBM's error actually sits

Frozen LBM C32 default vs gem5 (all 32 O3 cores cut at 29,313,840 cycles):

- Miss *counts* already match gem5: L1D 6,620,148 vs 6,710,564 (-1.3%); L2
  4,039,756 vs 4,140,711 (-2.4%). The error is not a miss-rate error.
- The exposed-memory penalty is 941,155,750 cycles = **93.3% of all simulated
  cycles**. The whole-run overestimate is 70,566,670 cycles = **only 7.5% of
  that bucket**. The two-stage model already overlaps ~92.5% of memory latency
  correctly; the error is the residual un-overlapped tail.
- Normalized: **+17.47 extra exposed cycles per L2 miss** (+293.88 cycles per
  1,000 committed instructions). Per-core cycle inflation correlates with
  `exposed_memory_penalty/inst` at r = +0.856 (core0 excluded) and is
  uncorrelated with miss-rate signals (all r < 0.16).

Interpretation: the two-stage inference under-credits memory-level parallelism
by ~17 cycles per L2 miss on LBM — it treats slightly too much of each miss's
latency as serialized rather than overlapped with an independent in-flight miss.

## Recoverable ceiling and the reverse risk

The *arithmetic* ceiling is the full 70.5M cycles (7.52%), reachable only if
every currently-serialized residual were perfectly overlappable — which it is
not, because some of that residual is genuine serialization gem5 also pays. A
credible mechanism target is the portion of the 17.47 cyc/L2-miss residual that
has a concurrently outstanding independent miss.

The reverse risk is measured, not assumed. Same exposure decomposition on the
underestimating cases:

| case | signed err | exposed penalty (% of cyc) | L2 miss/1k inst | exposed/L2 miss |
|---|---|---|---|---|
| LBM C32 | +7.54% (over) | 93.3% | 16.82 | 233.0 |
| TeaLeaf C16 | -13.31% (under) | 38.0% | 2.72 | 78.3 |
| namd C16 | -10.57% (under) | 21.3% | 0.83 | 123.5 |

The underestimating cases live in a *different bucket*: their exposed-memory
penalty is a small fraction of cycles and they already fall below gem5. A change
that globally increases miss overlap (reduces exposed latency) would push
tealeaf/namd further down — the same class of failure as the rejected projected
RD/WB candidate. Therefore any exposure-reducing mechanism must be gated on a
locally observable high-MLP condition (multiple independent outstanding misses),
not applied globally, and must be validated against the guardrail before
promotion.

## Bidirectional regression guardrail (committed)

`configs/cpi-guardrail-baseline-v1.json` freezes signed relative CPI error for
12 cases — 4 LBM (the overestimate tail) plus 4 TeaLeaf and 4 namd (the
underestimate tail). `tools/run_cpi_guardrail.py` re-simulates them against the
maintained `configs/gem5-fs-native-kernel.cfg` and fails if any case's signed
error leaves `[frozen - 1.0pp, frozen + 1.0pp]`. For the underestimating cases
the binding edge is the lower bound (must not drop further); for LBM it is the
upper bound. Verified today: all 12 reproduce the frozen error exactly, 0
violations.

```sh
tools/run_cpi_guardrail.py --jobs 6 --output OUT.json
```

## Recommendation

Do not pursue a global exposure change. If a next experiment is run, it should
be a bounded, locally-gated MLP-credit ablation on the fixed request stream that
only reduces exposed latency where an independent outstanding miss is present,
with acceptance = LBM C32 abs CPI error and 32-core MAE fall while the
guardrail's tealeaf/namd lower bounds and throughput hold. Absent that
condition, +7.54% on LBM C32 is an accepted cost of the throughput-first design,
and the guardrail prevents future changes from trading it against the
underestimating tail.
