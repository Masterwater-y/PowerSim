# Buffer service repair implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution.

**Goal:** Implement and test an opt-in bounded transactional read-service path.

**Architecture:** Reuse hierarchy transactions and timing feedback. Reclassify
service admissions on corrected arrivals; commit cache effects and timing
together only at a fixed point. Preserve the existing baseline on failure.

**Tech Stack:** C++17, existing CMake/fastsim_tests and native validation tools.

**Spec:** `docs/superpowers/specs/2026-09-23-buffer-service-repair-design.md`

## Global constraints

Default off; preserve fallback; throughput is a release gate. Work in the user's
managed dirty FastSim workspace, retaining pre-edit snapshots under
`tmp/buffer-service-repair-20260923/before`. Do not commit unrelated changes.

## Review focus

- A rejected trial must not leak cache PMU or controller reservations.
- CPU request capacity includes followers; MSHR capacity does not.
- L1-hit parents and callback equality must not create extra lookup suppression.
- Corrected order changes may both create and remove aliases.
- Cross-epoch fallback must count discarded identities explicitly.

## Task 1: Regression contract

- [x] Add real simulator tests in `tests/test_response_completion.cpp` using
  four independent reads in one line. Assert three aliases, one L1 lookup,
  four CPU requests and equal response timestamps.
- [x] Force zero work budget and compare cycles, L1/L2/LLC counters to baseline.
- [x] Add finite-capacity and read/write/read cases that expose stale merging
  and rollback; run tests to establish failure before implementation.

## Task 2: Transactional integration

- [x] Add configuration and JSON diagnostics in `config.hpp`, `config.cpp`,
  `types.hpp`, `main.cpp`; mutually exclude existing alias/read-service modes.
- [x] Add a bounded repair function in `simulator.cpp`, immediately before
  ordinary epoch replay. Save counters and line state; each attempt owns a
  hierarchy transaction and only publishes stable timing.
- [x] Sort corrected arrivals by reference time, core and event index. Never
  apply the legacy `last_core_issue` clamp inside the candidate.
- [x] Activate the existing alias-before-cache branch only inside trials;
  include L1 parents, reject overlapping writes, preserve successful state.
- [x] Restore trial state on rejection, clear retained identities with a
  diagnostic, and let existing baseline code run.

## Task 3: Verification and limitations

- [x] Build with `cmake --build build -- -j16`; run `./build/fastsim_tests`.
- [x] Run directed capacity/reorder/cross-epoch probes and check fallback work.
- [x] Run maintained native validation on representative cases, preserving
  identity/source/scope gates and CPI absolute error/MAE.
- [x] Measure serial ABBA throughput and publish coverage together with cost.
- [x] Review changes and document what remains before default rollout.

## Execution ledger

Ruling: implementation is already authorized by “开始设计并实施”; no additional
design approval round. Use in-place snapshots because the managed workspace
contains the prerequisite uncommitted buffer-TSO implementation.


Task 1: complete — first joint-PMU regression failed against the unimplemented
flag; capacity, rollback, I-side and budget regressions now pass. Red and green
logs are retained under `tmp/buffer-service-repair-20260923/`.

Task 2: complete for the bounded reference implementation — independent review
found late FU shifts, independent controller retime, both-direction write
conflicts, resurrected fallback identities and missing I-side arrivals. Added
explicit fallback guards and diagnostics. Preflight scans also spend credit.
No claim that these guards constitute the production sparse closure.

Task 3: complete — final default build and full C++ tests passed. Two native
cases passed identity/source validation; both fully fell back on FR-FCFS.
Serial ABBA and a separately identified active FCFS synthetic benchmark are
reported in `docs/buffer-tso-service-repair-20260923.md`.

Ruling: coverage gate failed. Keep the candidate off and do not broaden to a
40-case accuracy run that cannot exercise it. The production high-throughput,
broad-coverage objective remains unfinished; the spec lists the required
sparse dependency/resource closure and controller transaction work.
