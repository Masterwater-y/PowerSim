# CPI, PMU, and throughput reporting contract

The controlling project objective and semantic hierarchy are defined in
[`project-goal-and-semantic-contract.md`](project-goal-and-semantic-contract.md).
This document specifies report aggregation; it does not authorize comparing
two counters whose event semantics differ.

This document defines the mandatory aggregate statistics for FastSim accuracy
and performance reports. It applies to all subsequent formal reports unless a
report explicitly declares and justifies a different contract.

Effective 2026-09-11, formal multicore reports must use the common collection
and statistics end in section 3.0 of the controlling project contract:
`first-core-target-common-end-v1`. A report using an independent per-core cutoff
or fixed per-core scoring plus unscored context is historical/diagnostic; a
report-level exception cannot promote it to a common-end formal result.

## 1. Dataset and provenance

Every report must state:

- the dataset path, configuration count, per-core trace count, workloads and
  core counts;
- which cases are calibration and which are held out;
- the trace/oracle integrity gate and the number of rejected cases;
- the frozen simulator and kernel-model configuration;
- whether results are formal, pilot, diagnostic, or historical.

For the common-end measurement policy, provenance must also retain the
participant core set, requested target and counting unit, common start/end
event identities, actual per-core user/mixed UOP and macroinstruction counts,
exclusive record boundaries, and stop reason. These are required metadata
semantics, not a claim that existing JSON producers already emit them.
The fastest core reaching the 10M user-UOP threshold closes the common window;
slower cores may have fewer UOPs. Changing a legacy manifest or relabeling an old oracle cannot establish
the missing common-window provenance.

For gem5-FS PMU reports, the data gate must also prove that
`tao_trace/uarch_profile.json` describes the restored target recorded by
`request.json:boot_profile.profile`.  At minimum it must compare core count and
frequency, L1/L2 capacity and associativity, total LLC capacity, LLC bank count
and associativity, coherence protocol, DRAM size/channel count, and address
interleaving.  A mismatch invalidates cache-path PMU truth even when the cycle
and CPL classification is conserved.  The paired formal pipeline is
fail-closed on such a mismatch; use `tools/validate_fs_oracle_identity.py` for
the standalone audit.  A report may retain the affected CPI result only when
it explicitly labels the PMU result invalid and explains why the profile does
not affect the CPI cycle oracle.

Calibration and held-out results must remain separate. The held-out split is
the primary accuracy result. A combined statistic may be shown only when it is
explicitly labeled and cannot replace the two split-specific results.

The held-out axis must be named. Freezing C4 parameters and evaluating C8 is a
**core-count-held-out** test when the workload names overlap; it is not evidence
of workload-held-out generalization. A report that claims resistance to
workload overfitting must additionally provide disjoint-workload evaluation or
leave-one-workload-out diagnostics. Neither diagnostic may replace the formal
held-out result.

## 2. Measurement-pipeline scope lock

Every FastSim CLI run must select exactly one measurement pipeline with
`--measurement-scope user|user-plus-kernel` or the equivalent
`measurement.scope` config key. The selected scope is part of the simulation
contract, not a filename convention:

- `user` permits only functional user-trace timing. Syscall active service,
  syscall cost/event, page-fault event, and IRQ event models must all be off.
  A separately declared state-only model may replay trace-inferred kernel
  cache effects, but it must add no cycles, retired work, or PMU counts.
  Trace-visible syscall serialization, drain, system-UOP execution, and the
  return-to-user frontend restart remain user-pipeline effects;
- `user-plus-kernel` requires at least one enabled kernel service model and
  reports the resulting combined timeline and PMU counts.

`fastsim-stats-v5` records the selection in `measurement_scope`. CPI, PMU,
and formal trace-processing throughput must be read only from
`scope_metrics`; the broad `totals` object contains internal diagnostics and
legacy compatibility fields and is not a formal cross-scope statistics API.
Tools must reject a report whose scope does not match the requested
comparison. A paired report uses `user.json` and `user-plus-kernel.json`; it
must not infer scope from those names.

## 3. Warmup declaration

Every report must say whether FastSim receives a functional warmup prefix that
is replayed only to establish state and excluded from the measurement window.
It must record the manifest kind, warmup record/uop count, measurement
record/uop count, reset barrier, and retained state.

`fastsim-binary-warmup-slice` means the prefix is replayed before a common
barrier. Formal rows carry both instruction and exact record counts; the
record boundary is authoritative because the asynchronous serial marker can
fall between UOPs of one macro instruction, while the instruction counts are
checked independently. Measurement counters and time are then reset while cache/coherence,
directory, branch-predictor, DTLB, DRAM/controller, dependency, and response
scoreboard state remain resident. A plain `fastsim-binary` or
`fastsim-binary-slice` manifest has no functional warmup unless another
explicit mechanism is documented.

A gem5 request field such as `sampling.warmup_mode = source` describes the
reference collection process. It is not evidence that the exported FST gives
FastSim a warmup-only prefix. If the two-phase FastSim prefix is absent, the
report must label the FastSim measurement as a cold trace slice.

