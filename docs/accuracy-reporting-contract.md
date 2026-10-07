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

## 0. Mandatory source selection before evaluation

Read this section before writing a report, diagnosing an accuracy outlier, or
creating an evaluation script. Source and scope rules also apply to pilot and
conversational results: calling a result diagnostic does not make two different
events comparable. Field names, zero values, and agreement with an expected
number are not evidence of event semantics.

For native-FS `user-plus-kernel` evaluation, use the following sources. Paths
in the gem5 column are relative to a case's `oracle/` directory; the audited
hierarchy is produced by `audit_p1_native_response_sideband.py`.

| Metric | gem5 reference | FastSim prediction | Required distinction |
|---|---|---|---|
| Macro-instruction CPI | `kernel_events.json:aggregate.perf_like_cpi_user_plus_kernel`, after cycle/scope validation | `scope_metrics.sum_core_cycles / scope_metrics.perf_like_cpi_denominator_instructions` | Active cycles only; use each simulator's own macro denominator, not UOPs or an average of core CPIs |
| Branch misses | `kernel_events.json:aggregate.pmu_user_plus_kernel.branch_misses`, with `branch_miss_source=taotrace-retired-bpred-v1` | `scope_metrics.pmu.branch_misses` | Original retired BPred redirect outcome; not a retirement-time recomputation |
| L1D tag misses | Audited native `hierarchy.l1d.tag_misses` from `native-summary-core*.json` | `scope_metrics.pmu.l1d_tag_misses` | Data demand tag misses; upgrades are separate |
| Private-L2 tag misses | Audited native `hierarchy.l2.tag_misses` from `native-summary-core*.json` | `scope_metrics.pmu.private_l2_tag_misses` | Private L2, not shared LLC; direct instruction-side requests are a separate domain |
| LLC tag misses | Audited native `hierarchy.llc.tag_misses` from `native-summary-core*.json` | `scope_metrics.pmu.llc_tag_misses` | Excludes upgrades, merged misses and remote supplies |
| Remote supply / upgrade diagnostics | Corresponding native hierarchy level and outcome, with the event dictionary's mapping | Corresponding `scope_metrics.pmu` field, only when that level's semantics match | Do not combine all native levels or reclassify a supply as an L2 hit |
| Trace-processing throughput | No gem5 accuracy reference | `scope_metrics.throughput.user_uops_per_second / 1e6` | Host million measured user UOP/s, not target IPC or validator wall time |

The audited combined hierarchy is at
`scope_metrics.user_plus_kernel.native_ruby_pmu.hierarchy`. It sums user,
syscall, page_fault, irq and scheduler populations; idle is excluded and unknown
kernel accounting must pass the zero-unknown gate. Never construct a combined
scope by blindly summing every entry of `native_ruby_pmu_by_scope`.

**Forbidden cache source:** `taotrace-path-class-v3` fields under
`kernel-events-core*.json:pmu_user_plus_kernel` or
`kernel_events.json:aggregate.pmu_user_plus_kernel`. Names such as `l2_misses`
and `private_l2_tag_misses` there remain TaoTrace proxies. The same document
can be valid for CPI/branch and invalid as a cache reference. No missing-native
fallback, counter folding, or coefficient may turn those proxies into native
truth. A proxy zero does not prove that gem5 never emitted the event.

Use the maintained native validator and its source/conservation checks; do not
duplicate reference selection in a temporary script. Its current scored fields
are branch, L1D, private-L2 and LLC misses. Other events such as DTLB misses,
DRAM transactions or hit rates require their own dictionary and population
checks; this table does not add them to the validator's supported score set.
The executable procedure and release checklist are in section 8.

### 0.1 Timing parameters must match the gem5 collection configuration

Every microarchitectural timing parameter FastSim consumes — at minimum each
cache level's `hit_latency`, request/response/fill latency, controller and
message-buffer latencies, and the TSO store-buffer geometry — must be taken
from the **same gem5 run that produced the reference trace**. The authoritative
source is the collected case's dumped `config.ini` (controller `request_latency`
/ `response_latency`, cache-array `dataAccessLatency` / `tagAccessLatency`,
`mandatory_queue_latency`, directory latency) together with `effective-target.json`
for size/associativity/geometry. `fastsim.cfg`/`config.json` in the case
directory record what was actually used; do not infer parameters from a generic
upstream default.

Hard rules:

