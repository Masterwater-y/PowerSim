# LBM Mixed-Service Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair LBM's modeled memory-service/SQ chain using replayable RD/WB services, persistent source-aligned controller state and actual ordinary-store admission, then measure the result without changing the maintained default prematurely.

**Architecture:** Preserve the two-stage Q=1024 producer/shared/response engine. Build a small, independently testable mixed controller whose pending requests and completed service identities survive batch boundaries, integrate it transactionally, and connect final store admission to absolute response/SQ ownership. Controller-only, admission-only and combined interventions remain separate; none is promoted by coverage counters alone.

**Tech Stack:** C++17, existing CMake/fastsim_tests, Python analysis of frozen FST/gem5 artifacts.

**Spec:** `docs/lbm-common-end-error-audit-20260912.md`, sections 4–6; structural constraints from `docs/lbm-two-stage-mechanism-repair-design-20260911.md`, sections 4–7.

## Global Constraints

- Current ordinary-load=3 and response-to-ready=1 remain unchanged.
- Maintain time-epoch/Q=1024 and two-stage execution. No per-UOP global causal_read replacement and no added unconditional third full feedback pass.
- No workload/PC-specific compensation, oracle timing injection, fitted latency scaling, input truncation or new gem5 collection without a demonstrated need.
- Use the existing common-end dataset and `user-plus-kernel` scope; each CPI relative-error row also reports absolute CPI error, and aggregate accuracy reports include CPI MAE.
- Native RD/WR semantics come from the local frozen gem5 sources. `open_adaptive` scans the selected direction's admitted queue, not both directions. Read-empty starts a write turn only when pending writes are strictly above the low threshold; write-turn continuation uses gem5's low/min-write hysteresis.
- Cross-Q frontier means exclusive visibility: an arrival at the frontier belongs to the next submission batch. No new arrival may change a selection already committed below the frontier, even when that selection reserves future PRE/ACT/RD/WR commands.
- Preserve existing dirty worktree changes. Snapshot is `tmp/lbm-mechanism-repair-20260912.OEvBcu/before/`. Do not commit, push, delete historical artifacts, or change immutable profile files.
- This repository's managed `.git` requires the existing temporary metadata bound to the current work tree. Keep all execution/review artifacts in the above project-owned `tmp` directory; no host `/tmp` or new unrelated repository.
- Default baseline: `tmp/load-ready-default40-20260912.Ydiy65/fastsim`; configuration snapshot beneath the same directory; baseline SHA `f704f0bf52f95ade02a6d9eb5f988de93b290f2ca6f109b9425d2265db822e73`.

## File responsibilities

- `include/fastsim/mixed_dram.hpp`, `src/mixed_dram.cpp`: typed service identity, persistent controller queues, command calendar, service completion and copyable transaction state; no trace or gem5-file dependency.
- `tests/test_mixed_dram.cpp`: standalone, literal controller tests using the production controller (not mocks).
- `include/fastsim/config.hpp`, `src/config.cpp`: explicit experimental mode and source-cycle RD/WR fields, fail-closed incompatible-mode validation.
- `src/simulator.cpp`: real dirty-write service ownership, transactional adapter, actual admission/response and bounded invalidation integration.
- `include/fastsim/types.hpp`, `src/main.cpp`: activation/ownership/conservation and fallback/recompute accounting; no default per-UOP logging.
- `tests/test_main.cpp`, `tests/test_response_completion.cpp`, `CMakeLists.txt`: test registration, production integration and generic/materialized parity.
- `configs/gem5-exp-lbm-mixed-service.cfg`: opt-in experiment only; maintained alias unchanged pending acceptance.
- `tmp/lbm-mechanism-repair-20260912.OEvBcu/`: task briefs, progress, before/after diffs, run configurations, logs, paired evidence and results.

## Task 1: Independently tested persistent mixed controller

**Files:** Create `include/fastsim/mixed_dram.hpp`, `src/mixed_dram.cpp`, `tests/test_mixed_dram.cpp`. Do not edit the large simulator or maintained profiles in this task.

**Interfaces:** C++ namespace `fastsim`; the header exposes `MixedDramController`, `MixedDramConfig`, `DramServiceId`, `MixedDramRequest`, `MixedDramCompletion`, `MixedDramStats`.

`DramServiceId` contains core, sequence, fragment ordinal and kind (read/writeback), with value equality. `MixedDramRequest` contains ID, arrival cycle and physical cache line. `MixedDramCompletion` retains the same ID and arrival plus selection, command and response cycles, row-hit flag. `MixedDramConfig` contains `DramConfig` and the extra write/direction timings expressed in the same integer-cycle unit; it never reads gem5 files. `MixedDramController(config, line_size)` supports `submit(request)`, `advance(exclusive_frontier)` returning newly selected completions, and `stats()`. Controller state must be safely copyable for speculative transaction/rollback. Any result-returning API must retain ownership until consumed and must not use batch-local vector indices as persistent identities.

