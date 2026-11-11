# SPR cache observation and retired PMU classification design

## Goal and scope

Eliminate unclassified members for the declared C4 workload-task retired-load
L1/L2/L3 miss population, while reporting native hierarchy request diagnostics
separately. Align the retired event contract with Intel SPR's documented event
definitions. An estimate must be identified as such; an
unobserved fact cannot be relabeled as a measured hit or miss.

User update (2026-11-07): do not collect physical perf measurements for this
repair. Use Intel's official documentation to confirm the event contract.
This supersedes the original physical-microkernel validation requirement.
Previously collected raw files are retained as historical observations and
are not acceptance evidence for this repair.

## Approaches

1. Force a default value for every unknown: fast, but cannot meet the requested
   physical perf semantics and would hide missing source facts; rejected.
2. Reconstruct transaction service and retirement membership independently,
   with an explicitly identified approximation for cases where the hardware
   rule is not fully specified by documentation or derivable from native facts;
   selected.
3. Report only bounds: useful for diagnosing existing captures, but does not
   satisfy the requested complete classification; retain as a diagnostic.

## Architecture

* Request diagnostics count actual Ruby/SLICC layer transactions and conserve
  their typed outcomes, including demand, prefetch, ownership and writeback.
  Explicitly identify requester, level, path and ROI; do not rename request
  counts into retired PMUs.
* The retired PMU observer joins actual demand fragments and attempts to their
  cache service and retirement boundary. Extend the existing oracle-only
  prefetch generation lineage through L1 prefetch, L2 prefetch and LLC, and
  link late demand followers explicitly. Handle multiple followers, retries,
  invalidate/cancel, mixed fragments, boundary crossing, forwarding and device
  paths. Never feed oracle facts into FastSim inputs or target timing.
* Classify each eligible retired macro separately for L1, L2 and L3. Preserve
  the observed facts and classification reason, and emit separate exact and
  estimated counts. Require every member to be classified in the requested
  workload set; unsupported populations are an explicit failure, not zero.
* Check Intel's official SPR event table for D1/08, D1/10, D1/20 and D1/40,
  and its glossary for retirement and request-event semantics. Record the
  exact definition and source for each rule. Definitions do not specify every
  follower/forward/split behavior; those rules remain explicitly estimated or
  unconfirmed. Do not infer all followers inherit a parent's miss, or treat
  document confirmation as empirical hardware equivalence.

## Validation and acceptance

Unit and integration tests must exercise each transaction lifecycle and
counterexample, ensure per-level and per-core conservation, and verify that
observer instrumentation changes neither functional stream nor cache/timing
behavior. Run the required FastSim build and test gate and gem5 build following
the workspace workflow. Then capture and audit FLAC, Graph500, Marian and XZ
at common end, with zero unclassified eligible retired members per event and
separate exact/estimated totals. Independently check target identity, source,
per-core population, native hierarchy conservation and window boundaries.
Documented population, event encoding and retirement phase must match Intel's
official definitions. Report undocumented boundary rules separately; hardware
equivalence remains unverified without empirical qualification. No physical
perf collection is required or authorized for this repair. Report CPI absolute
error and MAE with any CPI accuracy table, and measure throughput independently
on identical inputs.

## Official documentation check (2026-11-07)

Sources: [Intel SPR core event table](https://perfmon-events.intel.com/platforms/sapphirerapids/core-events/core/),
[Intel event glossary](https://perfmon-events.intel.com/glossary/), and the
[official SPR JSON](https://raw.githubusercontent.com/intel/perfmon/main/SPR/events/sapphirerapids_core.json).
The checked JSON is V1.40, published 09/09/2026, SHA256
`95caad89cbe4c96647a540897251e1998edc7eee8efb2dc84a5b13f7d94ce102`,
matching `configs/retired-load-cache-events-v1.json`.

| Event | Event/umask | Official PublicDescription (verbatim) |
| --- | --- | --- |
| MEM_LOAD_RETIRED.L1_MISS | D1/08 | Counts retired load instructions with at least one uop that missed in the L1 cache. |
| MEM_LOAD_RETIRED.L2_MISS | D1/10 | Counts retired load instructions missed L2 cache as data sources. |
| MEM_LOAD_RETIRED.L3_MISS | D1/20 | Counts retired load instructions with at least one uop that missed in the L3 cache. |
| MEM_LOAD_RETIRED.FB_HIT | D1/40 | Counts retired load instructions with at least one uop was load missed in L1 but hit FB (Fill Buffers) due to preceding miss to the same cache line with data not ready. |

All four entries have `Speculative=0`. The glossary's AtRetirement category
requires instruction/uop commit; actual speculative cache requests belong to
the separate diagnostic population. An architectural instruction with multiple
uops must not automatically contribute multiple retired instruction counts.
The L2 description does not explicitly repeat the L1/L3 any-uop rule; an
implementation applying that rule to L2 must identify it as an interpretation.

These definitions do not specify complete overlap, prefetch-follower membership,
store-forwarding, retry selection or mixed split-fragment rules. A unique
prefetch transaction join establishes service lineage, not proof that the
follower increments every miss event of its parent. Preserve facts separately
from estimated membership and keep `hardware_equivalence=unverified`.

## Initial implementation and verification scope

The native observer now conserves live transactions across ROI boundaries,
including releases and superseded attempts, freezes closed-window counts, and
separates per-level writebacks/invalidations from unassigned whole-run LLC
diagnostics. Host live-transaction storage is bounded at 65,536.

The diagnosed prefetch lineage API supports eight distinct followers per
generation and idempotent repeated attachment of an identical identity/attempt.
Conflicting identity/attempt joins, storage overflow, invalidation and ambiguous
responses reject the affected population explicitly. Production finish hooks
emit each follower's witness separately without converting lineage into PMU
membership.

Focused regression coverage checks multi-follower production hooks, conflicts,
overflow, invalidation, response ambiguity, ROI conservation/freeze and absence
of silent PMU reclassification. Unit coverage does not establish native timing
neutrality or four-workload unknown-zero acceptance. Those integration and
common-end capture gates remain required before declaring the repair complete.

Completed checks:

* `cmake --build build -- -j16` and `./build/fastsim_tests` passed. The latter
  requires `/opt/gcc-11/lib64` in `LD_LIBRARY_PATH` in this agent environment.
* `test_spr_native_prefetch_lineage.py`: 9 passed, including full native
  observation-table overflow and recovery without loss of existing facts.
* `test_native_pio_lifecycle.py`: 2 passed.
* `test_spr_native_prefetch_unknown.py`: 1 passed.
* `test_retired_cache_qualification.py`: 4 passed.

## Constraints

Preserve existing uncommitted workspace changes and the parallel two-stage
FastSim framework. Diagnostic observation must be bounded, avoid changing
simulated protocol decisions and remain independent from FST model input.
No workload-specific coefficients, path-class fields as native cache truth,
or silent unknown-to-zero conversion. Existing captures cannot reconstruct
facts that were never collected; retain them and recapture when necessary.