The formal kernel-event pipeline must fail closed: every input manifest must
contain exactly one record-bounded functional-warmup row per participating
core, with measurement bounds covering its full common-end population.
`fastsim-binary-warmup-slice` can represent those actual counts; its format
alone does not prove the collection policy. `trace.json` must declare
`functional_warmup_enabled = true`, and aggregate warmup and measurement
counts must both be nonzero. Each oracle `n_user` must equal that core's actual
measurement user-UOP population under the declared marker convention, which
need not reach the target on a slower core. The trigger core must reach it. In native mode the mixed measurement-record
count additionally includes kernel/auxiliary records; it is not interchangeable
with `n_user`. A core may legitimately have a zero
warmup prefix when it was not scheduled in user mode before the common serial
marker, provided the configuration-wide warmup total is nonzero. Cold slices
are accepted only with the explicit diagnostic `--allow-cold-slice` override
and cannot be called formal data.

The common close applies to FST recording and all metrics together. The
fastest core reaching the target closes every participant at that event;
slower cores keep their actual counts without padding. All work before that
common end is scored. The old local-cutoff checks are insufficient; the updated gate must
prove continuous capture and accounting with unequal core progress.

## 4. CPI accuracy

CPI is an end-to-end system outcome, not a standalone component-correctness
test. A CPI improvement can result from cancellation between unrelated model
errors, while a semantically necessary component repair can initially expose
an error elsewhere and worsen aggregate CPI. Component decisions must first
use paired event identity, ordering, ownership, occupancy, visibility, and
conservation evidence; conditional latency/PMU distributions and interaction
ablations come next. CPI distributions and signed bias are the final model
promotion gate. They must not be used alone to attribute an error to one
component or to revert an independently proven semantic repair.

The project distinguishes perf-like macro-instruction CPI from the existing
user-work-normalized UOP metric:

```text
perf_like_CPI(scope) = cycles(scope) / retired_instructions(scope)
cycles_per_user_uop(scope) = cycles(scope) / N_user
```

Here all numerators and denominators cover the common measurement window,
and `N_user` is the actual sum of measured user UOPs. It is not the requested
target multiplied by the core count. Macro-instruction CPI uses the actual
scope-matched completed macroinstructions over that same window. All per-core
counters close at the fastest core's trigger event, using actual counts even
when a slower core is below the target. Aggregate CPI is the sum of scope-matched core cycles divided by
the sum of actual retired macroinstructions, not the mean of per-core CPIs.
gem5 and FastSim derive their own cycle values and actual denominators; a
common boundary does not authorize injecting a gem5 timestamp or cycle count
into FastSim.

The latter remains the current deployable FS compatibility and system-overhead
metric. Because both scopes use the same user-UOP denominator, it must not be
described as real-machine `perf cycles/instructions` CPI.

Availability is scope-dependent. FST v7 preserves user macro-instruction
boundaries with `kMicroOp`/`kLastMicroOp`, so user perf-like CPI is strict when
that boundary count passes conservation. A user-only trace does not contain
kernel retired instructions, so FastSim user-plus-kernel perf-like CPI is
unavailable unless a frozen kernel event profile supplies a modeled count. In
that case it must be labeled `proxy`, never strict. gem5 must retain exact user
and user-plus-kernel perf-like CPI as auxiliary references, but FastSim must not
consume the gem5 combined instruction denominator during inference.

For each case `i`, report absolute percentage error:

```text
APE_i = abs(predicted_i - reference_i) / reference_i * 100%
```

From 2026-09-11, every CPI accuracy table, including conversational summaries
and P99 tail-case tables, must include a **CPI absolute error** column alongside
relative error:

```text
absolute_cpi_error_i = abs(predicted_CPI_i - reference_CPI_i)
CPI_MAE = mean(absolute_cpi_error_i)
```

Absolute CPI error is nonnegative and measured in cycles per macro instruction,
not percent. Compute it from unrounded, scope-matched CPI values and normally
display six decimal places. Aggregate tables must include CPI MAE alongside
MAPE and relative-error percentiles. If absolute-error percentiles are also
shown, label them separately: they need not select the same cases as relative
P99. For `cycles_per_user_uop`, label the corresponding absolute difference in
cycles per user UOP rather than calling it macro-instruction CPI error.

Aggregate and report, for both scopes:

- arithmetic mean APE (MAPE);
- P50, P90, and P99 of the per-case APE distribution;
- WAPE, signed aggregate bias, and maximum APE as diagnostics.

The percentile estimator is R/NumPy Type 7. For sorted samples `x` and
fraction `q`, set `r = (n - 1)q` and linearly interpolate between
`x[floor(r)]` and `x[ceil(r)]`. Each configuration contributes one APE sample,
independent of its instruction count.

The two compatibility/system-overhead scopes use the same denominator,
`N_user`:

```text
cycles_per_user_uop_user = user cycles / N_user
cycles_per_user_uop_user_plus_kernel =
    (user + syscall + page-fault + IRQ + scheduler cycles) / N_user
```

