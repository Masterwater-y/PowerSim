# Private preview 的 epoch-local 自适应选择设计

日期：2026-09-17。状态：**设计与收益边界分析，尚未实施、未运行当前版本实验。**

本文回答一个限定问题：在保持 FastSim 目标状态、CPI 和 PMU 与现有
`interval_private_preview` 路径等价的前提下，能否按 epoch 决定使用并行
private preview，避免当前“有些负载受益、有些负载回退”的全局开关问题。

最高层语义和评估仍遵守
[`project-goal-and-semantic-contract.md`](project-goal-and-semantic-contract.md)
与 [`accuracy-reporting-contract.md`](accuracy-reporting-contract.md)。本文只讨论
宿主执行路径，不改变目标微架构模型、Q、事件顺序或报告口径。

## 1. 当前机制和问题

当前 `run_interval_weave()` 已有两道静态门：

1. 只有 `batch.size() >= sim.domain_min_events` 才请求 preview，维护值为 2048；
2. 完整 `plan_private_preview()` 之后，若 `safe_events < domain_min_events`，
   则因低收益绕过 preview。

第一道门只看事件数量，不能识别核间负载不均、write/atomic 比例、共享逃逸比例
和 worker barrier 是否可摊销。第二道门虽然看到精确 `safe_events`，但此时已经
支付了完整证书规划成本，包括 batch 扫描、访问者集合构建、反向 component
扫描和正向 safe/unsafe 统计。

private preview 也没有删除 safe event：`preview_core()` 仍执行 L1/L2 lookup、
replacement、dirty/eviction、PMU 和 `MemoryReplay` 写入。它主要把 private
cache replay 从 coordinator 搬到永久 domain workers，并只让 escape event 继续
进入共享 replay。后续 batch materialization、共享系统 replay 和逐 UOP response
feedback 仍然存在。

因此真正的收益条件是：

```text
省下的串行 private replay 时间
  >
廉价判定 + 完整证书 + worker barrier + 并行 preview
+ materialization/状态传递的额外成本
```

## 2. 已有收益边界

历史 v28.7 固定绑核实验比较了 C16/C32 各十个负载，20/20 对目标状态递归
一致。相对始终开启 preview：

| 策略 | 等负载几何平均吞吐变化 |
|---|---:|
| 所有 case 都关闭 preview | +0.7438% |
| 每个 case 离线选择 on/off 中更快者 | +1.4698% |
| C16 每 case oracle | +1.7043% |
| C32 每 case oracle | +1.2359% |

这里的 “oracle” 使用完整运行后的结果选择模式，只是 workload-level 粗粒度
机会估计，不能作为在线算法。它也不是 epoch-local oracle 的上界：同一负载内部
可能同时存在适合 on 和 off 的阶段。没有逐 epoch 成对反事实数据，当前不能计算
真正的 epoch oracle。

单点方向说明 crossover 确实存在：

- LBM C16/C32 关闭 preview 分别快 4.552% / 4.327%；
- Graph500 C32 关闭后慢 4.463%；
- Neutron C32 关闭后慢 5.193%。

旧 C32 基线中 certificate 累计约占 measurement wall 的 10.82%，但这是可避免成本
的宽松算术上界，不是可实现加速：若跳过 preview，也会失去并行 private replay。
历史结果只能支持“小幅、负载和阶段相关”的收益假设，不支持两位数整体加速。

## 3. 设计原则

1. **两级门控。** 在完整证书之前使用几乎免费的 pre-gate；通过后才生成
   `PrivatePreviewPlan`，再用精确 safe-work 分布做 post-gate。
2. **不使用 workload、PC、gem5 标签或误差符号。** 决策只依赖当前 target
   functional event 和宿主执行形状。
3. **生产决策确定。** 正式模式使用冻结的整数系数和事件计数，不直接用本次运行的
   wall-clock 抖动决定路径。wall time 只用于离线校准和报告。
4. **两条路径目标状态等价。** 自适应只选择宿主算法；任何路径差异都必须视为 bug，
   不能以小 CPI 容差接受。
5. **先证明 oracle，再实现复杂策略。** 若逐 epoch oracle 本身没有足够收益，
   不引入在线学习器或复杂特征。

## 4. 第一级：廉价 pre-gate

在现有 batch merge 时顺手累计 `PreviewEpochFeatures`，避免新增完整扫描：

```text
event_count
active_event_cores
events_per_core_sum
max_events_on_one_core
write_events
atomic_events
page_fault_state_events
page_walk_events
functional_carried_events
```

派生两个主要量：

```text
effective_parallelism = event_count / max_events_on_one_core
parallel_slack_events = event_count - max_events_on_one_core
```

`parallel_slack_events` 比 `event_count` 更接近可并行收益：若 90% 事件集中在一个
核心，即使 batch 很大，worker 并行也无法缩短关键路径。

V1 pre-gate 建议采用可解释的整数规则：

```text
hard reject:
  当前配置不允许 preview
  或 batch < min_batch
  或 active_event_cores < 2
  或存在 atomic/page-fault-state 全局边界

profitability reject:
  parallel_slack_events < min_parallel_slack
  或 max_events_on_one_core / event_count > max_core_share
  或预测的证书加 barrier 成本 >= 预测的串行 private replay 节省
```

不要在第一版增加 unique-line hash、复杂 line-sharing 图或 ML 推理；这些可能复制
`plan_private_preview()` 的成本。`min_batch`、`min_parallel_slack` 和
`max_core_share` 必须通过离线 epoch 数据冻结，而不是先拍常数再选择有利负载。

## 5. 第二级：证书后的精确 post-gate

扩展 `PrivatePreviewPlan`，在现有最终扫描中同时累计：

