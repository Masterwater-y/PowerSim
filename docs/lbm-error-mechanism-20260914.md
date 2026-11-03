# LBM Error Mechanism Analysis and Reduction Boundary

Date: 2026-09-14; latest default/validation update: 2026-09-16. Status:
the partial bank/PRE v2 repair is enabled by default in the maintained native
profile by explicit user decision. LBM improves, with a small C16 regression;
the ten-workload validation finds increased average non-LBM error. Full repair
and broad accuracy improvement are not claimed. The subsequent ACT extension
remains rejected; the v2 mechanism is unchanged.
This report records the investigation of LBM's CPI overestimate, distinguishes
accounting evidence from unresolved delay causes, and defines the next
experiments under the throughput-first design and bidirectional error controls.
The bank-gated repair is retained with an explicit rollback switch. The two-stage
partial repair section describes its implementation; the final ACT-extension section
records the failed extension. Earlier default-off statements are historical.

> **2026-09-16 follow-up (current).** A response-critical bank gate improved
> strict 33-case CPI MAE **0.054931→0.054126** and MAPE **5.0614%→4.9821%**,
> but LBM MAE worsened **0.100394→0.106971**, MAPE **2.7546%→2.9273%**.
> Its frozen positive/negative retirement-span absolute CPI errors worsened
> **2.822306→11.135744** and **0.480882→8.969412**, with signed errors
> **+24.2328%→+95.6132%** and **−4.5262%→+84.4227%**.
> The candidate remains isolated, not promoted; maintained v2 is unchanged.
> Warmup-only relief now exactly reproduces the Graph500 C32 regression.
> See [phase ablations and the complete follow-up](store-wait-v3-critical-bank-20260916.md).

> **2026-09-16 default update (current).** The user requested v2 default-on and
> maximum-parallel validation on ten workloads. The maintained native profile now
> enables `core.store_wait_relief`; `--store-wait-relief false` restores the prior
> behavior on a new run. All 80 paired C4/C8/C16/C32 runs completed. The 33 cases
> with exact macro populations have CPI MAE **0.055661→0.054931**, MAPE
> **5.0359%→5.0614%**; their 29 non-LBM cases worsen to CPI MAE **0.048660**
> from **0.047218**. Graph500 C32 is the largest regression: signed error
> **−2.5482%→−4.1365%**, absolute CPI error **0.060812→0.098717**. Seven
> cases with pre-existing population differences remain separately diagnostic.
> See [the complete default-on report](store-wait-v2-default40-20260916.md).

> **2026-09-14 correction (read first).** The original mechanism claim below —
> that LBM's overestimate comes from exposed miss latency being *summed rather
> than max-ed* across overlapping misses — was **falsified by intervention on
> the active timing path**. Two errors compounded it: (1) it described the
> `process_memory_event` path, but the maintained LBM profile
> (`response_materialized_uop_fast_kernel=true`) executes
> `compute_core_timing_feedback_impl<false,true,false>`, a *different* template
> instantiation whose load frontier already uses `std::max`, not a sum; (2) the
> conclusion was reached by reading source, never by perturbing the active path
> and measuring. A locally-MLP-gated exposure relief, correctly wired into the
> active load frontier, moved LBM C32 by only **-0.156%** while it triggered
> 3.3x more often than on tealeaf (which moved -10.58%). LBM's +7.5% is **not**
> on the memory-response frontier. The corrected analysis and the intervention
> evidence are in the section "Intervention result: LBM error is not on the
> memory frontier" near the end. The mid-document exposure text is retained
> unchanged as the falsified hypothesis of record.
> **Provenance qualification added 2026-09-15:** the quantitative intervention
> claims in this historical notice require matching run artifacts; the saved
> sweep inspected in the latest audit does not reproduce them. The latest
> notice and final section control the next experiments.

> **2026-09-15 update (read second; corrected after source/output audit).**
> Investigate `interval_extra`, but **do not instrument
> `checkpoint_closed_gap_cycles` as the maintained path's source**. The fast
> kernel forces `response_paired_frontier=false`; its active charge is
> `max(0, sparse_last_retire_cycle - base_end_cycle)` in the sparse-scoreboard
> branch. Frozen LBM C32 output confirms `exposed_memory_penalty_cycles =
> 941,155,750` while paired-frontier checkpoints and closed-gap cycles are both
> **zero**. CPI is 4.200373 vs gem5 3.906000, signed error +7.5364%, absolute
> error **0.294373 cycles/macro-instruction**. The earlier closed-gap source
> chain below joined mutually exclusive branches and is superseded. Next:
> reconcile the active interval-tail charge, trace its delay origins, then test
> a locally justified candidate. See the final section, "2026-09-15 follow-up:
> active-path correction and next experiments". This update adds source and
> saved-output evidence plus an experiment plan; it does not report new runs.

> **2026-09-15 execution result (read third).** The live fast-kernel ledger
> exactly reproduces the charged retirement tail for LBM C4/C8/C16/C32. The
> main newly localized edge is **SQ admission before issue**: 2.193836 versus
> 2.690427 cycles/macro-instruction at C16/C32, accounting for 72.03% of the
> tail's growth between those cases. This identifies a blocking edge, not a
> proven faulty SQ rule. C32 overestimates on all 32 cores; C16's small total
> error includes opposite-sign per-core errors. There is **no timing repair**
> in this execution. See "2026-09-15 execution: active tail ledger and SQ
> admission localization" below for conservation, paired witnesses, and limits.

> **2026-09-15 SQ-owner follow-up (read fourth).** The preselected positive
> C32 checkpoint now has matched store-service evidence: its store-chain span
> excess is 38,064 cycles, of which 35,586 (93.49%) is service-span excess.
> Matched DRAM stores average 640.18 FastSim versus 306.14 gem5 cycles. The
> preselected negative checkpoint instead averages 257.54 versus 287.07.
> Direct SQ-to-issue handoff is 3 FastSim cycles in both, and captured TSO
> sends satisfy the existing max recurrence exactly. This localizes the
> positive witness further to modeled DRAM service waiting; it does not yet
> attribute all C32 absolute error or establish a deployable repair. See the
> final SQ-owner execution section. Default simulated timing remains unchanged.

> **2026-09-15 bank-provenance result (read fifth).** A concrete local
> inconsistency is now paired: projected same-core store requests overlap and
> inherit their earlier store's bank backlog, but both the final FastSim send
> timeline and the matched native admission timeline already place that
> parent's response before the child. The positive window has 60 such parent
> pairs, 54 with nonzero bank backlog, totaling 24,056 modeled request-wait
> cycles; the negative window has 67 pairs, 12 nonzero, totaling 785.
> Consuming the old queued service duration at the later serialized send is
> the newly demonstrated local mechanism. These sums are not recoverable CPI
> cycles. Cross-bank ACT ownership and a consistent scheduling intervention
> remain open; no timing fix is delivered. See the final bank-provenance section.


> **2026-09-15 repair experiment (read sixth).** A default-off canonical
> store-response projection was implemented and tested in an isolated worktree.
> It updates cache/MSHR/bank/channel state together and excludes projected send
> waiting from service duration, but **fails the full-run gate on all four LBM
> core counts**. C32 CPI is 88.148109 (+2156.7358%, absolute error 84.242108),
> versus baseline 4.200373 (+7.5364%, absolute error 0.294373). Four-case CPI
> MAE rises from 0.116875 to 36.209032 cycles/macro-instruction. Extensive
> horizon crossings and cross-epoch arrival inversions expose the limits of
> this projection; they are not a quantitative attribution of the original
> error. The candidate is rejected and isolated. The maintained sources and
> executable are unchanged; configuration fallback exactly reproduces all four
> baselines. See the final repair-experiment section.


> **2026-09-15 two-stage partial repair (read seventh; retained implementation).**
> `core.store_wait_relief` now provides a **default-off mechanism correction**:
> a same-core earlier ordinary store must own an expired DRAM reservation, its
> removal must advance this request's bank command, and the accepted core-side
> TSO frontier must already cover that owner before service can be reduced.
> No DRAM scalar is fitted. LBM C32 CPI **4.200373→4.158580**, signed error
> **+7.5364%→+6.4665%**, absolute CPI error **0.294373→0.252580** (14.20% lower).
> Four-case CPI MAE **0.116875→0.100394** (14.10% lower): C4/C8/C32 improve,
> C16 absolute error **0.017452→0.025921** worsens slightly. Both frozen local
> windows improve. This is a useful partial repair, **not complete error
> attribution or a default-on recommendation**. The broader first version
> regressed aggregate accuracy and was superseded by the bank gate. See the
> two-stage partial-repair section for cases, controls, limits, and rollback.

> **2026-09-15 further optimization (read eighth; latest experiment).**
> Extending the existing bank-gated relief to explicitly owned ACT floors was
> implemented and tested in isolation, but **is not retained**. All four LBM
> absolute errors worsen versus v2; CPI MAE **0.100394→0.102885**. C32 signed
> error **+6.4665%→+6.5913%**, absolute CPI error **0.252580→0.257455**.
> Both frozen windows also regress despite passing identity/order checks.
> The maintained v2 source and binary are unchanged. This rejects this coverage
> extension, not the possibility of further improvement within two stages.
> Next: separate direct local credits from newly induced shared-service waits,
> starting at the first changed request and including warmup carry.


## Methodology discipline

Two-stage inference is a throughput-first approximation. The goal here is *not*
to drive LBM error to zero; it is to decide whether a mechanism-level change can
reduce the LBM overestimate without (a) degrading throughput or (b) worsening
the many cases that already underestimate. Current claims require evidence from
the active source path and matched measurement against gem5. Superseded
interpretations are retained explicitly as history, not as established causes.
Acceptance requires end-to-end CPI and separately measured throughput;
`tools/run_cpi_guardrail.py` checks only the frozen CPI compatibility band.
Local service MAE is explicitly not an acceptance metric.

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

| case | signed err | Absolute CPI error (cyc/macro-inst) | exposed penalty (% of cyc) | L2 miss/1k inst | exposed/L2 miss |
|---|---|---|---|---|---|
| LBM C32 | +7.54% (over) | 0.294373 | 93.3% | 16.82 | 233.0 |
| TeaLeaf C16 | -13.31% (under) | 0.086111 | 38.0% | 2.72 | 78.3 |
| namd C16 | -10.57% (under) | 0.056866 | 21.3% | 0.83 | 123.5 |

The underestimating cases live in a *different bucket*: their exposed-memory
penalty is a small fraction of cycles and they already fall below gem5. A change
that globally increases miss overlap (reduces exposed latency) would push
tealeaf/namd further down — the same class of failure as the rejected projected
RD/WB candidate. Therefore any exposure-reducing mechanism must be gated on a
locally observable high-MLP condition (multiple independent outstanding misses),
not applied globally, and must be validated against the guardrail before
promotion.

## Intervention result: LBM error is not on the memory frontier

The exposure hypothesis above was tested on the *active* timing path by two
interventions, both default-off and reverted/kept-disabled after measurement.

### Locating the active path first (the methodology fix)

The maintained LBM profile sets `core.model=interval_weave`,
`sim.interval_scheduler=time_epoch`, and
`core.response_materialized_uop_fast_kernel=true`. Under the fast kernel the
timing loop is the template instantiation
`compute_core_timing_feedback_impl<false,true,false>` — *not* the
`process_memory_event` path the original section analyzed. On this active path:

- The load-completion frontier already max-combines overlapping misses:
  `load_data_ready_cycle = std::max(load_data_ready_cycle, event_response_cycle)`
  (`simulator.cpp:13793-13795`). There is no sum-over-overlap to remove.
- `event_exposed_cycles` is effectively dead for loads: it feeds only the
  parallel `completion_extra` floor (`:13841-13843`), which is dominated by the
  `load_producer_ready_cycle` repair (`:13958-13966`).
- Per-cause attribution counters (`response_critical_*`,
  `ResponseCriticalCycleCounters`) are hard-gated off here:
  `attribute_cycles = !MaterializedFastKernel && config_.cpi_attribution`
  (`simulator.cpp:10642`), and the fast kernel is mutually exclusive with
  `cpi_attribution` by config validation (`config.cpp:848-862`). So the
  attribution breakdown reads all-zero on the active path; it only populates on
  the *inactive* scalar path. Reading it would repeat the original mistake.

### Intervention 1 — locally-MLP-gated exposure relief (kept, default-off)

`core.mlp_exposure_relief` (default 0.0, range [0,1]) credits the exposed excess
of a retirement-blocking L2-miss load by `factor = 1 - relief*(1 - 1/D)`, where
`D` is the L2-MSHR overlap depth at issue, gated on `D>1`, feeding the relieved
value into `load_data_ready_cycle` and matching `event_exposed_cycles`. Physical
MSHR/sequencer/IQ releases keep full latency. `relief=0.0` is bit-identical.

Sweep over the 12 guardrail cases (`tools/sweep_mlp_relief.py`):

Historical activation results below await matching run provenance (see the
follow-up audit). Added absolute-error values are approximate conversions from
the recorded rounded relative errors and references 3.906 / 0.724112; they do
not establish a reproduced measurement.

