# Validation report

The current gem5-parameterized O3/DTLB implementation and its complete
23-workload × C4/C8/C16/C32 validation are reported in
[gem5 O3/DTLB Stage 3 validation](gem5-o3-dtlb-stage3-validation.md). It is the
authoritative current CPI/PMU/throughput result; the older sections below are
retained as development baselines.

The C4--C32 Stage-1 time-epoch results are reported separately in
[Time-Epoch Stage 1 validation](time-epoch-stage1-validation.md).  They are a
failed accuracy/certificate baseline, not a replacement for the accepted PMU
scope below.

Date: 2026-07-31. Build: CMake Release, C++17, with
`FASTSIM_ENABLE_NATIVE=ON`. Host: two-socket Intel Xeon Platinum 8457C,
96 physical cores / 192 logical CPUs.

## Acceptance summary

| Requirement | Result |
|---|---|
| 64-core aggregate throughput > 1 MIPS | Pass on gem5 functional replay: median 26.127 MIPS, minimum 25.964 MIPS |
| gem5 functional trace input | Pass: JSONL and aligned Parquet-to-v6 binary; v2-v5 remain readable for models that do not require destination classes |
| Physical cache address integrity | Pass: strict mode rejects virtual-only memory records |
| L1D/private L2 miss PMU | Aggregate-count scope only: C4 WAPE 0.043%/0.239%; C8 0.048%/0.266% |
| Per-CHA LLC lookup PMU | Aggregate-count scope only: C4/C8 WAPE 0.239%/0.265% |
| Branch miss PMU | Aggregate-count scope with complete outcomes: C4/C8 WAPE 0.165%/0.170% |
| LLC tag miss | Validated against functional memory-path labels; not conflated with Ruby protocol demand miss |
| Cross-core ordering/coherence events | Not validated; aggregate counts do not certify event order or Ruby protocol behavior |
| Total cycle/IPC | Still fail: scalar C4/C8 mean error 33.858%/30.703%; best experimental interval result 11.853%/14.990% |

## Sources

Full seed0 C4/C8 corpus:

```text
/data00/yinhaolang/TSim/data/
  raw_v28_1_business_a2_sharedzipf_seed0_c04/
  raw_v28_1_business_a2_sharedzipf_seed0_c08/
```

Legacy C32 cache-only case:

```text
/data00/yinhaolang/TSim/data/
  raw_v28_business_a1_sharedzipf_seed0_c32/
  W_v28_gofeed_base/
```

All comparisons use each case's `stats.txt` and only functional aligned
Parquet columns to drive FastSim. `path_class` is read afterward by the
validator solely as an LLC diagnostic label.

## Full TCSim C4/C8 results

All 23 workloads in each native core-count capture were replayed. CPI is
`sum(core cycles) / sum(commitStats0.numOps)`, matching TCSim's UOP-CPI
definition.

| Cores | Workloads | CPI mean / median / P90 / max abs. error | Signed bias | Per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 33.858% / 29.947% / 81.088% / 94.128% | +4.570% | 33.858% |
| 8 | 23 | 30.703% / 22.956% / 74.240% / 84.756% | +0.111% | 30.722% |

The nearly zero C8 signed bias is cancellation, not accuracy. The worst cases
include Pytorch base, random-memory MLP, integer/FP ALU, and sequential
memory. Their opposite error signs show that scalar issue-width throughput
plus serialized memory exposure cannot represent dependency/FU latency, ROB
pressure, or memory-level parallelism.

PMU count results use both workload-equal MAPE and count-weighted WAPE:

| Cores | Metric | Trace-equal MAPE | WAPE | Pooled signed error |
|---:|---|---:|---:|---:|
| 4 | L1D miss | 6.706% | 0.043% | -0.043% |
| 4 | Private L2 miss | 11.768% | 0.239% | -0.239% |
| 4 | CHA LLC lookup | 11.767% | 0.239% | -0.239% |
| 4 | Branch miss | 2.028% | 0.165% | -0.072% |
| 8 | L1D miss | 7.257% | 0.048% | -0.048% |
| 8 | Private L2 miss | 11.862% | 0.266% | -0.266% |
| 8 | CHA LLC lookup | 11.860% | 0.265% | -0.265% |
| 8 | Branch miss | 2.007% | 0.170% | -0.060% |

Trace-equal MAPE is inflated by compute microbenchmarks with only tens of
reference misses, while WAPE is dominated by high-volume workloads. Neither
statistic validates the ordering of individual memory operations. In
particular, this run does not validate coherence invalidations/upgrades,
remote supplies, Ruby transient/message classes, I-cache, TLB, or ROB stalls.