- A timing value may enter the model only with a documented mapping to the
  matching gem5 `config.ini` field(s). When one FastSim value represents a sum
  of gem5 edges (for example a controller request edge plus its response edge),
  the mapping must name every edge. A value without such a provenance is an
  unfounded constant and is forbidden.
- It is **forbidden** to introduce, change, or tune any latency/publish/drain
  constant in order to move CPI toward the gem5 number, to borrow a load-path
  latency for a store edge, or to special-case a workload. The demand service
  edge, prepare edge and publish edge must each be modeled from the protocol's
  own edges; a "publish 1→2" style mechanical nudge is not a fix.
- When the gem5 collection geometry changes (TSO mode, buffer size, cache
  sizes/associativity, clock), the matching FastSim parameters must be
  re-derived from that run before any accuracy claim. Reusing parameters from a
  different geometry silently breaks this alignment.
- A latency that is still an end-to-end approximation rather than a direct
  gem5 mapping must be labeled as such (see `gem5-parameter-coverage.md`); it
  must not be presented as a verified hardware/collection value.

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
`tao_trace/uarch_profile.json` matches the actual target in the final gem5
`config.ini`, with `effective-target.json` and the profile bound to that
configuration's SHA256. `request.json:boot_profile.profile` records requested
settings and is not sufficient proof of the effective target. At minimum the
gate must compare core count and frequency, L1/L2 capacity and associativity,
total LLC capacity, LLC bank count and associativity, coherence protocol,
DRAM size/channel count, and address interleaving. A mismatch invalidates
cache-path PMU truth even when the cycle
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

Do not take cache truth from `kernel-events-core*.json:pmu_user_plus_kernel`
or `kernel_events.json:aggregate.pmu_user_plus_kernel` when their source is
`taotrace-path-class-v3`. Even fields named `private_l2_tag_misses` in those
objects are legacy TaoTrace path-class proxies, not native Ruby/SLICC outcomes.
Use the conserved `native-summary-core*.json:native_ruby_pmu_by_scope` through
`audit_p1_native_response_sideband.py` and `run_native_kernel_fastsim_validation.py`.
Missing native evidence must fail closed, never fall back to the path class.
The same kernel-events document remains the selected source for retired BPred
branch misses and active-cycle CPI; the restriction is on cache fields.
For an existing baseline, use the validator's `--source-root` and `--stats-dir`
mode. See [the nab C32 source-selection incident](private-l2-pmu-source-repair-20260916.md).

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
the paired kernel-event report's aggregate statistics. Native-FS cache/branch
comparison must use `tools/run_native_kernel_fastsim_validation.py`, which
selects the native cache source and emits explicit MAPE/P50/P90/P99/WAPE/bias.
Neither tool's success status waives the source, scope or formal-status rules
above. Older median aliases must not replace the explicit P50 fields.

## 8. Native-FS evaluation procedure and release checklist

### 8.1 Freeze the case set and run the gates

1. Record the exact case IDs, count, participant cores, split, trace/manifest,
   measurement policy, warmup/reset boundary, configuration include chain and
   binary identity. Verify common-end and per-core populations against section
   3; a directory named `formal-*` does not prove these properties.
2. Run `validate_fs_oracle_identity.py` for every case. Check both the expected
   case set/count and `valid=true`. Discovery can omit directories missing a
   profile, so success on a smaller discovered set is not enough. Normal
   validation requires `effective-target.json`, `config.ini` and
   `tao_trace/uarch_profile.json`; do not use `--report-only` or
   `--allow-legacy-request-identity` as a formal gate.
3. Run the native validator below. It calls the native sideband auditor and
   checks source, native hierarchy/drain, replay population and LLC-outcome
   conservation. Require the expected cases to be discovered, passed and PMU
   scored, with zero failed/pending cases. Missing native evidence is a failure.
4. Inspect per-case CPI instruction-population flags and the actual per-core
   denominators. Separate mismatched cases from population-aligned results;
   never overwrite either simulator's denominator to force agreement.
5. Verify timing-parameter alignment per section 0.1: for the actual collection
   geometry, check every FastSim cache/controller latency and the TSO buffer
   geometry against the case's gem5 `config.ini` and `effective-target.json`,
   with a named mapping for any summed or approximated edge. A model change that
   adds or moves a latency constant without that provenance fails this gate.

