# FastSim native-kernel C4--C32 current-set validation

Date: 2026-08-22

Status: current-set diagnostic validation. The input/oracle gates pass, but
this combined set has no declared calibration/held-out split. It is therefore
an accuracy report for the currently collected cases, not a workload- or
core-count-generalization claim.

## 1. Result

FastSim completed all 39 currently available native-kernel cases: 39 passed,
0 failed. C32 `803.sph_exa_s` was still unavailable when this validation set
was frozen, so it is excluded rather than counted as a failure.

- strict user-plus-kernel perf-like CPI MAPE: **7.30%**;
- CPI P50 / P90 / P99: **8.87% / 11.84% / 13.15%**;
- CPI maximum APE: **13.63%** (`16c-811.tealeaf_s`);
- CPI WAPE: **6.26%**; signed aggregate bias: **-1.94%**;
- 24/39 cases are within 10%; all 39/39 are within 15%;
- serial FastSim trace-processing throughput: mean **8.68 M user-uops/s**,
  P50 **8.67 M**, P90 **11.93 M**, P99 **12.76 M**, minimum **4.74 M**.

The previous 20% CPI tail is not present in this current successful set: P99
is 13.15% and the sample maximum is 13.63%. This statement does not cover the
missing C32 SPH case.

PMU accuracy is not uniform. Strict retirement/population counters are exact
or effectively exact, while miss-path proxies remain materially worse:

- committed memory UOPs are exact in 39/39 cases;
- retired instructions differ by only 147 counts over 3.524 billion
  reference instructions (WAPE 0.0000042%);
- L1D misses have 6.46% MAPE and 5.37% WAPE;
- branch misses have 17.45% MAPE and 7.56% WAPE;
- private-L2 misses have 17.92% MAPE and 11.82% WAPE;
- DTLB misses are the largest dense-counter residual: 34.47% MAPE,
  49.84% WAPE, and -48.24% signed aggregate bias;
- DRAM reads/writes are unavailable in the event contract and cannot be
  scored against the all-zero gem5 reference fields.

## 2. Data gate and provenance

The input set is the union of:

- `tmp/taotrace-fst-v7-native-kernel-c4-c8-10m-20260821`: 20 cases,
  10 workloads at C4 and C8;
- `tmp/taotrace-fst-v7-native-kernel-c16-c32-10m-20260821`: 19 cases,
  10 workloads at C16 and 9 workloads at C32.

Workloads are `706.stockfish_r`, `710.omnetpp_r`, `777.zstd_r`, `782.lbm_r`,
`803.sph_exa_s`, `811.tealeaf_s`, `816.nab_s`, `854.graph500_s`,
`857.namd_s`, and `881.neutron_s`. Each core/workload configuration contributes
one equal-weight error sample. Percentiles use Type-7 linear interpolation.

The following gates were run before aggregation:

| Gate | Result |
|---|---:|
| FastSim rebuild | passed |
| `fastsim_tests` | passed |
| FastSim inference and native-input conservation checks | 39/39 passed |
| final-config/effective-target/uarch-profile identity | 39/39 valid |
| strict `kernel_events.json` validation | 39/39 valid |
| manifest type | 568/568 `fastsim-binary-warmup-slice` rows |
| measurement scope | 39/39 `user-plus-kernel` |
| CPI status | 39/39 `strict-native-user-plus-kernel-trace` |
| PMU source | 39/39 `fastsim-functional-native-user-plus-kernel-v1` |

Frozen executable and contracts:

| Artifact | SHA-256 |
|---|---|
| `build/fastsim` | `51a186ed2b3a75ebee74d0f7bbeedbc0b117a6c6baf8b86346f26ab1adaa987e` |
| `configs/gem5-v28_2-fs-native-kernel.cfg` | `fbf32710e037c2f5ec048ae2f080a5e43b0e0c4994c1f48291f58ee79dddc70e` |
| `configs/pmu-event-dictionary-v1.json` | `a0e56537d2cda107bf9a641721eb30e60236a88bebca25ea029b8299f86a34e1` |

This evaluation uses only FST/runtime configuration as FastSim input. It does
not feed gem5 timing or PMU outcomes into inference.

## 3. Functional warmup

All 39 manifests contain a record-bounded functional warmup slice for every
core. There are 568 manifest rows in total. FastSim replays the prefix, applies
the common measurement reset barrier, resets measurement counters/time, and
retains warmed cache/coherence, directory, branch-predictor, DTLB,
DRAM/controller, dependency, and response-scoreboard state.

Across the 39 cases:

