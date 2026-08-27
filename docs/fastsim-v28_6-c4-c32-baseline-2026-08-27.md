# FastSim v28.6 C4--C32 baseline

Date: 2026-08-27

Status: maintained exact baseline for native full-system traces. The source
revision is the `FastSim` commit containing this document.

## Baseline selection

The maintained entry point is `configs/gem5-fs-native-kernel.cfg`, which
selects `gem5-v28_6-fs-materialized-kernel.cfg`. The current Release binary
used for the final audit has SHA-256
`217ae2e91c1b6201e33e65fc72b0dfe36124e6557977c3b82c632a9b4d22f56f`.

This baseline keeps the exact response model:

- `core.response_materialized_uop_fast_kernel = true` specializes host
  control flow while preserving all target-visible transitions;
- `core.response_event_only_approximation = false` remains the global default;
- `gem5-v28_7-fs-preview-bypass.cfg` and
  `gem5-v28_8-fs-event-feedback-p0.cfg` are experimental overlays and are not
  selected by the maintained entry point.

The v28.8 event-only experiment is explicitly rejected as a production
baseline. On its C16/C32 20-case gate it improved equal-case throughput by
3.412%, but CPI error against gem5 reached 93.444% at P99 and 105.369% at the
maximum. Its branch/cache event population remained close to exact, so the
failure is a target-timing/CPI failure rather than a PMU population loss.

## Accuracy contract and result

The formal set contains ten workloads at each of C4, C8, C16, and C32: 40
equal-weight cases in total. Measurement scope is user plus native kernel.
Each case first computes absolute percentage error against its matching gem5
oracle; MAPE is the arithmetic mean and percentiles use Type-7 linear
interpolation.

The current work tree was replayed in parallel for accuracy only. After
excluding configuration-selection fields, host timers, and zero-valued
event-only audit counters, all 40 target-state documents matched the frozen
exact baseline recursively. CPI and all four scored PMU counts matched without
tolerance in 40/40 cases, and every output reported event-only approximation
disabled.

| cores | cases | CPI MAPE | CPI P50 | CPI P90 | CPI P99 | CPI max |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 10 | 5.970% | 6.229% | 10.281% | 10.302% | 10.305% |
| 8 | 10 | 6.896% | 7.590% | 12.270% | 12.275% | 12.276% |
| 16 | 10 | 7.156% | 7.189% | 12.909% | 13.378% | 13.430% |
| 32 | 10 | 7.407% | 6.634% | 12.355% | 12.390% | 12.394% |
| all | 40 | **6.857%** | **6.564%** | **12.283%** | **13.204%** | **13.430%** |

The earlier 13.32% CPI P99 refers to the C16/C32 20-case subset. The canonical
C4--C32 40-case baseline is 13.204%.

| PMU field | mapping | MAPE | P50 | P90 | P99 | WAPE | max |
|---|---|---:|---:|---:|---:|---:|---:|
| branch misses | proxy | 2.630% | 1.692% | 8.148% | 11.609% | 1.643% | 12.421% |
| L1D tag misses | proxy | 2.369% | 1.878% | 4.807% | 8.402% | 2.021% | 9.105% |
| private-L2 tag misses | proxy | 3.579% | 2.061% | 9.353% | 18.707% | 2.275% | 22.303% |
| LLC tag misses | diagnostic | 5.553% | 3.891% | 12.148% | 21.532% | 2.141% | 24.292% |

These miss fields compare FastSim with gem5 TaoTrace/Ruby semantic oracles.
They are useful simulator diagnostics, but must not be relabeled as a formal
claim about a particular hardware PMU event.

## Throughput contract and result

Accuracy jobs may run concurrently because their target result is
deterministic. Throughput jobs must run one FastSim process at a time with
fixed CPU and NUMA affinity. The current representative sample used one round
of Stockfish (branch/frontend), LBM (memory bandwidth), and Graph500
(irregular memory) at every core count, with
`numactl --physcpubind=0-47 --membind=0`.

The unit is measurement user-UOP/s. Native-kernel UOPs are simulated and their
cost is included in measurement wall time, but they are not included in the
throughput numerator.

| cores | Stockfish | LBM | Graph500 | equal-case geometric mean |
|---:|---:|---:|---:|---:|
| 4 | 7.668M | 5.125M | 5.082M | **5.845M** |
| 8 | 13.005M | 5.888M | 10.664M | **9.347M** |
| 16 | 13.669M | 5.776M | 8.539M | **8.769M** |
| 32 | 12.089M | 5.817M | 9.325M | **8.688M** |

Across the 12 representative cases, the equal-case geometric mean is
**8.032M user-UOP/s** and the work-weighted aggregate is **8.105M
user-UOP/s**. This is a one-round absolute-rate snapshot on a shared host, not
a confidence interval for small performance changes.

The stricter v28.5/v28.6 C32 gate used ten workloads, three adjacent
counterbalanced A/B pairs per workload, fixed affinity, and one process at a
time. All 30 pairs were target-state exact. v28.6 improved the equal-workload
geometric mean by **1.697%** (workload-bootstrap 95% interval
**1.304%--2.096%**), improved 10/10 workload medians, and changed the
work-weighted rate from 9.071M to 9.229M user-UOP/s (+1.747%). Use paired,
multi-round measurements of this form for future throughput acceptance.

## Regression policy

Future exact host optimizations must satisfy all of the following:

1. build and `fastsim_tests` pass;
2. all target-visible state, CPI, and PMU fields match this exact baseline
   recursively, not merely within an error tolerance;
3. accuracy collection may be parallel, but no parallel-run wall time is used
   as throughput evidence;
4. throughput claims use fixed-affinity, serial, adjacent and counterbalanced
   A/B pairs over representative slow, fast, and irregular workloads;
5. an approximate mode is never promoted through the exact alias without a
   separate accuracy gate and an explicit product decision.

Local machine-readable evidence at baseline creation time:

- `tmp/current-optimal-v28_6-c4-c32-20260827/accuracy.json`;
- `tmp/current-optimal-v28_6-c4-c32-20260827/throughput-serial-representative/summary.json`;
- `tmp/materialized-fast-kernel-c32-paired-20260826/summary.json`.

The `tmp/` evidence is intentionally not committed. This document records the
curated metrics and contracts; raw traces and bulk experiment results remain
external artifacts.
