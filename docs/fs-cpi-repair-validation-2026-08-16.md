# FS CPI repair validation (2026-08-16)

## 1. Data gate, split, and provenance

This is the formal validation of the destination-class and functional-warmup
boundary repair. The immutable run root is:

```text
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816
```

The set contains the same ten workloads at C4 and C8: Stockfish, omnetpp,
zstd, LBM, SPH, TeaLeaf, NAb, Graph500, NAMD, and Neutron. C4 is the ten-case
calibration split. C8 is a ten-case **core-count-held-out** split; it is not a
workload-held-out claim.

| Gate | Result |
|---|---:|
| Formal configurations | 20 |
| Per-core FST files | 120 |
| Total FST records | 1,663,570,251 |
| FST bytes | 106,468,610,688 |
| Destination-class marked records | 1,663,570,251 |
| Destination-class UOP rows | 1,417,937,936 |
| Syscall rows | 828 |
| Oracle target identity | 20/20 valid |
| Rejected formal cases | 0 |
| Integrity errors | 0 |

The frozen base simulator configuration is
`configs/gem5-v28_1-time-epoch.cfg`. The C4 kernel-event model was calibrated
only from C4 and then frozen for C8. Both splits use `dtlb.miss_model=se_atomic`
and the shared page-fault semantic/cache-state selector. The exact effective
configs are recorded beside each pipeline. Data collection used parallel gem5
workers; FastSim accuracy/throughput replays were run sequentially by the
pipeline. The host was not reserved exclusively, so throughput is a
single-process observed distribution rather than an isolated-host benchmark.

## 2. Functional warmup

Every core uses a record-bounded `fastsim-binary-warmup-slice`. The common
barrier excludes the warmup prefix from CPI, PMU, and throughput denominators.

| Phase | Records | Macro instructions |
|---|---:|---:|
| Functional warmup | 463,570,143 | 259,802,203 |
| Measurement | 1,200,000,108 | 722,016,405 |

At the barrier FastSim resets measured time, PMU, CPI-attribution, and
committed-pipeline audit counters. It retains cache/coherence/directory,
branch-predictor, DTLB, DRAM/controller, dependency, response-scoreboard, and
timing history. The repair additionally drains fully retired warmup
destination tokens before a finite rename free list enters measurement.

## 3. CPI accuracy

APE and Type-7 percentiles follow
[`accuracy-reporting-contract.md`](accuracy-reporting-contract.md). Both
scopes use measured user UOPs as the denominator. Idle and blocked wall time
are excluded from user+kernel CPI.

| Split | Scope | Mean APE | P50 | P90 | P99 | WAPE | Bias | Max |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| C4 calibration | user | 13.54% | 11.34% | 25.19% | 40.45% | 12.87% | -12.78% | 42.14% |
| C4 calibration | user+kernel | 14.06% | 9.81% | 26.92% | 40.46% | 12.50% | -10.29% | 41.97% |
| C8 held-out | user | 14.63% | 12.71% | 22.99% | 44.34% | 13.22% | -13.22% | 46.71% |
| C8 held-out | user+kernel | 15.41% | 12.00% | 24.46% | 44.33% | 12.39% | -12.39% | 46.54% |

The user-CPI P99 gate is not met. Neutron remains the maximum: C4 reference /
prediction is 0.999879 / 0.578486 (42.14% APE), and C8 is 0.952354 / 0.507522
(46.71% APE).

## 4. PMU accuracy

The complete per-counter tables, including finite-APE coverage, WAPE, and
bias, are machine-generated at the artifact paths in section 6. The following
formal tables retain the main architectural and cache/TLB counters; no pooled
C4+C8 statistic is used.

### C4 calibration, user