Reproduce the complete run with:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/validate_tcsim_c4_c8.py \
  --out-dir results/tcsim-v28_1-seed0-c4-c8-current
```

The validator reruns cases by default so a changed FastSim binary or
configuration cannot silently reuse stale per-case JSON. Pass
`--reuse-existing` only when the binary, configuration, converter, and gem5
inputs are known to be unchanged.

Machine-readable per-workload results are in
`results/tcsim-v28_1-seed0-c4-c8-current/summary.json` and `summary.csv`.
The generated compact report is `summary.md` in the same directory.

For context, the local TCSim v29 packed3 report evaluates the same seed0
23-workload C4/C8 corpus and UOP-CPI headline:

| Engine | C4 mean / median / P90 | C8 mean / median / P90 | UOP/s |
|---|---:|---:|---:|
| FastSim scalar | 33.858% / 29.947% / 81.088% | 30.703% / 22.956% / 74.240% | median 77.7M / 79.1M |
| FastSim interval weave | 11.853% / 12.107% / 19.098% | 14.990% / 13.984% / 33.666% | median 15.6M / 11.5M |
| TCSim v29 | 5.07% / 3.19% / 6.80% | 4.62% / 2.81% / 6.43% | 58.6K / 81.4K |

The throughput paths are different and are not a controlled speedup
comparison. The table establishes the real tradeoff: FastSim is much faster,
but its current CPI result is not competitive. TCSim source:
`/data00/yinhaolang/TCSim/docs/v29_packed3_checkpoint_evaluation_report.md`.

## Experimental interval-bound result

The v4 trace and `core.model=interval_bound` preserve the functional
operation/dependency fields and add a 256-UOP OoO lower-bound core. The shared
memory path is intentionally unchanged in this stage.

| Cores | Scalar mean / median / P90 | Interval-bound mean / median / P90 | Improved workloads |
|---:|---:|---:|---:|
| 4 | 33.858% / 29.947% / 81.088% | 23.682% / 16.687% / 74.059% | 16 / 23 |
| 8 | 30.703% / 22.956% / 74.240% | 20.180% / 16.699% / 65.854% | 17 / 23 |

Compute-side examples:

| Workload | Scalar C4 error | Interval C4 error | Interval C8 error |
|---|---:|---:|---:|
| Integer ALU | 65.872% | 0.089% | 0.101% |
| FP ALU | 74.267% | 0.156% | 0.179% |
| L1 mixed | 43.587% | 0.208% | 1.877% |
| L2 mixed | 29.947% | 2.906% | 3.239% |
| SIMD SSE | 8.353% | 16.687% | 16.699% |
| Integer divide | 35.030% | 16.156% | 16.144% |

The remaining worst cases are random-memory MLP (+82.811%/+74.401%),
Pytorch, sequential memory, and business traces with missing frontend/TLB
stalls. Aggregate PMU WAPE is unchanged from scalar replay: C4/C8 L1D
0.043%/0.048%, L2 0.239%/0.266%, CHA 0.239%/0.265%, and branch
0.165%/0.170%. This stability is useful but also demonstrates that aggregate
counts do not expose the changed cross-core event timestamps.

Reproduce with:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/validate_tcsim_c4_c8.py \
  --config configs/gem5-v28_1-c04-interval-bound.cfg \
  --out-dir results/tcsim-v28_1-seed0-c4-c8-interval-bound
```

## Experimental global-time interval weave

`core.model=interval_weave` replaces the per-event causal-frontier scheduler
with a TCSim-style protocol: each core proposes a 256-UOP lower-bound window,
one global time selects variable accepted prefixes, and all accepted memory
events are gathered and sorted once per global step. Memory latency is fed
back through producer-distance chains and in-order retirement at the end of
the step. The DDR4-2400 `22/22/22/4` memory-clock timings are converted to
`55/55/55/10` cycles at the trace profile's 3 GHz core clock.

| Cores | Mean / median / P90 / max UOP-CPI error | Signed bias | Per-core MAPE | Median UOP/s |
|---:|---:|---:|---:|---:|
| 4 | 11.853% / 12.107% / 19.098% / 30.733% | +8.949% | 11.853% | 15.61M |
| 8 | 14.990% / 13.984% / 33.666% / 47.376% | +10.531% | 14.980% | 11.53M |

This improves the scalar mean by 22.00/15.71 percentage points and the
event-at-a-time interval P90 by 54.96/32.19 points at C4/C8. It does not pass
the 6% mean / 10% P90 gate or match TCSim v29. The largest residuals are C8
Pytorch base (+47.38%), C8 sequential memory (-33.94%), and C8 Pytorch
heldout (+33.67%), which expose incorrect core-count scaling in contention
feedback plus missing frontend/TLB effects.

