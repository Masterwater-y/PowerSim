# TeaLeaf CPI 误差根因与修复方案（2026-09-03）

## 1. 文档目的

本文记录 SPEC CPU 2026 `811.tealeaf_s` 在当前微架构探索数据组中的 CPI
误差、已有证据、修复动机、实施顺序和验收标准，避免后续再次用统一延迟常数、
逐负载修正系数或未经闭环验证的实验开关掩盖问题。

本文描述修复方案及其分阶段实施状态。P0 度量修正、P1 前置定向诊断和固定序列
response-frontier 审计已于 2026-09-03 完成；可组合 response frontier 与条件式共享
时序闭环本身尚未实现，也没有进入生产配置。
当前维护的生产别名仍为 `configs/gem5-fs-native-kernel.cfg`，它指向 v28.6。

自 2026-09-04 起，新的 TeaLeaf 建模实验固定
`sim.interval_max_cycles=1024`。历史 Q sweep 只保留为 checkpoint 组合性诊断，禁止按
gem5 CPI 误差选择 Q，也不再把 Q 当作寻优参数。

## 2. 当前结论

TeaLeaf 的主要问题不是需求 cache miss 数量。截至本轮，已经直接确认的首要缺陷是
response timing 在 time-epoch checkpoint 之间不满足组合不变性：Q 从 128 增大到
2048 时，C4/C8 的非 response lower-bound 周期完全不变，而所有总周期差都逐周期
等于 `response_critical_total_cycles` 的差。共享请求重排、ROB/LSQ 生命周期和 DRAM
约束仍是可能的后续贡献项，但尚不能把其中任一项单独宣称为已验证主因。

固定序列审计进一步把首个生效分叉定位到 response-corrected ROB/SQ admission 与
ordered commit，而不是共享 arrival：测量入口处，各 Q 的当前 UOP 阶段时间仍一致；
随后 Q=128 与 Q=1024 的 4096-UOP 细粒度对照中，C4 四个核首次采样到的时序分叉
分别由一次 ROB capacity 和三次 SQ capacity 触发，C8 八个核中七个首先进入 ROB
capacity 路径，另一个首先出现 ordered-retire dependency 分叉。这个证据限定了 P1a
下一版状态合同，但还不能把 4096-UOP 采样点当作精确的第一条分叉 UOP。

当前最新 v28.6 的 18 个 TeaLeaf 配置（9 种微架构乘 C4/C8）结果为：

| 指标 | 数值 |
|---|---:|
| 平均 CPI 绝对误差 | 11.824% |
| 平均 CPI 有符号误差 | -10.418% |
| baseline C4 误差 | -11.771% |
| baseline C8 误差 | -11.288% |

负号表示 FastSim 将 TeaLeaf 模拟得偏快。此前 10 负载 C4/C8 集合中，TeaLeaf
平均绝对误差为 9.199%；当前输入和 ROI 的参考 CPI 更低，因此相似的绝对 CPI
缺口会表现为更大的百分比误差。

用于完整归因的 v28.2 baseline 数据如下；v28.6 只小幅改善，根因不变：

| 指标 | C4 | C8 |
|---|---:|---:|
| gem5 周期 | 26,786,507 | 33,902,521 |
| FastSim 周期 | 23,482,484 | 30,045,197 |
| 缺失周期 | 3,304,023 | 3,857,324 |
| 缺失 CPI | 0.08260 | 0.04822 |
| CPI 误差 | -12.335% | -11.378% |

## 3. 修复动机与证据

### 3.1 cache miss 数量基本正确，但 CPI 仍低估

baseline PMU 对比如下：

| PMU | C4 误差 | C8 误差 | 判断 |
|---|---:|---:|---|
| L1D miss | -6.96% | +0.25% | 不是主要误差量 |
| Private-L2 miss | +0.06% | -1.21% | 基本一致 |
| LLC tag miss | +0.09% | +0.20% | 基本一致 |
| DRAM read | -5.90% | -0.94% | C4 有部分缺失 |
| DTLB miss | -42.37% | -34.21% | 明确缺口 |
| LSQ full | -77.67% | -78.93% | 生命周期/口径需要修复 |

L2 和 LLC miss 基本一致而 CPI 仍缺失数百万周期，说明主问题位于请求 arrival、
服务顺序、MLP 暴露和响应后的资源生命周期，而不是简单少生成了大量需求 miss。

### 3.2 大量修正后的请求越过当前 epoch

当前 time epoch 大小为 1024。归因审计发现：

| 指标 | C4 | C8 |
|---|---:|---:|
| 修正后越过 horizon 的内存事件 | 2,875,267 | 2,654,727 |
| 占全部内存事件 | 51.0% | 23.7% |
| 跨 epoch 依赖边 | 138,483 | 214,596 |
| horizon violation | 13,527 | 21,462 |

这些请求不会被直接丢弃。它们直接说明 response-corrected core 状态大量存活到
checkpoint 边界，因此边界组合语义是高风险点；它们本身不等价于共享请求顺序已经
倒置。新增生命周期账本进一步区分了两个坐标：共享层次阶段由
`shared_stage_issue` 生成，而 core 使用 `corrected_issue` 加相对响应延迟。把共享阶段
平移到 corrected-issue 坐标后，baseline C4/C8 的有效
`corrected issue > controller arrival` 比例均为 0；未平移的 90.706%/86.056% 只是
混合坐标差异，不能作为重放共享层次的直接证据。