```text
safe_events_per_core
max_safe_events_on_one_core
safe_active_cores
safe_write_events
unsafe_events
```

无需另加一遍 batch 扫描。定义：

```text
safe_parallel_slack =
    safe_events - max_safe_events_on_one_core
safe_effective_parallelism =
    safe_events / max_safe_events_on_one_core
```

完整证书已经是 sunk cost；post-gate 只判断“是否还值得支付 worker barrier 和
preview/materialization 成本”。建议条件为：

```text
safe_events >= min_safe_events
safe_active_cores >= 2
safe_parallel_slack >= min_safe_parallel_slack
predicted_remaining_saving >= safety_margin
```

现有条件只有 `safe_events >= 2048`，无法区分 2048 个事件平均分布在八个 worker，
还是几乎全部集中在一个核心。

## 6. 成本模型

第一版不需要复杂机器学习。使用固定点线性模型或小型 decision tree：

```text
predicted_preview_ns =
    certificate_fixed
  + certificate_per_event * event_count
  + domain_barrier
  + private_per_critical_event * max_worker_events
  + materialize_per_event * event_count

predicted_canonical_ns =
    canonical_private_per_event * event_count

choose preview iff
    predicted_canonical_ns - predicted_preview_ns >= safety_margin
```

`max_worker_events` 应按当前 `domain_worker_count` 对 per-core event counts 做
deterministic greedy bin packing 或保守上界，而不只使用平均值。系数按
core-count/worker-count 桶冻结在配置或版本化 profile 中，使用整数运算。

不建议生产模式根据最近 wall-clock EWMA 自适应。host 噪声、NUMA 抢占和同时运行的
case 会改变决策，降低复现实验的可解释性。若未来需要部署自调优，可作为独立模式，
且目标状态仍必须与固定策略一致。

## 7. 如何获得 epoch 训练和 oracle 数据

先增加只读 profile 模式，每个 epoch 输出：

- epoch/accepted-prefix 身份哈希；
- pre-gate 特征；
- certificate 后 safe/unsafe 分布；
- certificate、domain preview、materialization 和 canonical replay 的独立计时；
- 路径选择及目标状态摘要。

随后对相同输入进行多轮固定 affinity 的 `always_on` / `always_off` ABBA 运行，
按 epoch 身份对齐。两条路径必须产生相同 accepted ranges 和目标状态摘要；
不一致时停止，不能训练选择器。

由于分开的进程仍有 host 噪声，标签应按特征桶和多轮中位数生成，而不是把单个
epoch 的纳秒差直接作为真值。第一版可网格搜索三至五个阈值，使用
leave-one-workload-out 选择参数；不要训练高容量分类器。

如果需要更精确的 epoch 反事实，可在专用 profiling binary 中从同一 epoch entry
transaction/checkpoint 依次执行两条路径、回滚第一条并提交第二条。该能力只用于
离线测量，不能进入生产热路径。

## 8. 配置和统计建议

保留原开关作为强制参考，并新增：

```text
sim.private_preview_policy = on | off | adaptive
sim.private_preview_adaptive_profile = <versioned profile>
```

新增统计至少包括：

```text
adaptive_candidate_epochs
adaptive_pre_reject_small_batch
adaptive_pre_reject_low_parallelism
adaptive_pre_reject_global_boundary
adaptive_certificate_attempts
adaptive_post_reject_low_safe_work
adaptive_preview_epochs
adaptive_predicted_saved_ns
```

调试模式可输出分桶结果，但正式统计不能依赖 host timer 才能重现路径选择。

## 9. 预期收益

基于现有历史证据，建议采用以下区间，而不是承诺单个数字：

| 范围 | 合理预期 |
|---|---:|
| V1 廉价确定性门控，完整 C16/C32 等负载集合 | **约 0.5%--1.5%** |
| 有可靠 epoch oracle、能识别同一负载内不同阶段 | **约 1%--3%** |
| LBM/Graph500/Neutron 一类 crossover 明显的单 case | **约 3%--6%** |
| 当前证据支持的整体两位数加速 | **不支持** |

0.5%--1.5% 的依据是：历史 always-off 已有 +0.744%，workload-level oracle 为
+1.470%，而实际选择器存在误判和自身开销；epoch phase 信息可能追回部分额外收益。
1%--3% 是需要逐 epoch oracle 证明的研究目标，不是当前测量结果。若 current
common-end/v28.9 的理想 epoch oracle 低于 2%，复杂自适应实现大概率不值得。

## 10. 验收和停止条件

按以下顺序验收：

1. synthetic epoch 覆盖小 batch、均衡/极端倾斜、write-heavy、atomic、
   safe/unsafe 边界和不同 worker 数；
2. `on/off/adaptive` 三种策略的目标状态、逐核统计、PMU 和事件顺序递归一致；
3. current common-end 代表 case 固定 CPU/NUMA、单进程、至少五轮 ABBA；
4. 报告每种拒绝原因、证书尝试率、实际 preview 率和预测误差；
5. 再跑完整冻结矩阵，不以并发 cases/hour 代替单例吞吐。

建议停止条件：

- epoch oracle 相对最佳全局固定策略小于 1%；
- adaptive 相对维护默认的等负载几何平均提升小于 1%，或置信区间跨过明显回退；
- 任一目标状态不一致；
- 选择器需要 workload/PC、gem5 timing/path label 或在线误差符号；
- 为预测收益增加的扫描与哈希接近完整证书成本。

满足停止条件时，应保留现有静态 `domain_min_events`，把优化资源转向调度历史
有界化、热字段布局或 response-feedback 的真正逐 UOP 工作，而不是继续提高
`private_preview_events` 覆盖率。
