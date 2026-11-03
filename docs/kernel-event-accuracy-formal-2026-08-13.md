# Kernel-event CPI/PMU accuracy report (2026-08-13)

The formal reports were regenerated on 2026-08-14 with the scope-locked v2
pipeline. FastSim now requires one explicit mode per run:

- `measurement_scope=user` disables active syscall service/cost/events,
  page-fault events, and IRQ events, and exposes only user CPI/PMU/throughput
  through `scope_metrics`;
- `measurement_scope=user-plus-kernel` requires an enabled kernel service
  model and exposes only the combined CPI/PMU/throughput through the same
  canonical object.

The accuracy comparator rejects missing, legacy, or cross-wired scopes. The
regeneration found all 36 paired reports valid. User cycles, combined cycles,
user PMU counters, and combined PMU counters were bit-exact against the prior
formal run in 36/36 cases; only the schema, filenames, canonical statistics
path, and newly measured host throughput changed.

## Data gate and split

This is the formal FS/Ruby result for deployment-available input only:
user-mode functional records, memory metadata, and syscall numbers. Kernel
instructions, measured kernel durations, oracle PMU fields, syscall arguments,
page residency, and IRQ vectors are not FastSim inputs.

The dataset root is
`logs/gem5-fs-roi/sample/mesi-three-level-3GiB`. The formal matrix contains
36 configurations and 472 per-core FST files:

| Split | Configurations | Per-core FSTs | Role |
|---|---:|---:|---|
| 4 cores | 10 | 40 | calibration |
| 8 cores | 10 | 80 | held-out |
| 16 cores | 10 | 160 | held-out |
| 32 cores | 6 | 192 | held-out |
| Total | 36 | 472 | — |

The integrity audit recomputed every FST header, size, and SHA-256 and found
zero errors. It also checked phase conservation and required every oracle
`n_user` to equal the matching measurement record count. Totals are:

| Quantity | Count |
|---|---:|
| Functional warmup records | 2,175,572,564 |
| Functional warmup instructions | 1,294,374,182 |
| Measurement records | 4,720,002,798 |
| Measurement instructions | 2,836,673,302 |
| Rejected formal cases | 0 |

The 4-core cases alone calibrate one deployable kernel-event configuration.
The 26 cases at 8/16/32 cores are held out and evaluated with that frozen
configuration. A post-cleanup filesystem audit finds 63 complete result
directories: 36 formal replacements and 27 complete history, pilot, or valid
smoke results.

## Functional warmup and boundary repair

Every formal manifest uses a record-bounded
`fastsim-binary-warmup-slice`. FastSim replays the prefix before a common
barrier, resets measurement counters and time, then retains cache/coherence,
directory, branch-predictor, DTLB, DRAM/controller, dependency, and response
scoreboard state for the measurement interval. A core may have a zero-length
prefix when it was not scheduled in user mode before the global serial marker;
the configuration-wide prefix must remain nonzero.

The initial implementation sliced only by macro-instruction count. The serial
marker is asynchronous and, on Stockfish core 3, arrived after two UOPs of a
macro instruction had committed. Consumer-side macro counting therefore put
those two warmup UOPs into measurement. The repaired manifest carries both
producer instruction counts and exact producer record counts: record counts
select the phase boundary and instruction counts independently check
conservation. Stockfish now consumes exactly 40,000,003 measurement records,
matching the oracle, instead of 40,000,005.

The FS producer also collapses gem5/x86's syscall transition macro into one
serial marker. The privilege-scoped PMU sees 25 user-decoded UOPs and one
branch, so FastSim restores the fixed additional 24 UOPs and one branch only
in PMU output. The functional replay count and CPI denominator remain one
marker. This identity held for all 36 cases; after repair, user `retired_uops`
and `branches` match exactly in every calibration and held-out case.

## Frozen kernel model

The principal frozen parameters are:

| Parameter | Value |
|---|---:|
| Allocation first-touch window | 16,777,216 records |
| Allocation first-touch probability | 643,036 ppm |
| Background first-read probability | 3,804 ppm |
| Background first-write probability | 4,906 ppm |
| IRQ foreground period | 1,448,909 cycles |
| Explicit syscall profiles | 15 syscall numbers |

## CPI accuracy

