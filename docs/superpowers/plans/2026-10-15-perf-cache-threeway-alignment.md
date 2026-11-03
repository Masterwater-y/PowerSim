# Perf Cache Three-way Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. User approved the written spec and requested implementation; execute continuously without another approval handoff.

**Goal:** Implement independently audited, hardware-bound ten-field cache PMU accounting, prioritizing L1 retired miss, L2 demand miss and core-origin LLC miss.

**Architecture:** Preserve the two-stage simulator. Separate retirement qualification from request observations, retain per-event coverage and independent native evidence, and expose missing speculative/prefetch capabilities instead of legacy fallback. A common event dictionary defines semantics; gem5 and FastSim observers classify their own facts.

**Tech Stack:** C++17 FastSim, gem5 O3/Ruby/SLICC, Python3.11 audits and hardware probes.

**Spec:** [approved design](../specs/2026-10-15-perf-cache-threeway-alignment-design.md).

## Global Constraints

- Preserve existing workspace changes and prior experiment evidence; no commit/push or destructive operations were requested.
- Continue the existing FastSim workspace because the prerequisite collector, runtime, native sidecars and binaries are uncommitted here; snapshot modified inputs before editing.
- No gem5 prediction labels, timings, misprediction labels or request outcomes enter FastSim inputs.
- Match the ten SPR V1.40 raw bindings; keep each event's availability, hardware validation and narrower diagnostic count separate.
- FST total <=20,000,000,000 bytes; each new probe <=300MB; no TeaLeaf trace copy. Compile gem5 with floor(80% current affinity) and GCC11/Python3.11.
- Reuse maintained native validation and independent target identity gates; every CPI comparison includes absolute error and aggregate MAE.

## Review Focus

- Mixed-source multi-UOP instructions must not force independent hardware events into a universal exclusive partition.
- Rejected model trials are rolled back, but accepted target wrong-path transactions remain request events after squash.
- Event windows follow counting edges; late responses and draining must not extend a closed ROI.
- A request lacking a retiring DynInst, such as fetch/prefetch/writeback, must not vanish silently.
- Missing primitive facts or full request classes must leave the affected field unavailable, including an apparently benign zero.

### Task 1: Versioned event contract and independent report gate

**Files:** Create `configs/perf-cache-event-dictionary-v2.json`, `tools/perf_cache_contract.py`, `tests/test_perf_cache_contract.py`; modify `tools/run_native_kernel_fastsim_validation.py` and its maintained tests.

**Interfaces:** `load_dictionary(path=None) -> dict`, `validate_metric(metric, producer) -> dict`, `compare_metrics(prediction, reference) -> dict`, `aggregate_accuracy(reports) -> dict`. Schema `fastsim-perf-cache-v2` / `gem5-perf-cache-v2`; each scope contains exactly the ten fields with domain/unit, diagnostic_value, value/availability, coverage and reasons. The reference is oracle-only and never path-class-derived.

- [x] Write failing tests for exact ten-event bindings, missing native evidence, no legacy fallback, independently unavailable fields, definition/scope/ROI mismatch and raw versus normalized scores.
- [x] Run the focused unittest discovery and observe the intended failures.
- [x] Implement dictionary, validators and maintained `--perf-cache` report/fingerprint integration, including MAPE/max APE/WAPE and zero-reference cases.
- [x] Run focused tests and existing native/retirement validation tests.

### Task 2: Independent L1 retirement event qualification

**Files:** Modify `include/fastsim/perf_retirement.hpp`, `tests/test_perf_retirement.cpp`, `gem5-fs/src/cpu/o3/probe/perf_retirement.hh`, `gem5-fs/src/cpu/o3/probe/tao_trace.cc`, matching overlay, `tests/test_perf_retirement_collector.py`; add focused per-event audit tests.

**Interfaces:** Preserve legacy scalar fields; add per-event qualification/unknown counters and native facts for full forwarding, genuine fill followers and macro completion. V2 selects these counters rather than changing the meaning of legacy fields.

- [x] Add failing cases for mixed-source eligibility, forwarding, hot aliases, incomplete tails, scope crossing and unresolved member facts.
- [x] Observe failures, implement qualification ledger and emit independently scoped source coverage.
- [ ] Verify retired macro populations, warmup and rollback conservation with existing tests; validate hardware event mappings on controlled kernels before setting hardware-validation status.

### Task 3: Native request observer independent of retirement

**Files:** Add focused observer header under `gem5-fs/src/mem/`, matching overlay and tests; connect `RubySlicc_Util.hh`, MESI request messages/controllers, Sequencer, fetch/LSQ and TaoTrace sidecar lifecycle as needed.

