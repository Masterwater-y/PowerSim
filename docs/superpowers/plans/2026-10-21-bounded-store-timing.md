# Bounded store timing implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; test-driven-development and a fresh final code review. User explicitly authorized documentation and implementation; continue without another approval. No automatic commit/push.

**Goal:** Replace whole-epoch fixed-point replay with an opt-in bounded approximate model, preserving measured populations, finite resources and store lifecycle; evaluate SQLite/XZ/Marian.
**Architecture:** 按spec第15节先接入非绑定资源解析子域：规范cache/uncore分类一次，沿用既有核心反馈的一次遍历，在实际退休点计算store响应和SQ释放。通用response cursor/calendar为独立原型，尚无生产流式适配。无后端重放和收敛循环。
**Tech Stack:** C++17, GCC11, current CMake/validator; no new runtime dependencies.
**Spec:** [written design](../specs/2026-10-21-bounded-store-timing-design.md).

## Global constraints

- Preserve current dirty source; isolated worktree receives current source overlay, not old HEAD alone. Root integration only after review and tests.
- FST total <=100,000,000,000 bytes; no new workload trace/checkpoint needed. Build FastSim -j16; gem5, if needed,80% affinity.
- No gem5 timing/path labels or per-workload parameters as inputs. No model/config switching by workload.
- Retired counts vs physical services separate; finite resource reservation at actual model admission, unique completion and SQ release.
- Native reference/identity/population per accuracy contract0/8; CPI relative and absolute, aggregateMAPE/MAE, elapsed separate. Throughput paired quiet-host, not validator wall time.
- Failure/incomplete implementation must be explicitly reported; standalone tests are not evidence of production integration or accuracy.

## Review focus

1. Younger independent load before an older delayed load must remain possible; future store cannot consume a past slot.
2. SQ dispatch occupancy is not duplicated at retire; permission returns do not release SQ.
3. Cross-window identity/reset must preserve in-flight work, including local/split boundaries.
4. Canonical classification queues/counters cannot be charged again during scheduling.
5. Certificate changes host work only; no permanent epoch rollback, forced drain, retry tick polling or hidden legacy fallback.

## Task1: isolated bounded response prototype

Files: include/fastsim/bounded_store_timing.hpp, src/bounded_store_timing.cpp, tests/test_bounded_store_timing.cpp, CMakeLists.txt.
Interface: BoundedUop(lower-bound stages,dependencies,load/store/local/special flags); BoundedServiceShape(recipe,identity,line); BoundedResponseCursor emits requests/retirements and accepts completions. No simulator/cache mutation inside component.
- [x] Freeze dirty baseline and create isolated overlay; baseline build/full tests.
- [x] RED: older delayed read + younger independent read, future store, ROB/SQ capacity and long local chains.
- [x] Implement prototype readiness/dispatch/retirement cursor and resource ownership, GREEN. Long-chain and issue-pressure work bounds pass; production adapter remains Task3.
- [x] Record exact interfaces and prototype fixture throughput; not workload throughput.

## Task2: analytical planner and service/calendar split

Files: same component; src/simulator.cpp adapter.
Interface: StoreTimingPlanner receives actual canWB and immutable shapes; ServiceCalendar admits finite resources and returns unique callbacks. classify_once produces recipes, no final timing allocation or duplicate counters.
- [ ] RED: overlapping cold stores,TBE1/2,takeover,write callback,SQ handoff,NO_ACCESS,special fragments,invalidations,certificate neutrality.
- [ ] Implement finite calendars and per-owner store lifecycle, plus persistent cross-window state, GREEN.
- [ ] Remove old-queue charge from recipe; all physical resource/counter ownership explicit. Preserve legacy off exactly.

## Task3: production adapter/config/report contract

Files: src/simulator.cpp, src/config.cpp, include/fastsim/config.hpp, src/main.cpp, include/fastsim/types.hpp; targeted integration tests.
Interface: opt-in bounded model, immutable cache-shape adapter, cursor -> TimingFeedback and existing commit/accounting. Incompatible old repairs rejected; no silent fallback.
- [x] RED: actual Simulator fixture；普通store、跨Q、NO_ACCESS、SQ2/114、跨行扫描和审计；缺失atomic资格拒绝。
- [x] 第15节生产子域adapter、资源/事件资格和配置/估计输出GREEN；通用绑定资源adapter仍未实施。
- [x] Full tests and existing PMU integration/retirement regressions；组件sanitizer通过，独立审核P1/P2已修复复审。最终metadata重编译检查另留receipt。

## Task4: frozen workload validation and root integration

Files: curated results doc; existing maintained validator only.
- [x] Freeze configs/binaries/includes; independently validate3 target identities. 按下文执行细化，新bounded完成3源审计，旧prepare完成同binary配对复现；default保留原维护入口记录，未新增default性能测量。
- [x] Report all3 CPI/elapsed outcomes and PMU source/coverage; all3 maintained validators passed, incomplete hardware-PMU evidence remains explicit.
- [x] Paired throughput repeats using identical build/placement, work/wall distributions and oldprepare baseline. Peak RSS未采样，明确unavailable；该项不代表内存验收完成。
- [x] Integrate minimal patch after review; root -j16/full tests, source no-drift receipt, FST/free space check, final report.

## Execution rulings

2026-10-21: User authorization covers plan and inline implementation; no new approval pause. Plan is preserved in docs and execution ledger rather than auto-committing a dirty workspace. Unknown cursor feasibility is tested first, not assumed or replaced with two full feedback passes.

2026-10-22: 实现细化按spec第15节执行。通用绑定资源游标的持久流式适配仍未完成，不把组件测试勾作此项完成。生产快路径要求TBE>=maxSQ+2*LQ+1、写接口>=maxSQ+2，单UOP最多2个data fragment；atomic须具备locked及前后barrier。独立审查提出的跨行扫描、load片段上界、store审计缺失和atomic资格均已修复并复审。完整回归及三负载验证结果见[结果报告](../../bounded-store-timing-results-20261022.md)。Task2通用资源职责和持久适配仍未完成；本次交付仅第15节生产子域，不代表整份通用设计全部完成。

验证执行细化：每个源只做一次新模型完整维护入口审计；旧prepare使用已有同源维护入口记录，并用本次同一二进制的三轮配对回放核验target时间/PMU/人口逐值一致，避免重复扫描同一FST。吞吐固定CPU32..47、18次串行运行；源审计在性能窗口暂停、结束恢复，隔离记录保留。