Historical fields may still call these values `CPI_user` and
`CPI_user_plus_kernel`; reports must label that alias as user-work-normalized,
not perf-like. The first value comes from the `user` pipeline and the second
from the `user-plus-kernel` pipeline. Idle, blocked wall time, and unknown
kernel cycles are excluded from the formal numerator; formal oracle input
requires unknown kernel cycles to be zero.

When a state-only model is enabled, both paired runs must use the same frozen
event selector and cache-state transition. Only the user-plus-kernel run may
add the corresponding service cycles and kernel PMU bundle. Reports must name
the state model and expose exact semantic candidates, residual candidates,
selected event/page count, and any frozen residual probability.

For `cycles_per_user_uop` only, WAPE and signed bias weight each per-case value
by its `N_user`, so they are equivalent to aggregate cycle error over the
shared user-UOP denominator. Perf-like CPI must instead use its scope-matched
retired-instruction count. MAPE and percentiles remain configuration-equal.

## 5. PMU accuracy

Before an error statistic is formal, the report must link a versioned event
dictionary containing the perf event/encoding and privilege scope, count unit,
speculative policy, gem5 increment site, FastSim increment site, reset boundary,
and conservation equation. Every counter must be classified as `strict`,
`proxy`, or `diagnostic`. Only `strict` counters enter the formal PMU headline.

In particular, a FastSim LLC tag miss must not be scored as the same event as a
Ruby demand/protocol miss. Permission upgrades, remote supplies, shared-LLC/CHA
lookups, merged misses, unique fills, and DRAM transactions must remain separate
unless the event dictionary proves an exact aggregation. Generic names such as
`LLC misses` without this definition are invalid formal metrics.

The data gate must also prove count coverage before scoring accuracy. At
minimum it must conserve committed memory UOPs across packet-attributed,
fallback-attributed, and explicitly rejected paths; separately conserve
cross-line expansion into cache-line requests; and require zero unaccounted or
duplicate events. Scope/class conservation alone is insufficient.

Collection and PMU must close at the same common event. Do not retain only
each core's first target-sized population, and do not combine full-window
FST with a local-cutoff oracle. Event dictionaries must state whether an event
is selected by occurrence in the common window or by ownership by a measured
committed request. For the latter, late completion may be finalized during
drain without counting newly admitted post-window work; drain time is not
silently included in the CPI numerator. Raw post-exit totals cannot substitute
for the defined measurement population.

For every reported PMU counter, separately for user and user+kernel scope,
report:

- finite-APE case count over total case count;
- mean APE (MAPE), P50, P90, and P99 APE;
- WAPE and signed aggregate bias.

PMU APE distributions are configuration-equal. PMU WAPE is computed from the
sum of absolute count errors divided by the sum of reference counts, without
an extra instruction-count weight.

If reference and prediction are both zero, the case contributes APE zero. If
the reference is zero but the prediction is nonzero, relative error is
undefined: exclude that case from MAPE/percentiles, show the finite-APE case
count, and retain its absolute error in WAPE. Sparse counters must therefore
be interpreted using both the APE distribution and WAPE; neither may be
reported alone.

## 6. Throughput

Throughput is a direct performance distribution, not an error distribution.
For both the `user` and `user-plus-kernel` runs, report arithmetic
mean, P50, P90, P99, and minimum trace-processing throughput in million user
uops/s. Use the same Type-7 percentile estimator as accuracy statistics.

Throughput runs must be sequential on a quiet host, or the report must state
the actual concurrency and host-load conditions. Throughput is FastSim host
trace-processing rate and must not be described as target-program IPC.

The numerator must be the actual common-window user UOP count, across all
participants, rather than `cores * target`.
Report measured user/mixed work counts, measurement wall time excluding
functional warmup, and end-to-end wall time including warmup. Any explicitly
separate diagnostic work must have a separate work/rate field; it must not
hide the actual work of slower cores with fewer than the requested UOPs.
When comparing legacy cutoff data with newly collected common-end data,
report the changed work populations and do not attribute the entire wall-time
increase to simulator overhead.

## 7. Required report layout

A formal report must contain, in order:

1. data gate, split, and provenance;
2. event dictionary and strict/proxy/diagnostic PMU classification;
3. FastSim functional-warmup declaration;
4. user-work-normalized cycle tables, strict user perf-like CPI, exact gem5
   user-plus-kernel perf-like CPI, and any explicitly proxy-labeled FastSim
   combined CPI for calibration and held-out splits;
5. per-counter PMU tables for user and user+kernel scopes;
6. microarchitecture-parameter delta/direction/ranking tables when a parameter is varied;
7. throughput table for both paired-run scopes;
8. machine-readable JSON/CSV and per-case artifact paths;
9. known modeling limitations and any invalidated gate.

`tools/summarize_kernel_event_accuracy.py` is the canonical implementation of
these aggregate statistics. Its JSON output retains compatibility aliases for
the older median fields, but new consumers must use the explicit P50 fields.
