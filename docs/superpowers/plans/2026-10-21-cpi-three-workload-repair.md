# SQLite、Marian、XZ CPI 机制修复计划

> **For agentic workers:** 使用隔离源码快照分派独立修复，主任务顺序整合。逐项执行 RED/GREEN，最后审核组合结果。

**Goal:** 修复已被反例证实的功能身份、分支发现阶段及准备资格错误，并量出真实负载上的净影响。

**Architecture:** 保留两阶段框架、原指令窗口与硬件参数。功能输入描述 ISA 语义，目标时间只作离线参考；实际准入与 CPU 完成、后台填充、退休分别建模。

**Tech Stack:** C++17、gem5 O3/Ruby、FST7 功能 companion、Python 采集及维护验证器。

**Spec:** [三负载排查与修复顺序](../../cpi-sqlite-marian-xz-analysis-20261021.md)，用户随后要求开始修复。

## Global Constraints

- 四核、八通道、buffer TSO114；不靠改变延迟常数或缩小退休宽度拟合 CPI。
- KVM 初始化 → 已绑定身份的 checkpoint → O3 warmup → stat；warmup 宿主硬上限180秒。
- 全项目 FST 不超过100,000,000,000 bytes，磁盘空闲至少20GB。
- 新自然窗口与原窗口分开；同源控制的收益不相加。
- gem5 构建使用进程可用核的80%，本轮192核对应153 jobs。
- 新结果保留独立 identity、共同终点、任务人口和 native Ruby/SLICC 来源门禁。
- 所有 CPI 表同时列绝对误差；validator 通过不等于精度或硬件 PMU 认证。

## Review Focus

- 条件方向错误不能用静态目标提前解出；间接分支和缺失静态元数据回到后端发现。
- 预取的 CPU 确认不释放后台 fill/MSHR，接受、重试和跳过必须有各自依据。
- 旧 `.mreq` v1 可读但不暗示已拥有软件预取身份；未知版本和损坏内容拒绝。
- LRU 准备资格按请求后缀判定；跨核、page-fill、permission 丢失保护仍保留。
- stage 时间满足约束不证明 canonical cache 已实际延后，必须独立检查首次请求。

## Task 1: XZ 采集入口与准备资格

**Files:** `workloads/spec_roi_integration/fastsim_native_roi.h`、`patches/spec2026-xz_s-roi.patch`、`src/simulator.cpp`、`include/fastsim/config.hpp`、相应 C/C++ 回归。

- [x] 真实5任务 launcher 重复调用，复现进入后仍调用 getenv；修复后为零。
- [x] XZ 调用点跨多个 block 保持每任务一次，保留16KiB及全部worker条件。
- [x] 构造两路同set的 A/B/C/A store，复现旧的不安全 A 拒绝年轻安全 A。
- [x] 证书模式按事件保存同set拒绝；全局安全拒绝仍按line；指针不越过epoch回收。
- [x] 同一正式FST对照证书+lifecycle以及本次代码改动，区分开关效果与新修复效果。
- [x] 新XZ二进制正确性、checkpoint、恢复和正式采集；新正式测量期的三个ROI辅助符号计数均为零。

## Task 2: SQLite 分支错误发现阶段

**Files:** `include/fastsim/interval_core.hpp`、`src/interval_core.cpp`、`src/simulator.cpp`、分支计数和真实 FST/imap 回归。

- [x] 用模型自己的预测方向/目标及静态直接目标识别 Decode 可纠正错误。
- [x] 条件方向、间接目标、缺失/矛盾静态数据保留后端发现。
- [x] 前端门禁、匿名推测窗口及 response 传播共用发现起点；不重复加恢复尾部。
- [x] 新回归 RED/GREEN、旧尾部回归与原正式SQLite窗口验证。
- [x] 单独检验发现阶段到实际 canonical 请求准入的剩余缺口。廉价切片被冷取指反例否定，源码与失败证据保留，未整合；实际准入修复仍未完成。

## Task 3: Marian/XZ 软件预取

**Files:** FST request-role producer/reader、`trace.hpp`、`interval_core.cpp`、`simulator.cpp`、native退休分类及机制回归。

- [x] 增加纯功能 `SoftwarePrefetch` 角色；新 companion v2，读取兼容旧v1。
- [x] native 预取仍退休为指令，排除其架构load/store结果；真实退休ledger回归。
- [ ] 用真实接受时刻完成CPU，后台服务和资源寿命不随CPU完成提前释放。独立反例证明仅拆分CPU完成和后台寿命不够；完整在线owner未实现。
- [x] 不确定时拒绝错误认证：early_ack默认false，开启后对失效父请求和被移动的后台服务明确拒绝。完整接受/重试重判仍未完成。
- [x] 编译新gem5并绑定新checkpoint，采集含角色的真实Marian/XZ窗口；warmup分别62.41秒和96.75秒。
- [x] 组合全量测试、同源回归和新窗口维护验证；三来源两配置共6/6通过，独立identity3/3通过。报告包含残差、未知资格与原生请求退化。

最终组合构建、完整C++测试、51项Python回归及三个PMU测试已通过；三来源各default/prepare两配置的维护验证已完成，另有XZ同源branch-only消融通过。最终结果记录在[修复报告](../../cpi-sqlite-marian-xz-repair-results-20261021.md)，不将未完成的在线请求归属列为已修复。XZ prepare CPI较好，但native L1D tag miss严重偏高；所有候选保持显式启用，未推广默认。

实验与源码快照保存在 `tmp/cpi-three-workload-repair-20261021/`，进度以其中 `progress.md` 与逐项收据为准。未自动提交、推送或改变无关默认选项。
