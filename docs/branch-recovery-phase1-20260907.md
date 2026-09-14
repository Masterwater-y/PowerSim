# 分支解析后正确路径恢复：第一阶段实现与验证

日期：2026-09-07。参考仍为 gem5；所有实验固定 Q=1024。本轮从
[全局优化审查](global-optimization-screening-20260907.md) 中已经证明的控制依赖缺口出发，
先取得真实 workload 见证，再实现一个默认关闭的单遍候选。结论是：**缺失边本身已被
修复并通过局部机制验证，但独立候选没有通过正误差控制，不能进入生产默认配置，也不应
扩展到 formal40/DSE54 完整矩阵。**

## 1. 实现前的真实见证

诊断在 `ChunkUopBound` 和 response-frontier audit 中补充 PC、branch、branch-miss
身份；`core.response_branch_recovery_audit=true` 会在原稀疏采样之外记录每个真实
predictor miss 及其下一条 committed UOP。审计只读目标时序，不改变模拟状态。

Graph500 C8 core 2 的完整 ROI 稀疏筛查覆盖 sequence 4,500,126–15,848,586：

| 指标 | 数值 |
|---|---:|
| 稀疏样本 | 53,868 |
| 成对 branch miss | 26,935 |
| 未成对 miss | 0 |
| 正确路径 fetch 早于分支解析 | 9,058 |
| 其中下一条 UOP 是 load | 4,733 |
| 正确路径 issue 也早于分支解析 | 6,746 |
| 其中下一条 UOP 是 load | 3,702 |

这只是必要先后关系的覆盖率，不是 CPI 周期和。为确认它确实能进入退休关键路径，另取
core 2 的连续 20,001-UOP 窗口（sequence 9,390,000–9,410,000）：12 个 miss 中
10 个违反必要 fetch 顺序，而且 10 个 successor 全是 load。固定服务反事实的最大局部
retire 位移为 137 cycles，窗口末位移回到 0；零改动回放逐项恒等。

窗口中最清楚的事件是：

| 字段 | 基线 |
|---|---:|
| branch sequence / PC | 9,399,199 / 4,232,224 |
| branch base completion | 7,850,884 |
| branch corrected completion | 7,851,434 |
| correct-path sequence / PC | 9,399,200 / 4,232,496 |
| correct-path fetch / dispatch / issue | 7,850,887 / 7,851,026 / 7,851,027 |
| fetch / issue 提前量 | 547 / 407 cycles |
| 第一直接消费者 | sequence 9,399,202，store，producer slot 1 |

因此实现门禁成立：这不是只在三条合成 UOP 中出现的结构问题，也不是把所有分支强制
串行化后人为制造的等待。

## 2. 默认关闭的候选

新开关为 `core.response_branch_recovery=false`；显式复现配置是
`configs/gem5-exp-branch-recovery.cfg`。候选只支持当前维护配置对应的单遍
interval-weave/time-epoch sparse response 路径，配置校验会拒绝 paired frontier、
event-only、suffix replay、pending-fill、hierarchy-walk、response retime 等并行实验。

每核维护一个跨 checkpoint 的绝对 `recovery_ready_cycle`：

1. response feedback 遇到真实 `branch_miss`，且 corrected completion 晚于该分支
   当前 checkpoint 的 base completion 时，发布 corrected completion；
2. 后续 committed UOP 的 fetch 不得早于这个前沿，差值沿现有 rename、dispatch、
   issue、IQ/ROB/LSQ 和退休路径传播；
3. 当未加该约束的前端时间追上前沿时清除；若 checkpoint 结束时仍未追上，则持久化；
4. 不增加第二遍 closure，也不额外叠加一个 redirect 常数。producer 已包含原基础恢复
   penalty；本候选只补 `correct-path fetch >= corrected branch completion` 这一必要下界；
5. 当前 checkpoint 已选择的 cache/coherence 路径不重放。候选改变后续 checkpoint
   的核时刻和跨核交错，因此不能宣称完整 cache/shared 状态等价。

统计把 miss 数、被 response 延迟的 miss、前沿更新/清除/跨 checkpoint、被 gate 的
UOP/内存 UOP、重叠等待总和和最大等待分别列出。等待总和可以大量重叠，不能当作 CPI；
只有互斥的 `response_critical_branch_recovery_cycles` 用于关键原因归因。

## 3. 微型差分

`tools/probe_branch_response_frontier.py` 同时运行原五个 3/4-UOP case 及其 recovery
版本。九项断言全部通过：

- 依赖 load 的真实误预测：branch completion=414；successor fetch 从 238 变为
  414，dispatch/issue/completion 为 418/419/420，必要违例 176→0；
- 去掉 load→branch 依赖的 miss：全部阶段和总周期保持不变；
- 预测正确控制：全部阶段和总周期保持不变；
- 正确路径 `load B → consumer B`：末尾从 448 变为 624，证明缺失边可以暴露后续
  内存链，但该数值仍不是 gem5 测量；
- recovery 开/关的所有五组对应 case，功能 PMU 均不变；
- generic audit 与 production materialized fast kernel 的 sum cycles、两种 CPI 和
  完整 scope PMU 分别逐项相同。