生产配置关闭了：

- `sim.interval_response_retime`；
- `sim.interval_corrected_suffix_carry`；
- `core.response_sparse_resource_repair`。

现有 retime 在请求跨 epoch 时 fallback；现有 suffix carry 需要扫描和重放过大
后缀，历史诊断无法满足单任务 60 秒的运行约束。因此不能简单打开已有开关。必须先
使 core response frontier 在不同 Q 分割下等价；只有同坐标的请求顺序审计实际发现
倒置时，才进入增量式共享状态重放。

相关代码：

- `src/simulator.cpp`：修正后 epoch 边界审计、响应传播和跨 epoch fallback；
- `include/fastsim/types.hpp`：committed epoch 与逐请求生命周期账本；
- `configs/gem5-v28_1-time-epoch.cfg`：当前默认开关。

### 3.3 响应传播到依赖，但没有完整传播到 ROB/LSQ 容量

v28.2 归因显示：

| 响应关键周期 | C4 | C8 |
|---|---:|---:|
| 总计 | 15.08M | 12.42M |
| dependency | 13.98M | 11.68M |
| 直接 memory response | 1.08M | 0.73M |
| ROB/IQ/LQ/SQ capacity 合计 | 17.9K | 7.4K |

FastSim 已经把大量响应延迟传播到寄存器依赖和 ordered retire，却几乎没有生成
对应的 ROB/LQ/SQ capacity critical 周期。与此同时，gem5 对照下 LSQ full
计数被 FastSim 低估约 78%。虽然占用计数目前仍属于诊断口径，这两个核数上相同
方向的差异仍说明 response 延迟没有完整改变资源释放和后续 dispatch/issue。

v28.6 使用 `MaterializedFastKernel`；当前代码会用
`!MaterializedFastKernel` 排除 sparse resource repair。因此未来修复必须同时覆盖
通用标量路径和 materialized 快路径，不能只修改实验路径。

### 3.4 缺失的 DRAM command timing 是已确认贡献项

当前生产配置中以下 DRAM 字段仍为 0：

- `tRAS`、`tRTP`、`tRRD`、`tRRD_L`；
- `tXAW`、`activation_limit`、`tCCD_L`、`tCS`。

仅补齐这些字段的消融结果为：

| 结果 | C4 | C8 |
|---|---:|---:|
| FastSim CPI，原始 | 0.58706 | 0.37556 |
| FastSim CPI，完整 DDR calendar | 0.60822 | 0.38355 |
| 补回周期 | 846K | 639K |
| 占原缺口 | 25.6% | 16.6% |

因此 DRAM command timing 至少解释约 2 到 3 个 CPI 误差百分点。但在 checkpoint
组合仍依赖 Q 时直接启用全部约束，可能在其他配置上过度收费。实施顺序必须是先修
response frontier 的边界组合，再正式启用完整 DDR calendar；共享 arrival 重放则
以同坐标顺序审计为前置证据。

### 3.5 DTLB/page walk 是明确的建模缺口

当前模型使用 64 项 LRU DTLB、单 walker 和固定服务延迟，不把多级 page walk
请求送入 L1/L2/Ruby/DRAM。baseline 中：

| 指标 | C4 | C8 |
|---|---:|---:|
| FastSim DTLB miss | 8,325 | 15,630 |
| gem5 DTLB miss | 14,446 | 23,759 |
| 少算 | 6,121 | 8,129 |

按当前 walker 的平均累计延迟粗略外推，该缺口的数量级约为总缺失周期的 11% 到
12%，但部分延迟会被执行重叠，不能作为可直接相加的 CPI 收益。C4 中需求 LLC
miss 基本一致而 DRAM read 少 13,722 次，也与 page walk 等非普通需求访问没有
进入共享内存层次相符。

2026-09-04 的代码级审计进一步确认，旧实现不仅少了请求数量，还把固定 walk 完成
直接当作 DTLB 可见填充；当真实 PTE 响应更晚时，后继访问会使用未来状态。当前 P4
pilot 已用 `.vmap` v2 的初始/ROI-entry 功能快照生成真实 PTE 物理请求，并用唯一 walk
generation 把后继 hit 绑定到正确的实际填充。它不读取最终页表，也不使用 gem5 hit /
latency 标签。gem5 x86 walker 对未完成 walk 的 follower 不合并，而是排队重新执行
完整 walk；当前 pilot 对这类访问只做因果等待，尚未生成 follower PTE 流量，因此仍
是有明确残差的下界模型。

### 3.6 CHA PMU 当前混合了不同事件定义

FastSim 的 `cha.requests` 包含 permission upgrade，而 gem5 标签
`cha_llc_demand_accesses` 主要表示 demand lookup：

```text
C4: 304,542 L2 miss + 53,522 upgrade = 358,064 CHA request
C8: 366,330 L2 miss + 180,111 upgrade = 546,441 CHA request
```

因此当前 TeaLeaf CHA lookup 的 +17.5%/+46.7% 至少部分是 PMU 口径错误，不能
直接解释为相同幅度的共享请求数量误差。demand lookup、upgrade 和 remote supply
必须分别报告；upgrade 仍需参与时序模型。

### 3.7 当前误差不是统一的 memory latency 常数

TeaLeaf 在不同微架构配置上同时存在低估和高估。例如 v28.6：

- baseline C4：-11.77%；
- L2 2 MiB C4：+2.58%；
- LLC 32 MiB C4：+10.08%；
- LLC 128 MiB C8：-17.38%。

