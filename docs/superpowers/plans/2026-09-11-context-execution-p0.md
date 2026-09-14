# Context execution P0 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. The user has authorized implementation and minimal validation in the current workspace; preserve unrelated edits and do not commit or start gem5.

**Goal:** Replay a real execution suffix while preserving the original per-core scoring population and retirement endpoint.

**Architecture:** Versioned manifest bounds extend the existing warmup source. Producer chunk metadata carries the score marker and functional counter prefix without splitting the chunk or inserting a barrier. Accepted core feedback freezes score retirement; context PMU is accounted separately while all functional and timing state continues.

**Tech Stack:** C++17, existing CMake test binary, Python for the existing LBM fixture comparison.

**Spec:** [LBM mechanism design](../../lbm-two-stage-mechanism-repair-design-20260911.md), P0 only.

## Global Constraints

- Keep interval_weave + time_epoch and Q=1024 for the real workload.
- Keep the 64-byte FST record and complete RAW dependencies unchanged.
- No causal_read, timing oracle input, compensation, new collection or device work.
- Preserve existing formats and production behavior when no context bounds are specified.

## Task 1: Boundary regression and source contract

Files: `tests/test_main.cpp`, `include/fastsim/trace.hpp`, `src/trace.cpp`.

- [x] Add a binary fixture whose manifest scores 8 records but executes 800 on core0, with core1 scoring/executing 128. Require core1 timing to equal the identical full-execution fixture and require the score population to exclude core0's 792 context records.
- [x] Build/run the focused test against the existing parser and record the missing-format failure.
- [x] Implement `fastsim-binary-context-v1`: existing source/warmup/score macro+record fields followed by exact execution record count after warmup. Reject execution shorter than score, physical EOF before bounds and boundaries inside a macroinstruction.
- [x] Expose immutable context capability plus producer-only score-marker state on TraceSource. Continue forwarding dependency and mapping metadata through the existing wrapper.

## Task 2: Accepted scoring and context PMU

Files: `src/simulator.cpp`, `include/fastsim/types.hpp`, `src/main.cpp`.

- [x] Carry the score marker and a functional counter prefix in CoreChunk without splitting timing work. Install the sequence cutoff through the consumer queue, avoiding cross-thread source reads.
- [x] Capture marker retirement in transaction-local TimingFeedback and freeze only at accepted commit. Keep execution state and all requests alive until the declared execution boundary.
- [x] Separate scored functional population and context private/shared PMU; include new counters in existing transaction restoration. Formal PMU excludes context while queue/timing diagnostics retain an explicit execution scope.
- [x] Export score/execution bounds, cycles, actual processed work and context-coverage status. Preserve old JSON semantics for old input formats.
- [x] Extend the focused test for generic/materialized equivalence, a marker inside a producer chunk, source truncation and zero-tail compatibility.

## Task 3: Minimal production gate

- [x] Run `cmake --build build -- -j16` and `./build/fastsim_tests` after the implementation.
- [x] Reuse the frozen C4 suffix fixture, changing only the manifest scoring bounds. Run the candidate once; compare original scoring denominator, per-core endpoints and execution PMU conservation against the already recorded full-execution result.
- [x] Run an old-format prefix control only if required to verify default compatibility. Record CPI absolute/relative error and throughput with explicit processed/scored populations.
- [x] Review the task diff against the saved starting files and document results, including the existing suffix coverage limitation. Do not run the full 40-case matrix.

## Completion evidence

Final build and all FastSim tests passed. The one C4 context run passed all 140 fixture/accounting checks; the old-format prefix control preserved all model fields. Host wall-time/rates and the producer condition-variable wait count are explicitly excluded from that control comparison. See [implementation and validation](../../context-execution-p0-20260911.md). Existing suffix coverage remains incomplete; collector and P1–P3 work are outside this P0 implementation.
