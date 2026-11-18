# Estimated parent service implementation plan

> Use superpowers:executing-plans inline, TDD, and one fresh-context final review. User authorized implementation and validation after reviewing the existing-parent approach; no further design approval is needed.

**Goal:** Add opt-in bounded estimated read-service timing to the existing ParentServicePool, and measure same-input CPI and host throughput independently.

**Architecture:** Preserve certified sources and legacy prepare. For safe ordinary reads with complete supported selected timing but late CPU admission, estimate a single owner's data edge using the frozen selected completion plus an unqueued minimum-service floor. Reuse that owner for eligible same-line followers; all response/resource updates use the existing pipeline.

**Tech stack:** C++17, ordinary Release / GCC LTO16, existing Simulator tests and native quick validator.

**Spec:** docs/graph500-estimated-parent-design-20261117.md

## Global constraints

- Functional retired streams only. No native OoO time as input, fixed delay fitting, per-cycle runtime, whole-window iteration, or parallel authoritative fill table.
- Config `core.unified_parent_estimated` defaults false, requires v2-observe/v2-wait. Installed M4 and frozen profiles remain unchanged.
- Preserve unrelated dirty files; source backups and isolated snapshot/build under tmp/estimated-parent-20261117. Root edits remain reviewable. No commit/push requested.
- Build with FASTSIM_LTO_JOBS=16. Tests require LD_LIBRARY_PATH=/opt/gcc-11/lib64.
- Compare both accuracy and measurement-only throughput on the same input/ELF/config, preload validation completed beforehand. Use CPU52-63 high-frequency wrapper and disjoint masks.

## Review focus

- Estimated data must never be promoted to strict certification or prepare permission; special/carried/remote/ambiguous generations remain fallback.
- Queue time must be subtracted once, with checked monotone boundaries; no queue-free interval inferred from a merged lower request without a matching owner.
- Reusing an owner must not overwrite its data edge or allocate duplicate service leases; individual request IQ/Sequencer occupancy still exists.
- Copy-on-write snapshots and dispatch watermark expiry must preserve service identity across rejected/accepted timing passes.
- OFF behavior must match the field-repair baseline exactly; estimate counters and strict source ledger must remain distinguishable.

## Task 1: contract and regression

- [x] Add independently derived owner estimate/identity tests, invalid-source/future/generation/snapshot checks.
- [x] Add real Simulator displaced-owner sibling-load fixture; prove OFF has a missing parent wait, ON has the intended response and downstream resource effect.
- [x] Watch the behavioral assertion fail before production implementation.

## Task 2: bounded implementation

- [x] Implement checked data-ready estimate and estimated owner quality in existing ParentServicePool, one additional fill-generation field only.
- [x] Add config parsing/validation/output and separate estimate counters.
- [x] Integrate supported source estimate, same-owner reuse and existing response/resource chain; preserve certified/legacy paths.
- [x] GREEN focused tests, main fastsim_tests and relevant config/resource tests; check frozen identities.

## Task 3: actual inputs and throughput

- [x] First quick replay new short C4 ON/OFF; record predicted responses/resource pressure and source coverage.
- [x] Quick replay full Graph500 C4/C8 ON/OFF, preserve all results including regressions; add FLAC/Zstd controls if inputs and disk allow.
- [x] Measure repeated same-input ON/OFF pairs separately from audit runs, excluding preprocessing, integrity, initialization and warmup from primary denominator. A 5% throughput-loss target is a proposed gate, not a guarantee.
- [x] Report CPI AE/MAE, RE/MAPE, source coverage, host rates, repeats/distributions and any failure separately. Do not promote default on failed accuracy/throughput.
- [x] Fresh read-only final review, resolve important findings with regression and green suite; finalize report/ledger.

Completed implementation and all planned evaluations. Accuracy/throughput candidate rejected, not promoted. Final report: docs/graph500-estimated-parent-validation-20261117.md. All source-complete predictions exactly match benchmark timing.