| Counter | Cases | Mean | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| retired instructions | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| retired UOPs | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| branches | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| branch misses | 10/10 | 6.49% | 0.89% | 16.24% | 28.86% | 2.92% | +2.52% |
| DTLB accesses | 10/10 | 4.51% | 2.16% | 13.05% | 16.84% | 5.42% | +5.42% |
| DTLB misses | 10/10 | 15.96% | 2.61% | 58.99% | 72.90% | 1.75% | +1.10% |
| L1D accesses | 10/10 | 4.65% | 2.16% | 13.05% | 16.84% | 5.61% | +5.61% |
| L1D misses | 10/10 | 70.90% | 8.91% | 132.04% | 514.53% | 37.36% | +32.12% |
| L2 misses | 10/10 | 118.09% | 18.84% | 326.74% | 542.33% | 38.27% | +28.29% |
| LLC misses | 10/10 | 93.73% | 44.82% | 301.41% | 314.07% | 131.81% | +131.81% |

### C4 calibration, user+kernel

| Counter | Cases | Mean | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| retired instructions | 10/10 | 0.40% | 0.25% | 0.90% | 1.17% | 0.43% | -0.04% |
| retired UOPs | 10/10 | 0.59% | 0.30% | 1.21% | 2.03% | 0.58% | -0.05% |
| branches | 10/10 | 3.81% | 0.47% | 5.93% | 27.88% | 0.93% | -0.07% |
| branch misses | 10/10 | 16.94% | 5.27% | 46.60% | 82.24% | 4.84% | +2.05% |
| DTLB accesses | 10/10 | 4.30% | 2.08% | 13.03% | 18.13% | 4.99% | +4.99% |
| DTLB misses | 10/10 | 12.55% | 2.76% | 28.18% | 49.34% | 2.33% | +1.07% |
| L1D accesses | 10/10 | 4.42% | 2.08% | 13.03% | 18.13% | 5.16% | +5.16% |
| L1D misses | 10/10 | 66.73% | 7.87% | 114.08% | 493.35% | 35.36% | +30.33% |
| L2 misses | 10/10 | 125.77% | 16.79% | 347.12% | 815.45% | 35.72% | +25.54% |
| LLC misses | 10/10 | 79.74% | 30.11% | 303.22% | 328.74% | 103.45% | +103.45% |

### C8 held-out, user

| Counter | Cases | Mean | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| retired instructions | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| retired UOPs | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| branches | 10/10 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| branch misses | 10/10 | 5.92% | 1.20% | 17.46% | 22.03% | 3.09% | +2.52% |
| DTLB accesses | 10/10 | 4.38% | 2.19% | 13.27% | 16.26% | 5.26% | +5.26% |
| DTLB misses | 10/10 | 17.26% | 2.62% | 67.73% | 78.06% | 1.63% | +1.16% |
| L1D accesses | 10/10 | 4.53% | 2.19% | 13.28% | 16.26% | 5.45% | +5.45% |
| L1D misses | 10/10 | 72.71% | 12.60% | 132.03% | 515.26% | 29.71% | +24.10% |
| L2 misses | 10/10 | 174.85% | 19.83% | 475.48% | 854.71% | 33.11% | +16.21% |
| LLC misses | 10/10 | 157.29% | 45.32% | 357.95% | 824.84% | 118.21% | +118.21% |

### C8 held-out, user+kernel

| Counter | Cases | Mean | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| retired instructions | 10/10 | 0.47% | 0.21% | 1.28% | 1.86% | 0.51% | -0.08% |
| retired UOPs | 10/10 | 0.73% | 0.34% | 2.18% | 2.99% | 0.73% | -0.14% |
| branches | 10/10 | 4.94% | 0.32% | 14.51% | 31.65% | 1.15% | -0.28% |
| branch misses | 10/10 | 19.72% | 6.21% | 38.84% | 116.53% | 4.86% | +2.31% |
| DTLB accesses | 10/10 | 4.48% | 2.13% | 13.32% | 17.61% | 5.24% | +4.84% |
| DTLB misses | 10/10 | 15.04% | 4.03% | 34.66% | 73.05% | 1.98% | +1.04% |
| L1D accesses | 10/10 | 4.60% | 2.13% | 13.32% | 17.61% | 5.42% | +5.03% |
| L1D misses | 10/10 | 68.18% | 10.20% | 114.58% | 495.49% | 28.18% | +23.03% |
| L2 misses | 10/10 | 139.69% | 11.42% | 362.65% | 947.98% | 31.84% | +15.33% |
| LLC misses | 10/10 | 178.86% | 12.80% | 404.03% | 1238.26% | 97.88% | +97.81% |

