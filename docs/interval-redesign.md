# Interval core and shared-state redesign

> This document records the implemented frontier baseline.  The current
> research design, staged implementation status, and C4--C32 validation plan
> are in [Conflict-Certified Sparse Weave](conflict-certified-sparse-weave.md).

## Baseline and objective

The current event-at-a-time scalar model is retained only as a reproducible
baseline. On the complete TCSim v28.1 seed0 corpus its mean absolute UOP-CPI
error is 33.858% at C4 and 30.703% at C8. The redesign is not accepted merely
because it is interval based; it must improve both timing and the cost of
shared-state replay without hiding event-order failures.

The functional Parquet trace already contains the fields needed for a real
window model: `op_class`, `n_src`, `n_dst`, `producer_dists[4]`,
`producer_classes[4]`, branch outcome, serializing markers, and physical data
addresses. Timing/oracle fields such as `issue_tick`, `commit_tick`,
`path_class`, and `coh_oracle` remain forbidden as simulator inputs.

## Proposed execution protocol

The initial interval is 256 retiring UOPs, matching TCSim's useful context
length. It is an upper bound on lookahead, not an instruction-count barrier.

```text
per core: decode up to 256 functional UOPs
    -> dependency/FU/ROB lower-bound schedule
    -> proposed retire prefix + memory issue lower bounds
all cores:
    -> choose one global virtual-time horizon
    -> accept only each core's provably covered prefix
    -> batch-weave accepted shared-memory events
    -> feed contention/path latency back to core clocks
    -> audit path-altering conflicts
    -> commit, selectively replay, or shorten the interval
```

### 1. Per-core bound phase

For each core, build an instruction-driven schedule with:

- 8-wide fetch/decode/rename/dispatch/issue/commit;
- 192-entry ROB, 64-entry IQ, 32-entry LQ, and 32-entry SQ;
- producer-distance edges that cross interval boundaries;
- gem5 operation-class latency, pipelining, and functional-unit capacity;
- configurable fetch/decode/rename/writeback bandwidth and forward stage
  delays from the effective gem5 O3 configuration;
- in-order retirement, serializing UOPs, and replayed branch penalties;
- functional virtual-page replay through a fully-associative DTLB and finite
  page-walk service lanes;
- multiple outstanding independent memory operations.

Memory operations initially receive a private-L1 lower-bound response. The
bound phase emits their issue lower bounds and dependency edges; it does not
finalize shared-cache or DRAM timing.

### 2. Global horizon

The coordinator chooses a single horizon covered by every active core's
lookahead. Each core commits the longest monotonic retirement prefix within
that horizon. A core may therefore consume fewer than 256 UOPs and retain the
rest, as in TCSim's global-time rollout.

For shared-memory safety, coverage is based on dispatch/issue lookahead plus a
ROB tail, not only the 256th retirement time. This prevents an unexamined UOP
from issuing a memory operation before an already accepted event from another
core.

### 3. Batch weave

Private L1/L2 accesses are evaluated per core during the bound phase and only
events that leave the private domain, request write permission, or evict a
directory-visible line enter the shared batch. The batch is ordered by
`(lower-bound issue cycle, core ID, per-core ordinal)` and applied to the
directory, CHA, LLC, NoC, and DRAM models. Returned delays are propagated
through the per-core dependency/retirement graph at interval end.

This follows Zsim's useful separation: parallel contention-free bound work,
then a global weave of the smaller shared event set. It also adopts TCSim's
single global virtual time and variable accepted prefix.

The current `interval_weave` implementation is an intermediate form: it does
the global horizon, variable prefix, gather/sort, and end-of-step feedback,
but still evaluates all private tags in the coordinator. Parallel private
preview plus escape filtering remains part of stage 4 because it requires the
rollback certificate below.

### 4. Path-altering conflict certificate

Parallel private-cache preview is valid only if the shared weave does not
invalidate or evict a line before a later previewed private hit. Each interval
therefore records:

- per-core ordered line accesses and private set victims;
- directory ownership/read/write sets;
- invalidation and inclusive-eviction targets;
- proposed and feedback-adjusted shared-event order.

If an invalidation, ownership transfer, or eviction changes a recorded later
path, only affected cores are restored from cache transactions and replayed.
Repeated failure halves the horizon. The interval commits only with a clean
certificate. Counts of replays, shortened intervals, order inversions, and
path changes are mandatory output statistics.

Selective conflict replay is the research hypothesis worth evaluating here:
it may retain explicit PMU causality while avoiding both global event-at-a-time
coordination and full-phase rollback. It is not an innovation claim until the
accuracy, replay rate, and speedup are measured.

## Implementation stages

1. **Implemented:** canonical v4/v5 preserves functional dependency/op-class
   fields plus an opaque virtual-page token beside the physical address,
   while retaining read compatibility with v2/v3.
2. **Implemented as `interval_bound`:** add a stateful lower-bound interval
   core and validate compute-only C4/C8 workloads before coupling memory. The
   four-workload mean error is 8.27%; integer ALU and FP ALU are below 0.2%,
   while SIMD and integer divide remain about 16% off.
3. **Implemented as `interval_weave`:** add one global time, variable accepted
   prefixes, batch gather/sort, dependency-aware memory feedback, 16-entry
   L1D plus configurable L2/LLC miss backpressure, configurable O3 stage
   widths/delays, DTLB/page-walk timing, and explicit order-inversion counters.
   The pre-DTLB full-suite C4/C8 mean error is 11.853%/14.990%; P90 is
   19.098%/33.666%.
4. Add parallel private preview, the conflict certificate, and selective
   replay before making any cross-core order or coherence claim. The current
   audit finds 127,426/153,761 same-line reordered pairs at C4/C8, so this
   stage is demonstrably required.
5. Run the same 23-workload C4/C8 suite after every stage.

## Acceptance gates

| Property | Gate |
|---|---|
| C4/C8 UOP-CPI | Mean absolute error at most 6%; P90 at most 10% |
| Heldout UOP-CPI | Mean absolute error at most 12% |
| Per-core CPI | MAPE at most 7% |
| Aggregate PMU | No regression beyond 1% WAPE for L1D/L2/CHA/branch |
| Coherence | Direct validation of invalidation, upgrade, and remote-supply counts |
| Ordering | Report inversion/path-change rate against gem5 event diagnostics |
| Performance | At least 1 MIPS at C8 and measured speedup over scalar replay |

The TCSim v29 seed0 C4/C8 result (5.07%/4.62% mean UOP-CPI error) is the
timing reference. A faster result with materially worse CPI does not pass.

The current full-suite `interval_weave` result is 11.853%/14.990%, with median
throughput 15.6M/11.5M UOP/s. Aggregate PMU WAPE stays below 0.27% in the
validated scopes, but CPI, per-core CPI, ordering, and coherence gates still
fail. This is evidence that global-time batching and correct clock units are
necessary, not evidence that conflict replay or missing frontend/TLB timing
can be skipped.