LLC 32 MiB C4 中，gem5 相对 baseline 的 speedup 为 0.902，FastSim 为
0.723，FastSim 将性能损失放大约三倍。这说明错误来自请求顺序、MLP 和资源
暴露的非线性变化，不能通过统一增加 DRAM latency 或 TeaLeaf 专属缩放系数解决。

## 4. 已排除或非主要的机制

- ROI、FST 和 CPI 分母：trace、label 与推理记录数一致，均按 user retired UOP
  计算 CPI。
- warmup：source warmup 已启用，cache 和架构状态被保留；其不能恢复精确推测态，
  但不足以解释完整 10M 区间的稳定偏差。
- native kernel：占比小，user-only/native 对照改善不足。
- branch：TeaLeaf baseline 只有约 7 千次 branch miss，无法解释数百万周期。
- StoreSet：C4 仅 8 条边、18 个扩展周期；C8 仅 2 条边、5 个周期。
- rename free list：直接实验为零 stall，CPI 不变。
- 提交路径 I-cache：v28.6 只补回 C4 约 151K、C8 约 30K 周期，不是主项。

## 5. 修复方案

### P0：固定度量口径和回归输入

1. 所有新结果保存展开后的有效配置、版本和哈希。
2. TeaLeaf 当前 18 组 FST 保留为冻结回归集，不立即重采。
3. 修正 CHA PMU：demand lookup、permission upgrade、remote supply 分开比较。
4. 对 DTLB、LSQ、ROB/IQ 的正式指标与诊断指标做显式区分。

动机：先排除指标定义错误，避免为错误的 PMU 差异修改时序模型。

### P1：先实现可组合的 response frontier，再决定是否重放共享时序

P1a 先解决已经被 Q 账本直接证明的边界问题：

1. 保留 response-delayed producer、dependency、ROB/LSQ/IQ、commit 和 store-drain
   的完整开放 frontier，不把尚未闭合的 checkpoint-tail delay 永久写入全核标量 gap。
2. 在与 Q 无关的共同 UOP 里程碑记录 frontier 摘要，比较 Q=128/256/512/1024/2048
   的最早分歧位置和具体状态字段。
3. 只有当开放波被后续下界 slack 吸收，或形成已闭合的新增关键路径时，才结算一次
   response-critical 周期。
4. 要求相邻 Q 的非 response lower-bound 保持相同，总周期变化与新增关键路径账本
   守恒，且 CPI 差异不超过 2%。

P1a 通过后，再用统一坐标审计真实的共享请求顺序。如果确有倒置，P1b 才执行：

1. 使用当前 core 状态生成候选 memory issue 时间。
2. 按候选时间运行 CHA/LLC/DRAM。
3. 将 response 传播到 producer、consumer、completion、retire 和资源释放。
4. 找出 issue 时间改变并造成全局顺序倒置的共享请求。
5. 从最早受影响事件恢复共享状态，只重放受影响的因果连通分量。
6. 请求越过 horizon 时裁掉该请求及其因果后缀，携带到下一 epoch。
7. 只有请求顺序与时间稳定后才提交 epoch 状态。

为了满足单任务 60 秒约束，不能扫描整个 UOP suffix。建议使用：

- 带 generation 的 memory-event 最小堆；
- response 到 consumer 的稀疏反向依赖索引；
- 只覆盖 ROB 窗口和跨 epoch compact frontier 的资源日历；
- 从最早倒序事件开始的局部 shared-state checkpoint/rollback；
- 单调延迟固定点，时间只允许向后移动。

动机：P1a 是现有数据直接确认的上游缺陷；P1b 是需要新顺序证据才能启动的条件性
修复，避免对并不存在的有效 issue/arrival 逆序做昂贵重放。

### P2：让 ROB/LSQ 生命周期跟随响应修正

需要同时修复：

- response 延迟后的 ROB release；
- load completion 后的 LQ release；
- store address-ready、commit、send、response 与 SQ release；
- LSQ full 对 rename/dispatch 的背压；
- moved UOP 对 FU、issue port 和 writeback port 的重新竞争；
- 通用标量路径与 `MaterializedFastKernel` 的语义一致性。

动机：当前响应关键周期几乎全部归入 dependency，而 LSQ full 持续低估约 78%。

### P3：在 Q 稳定的请求时间线上启用完整 DRAM 状态机

闭环完成后再启用缺失的 8 项 DRAM 参数，并验证：

```text
request create
  -> sequencer accept
  -> controller enqueue
  -> FR-FCFS select
  -> ACT/RD/PRE/bus
  -> response
```

使用小规模 gem5 debug 记录逐请求阶段时间作为离线验证 oracle；这些时间不得进入
FastSim 的生产输入。

动机：DDR calendar 已直接补回 16.6% 到 25.6% 的 baseline 缺失周期，但 Q 相关的
checkpoint 组合会使精确 command timing 变成对不稳定请求流的精确模拟；若同坐标
审计后再发现 arrival 顺序错误，也必须先修正该顺序。

### P4：实现 cache/Ruby 可见的 page walk

DTLB miss 应生成多级 PTE 请求，并支持：

- walker 并发数和 walk 合并；
- page-walk cache；
- CR3/ASID 作用域；
- 4 KiB 页与大页；
- page walk 与普通请求争用 Ruby、LLC 和 DRAM。

