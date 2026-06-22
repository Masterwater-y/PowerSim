
## 补充：五个 workload 的特征与指标影响

这 5 个 workload 不是随机 benchmark，而是为了把 CPU 仿真误差拆成更可解释的几类瓶颈：分支预测、cache hierarchy、backend/dependency、memory criticality 与混合业务状态更新。它们对 `branch.misses`、`cache.llc.load_misses`、`core.cycles`、`core.instructions` 和 CPI 的影响模式不同，因此适合在 agent 协作中承担不同的诊断角色。

| workload | 主要特征 | 主要放大的指标 | 典型解释 |
| --- | --- | --- | --- |
| `log_state` | 类业务状态更新，包含 hash、数组访问、session/bucket 更新 | `core.cycles`、`branch.misses`、LLC/TLB 观测偏差 | 混合回归点；CPI 接近不代表所有 PMU 都解释正确 |
| `graph_walk` | 图遍历 / pointer chasing，branch、memory、dependency 叠加 | `branch.misses`、`core.cycles`、CPI、memory criticality | 组合型难点；适合验证 overlap、critical path、OoO window 语义 |
| `codec_pipeline` | compute-bound pipeline，backend/dependency 主导 | `core.instructions`、`core.cycles`、CPI、dependency stall | backend 路径较干净；适合排查 base cycles、dependency、port pressure |
| `branch_dense` | 高密度分支，热点 PC 方向模式复杂 | `branch.misses`、branch recovery cycles、CPI | 分支预测隔离 workload；用于判断 predictor family 是否已到上限 |
| `cache_bench` | cache hierarchy 与写回压力放大 | `cache.llc.load_misses`、L1/L2/L3 统计、SQ drain、CPI | cache / writeback / store queue 归因主线 |

### `log_state`：混合业务状态更新

`log_state` 模拟日志或状态流处理：每轮从 event 数组取事件，更新 session 状态，并通过 hash 访问 bucket。它的访问模式既有连续数组，也有 hash 后的间接访问，因此不是纯 compute，也不是纯 memory benchmark。

对指标的影响：

- `branch.misses` 通常不应极端放大，适合做 branch 回归 sanity check。
- `core.cycles` 和 CPI 对整体 timing 是否稳定较敏感。
- `cache.llc.load_misses` 与 `tlb.dtlb_load_misses` 可能出现解释不对齐，因为 hash/bucket 访问会把局部性、地址映射和 PMU 事件语义混在一起。
- 它更适合做“修完其他路径后是否引入副作用”的混合回归点，而不是第一主修对象。

在最新结果中，`log_state` 的 MineSim CPI 误差约 `-3.627%`，说明整体 timing 接近 baseline；但 LLC/TLB 类指标仍严重失真，说明不能只用 CPI 判断模型已经正确。

### `graph_walk`：branch + memory + dependency 组合难点

`graph_walk` 的核心特征是 pointer chasing 和数据相关控制流。下一步访问地址依赖当前加载结果，分支方向也可能受数据状态影响，因此它同时考验：

- load miss criticality；
- branch direction prediction；
- branch recovery 与 memory stall 的 overlap；
- dependency chain 在真实 OoO window 中的隐藏程度。

对指标的影响：

- `branch.misses` 容易偏高或偏低，因为分支方向模式受图结构和数据路径影响。
- `core.cycles` 与 CPI 对 memory criticality 和 branch recovery overlap 非常敏感。
- `core.instructions` 如果 MineSim 与 Sniper 同时低于 perf，通常要怀疑 trace/ROI 共性问题，而不是直接归因给 MineSim。
- `cache.llc.load_misses` 偏差可能来自 trace 口径、cache 模型或 CounterPoint mapping，而不能单独解释为 cache latency 错。

因此，`graph_walk` 不适合作为第一轮盲修对象。它更适合作为“isolating workload 已证明某个语义修正有效之后”的组合回归点。

### `codec_pipeline`：backend / dependency 主导的 compute-bound workload

