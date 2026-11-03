# LBM Cross-Q Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Preserve the task-entry snapshot; do not commit or push.

**Goal:** Connect real cross-Q memory services and ordinary-store response/SQ ownership to production, then validate the known LBM witnesses.

**Architecture:** Retain the two-stage producer/shared/core engine. Split shared submission from response publication, retain core live fragments and detached stores, and advance only certified shared frontiers.

**Tech Stack:** C++17, CMake/fastsim_tests, existing frozen FST/gem5 evidence.

**Spec:** docs/superpowers/specs/2026-09-14-lbm-cross-q-service-design.md

## Global Constraints

- Keep ordinary_load_latency=3, load_response_to_ready=1 and Q=1024; maintained defaults and historical profiles do not change.
- Preserve interval_weave/time_epoch and parallel core work; no global per-UOP causal_read replacement or unconditional third whole-prefix pass.
- No unknown response is published as zero, maximum integer, old latency or forced future drain.
- Real dirty LLC WB, shared/cache state, service identities and PMU are transactionally exactly-once.
- Unknown services survive Q boundaries; ordinary store retirement does not wait for its own response, but SQ release does.
- Preserve younger independent requests and non-RAW capacity edges.
- Use current common-end user-plus-kernel full inputs, functional warmup excluded; include absolute CPI error and CPI MAE.
- Preserve existing dirty changes, no commits/pushes/deletions. Task artifacts: tmp/lbm-cross-q-repair-20260914.2r2LRW/; snapshot: before/ there.

## Task 1: Explicit experimental configuration and output contract

Files: include/fastsim/config.hpp, src/config.cpp, src/main.cpp, tests/test_cross_q_config.cpp, CMakeLists.txt, tests/test_main.cpp.

Interface: SimulatorConfig::cross_q_service_mode is a string, default "off".
Configuration key is core.cross_q_service_mode; allowed values are "off",
"controller", "admission", "combined". JSON configuration exports that exact
key without the core prefix. No independent timing knob is changed implicitly.

- [x] Write tests that parse temporary config files and call validate(). Unknown values are rejected; default remains off. Non-off requires interval_weave/time_epoch, response_queue_feedback, response_sparse_scoreboard, needs_tso and memory_exposure=1.0. Reject event-only, causal-block, pending-fill, private-read/shared-service experiments, independent store_post_commit_request, causal/response retime, corrected suffix carry, source Sequencer coalescing/admission and DTLB hierarchy walk. Existing materialized and parallel feedback remain permitted. Reject nonidentity reference/core frequencies and DVFS overrides until supported.
- [x] Observe the expected parser failure for the new mode before implementing.
- [x] Add parsing/validation/output fields and tests for all four values, supported default-profile inheritance, incompatible combinations and option-off compatibility. Do not modify simulator.cpp in this task; main owns its dispatch and runtime guard until production integration is complete.
- [x] Register tests with the existing executable; build and run focused test plus normal suite. Report RED/GREEN evidence and self-review; independent review follows.

## Task 2: Shared pending service and transaction ownership

Files: src/simulator.cpp, include/fastsim/shared_fill.hpp, new focused shared-service types/test files if needed, tests/test_response_completion.cpp.

Interface: shared access returns service identity plus optional absolute response;
advance(exclusive frontier) publishes newly selected responses and fills. The
registry retains source core/sequence/fragment and dirty side-effect identity.

- [ ] Add the two-core dirty/cross-Q Simulator fixture before production changes; require actual admission/response/SQ ownership and nonzero new-path activation.
- [ ] Separate shared request submission from completion installation. Pending fills are not hits; followers refer to their parent. Track finite MSHR/Sequencer and write capacity through response owners, without invented release times.
- [ ] Include mixed controller and ID allocation state in the same cache/directory transaction. Capture actual dirty victims once, including private-eviction cascades; distinguish RD acquisition from WB.
- [ ] Verify pending+selected service conservation, copied snapshot rollback, split/follower completion, boundary equality and empty-epoch progress.

## Task 3: Core continuation and actual store service closure

2026-09-14 continuation checkpoint: all four existing scalar arithmetic bodies
extracted into immediate callable operations with explicit captures/frame handoff;
closed full-suite/default-C32 parity passed. They are NOT retained contexts, so
the continuation/unknown-RAW checkboxes below remain open. Shared unique-miss
memory leg uses actual mixed RD/WB in scoped integration tests, but production
Simulator caller, general lookup/followers and all-source F are still pending.

Files: src/simulator.cpp and focused retained-state implementation/header if extraction is required; include/fastsim/types.hpp, src/main.cpp and production integration tests.

Interface: per-core dispatch/discovery, execution and retirement cursors operate
on retained live fragments. A detached store owns its SQ slot and submits its
fragments after commit/TSO/resource admission. Response changes resume affected
core work and update source arrival lower bounds.

- [ ] Test old complete-feedback parity on closed fragments; unresolved RAW stalls only its dependent instructions, and independent younger requests remain discoverable.
- [ ] Retain all live calendars and resource owners needed for bounded replay/continuation. StoreSet remains the AGU edge; ordinary store response is not its own retirement dependency.
- [ ] Separate producer coverage H from safe shared selection F and retirement. Integrate request submission, controller advancement and changed-owner core feedback; hold values/identities across chunk compaction.
- [ ] Pass the production two-core fixture, split stores, permission/fill visibility changes, retries, empty epochs, and generic/materialized plus serial/parallel comparisons. Report replay work and resource high-water use.

## Task 4: LBM witnesses, interventions and regression acceptance

Files: task-local validation scripts and reports, curated final implementation report and optimization-decisions.md.

- [x] Freeze binary/config/input fingerprints. Default-off full C32 matches its task-entry baseline excluding host-only counters. Repeated for continuation.HBC39M, binary afef6853a16196965d303756337108e7f163f51184aa81893a4e8772475ca2b6; input/population/config gates passed.
- [ ] Full C32 audit targets C13 3127618..3137618 and C1 2M/8M; test actual service coverage of 315 DRAM stores and 430 SQ owners before judging CPI.
- [ ] Run controller-only/admission-only/combined and report paired service, owner, stage and aggregate changes. If coverage fails, return to the responsible task; do not promote from counters alone.
- [ ] After mechanism gates, run LBM C4/C8/C16/C32 and the full 40-case matrix if narrower validation passes; report all regressions, absolute CPI error, CPI MAE and Type-7 tails.
- [ ] Compare sequential quiet-host throughput separately. Final build, fastsim_tests and independent task-only diff review; retain artifacts and uncommitted changes.