现有 FST 足以开展 P1 到 P3。P4 如果缺少 PTE 物理页关系，可以增加只包含 PTE
地址、页大小和地址空间身份的功能性 sidecar；不得记录 gem5 服务延迟。

实施状态（2026-09-04）：sidecar、实际 2/3/4 层深度、普通层次请求、单 walker
串行响应、CR3 作用域、phase-local 缺失回退和 generation 因果检查已经完成并通过
单元测试；no-coalescing follower 的条件式完整 walk 尚未完成，生产配置保持关闭。

### P5：审计 MESI 共享和 PMU 语义

1. 将 CHA demand、upgrade、remote supply 分开计数。
2. 对 TeaLeaf C8 较多的 upgrade 检查 directory sharer/owner、GETS/GETX 和
   invalidation 流程。
3. 为 ROB/IQ/LSQ full 建立与 gem5 完全相同的事件定义后再纳入正式 gate。

动机：这一步主要提高 PMU 可信度，并防止共享状态差异污染请求顺序诊断。

## 6. 验证与晋级门槛

修复按以下顺序验证：

1. TeaLeaf baseline C4/C8；
2. TeaLeaf 全部 18 组配置；
3. Stockfish、astcenc、TeaLeaf 六个 baseline；
4. 完整 54 组微架构探索矩阵。

运行约束与验收标准：

- 每个 FastSim case 设置 60 秒 watchdog；
- 使用主机可用核心尽可能并行；
- baseline C4/C8 CPI 误差分别低于 6%；
- TeaLeaf 18 组 P90 CPI 误差低于 10%；
- 显著配置的 speedup 方向和两两排序准确率不低于 90%；
- L2/LLC miss 精度不得回退；
- DTLB、DRAM、LSQ 指标必须与 CPI 同方向改善；
- LLC 32 MiB C4 不得继续出现过度惩罚；
- 禁止逐负载 CPI、memory exposure 或 latency 修正系数。

若某个候选只能改善 TeaLeaf、却使 Stockfish 或 astcenc 明显回退，不得晋级为生产
配置。

## 7. 是否需要重新采集 FST

当前不需要重采 54 组 FST。现有 FST 已包含 P1 到 P3 所需的 committed UOP、
memory address、PC、依赖距离、CPL/ASID 和虚拟页信息，适合做闭环和资源生命周期
修复。

只有以下情况才考虑扩展 trace 合同：

- PTE 物理访问关系无法从现有虚拟页 sidecar 重建；
- 需要功能性的跨核同步顺序，而现有记录无法区分合法的共享事件次序；
- 需要补齐 operand-complete 的静态指令映射。

扩展字段只能表达功能事实，不能携带 gem5 时序 oracle。先用小规模 pilot 证明新增
字段有收益，再决定是否重新采集完整 54 组。

## 8. 关键数据与参考

- 当前 54 组 v28.2 报告：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/evaluation-v28_2-native/generalization-report.md`
- 当前 54 组 v28.6 报告：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/evaluation-v28_6-maintained/generalization-report.md`
- v28.6 profile identity 审计：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/profile-identity-v28_6/summary.md`
- 历史 controller-arrival 结论：
  `docs/fs-profile-frontend-repair-2026-08-17.md`
- 历史 10 负载 v28.2 结果：
  `docs/stockfish-store-set-root-cause-20260821.md`
- 当前语义对齐审计：
  `docs/fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md`

## 9. 实施原则

TeaLeaf 修复必须解决通用机制，而不是拟合单个负载。最终目标是同时得到：

1. 正确的需求事件数量；
2. 因果一致的跨核请求顺序；
3. 与目标一致的 DRAM 服务约束；
4. 响应延迟驱动的 ROB/LSQ 背压；
5. 可解释、口径一致的 PMU；
6. 在 60 秒单任务预算内可用于完整参数探索的吞吐率。

只有上述闭环同时成立，TeaLeaf 的绝对 CPI、微架构 speedup 和排序精度才可能一起
改善，而不会继续出现 baseline 低估、部分 cache 变体又过度惩罚的抵消现象。

## 10. 项目 1/2 实施记录（2026-09-03）

### 10.1 项目 1：度量口径和结果溯源

已完成：

- CHA 正式比较改为 `demand lookups = requests - permission upgrades`；
  permission upgrade 和 remote supply 单独报告，remote supply 不重复加入 demand；
- ROB/IQ/LSQ full 明确标记为 `proxy`，不再作为同口径正式 gate；
- 新实验保存实际有效配置、二进制/配置/trace/label 哈希和 experiment ID；
- 增加指标口径回归测试；
- 使用修正后的 v3 口径重新生成 54-case v28.6 报告。

修正后 variant CHA demand WAPE 为 `0.825%`，pooled signed error 为 `-0.490%`，
通过 2% gate；此前约 `28.365%` 的数值主要是把 upgrade 混入 demand 造成的定义
错误。TeaLeaf baseline 分解为：

| 核数 | Total requests | Demand lookups | Upgrades | Remote supplies | gem5 demand | Demand error |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 358,104 | 304,587 | 53,517 | 339 | 304,658 | -0.023% |
| 8 | 549,245 | 369,297 | 179,948 | 1,974 | 372,558 | -0.875% |

报告：
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/evaluation-v28_6-maintained-pmu-v3/generalization-report.md`。
CHA 口径通过不代表整体模型通过；54-case 总体 gate 仍因 CPI、吞吐和方向/排序问题
失败。