- [x] Write tests first. Demonstrate semantic RED with a minimal interface stub if the initial missing-interface compilation fails, then implement actual behavior. Keep the RED command/output and why the fixture catches the defect.
- [x] Test literal read-empty threshold: one channel, write capacity 4, low 50%, minWrites 1, three WB arrivals at cycle 0 and no reads. After `advance(1000)`, all three have drained through the low/min-write hysteresis, exactly three IDs complete and occupancy is zero. With two writes, no turn starts and both remain pending.

```cpp
// Hand-derived threshold: low=2. Start requires 3>2; the turn ends only
// when queue + minWrites < low, i.e. 0+1<2 after all three writes.
check(three.stats().writes_serviced == 3, "read-empty write progress");
check(two.stats().writes_serviced == 0, "at-low writes stay pending");
```

- [x] Test a late future row hit cannot replace an earlier committed selection: compare submitting a full stream before advance with submitting only arrivals strictly below successive frontiers. Arrival ties at a frontier are all submitted before the next advance. Check literal service order and exact completion equality.
- [x] Test pending reads and pending read responses both consume read capacity; capacity releases at DRAM-ready, not selection or external frontend/backend response. Future writes above a frontier must not be admitted early.
- [x] Test same-bank WR→PRE includes write data ready plus tWR, and test RD→WR/WR→RD for same/different bank group and rank. Use explicit small timing constants with independently calculated command cycles; retain expected values in tests, not a second copy of the production formula.
- [x] Test `open_adaptive` on the selected direction: an opposite-direction queued row hit cannot keep the row open, while a same-direction hit can; row access cap causes one precharge.
- [x] Test single-stream vs split-at-frontier execution, multi-channel independence, snapshot-copy rollback, duplicate IDs, invalid lines, backwards frontier, and zero/invalid geometry. Service count + pending count must equal admitted count.
- [x] Implement the minimum persistent queues, direction state and command calendars to satisfy the fixtures. The selection event advances independently of a reserved future command. Never drain everything merely because one batch's read list ends.
- [x] Run a focused standalone test build with `c++ -std=c++17 -O2 -DFASTSIM_MIXED_DRAM_STANDALONE -Iinclude src/mixed_dram.cpp tests/test_mixed_dram.cpp -o tmp/lbm-mechanism-repair-20260912.OEvBcu/mixed_dram_tests`, then run that exact binary. Keep the test entry point behind `FASTSIM_MIXED_DRAM_STANDALONE` so CMake can later link its test function into fastsim_tests.
- [x] Self-review and independent spec/code review before integration. No commit; deliver a diff against nonexistent/new files plus recorded RED/GREEN evidence.

## Task 2: Typed dirty-WB capture and transactional controller integration

**Files:** Modify config/types/main/simulator, CMake and integration tests; create opt-in configuration.

**Consumes:** Task 1's controller and owned absolute completions. **Produces:** explicit RD/WB capture and whole controller snapshot/restore; selected service IDs appear in event feedback and downstream ownership diagnostics.

- [ ] Add a failing simulation fixture with a tiny dirty LLC and two competing cores. Assert each actual LLC dirty victim creates exactly one WB, no WB is created for private victim absorption, and a restored transaction neither duplicates nor loses WB services.
- [ ] Add experimental config parsing tests that reject illegal combinations instead of disabling existing guards. The maintained alias remains off; all existing profiles load unchanged.
- [ ] Capture actual dirty writeback side effects at `handle_llc_eviction` and any private cascade, carrying triggering request ID and distinct WB identity. Include them in the same rollback transaction as cache, directory and timing state.
- [ ] Replace the experimental controller solve with mixed RD/WB service submission, including pending ownership across Q. Do not simply set legacy `descriptor.replayable=true`. Return explicit reason when a non-expressible atomic/alias/visibility condition requires fallback.
- [ ] Add counters for admitted/serviced/pending RD/WB, input frontier and selected frontier, dirty-sideeffect fallback, accepted/rejected transactions, and recomputed work. Existing aggregate counters must remain conservative and meaningful.
- [ ] Verify option-off full-test compatibility and generic/materialized equality. Run current full C32 once in controller-only mode with the known windows audited; controller-only success is not end-to-end closure acceptance.

## Task 3: Actual store admission and response-owned SQ closure

**Files:** Modify `src/simulator.cpp`, shared feedback types and focused response tests.