**Interfaces:** Versioned native event facts retain origin core, request kind, logical/fragment/attempt/transaction IDs, parent and per-level lookup outcome. Aggregate into `oracle/perf-cache-coreN.json`; sidecars bind common boundaries and source/target identity. Actual unknown classes are reported explicitly.

- [x] Write standalone literal tests for cold data/RFO/code, local L2 miss followed by remote supply, true merge/retry, window crossing and writeback without a retired instruction.
- [x] Observe failures and implement observer state, identities, independent per-layer classifications and coverage.
- [x] Wire real protocol edges, retaining speculative/fetch/prefetch/writeback populations without the old committed `TraceValid` gate.
- [ ] Compile observer tests, synchronize overlay, build gem5, and validate a bounded real capture before marking any native hook complete.

### Task 4: FastSim request observer and accepted replay integration

**Files:** Add `include/fastsim/perf_cache.hpp`, focused C++ tests; modify `include/fastsim/types.hpp`, `src/simulator.cpp`, `src/main.cpp`, CMake.

**Interfaces:** Local request facts and scoped counts travel with `CoreCounters` rollback. Classification uses local-level lookup state and request type; reports expose full-event availability separately from existing narrower demand estimates. No values are copied from native sidecars.

- [x] Add failing tests for independent L2/LLC outcomes, read/RFO/code/prefetch masks, rejected attempts versus accepted squash, rollback/carry and begin/end edges.
- [ ] Implement request ledger and observers on actual replay paths, fetch and buffered-store transitions; retain complete origin/core/context information.
- [x] Expose versioned ten-field output with per-event missing capabilities; prove counter-only changes preserve existing target timing on frozen sources.
- [x] Run direct C++ tests and dedicated PMU integration tests.

### Task 5: Request-generation coverage and hardware qualification

**Files:** Add/extend maintained hardware microbench and runner under `tests/workloads/` and `tools/`; use existing static/functional metadata for model-generated request providers with an explicit capability interface.

**Interfaces:** Provider describes which code/data/prefetch classes and functional dependencies it can generate. No fabricated addresses or gem5 oracle replay. Unknown speculative addresses or unmodeled prefetch streams fail the affected complete-event coverage gate.

- [ ] Add tests distinguishing missing functional state, supported generated requests, model rollback, independent event qualification and writeback direction.
- [ ] Implement supported providers incrementally from local functional facts, with bounded work and independent runtime accounting.
- [x] Run pinned non-multiplexed hardware probes for L1/FB/forwarding and working-set L2/LLC/RFO/writeback classes; retain noise, overhead and unresolved mappings.
- [x] Freeze capability results without promoting incomplete fields to strict merely because definitions agree.

### Task 6: Bounded multicore validation, review and results

**Files:** Add curated results document and update reporting contract/spec status; evidence in `tmp/perf-cache-threeway-20261015/`.

- [x] Run all required C++ tests and the appropriate Python suite; list pre-existing environment failures separately.
- [x] Check actual FST budget before any bounded capture. Run source/identity/request/retirement gates and expose unsupported old sources as rejected for v2.
- [ ] Compare supported events independently across hardware/gem5/FastSim; report source gaps without inventing ten-field accuracy. Measure throughput only with same-source sequential paired runs.
- [x] Obtain final fresh review, fix important findings with regression evidence, and record completed versus remaining capabilities in the results/ledger. Never describe full three-way alignment as complete while required populations or mappings remain unverified.

## Current delivery status — 2026-10-15

Independent observers, dictionary, audit/report mode and real capture are implemented.
The first real capture exposed a CorePair-versus-physical-core mapping defect; the
observer-only fix has RED/GREEN coverage and a second successful capture with
nonzero data/RFO/code request identities. The first capture is excluded from the
accepted request evidence set.

Task 3's real build/capture ran successfully, but full native hook coverage (notably
unqualified protocol-generated prepare/prefetch identities and request context)
is incomplete. Task 4 emits actual self-generated request diagnostics with rollback;
complete event-clock context, generated speculation/prefetch and ROI are not implemented.
Task 2 remains incomplete for target ISA/source qualification. Task 5 has hardware
probes and conservative capability gates, but no complete functional-state provider.
Task 6's maintained gates, code review and same-input 5-pair throughput ran; full
three-way supported-event accuracy remains unavailable. Do not close these tasks
because test or source-validation commands passed.

See [implementation and evidence](../../perf-cache-threeway-implementation-results-20261015.md)
and [missing functional source interface](../../perf-cache-functional-source-interface-20261015.md).