`codec_pipeline` 更接近计算流水线，memory 贡献较小，主要用于观察 backend、dependency、issue、forwarding 和 base timing 是否合理。

对指标的影响：

- `core.instructions` 通常应比较稳定，因此适合检查 trace/ROI 是否一致。
- `core.cycles` 和 CPI 偏差更可能来自 dependency stall、port pressure、base cycles 或 forwarding 语义。
- `branch.misses` 可能存在一定误差，但通常不是主导项。
- `cache.llc.load_misses` 不应成为主解释路径；如果 CP 把 memory 放到前排，需要检查是否是 mapping 或 solver artifact。

最新归因中，`codec_pipeline` 已被判断为 compute-bound，主要 residual 更接近 CounterPoint solver artifact 或轻量 backend 偏差，不支持继续大修 MineSim backend 语义。

### `branch_dense`：分支预测隔离 workload

`branch_dense` 用来放大 branch predictor 与 branch recovery 语义。它的价值在于相对减少 memory/cache 干扰，让 agent 能更直接地观察 `branch.misses` 与 branch cost 对 CPI 的贡献。

对指标的影响：

- `branch.misses` 是最关键指标，能直接暴露 predictor family、chooser、history、局部模式等问题。
- `core.cycles` 和 CPI 会被 branch recovery cost 明显影响。
- `cache.llc.load_misses` 因 baseline 绝对值可能很小，相对误差容易被放大，不应作为第一判断依据。
- 如果不同 predictor family 在热点 PC 上都只有约 `50%`–`57%` 准确率，说明继续改 predictor 结构的收益可能有限。

在 agent workflow 中，`branch_dense` 曾带来明显收益：CPI 误差从 `+32.46%` 降到 `+3.88%`，后续到 `+0.77%`。这说明 branch/memory overlap 和 resolve_cycle 的 targeted 修正确实有效。但后续归因也显示，当前 predictor family 对部分热点 PC 已接近上限，因此不再是首选 patch 方向。

### `cache_bench`：cache hierarchy 与 store pressure 放大器

`cache_bench` 用于放大 L1/L2/L3/DRAM 层级、writeback 路径和 store queue drain。它是当前 cache path 归因的主 workload。

对指标的影响：

- `cache.llc.load_misses` 是核心观测点，但需要同时看 L2 miss、L2 writeback、L3 access、DRAM access 是否守恒。
- `core.cycles` 和 `core.instructions` 可能同向偏低，导致 CPI 表面接近，但这不代表模型正确。
- `SQ Drain` 可能单独贡献 CPI，是判断 store queue / writeback pressure 是否过于悲观的重要信号。
- CounterPoint 如果缺少 `L2 writeback -> L3 access` anchor，会把真实 cache path 缺口误报成 L2 miss violation。

最新结论是：`cache_bench` 的 cache path 解释已显著收敛，`cache.l2.writebacks` 精确 anchor 是当前最重要的 CounterPoint 补模方向；MineSim 侧更值得继续 observation-only 的问题是 `SQ Drain = 0.198 CPI` 是否过于悲观。

### 使用这些 workload 的原则

这 5 个 workload 的正确用法不是一起看谁误差最大，而是按诊断问题选择入口：

| 诊断问题 | 优先 workload | 回归 workload |
| --- | --- | --- |
| branch predictor / branch recovery | `branch_dense` | `graph_walk`, `log_state` |
| memory criticality / overlap | `graph_walk` | `branch_dense`, `cache_bench` |
| backend / dependency / base cycles | `codec_pipeline` | `log_state` |
| cache hierarchy / writeback | `cache_bench` | `graph_walk`, `log_state` |
| 是否引入混合业务副作用 | `log_state` | full 5-workload suite |

因此，多 agent 流程要求：先用最能隔离问题的 workload 做 targeted validation，再用 `graph_walk` 或 `log_state` 检查组合影响，最后才跑 full suite。