**Consumes:** Task 2's transactional service IDs and absolute responses. **Produces:** explicit ordinary-store final admission/response/SQ lifecycle with bounded restoration and no stale post-solve controller rewrite.

- [ ] Add failing tests for a store held behind an older store, a store held behind commit, and two fragments returning at different times. SQ releases at the true latest owned response, not final send plus a latency evaluated at a former origin.

```cpp
// Fixture has independently supplied request readiness and a real shared
// controller; these are assertions on exported service/slot ownership.
check(service.admission >= store.commit, "no pre-commit ordinary store");
check(service.admission >= prior.response, "ordinary TSO predecessor");
check(store.sq_release == last_fragment.response, "SQ owns actual response");
```

- [ ] Add a fixture with no RAW dependency where SQ/ROB capacity propagates an earlier service response; compare bounded restored suffix with the full-feedback reference, including a cross-Q predecessor.
- [ ] Do not re-enable old `response_shared_service_constraints` unchanged: its one-core component ownership and dirty/carried exclusions miss C32's critical services. Do not use `store_post_commit_request` alone: accepted arrival schedule must be retained, request resources must be acquired at actual send, and later FRFCFS may not invalidate that schedule without recertification.
- [ ] Connect final commit/TSO/resource admission to service submission and absolute response consumption inside one service/response closure. Preserve pending IDs and entry state at Q boundaries. Restore only affected resource groups and core suffixes; record actual whole-prefix fallback work if no smaller checkpoint exists.
- [ ] Run admission-only and combined modes separately; verify original C13 315 DRAM store witnesses and 430 SQ owners really enter the new service path. No promotion if they remain bypassed, even when other coverage increases.
- [ ] Run the default full build/tests and review the integration diff, including off-mode unchanged behavior.

## Task 4: Paired workload acceptance and handoff

**Files:** analysis scripts/results under the task's tmp directory; a curated repair/validation report and optimization decision entry.

- [ ] Freeze candidate binary and complete config include chain; save hashes. Run full current C32 input, not cold 10k windows. Reuse the prior identity-verified gem5 stage/native data only within the current authoritative record bounds.
- [ ] Recompute C1 2M/8M and C13 3127618..3137618 matched service distributions, send/admission/response/SQ edges, clipped occupancy, fallback reasons and stage conservation. Report exact key-service coverage and unresolved paths.
- [ ] Run LBM C4/C8/C16/C32; keep all denominator/trace identity gates. Include signed error and absolute CPI error per case and per-core cancellation diagnostics.
- [ ] If mechanisms and LBM gates pass, run all 10 workloads × C4/C8/C16/C32. Report CPI MAE, MAPE, Type-7 P50/P90/P99 APE, absolute tails, signed bias and regressions; don't replace the 40-case headline with a four-case subset.
- [ ] Measure throughput separately with audits off and a quiet, sequential comparable-host run. Do not claim speed from concurrent diagnostic wall times.
- [ ] Final fresh build and fastsim_tests, source/config/hash/identity/conservation verification, and independent full-diff review. Leave uncommitted changes and report whether the candidate is accepted, remains experimental, or fails a named gate. Never claim full repair from foundational tests alone.

## Progress and rulings

- Phase-one handoff: Task 1 and the independently reproduced existing post-commit certificate sub-fix are implemented and reviewed. Tasks 2/3 are NOT complete. C32 default/control and before/after postcommit checks completed; the latter has only 1/31308 accepted outer epochs and no patch CPI change, so no wider accuracy promotion. See `docs/lbm-mixed-service-phase1-20260912.md` and task-local `phase1-verification.json`.
- Integration prerequisite refinement: replace the plan's assumed synchronous adapter with explicit unresolved shared fill/service ownership, separate submission/retirement cursors and per-core continuation before proceeding. A bounded complete-Q retry only certifies all-resolved cases and cannot safely cover the observed cross-Q stores; see `frontier-integration.md` in the task directory. No forced drain or placeholder response was implemented.

- Baseline build and `./build/fastsim_tests` passed before edits.
- User approved implementing the prior report; this plan makes that work executable, not a new request for design approval.
- Ruling: source inspection overrides the prior report's dual-queue adaptive wording; implementation uses the selected direction's queue. The report has a dated implementation-review correction. Cost if wrong: revisit controller tests against the actual frozen source, not tune CPI.
- Ruling: work in the existing managed FastSim directory with a before snapshot and project-local temporary metadata; no new worktree or automatic commits. Cost if wrong: retain/review task-only diffs against the snapshot without disturbing older modifications.
- Ruling: progress is saved under project `tmp`, overriding skill scratch-directory defaults to follow this repository's artifact-location requirement. Keep review evidence; do not delete it on completion.
