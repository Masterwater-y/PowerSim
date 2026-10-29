# Recorded scheduler and complete task-cycle implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development; root integrates the shared simulator path. The user explicitly requested execution of the documented design. Do not ask for another execution approval.

**Goal:** Complete fixed-C4 task-active/elapsed accounting with actual blocking facts, a reusable recorded scheduler, and measurable two-stage replay overhead.
**Architecture:** Functional sources remain physical CPU streams. A coordinator-owned RecordedScheduler tracks logical tasks/core ownership and receives corrected-retirement events; it never copies gem5 durations into the target clock. Certified common-cut coverage closes every core at the target common end. Accounting availability and wake-causality quality are independent.
**Tech Stack:** C++17, Python3.11, exact-build x86 Linux/gem5, existing libgomp markers.
**Spec:** docs/thread-state-active-elapsed-design-20261013.md, including its scheduler proposal and the user's subsequent execution authorization.

## Global constraints

- Preserve the existing dirty FastSim workspace and earlier implementation; no commit/push or unrelated checkout required. Work in place with file-scoped ownership and snapshots of changed files.
- Retain bound/weave/feedback and its ordinary no-event fast path. No per-cycle scheduler, source-tick scheduling, guessed kernel offsets, or tuned latencies.
- Fixed task/core mappings first; autonomous rescheduling, migration and oversubscription remain rejected until source semantics support them.
- gem5 builds use floor(available affinity cores * 0.8), minimum1; explicit Python3.11/GCC11. Do not rebuild a binary during capture.
- FST aggregate <=20,000,000,000 bytes, new capture <=2GB; use small C4 probe and never copy the15.405GB TeaLeaf sources.
- Accounting contract `task-oncpu-switch-entry-v1`: owner changes at the committed last micro-op of the audited switch-entry instruction; user+kernel+IRQ time follows that owner. This is task on-CPU reference time, not certified perf hardware-unhalted time. Emit `perf_equivalence=unverified` separately.
- A source may certify owner continuity to the common cut only with complete retained kernel coverage, initial owner, common closure, and zero read/identity/unretained-hook failures. Unsupported/missing evidence remains unavailable.
- Primary active CPI requires matching accounting contract/scope/task sets and positive independent task-macro denominators. Unknown sleep cause alone does not invalidate otherwise complete on-CPU accounting.

## Review focus

- Wake before actual sleep and EAGAIN must not create a false out->wake cycle.
- A blocked task never freezes the physical CPU/kernel stream.
- Complete common-cut coverage may close a known owner through the tail; uncertified EOF cannot.
- Native traced scheduler/context-switch instructions are not charged again as synthetic service.
- Speculative producer work cannot cross an execution-permission boundary; ordinary observation events should not force unnecessary handoffs.

## Task 1 — Collector facts and source closure

**Files:** gem5-fs/src/cpu/o3/probe/TaoTrace.py, tao_trace.hh/.cc; tools/generate_guest_scheduler_profile.py; scripts/run_gem5_sync_probe.py; patches/gem5-taotrace-thread-state.patch; tests/test_guest_scheduler_profile.py.

- [x] Audit the actual cached kernel ELF for the DEQUEUE_SLEEP decision and actual futex enqueue/selected-waiter paths. Verify exact bytes/registers/field widths in the build-bound profile. No inferred state read at late switch entry.
- [x] Use a failing actual/source test showing UNKNOWN switches and missing confirmed block admissions, then export proven sleep evidence and precise waiter/request facts. Preserve wake-before-park as a separate path; never infer a wake cause solely from a runtime bracket.
- [x] Extend portable events only if needed; agree new event kinds with root before changing shared interfaces. Existing switch rows may declare `state_status=verified-dequeue-sleep` and `state_can_race_wakeup=false` only with proven per-invocation dequeue evidence.
- [x] Metadata adds `accounting_contract=task-oncpu-switch-entry-v1`, `kernel_stream_complete=true`, `common_cut_complete=true`, `owner_continuity_to_common_cut=true`, `common_cut_id=first-core-target-common-end-v1`. These flags are certified facts derived from actual capture, not defaults. No ticks in functional metadata.
- [x] Export source timing/ownership audit separately in oracle; compile with80% cores, run embedded-Python smoke, produce applicable incremental patch.

## Task 2 — RecordedScheduler module

**Create:** include/fastsim/recorded_scheduler.hpp, src/recorded_scheduler.cpp, tests/test_recorded_scheduler.cpp. Root owns CMakeLists.txt/test registration and simulator integration.

