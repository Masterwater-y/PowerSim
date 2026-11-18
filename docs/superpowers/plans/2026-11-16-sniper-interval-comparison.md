# Sniper interval Comparison Implementation Plan

> Execute inline under the user's instruction to run the experiment; preserve all existing batches.

**Goal:** Run the ten retained C4/C8 FSTs with native Sniper interval and compare end-to-end throughput and completion predictions.

**Architecture:** Isolated copy of the frozen Sniper source; native interval algorithm with FST dependency/drain/population and aggregate resource adapters. Reuse the existing identity receipt and end-to-end launcher.

**Tech Stack:** C++, CMake/Ninja, Python, SQLite.

**Spec:** ../specs/2026-11-16-sniper-interval-comparison-design.md

## Constraints and review focus

Preserve raw outputs and production FastSim. Same complete physical population; tail fragments do not increment macros. Explicit dependencies must affect timing. EOF must dispatch all pending work and account for terminal memory service. FU descriptors must use the correct dynamic type. Startup and warmup stay timed; validation stays separate. Check frozen inputs, configs and ELF hashes, not a filename alone.

- [x] Add integration regressions; observe failure with the current binary (short stream currently returns zero completion time).
- [x] Copy frozen Sniper into project tmp, add interval-only adapters, build and pass dependency/fragment/drain/role/resource regressions plus existing ROB tests.
- [x] Freeze sources, ELF and configs; run ten cases sequentially on CPU 52–63 with the existing e2e launcher and input receipt.
- [x] Validate all populations/endpoints and report weighted-work throughput plus case-equal completion MAPE/proxy MAE, comparing preserved historical samples with explicit provenance.

Ruling: no production default change or commit/push; this is the requested isolated experiment. A same-host sequential batch controls CPU budget but does not establish repeated throughput acceptance.

Evidence: unmodified interval fixture predicts zero EOF time; red-tests.log six failures includes wrong dynamic core type. Candidate green-tests.log six passes, rob-regression.log three passes, e2e-tests.log 24 passes. Full campaign started with frozen candidate SHA 87a95e91d6a5463ebad9678332f8ec099c3b21f2d9e3070dabccc86ee4df755c.

Review correction: v1 EOF lacked aggregate nonpipelined FU pressure (four sqrt records predicted 25 cycles). Regression reproduced failure; v2 uses max(effective resource CP, dependency/memory residual). green-v2-tests.log: 8/8 pass. Candidate SHA 75015324d38e9dd36497241d8cea7d5dcc740a0b2e734c2d318dd8e4d7a58da9. v1 campaign excluded; complete campaign-v2 started. Reviewer found no remaining confirmed blocker.

Campaign-v2 completed 10/10 with exact per-core model-dispatched populations and complete monotonic endpoints. Report sniper-interval-comparison-20261116.{md,json,csv}: C4 0.7093361195 MUPS, C8 1.1810499885 MUPS; completion MAPE 39.402267/39.292644%, proxy MAE 0.5809306/0.5282591 cycles per macro. Baseline samples preserved; no task-active CPI or hardware-PMU equivalence claim.

Final verification: full tools/trace_comparison unittest discovery with candidate Sniper tree completed: Ran 65 tests in 75.891s; OK. Frozen candidate ELF and all 1663 copied source files unchanged after the ten-case run.