Both scopes use the same `N_user` denominator. User+kernel adds active syscall,
page-fault, IRQ, and scheduler cycles; idle and blocked wall time remain
outside the CPI numerator. APE percentiles use R/NumPy Type 7.

| Split | Scope | Mean APE (MAPE) | P50 | P90 | P99 | WAPE | Bias | Max |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 4-core calibration | User | 16.15% | 11.61% | 39.50% | 41.49% | 14.86% | -7.08% | 41.71% |
| 4-core calibration | User+kernel | 15.80% | 12.61% | 29.37% | 39.28% | 12.66% | -5.48% | 40.38% |
| 8/16/32-core held-out | User | 14.90% | 13.77% | 27.92% | 49.08% | 10.86% | -5.80% | 50.02% |
| 8/16/32-core held-out | User+kernel | 14.55% | 13.67% | 24.84% | 47.84% | 10.39% | -4.62% | 48.75% |

The held-out user+kernel MAPE is slightly lower than user MAPE, but this does
not establish accurate kernel-event decomposition: page-fault and IRQ errors
remain large and can cancel the compute-model bias. The largest errors are
Neutron at 8/16 cores; Stockfish and SPH also remain material. Further CPI
work should focus on the user timing/cache model before adding more aggregate
kernel-cycle fitting.

## Held-out PMU accuracy

The oracle is `taotrace-path-class-v2`, not host `perf`. All rows have finite
APE in 26/26 held-out cases. Sparse cache counters require both the
configuration-equal MAPE/percentiles and WAPE; very large relative errors from
small denominators are retained.

### User PMU

| Counter | Mean APE | P50 | P90 | P99 | WAPE |
|---|---:|---:|---:|---:|---:|
| Branch misses | 5.09% | 1.26% | 14.46% | 21.12% | 4.42% |
| Branches | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| DTLB accesses | 4.60% | 2.19% | 14.24% | 16.49% | 5.95% |
| DTLB hits | 6.99% | 3.29% | 16.42% | 30.38% | 7.53% |
| DTLB misses | 65.93% | 14.58% | 61.32% | 663.73% | 38.67% |
| L1D accesses | 4.77% | 2.19% | 14.24% | 16.49% | 6.20% |
| L1D hits | 3.92% | 1.95% | 11.18% | 14.13% | 4.84% |
| L1D misses | 84.31% | 15.62% | 328.90% | 559.84% | 33.33% |
| L2 accesses | 84.31% | 15.62% | 328.90% | 559.84% | 33.33% |
| L2 hits | 26,362.47% | 12.05% | 71,985.39% | 285,523.85% | 39.86% |
| L2 misses | 129.86% | 26.77% | 303.18% | 798.95% | 38.69% |
| LLC accesses | 129.86% | 26.77% | 303.18% | 798.95% | 38.69% |
| LLC hits | 5,363.42% | 113.14% | 20,288.23% | 29,736.02% | 51.21% |
| LLC misses | 145.01% | 74.62% | 302.83% | 867.00% | 108.05% |
| Retired instructions | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| Retired UOPs | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |

### User+kernel PMU

| Counter | Mean APE | P50 | P90 | P99 | WAPE |
|---|---:|---:|---:|---:|---:|
| Branch misses | 31.51% | 7.91% | 79.86% | 237.64% | 7.38% |
| Branches | 7.65% | 1.21% | 24.61% | 34.62% | 3.77% |
| DTLB accesses | 5.92% | 2.15% | 15.09% | 17.81% | 6.85% |
| DTLB hits | 8.14% | 3.12% | 17.78% | 29.45% | 8.44% |
| DTLB misses | 67.42% | 13.44% | 60.98% | 662.11% | 38.42% |
| L1D accesses | 5.82% | 2.15% | 15.09% | 17.81% | 6.67% |
| L1D hits | 5.65% | 3.46% | 13.19% | 13.96% | 6.49% |
| L1D misses | 80.90% | 19.09% | 306.34% | 540.16% | 32.28% |
| L2 accesses | 80.90% | 19.09% | 306.34% | 540.16% | 32.28% |
| L2 hits | 3,914.21% | 13.26% | 5,844.97% | 51,315.70% | 37.64% |
| L2 misses | 119.83% | 29.91% | 293.61% | 835.80% | 39.30% |
| LLC accesses | 119.83% | 29.91% | 293.61% | 835.80% | 39.30% |
| LLC hits | 1,012.42% | 74.47% | 3,560.74% | 8,571.66% | 51.13% |
| LLC misses | 243.08% | 70.84% | 450.23% | 2,174.22% | 104.43% |
| Retired instructions | 1.69% | 0.50% | 5.23% | 7.28% | 1.75% |
| Retired UOPs | 2.37% | 0.68% | 6.91% | 9.39% | 2.51% |