| case | cores | relief | Recorded signed error | Approx. absolute CPI error (cyc/macro-inst) |
|---|---|---|---|---|
| lbm (target) | 32 | 0.00 | +7.536% | ≈0.29436 |
| lbm (target) | 32 | 0.25 | +7.192% | ≈0.28092 |
| lbm (target) | 32 | 0.50 | +7.642% | ≈0.29850 |
| lbm (target) | 32 | 1.00 | +7.369% | ≈0.28783 |
| tealeaf | 8 | 0.00 | -11.984% | ≈0.08678 |
| tealeaf | 8 | 0.25 | -18.041% | ≈0.13064 |
| tealeaf | 8 | 0.50 | -25.044% | ≈0.18135 |
| tealeaf | 8 | 1.00 | -39.810% | ≈0.28827 |

| Recorded aggregate (12 cases) | r=0.00 | r=0.25 | r=0.50 | r=1.00 |
|---|---|---|---|---|
| CPI MAE (cyc/macro-inst) | 0.08374 | 0.09728 | 0.11278 | 0.13342 |

LBM C32 moves non-monotonically inside noise (±0.3pp) while tealeaf is driven
catastrophically further below gem5. Trigger counts at `relief=1.0`: LBM fired
604,530 times crediting 145.3M cycles yet moved CPI only **-0.156%**; tealeaf
fired 183,226 times (2.1M fewer cycles) yet moved **-10.58%**. The relief
*heavily engages* LBM — the null result is not "it never fires" (case a) but
"the LBM load-ready frontier is off the CPI critical path" (case b).

### Intervention 2 — full memory-latency elasticity probe (throwaway, reverted)

To bound the effect independent of any overlap heuristic, a throwaway probe
scaled the FULL response latency of every retirement-blocking load, feeding all
frontiers (memory_response, load_data_ready, MSHR/sequencer release):

| lat_scale | LBM C32 CPI | vs baseline | vs gem5 (3.906) | Approx. absolute CPI error (cyc/macro-inst) |
|---|---|---|---|---|
| 0.5 (half latency) | 4.16974 | -0.73% | +6.75% | ≈0.26374 |
| 0.8 | 4.17648 | -0.57% | +6.92% | ≈0.27048 |
| 1.0 (baseline) | 4.20037 | 0 (bit-identical) | +7.54% | ≈0.29437 |
| 1.2 | 4.21948 | +0.46% | +8.03% | ≈0.31348 |

Absolute errors in this historical probe are computed from the displayed
rounded CPI values; retain raw unrounded values in future runs.

**Halving all memory latency reduces LBM C32 CPI by only 0.73%.** The linear
slope is ≈0.061 CPI per unit of latency scale, so extrapolating to zero memory
latency lands near 4.14 CPI — still **+6% over gem5**. Memory latency explains
at most ~20% of the 0.294 cyc/inst gap, and that fraction is not cleanly
recoverable (some is genuine serialization gem5 also pays).

### Conclusion

LBM C32's +7.5% overestimate is **dominated (~80%) by the compute/issue/retire
base schedule, not by memory response latency**. Every prior LBM candidate acted
on the memory-exposure frontier, which is why they all produced noise-level
movement. The next investigation must target the per-uop base issue/completion
schedule (the `interval_extra_q16` / paired-frontier `checkpoint_closed_gap`
accumulation at `simulator.cpp:11302-11305`, `:15553`), i.e. how much
compute-side latency the interval model puts on LBM's critical path relative to
gem5's O3 overlap — measured by intervention on the active path, never by
reading inactive-path instrumentation.

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

Do not pursue any further memory-exposure change for LBM. Interventions 1 and 2
prove LBM C32's error is not on the memory-response frontier: a locally-gated
exposure relief that heavily engages LBM moves it -0.156%, and halving all
memory latency moves it -0.73%. `core.mlp_exposure_relief` stays default-off and
is retained as a failed candidate with its evidence, not deleted. The
`+7.54%` is an accepted cost of the throughput-first design *for now*, and the
guardrail prevents future changes from trading it against the underestimating
tail.

The productive next step is on the compute/issue/retire base schedule, not
memory. Instrument and perturb the paired-frontier `checkpoint_closed_gap`
accumulation on the active fast-kernel path to quantify how much compute-side
latency the interval model serializes that gem5's O3 overlaps.

## Methodology reflection (why LBM yielded no gains across attempts)

Every prior LBM attempt, including this one's first framing, shared one failure
mode: **causal conclusions drawn from reading source instead of from perturbing
the running model.** Three concrete lessons:

1. **Confirm the active code path before analyzing it.** A templated timing core
   has multiple instantiations. The maintained LBM profile runs
   `compute_core_timing_feedback_impl<false,true,false>`; the falsified "sum not
   max" story described a different, inactive instantiation. Always resolve
   which template/branch a profile actually executes first.

2. **Instrumentation can be path-gated.** The `response_critical_*` attribution
   is disabled whenever the fast kernel is on. Zero counters do not mean "no
   cost here" — they can mean "this counter is dead on this path." Verify the
   counter is live before trusting it.

3. **Causal claims require intervention, not inspection.** The exposure
   hypothesis survived multiple document revisions purely on code reading. One
   default-off intervention (relief) plus one throwaway probe (latency scale)
   falsified it in a single session by measuring elasticity directly. Prefer a
   cheap, reversible perturbation of the active path over any amount of static
   reasoning.

The tealeaf/namd reverse-risk finding is the positive corollary: because those
cases *are* memory-frontier-bound (tealeaf moved -10.58% under the same relief),
they belong in the guardrail precisely to catch any exposure change that would
help LBM at their expense — a trade the elasticity data now shows would not even
help LBM.

## 2026-09-15: base-schedule intervention — the dominant term is the interval-extra closed gap

> **Historical interpretation; superseded by the follow-up below.** The sweep
> observations are retained, but the identification of `interval_extra` with
> `checkpoint_closed_gap_cycles` is invalid on the maintained fast kernel.
> Anti-monotonicity and a large accounting bucket do not by themselves prove
> which stage overcharges. These hardware overrides use the original gem5
> reference as a diagnostic anchor, not a matched changed-hardware oracle.

The 2026-09-14 conclusion pointed at "the compute/issue/retire base schedule" as
the next target. This section executes that investigation on the active
fast-kernel path with pure default-off config overrides (no code change, no
default change, nothing committed). The result narrows the dominant term much
further than "base schedule": it is **not any structural resource**, and it is
**not the fetch front-end**. It is the `interval_extra` closed-gap residual that
the model buckets under the mislabeled `memory_penalty` counter.

### Method

`tools/probe_lbm_base_schedule.py` sweeps the interval core's structural knobs
one at a time over LBM C32 (`configs/gem5-fs-native-kernel.cfg` +
`--measurement-scope user-plus-kernel --cores 32`) and reports CPI elasticity
against the frozen baseline (4.200373397) and gem5 (3.906). Every maintained
value reproduces the baseline bit-identically (sanity check passed on all six
knobs). Follow-up interventions toggle the fetch-supply stack and build an
"unbounded machine" to separate contention from base latency.

### Finding 1 — every structural knob is near-inert or anti-monotonic

Relative/absolute differences below use the historical rounded CPI and fixed
gem5 reference 3.906; they are diagnostic comparisons, not matched-hardware
accuracy results. All six maintained-value runs were recorded as identical.

