# Task PMU population repair

User authorized implementation after the diagnosed full-physical/legacy-reference
population mismatch. Preserve existing dirty work in place; no commit/push.

## Contract

Complete physical FST remains the execution input. The primary new PMU population
is `workload-task`, using the same `task-oncpu-switch-entry-v1` owner boundary as
active CPI. This is an explicitly new population, not a relabeling or expansion
of the old idle-excluding user-plus-kernel PMU contract. Legacy physical counts
remain diagnostics; hardware-perf event equivalence remains unverified.

Task membership comes from the existing functional thread-state plan: the
switch anchor itself belongs to the old owner, subsequent records to the new
owner. No gem5 source timing, cache outcome or oracle classification is fed into
FastSim. Initial ownership and complete common cut must be certified; missing
facts fail closed. Task identities may be discovered later, so the producer
aggregates raw owner IDs before selecting the final measured TASK set.

## Producer

gem5 must observe every retained measured commit from the common start, including
kernel prefixes before the first user commit. Legacy CPL output may remain
separate. Committed branch truth retains the original BPred redirect bit. Data
cache truth is native Ruby/SLICC hierarchy. One memory identity is consumed once;
late responses retain its frozen source owner. Separate full/legacy populations
must conserve independently, and native target drain must complete.

An oracle-only per-core task PMU sidecar provides per-owner instruction/UOP/
memory/branch counters and native cache outcomes, common start/end and source
ordinal coverage, final TASK set, collection failures and conservation evidence.
The validator must prove these facts against functional ownership, not merely
trust sidecar availability flags.

## Consumer

FastSim adds an independent task-PMU accumulator. All physical instructions and
requests still update hardware state. At each record the immutable source owner
selects the reporting population; memory requests carry that membership through
weave, replay and delayed service. Counter snapshots/rollback include the new
subset, and accepted events count exactly once. Instruction-side/page-table
requests remain outside the existing data-demand cache PMU definitions.

Expose task PMU separately from existing physical `scope_metrics.pmu`, with
explicit population and ownership contract. Validate task+other conservation
for supported counters. Ordinary no-scheduler replay remains unchanged.

## Validation and constraints

Maintain the native validator and separate target identity gate. Missing owner,
branch/native evidence, source mismatch, unknown coverage and incomplete drain
must not score. Report CPI absolute error and MAE, elapsed separately, PMU event
mapping and MAPE/WAPE separately. Do not tune model latency to change errors.

Run mechanism tests for kernel prefix, after-record ownership, non-task cache
effects, late response after switch, rollback and missing/malformed evidence.
Then bounded real C4 capture/replay: new FST <=300MB, total tmp FST <=20GB.
gem5 compile uses floor(80% affinity cores), explicit GCC11/Python3.11. Never
replace a binary during a run using it. Existing TeaLeaf FST must not be copied.
