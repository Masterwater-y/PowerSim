# Store Request Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 在当前会话逐项实施；末尾独立审核。用户已明确要求整理文档并开始实施，不重复请求实施许可。

**Goal:** 首先交付可运行的store请求生命周期参考组件，明确记录cache/core接入尚未完成；最终将证书变为纯执行加速选择。

**Architecture:** 沿用PendingOwner，分离SQ、optional enqueue、取权事务、mandatory与回调资源；组件不猜测cache命中或完成时间。完整目标保留两阶段，阶段二按实际准入提交cache效果。

**Tech Stack:** C++17、现有FastSim CMake/test harness、事件sink。

**Spec:** [请求生命周期设计](../specs/2026-10-21-store-request-engine-design.md)

## Global Constraints

- 保留所有既有dirty变更；隔离worktree从当前工作源码覆盖得到，不只用HEAD。
- 当前首片不更换主模型的cache后端，不发布新CPI/吞吐数字，不推广任何候选默认。
- 不输入gem5时序/cache outcome；所有服务完成必须是callback事件。
- FST总量<=100,000,000,000 bytes；gem5若编译使用可用核80%。
- 不自动commit/push；有缺项必须记为未完成，不能以组件测试替代真实负载精度。

## Review Focus

- 旧代次callback释放自身资源但不能授予新权限；重复callback不能双释放。
- optional enqueue和取权资源分配不同，队满拒绝不污染状态。
- head接管在途prepare不再分配取权槽；permission返回不释放SQ。
- 失效后的mandatory重试保留原身份和SQ；序号/时间/容量溢出不能部分提交。
- 普通单fragment限定必须fail closed；完成历史不能随整个trace无限增长。

## Task 1: 文档、隔离和基线

- [x] 保存设计/计划并自审：身份、阶段边、API名称与首片范围一致。
- [x] 创建ignored detached worktree；覆盖当前src/include/tests/tools/configs/docs及CMake，冻结hash。
- [x] 运行当前完整fastsim_tests，记录基线。

## Task 2: 生命周期参考组件

**Files:** create `include/fastsim/store_request_engine.hpp`、`src/store_request_engine.cpp`、`tests/test_store_request_engine.cpp`；modify `CMakeLists.txt`。

**Interfaces:** `StoreRequestProfile`、`StoreDescriptor`、`StoreRequestEngine`、`StoreEngineEvent`；均只消费模型事件与PendingOwner，不读取oracle。

- [x] 写双冷store并行、接管和SQ守恒契约，确认RED。
- [x] 实现有限SQ/端口/可配置optional queue、FIFO mandatory、有限取权事务、回调与streaming event sink，确认GREEN。
- [x] 添加同line跳过、权限失效、在途失效重试、迟到callback、跨时间推进、队满、非法序号/fragment/时间溢出负例；分批RED/GREEN。
- [x] 独立target与完整FastSim测试均通过；保持主模型路径未接入的准确说明。

## Task 3: 整合和独立审核

- [x] 用相对当前dirty基线的最小diff整合；根工作树构建 -j16、完整fastsim_tests及三个PMU测试。
- [x] 使用新上下文审核身份、资源、时间、错误原子性与真实接入边界；重要问题修复后再验证。
- [x] 文档加入实际结果、日志/收据、未完成项；不声称CPI已改善。

## 后续里程碑（尚未实施）

- [ ] gem5请求级oracle ledger与短有界probe，绑定新producer/checkpoint。
- [ ] SharedSystem实际准入适配器：cache瞬态、TBE、目录和服务callback都按同一事件推进。
- [ ] IntervalCore continuation/共同安全frontier与跨epoch事务；完整两阶段闭环。
- [ ] 证书批处理与逐事件参考路径目标输出一致；真实三负载四模式CPI/PMU及独立吞吐验收。


## 本轮验收收据

首片已整合，2026-10-21根目录构建、完整fastsim_tests、两个新增独立target、三个既有PMU回归均exit 0。新增生命周期回归ASan/UBSan检查通过；独立审核提出的两项P2均已修复并复核。根目录原simulator/cache/core源码hash未漂移，整合源码与审核worktree一致。

详见[首片结果](../../store-request-engine-phase1-results-20261021.md)，日志及身份文件在`tmp/store-request-engine-20261021/`。此处完成的是Task 1–3的首片范围，后续四项仍未完成，不构成当前主模型CPI/PMU修复。