| knob | value (maintained marked *) | CPI | Approx. signed difference vs reference | Approx. absolute CPI difference (cyc/macro-inst) |
|---|---|---|---|---|
| `core.issue_width` | 4 | 4.15980 | +6.50% | ≈0.25380 |
| `core.issue_width` | 8 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.issue_width` | 16 | 4.19955 | +7.52% | ≈0.29355 |
| `core.fetch_width` | 4 | 4.12194 | +5.53% | ≈0.21594 |
| `core.fetch_width` | 8 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.fetch_width` | 16 | 4.28590 | +9.73% | ≈0.37990 |
| `core.dispatch_width` | 4 | 4.18089 | +7.04% | ≈0.27489 |
| `core.dispatch_width` | 8 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.dispatch_width` | 16 | 4.19267 | +7.34% | ≈0.28667 |
| `core.commit_width` | 4 | 4.17287 | +6.83% | ≈0.26687 |
| `core.commit_width` | 8 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.commit_width` | 16 | 4.19819 | +7.48% | ≈0.29219 |
| `core.rob_entries` | 96 | 4.20202 | +7.58% | ≈0.29602 |
| `core.rob_entries` | 192 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.rob_entries` | 384 | 4.20786 | +7.73% | ≈0.30186 |
| `core.iq_entries` | 32 | 4.20236 | +7.59% | ≈0.29636 |
| `core.iq_entries` | 64 * | 4.20037 | +7.54% | ≈0.29437 |
| `core.iq_entries` | 128 | 4.20646 | +7.69% | ≈0.30046 |

No single resource explains the +0.294 cyc/inst gap. The only knob with
meaningful movement, `fetch_width`, moves the *wrong way* (a 16-wide frontend
raises CPI to +9.73% vs gem5), which is a machine-inconsistency signature, not a
real bottleneck.

### Finding 2 — the fetch front-end is not the driver

Disabling the entire fetch-supply stack (`fetch_supply_model`,
`fetch_supply_speculative_shadow`, `fetch_supply_physical_request_ledger`,
`fetch_supply_lower_hierarchy`, `fetch_supply_static_instruction_span`,
`cache.l1i.enabled`) moved LBM C32 CPI by only **+0.002** (4.20037 → 4.20239);
the 20.5M exposed fetch-refill cycles persist. The fetch-buffer refill exposure
is intrinsic to the trace, not a fetch-supply artifact, and it is not the
dominant CPI term either.

### Finding 3 — the machine is anti-monotonic (decisive)

Widening *every* execution resource at once, with `fetch_width` pinned at 8 to
exclude the inverted-fetch artifact:

| configuration | CPI | Δ vs baseline | vs fixed gem5 reference | Approx. absolute CPI difference (cyc/macro-inst) |
|---|---|---|---|---|
| baseline (8-wide) | 4.20037 | 0 | +7.54% | ≈0.29437 |
| ROB=1024, IQ=512 only | 4.18646 | -0.01392 | +7.18% | ≈0.28046 |
| exec widths=32, fetch=8, ROB/IQ default | 4.20860 | +0.00822 | +7.75% | ≈0.30260 |
| all exec widths=32, ROB=1024, IQ=512, fetch=8 | 4.21568 | +0.01530 | +7.93% | ≈0.30968 |

Giving LBM more of these resources makes CPI **worse in this sweep**. This
rejects the simple explanation that widening those resources alone removes the
error. It does not prove the absence of contention: the intervention can also
change request ordering, overlap, and checkpoint populations. Audit those
changes before attributing the inversion to accounting.

### Finding 4 — the dominant term is the mislabeled closed-gap residual

Under baseline, the `exposed_memory_penalty_cycles` counter is **941,155,750 of
1,008,609,550 cycles = 93.3% of all simulated cycles**, and it *grows* to
987.8M when the machine is widened (tracking the anti-monotonic CPI). Yet the
2026-09-14 latency-scale probe showed halving all DRAM latency moves CPI only
-0.73%. A "memory penalty" that is 93% of all cycles but insensitive to memory
latency is not a memory penalty. Source confirms the mislabel: the counter is
fed the whole interval-extra residual —

```
cores_[core]->total.memory_penalty_cycles += exposed_extra_cycles;   // simulator.cpp:16620
// where exposed_extra_cycles = fixed_to_cycle_ceil(extra_q16),  extra_q16 = interval_extra_q16[core]
// and interval_extra_q16[core] = cycles_to_fixed(checkpoint_closed_gap_cycles) // simulator.cpp:15554
// with checkpoint_closed_gap_cycles accumulated at simulator.cpp:11305 during frontier settlement
```

`checkpoint_closed_gap_cycles` is the paired response-frontier's *closed gap* —
the cycles the two-stage settlement charges when it collapses the deferred
response frontier back onto the committed schedule. It is generic scheduling
residual, not DRAM latency, but it is bucketed and reported as `memory_penalty`.
This is why every prior "memory-side" candidate produced only noise: they acted
on a label, not on the term that actually dominates.

### Finding 5 — cross-workload contrast isolates LBM

Same decomposition on C4 and C32 (user-plus-kernel):

| case | signed err | Absolute CPI error (cyc/macro-inst) | `memPenalty` / total cycles | `fetchExposed` / total cycles |
|---|---|---|---|---|
| lbm C32 | +7.54% (over) | 0.294373 | **93.3%** | 2.1% |
| lbm C4 | -2.99% (under) | 0.096513 | 90.9% | 2.8% |
| tealeaf C32 | -12.02% (under) | 0.060242 | 21.5% | 17.2% |
| tealeaf C4 | -5.20% (under) | 0.057070 | 65.7% | 8.9% |
| namd C32 | -10.46% (under) | 0.056394 | 23.5% | 15.7% |
| namd C4 | -12.43% (under) | 0.078236 | 28.2% | 19.3% |

LBM lives almost entirely inside the interval-extra accounting bucket (>90%),
while the underestimating C32 cases sit at 21-23%. However, LBM C4 also has a
90.9% bucket and **underestimates** CPI. Bucket share cannot establish
overcharge or serve as a relief gate. The paired-frontier interpretation is
superseded by the active-path audit below.

### Dominant-term statement at the time (superseded)

- Falsified (2026-09-14): "exposed miss latency summed not max-ed." — inactive path.
- Superseded (2026-09-14): "~80% compute/issue/retire base schedule." — direction
  correct, but too coarse: no structural resource carries the term.
- Superseded (2026-09-15, before the follow-up audit): the dominant term is the **paired response-frontier
  closed-gap residual** (`checkpoint_closed_gap_cycles`, mislabeled as
  `memory_penalty`), which the settlement over-charges on LBM's serialized
  schedule and which is anti-monotonic in machine resources.

### Earlier mechanism proposal (superseded, not built)

Target the closed-gap settlement, not memory and not any resource width. A
credible default-off, guardrail-gated candidate would reduce the closed-gap
charge only where a locally observable condition shows the interval schedule
serialized work that an O3 window would have overlapped — e.g. gating on
concurrent independent in-flight work at the settlement point — and must be
validated to not push the already-underestimating tealeaf/namd (21-23% bucket)
further below gem5. First diagnostic step before building it: instrument
`checkpoint_closed_gap_cycles` at `simulator.cpp:11305` to attribute the closed
gap to its settlement cause (completion vs SQ-release vs retire vs store-drain
frontier), so the gate targets the specific frontier that over-charges LBM.

## 2026-09-15 follow-up: active-path correction and next experiments

Status: **source/saved-output audit and proposed experiments, not an implemented
repair**. The immediate task is to attribute the maintained fast kernel's
**checkpoint retirement-tail extension**. Instrumenting line 11305 alone would
repeat the inactive-path mistake. A reduction candidate comes only after an
identified delay edge explains an excessive end-to-end span.

### 1. Evidence gate: correct the source chain before adding counters

The current source distinguishes two mutually exclusive calculations:

| Stage | Maintained fast kernel | Optional paired-frontier path |
|---|---|---|
| Path selection | `compute_core_timing_feedback_impl<false,true,false>` | Requires `MaterializedFastKernel=false` |
| Local paired flag | `!MaterializedFastKernel && config_.response_paired_frontier`, therefore false | May be true |
| `interval_extra` source | `max(0, sparse_last_retire_cycle - base_end_cycle)` | `checkpoint_closed_gap_cycles` |
| Source site | `src/simulator.cpp:15579`, sparse-scoreboard branch | `:15554`, paired branch; accumulation at `:11305` |
| Common sink | `commit_timing_feedback`: add `extra_q16` to interval gap and memory-penalty bucket (`:16618`) | Same sink; paired mode also adds a final open tail (`:5322`) |

On the maintained branch,
`base_end_cycle = last_bound_retire_q16 / kCycleUnit + interval_gap_cycles`.
The name `memory_penalty` therefore describes a **retirement-tail extension
accounting bucket**, not a direct measurement of DRAM service. Its large share
does not identify the root delay or the recoverable fraction.

The frozen output independently confirms the branch selection:

| Saved LBM C32 field | Value |
|---|---:|
| `configuration.response_materialized_uop_fast_kernel` | true |
| `configuration.response_paired_frontier` | false |
| `totals.exposed_memory_penalty_cycles` | 941,155,750 |
| `totals.response_frontier_checkpoints` | 0 |
| `totals.response_frontier_closed_gap_cycles` | 0 |
| `totals.response_frontier_final_tail_cycles` | 0 |

Evidence: [frozen summary](../tmp/load-ready-default40-20260912.Ydiy65/summary.json)
and [LBM C32 raw output](../tmp/load-ready-default40-20260912.Ydiy65/cases/formal-32c-782.lbm_r/fastsim.json).
These are historical results from the recorded binary; source inspection does
not substitute for reproducing them with the next instrumented binary.

There are two further limits on the earlier conclusions:

- Resource/fetch sweeps and latency scaling identify sensitivity under those
  interventions, not an exhaustive causal decomposition. Small CPI movement
  can reflect competing changes or movement absorbed by another gate. The
  earlier linear extrapolation to zero latency is not a measured recoverable
  bound. Neither "all structural resources excluded" nor "80% proven
  non-memory error" is a candidate premise.
- [The saved MLP sweep](../tmp/mlp-relief-sweep-20260914.json) contains identical
  CPI rows for all four relief values, unlike the activated-intervention table
  above. Locate the matching binary/config/output for that table before reusing
  its quantitative conclusions. Preserve it as an unverified historical
  observation; the saved file does not reproduce it. The base-schedule probe
  also deletes successful raw outputs, so the next run must retain them.

### 2. First implementation slice: observe the active charge without changing timing

Add a dedicated **default-off diagnostic**, live in the materialized kernel;
do not turn off that kernel or enable `cpi_attribution` to obtain the primary
evidence. Names below describe proposed fields, not existing CLI options.

1. At the `interval_extra_q16` assignment, record the selected source branch,
   core, accepted sequence range, checkpoint/attempt identity, old interval
   gap, base-end retirement, corrected last retirement, and charged extra.
   Include accepted UOP/macroinstruction counts and a request-stream digest.
   Start with complete per-core aggregates and bounded checkpoint samples.
2. Keep trial counters in `TimingFeedback`; publish only the accepted prefix
   through `commit_timing_feedback`. Rejected/retried attempts, functional
   warmup, and committed measurement must be distinguishable. A global counter
   incremented on every speculative solve would overcount.
3. Reconcile `sum(accepted extra_cycles)` with the memory-penalty increments
   owned by this branch. Account separately for any other sink and final-tail
   contribution. Check integer/Q16 conversion, measurement reset, and original
   per-core population. Explain every difference; do not force the sum to fit.
4. Compare original binary, new binary with diagnostics off, and the same new
   binary with diagnostics on. Require equal timing, scope counters, per-core
   work, and request-stream identities; exclude only diagnostic fields and
   host wall time from equality. Record active branch counts so zero attribution
   cannot be mistaken for zero cost.

Only after this ledger closes, add **two distinct attribution views**:

| View | What to record | Interpretation |
|---|---|---|
| Immediate timing gate | Selected completion/producer-ready, ROB admission, SQ release, ordered retirement/commit, store-drain/serialize edge; keep base/resource/other and unknown categories | Where the corrected timing is constrained |
| Delay origin | Dynamic owner core/sequence/request identity, selected predecessor, new delay versus inherited delay, local versus carried checkpoint state | Where the constraint originated |

Follow the selected predecessor through the existing dispatch → issue →
completion → retire calculations. A retirement gate may inherit an earlier
load or SQ delay; "retire wins" alone does not mean commit width caused it.
Keep all tied winners and mark unresolved provenance explicitly. Never assign
the entire checkpoint to the final UOP's type and call that root-cause evidence.

Report two separate outputs: (a) a conservative checkpoint-owner histogram
whose assigned cycles plus unknown cycles equal the accepted charge, explicitly
labeled an **endpoint classification**; (b) sampled predecessor-chain witnesses
that partition the exposed tail into non-overlapping inherited/new segments.
Do not add full completion, SQ, retire, and store-drain waits: those waits can
overlap. Normalize stage diagnostics per actual macroinstruction for comparison,
but do not call them CPI error or recoverable cycles.

If the optional paired mode is examined later, its closure limiter is the
minimum displacement over **ROB retire entries, valid SQ releases, commit,
and valid store-drain**. Completion and StoreSet-completion are translated but
do not directly choose that minimum. `ResponseFrontierRelease::settle` preserves
`actual = base + displacement` by adding to base and subtracting from
displacement. That bookkeeping transformation is not itself proof of charging
twice. Label any such run a different model configuration.

### 3. Pilot order and falsifiable questions

Freeze the binary/source hashes, full include-resolved config, native manifest,
oracle identity, warmup, participant set, and common start/end record markers.
Use the existing `first-core-target-common-end-v1` full measurement population
and `--measurement-scope user-plus-kernel`. Small witness windows are diagnostic;
they must not silently replace the full-run denominator or reset prior state.

Start with four cases; reuse these frozen references as context, not new data:

| Case | Role | FastSim CPI | gem5 CPI | Signed error | Absolute CPI error (cycles/macro-instruction) |
|---|---|---:|---:|---:|---:|
| LBM C32 | Primary overestimate | 4.200373 | 3.906000 | +7.5364% | 0.294373 |
| LBM C4 | Same workload, opposite error direction | 3.134823 | 3.231335 | -2.9868% | 0.096513 |
| TeaLeaf C32 | Underestimating control | 0.441014 | 0.501256 | -12.0183% | 0.060242 |
| NAMD C32 | Underestimating control | 0.482635 | 0.539028 | -10.4621% | 0.056394 |

All entries above come from unrounded values in the frozen summary. These are
mechanism pilots, not a workload-held-out generalization result.

| Order | Experiment | Required answer before proceeding |
|---|---|---|
| A | Charge ledger, diagnostics off/on, four pilots | Is the active sparse-tail charge fully accounted for, with unchanged simulated behavior? |
| B | Full-run owner aggregates, then bounded traces around the largest exposed tails; retain LBM C1/C13 as historical witnesses and include newly identified cores | Which predecessor chain explains LBM C32's excess span, and why does the same mechanism not justify relief on LBM C4? |
| C | Repeat fetch 4/8/16 and the prior widened-execution config on LBM C32, one change at a time; retain raw outputs and owner populations | Does the inversion follow base schedule, changed request ordering/service, inherited delay, or checkpoint boundaries? |
| D | Only if B/C implicate boundary carry: Q=512/1024/2048 with all other parameters fixed | Does the identified edge get reapplied at a boundary, or does Q change the modeled request stream? Q sensitivity alone is not proof of double charging. |
| E | One source-specific diagnostic intervention on the four pilots, followed by a semantically valid candidate if justified | Does changing that edge move the expected tail and full-run CPI without violating other timing edges? |

**Core-count contrast priority:** after A, extend B to LBM C8/C16 before
running C/D. The C16→C32 change is the primary comparison; C4 and C8 test the
opposite direction and non-monotonicity. See section 6 for the saved-data
decomposition and the hypotheses this comparison must distinguish.

For B, pair dynamic UOP/request identities with available gem5 stage records.
Compare interval lengths and ordering over the same functional region; keep
gem5 timing offline. If matching stage records do not exist, collect only the
required witness regions before asserting the serialization is excessive.
Include both large-charge regions and controls with similar instruction/miss
mix. Do not infer a full-run benefit from one improved C13 region.

For C/D, report `base schedule`, `accepted interval_extra`, other cycle terms,
and reconstructed total together. A smaller bucket accompanied by a larger
base schedule is redistribution, not acceleration. Hardware changes need a
matched gem5 configuration before they become formal accuracy/trend results.
Run C/D only after A/B pass; do not start the 40-case matrix for discovery.

### 4. Candidate decision and acceptance

The diagnostic outcome determines the candidate; **there is no selected relief
formula yet**:

- If a carried delay is demonstrably charged again, remove only the duplicate
  propagation using its dynamic owner identity; preserve the actual completion,
  capacity release, and ordering constraints.
- If the excess is introduced upstream at load-ready, SQ admission, store send,
  or serialized retirement, repair that edge. Subtracting cycles at the generic
  `memory_penalty` sink would conceal the source and can break state consistency.
- If spans are required by the modeled dependency/resource schedule, or no
  excessive span can be demonstrated, retain the baseline. A large bucket,
  high miss overlap, or CPI error sign is not sufficient evidence for relief.

An eventual candidate must be default-off and depend only on functional trace
and modeled state. No workload/PC-specific discounts, gem5 timing inputs, or
globally fitted multiplier on `interval_extra`. No automatic reopening of the
rejected projected-DRAM or full cross-Q continuation approaches; follow
[optimization decisions](optimization-decisions.md).

Acceptance proceeds in this order:

1. **Semantic and compatibility gates:** deterministic off reproduction;
   on/off instruction, request, and accepted-prefix conservation; valid
   dependency/visibility/SQ-release ordering; mechanism tests covering the
   identified edge, a negative control, and cross-checkpoint carry. For future
   code changes run `cmake --build build -- -j16` and `./build/fastsim_tests`.
2. **Four pilots:** lower LBM C32 absolute CPI error and 32-core unweighted CPI
   MAE, with no absolute-error increase on LBM C4, TeaLeaf C32, or NAMD C32.
   Keep signed errors to expose crossing zero. Reject local improvement that
   worsens full-run CPI or merely transfers cycles between accounting buckets.
3. **All 12 guardrail cases**, then the wider held-out/parameter matrix only
   after pilot and throughput screening. Report configuration-equal CPI MAE,
   MAPE/P50/P90/P99 and signed bias, plus per-case absolute errors. Keep per-core
   MAE distinct from configuration MAE and freeze selection before holdout runs.
4. **Throughput:** quiet-host sequential ABBA with diagnostics off, identical
   measured work, and retained individual timings; report mean, P50/P90/P99 and
   minimum for both user and user-plus-kernel runs. State warmup and end-to-end
   time separately. Require the project's minimum **5M user UOP/s** formal gate
   and no demonstrated throughput regression for this throughput-first repair.
   An already-below-gate baseline does not waive the absolute gate; shared-host
   or noisy measurements leave throughput acceptance pending.

The current `run_cpi_guardrail.py` checks **frozen signed error ±1pp**, so a
successful correction larger than 1pp also fails its compatibility band. Keep
that check unchanged for the off/default run. For candidate-on evaluation,
retain every reported band crossing and distinguish movement toward zero from
regression using absolute CPI error; do not silently rebaseline or claim the
legacy guardrail passed. The existing tool reads its config from the baseline
JSON, uses `build/fastsim`, deletes successful raw outputs, and its summary
does not retain all successful rows. A future candidate runner must explicitly
select the candidate config and save every result; the legacy command alone is
insufficient evidence for candidate acceptance.

### 5. Deliverables for the next execution session

Use a unique `tmp/lbm-interval-tail-attribution-20260915-<run>/` directory for
raw runs and witnesses. Keep `provenance.json` (hashes/config/boundaries),
per-run raw stats, `checkpoint-attribution.jsonl` (bounded samples),
`attribution-summary.json`, `paired-witnesses.csv`, and `ablation-summary.csv`.
Record rejected/unmatched samples and unresolved provenance, not only successes.
Every accuracy row needs FastSim/gem5 CPI, signed relative error, and
`abs(FastSim CPI - gem5 CPI)` in cycles per macroinstruction; aggregate rows
also need CPI MAE. Use `scope_metrics` for formal CPI and work; `totals` and
stage ledgers remain internal diagnostics.

The first execution session should deliver **A plus the B attribution report**:
the actual charge formula reproduced on the fast kernel, a conserved ledger,
ranked delay origins with uncertainty, and paired evidence for or against one
concrete excessive edge. Only that evidence selects E. This documentation
update changes no simulator/configuration behavior and launches no experiments.

### 6. Core-count contrast: prioritize why the tail grows from C16 to C32

The change in error direction is a useful diagnostic control, **not proof of a
core-count-specific bug**. C8 also overestimates; the signed residual is not
monotonic in core count. C16's small net error may conceal cancellation between
component errors, so it is a comparison case rather than presumed ground truth.

Recomputed from the same frozen 40-case summary, using unrounded, scope-matched
macroinstruction CPI values (`user-plus-kernel`):

| LBM cores | FastSim CPI | gem5 CPI | Signed error | Absolute CPI error (cycles/macro-instruction) |
|---|---:|---:|---:|---:|
| 4 | 3.134823 | 3.231335 | -2.9868% | 0.096513 |
| 8 | 3.261426 | 3.202265 | +1.8475% | 0.059161 |
| 16 | 3.510302 | 3.527754 | -0.4947% | 0.017452 |
| 32 | 4.200373 | 3.906000 | +7.5364% | 0.294373 |

The first useful observation is the **change from C16 to C32**:

```text
FastSim CPI increase = 4.2003733966 - 3.5103020809 = 0.6900713157
gem5 CPI increase    = 3.9060004148 - 3.5277541889 = 0.3782462259
signed residual increase                             = 0.3118250898
```

All differences are cycles/macro-instruction. The residual increase is a
cross-configuration diagnostic; it is not the C32 absolute CPI error and is
not a measured recoverable bound.

The saved FastSim accounting further narrows where to inspect:

| LBM cores | Memory-penalty bucket / measured macroinstructions | Remaining modeled cycles / measured macroinstructions |
|---|---:|---:|
| 4 | 2.849952 | 0.284871 |
| 8 | 2.978979 | 0.282448 |
| 16 | 3.230015 | 0.280287 |
| 32 | 3.919461 | 0.280913 |

Both columns are internal diagnostics in cycles/macro-instruction and sum to
FastSim CPI. "Remaining" means total modeled cycles minus that bucket; it is
not a pure compute/base-schedule measurement. From C16 to C32, the bucket grows
by **0.689445**, while the remainder grows by **0.000626**. Thus nearly all of
FastSim's CPI increase is in the bucket fed by the active retirement-tail
extension. This establishes a location for the scaling investigation, not the
delay's cause. gem5 also slows down, so removing the whole bucket increase
would erase real scaling cost as well.

Evidence and calculation inputs:
[saved-data contrast JSON](../tmp/lbm-core-count-contrast-20260915-8g88ko5d/summary.json),
with the source summary hash and per-case values. No simulator was run for
this contrast.

Use the following investigation order within B:

1. **Check what actually differs between core counts.** Verify per-core model,
   LLC capacity/banks, DRAM channels and address mapping, workload arguments,
   functional phase, warmup coverage, kernel-work fraction, and common-end
   populations. The four cases have different traces and measured work; equal
   benchmark names do not establish a controlled concurrency-only experiment.
   Do not change `--cores` on a C32 manifest and treat it as a matched C16 run.
2. **Compare all-core and phase distributions.** Apply the same live diagnostic
   to C4/C8/C16/C32. Record extra cycles per macroinstruction, checkpoint count
   per macroinstruction, accepted UOPs per checkpoint, extra per checkpoint,
   and newly introduced versus inherited delay. Pair each case with its own
   gem5 oracle; compare dynamic roles/phases across cases instead of assuming
   the same core index has the same role. Separate a widespread shift from
   a small set of cores or phases dominating the instruction-weighted result.
3. **Distinguish competing explanations with paired witnesses.** If the
   FastSim-vs-gem5 request-service gap grows at C32 before the core tail does,
   investigate shared-service admission/queueing and store/SQ pressure. If
   service timing is reasonably matched but response-to-retire span grows too
   much, investigate core overlap and inherited-delay propagation. If stages
   have large opposite errors already at C16, investigate cancellation rather
   than treating near-zero total error as correct component timing. Each is
   a hypothesis until matched event spans support it.
4. **Intervene only at the discriminating edge.** Preserve request identity and
   ordering constraints for a bounded replay/ablation, then confirm the effect
   on full-run timing. Change one service or carry mechanism at a time. Repeat
   on both C16 and C32 and retain C4/C8 controls. Replacing physical timing with
   gem5 timestamps is not a deployable candidate, and removing participants
   changes the experiment's meaning. The same local rule must explain all four
   directions without a `cores == 32` discount.

The next attribution report must therefore answer: **which modeled delay grows
too fast from C16 to C32, and does that growth originate in shared service or
in propagation from service to retirement?** If the existing samples cannot
separate those explanations, collect matching stage witnesses before choosing
a correction. Core count is the experimental axis, not the repair condition.

## 2026-09-15 execution: active tail ledger and SQ admission localization

### Scope, implementation, and validation

Executed the first localization slice on the maintained materialized fast
kernel, with `core.response_interval_tail_audit=true` as the only diagnostic
override. The flag defaults to false and rejects configurations without that
kernel or with paired-frontier/event-only alternatives. It does not turn on
`sim.cpi_attribution` or change scheduling constraints.

The four cases are the LBM C4/C8/C16/C32 manifests under
`tmp/first-core-common-end-20260911/source/`, using
`configs/gem5-fs-native-kernel.cfg`, `user-plus-kernel`, and the existing
`first-core-target-common-end-v1` boundaries. These are investigation cases,
not a held-out accuracy claim. All four restored-target identity audits passed.
Per-core model, LLC capacity/banks, and DRAM channel count are unchanged across
the four configurations; their functional traces and measured populations differ.
Functional warmup remains enabled: total warmup records are 410,897 / 698,020 /
1,212,164 / 2,250,706 respectively. The detailed per-core boundaries, metadata
hashes, measured counts and common-end identities are retained in the run bundle.

The implementation is in [the live kernel](../src/simulator.cpp),
[ledger types](../include/fastsim/interval_tail_audit.hpp), and
[JSON output](../src/main.cpp). Each trial holds its own counters; only accepted
timing feedback is merged. Warmup measurement reset clears the accumulated
ledger. The inactive-segment certificate is represented as certified-zero work;
none of the four LBM runs took that shortcut. Each core retains its eight
largest charged checkpoints, with sequence boundaries, old interval gap, tail
floor, entry/final retirement clocks, and one largest completion-advance witness.
`witness_valid=false` means that no positive completion witness was selected.

Validation completed:

- Four full original-binary baseline runs, eight initial diagnostic off/on
  runs, then eight final refined-diagnostic off/on runs. All successful runs
  reproduce the original `scope_metrics` excluding host throughput, all thread
  and core stats, totals, cache/coherence stats, and pending-fill output exactly.
  One initial config-include typo was rejected before simulation and retained
  in the run log; the corrected run completed.
- At every accepted checkpoint, the ledger conserves its charged tail. All
  four full-run ledger sums equal `exposed_memory_penalty_cycles`; accepted
  UOP counts equal both accepted-interval and materialized-kernel UOP counts.
- `cmake --build build -- -j16`, `./build/fastsim_tests`, and
  `python3 tests/test_interval_tail_audit.py` passed. The new test exercises
  nonzero charge, all nested conservation identities, sample validity, and
  diagnostic on/off timing parity. Independent read-only review found no
  blocking correctness defect.

No timing-relief candidate was enabled, and no commit or push was made. Host
wall times were retained, but these runs are not a quiet-host throughput gate.
Full accepted-request identity digests and predecessor chains are not emitted
yet; matching work/request counts and cache outcomes do not replace that gate.

### Fresh CPI reproduction and the per-core cancellation control

Formal CPI uses measured macroinstructions. gem5's matching active-cycle
numerator excludes `idle_cycles`; raw `measured_cycles` includes them in C4
and C32 and must not be substituted for `perf_like_cpi_user_plus_kernel`.

| LBM cores | FastSim CPI | gem5 CPI | Signed error | Absolute CPI error (cycles/macro-instruction) | Per-core unweighted CPI MAE | Cores with positive error |
|---|---:|---:|---:|---:|---:|---:|
| 4 | 3.134823 | 3.231335 | -2.9868% | 0.096513 | 0.092310 | 0 / 4 |
| 8 | 3.261426 | 3.202265 | +1.8475% | 0.059161 | 0.070196 | 8 / 8 |
| 16 | 3.510302 | 3.527754 | -0.4947% | 0.017452 | 0.107102 | 1 / 16 |
| 32 | 4.200373 | 3.906000 | +7.5364% | 0.294373 | 0.321020 | 32 / 32 |

Across these four investigation configurations, equal-weight CPI MAE is
**0.116875**, MAPE **3.21635%**, and signed relative bias **+1.47561%**. These
configuration statistics differ from the per-core MAE column.

C16 core 0 has FastSim/gem5 CPI 10.616686 / 9.548921, signed error +11.1821%,
absolute CPI error **1.067766**; the other 15 cores all underestimate. Thus
C16 is a cancellation control, not a validated model of component timing.
C32 core 0 also overestimates, but core 1–31 contribute **93.35%** of the
configuration's signed cycle residual. The C32 effect is widespread rather
than explained by one unusual core. Different core indices across cases are
not assumed to represent equivalent dynamic phases.

### Conserved charge: most growth reaches retirement through SQ admission

Let `B` be the last lower-bound retirement plus the old interval gap, and
`R` the final ordered retirement. The charged tail is `max(0, R-B)`.
The diagnostic partitions advances of the retirement clock **above B**, rather
than adding overlapping per-UOP waits:

```text
tail = entry carry + base advance + completion advance + commit-width advance
completion advance = pre-issue span + post-issue span
```

For the second identity, issue is projected onto the retirement clock with
`execute_to_commit`, clipped to exposed completion, then clipped above both
the preceding retirement clock and B. Consequently these are disjoint tail
spans, not whole instruction latencies or independent causal costs.
Pre-issue spans are further classified by the UOP's issue gate and, for a
dispatch-gated issue, the local strict-max dispatch winner.

All values below are internal diagnostic cycles per measured macroinstruction.
The SQ column is a **subset of pre-issue**, not an additional summand.

| LBM cores | Charged tail | Pre-issue | Of which SQ admission | Post-issue | Commit width |
|---|---:|---:|---:|---:|---:|
| 4 | 2.849952 | 1.896439 | 1.895798 | 0.845765 | 0.107748 |
| 8 | 2.978979 | 2.009162 | 2.008725 | 0.860111 | 0.109705 |
| 16 | 3.230015 | 2.194114 | 2.193836 | 0.925715 | 0.110187 |
| 32 | 3.919461 | 2.690874 | 2.690427 | 1.115967 | 0.112620 |

Entry carry above B and base advances above B are both zero in these runs.
This does **not** rule out inherited delays: old register, dispatch, SQ or
store-drain state can move a later completion beyond B. Likewise a zero ROB/IQ
entry in the final classified pre-issue spans does not prove those resources
never constrain any UOP. Gate ties follow evaluation order; `carry` includes
earlier UOPs within or across checkpoints, and `width` labels the last advance,
not necessarily the origin of all inherited delay.

C16→C32 tail growth is **0.689445** cycles/macro-instruction. Its SQ-classified
pre-issue part grows by **0.496592**, or **72.03%** of that growth; post-issue
grows by **0.190252**, and commit width by **0.002433**. The full C32 tail
contains **646,035,610 SQ-classified cycles**, or **68.64%** of its 941,155,750
charged cycles. These are accounting spans, not the C32 absolute error and
not recoverable-cycle estimates.

| LBM cores | Accepted checkpoints | Checkpoints / million macroinstructions | Accepted UOPs / checkpoint | Tail cycles / checkpoint |
|---|---:|---:|---:|---:|
| 4 | 13,103 | 516.24 | 2,496.37 | 5,520.63 |
| 8 | 28,712 | 520.43 | 2,487.80 | 5,724.08 |
| 16 | 60,341 | 534.33 | 2,426.02 | 6,045.00 |
| 32 | 124,448 | 518.27 | 2,506.42 | 7,562.64 |

Normalized checkpoint frequency falls slightly from C16 to C32 while charge
per checkpoint increases. Thus a simple increase in the number of settlements
does not explain the scaling. The earlier load/store endpoint split also
conserves: store endpoints account for 2.224157 / 2.719251 at C16/C32, but
that label alone would have conflated late issue with store completion service.

### Paired checkpoints: excessive spans exist, but SQ root ownership is unresolved

Reused the identity-gated C32 gem5 stage archive in
`tmp/lbm-common-end-audit-20260912.YqJDXj/`. This is historical stage evidence,
not a newly collected gem5 run. For all **256** current live samples, freshly
checked the witness sequence/PC against current FST and both archived stage
streams, and matched the current fast kernel's issue/completion and checkpoint
end-retirement timestamps to the archived FastSim timestamps. The archive's
full functional identity report and current common-end boundaries are retained.
Full RAW companion identity is still unavailable.

Of these samples, **225** have valid predecessor-to-end retirement anchors;
**31** first-measurement checkpoints were rejected from local span comparison
because the archived gem5 stream lacks their pre-window retirement anchor.
All rejected samples are retained. **221 / 225** matched checkpoint spans are
longer in FastSim; four are shorter. The selected-checkpoint unweighted CPI
MAE is **7.873482 cycles/macro-instruction**. This is a deliberately biased
largest-tail sample, not whole-run MAE or a representative phase estimate.

Two retained examples illustrate why tail size alone cannot select a repair.
Window CPI here means predecessor-to-end retirement span divided by the
completed macroinstructions in that interval; it is a local span diagnostic,
not the formal active-cycle CPI of an independently replayed window.

| C32 core; sequence interval [begin, end) | FastSim window CPI | gem5 window CPI | Signed span error | Absolute window CPI error (cycles/macro-instruction) |
|---|---:|---:|---:|---:|
| 13; [3,122,784, 3,126,150) | 24.811392 | 11.646658 | +113.0344% | 13.164734 |
| 1; [8,479,096, 8,483,017) | 9.739706 | 10.624412 | -8.3271% | 0.884706 |

In the first example, the largest completion-advance witness is sequence
3,124,494, with dispatch/SQ as the selected gate. Its **issue-to-retire** span
is 1 FastSim cycle versus 3 gem5 cycles, despite the enclosing checkpoint being
38,599 cycles longer. This witness points upstream of that store's issue;
it does not support subtracting its post-issue latency. It does not prove the
whole checkpoint error comes from the same owner.

The gem5 archive has no completion timestamps in its completion column, so
these pairs cannot separate native service-to-completion from retirement
propagation. An equivalent retained C16 stage archive was not established in
this execution. A complete paired service/owner test therefore remains open.

### Next discriminating experiment and repair status

The next edge to follow is now concrete:

```text
older store send/service -> SQ release -> younger store dispatch/issue
                        -> ordered retirement -> accepted interval tail