Target identity and complete common-end/provenance review are **separate** from
the native validator's `passed` status; that status does not run or replace all
steps above. Aggregate `instruction_population_match=true` alone does not prove
per-core or per-event identity. Event dictionary mappings remain authoritative:
the current branch/L1D/private-L2 mappings are `proxy`, LLC miss is `diagnostic`.
Passing native-source gates does not promote them to strict hardware PMUs.

### 8.2 Supported commands

Run from the repository root. These commands use the existing 40-case source
set; use the actual case set/count for another experiment. Keep outputs in a
new experiment directory. The identity check is required before either mode.

```bash
python3 tools/validate_fs_oracle_identity.py \
  --result-root tmp/first-core-common-end-20260911/source \
  --output tmp/native-fs-evaluation/identity.json
```

Rescore existing `<case_id>.json` outputs without running the simulator:

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/first-core-common-end-20260911/source \
  --stats-dir tmp/baseline-40-20260916 \
  --output-dir tmp/native-fs-evaluation/rescored \
  --expected-cases 40 --jobs 1
```

Run a new serial replay through the same validation entrypoint:

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/first-core-common-end-20260911/source \
  --fastsim build/fastsim --config configs/gem5-fs-native-kernel.cfg \
  --output-dir tmp/native-fs-evaluation/replay \
  --expected-cases 40 --jobs 1
```

`--source-root` discovers direct child directories containing `request.json`;
`--stats-dir` must contain matching `<case_id>.json` files. Existing matrix
workflows may use `--matrix` instead of `--source-root`. Rescoring fingerprints
the saved prediction and reference inputs, but cannot certify its original
binary/configuration provenance using the currently installed binary. Serial
`--jobs 1` avoids inter-case replay contention; quiet-host, placement and repeat
requirements still apply before publishing a throughput comparison.

### 8.3 Check the report before sharing it

- Retain `identity.json`, `summary.json`, `summary.csv`, `summary.md` and per-case
  `cases/<case_id>/validation.json`; link the exact artifact paths. Inspect
  `reference_source`, `mapping`, `pmu_validation_status` and the fingerprint,
  not only the process exit code or `passed_cases`.
- Label the measurement scope and formal/pilot/diagnostic status. Do not derive
  user-only PMU by copying combined PMU or subtracting an unvalidated kernel
  estimate. Explicitly mark unavailable scopes/events as unavailable.
- Show PMU finite-APE case count, MAPE, P50/P90/P99, WAPE and signed bias.
  MAPE is configuration-equal; WAPE is absolute count error divided by reference
  count. Keep the names and denominators distinct, including in conversation.
  Follow section 5's zero-reference handling rather than dropping difficult cases.
- Show CPI absolute error in cycles/macro instruction next to relative error,
  and CPI MAE in every aggregate table. Use unrounded ratios where possible;
  never use `measured_cycles` containing idle as the active CPI numerator.
- For a hit rate, name the exact numerator/denominator. Native outcomes conserve
  `accesses = hits + tag_misses + permission_upgrades + merged_misses + remote_supplies`.
  `hits/(hits+tag_misses)` and `hits/accesses` are different metrics. L1 misses
  need not equal all L2 accesses because permission upgrades can reach L2.
- State whether a result changes the model, changes the report, or only reruns
  validation. Correcting a reference source is not a CPI/PMU prediction gain.
  Rescoring is not a new throughput measurement; offline audit time is not
  simulator trace-processing time. A `passed` gate is not an accuracy threshold
  or throughput acceptance result.
- Before changing a model for an outlier, verify native event identity,
  source, scope and conservation first, then inspect the model path and obtain
  a controlled mechanism witness. Disabling coherence may change residency;
  that alone does not prove an inclusion or refill defect. Do not tune toward
  the old proxy, or count a remote supply as a resident hit to reduce error.
- When correcting a published report, preserve the raw outputs, mark the old
  summary invalid/superseded with a reason and replacement link, and re-evaluate
  the complete frozen case set. Do not silently replace old numbers or present
  source correction as model improvement.

The [2026-09-16 nab C32 incident](private-l2-pmu-source-repair-20260916.md)
records the counterexample and corrected results; this contract governs future
evaluations. Existing source-selection regression tests are
`tests/test_native_kernel_pmu_validation.py` and
`tests/test_p1_native_response_sideband.py`. Changes to evaluation code must
retain native-vs-proxy disagreement, missing-native failure, scope rejection,
archived-output fingerprint and incomplete-case failure coverage.