Aggregate PMU WAPE remains C4/C8 L1D 0.043%/0.048%, private L2
0.239%/0.266%, CHA lookup 0.239%/0.265%, and branch miss 0.165%/0.170%.
This does not certify order. The mandatory audit reports:

| Cores | Global steps | Memory events | Mean events/step | Max batch | Reordered pairs | Same-line reordered pairs |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 276,481 | 3,546,652 | 12.83 | 184 | 2,120,007 | 127,426 |
| 8 | 557,462 | 7,093,255 | 12.72 | 368 | 4,085,651 | 153,761 |

Same-line reorder is a path-changing conflict candidate. The current stage
records it but does not roll back and reweave the affected cores, so no
coherence-order claim is made.

Reproduce with:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/validate_tcsim_c4_c8.py \
  --config configs/gem5-v28_1-c04-interval-weave.cfg \
  --out-dir results/tcsim-v28_1-seed0-c4-c8-interval-weave-dramclock
```

## Exact-branch C4 results

Absolute aggregate relative error:

| Workload | L1D miss | Private L2 miss | CHA LLC lookup | Branch miss | LLC tag vs functional path | Cycle sum |
|---|---:|---:|---:|---:|---:|---:|
| GoFeed base | 0.048% | 0.276% | 0.276% | 0.085% | 1.997% | 24.803% |
| Flink base | 0.042% | 0.245% | 0.245% | 0.285% | 0.003% | 4.511% |
| BVC encoder base | 0.133% | 0.432% | 0.432% | 0.939% | 0.000% | 4.618% |
| Redis heldout | 0.065% | 0.291% | 0.288% | 0.294% | 0.000% | 32.994% |

For GoFeed, FastSim replayed 5,901 branch misses versus gem5's 5,896 and
53,080 committed branches versus 53,083. Per-core branch-miss MAPE was
0.085%.

Conversion of four approximately 818K-row Parquet cores took about
0.6 seconds when three cases were converted concurrently.

## C32 cache-only result

The legacy trace contains 26,190,794 UOPs / 20,869,941 macro instructions and
does not contain committed branch outcomes.

| Metric | FastSim | gem5/functional reference | Absolute error |
|---|---:|---:|---:|
| L1D misses | 397,444 | 398,016 | 0.144% |
| Private L2 misses | 272,186 | 274,272 | 0.761% |
| CHA LLC lookups | 272,186 | 274,271 | 0.760% |
| LLC tag misses | 128,942 | 126,640 functional memory path | 1.818% |

Across three final-binary runs, median simulation time was 0.508 seconds:
41.08 MIPS and 51.55 million UOP/s.
Branch PMU is explicitly unavailable (`branches_without_outcome = 426,752`).
The 9.9% cycle difference is diagnostic and includes the missing branch
penalty limitation.

## 64-core gem5 functional throughput protocol

The four C4 GoFeed traces were converted once, then mapped to 64 simulated
cores without copying or modifying trace data:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/replicate_binary_manifest.py \
  --input tmp/fastsim-trace/manifest.txt \
  --output tmp/fastsim-trace/manifest64.txt \
  --copies 16

./build/fastsim simulate \
  --measurement-scope user \
  --config configs/gem5-v28_1-c04.cfg \
  --cores 64 \
  --manifest tmp/fastsim-trace/manifest64.txt \
  --output tmp/fastsim-c64.json
```

Each run retired 41,733,056 instructions / 52,361,280 UOPs. Sequential
repetitions were unpinned and used the host page cache:

```text
26.65069145 MIPS
26.12699295 MIPS
25.96446722 MIPS
```

Median is 26.12699295 MIPS. All runs had zero causal-frontier producer waits
and a maximum 128 resident chunks (two/core). After removing wall-clock
throughput fields, all three statistics documents were byte-identical after
canonical JSON sorting.

This deliberately replicated capture tests the 64-core host-parallel and
shared-state paths under gem5 functional input. It is not used for PMU
accuracy claims; those use the native C4 and C32 captures above.

## Interpretation rules

- L1D and private L2 miss counters are direct Ruby comparisons. Access counts
  are also reported, but are not accepted accuracy metrics because one
  functional trace operation does not always have the same counting scope as
  one Ruby demand access.
- CHA LLC lookup is `llc_hits + llc_misses` per home slice and is compared to
  Ruby shared-LLC demand accesses. Total CHA requests also include
  permission-only upgrades and therefore use a different scope.
- FastSim LLC miss is a tag-state miss. Ruby demand miss can include
  protocol/permission behavior and is reported only as a diagnostic.
- Branch comparison is enabled only at 100% committed outcome coverage.
- Cycle comparison is never used to certify PMU accuracy.