- functional warmup records: 4,854,600,356;
- measurement user UOPs: 5,680,002,852;
- measurement native-kernel UOPs: 273,953,475;
- measurement user instructions: 3,406,384,301;
- measurement native-kernel instructions: 117,163,738.

## 4. CPI accuracy

The metric is strict native user-plus-kernel perf-like CPI:

```text
active user-plus-kernel cycles / retired user-plus-kernel macro instructions
```

All error columns are percentages. Bias is signed aggregate cycle bias;
negative means FastSim predicts fewer cycles than gem5.

| cores | cases | MAPE | P50 | P90 | P99 | WAPE | bias | max |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 10 | 6.48 | 7.44 | 10.43 | 11.47 | 6.30 | -5.32 | 11.59 |
| 8 | 10 | 7.39 | 9.53 | 11.80 | 12.30 | 5.88 | -4.48 | 12.35 |
| 16 | 10 | 7.55 | 9.16 | 12.40 | 13.51 | 5.24 | -2.92 | 13.63 |
| 32 | 9 | 7.84 | 8.51 | 11.65 | 12.29 | 6.86 | -0.36 | 12.36 |
| all | 39 | **7.30** | **8.87** | **11.84** | **13.15** | **6.26** | **-1.94** | **13.63** |

The largest per-case errors are:

| case | gem5 CPI | FastSim CPI | signed error |
|---|---:|---:|---:|
| `16c-811.tealeaf_s` | 0.61233 | 0.52886 | -13.63% |
| `32c-811.tealeaf_s` | 0.49539 | 0.43417 | -12.36% |
| `08c-811.tealeaf_s` | 0.71342 | 0.62529 | -12.35% |
| `16c-706.stockfish_r` | 0.67184 | 0.75419 | +12.26% |
| `08c-854.graph500_s` | 1.87777 | 1.65732 | -11.74% |

Workload direction is stable enough to be visible after combining core
counts:

| workload | cases | MAPE | signed mean error | max APE |
|---|---:|---:|---:|---:|
| stockfish | 4 | 10.49% | +10.49% | 12.26% |
| omnetpp | 4 | 9.64% | -9.64% | 10.45% |
| zstd | 4 | 2.84% | -2.05% | 4.38% |
| lbm | 4 | 3.88% | -1.15% | 6.77% |
| sph | 3 | 9.58% | -9.58% | 10.44% |
| tealeaf | 4 | 11.10% | -11.10% | 13.63% |
| nab | 4 | 2.42% | -2.42% | 3.53% |
| graph500 | 4 | 10.53% | -10.53% | 11.74% |
| namd | 4 | 9.90% | -9.90% | 11.59% |
| neutron | 4 | 3.20% | +3.20% | 5.49% |

Stockfish remains a systematic positive-bias workload: +8.11%, +10.10%,
+12.26%, and +11.47% at C4/C8/C16/C32. The current model changes therefore do
not remove its workload-specific residual, although all four cases stay below
13%.

## 5. PMU accuracy

Classification follows `configs/pmu-event-dictionary-v1.json`. Only strict
counters are formal headline counters. Proxy and diagnostic values are shown
to locate the remaining model residual; they must not be relabeled as target
hardware PMU events. WAPE includes absolute count error, while finite-case APE
excludes a nonzero prediction divided by a zero reference.

| counter | class | finite | MAPE | P50 | P90 | P99 | WAPE | signed bias | max |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| retired instructions | strict | 39/39 | 0.000005% | 0 | 0.000009% | 0.000056% | 0.000004% | -0.000004% | 0.000061% |
| committed memory UOPs | strict | 39/39 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| retired UOPs | diagnostic | 39/39 | 0.000003% | 0 | 0.000006% | 0.000033% | 0.000002% | -0.000002% | 0.000036% |
| retired branches | proxy | 39/39 | 0.000033% | 0 | 0.000131% | 0.000360% | 0.000029% | -0.000029% | 0.000378% |
| branch misses | proxy | 39/39 | 17.45% | 15.04% | 45.66% | 61.15% | 7.56% | +7.56% | 63.82% |
| DTLB accesses | proxy | 39/39 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| DTLB misses | proxy | 39/39 | 34.47% | 31.29% | 65.76% | 88.83% | 49.84% | -48.24% | 88.84% |
| L1D accesses | proxy | 39/39 | 0.000402% | 0.000195% | 0.000811% | 0.003434% | 0.000391% | -0.000391% | 0.004781% |
| L1D misses | proxy | 39/39 | 6.46% | 3.82% | 13.68% | 33.79% | 5.37% | -4.43% | 34.13% |
| private-L2 accesses | proxy | 39/39 | 6.46% | 3.82% | 13.68% | 33.79% | 5.37% | -4.43% | 34.13% |
| private-L2 misses | proxy | 39/39 | 17.92% | 13.04% | 39.80% | 113.07% | 11.82% | -11.48% | 147.33% |
| LLC accesses | diagnostic | 39/39 | 17.92% | 13.04% | 39.80% | 113.07% | 11.82% | -11.48% | 147.33% |
| LLC misses | diagnostic | 39/39 | 15.00% | 4.57% | 36.07% | 96.94% | 4.31% | -3.75% | 99.54% |
| DRAM reads | unavailable | 0/39 | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| DRAM writes | unavailable | 32/39 zero/zero; 7 undefined | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