```

The maintained path currently has `response_shared_service_constraints=false`
and `response_pending_fill_store_commit=false`. For regular stores, inspect
the active `store_send` / `store_latency_cycles` / `sq_release` calculation,
then the SQ admission backward delay; do not analyze an inactive shared-service
alternative as if it generated this ledger.

1. Capture accepted-only predecessor owner IDs for SQ release and TSO drain,
   plus the request identity/digest, projected service origin, actual store
   send, response, release, younger dispatch/issue, and current interval gap.
   Split waiting for the earlier store from its own service span. Retain ties
   and cross-checkpoint owners explicitly.
2. Pair those owners on positive and negative C32 windows, including the two
   examples above. Establish matching C16 store phases and native stage/service
   observations rather than pairing the same numeric sequence across traces.
3. Test whether the overlong span first appears in store service, TSO/send
   ordering, or reuse/propagation of an already-carried delay. Only then choose
   an intervention at that edge. SQ is also dominant at C4, which underestimates;
   a blanket SQ discount or a `cores == 32` condition is not justified.

**Status: the dominant observed tail path is localized to SQ admission, but
the excessive component's root cause is not yet established. No timing fix
has been implemented or accepted.** This completes the live conserved ledger
and a bounded paired-checkpoint test; it does not complete the plan's full
paired predecessor/service attribution or candidate acceptance gates.

Run bundle:
[`tmp/lbm-interval-tail-attribution-20260915-tv0dw80j/`](../tmp/lbm-interval-tail-attribution-20260915-tv0dw80j/).
Key artifacts are `provenance.json`, `refined-provenance.json`,
`oracle-identity.json`, `measurement-provenance.json`, `refined-runs.json`,
`attribution-summary.json`, `per-core-attribution.json`,
`checkpoint-attribution.jsonl`, `paired-checkpoint-summary.json`,
`paired-witnesses.csv`, and `ablation-summary.csv`. The latter records diagnostic
parity/accuracy, not a timing-relief ablation. Raw outputs, scripts, failed
preflight log, and build/test logs are preserved in the same bundle.

## 2026-09-15 SQ-owner execution: local excess is inside DRAM store service

### What was captured on the active kernel

The existing `response_frontier_audit` can now run on the materialized kernel
when `response_interval_tail_audit=true`, while `cpi_attribution` stays false.
Raw sequence/PC, SQ owner, timing and memory-event fields remain available;
`cycle_causes_available=0` explicitly marks the unavailable scalar CPI-cause
labels. The refined `tail_dispatch_gate` is populated by the materialized
ledger; existing scalar CPI audits emit `null` for that new field.

Added accepted-only store send, service and SQ-release timestamps, the prior
TSO store's sequence/release, and an identity-only digest of each sampled
UOP's ordered memory audit events. The digest excludes modeled timing/path
fields and is distinct from the existing timing digest. SQ and TSO owner
metadata follow trial replacement, accepted feedback, and warmup-retained
state; they never supply a cycle to the scheduler.

Eight full common-end runs completed: default C4/C8/C16/C32, two C32 dense
owner captures, and two C16 captures. All reproduce the previous run's formal
scope metrics, threads, totals and cache/coherence results exactly. The four
dense runs also reproduce the earlier conserved tail ledger exactly. One
initial config with an out-of-range core filter was rejected before simulation;
the corrected run and rejected log are retained.

The C32 scored checkpoints were frozen in the preceding execution. Wider
capture intervals supply context/parents; they do not redefine those score
windows. The C16 captures are fractional-progress controls, not dynamic matches
to another core count:

| Capture | Core | Captured sequences (inclusive) | Scored checkpoint [begin, end) |
|---|---:|---|---|
| C32 positive | 13 | 3,121,500–3,127,000 | [3,122,784, 3,126,150) |
| C32 negative | 1 | 8,478,000–8,484,000 | [8,479,096, 8,483,017) |
| C16 early control | 13 | 3,022,988–3,028,988 | No paired native score |
| C16 late control | 1 | 8,651,490–8,657,490 | No paired native score |

Across the four captures there are **23,504 accepted UOP samples and 3,301
ordinary stores**. The current FST PC/sequence and archived FastSim/gem5 stages
were freshly checked for every captured C32 UOP. Native store instruction IDs
were joined through the archived memory-ID map and full native store ledger;
only single-admission/response stores completing before common end enter the
service comparison. All 631 / 721 ordinary stores in the two central C32
checkpoints met that gate. Archived full functional identity is retained;
this execution does not re-establish full RAW companion identity or a
whole-run request digest.

### Direct SQ and TSO propagation does not explain the positive witness

All 3,301 captured ordinary stores satisfy the active default equations:

```text
store_send = max(actual_retire, previous_TSO_store_release)
SQ_release = store_send + store_service_cycles
```

The default helpers add zero commit-to-send and response-to-next-send offset
in this configuration. The captured owner links include **12 cross-checkpoint
TSO links**. No additional `interval_gap` is added at this direct send edge.
This checks the observed recurrence; it does not rule out upstream inherited
errors already present in a retire or service input.

The `regular_stores` and `tso_owner_links` summary counts cover the wider
captures. Each capture has one initial TSO predecessor outside the capture:
its owner metadata is available, but its raw parent row cannot be rejoined
there. All TSO and selected SQ parents for the two central scored checkpoints
are present in their captures. The JSON reports both coverage levels explicitly.

For SQ-selected younger stores, 180 / 215 predecessor-response pairs are
available in the positive/negative checkpoints, including 9 / 11
cross-checkpoint SQ links. FastSim issue minus that predecessor's SQ release
is **exactly 3 cycles for every pair**. gem5 issue minus the *same dynamic
store's* native response has median **4 cycles** in both windows (means
7.94 / 9.94, minima 1 / 1). No paired gem5 issue precedes that response.

Thus the selected positive error is not explained by an overlong modeled
3-cycle release-to-issue handoff. Pairing the same predecessor does not by
itself prove gem5 selected precisely the same SQ blocker; dependency and
other admission constraints can also affect gem5 issue.

### Store-service spans provide the next localization

For the same ordered, non-overlapping set of paired ordinary stores, the
span from first store send to last response has an exact decomposition:
`span = sum(service) + sum(inter-store gaps)`. These endpoints differ from
the checkpoint's retirement endpoints. The following is a service-chain
diagnostic in cycles, **not an additive decomposition of CPI error**.

| Scored C32 checkpoint | FastSim chain span | gem5 chain span | Span difference | Service difference | Inter-store gap difference |
|---|---:|---:|---:|---:|---:|
| Positive, core 13 | 71,934 | 33,870 | +38,064 | +35,586 | +2,478 |
| Negative, core 1 | 30,976 | 34,396 | -3,420 | -2,747 | -673 |

The positive chain's excess is **93.49% service**, with 35,074 of its 35,586
service-excess cycles on the 105 stores whose native outcome and modeled path
both reach DRAM. The 526 non-DRAM/path-L1 stores add only 512 service cycles
in total. This is much narrower evidence than naming every SQ-gated cycle
as erroneous.

| Scored checkpoint; paired DRAM stores | Requests | FastSim mean service | gem5 mean service | Signed mean difference | Service MAE (cycles/request) |
|---|---:|---:|---:|---:|---:|
| Positive, core 13 | 105 | 640.181 | 306.143 | +334.038 | 361.657 |
| Negative, core 1 | 113 | 257.540 | 287.071 | -29.531 | 88.381 |

Native service starts at Ruby admission; modeled latency includes its modeled
handoff/transport. That boundary difference is retained, not fitted away.
For non-DRAM/path-L1 stores the mean difference is approximately +0.97 cycle
in both windows, far smaller than the positive DRAM difference.

The matched retirement-window results remain:

| Scored C32 checkpoint | FastSim window CPI | gem5 window CPI | Signed error | Absolute CPI error (cycles/macro-instruction) |
|---|---:|---:|---:|---:|
| Positive, core 13 | 24.811392 | 11.646658 | +113.0344% | 13.164734 |
| Negative, core 1 | 9.739706 | 10.624412 | -8.3271% | 0.884706 |

Equal-weight CPI MAE of these two deliberately selected windows is **7.024720**;
it is not a configuration or whole-run accuracy statistic. Their cycle
differences are +38,599 / -3,008, distinct from the service-chain differences.
The wider *capture context* around the negative checkpoint actually has
FastSim/gem5 window CPI 10.041029 / 9.432666, signed error +6.4495%, absolute
CPI error **0.608363**. This sign reversal is preserved in `expanded-context/`:
changing a window boundary can change the conclusion. The central checkpoints
were not reselected after observing these service results.

### The distinguishing modeled component is command/bank waiting

For those same paired DRAM stores, the modeled descriptor separates arrival
to bank-command readiness, additional wait until actual command selection,
and the remainder of service:

| Scored checkpoint | Arrival→bank-ready mean | Bank-ready→command mean | Total arrival→command mean | Service minus arrival→command mean |
|---|---:|---:|---:|---:|
| Positive, core 13 | 443.905 | 35.067 | 478.971 | 161.210 |
| Negative, core 1 | 49.398 | 46.938 | 96.336 | 161.204 |

All entries are modeled cycles/request. Almost the entire difference between
the modeled DRAM service means is in command waiting, especially the earlier
bank-ready frontier. This identifies the next modeled component to inspect;
the native ledger has no matched DRAM-command timestamps, so it does not
establish which particular bank reservation is wrong.

The final command-blocker labels also require care. Positive requests have
39 bank / 59 channel / 7 intrinsic winners; negative requests have 5 bank /
75 channel / 33 intrinsic winners. Only 17 / 38 final blockers are directly
tagged as writeback owners. A final channel winner can hide a large earlier
bank wait. Neither a last-winner label nor writeback count explains the full
span; bank-predecessor provenance is the next missing link.

All 105 / 113 selected DRAM stores are sent by the core after their projected
shared response timestamps (mean leads 34,979.82 / 16,646.69 cycles). This
confirms late reuse of a projected service duration in both windows. Since
one window overestimates and the other underestimates, this fact alone is not
a sufficient relief condition and does not authorize treating a projected
response as an already completed physical store.

The C16 early/late captures contain 54 / 55 modeled path-DRAM stores, with
mean service 229.91 / 264.93 cycles. These are FastSim-only controls and their
store/phase populations differ from C32. Without matched C16 native response
timings, they cannot prove that DRAM service error explains the entire
C16→C32 error-direction contrast.

### Status and the next discriminating test

**The positive checkpoint has a demonstrated local store-service overestimate,
dominated by DRAM service waiting. The direct SQ/TSO handoff is not its main
excess. The full C32 absolute error has not yet been causally apportioned, and
there is no accepted timing fix.**

Follow the bank-ready predecessor reservation, including its request ID,
read/write direction, original/projected arrival, channel/bank calendar update,
and inherited backlog. Separate this from the later channel winner. On the
frozen request stream, test late-discovery clamping and mixed RD/WB selection
separately, retaining both central windows and their surrounding phases.
Establish equivalent native service evidence at C16 before claiming a
core-count explanation. Follow the existing
[optimization decisions](optimization-decisions.md): the previously rejected
projected-DRAM feedback and full continuation routes remain default-off;
their earlier hotspot benefit is not a successful end-to-end repair.

Build, full FastSim tests, the extended owner/compatibility regression test,
and whitespace checks passed. Independent review covered owner state,
accepted-only publication and default timing neutrality. The legacy scalar
dispatch-field issue found in review has a regression test and was corrected.
The four default configurations retain CPI MAE **0.116875** and the previously
reported per-case errors; C32 remains +7.5364%, absolute CPI error **0.294373**.
No throughput acceptance claim, timing-relief change, commit or push is made.

Artifacts and reproduction scripts:
[`tmp/lbm-sq-owner-attribution-20260915-4hs089yz/`](../tmp/lbm-sq-owner-attribution-20260915-4hs089yz/).
`runs.json` retains commands/results; `owner-attribution-summary.json` and the
`*-services.csv`, `*-sq-links.csv`, `*-tso-links.csv` files retain the paired
evidence and unresolved boundaries. The original binary/source snapshot,
failed preflight/build attempts, final build/tests, and expanded-context
comparison are preserved separately.
`formal-accuracy.json` / `.csv` retain the unchanged four-configuration CPI
results with absolute errors and CPI MAE; `input-provenance.json` and
`final-verification.json` retain the input, config, source and binary hashes.
`finalize.py` rechecks parity and regenerates those verification records.

## 2026-09-15 bank provenance: projected overlap survives serialized store send

### Scope and identity gate

This is a diagnostic execution on the same full C32 common-end input and
functional warmup. It does not change maintained simulator source, build
products, configuration defaults, or any timing rule. An isolated copy of
`src/simulator.cpp` extends the existing accepted projected-request capture
with the bank's pre-update calendars and the previous request's identity,
arrival, command, and write-drain trigger. The extra bank fields follow the
existing DRAM state copies, transaction rollback and channel installation.

The isolated full C32 replay reproduces baseline scope metrics (excluding
host throughput), threads, cores, totals and cache/coherence results exactly.
All **7,141,740** original request rows, including their order and first ten
CSV columns, match the earlier frozen capture: 44,348 warmup reads,
4,020,282 measured reads and 3,077,110 measured writeback enqueues. The new
schema is local to the diagnostic binary; existing replay tools must not be
given this extended CSV without an explicit adapter.

All **105 positive / 113 negative** central-window DRAM store events match
the preceding accepted core audit in core/sequence/ordinal, physical line,
arrival, bank-ready, command and response. Their consumed store service
duration includes the captured canonical DRAM service plus 48--50 transport
cycles. Thus the captured waiting is actually present in the consumed service;
it is not merely an unused timing descriptor.

### Bank waiting has two separate sources

Re-evaluating the unchanged timing equations from their captured inputs gives
the following exact additive decomposition of modeled arrival-to-command
time. The intrinsic term uses the *modeled* row state; it is not a native
row-hit classification.

| Modeled cycles/request | Positive checkpoint, 105 stores | Negative checkpoint, 113 stores |
|---|---:|---:|
| Intrinsic row access / PRE+ACT | 63.886 | 35.009 |
| Existing target-bank busy / precharge floor | 328.076 | 12.761 |
| Additional cross-bank ACT floor | 51.943 | 1.628 |
| Later channel-command floor | 35.067 | 46.938 |
| Total arrival-to-command | 478.971 | 96.336 |

All nonzero additional ACT floors (26 / 4 requests) come from
`activate_ready_before`, not the activation-window limiter. In this profile,
`t_rrd=t_rrd_l=t_xaw=activation_limit=0`, but the code still propagates each
selected future ACT timestamp to every bank in the rank. The target bank's
last-access owner is **not** the owner of that cross-bank update. Its
5,454 / 184 summed cycles remain owner-unresolved and must not be presented
as verified tRRD/tFAW contention or attributed to the direct bank parent.

### Same-core store parents expose the local inconsistency

For the target-bank busy term, the direct parent breakdown is:

| Direct bank predecessor | Positive requests | Positive summed backlog | Negative requests | Negative summed backlog |
|---|---:|---:|---:|---:|
| Earlier ordinary store on the same core | 60 | 24,056 | 67 | 785 |
| RD from another core | 21 | 3,487 | 21 | 0 |
| Dirty writeback | 24 | 6,905 | 25 | 657 |
| Total | 105 | 34,448 | 113 | 1,442 |

Counts include zero-wait parents. Of the same-core pairs, **54 / 12** actually
have nonzero backlog. Every one of these parents is a matched ordinary store;
the parent event's ordinal, line, projected arrival and command were rejoined
to its earlier accepted core sample. For all 60 / 67 pairs, both hold:

```text
FastSim parent SQ release <= FastSim child store send
native parent Ruby response < native child Ruby admission
```

Native response-to-next-selected-admission gaps are at least 1 / 1,035 cycles
(means 4,282.68 / 7,371.97). In the positive window, the modeled bank busy time
is nevertheless carried from those same parent requests into the child's
service duration. For every same-core pair, the captured busy floor is exactly
`parent_projected_command + burst_cycles`; a later precharge floor does not
explain this attribution.

The input-time compression is substantial. The 105 positive DRAM store
arrivals span only **706 projected cycles**, while their native Ruby admission
times span **33,553 cycles** and their final FastSim sends span **71,261 cycles**.
For the 113 negative requests the corresponding spans are 787 / 34,002 /
30,570 cycles. Native Ruby admission and modeled DRAM arrival are different
hierarchy boundaries; these spans illustrate time compression, not an
exact per-request arrival error. Compression exists in both windows; the
bank/row workload and accumulated reservation state determine its consequence.

No selected direct bank parent arrives later than its child in the projected
stream, and no selected WB parent arrives after its recorded drain trigger.
The immediate-parent hypothesis of a future-arriving WB jumping ahead is
therefore unsupported here. This does not rule out earlier ancestors or the
late-clamping behavior of the previously rejected alternative controller.

### A concrete pair, including the channel constraint

On core 13, child store **3,124,755** has bank parent **3,124,313** and channel
parent **3,124,751**. Both are earlier ordinary stores on the same core.

| Event on the modeled projection clock | Cycle |
|---|---:|
| Bank parent projected arrival | 10,476,718 |
| Child projected arrival | 10,476,791 |
| Bank parent projected command | 10,477,986 |
| Bank becomes available | 10,477,996 |
| Channel parent projected command | 10,478,016 |
| Child command after channel spacing | 10,478,026 |

The child is a modeled row hit, so its bank busy term is
`10,477,996 - 10,476,791 = 1,205` cycles. Its total FastSim store service is
**1,396 cycles**, versus **189 native cycles**, a signed service difference of
**+1,207 cycles/request**. These are service metrics, not macroinstruction CPI.

On the final core clock, the bank parent has already completed **8,952 cycles**
before the child sends; on the native clock it has completed **3,013 cycles**
before the child is admitted. The channel parent is also complete: its final
FastSim response equals the child send, and its native response precedes child
admission by one cycle. Neither parent is still an outstanding transaction
on those respective send/admission timelines.

This example also explains why a patch to only `bank.ready` is insufficient:
holding all other recorded inputs fixed, the channel floor still sets the
child command to 10,478,026. The same projected history has propagated into
multiple calendars. A valid intervention must recompute the affected ordering
and reservations consistently, rather than subtracting the direct bank term.

### Mechanism and repair boundary

The newly supported local mechanism is **reuse of a service duration whose
queue competition was computed under overlapping projected store arrivals,
after core retirement and TSO have serialized those stores**. The active code
loads `feedback.latency_cycles` into `store_latency_cycles`, then computes
`SQ_release = store_send + store_latency_cycles`; the default does not
re-evaluate shared service at that final send. The observed sends satisfy the
configured TSO max recurrence, while the inherited queue-duration input can
describe concurrency that no longer exists.

This is evidence against a portable scalar latency assumption, not against
the use of integers to represent time. A faster tCL constant, an SQ discount,
or a core-count-specific factor does not restore request ordering. It also
does not establish that every same-core parent term can be removed: moving
arrivals changes other-core interference, row state, WB selection, cache fill
visibility, and downstream bottlenecks. **24,056 is a request-wait attribution
sum, not an additive or recoverable component of the C32 CPI error.**

The next intervention should preserve the same-core store non-overlap
constraint when constructing the shared-service schedule and update its bank
and channel reservations together, with consistent response/fill ownership.
First test a bounded fixed-request diagnostic around both frozen windows and
their context; retain request identities and population. Treat that as a
mechanism test until core/cache feedback and full-window accuracy are rerun.
Resolve the separate ACT-floor owner before attributing its residual. Do not
re-enable the rejected projected controller, import native timing into
inference, or interpret this result as a C16--C32 scaling proof.

The full C32 result is unchanged:

| Configuration | FastSim CPI | gem5 CPI | Signed error | Absolute CPI error (cycles/macro-instruction) |
|---|---:|---:|---:|---:|
| C32 diagnostic capture | 4.200373 | 3.906000 | +7.5364% | 0.294373 |

No timing fix, throughput acceptance, commit or push is claimed. The isolated
full test suite passed after changing its copied test's exact CSV-header
expectation to the diagnostic schema; the original header assertion rejection
and a missing compile-definition retry are retained. Maintained tests and
source were not edited. Independent review checked state neutrality, dynamic
parent matching and the distinction between direct bank and ACT ownership.

Artifacts:
[`tmp/lbm-bank-provenance-20260915-hfk_903m/`](../tmp/lbm-bank-provenance-20260915-hfk_903m/).
`instrument.py`, `diagnostic-source.diff`, `run.py`, `run.json` and
`bank-requests.csv` reproduce the capture. `analyze.py`, `bank-summary.json`,
`selected-bank-owners.csv` and `context-bank-owners.csv` retain the decomposition;
`pair_parents.py`, `paired-parent-summary.json`, `paired-same-core-parents.csv`
and `example-bank-channel-parents.json` retain the native/model parent evidence.
`accuracy.json` / `.csv` and `final-verification.json` retain the unchanged CPI
result, input/source/binary hashes and test status; `finalize.py` regenerates
those checks.

## 2026-09-15 repair experiment: canonical store-response projection rejected

The user authorized limited interface/state changes and required a fallback.
This execution implements and validates one concrete mechanism candidate. It
**does not deliver a usable LBM accuracy fix**. The independent review found no
blocking implementation error in its stated projection contract; the contract
itself remains an approximation whose full-run result fails the promotion gate.

### Scope, provenance, and implementation

This is a **combined-scope repair pilot on formal common-end inputs**, not a
held-out/generalization report. Data are
`tmp/first-core-common-end-20260911/source/formal-{04,08,16,32}c-782.lbm_r/`:
one workload, four configurations, 60 per-core traces, ten complete runs
(four off, four on, C16/C32 canonical controls). No input cases were rejected.
The prior 4/4 oracle identity gate is reused after rechecking manifest, trace
metadata, oracle, native config and event-dictionary hashes. It does not assert
new native collection or new full raw-trace pairing. No native timing enters the
candidate. This pilot does not add a PMU-accuracy or user-only comparison.

Functional warmup remains record bounded and excluded from measurement; caches,
DRAM and the new absolute response frontier survive the measurement barrier,
while counters reset. Native user+kernel CPI uses `scope_metrics.sum_core_cycles`
/ `scope_metrics.perf_like_cpi_denominator_instructions`; gem5 uses measured
minus idle cycles over the same completed macroinstruction population. The
`first-core-target-common-end-v1` windows and per-thread instruction/UOP
populations are unchanged. Each off run exactly matches frozen scope, thread,
cache and timing results.

The isolated switch is `core.store_response_frontier = false`, with CLI override
`--store-response-frontier true|false`. Its implementation:

- retains five small fields per core (last admission, owner sequence, group
  admission, maximum response, validity), plus a global diagnostic maximum;
- merges per-core memory heads by projected admission; a distinct ordinary store
  waits for the previous store response and already available retirement/carry
  lower bounds; same-UOP fragments share the floor and aggregate maximum response;
- calls canonical private/shared replay at that admission, updating functional
  cache effects, MSHRs, bank and channel calendars together; returned service
  starts at the shifted admission, excluding projected send waiting;
- preserves same-core memory order, which can also shift younger load/ifetch
  admissions; their core issue times are **not** thereby closed against the
  shared projection;
- disables private preview and subsequent timing-only FR-FCFS repair, which could
  invalidate the parent responses used to release children; rejects other
  corrected-service experiments, non-reference frequencies/DVFS, non-TSO and
  non-materialized paths. The old `store_post_commit_request` horizon guard and
  rejected `dram.projected_feedback` implementation are unchanged.

The candidate knowingly permits admissions beyond the current horizon. It is
therefore a directional test of a two-stage projection, not a certified
actual-arrival scheduler. Canonical state is consistent with requests already
processed by this projection, but later epochs may reveal earlier requests.

### Full-run accuracy gate

All CPI and absolute errors below are cycles per completed macroinstruction.
Relative error is signed `(FastSim - gem5) / gem5`.

| Cores | gem5 CPI | Off CPI | Off error % | Off abs CPI error | On CPI | On error % | On abs CPI error |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 3.231335 | 3.134823 | -2.9868 | 0.096513 | 9.155274 | +183.3279 | 5.923938 |
| 8 | 3.202265 | 3.261426 | +1.8475 | 0.059161 | 19.304942 | +502.8527 | 16.102677 |
| 16 | 3.527754 | 3.510302 | -0.4947 | 0.017452 | 42.095159 | +1093.2566 | 38.567404 |
| 32 | 3.906000 | 4.200373 | +7.5364 | 0.294373 | 88.148109 | +2156.7358 | 84.242108 |

All four cases worsen. This cannot be accepted as a correction for C32's
opposite-sign behavior. The four-configuration distributions are:

| Mode | CPI MAE | MAPE % | P50 APE % | P90 APE % | P99 APE % | Max APE % | Macro-weighted WAPE % | Macro-weighted signed bias % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| off | 0.116875 | 3.2163 | 2.4171 | 6.1715 | 7.3999 | 7.5364 | 4.9135 | +4.3592 |
| on | 36.209032 | 984.0432 | 798.0546 | 1837.6920 | 2124.8314 | 2156.7358 | 1606.4417 | +1606.4417 |

The canonical controls disable private preview and set FR-FCFS selection to one
with topology scaling enabled. They retain canonical RD/WB/cache replay and
execute no timing-only controller reorder, matching that part of the candidate.
This separates a controller-path confound from the much larger projection failure.

| Cores | gem5 CPI | Canonical off CPI | Signed error % | Abs CPI error |
|---:|---:|---:|---:|---:|
| 16 | 3.527754 | 3.562715 | +0.9910 | 0.034961 |
| 32 | 3.906000 | 4.263652 | +9.1565 | 0.357652 |

The existing topology rule already limits C4/C8 to one entry; no extra canonical
runs were added for them. The failed full-run gate stops the non-LBM sweep and
frozen-window reruns. The prepared `audits.py` is not an executed result.

### What the failure establishes and leaves unresolved

C32 has 90,022,642 accepted memory-replay events in this diagnostic population
(including private hits and ifetch, not only shared-hierarchy/DRAM requests):

| Diagnostic | Value |
|---|---:|
| Projected admission beyond current Q1024 horizon | 86,834,973 events (96.46%) |
| Maximum distance beyond horizon | 408,711 cycles |
| Arrival earlier than the maximum published by prior epochs | 10,732,981 events |
| Maximum such backward distance | 392,454 cycles |
| Shifted ordinary-store events | 32,854,147 |
| Shifted non-store events | 57,094,230 |

These observations are consistent with excessive future-state publication and
later-discovered arrival inversions breaking the projection. They do **not**
prove which inversions contend for the same bank/channel/cache resource, or
isolate each mechanism's contribution to CPI inflation. The late-visibility
counter compares a previous-epoch global maximum, not conflict ownership.
Non-store displacement is a separate approximation, not direct store-frontier
waiting. Counts are memory events, not unique harmed instructions; summed
shifts are not additive stall cycles, recoverable CPI, or an attribution of the
original baseline's 0.294373 absolute error.

The original paired local inconsistency remains valid evidence: a queued service
was consumed after its same-core parent had already completed. This failed
intervention shows that correcting that local edge by publishing a long chain of
future requests is insufficient. It does not show an immutable DRAM limit, nor
justify changing `t_cl`, bank latency, a per-core scalar, or a global CPI discount.

The next bounded design should require **horizon-contained accepted replay with
whole-epoch fallback**, recording coverage of the affected stores before a large
accuracy sweep. If coverage is negligible, the missing interface is persistent
pending admission: requests crossing Q must remain unpublished until earlier
competitors are visible, with response/group ownership retained until closure.
A pending record alone does not solve core continuation; response availability,
accepted-prefix progress and state/counter rollback must have an explicit contract.
This design remains open, and a full core rewrite is not assumed as a prerequisite.

### Verification, throughput, and configuration fallback

The isolated C++ suite passes, including two-fragment stores sharing admission,
next-store dependence on the maximum response, cross-checkpoint ownership and
warmup boundary/count reset. Python integration tests pass for predecessor
ordering, service-duration origin, deterministic reruns, incompatible modes,
default versus explicit false, and CLI false overriding an enabled config. The
existing interval-tail audit test also passes. C++ test linking used 16 LTO workers
for the final fixture build; the simulator/library used in all ten runs was
unchanged. Independent code and conclusion review found no blocking defect
within the declared approximation, with the limitations above retained.

Observed processing rates below use only `scope_metrics.throughput` and actual
measured user UOPs. Simulations were sequential on a shared host; compilation and
unit tests overlapped some runs. These single observations are **not quiet-host
throughput certification**, although they provide no reason to promote the
already inaccurate candidate. Detailed work counts and measurement/end-to-end
wall times are in `accuracy.json`.

| Mode | Mean M user-UOP/s | P50 | P90 | P99 | Minimum |
|---|---:|---:|---:|---:|---:|
| off | 3.5949 | 3.6616 | 3.6995 | 3.7106 | 3.3444 |
| on | 2.8465 | 2.9043 | 3.0166 | 3.0206 | 2.5563 |

**Disposition:** rejected; candidate remains only in the isolated worktree.
Maintained source files, headers, tests and the original executable retain their
pre-experiment hashes. The required maintained `cmake --build build -- -j16` and
`./build/fastsim_tests` pass. No commit or push was performed.

Artifacts: `tmp/lbm-store-response-frontier-20260915-nwsmb7pj/`:
`README.md` contains exact replay/fallback commands; `candidate.patch` contains
only the new experiment relative to the saved dirty baseline; `worktree/` holds
the implementation and default-off profile; `fastsim-before` retains the original
binary. `accuracy.json`, `accuracy.csv`, `runs.json`, `input-provenance.json`,
`oracle-identity.json` and `final-verification.json` retain outcomes and checks.

The configuration fallback is `--store-response-frontier false` when using the
**isolated candidate binary**. It takes effect on a new run; it is not in-flight
state rollback. All four off runs reproduce the frozen baseline exactly. The
maintained executable does not acquire the experimental option.


## 2026-09-15 two-stage partial repair: bank-gated expired-store reservation relief

The user explicitly requested a partial mechanism correction **within the existing
two-stage framework**, permitting small interface/state changes and requiring a
rollback. This execution delivers an opt-in candidate under that scope. The original
unbounded store-response projection remains rejected and isolated; it is not enabled
or reused here. No workload name, core-count threshold, gem5 timestamp, or fitted
DRAM latency/exposure multiplier enters the new rule.

### Mechanism and scope

1. The shared stage keeps its canonical cache/MSHR/DRAM schedule. Each DRAM
   reservation carries an explicit core, UOP sequence, ordinary-store flag and full
   DRAM response. PRE has its own source, updated when ACT+tRAS or the later column
   precharge constraint establishes its value. A qualifying floor must belong to a
   **strictly earlier ordinary store of the same core** and end no later than that
   owner's full response. Loads, atomics, writebacks, ifetches, same-UOP fragments,
   foreign-core and unknown owners cannot authorize relief.
2. The model estimates one counterfactual completion with qualifying bank/PRE,
   bank-group, channel/rank and data-bus floors removed; ACT and activation-window
   floors remain. **The bank-command time must actually decrease** before any credit
   is allowed. The credited amount is the joint max-plus completion difference,
   not a sum of overlapping floors and not a pure-bank attribution. PRE numerical
   ties retain a representative current-column owner; this rule models already-covered
   predecessor reservations, including inherited waits. It does not prove that all
   transitive foreign/unknown contention has been separately reconstructed.
3. The core stage consumes credit only at the actual ordinary-store drain/send edge,
   after its accepted TSO owner frontier covers all source sequences used by the
   certificate. Credit is capped by actual-send minus canonical-issue and the
   fragment's latency lower bound. A split store saves **max(raw fragment services)
   minus max(adjusted fragment services)**. The canonical descriptor and raw service
   remain available; the SQ release uses adjusted service. Tests verify that release
   does not precede the canonical fragment responses.
4. FR-FCFS controller replacement invalidates the old certificates, including cases
   where numeric responses tie; the replacement's unclassified sources are ineligible.
   Retry counters and owner state are committed only with accepted timing. Warmup
   retains absolute owner state while resetting measurement populations/counters.
5. Other replay/pending/shared-service experiments and unequal clocks, including
   runtime DVFS, are rejected by explicit guards. The maintained private preview,
   FR-FCFS path and DRAM constants stay configured as before. Changed core feedback
   can change arrivals and shared schedules in **later epochs**: whole-run queue,
   load, and cache behavior are not claimed invariant when the feature is on.

The local inconsistency being addressed is reuse of a queued service duration after
the earlier store responsible for part of that reservation has already been awaited
at TSO send. This is a timing-origin/response-reuse issue in the two-stage model;
it is not evidence that tCL/tRCD/tRP values are wrong. Remaining ACT ownership,
inter-core arrival approximation, other tails and compensating errors are unresolved.

### Full common-end LBM results

Frozen dataset: `tmp/first-core-common-end-20260911/source/`, workload `782.lbm_r`,
C4/C8/C16/C32, four configurations and 60 per-core traces. These are investigation
cases, not a held-out accuracy certification. All eight final-version on/off runs
use one binary and the maintained `configs/gem5-fs-native-kernel.cfg` profile plus
only the new switch. Record-bounded functional warmup and the original
`first-core-target-common-end-v1` population are retained. The existing four-case
oracle identity and metadata hashes are rechecked; every per-core macro count is
checked against the oracle, and off reproduces the frozen scope/threads/caches exactly
apart from throughput and newly added zero diagnostics. Counts are macroinstructions.

Formal CPI = `scope_metrics.sum_core_cycles / perf_like_cpi_denominator_instructions`;
gem5 active cycles = measured minus idle cycles. Absolute CPI errors below are
**cycles per completed scope macroinstruction**, not percentage points.

| Cores | gem5 CPI | Off CPI | Off signed error | Off absolute CPI error | On CPI | On signed error | On absolute CPI error |
|---|---|---|---|---|---|---|---|
| 4 | 3.231335 | 3.134823 | -2.9868% | 0.096513 | 3.137956 | -2.8898% | 0.093379 |
| 8 | 3.202265 | 3.261426 | +1.8475% | 0.059161 | 3.231963 | +0.9274% | 0.029698 |
| 16 | 3.527754 | 3.510302 | -0.4947% | 0.017452 | 3.501833 | -0.7348% | 0.025921 |
| 32 | 3.906000 | 4.200373 | +7.5364% | 0.294373 | 4.158580 | +6.4665% | 0.252580 |

Four-case CPI MAE **0.116874798→0.100394499** (14.10% lower); MAPE
**3.216350%→2.754609%**. C32 absolute error falls 14.20% and C8 49.80%; C16's
absolute error increases by 0.008469 cycles/macroinstruction. This tradeoff is why
the switch remains default-off. Per-core CPI MAE, APE percentiles, macro-weighted
WAPE/bias and scope throughput are retained in `accuracy.json` and `accuracy.csv`.

C32 applies relief to **62,554 measured store groups**, totaling 9,113,556 service
cycles. Total scope cycles fall by 10,035,560. These are different quantities:
core overlap and later shared scheduling change as well, so service credits must
not be presented as an additive attribution of saved CPI. C4's aggregate CPI rises
slightly despite nonnegative local service reductions, illustrating this feedback.

### Frozen positive and negative windows

The original sequence bounds were preserved: positive C32/core13
`[3122784,3126150)` with 2,932 macros; negative C32/core1 `[8479096,8483017)` with
3,400 macros. This metric is the predecessor-to-end retirement span per completed
macro, **not the formal whole-run CPI**. Audit-on reruns exactly match final-version
plain-run scope/threads/totals; captured request identities match the frozen captures,
TSO sends never precede the previous captured store release, and releases do not
precede canonical fragment responses.

| Frozen window | gem5 span CPI | Off | Off signed error | Off absolute CPI error | On | On signed error | On absolute CPI error |
|---|---|---|---|---|---|---|---|
| positive | 11.646658 | 24.811392 | +113.0344% | 13.164734 | 14.468963 | +24.2328% | 2.822306 |
| negative | 10.624412 | 9.739706 | -8.3271% | 0.884706 | 10.143529 | -4.5262% | 0.480882 |

The positive window's 105 native-DRAM-paired stores average **640.181→383.876**
FastSim service cycles, versus gem5 **306.143**. Negative-window matched service
moves **257.540→273.982**, versus gem5 **287.071**, over the same 113 pairs.
These changes include induced scheduling changes: before applying local credits,
new-run raw service already averages 416.724 in the positive window and 289.717
in the negative window. Direct captured credits total only 3,449 and 1,778 cycles,
respectively. Therefore neither the window-span improvement nor the entire service
mean change can be assigned solely to direct local subtraction.

### Non-LBM controls, throughput and rejected inputs

Two further C32 control configurations, tealeaf and stockfish, contribute 64 per-core
traces. They are regression controls, not a workload-held-out generalization claim.
Both final-config-derived oracle identities and measured macro populations pass.

| Control | gem5 CPI | Off CPI | Off signed error | Off absolute CPI error | On CPI | On signed error | On absolute CPI error |
|---|---|---|---|---|---|---|---|
| 811.tealeaf_s C32 | 0.501256 | 0.441014 | -12.0183% | 0.060242 | 0.441014 | -12.0183% | 0.060242 |
| 706.stockfish_r C32 | 0.690507 | 0.685977 | -0.6560% | 0.004530 | 0.685960 | -0.6585% | 0.004547 |

Control CPI MAE **0.032386260→0.032394717**; the small increase is from stockfish.
Tealeaf does not trigger relief and is timing-identical; stockfish triggers 38 groups,
with 1,253 service cycles credited. NAMD C32 was attempted but rejected at the scope
population gate: FastSim reports 184,282,895 macros versus gem5 184,282,933. Its
CPI is not scored and its denominator is not rewritten to make the case pass.
The valid final report therefore covers six configurations/124 per-core traces,
with one rejected control input; LBM and control aggregates remain separate.

Observed four-case mean scope throughput is **3.28755→3.27349 M user-UOP/s**
(−0.43%). Runs overlapped other experiments on a shared host; these single paired
observations are not quiet-host throughput certification. Both on/off populations
and run-specific timing/throughput fields are retained. Default promotion would need
broader workload validation and controlled performance measurements.

### Superseded broader version and verification

The first local-relief version admitted channel-only credits and retained unowned
PRE floors. It improved C32 (signed error +7.5364%→+5.4865%, absolute CPI error
0.294373→0.214303) but regressed
C4/C8/C16, raising four-case CPI MAE **0.116875→0.135094**; its positive/negative
window signed errors were +80.6226%/+25.7703%, with absolute CPI errors
9.389836/2.737941. It is preserved under `v1/` and is
not the retained implementation. The final change was a mechanism gate and explicit
PRE ownership, not a fitted fraction or threshold chosen by core count.

The isolated full `fastsim_tests` suite and Python rollback/interval-tail tests pass.
Targeted tests cover max-plus overlap, foreign/load/writeback/same-UOP exclusion,
channel-only rejection (observed failing on v1), owned PRE expiry, live tRAS/tCCD
retention, actual positive core consumption, single/two-fragment groups, accepted-only
counters, cross-checkpoint and warmup carry, canonical-response release lower bounds,
shared-service conflicts and runtime-DVFS rejection. Code review found and fixed the
shared-service and DVFS scope omissions; the final bank/PRE implementation has no
remaining review blocker. Maintained `cmake --build build -- -j16`, the full
`./build/fastsim_tests` suite, and both Python mechanism/audit tests also pass.
Maintained source hashes exactly match the tested isolated candidate; the rebuilt
maintained binary and all checks are recorded in `final-verification.json`.

### Use, rollback and next boundary

`include/fastsim/config.hpp` defaults `store_wait_relief=false`. To try the retained
candidate with the same supported profile, add `--store-wait-relief true` to the
normal `fastsim simulate` command, or set `core.store_wait_relief = true` in the
config. **Rollback: set it to false and rerun**; it is not a hot rollback of an
already-running simulation. All four final-version off runs exactly recover the
frozen baseline. No default config is enabled and no commit or push was made.

The delivered scope is partial error reduction inside two stages. Before widening
coverage, keep the C16 regression and both frozen signed-error windows as controls;
trace unresolved ACT/source constraints separately and require full-case absolute
error/CPI MAE checks again. Do not reintroduce unbounded future-store projection or
rescale raw DRAM service to force agreement.

Artifacts: `tmp/lbm-store-wait-relief-20260915-czins1ei/`, including `before/`,
`baseline-hashes.json`, `fastsim-before`, `candidate.patch`, `promoted-source-hashes.json`,
`runs.json`, `accuracy.json/csv`, `controls-accuracy.json`, `frozen-windows.json`,
paired-service CSVs, input/oracle identity records, build/test logs and reproducible
run/finalization scripts. Final experiment binary SHA-256:
`4f2a1517f354cd34140f14fd9d1cbbcd71d26a706a2992ddccd9cf19e638bd5f`.


## 2026-09-15 further optimization: ACT ownership extension rejected

The follow-up tested whether explicitly owned ACT reservations could extend the
retained bank/PRE correction. **The extension fails the accuracy gate and stays
isolated.** The maintained implementation remains v2, default-off. No DRAM timing
constant, scalar relief fraction, workload selector or core-count gate was changed.

### Candidate and validation scope

The isolated `core.store_wait_relief_act` option defaults to false. Enabling it
requires `core.store_wait_relief=true`. It tracks the ordinary-store source of each bank's
ACT-ready floor, including propagation between banks of one rank. A strictly later
floor installs its source; a numerical tie between different owners becomes unknown.
Unknown, foreign, load/writeback and same-UOP owners cannot authorize ACT relief.
An eligible source must be a strictly earlier same-core store, and its full canonical
DRAM response must cover the floor. Activation-window constraints remain intact.

The **original v2 bank-command benefit gate runs first**. ACT-only benefit cannot
create eligibility. Base v2 and additional ACT credits carry separate parent
sequences: if the core cannot cover the extra parent, it retains any valid base
credit. Per-fragment clipping precedes group max aggregation; the extra group credit
is the difference between the base-adjusted and ACT-adjusted maxima. FRFCFS
replacement invalidates both certificates. The core's accepted TSO frontier,
send-displacement cap, service lower bound and warmup/cross-Q rules are preserved.

All runs use the frozen common-end dataset and maintained profile described above:
four LBM configurations/60 per-core traces and two C32 controls/64 per-core traces,
native `user-plus-kernel` scope, original record-bounded functional warmup with
retained state, and actual scope macroinstruction denominators. These are
investigation/control cases, not held-out certification. Input and oracle identity
hashes and per-core macro populations pass. No new input is rejected; the previously
rejected NAMD population is not rescored. No PMU accuracy claim is made.

The same candidate binary ran ACT on/off at all four LBM core counts and both
controls (12 runs), plus one C4 run with both flags off. ACT off reproduces frozen
v2 scope/threads/cache/timing diagnostics exactly, apart from throughput and new
zero ACT counters; both off recovers the original C4 baseline. Two further C32
audit runs preserve the frozen request identities and plain-run scope/timing.

### Full common-end results

Absolute errors are cycles per completed scope macroinstruction. Formal CPI is
computed from `scope_metrics`, with gem5 active cycles equal to measured minus idle.

| Cores | gem5 CPI | v2 CPI | v2 signed error | v2 absolute CPI error | ACT CPI | ACT signed error | ACT absolute CPI error |
|---|---|---|---|---|---|---|---|
| 4 | 3.231335 | 3.137956 | -2.8898% | 0.093379 | 3.136529 | -2.9340% | 0.094806 |
| 8 | 3.202265 | 3.231963 | +0.9274% | 0.029698 | 3.232041 | +0.9298% | 0.029776 |
| 16 | 3.527754 | 3.501833 | -0.7348% | 0.025921 | 3.498249 | -0.8364% | 0.029505 |
| 32 | 3.906000 | 4.158580 | +6.4665% | 0.252580 | 4.163456 | +6.5913% | 0.257455 |

| Mode | CPI MAE | MAPE | P50 APE | P90 APE | P99 APE | Maximum APE | Macro-weighted WAPE | Macro-weighted signed bias |
|---|---|---|---|---|---|---|---|---|
| v2 | 0.100394 | 2.7546% | 1.9086% | 5.3935% | 6.3592% | 6.4665% | 4.2374% | +3.5731% |
| ACT | 0.102885 | 2.8229% | 1.9319% | 5.4941% | 6.4816% | 6.5913% | 4.3387% | +3.6192% |

All four absolute errors increase; four-case CPI MAE increases 2.48%. C32 accepts
4,077 groups with 685,533 additional ACT service cycles, but total scope cycles
increase by 1,170,736. These are not additive quantities. Credits only count the
measurement interval; warmup changes can also affect retained state. Neither the
small C4 measured ACT credit (two cycles) nor the C32 credit total identifies how
much end-to-end timing change it caused.

The C16 direction deserved a separate check before extending coverage. Under the
retained v2, unweighted per-core CPI MAE improves even though aggregate CPI absolute
error worsens. The original aggregate result included cancellation between cores.
ACT regresses the per-core measure as well as aggregate accuracy:

| Cores | Original off per-core CPI MAE | v2 per-core CPI MAE | ACT per-core CPI MAE |
|---|---|---|---|
| 4 | 0.092310 | 0.090020 | 0.091679 |
| 8 | 0.070196 | 0.037036 | 0.039080 |
| 16 | 0.107102 | 0.091581 | 0.095675 |
| 32 | 0.321020 | 0.270825 | 0.277469 |

These within-configuration core averages do not replace configuration-equal CPI
MAE or the aggregate CPI gate. Detailed signed/absolute per-core results are in
`baseline-per-core.json`; candidate per-core timings remain in the raw run JSONs.

### Frozen windows and service evidence

The positive core13 `[3122784,3126150)` and negative core1 `[8479096,8483017)`
bounds are unchanged. CPI below means predecessor-to-end retirement span per
completed macro (2,932 and 3,400 respectively), **not formal whole-run CPI**.

| Window | gem5 span CPI | v2 | v2 signed error | v2 absolute CPI error | ACT | ACT signed error | ACT absolute CPI error |
|---|---|---|---|---|---|---|---|
| positive | 11.646658 | 14.468963 | +24.2328% | 2.822306 | 18.168486 | +55.9974% | 6.521828 |
| negative | 10.624412 | 10.143529 | -4.5262% | 0.480882 | 13.527941 | +27.3288% | 2.903529 |

Both windows regress. The same native-DRAM-paired stores show increased raw service
before local credit consumption. The service means below are cycles/request, not
CPI errors; neither their differences nor the local credits may be added to CPI.

| Window | Pairs | Native service mean | v2 raw / adjusted service mean | ACT raw / adjusted service mean | v2 / ACT direct local credits |
|---|---|---|---|---|---|
| positive | 105 | 306.143 | 416.724 / 383.876 | 549.114 / 473.971 | 3,449 / 7,890 |
| negative | 113 | 287.071 | 289.717 / 273.982 | 379.177 / 372.407 | 1,778 / 765 |

Captured identities, PCs and sequences match the original audit. Actual TSO sends
do not overlap prior captured releases, and releases never precede canonical
fragment responses. Thus the local clipping/order checks pass while raw and adjusted
store-service means increase and window accuracy worsens. Changed later shared scheduling is compatible
with this evidence, but the captures do **not** isolate which earlier divergence,
warmup state, foreign-core wait or row-state transition causes the added service.
Unit-level max-plus correctness alone does not prove native ACT attribution or
justify promoting the extension as an independently established semantic repair.

### Controls, performance observations and disposition

ACT gives zero additional credit on both non-LBM controls, and their v2/ACT outputs
are timing-identical. Control CPI MAE remains **0.032395** in both modes.

| Control | gem5 CPI | v2 and ACT CPI | Signed error, both | Absolute CPI error, both |
|---|---|---|---|---|
| tealeaf C32 | 0.501256 | 0.441014 | -12.0183% | 0.060242 |
| stockfish C32 | 0.690507 | 0.685960 | -0.6585% | 0.004547 |

LBM scope throughput uses actual measured user UOPs. These are single observations
on a shared host with overlapping control/audit runs, **not a quiet-host performance
gate**. Full work counts, measurement timing and end-to-end wall times remain in
`runs/*.json` and `runs.json`. No throughput improvement is claimed.

| Mode | Mean M user-UOP/s | P50 | P90 | P99 | Minimum |
|---|---|---|---|---|---|
| v2 | 3.29869 | 3.30241 | 3.41904 | 3.45698 | 3.12874 |
| ACT | 3.28642 | 3.34977 | 3.43182 | 3.44600 | 2.99858 |

The isolated build, full C++ suite, new ACT Python test and existing v2/interval-tail
Python suites pass. Tests include positive extra credit, ACT-only rejection,
mixed-owner ties, cross-bank ownership, live tRRD/window retention, uncovered-parent
fallback to v2, clipped-to-zero credits, split groups, warmup and accepted-only
accounting. Independent code review found no blocking implementation defect.
This validates the implementation's stated rules, not its accuracy benefit.

**Decision:** do not promote ACT coverage or adjust a scalar to compensate for it.
The maintained 135 source/header/test/config snapshot files and executable retain
their v2 hashes. The existing `--store-wait-relief false` rollback remains intact.
Only the isolated binary accepts `--store-wait-relief-act`; setting it false and
rerunning restores v2, while both flags false restore original behavior. No commit
or push was made.

The next accuracy investigation should follow the first changed accepted request
from warmup through measurement, recording actual send, canonical arrival, row state
and each reservation's direct/inherited owner before comparing raw and adjusted
service. A fixed-request-stream replay can separate the local counterfactual from
later arrival feedback; paired native events must then establish whether the added
wait is spurious. Keep both frozen windows, C16 per-core errors and the full four-case
CPI MAE gate. The present results do not establish a new repair or an unavoidable
accuracy floor for two stages.

A source inspection also identified a possible host-cost optimization: the v2
counterfactual cannot advance its bank command unless a bank or applicable PRE floor
above arrival has an eligible owner. Such a precheck could skip futile estimates,
but it has not been implemented or benchmarked and is not an accuracy repair.

Artifacts: `tmp/lbm-store-act-relief-20260915-cwyc6iww/`, including `README.md`,
`before/`, `worktree/`, `fastsim-v2`, `candidate.patch`, `baseline-hashes.json`,
`candidate-hashes.json`, `runs.json`, `accuracy.json/csv`, `controls-accuracy.json`,
`baseline-per-core.json`, `frozen-windows.json`, paired-service CSVs, oracle identity
records, build/test logs and `final-verification.json`. Reproduction scripts are
`run.py`, `audits.py`, `finalize.py` and `verify_and_summarize.py`. Candidate binary
SHA-256: `7f1680545a965f9504805960463518fede129d2999dae7da5a3eef8335ef3d49`.