**Interface:** `RecordedScheduler(vector<uint64_t> initial_owners, vector<uint64_t> task_ids)`, `begin_measurement(uint64_t origin_reference_q16)`, `switch_owner(size_t core,uint64_t prev,uint64_t next,uint64_t normalized_prev_state,uint64_t time_reference_q16)`, `wakeup(uint64_t pid)`, `finish_core(size_t core,uint64_t time_reference_q16)`, `finish_common(uint64_t common_end_reference_q16,const vector<bool>& certified_owner_continuity)`, `owner(size_t)`, `active_q16()`, `other_q16()`, `unknown_q16()`, `complete()`. Values use target Q16 reference cycles.

- [x] Write RED mechanism tests: sleep vs runnable preemption, duplicate owner, wake!=run, other contexts execute, and complete/uncertified common tails.
- [x] Implement separate logical task state/core ownership; preserve begin-measurement owners and reset counters.
- [x] `finish_common` accrues known owners through the common end ONLY on certified cores; all other uncovered spans become unknown. No extrapolation across uncertified data. Overflow/regression/invalid-state checks reject.
- [x] Tests prove each core active+other+unknown equals common elapsed and that task sleep removes only task ownership, not CPU progress.

## Task 3 — Compiler, independent audit and reporting contract

**Files:** tools/thread_state_graph.py, tools/compile_thread_state.py, tools/audit_thread_state_accounting.py, tools/time_accounting.py, configs/time-accounting-v1.schema.json; matching Python tests.

- [x] RED tests for certified closure vs forged/incomplete flags, block/wake race, incompatible accounting definitions and independent TASK denominators.
- [x] Consume Task1 functional evidence, preserve source-order/DAG/owner guards; export optional plan section after TASK rows: `ACCOUNTING task-oncpu-switch-entry-v1` then one `CLOSURE <core> <0|1>` per source. Legacy plans without it remain proxy/unavailable. No source timestamps in plan.
- [x] Offline auditor uses observations only for gem5 time, preserves task macro before after-record transitions, and emits available active data only for certified accounting coverage. Wake-cause quality remains explicit independent diagnostics.
- [x] Reports accept compatible complete task-oncpu definitions; perf compatibility remains separate. Missing task macro, bad collection counters, unknown coverage or incompatible contracts fail closed.

## Task 4 — Integration and two-stage boundary policy

**Files:** include/fastsim/functional_sync.hpp, src/functional_sync.cpp, include/fastsim/types.hpp, src/simulator.cpp, src/main.cpp, CMakeLists.txt, tests/test_main.cpp and scheduler runtime tests.

- [x] Integrate RecordedScheduler at the existing corrected-retirement coordinator boundary, retain default no-event predictions, and expose complete active/inactive/unknown values plus numeric active CPI when eligible.
- [x] Parse optional ACCOUNTING/CLOSURE provenance with strict uniqueness/core validation. Legacy fixtures retain conservative unknown tails.
- [x] Reconcile functional sync and thread-state input through one scheduler/boundary interface; preserve source anchors and reject incompatible duplicate events. Keep scheduling policy recorded, not counterfactual.
- [x] Separate passive observations from execution constraints where proven safe; batch only when post-boundary private/shared state cannot escape before permission. Verify no memory issue enters a closed frontier.

## Task 5 — Real C4 and throughput/accuracy validation

- [x] C++ default build/direct tests, targeted Python suites, and independent review of collection, accounting and two-stage handoff.
- [x] Fresh bounded C4 capture: zero dropped hooks, at least one confirmed real sleep/wake chain, EAGAIN preserved, ownership/macro conservation and complete common cut.
- [x] Compile without oracle dependence, independent gem5 audit, FastSim replay, positive own TASK macro counts and numeric same-contract active CPI. Report absolute CPI error as well as relative and MAE; elapsed separate.
- [x] Use maintained native PMU validator and separate target identity; explicitly report unsupported scopes rather than selecting proxy truth.
- [x] Repeated sequential fixed-affinity same-source throughput vs frozen baseline; separate observation batching, bookkeeping and true dependency gating effects. Record input work and warmup/measurement times, disk budget and remaining limitations.

## Plan review rulings

Task1/3 share provenance/evidence semantics; root coordinates new event kinds before implementation. Task2/4 share the exact Q16 scheduler interface; root alone edits simulator/CMake. Task3/4 share ACCOUNTING/CLOSURE optional sections; old plans retain their prior behavior. Task1 build/capture and Task5 experiments are sequential, never concurrent with replacement binaries. User authorization covers completing the fixed-C4 design; no new permission gate is required for these reversible changes.

## 完成记录

2026-10-13 fixed-C4 首片完成。实际阻塞/公共闭合、模块、组合计划、观察批处理、独立审计、真实回放、重复吞吐和维护的 native PMU 范围检查均已执行。完整结果见 [recorded-scheduler-results-20261013.md](../../recorded-scheduler-results-20261013.md)。PMU 人口不匹配按计划显式拒绝，严格 perf、PMU 范围扩展及任意调度不在此次完成声明内。
