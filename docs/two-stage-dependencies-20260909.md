# 完整动态依赖接入原两阶段框架

日期：2026-09-09。状态：第一项机制接入完成，构建、整套测试及必要 TeaLeaf 验证通过。
接续 [两阶段修复审查](two-stage-repair-review-20260909.md)。

## 实现范围

完整动态 RAW 现在进入 `interval_bound` / `interval_weave` 的基础调度，以及原
`time_epoch` 第二阶段。使用既有 FST reader 和 `.deps` 格式，没有扩大热记录或
增加反馈轮次。原维护配置、并行 worker、窗口/DVFS 接口均保持。

- `IntervalCoreModel::schedule()` 消费内联和扩展 RAW；完整动态元数据优先于
  `committed_static_dependency_feedback` 的静态操作数补全。`n_src > 4` 不代表
  必须补边，多个操作数可能来自同一个 producer。依赖审计与 StoreSet 去重也读取扩展。
- `ChunkUopBound` 保持四条 RAW 加一条 StoreSet；`UopIndex` 定义也完全不变。
  producer 在推进 trace 前，将扩展复制到 chunk 的稀疏 ordinal/offset/count 表和
  连续 distance 区域。没有逐 UOP 的 vector 或固定 16 槽扩张。
- resident buffer 仅索引有扩展的记录，随所属 chunk 退休清理，回收时清空。
  顺序遍历沿扩展表推进查询游标；回看先前区间时重新定位。该游标只影响宿主查询，
  不参与目标时序。无扩展时直接使用内联边。
- 基础反馈、通用／materialized 快速内核、跨 checkpoint ROB 环、activity certificate、
  resource candidate 和 block-transfer 判定均消费完整边。距离超出 ROB 的既有吸收
  规则保留，不能按 chunk 起点丢弃依赖。
- 第二阶段槽位 4 始终是 StoreSet 地址生成边；槽位 5 起是扩展 RAW，等待 producer
  writeback。审计 JSON 新增 `producer_dist_extensions`，保留原五槽数组含义。

修改入口：`src/interval_core.cpp`、`src/simulator.cpp`、`include/fastsim/types.hpp`、
`src/main.cpp`。新增测试：`tests/test_interval_dependencies.cpp`。

## 必要验证

`cmake --build build -- -j16` 与 `./build/fastsim_tests` 均通过。
新增用例覆盖 5、8、16、20 条依赖：最老的慢除法／内存 producer 位于扩展区，
消费者及其后继必须等待实际完成。覆盖 7/64 UOP 分块、8/1024 cycle 窗口、chunk
回收、warmup 边界、完整元数据无静态表、通用／快速内核和 block-transfer 结果一致性。
原 StoreSet、窗口和其他机制回归继续由整套测试覆盖。

没有重新采集 trace，也没有运行其他负载或完整 CPI 矩阵。模拟器验证共七次：
旧 TeaLeaf 长输入的 ABBA 四次、同窗口旧依赖一次、完整依赖通用／快速内核各一次。
吞吐对照串行执行，未与构建或其他本任务模拟重叠。

## TeaLeaf 结果

### 旧输入兼容性与吞吐

采用原 C4 长输入、同一冻结维护配置和本次修改前保存的二进制。每次测量段约
4,000 万用户 UOP。合并两次测量时长计算吞吐：

| 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 合计 core cycles | 25,683,641 | 25,683,641 |
| 用户 UOP 吞吐 | 6.7108 M/s | 6.7189 M/s |
| 两次模拟总 wall time 合计 | 13.0042 s | 13.0034 s |

吞吐变化约 +0.12%，视为本次测量波动，不宣称加速。四次运行的目标 cycles、全部
scope PMU、per-core/totals 与 cache/CHA 输出一致。差异仅在宿主耗时、吞吐及等待计数。
这是一项负载的兼容性检查，不能外推为全部负载的吞吐保证。

### 已采集完整依赖输入

采用成对采集的同一 TeaLeaf C4 工作量、精确 warmup/measurement 边界，配置使用
原维护模型并对齐 L1D=64 KiB、DRAM=3 GiB。全程 2,939,373 UOP，其中 warmup
2,897,470 UOP；测量段为 40,000 用户 UOP 加 1,903 内核 UOP。

| 指标 | 旧依赖，当前快速内核 | 完整依赖，快速内核 | 完整依赖，通用内核 |
|---|---:|---:|---:|
| 合计 core cycles | 38,131 | 38,131 | 38,131 |
| cycles / user UOP | 0.953275 | 0.953275 | 0.953275 |
| cycles / 用户与内核宏指令 | 1.591112 | 1.591112 | 1.591112 |
| 反馈调用次数 | 16 | 16 | 16 |
| 并行反馈调用次数 | 3 | 3 | 3 |
| 测量段快速内核 UOP | 41,903 | 41,903 | 0 |

三者的目标时序与全部 scope PMU 相同：4,006 memory UOP、4,004 line requests、
249 L1D misses、248 LLC misses/DRAM reads、63 branch misses、12 DTLB misses。
本轮没有改善这个窗口的 CPI 或 PMU，也没有据此添加补偿项。

功能依赖确实增加：warmup 补回 10,461 条边，测量段补回 1,812 条边；对应扩展记录
分别为 27/56 条。依赖去重同时减少了重复边，第二阶段 absorbed/cross-epoch 边计数
因此变化。这证明完整输入进入了旧路径，但不足以认定其他时序误差已被修复。

完整依赖快速内核全程 wall time 为 0.6445 s，即约 4.56 M **全程 UOP/s**；该输入
98.57% 为 warmup，测量段仅约 45 ms，不用它替代上表长输入的吞吐对照或正式门禁。
FST 主文件和 `.deps` 未改写；本次新增磁盘格式开销为零，既有附件仍为 1,852 bytes。

## 后续边界

这次完成依赖合同和旧路径兼容接入，不是 gem5 精度验收。下一项应在既有反馈遍历内
统一同一 load 的请求／返回时刻、data-ready 与 UOP 完成下界，并验证 writeback
冲突如何传播。仍不增加全量 proposal，不迁移 `causal_read` 全局事件推进，不按
TeaLeaf CPI 残差调参。

证据目录：[tmp/two-stage-dependencies-20260909](../tmp/two-stage-dependencies-20260909/)。
`summary.json`、各次 JSON/log、冻结配置、`dependency-phase-audit.json`、
`layout-check.json`、修改前快照、`after-sha256.json` 和 `tests.log` 保存本次身份与结果。