### 10.2 项目 2：C4/C8 定向实验和逐请求守恒账本

新增 committed-data unique-DRAM-read 生命周期账本，逐请求记录：

```text
candidate create
  -> corrected issue
  -> controller arrival
  -> controller service
  -> response
  -> ordered retire
```

账本不钳制逆序时间，而是单独统计 backward event/cycle，并验证六阶段 population
相等和 signed adjacent-stage timing identity。定向 runner 对每个实验显式展开并验证
配置，使用 TeaLeaf baseline C4/C8、60 秒 case watchdog。首轮完成 8 个 variant、共
16 个 case：Q=512/1024/2048、现有 causal timing off/on、store post-commit off/on、
DDR ACT 组、DDR column/rank 组和两组组合；后续又补跑 Q=128/256 和稀疏资源、
speculative DTLB 定向项。

| 实验 | C4 CPI 变化 | C8 CPI 变化 | C4/C8 剩余 gem5 误差 | 结论 |
|---|---:|---:|---:|---|
| Q 512（相对 1024） | +3.443% | +3.325% | -8.734% / -8.339% | Q 未收敛，不能按贴近 gem5 选 Q |
| Q 2048（相对 1024） | -2.153% | -2.904% | -13.671% / -13.865% | 同上 |
| 现有 causal timing | +1.620% | +1.121% | -10.342% / -10.294% | 有效但不足，不是计划中的增量 P1 |
| Store post-commit | +0.195% | -0.025% | -11.599% / -11.310% | 不是主要修复方向 |
| DDR ACT 组 | +2.557% | +0.728% | -9.515% / -10.642% | 有真实贡献 |
| DDR column/rank 组 | +2.192% | +1.479% | -9.837% / -9.976% | 有真实贡献 |
| DDR 两组组合 | +3.504% | +2.125% | -8.680% / -9.403% | 仅部分补偿，不能单独晋级 |

Q 的四个相邻比较全部超过 2% 收敛线。baseline 中，未投影账本的 corrected issue
晚于 controller arrival 比例为 C4 `90.706%`、C8 `86.056%`；这是把 corrected core
时间与在 `shared_stage_issue` 上生成的绝对共享时间混合比较。将共享阶段整体投影到
corrected-issue 坐标后，有效逆序比例均为 0。因此旧比例不能直接验证共享请求顺序
缺口。现有 causal 路径共发现 119 个 candidate epoch，其中 58 个稳定、61 个
fallback，未达到 6% baseline CPI 门槛。

所有 16 个 case 的请求 population、signed timing、response-stage 和 epoch-memory
账本均守恒；通用 attribution baseline 与当前 materialized v28.6 的 CPI 完全一致。
完整报告和逐 case CSV/JSON：
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/tealeaf-cpi-directed-v1/`。

### 10.3 Q 周期归因和旧时间线结论修正

当前二进制重新跑完 Q=128/256/512/1024/2048 的 C4/C8 共 10 个 case。八个相邻
比较的 CPI 变化为 `2.200%` 到 `6.276%`，全部未通过 2% 收敛线。周期分解得到更强的
定位证据：

| 核数 | 所有 Q 下固定的非 response lower-bound | Q128 response-critical | Q2048 response-critical |
|---:|---:|---:|---:|
| 4 | 8,436,678 | 18,814,602 | 14,687,961 |
| 8 | 17,647,902 | 17,165,745 | 11,554,158 |

每一个相邻 Q 比较都满足：

```text
delta(sum_core_cycles) == delta(response_critical_total_cycles)
delta(non_response_lower_bound_cycles) == 0
```

这把首要缺陷定位到 response-critical checkpoint-tail 的组合方式，而不是 lower-bound
core 模型。当前有效报告：
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/tealeaf-q-ledger-v1/`。

### 10.4 已否决的两个边界修复 pilot

先后验证了两个看似直接的修复，并按 gate 撤回实现：

1. 在 ROB-head suffix 处理后再把响应修正 issue 送回共享层次。100K C4 pilot 从
   baseline CPI `0.586788` 放大到 `2421.304483`，response-critical 周期达到
   `1,622,612,882`。同一个 response delay 被重新喂给 issue/queue，形成正反馈，
   证明“调整执行顺序”本身不能解决双重收费。
2. 不结算开放 ROB admission wave 的标量 gap，只携带 sparse calendar 到后续
   checkpoint。100K pilot 看似收敛，但完整 10M C4/C8 反例中，C8/Q256 CPI 达到
   `0.594844`、gem5 误差 `+40.366%`；相邻 Q 最大变化 `29.187%`。单线程复跑完全
   重现。原因是开放波长期不闭合时，绝对 sparse dependency/retire 状态与缺失的
   跨边界状态继续产生 Q 相关递推。该实验开关、统计字段和测试已从源码撤回。

两个 pilot 都保持 population/timing ledger 守恒，但未通过数值收敛和绝对 CPI gate；
这也说明“账本守恒”是必要条件，不是充分条件。

### 10.5 已完成：固定序列 response-frontier 审计

新增默认关闭的 `core.response_frontier_audit_stride_uops`。审计在每个核的测量入口和
固定绝对 UOP 序号取样，记录 fetch/rename/dispatch/issue/completion/retire、capacity
原因、ROB/LQ/SQ cursor 与头部 release、IQ/Sequencer 最小 release、commit 和
store-drain；完整队列使用稳定 FNV-1a 摘要。所有时间先减去当前 interval gap，避免
把单纯的全局时间原点平移误判为结构分叉。审计要求 attributed time-epoch sparse
路径并禁用 causal-block transfer；生产默认值为 0。