## 4. 真实窗口闭环

候选开启后使用同一 sequence/PC 身份复核，不比较因更早事件已变化而整体平移的绝对
时间：

| case / 窗口 | 基线 miss | 基线必要违例 | 候选必要违例 | 候选零改动回放 |
|---|---:|---:|---:|---:|
| Graph500 C8 core 2，20,001 UOP | 12 | 10 | **0** | 恒等 |
| ASTCENC C4 core 3，20,239 UOP | 92 | 11 | **0** | 恒等 |
| TeaLeaf LLC32 C4 core 1，20,244 UOP | 0 | 0 | 0 | 恒等 |

Graph500 的 sequence 9,399,199/9,399,200 在候选运行中分别以 corrected completion
8,265,515 和 correct-path fetch 8,265,515 相接，违例归零。三份 audit/generic 运行
与各自 production fast-kernel 运行的目标 `scope_metrics`（排除宿主 throughput）和
`threads` 逐项相同。

## 5. 全 ROI 小门禁与停止决定

只运行审查文档指定的三组 pilot，不运行完整矩阵。signed error 定义为
`(FastSim − gem5) / gem5`；formal 使用原标签 macro CPI，DSE 使用 user-UOP CPI。

| case | gem5 | 基线 | 候选 | 基线误差 | 候选误差 | 绝对误差变化 |
|---|---:|---:|---:|---:|---:|---:|
| Graph500 C8 formal | 1.877765542 | 1.619822561 | 1.651547110 | −13.7367% | −12.0472% | **−1.6895 pp** |
| ASTCENC baseline C4 | 0.3788582561 | 0.3256970587 | 0.3295259335 | −14.0319% | −13.0213% | **−1.0106 pp** |
| TeaLeaf LLC32 C4 | 0.7427406257 | 0.8144051186 | 0.8206064429 | +9.6487% | +10.4836% | **+0.8349 pp** |

对应 sum-core-cycle 增量分别为 1,547,618（+1.9585%）、153,155（+1.1756%）和
248,053（+0.7615%）。运行时激活计数也与功能 branch-miss 数严格相等：

| case | miss | corrected completion 被延迟 | gated UOP | gated memory UOP |
|---|---:|---:|---:|---:|
| Graph500 C8 | 213,027 | 117,441 | 10,627,123 | 1,537,696 |
| ASTCENC C4 | 153,454 | 38,358 | 3,151,836 | 450,272 |
| TeaLeaf LLC32 C4 | 6,394 | 5,463 | 1,680,284 | 250,913 |

retired instructions/UOP、memory UOP、line request、branch/miss 和 DTLB 计数保持不变；
少量时序敏感的 L1/L2/LLC/coherence 分类发生变化。例如 Graph500 的 3,974,177 次
L2 access 中有 80 次 miss→hit，remote supply 增加 164。这来自候选改变后续
checkpoint 的跨核交错，也再次说明当前 checkpoint 未做 cache/coherence closure，
不能把候选描述成完整层次状态重放。

候选是只增加必要时序下界的单调修复，因而只改善两个负误差 case，同时把正误差控制
恶化 0.8349 pp；恶化量与 ASTCENC 的收益处于同一量级。按
`docs/optimization-decisions.md` 的明确停止条件，**本候选停在实验状态：代码、测试、
反例和复现入口保留，默认关闭，不调参、不跑 formal40/DSE54，也不做生产吞吐门禁。**
后续若继续，必须与一个有独立事件证据、能减少正误差的更早流水线/共享状态修复共同
评估，而不是按 workload 或误差符号选择性关闭这条真实控制依赖。

## 6. 无扰动与工程验证

当前二进制 SHA256：
`0cdf8a0811d7ac6747bf949d45b7ee4a2faeae695ff281f494b3404b83dda961`。

- 新二进制、候选关闭时，Graph500/ASTCENC/TeaLeaf 三个全 ROI 的目标
  `scope_metrics`（排除宿主 throughput）和 `threads` 与冻结基线逐项相同；
- `cmake --build build -- -j16` 通过；
- `./build/fastsim_tests` 通过；
- `python3 tests/test_branch_response_recovery.py` 通过（3 tests）；
- `python3 tools/probe_branch_response_frontier.py --output-dir <dir>` 通过全部断言；
- 结果根目录：`tmp/branch-recovery-20260907-audit-v1/` 和
  `tmp/branch-recovery-20260907-candidate-v1/`。

关键产物 SHA256：

| 产物 | SHA256 |
|---|---|
| candidate micro summary | `e679887a2804dc530e819c40b4a6f455a942b7c73992bfe95ae1e10f56af9607` |
| Graph500 full sparse witness | `dff049ccf2719ed5aed611acd85c55ad838a748265de0724b86bd7520ee34836` |
| Graph500 baseline dense witness | `6af33bf9d9e63826a2b786d65c3802500bfd13edb8efad86b548e94ad44056c5` |
| Graph500 candidate dense witness | `f4e312993c30249f31c18554a97e17a556eea12d7457d9e66e5960c2f7d4fc5b` |
