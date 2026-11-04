# SPR ROI Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Initialize native counter and proven idle task state at the existing ROI without altering workload synchronization.

**Architecture:** Native counter epoch via const BaseCPU state-copy getter, complete first-out functional idle-prefix proofs, and continuous functional synchronization across warmup/measurement. Unknown states remain unavailable.

**Tech Stack:** C++17 gem5 probe/FastSim, Python3.11 maintained source auditors.

**Spec:** docs/superpowers/specs/2026-11-04-spr-roi-bootstrap-design.md

## Global Constraints

- Preserve original OpenMP binary, arguments, WORKBEGIN, common-end and thread synchronization; no new guest barrier or wakeup.
- No SPR structure/timing changes. Only nonprobe change is a const state-copy getter in src/cpu/base.hh; all other nonprobe sources retain hashes.
- Oracle clocks never enter functional plans; old traces are not rewritten.
- Branches fastsim-spr and gem5-spr/yinhaolang; portable patch and fetchable02119d1 gitlink.

## Review Focus

- First ROI before any native notification, inherited sleeping state and uint32 delta wrap.
- Counter epoch chronology and audit independent of producer flags.
- Idle first-PC proof versus arbitrary kernel/PID0 forgery.
- Wait/wake/barrier dependencies crossing ROI, including Ready not Running.
- Per-core macro denominators, source hashes and common boundaries.

### Task1: Native counter epoch

Files: gem5-spr/src/cpu/o3/probe/{native_task_clock.hh,tao_trace.hh,tao_trace.cc}, tests/test_native_task_clock_producer.py, tools/audit_task_clock.py, tests/test_task_clock_audit.py.

- [x] Add failing real-ledger cases for unobserved Sleep/Wakeup/On ROI and inherited epoch; include bad futurecycle/period and long sleeping uint32 wrap.
- [x] Implement Ledger::bootstrap(tick,cycle,period,previous_cycle,State), explicit oracle counter_epoch and TaoTrace::ensureTaskClockCounter() using the const CPU observation getter before firstCycles/ROI.
- [x] Extend independent raw-counter auditor for optional versioned epoch, preserve legacy update bootstrap; invalid epochs refuse.
- [x] Run producer and offline counter tests RED→GREEN.

### Task2: Functional initial task proof

Files: probe TaoTrace/native_task_clock.hh, tools/task_clock_initial.py, tools/{thread_state_graph,compile_thread_state,audit_task_clock}.py and tests/test_task_clock_initial.py.

- [x] Audit initial source records and reject HWE/idle-PC aliasing; prove first-out lemma against existing exact kernel cut profile.
- [x] Add failing positive idle and negative arbitrarykernel/PID/PC proof tests.
- [x] Keep raw unknown metadata/snapshots; emit explicit first-account-out-idle-prefix-v1 proof with ordinal and source hash. Resolve only after complete real-cut/source verification.
- [x] Independently validate functional initial proof against first FST record/kernel identity; no oracle time in replay.

### Task3: Cross-ROI synchronization

Files: maintained functional_sync/thread_state/simulator code only if a defect is reproduced; focused tests.

- [x] Add regression warmup wait→measurement wake and wake→later sched-in, preserve pendingbarrier dependencies.
- [x] Verify ROI resets measurements only; fix actual dropped state with RED/GREEN if reproduced.
- [x] Run default FastSim build/tests and focused graph/clock/PMU tests.

### Task4: Native recapture and publication

- [x] Build native gem5 at affinity80%; original OpenMP C4 recapture + fixed-thread control, keep original workload/ROI.
- [x] Map actual config, compile plans, replay, identity/source/native validation and per-core population checks.
- [x] Verify protected source hashes, document all available/unavailable evidence and CPI absolute error/MAE.
- [ ] Fresh whole-change review, explicit child/parent commits, portable patch reverse-check, parent push and remote verification.

## Execution ledger

2026-11-04 start: parent4eb5ce5, child2c8250127d. Original failing source c4-combined-final preserved. Initial public-field assumption was corrected by compile verification; const state-copy accessor is required. Read-only independent task-initial/synchronization audits underway.

Ruling: 使用首个真实account-out(prev0)及完整功能流证明idle前缀，代替idle PC白名单；首PC可能是I/O或HWE返回地址，白名单会误证。原始unknown快照不改写，证明只解析功能归属；缺少完整流则拒绝。无需改变kernel profile机器码切点。

Ruling: BaseCPU字段实际protected，增加const state-copy getter；无新增状态、无updateCycleCounters调用。其余7362个非probe源哈希必须一致；报告该例外，不能宣称7363文件全不变。

Task1/2 tests: 8 native producer/profile/epoch tests GREEN; protected field compile error corrected by const getter; zero idle-macro entry review finding verified RED→GREEN with pre-ROI first-out case. Task3 complete: no reset defect found;3graph/model tests and actualSimulator crossing-ROI case GREEN; cmakebuild and fastsim_tests pass.149focusedPython tests GREEN with process-local GCC11 runtime library. Nativebuild pending finalrelink and recapture.

Final source review: independent reviewer found oneP2zero-entry issue, fixed RED→GREEN; no outstanding material findings. Finalgem5build succeeded and originalOpenMP recapture started.

实际首轮新采集的三个idle核均通过，但主核这次从任务内核路径开始（initial PID1194），暴露同一证明范围的启动差异。因此新增first-account-out-task-prefix-v1：初始任务PID/实例必须与首个真实account-out的prev完全一致，并沿用完整FST/.fsem扫描门禁。相同成对切点证明适用于正PID。正PID前缀若在ROI开始非ON或未知前缀中发生Sleep，producer拒绝解析，独立审计也必须拒绝其严格计时；不借此支持尚未建模的任务halt。原始initial-unknown仍保留。首轮c4-openmp标为diagnostic-only，最终必须重新采集，不能补写旧证明。

Task4 runtime: finalnativebuild/8nativechecks/155Pythonchecks/defaultFastSimtests GREEN. OriginalOpenMParguments unchanged and binaryhash unchanged; finalC4OpenMP953/953events,1012379taskmacros exactpercore; control136/136events,174066taskmacros. Both nativevalidatorspassed, strictCPIscored2/2,unknown0. CPIabsoluteerrors0.2118969279/0.4395976239, MAE0.3257472759cycles/macro; noaccuracythresholdacceptanceorhardwareperfclaim. Childb69ba85b05committed,portablepatch reverse-checkpassed. Parentselectivepublicationpending.