审计开启时，为了不跳过里程碑，会走完整逐 UOP 等价路径而不使用 response-inactive
快速证书。定向测试同时比较 block-summary 与逐 UOP ROB 写回路径的阶段和摘要，并
验证审计开关不改变周期或 PMU。

完整 Q=128/256/512/1024/2048、C4/C8 共 10 个 case 得到：

- C4 有 618 个跨全部 Q 的共同样本，C8 有 1233 个；
- 新旧运行的 CPI、总周期、response-critical 周期、非 response lower-bound、DTLB、
  CHA request 和 DRAM read 逐项完全相同；
- production equivalence、request/response/epoch ledger 全部通过；
- 首个 normalized frontier 差异已在测量入口存在，但对应 release 均早于当前 UOP，
  当前 UOP 阶段仍完全相同，所以这是过期历史状态而不是首个生效约束；
- 首个被 65536-UOP 周期采样捕获的当前时序差异为 C4 core 2 / sequence 393215 的
  SQ-capacity 路径，以及 C8 core 6 / sequence 262143 的 ROB-capacity 路径。

完整报告：
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/tealeaf-frontier-audit-v4/`。

随后仅对 Q=128/Q=1024 做 4096-UOP 细粒度复跑。下表列出每个核首个被采样到的
当前-UOP 时序差异；真实第一条分叉位于该点之前至多 4095 个 UOP 内。

| 拓扑 | 首次触发分类 | 采样证据 |
|---|---|---|
| C4 | 1 个 ROB，3 个 SQ | core 0: seq 2002943，Q1024 `rob_capacity` / Q128 无容量约束；core 1/2/3: seq 2113535/229375/372735，均为 `sq_capacity` 且延迟量不同 |
| C8 | 7 个 ROB，1 个 ordered-retire dependency | core 0/2/3/5/6/7: Q1024 首先进入 `rob_capacity`、Q128 未进入；core 4 两者均为 ROB 但 release 不同；core 1 首先在 retire/commit 分叉 |

细粒度原始结果：
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/tealeaf-frontier-fine-q128-q1024-v1/`。

### 10.6 下一实施边界：配对 lower-bound/open-response frontier

下一版 P1a 不应再尝试整体关闭标量 gap，也不应改共享 arrival。它应先把 ROB、SQ
和 ordered commit 的 lower-bound 状态与 response displacement 成对保存，使一次
checkpoint settlement 能区分已经闭合的公共平移和仍可能被 OoO slack 吸收的开放
波。具体合同为：

1. ROB retire/completion、SQ release/store-drain 和 commit 都保存 base release 与
   response displacement，禁止仅凭 checkpoint 最后一个 retire 的 extra 结算全核；
2. checkpoint 结束时，只把对完整可执行 frontier 都成立的公共位移转入 closed gap，
   其余位移继续留在开放 frontier；不能复用已否决的“开放就完全不结算”二值规则；
3. closed-gap、open-frontier 和最终未闭合 tail 分账，并验证三者与总新增关键路径守恒；
4. 先跑 Q128/Q1024 的 4096-UOP 定向对照，要求上表中的首个 ROB/SQ 分叉消失或由
   显式 open-wave 账本解释，再跑五个 Q 的 C4/C8 全矩阵；
5. 只有相邻 Q CPI 全部低于 2%、Q1024 C4/C8 误差低于 6%，才允许进入 DDR 组合和
   18-case/54-case 回归。

首个修复 pilot 仍必须同时满足：

1. 六阶段 population、signed timing、response-stage 和 cycle-delta 账本继续守恒；
2. 所有共同 UOP 里程碑的 frontier 摘要跨 Q 一致，或差异由显式开放事件账本解释；
3. 五个 Q 的相邻 CPI 差异不超过 2%；
4. 单 case 不超过 60 秒，Q=1024 的 C4/C8 CPI 误差分别低于 6%；
5. 只有同坐标顺序审计发现真实倒置后，才实施 shared-state checkpoint/rollback；
6. 通过后才组合 DDR 两组约束，并进入 TeaLeaf 18-case 和完整 54-case 回归。

Store post-commit 暂不作为主线；DDR 参数也不应在 Q 相关的请求流上直接晋级。此次
实施未修改生产别名，未增加逐负载修正系数，也未重采 FST。

### 10.7 P1a 实施结果：Q 与绝对 CPI 门禁均未通过

已按 10.6 实现默认关闭的 `core.response_paired_frontier` pilot。ROB completion /
retire、SQ release、store-drain 和 ordered commit 均保存 lower-bound base 与有符号
response displacement；completion 可以在公共退休平移闭合后保留负位移。公共位移随
完整语义 frontier 的推进结算，checkpoint 只提交已经闭合的 gap；仍开放的位移跨
checkpoint 保留，最终仅 ordered-retire tail 计入核完成时间。实现同时输出
closed-gap、open-frontier high-water、final-open 与 final-tail 账本。维护配置仍显式
保持该开关为 `false`，windowed `advance()` 暂不接受这个只完成了整段 `run()` 结算
合同的实验路径。

定向单元回归使用同一条 response/ROB/SQ trace 比较 Q=4 与 Q=16，功能、cache
population 和最终 cycles 完全一致。随后按合同使用 4096-UOP 审计步长复跑 TeaLeaf
的五个 Q：