Trace-explicit branches, retired counts, and first-level access/hit counters
are the strongest PMU results. DTLB misses and cache miss routing are not
accurate; in particular LLC-miss WAPE exceeds 100% in both scopes.

## Kernel-event diagnostics

| Quantity | Mean APE | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|
| Syscall events | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| Syscall cycles | 89.17% | 16.89% | 326.61% | 458.58% | 36.46% | +28.22% |
| Page-fault events | 261.39% | 28.46% | 1,227.18% | 1,291.97% | 58.19% | +10.40% |
| Page-fault cycles | 192.36% | 27.42% | 787.62% | 949.22% | 50.80% | +17.41% |
| IRQ events | 44.03% | 45.45% | 81.06% | 104.53% | 65.84% | +29.95% |
| IRQ cycles | 78.28% | 68.07% | 151.42% | 200.48% | 81.90% | +49.63% |
| Idle cycles | 76.92% | 100.00% | 100.00% | 100.00% | 100.00% | -100.00% |

Syscall event count is exact because every syscall number is trace-visible.
Page faults remain statistical because the trace lacks page residency,
mapping type, arguments, return values, and major/minor identity. IRQs remain
statistical because it exposes neither vector nor arrival boundary. Idle stays
zero intentionally because it is off-CPU/wall-time coverage rather than active
application CPI.

## Throughput

Runs were sequential; throughput is host trace-processing rate in million
user UOP/s, not target IPC.

| Split | Scope | Mean | P50 | P90 | P99 | Minimum |
|---|---|---:|---:|---:|---:|---:|
| 4-core calibration | User | 8.81 | 9.07 | 10.14 | 10.54 | 5.20 |
| 4-core calibration | User+kernel | 8.86 | 8.87 | 10.86 | 11.11 | 5.48 |
| 8/16/32-core held-out | User | 10.33 | 11.33 | 12.54 | 12.82 | 5.32 |
| 8/16/32-core held-out | User+kernel | 9.99 | 10.14 | 12.10 | 13.81 | 5.29 |

## Invalid-data removal

Deletion was performed only after the 36 replacements passed the full
472-FST integrity gate and the invalid/replacement directory sets had zero
overlap. Removed data:

| Kind | Removed |
|---|---:|
| Old cold formal results | 36 directories |
| First incomplete warmup-fix results | 36 directories |
| Invalid smoke result | 1 directory |
| Scratch directories | 37 |
| Obsolete matrix directories | 17 |
| Obsolete report directories | 8 |
| Total bytes | 427,818,869,746 (398.4 GiB) |

Checkpoint inputs were retained. The exact deletion list and replacement gate
are recorded in `tmp/kernel-events-v2/formal-warmup-v2/deletion-report.json`.

## Reproducibility artifacts

- Integrity audit:
  `tmp/kernel-events-v2/formal-warmup-v2/integrity-audit.json`
- Calibration report and frozen model:
  `tmp/kernel-events-v2/formal-scope-v2/calibration/summary.md` and
  `kernel-events.cfg`
- Held-out Markdown/JSON/CSV:
  `tmp/kernel-events-v2/formal-scope-v2/held-out/summary.{md,json,csv}`
- Per-case oracle validation, `user.json`, `user-plus-kernel.json`, and
  scope-checked `accuracy.json` files:
  `tmp/kernel-events-v2/formal-scope-v2/{calibration,held-out}/cases/`
- Scope-locked pipeline provenance:
  `tmp/kernel-events-v2/formal-scope-v2/{calibration,held-out}/pipeline.json`
- Formal matrices:
  `logs/gem5-fs-roi/matrix/kernel-v4-functional-warmup-10m-v2b/`

All definitions and future-report requirements follow the
[CPI, PMU, and throughput reporting contract](accuracy-reporting-contract.md).