The equal L1D-miss/private-L2-access and private-L2-miss/LLC-access rows are
not independent confirmations. They follow the current hierarchy accounting
identities: an upper-level miss becomes a lower-level access.

The strongest PMU evidence is therefore:

1. population accounting is closed: retirement, committed memory UOPs, branch
   population, DTLB accesses, and L1D access population are exact or nearly
   exact;
2. the largest dense residual is the DTLB miss decision, not the access
   population. The worst case is `16c-881.neutron_s`: 1,280,329 predicted vs
   11,469,831 reference misses, or -88.84%;
3. the cache residual begins at miss classification. The worst L1D miss case
   is `16c-710.omnetpp_r` at -34.13%; the large lower-cache P99 is amplified by
   sparse denominators, especially `32c-816.nab_s` private-L2 misses
   (41,699 predicted vs 16,860 reference, +147.33%);
4. branch-miss prediction is positively biased in aggregate (+7.56%);
5. DRAM accuracy is unmeasurable with this oracle. The gem5 contract explicitly
   leaves target IMC events unbound, while FastSim produced 13,089,855 reads
   and 4,415,565 writes. Treating these as 0% or 100% errors would be invalid.

## 6. Trace-processing throughput

Throughput is `scope_metrics.throughput.user_uops_per_second`, in million user
UOPs per host second. It is a host processing rate, not target IPC. The 39
FastSim cases were run serially (`--jobs 1`) so FastSim instances did not
compete with one another. The host was not exclusively reserved; it has 192
logical CPUs, and the post-run load averages were 3.71 / 7.01 / 7.49. These
figures should therefore be treated as serial loaded-host throughput, not a
dedicated-host benchmark.

| cores | cases | mean | P50 | P90 | P99 | min | max | total wall time |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 10 | 6.74 | 6.32 | 8.40 | 8.85 | 4.74 | 8.90 | 91.15 s |
| 8 | 10 | 9.47 | 10.41 | 11.15 | 11.48 | 5.24 | 11.52 | 127.97 s |
| 16 | 10 | 9.29 | 9.60 | 12.09 | 12.51 | 4.91 | 12.55 | 269.65 s |
| 32 | 9 | 9.28 | 8.71 | 12.36 | 12.83 | 5.08 | 12.88 | 634.85 s |
| all | 39 | **8.68** | **8.67** | **11.93** | **12.76** | **4.74** | **12.88** | **1,123.62 s** |

The minimum-throughput cases are all LBM: C4 4.74 M, C16 4.91 M, C32
5.08 M, and C8 5.24 M user-uops/s. This points to workload/event mix rather
than core count alone as the dominant throughput determinant.

## 7. Evidence and limitations

Machine-readable artifacts:

- `tmp/fastsim-native-validation-c4-c32-successful-20260821/summary.json`;
- `tmp/fastsim-native-validation-c4-c32-successful-20260821/summary.csv`;
- `tmp/fastsim-native-validation-c4-c32-successful-20260821/cases/*/validation.json`;
- `tmp/fastsim-native-validation-c4-c32-successful-20260821/cases/*/fastsim.json`;
- `tmp/fastsim-native-validation-c4-c32-successful-20260821/oracle-identity-audit.json`.

Known limitations:

- C32 SPH is absent, so the report covers 39 current successes, not a complete
  40-case matrix;
- no declared calibration/held-out separation exists in this run;
- the event dictionary classifies cache/branch/DTLB miss counters as proxy or
  diagnostic, so they locate model error but are not strict hardware-PMU
  accuracy claims;
- the host was shared during throughput measurement;
- the input intentionally contains no wrong-path stream; speculative effects
  come from FastSim's configured timing model rather than replayed wrong-path
  instructions.
