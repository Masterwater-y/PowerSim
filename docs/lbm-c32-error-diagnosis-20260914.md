# LBM C32 Error Diagnosis (zero-code-change)

Date: 2026-09-14. Status: measurement-only diagnosis on the frozen default-off
baseline. No simulator behavior changed. This report locates where the LBM C32
+7.54% CPI overestimate comes from before any intervention is designed, in
direct response to the "why do repeated LBM studies never yield a gain"
methodology concern.

## Inputs

- FastSim: `tmp/projected-feedback-disabled-20260914.no4tJO/verify.json`
  (candidate disabled; macro CPI 4.200373, +7.5364%, abs 0.294373
  cycles/macro-instruction, 32-core CPI MAE 0.321020).
- gem5 reference: `tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/stats.txt`.
  All 32 O3 (`switch*`) cores are cut at the identical common-window end of
  29,313,840 cycles; per-core gem5 CPI comes only from committed-instruction
  counts.

## Finding 1: the error is a uniform positive bias, not a hotspot

Per-core comparison (macro CPI = cycles / committed instructions):

- Instruction populations match gem5 within 0.1% on every core (0 mismatches),
  so the CPI gap is a pure cycle-count gap.
- Every one of the 32 cores overestimates cycles. Signed per-core CPI error is
  positive on all cores; per-core CPI MAE = 0.32093 equals the signed mean
  (0.32093), i.e. there is no cancellation.
- Excluding the kernel-heavy outlier core0 (CPI 11.92 vs 10.27, +16.0% cycle
  inflation), the remaining 31 compute cores inflate cycles by +5.2% to +12.0%,
  clustered near +7%. Two cores (4 and 13) stand out at ~+11.8%.

Conclusion: this is not a "fix one hot window" problem. It is a systematic
per-miss overcharge spread across all cores. That is exactly why prior
hotspot-targeted candidates (e.g. the C13 315-store window) improved a local
service window yet left whole-core CPI unchanged or worse — the local window
was never the dominant source of the whole-run gap.

## Finding 2: miss counts agree; exposed latency does not

FastSim vs gem5 aggregate memory events:

| metric | FastSim | gem5 Ruby | delta |
|---|---|---|---|
| L1D demand misses | 6,620,148 | 6,710,564 | -1.3% |
| L2 demand misses | 4,039,756 | 4,140,711 | -2.4% |

The two models agree on *what* misses. gem5's realized load-miss latency to the
sequencer is 255.27 cycles mean (LD) / 155.33 (ST). The whole-run overestimate
is 70,566,670 cycles = **17.47 extra exposed cycles per L2 miss** (10.66 per
L1D miss). Per-core extra cycles correlate with
`exposed_memory_penalty_cycles/inst` at r = +0.856 (core0 excluded) and are
uncorrelated with miss *rate* signals (l2_miss/inst, dtlb_miss/inst,
mem_acc/inst all r < 0.16 once core0 is removed).

Conclusion: the LBM error is a **memory-latency-exposure** error, not a
miss-rate error and not a DRAM service-time error. FastSim exposes ~17 cycles
per L2 miss more than gem5 overlaps away. Equivalently, the two-stage inference
under-credits memory-level parallelism / miss overlap on these cores.

## Why this reframes the past year of LBM work

- Prior candidates targeted DRAM service time (C13 window, projected RD/WB
  feedback). But miss counts and per-miss service are already close; the gap is
  in how exposed vs overlapped those misses are at the core. Faster or
  reordered DRAM responses cannot fix an overlap-accounting error, and the
  rejected projected-feedback candidate made it worse (+37.48%) precisely
  because it perturbed timing without adding overlap.
- The dominant lever is the exposed-penalty / MLP path, uniformly across cores.
  A correct intervention must *increase legitimate overlap* of independent
  outstanding misses within the two-stage inference, without reducing Q, fitting
  per-PC/workload constants, injecting gem5 timing, or clamping only speedups.

## Suggested next bounded experiment (not yet run)

Before any code change, design a narrow ablation on the fixed request stream
that measures, per core, how many of FastSim's exposed miss cycles have an
independent in-flight miss that gem5 would overlap. Acceptance is end-to-end:
LBM C32 CPI abs error and 32-core CPI MAE must fall while C1 (2M/8M) and
throughput do not regress. Local service MAE (cycles/request) is explicitly not
an acceptance metric.

## Reproduction

```sh
# FastSim default-off baseline
numactl --cpunodebind=0 --membind=0 ./build/fastsim simulate \
  --config configs/gem5-exp-projected-dram-feedback.cfg \
  --manifest tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/tao_trace/manifest.txt \
  --measurement-scope user-plus-kernel --cores 32 \
  --output OUT.json
# gem5 reference cycles/CPI: board.processor.switch*.core.{numCycles,cpi}
# gem5 miss counts: ruby_system.l*_controllers*.{Dcache,cache}.m_demand_misses
# gem5 miss latency: ruby_system.RequestType.{LD,ST}.miss_latency_hist_seqr::mean
```