Exact high-volume retired/branch fields are healthy. Sparse cache-level APE
is not: small reference denominators and missing kernel/wrong-path cache state
produce very large configuration-equal percentiles. WAPE and signed bias must
therefore accompany MAPE; the claim “PMU <5%” is not valid for this FS set.

## 5. Throughput

| Split | Scope | Mean M UOP/s | P50 | P90 | P99 | Minimum |
|---|---|---:|---:|---:|---:|---:|
| C4 calibration | user | 8.87 | 9.15 | 10.60 | 10.84 | 5.25 |
| C4 calibration | user+kernel | 9.21 | 9.33 | 11.73 | 11.81 | 5.33 |
| C8 held-out | user | 10.97 | 11.63 | 12.84 | 13.17 | 5.79 |
| C8 held-out | user+kernel | 10.82 | 11.88 | 12.61 | 12.69 | 5.85 |

All minimums exceed 5 M user UOP/s.

## 6. Machine-readable and per-case artifacts

```text
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/audit/matrix-integrity.json
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/audit/oracle-identity-formal.json
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7/index.json
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/accuracy/calibration-c4/summary.{json,csv,md}
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/accuracy/held-out-c8/summary.{json,csv,md}
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/accuracy/{calibration-c4,held-out-c8}/cases/*/accuracy.json
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/committed-audit-{c4,c8}/summary.{json,md}
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/rename-free-list-{c4,c8}/summary.{json,md}
```

## 7. Repair decision and remaining limitation

The implemented repair is accepted as input/infrastructure work:

- TaoTrace emits exact Int/Float/Vec/CC destination counts in direct FST v7;
- formal builders and audits fail closed on missing per-record metadata;
- committed audit and finite rename allocations reset correctly at the common
  warmup barrier while warmed target state remains resident;
- build and regression tests pass.

The finite per-class free-list candidate is rejected as a CPI model change.
Every formal core reports zero free-list stall cycles and its CPI distribution
is unchanged. It remains disabled by default.

C8 CPI gap has Spearman correlation 0.699 with gem5 rename IQ-full and 0.612
with ROB-full, while exact committed-path register capacity never stalls.
Neutron has 0.381 squashed instruction/UOP per measured user UOP and 0.234
rename IQ-full events/UOP. These overlapping counters are attribution signals,
not additive cycle corrections.

A committed functional FST does not contain wrong-path instruction identity,
architectural operands, dynamic memory addresses, or issue decisions. The
existing static-path diagnostic has 98.92--100% causal PC-profile coverage but
its ROB-capped UOP estimate ranges from 0.09x to 3.05x of gem5 squashed work
across workloads. Fixed penalty, anonymous branch shadow, and workload
coefficients are therefore not promotable.

The next safe timing candidate requires a producer-neutral static operand and
macro-lowering contract that both a normal drmemtrace decoder and TaoTrace can
emit. It must first run audit-only, demonstrate dependency/IQ/LSQ conservation
against workload-held-out cases, and only then be allowed to affect CPI. Until
that input exists, a user-CPI P99 below 12% cannot be claimed on this FS set.

The first input stage is now implemented and validated by the C4 Neutron
`.fst.imap` v2 pilot in
[`fst-imap-v2-operand-pilot-2026-08-17.md`](fst-imap-v2-operand-pilot-2026-08-17.md).
Its operand fields remain audit-only, so they do not revise any CPI or PMU
number in this report. The subsequent exact CPL3 C4/C8 gate found that frozen
renamed/memory population scales transfer with at most 11.70%/9.81% error, but
the wrong-path active-window ceiling explains only 25.98--46.78% of the CPI
gap and has negative per-core correlation with that gap in all four cases.
The resource estimator is retained for audit; the timing candidate is
rejected. See section 7 of that document for the frozen held-out evidence.

The superseded v3 run, two unreferenced duplicate result directories, and the
interrupted v4 trace scratch were deleted after their absence from all formal
pipeline indexes was verified. They are not counted in any statistic above.
