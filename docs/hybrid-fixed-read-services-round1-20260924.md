# Buffer TSO 固定读服务关系：第一轮设计与实施记录

日期：2026-09-24。用户已授权先整理方案、随后实施第一轮。
本文件细化 [混合式提案](hybrid-bound-line-coalescing-design-20260924.md)，后者中的
“精确 weave 保证整体精度”和“合并已经解释主要误差”不作为本轮前提。

## 目标与取舍

引入默认关闭的 `ruby.fixed_read_services` 候选，冻结一次程序序读服务分类，
随后复用现有竞争求解，传播父服务的响应，不再因时间变化反复重判 alias。
CPI、PMU、宿主吞吐并列观察。各缓存 PMU 的跨案例 MAPE 向 serial 历史的
5% 内靠拢，但 **5% 不是准入门槛，不触发自动回退，也不阻止继续实验**。
逐案误差、WAPE、尾部与覆盖一并展示。数据来源、人口和守恒要求仍遵循
[报告约定](accuracy-reporting-contract.md)；不调时延常数拟合结果。

首轮复用现有层次访问返回的初始时间估计，不声称它是严格 zero-load bound。
FR-FCFS 保留现有内部调度；消除的是 alias 分类外层的反复功能重放，
并非宣布所有控制器／核心固定点均被消除。

## 为什么可以采用固定访问路径近似

FastSim 可以采用“竞争只改时间、不重新改变本轮访问路径”的假设；它是需要测量误差的
建模取舍，不是架构上不允许。原 buffer service 实验会依据修正后的 admission/callback
重新检查合并关系，意在维护服务身份与实际到达的一致性，但由此又把身份判断接回时间求解。
本轮明确接受这部分近似：先固定普通读 owner/follower，再让竞争求解更新完成边。

固定关系不能保证缓存 PMU 不变或变准：竞争本可改变请求先后、替换与权限交错；
程序序选出的 owner 也可能在最终时间上晚于 follower。因此保留普通写、权限和替换路径，
记录 reversed/late 暴露量，并独立检查 CPI 与 PMU。现有 weave 的资源约束仍然有价值，
但不能弥补一切被固定分类遗漏的状态变化，也不能据此保证 buffer 达到 serial 的历史精度。
另修正原提案中的 serial 解释：`tso_store=serial` 指单个在途 store，不代表所有 load
整体串行；“serial 必然每行一次 miss”不能作为论证前提。

## 服务与计数

- 独立读服务表记录 line、generation、owner、当前 epoch 的事件位置、callback、response、父命中层。
- 每核按程序序建立关系，核间按可用队首的初始到达时间合并，避免一次耗尽整个核造成 core-major 偏置。
- 普通 load 可作为 owner/follower；L1-hit 父服务也有生命周期。follower 不重复查私有 tag、
  不新建下层 miss，但保留 CPU/Sequencer 请求容量和消费者／writeback 约束。
- 一次分类统一控制请求、PMU 与响应；每行每个 generation 的事务身份独立，不能全 ROI 按地址去重。
- 当前 epoch follower 引用 owner 的最终核心响应；incoming follower 引用已提交的绝对响应。
  请求已迟于 callback 时保留固定分类近似，但响应不得早于请求的最小完成边；记录 late-reference。
- store、atomic、forwarding、I-side、页表相关访问继续走现有路径，不删除其资源影响。
  写行预排除普通读合并，特殊事件及替换／失效终止受影响读关系，禁止陈旧权限复用。
- 跨 epoch 保留未完成 owner 身份与响应；事务失败恢复原表，不能清表掩盖在途服务。

## 回退与兼容

新开关关闭时不分配或遍历新服务表；原 serial/buffer、repair/local/no-fallback
默认值和行为保留。新候选与旧 alias/service 实验互斥。首轮仅支持现有
interval_weave/time_epoch/sparse-response、同频路径；不支持的配置明确拒绝，避免静默无效。
`response_paired_frontier` 和 `response_sparse_resource_repair` 首轮不支持：前者的
组合尚未完成独立验证，后者在访存响应已解析后移动 FU issue，不能直接平移父响应。
这两个现有实验仍保留，可在关闭新开关时使用。运行时 DVFS 同样明确拒绝。
private preview 在新模式内绕过优化入口，以保证候选不会被静默跳过。
OFF 目标统计等价不等于宿主零成本：新增 descriptor 字段仍占空间；本轮同一新二进制
ABBA 测量 ON 相对 OFF 的增量，未单独对旧／新二进制做宿主性能等价认证。

候选在层次事务中执行。现有 buffer permission 检查失败时，恢复共享层次、
核心计数、Sequencer 计数与新读服务表，再运行原 buffer 路径；时间变化本身不回退。
功能阶段只尝试一次。保留每核 incoming 身份；当前 epoch 新服务只有在最终结果确定后才提交。
精度百分比不参与任何运行时决定。回退不是把按行独立计算的 PMU 与另一调度结果拼接。

## 实施文件与测试

1. `include/fastsim/fixed_read_services.hpp`：独立身份表、跨 epoch、失效与响应更新。
2. `src/simulator.cpp`：功能分类入口、父响应引用、最终服务提交与事务恢复。
3. `include/fastsim/config.hpp`、`src/config.cpp`、`include/fastsim/types.hpp`、`src/main.cpp`：
   默认关闭开关、互斥配置、覆盖／路径变化／回退诊断及配置序列化。
4. `tests/test_response_completion.cpp`：同行读、FR-FCFS 改序、RAW 反转、回调边界、
   同组替换、写失效、跨 epoch 与 OFF 回归。表组件测试覆盖不可复用的旧 generation。

默认验证：`cmake --build build -- -j16`、`./build/fastsim_tests`。

## 第一轮实验

案例：`uarch-baseline-c04-857.namd_s`、`uarch-baseline-c04-782.lbm_r`、
`uarch-baseline-c32-782.lbm_r`。
原始源：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`。
冻结完整有效配置链，保留当前 store-forwarding 选择，不引入几何差异。

先保存修改前二进制，重跑当前 baseline；再运行 OFF、候选 ON。
用独立 identity 工具和维护 native validator 核对三案；PMU 只取 conserved native Ruby/SLICC。
输出每案 CPI 相对／绝对误差、聚合 MAPE／MAE，以及每项 PMU MAPE、WAPE、分位数和 bias。
同一新二进制做 OFF/ON/ON/OFF 对称串行吞吐测量，报告宿主干扰与目标输出一致性。
所有原始输出存 `tmp/hybrid-fixed-path-20260924/`。本轮不自动扩全矩阵或更改默认。

## 状态

方案先于代码实施记录，结果见 [首轮三个反例报告](hybrid-fixed-read-services-results-20260924.md)。既有未提交工作保留，
本轮起点源码／二进制已存实验目录的 `before/`。

实现过程中已验证：普通连续读跨多个 epoch 并不必然产生 incoming alias，普通稀疏
反馈会把退休尾部计入下一 epoch 的 issue envelope。测试分别检查已提交前缀不变、
请求守恒，以及身份表跨 epoch 的绝对响应／过期；完整 ROI 另报真实 incoming 数量。
新增的三事件 load/store/load 合成轨迹在关闭候选时也触发现有 residual CPI
attribution 守恒异常，不能归因于候选或 paired-frontier。该写冲突机制测试使用现有
materialized fast kernel 检查请求计数；普通读与 FR-FCFS 测试仍比较两个核心内核。
保留异常日志 `fixture-tests.log`；本轮未修改既有 residual attribution 检查。