| 拓扑 | Q | FastSim CPI | gem5 CPI | signed error | response cycles | closed gap | final tail | wall time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| C4 | 128 | 0.577016 | 0.669663 | -13.835% | 14,643,959 | 14,643,244 | 715 | 21.14 s |
| C4 | 256 | 0.576977 | 0.669663 | -13.841% | 14,642,387 | 14,641,672 | 715 | 23.13 s |
| C4 | 512 | 0.576813 | 0.669663 | -13.865% | 14,635,861 | 14,635,146 | 715 | 22.19 s |
| C4 | 1024 | 0.575940 | 0.669663 | -13.995% | 14,600,939 | 14,600,226 | 713 | 19.09 s |
| C4 | 2048 | 0.568688 | 0.669663 | -15.078% | 14,310,847 | 14,310,132 | 715 | 19.41 s |
| C8 | 128 | 0.366605 | 0.423781 | -13.492% | 11,680,468 | 11,680,379 | 89 | 22.06 s |
| C8 | 256 | 0.367126 | 0.423781 | -13.369% | 11,722,206 | 11,722,116 | 90 | 22.96 s |
| C8 | 512 | 0.367266 | 0.423781 | -13.336% | 11,733,415 | 11,733,324 | 91 | 22.33 s |
| C8 | 1024 | 0.365841 | 0.423781 | -13.672% | 11,619,391 | 11,619,301 | 90 | 18.75 s |
| C8 | 2048 | 0.358175 | 0.423781 | -15.481% | 11,006,064 | 11,005,974 | 90 | 21.11 s |

Q128 到 Q1024 的各段相邻 CPI 差异不超过 0.390%，但 Q1024→Q2048 为 C4
1.275%、C8 2.140%；C8 超出 2% 门限 0.140 个百分点。因此 endpoint pilot 显示的
约 0.2% 不能代表完整五 Q 门禁通过。五个 Q 的 non-response lower bound 始终分别
固定为 C4 8,436,678 cycles、C8 17,647,902 cycles。十个 case 的 DRAM 六阶段
population、signed timing、response-stage、epoch memory-event 和 paired-frontier
账本全部守恒，单 case 也均低于 60 秒。固定序列审计
中，首个当前-UOP 时序差异分别落在 C4 core 2 / sequence 229375 的 SQ-capacity
开放波和 C8 core 6 / sequence 237567 的 ordered-retire 开放波；对应样本的
open-frontier 均非零，因此残余 Q 差异已显式留在开放账本，而非被重复结算进 closed
gap。

此外，Q1024 的绝对误差仍为 C4 -13.995%、C8 -13.672%，明显超过 6% 门禁。
以 gem5 CPI 和相同 user-UOP 分母反推，在固定 lower bound 之外仍分别缺少约
3,748,890 / 4,635,228 response-critical cycles。这说明旧 Q128 接近 gem5 的部分主要
来自 checkpoint 边界重复放大的偶然补偿；配对 frontier 显著压低了 Q 敏感性，但
尚未完全修复分区不变量，也不是缺失 CPI 的来源模型。由于五 Q 和绝对 CPI 两项都
失败，本轮不组合 DDR，不进入
18-case/54-case，也不晋级生产配置。完整原始结果位于
`tmp/spec2026-uarch-exploration-v1-native-v28_2/diagnostics/tealeaf-paired-frontier-pilot-v2/`。

下一步先对唯一超限边 Q1024/Q2048 做 C8 core 6 / sequence 237567 附近的细粒度
审计，在 paired 摘要中补齐 shared response 的请求身份、relative latency 以及触发
SQ/commit 开放波的前驱，确认 2.140% 残差究竟来自共享事件分批，还是 live-frontier
范围仍不完整。只能扩展被证实缺失的状态，不能用 Q 特判或恢复 checkpoint 标量 tail。
Q 门禁通过后，再把上述绝对缺口作为独立于 Q 的 response-critical 模型缺项定位，
优先审计已退休 ROB/SQ 元素的真正 live frontier、跨核 coherence/LLC fill 等待到
dependent issue 的暴露，以及 gem5 中未进入当前 FST 的后端阻塞事件。

### 10.8 固定 Q=1024 的 P4 物理 page-walk pilot（2026-09-04）

本轮没有继续 Q sweep。所有 control/candidate 均固定 `Q=1024`，并使用同一份新采集
100K user-record/core TeaLeaf trace 和同一个 gem5 运行的 inclusive-cycle oracle。
该运行的线程调度/窗口与历史 10M C4 gate 不同，因此只能作为 matched pilot，不能
把它与历史 baseline 数值拼接成生产晋级结论。

代码审计和实现覆盖：

1. `.vmap` 升级到 v2，同时保留 v1 读取兼容；每个 token 可携带初始和精确 ROI-entry
   两条 root-to-leaf PTE 物理地址路径；
2. producer 扫描 x86-64 两个 canonical half，1 GiB/2 MiB 映射先压缩为 range，仅对
   FST 实际观察页展开；不按 U/S 位过滤 walker 会读取的 present 上层项；
3. FastSim 按当前 phase 选择路径，把实际 2/3/4 级 PTE read 送入普通
   L1D/L2/Ruby/LLC/DRAM；`page_walk_levels=4` 只是最大深度；
4. 两个因果快照都没有路径时保留 fixed-latency fallback 并显式计数，禁止用 final
   页表或 synthetic address 回填；
5. lower-bound DTLB entry 保存唯一 fill generation。response weave 只允许 hit 使用
   生成该 entry 的实际 PTE fill；按 virtual page 匹配并在第一次等待后删除状态的旧版
   修复会漏过后续 UOP，已由“双 follower”回归覆盖；
6. `dtlb.hierarchy_walk` 与 address-free speculative DTLB state 组合现在 fail closed，
   避免为错误路径虚构 PTE 身份。

新 trace 的测量段共有 36,586 条带 token 的内存记录；36,585 条有 phase-causal PTE
路径（99.9973%），全部 33,298 条 user 记录均有路径。唯一缺失是 core 2、token 7、
VA `0x1800000000` 的瞬态 kernel 映射，它在初始和 ROI-entry 两个快照都不存在；没有
从后续状态回填。该页在测量时已驻留，所以本次 413 个 timing walk 中 fallback 为 0。

matched 结果如下。`control-10` 只是把 hierarchy candidate 的结构下界（四次 L1 hit
加 restart）隔离出来，不是延迟寻优；正式对照仍是原 12-cycle fixed walk。

| 固定 Q=1024 配置 | per-core cycles | sum cycles | sum signed error | per-core MAPE |
|---|---|---:|---:|---:|
| gem5 oracle | 16,666 / 16,667 / 74,113 / 16,667 | 124,113 | 0 | 0 |
| fixed walk 12（control） | 16,667 / 16,668 / 61,116 / 16,668 | 111,119 | -10.469% | 4.389% |
| fixed walk 10（结构消融） | 16,667 / 16,668 / 61,541 / 16,668 | 111,544 | -10.127% | 4.245% |
| physical hierarchy + generation causality | 16,667 / 16,668 / 68,920 / 16,668 | 118,923 | -4.182% | 1.756% |

主要误差核 core 2 从 `-17.537%` 收窄到 `-7.007%`。这项改善来自真实事件模型：413
次 timing walk 生成 1,608 个 PTE 请求，其中 44 次为大页短路径；1,530 个请求命中
L1、16 个命中 L2、62 个进入共享层次。没有逐负载 CPI、memory-exposure 或 latency
拟合。

generation 审计还发现 65 次 lower-bound apparent hit 会在实际 PTE fill 前访问未来
状态，累计阻止 18,735 个 cycle、单次最大 1,469。旧的 page-key/erase 实现只报告
10 次，证明它会漏检而不能采用。当前实现已阻止未来状态，但仍低估 gem5：源码
`X86ISA::Walker::start()` 明确把 follower 放入 `currStates`，前一 walk 结束后调用
`startWalk()` 完整重走，并有 TODO 说明尚未 coalesce。当前 FastSim 只是等待原 fill，
没有为这 65 次 follower 生成新的 PTE 请求和 walker 占用。因此 production alias
保持不变，下一项实现应是按 source fill 先后推进的 response-side walker 事务，而不是
再改 fixed latency 或 Q。

可复现输入/输出：

- pilot 配置：`configs/gem5-fs-physical-page-walk-pilot.cfg`；
- trace：`tmp/tealeaf-pte-vmap-v2-100k/promoted-trace/`；
- gem5 oracle：`tmp/tealeaf-pte-vmap-v2-100k/result/sample/mesi-three-level-3GiB/4c/811.tealeaf_s/baf7e843ba539c974eec/20260904T144802Z/oracle/cpi.json`；
- FastSim candidate：`tmp/tealeaf-pte-vmap-v2-100k/fastsim/physical-walk-generation.json`；
- sidecar audits：`tmp/tealeaf-pte-vmap-v2-100k/vmap-audit.json`、
  `tmp/tealeaf-pte-vmap-v2-100k/imap-audit.json`。

### 10.9 follower-walk 固定点否决与稳定复验（2026-09-05）

在固定 `Q=1024` 下尝试过把 lower-bound apparent hit 预展开为条件式完整 PTE walk，
再由 response timing 激活并迭代到事件集合与 arrival 同时稳定。定向单元测试可以通过，
但 TeaLeaf 100K 暴露了非收敛：当前 hierarchy 先按候选请求重放功能状态、随后才计算
source fill；这样一个本应在 source fill 之后才启动的 follower，可能反向改变供应它的
早先 fill，导致激活集合往返振荡。这不是可接受的近似，也不能靠增加 pass 上限掩盖。
该实现已完整撤回，没有进入 pilot 或 production 配置。

撤回后重新编译、运行全部 `fastsim_tests`，并使用相同 trace、scope 和配置重跑
`physical-walk-generation-rerun.json`。结果逐核仍为
`16,667 / 16,668 / 68,920 / 16,668`，sum `118,923`；413 次 walk、1,608 个物理
PTE 请求、44 次短路径、0 fallback，以及 65 次 / 18,735 cycle / 最大 1,469 cycle
的 generation-causal wait 均与上一轮完全一致。这确认已保留的改善不依赖失败的
follower 实验。

下一实现边界是事件驱动的 walker 事务：先提交产生 source generation 的最终 PTE
响应，再按实际 lookup 时间把已排队请求启动为独立 walk，并把这些请求与更晚的普通
内存事件按时间合并。禁止在 source fill 尚未确定时让 follower 修改 cache/Ruby/DRAM
状态；每一步还必须能随事务回滚，避免跨固定 Q 边界泄漏未来状态。
