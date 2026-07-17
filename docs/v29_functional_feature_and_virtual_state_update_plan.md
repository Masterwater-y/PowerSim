# TCSim v29 Functional-only 特征与虚拟状态更新方案

状态：设计草案
日期：2026-07-17
适用范围：v29 global-time、monotonic-prefix 模型及其 free-running 推理框架

## 0. 结论

当前 v29 的主要特征缺口不是“缺少更多静态计数”，而是缺少两类可部署信息：

1. Redis heldout 所需的长期 working-set、reuse、page/TLB、跨核共享历史；
2. memory-seq 所需的有序 DRAM 请求相位、跨核相对竞争和持续仲裁状态。

所有新增模型输入必须遵守以下硬约束：

- 只能使用 functional trace、固定硬件配置和模型自己的历史预测状态；
- 不得读取 gem5 cache/TLB/DRAM outcome、commit tick、load-to-use、真实队列或真实仲裁结果；
- 不得加入 core ID、固定 channel/bank ID embedding 等 trace 身份捷径；
- 训练和部署必须使用相同的特征构造语义。

在此约束下，真实 DRAM open row、queue occupancy、request age、MSHR occupancy 和 arbitration winner **不能从 functional trace 唯一恢复**。正确做法不是伪造这些真实硬件量，而是维护一套由 functional 请求流和历史预测递推得到的 **virtual/belief state**。

推荐分三层推进：

1. 先增加可离线预计算的有序地址、依赖、相位和长期 locality 特征；
2. 用轻量 probe 验证增量信息和泛化性；
3. 只有验证通过后，才加入紧凑、增量式的 virtual FR-FCFS 状态。

## 1. 背景与问题定义

### 1.1 Redis heldout：长期 locality 的条件分布外问题

Redis heldout 同时改变 shared working set、private state、uniform-tail 比例和依赖/control 行为。真实 cache/TLB/DRAM 延迟显著变慢，但当前 functional 窗口中的 load density、局部 distinct line/page、reuse bucket 等变化较弱，甚至可能呈现“更轻”的局部特征。

结果是模型把 heldout 错误映射回 Redis base 的时间尺度。这个问题要求补充更长时间尺度的 functional locality 状态，而不是仅增大 horizon。

### 1.2 memory-seq：动态形成的离散慢核

memory-seq 的 32 个核执行相同代码和相同 load 数，对 8 个 DRAM channel 的长期访问比例也相同。逐核速度差异不是固定 core 属性，而是由以下组合形成：

- 每轮 4 条连续 load；
- `tid * 37` 带来的地址流起始相位；
- channel/bank/row 请求顺序；
- FR-FCFS 的 row-hit 优先级；
- open-page 和有限 read buffer 产生的长期服务队列。

源码位置：`/data00/yinhaolang/TSim/workloads/v28/v28_business_proxy.c:385-400`。

当前模型能够识别部分大尺度快慢分层，但无法稳定识别 FR-FCFS 形成的少数相位锁定慢核。free-running 中，一次局部快估会让该核 cursor 领先，后续功能窗口错位继续放大误差。

### 1.3 当前特征的结构性缺口

当前输入已经包含：

- per-UOP op、dependency、reuse、stride、memory kind；
- 当前 K 窗口的 channel/bank/row equality 和 fanout；
- same-row support、different-row conflict；
- 窗口级 channel/bank HHI、row reuse、set pressure；
- 少量预测状态，例如 elapsed、ROI age、active core fraction。

但现有 cross-core 关系主要基于当前窗口的无序 presence/count：

- 看得到“多少核访问同一 bank”；
- 看不到谁先到、谁后到；
- 看不到同一冲突是否跨多个窗口持续；
- 看不到上一轮虚拟仲裁后形成的服务债务；
- 看不到当前核在竞争 cohort 中的相对位置。

此外，`tcsim/v29/dataset.py:912-918` 当前给 `head_age` 与 `elapsed_since_last_commit` 填入相同值，两个名义状态没有提供独立信息。

## 2. Functional-only 输入合同

### 2.1 允许输入

允许的原始信息：

- retired functional UOP 顺序；
- op class、register producer/consumer 关系；
- load/store/atomic 类型和大小；
- functional/physical address 及其 equality；
- 由地址和固定硬件映射派生的 cache set、channel、rank、bank、row、column；
- 每核当前 **预测 cursor**；
- 模型此前预测的 delta、消费 prefix 和由此递推的 oracle-free state；
- 固定的 cache/DRAM 容量、bank/channel 数和调度策略配置。

训练标签仍可使用 oracle commit time、progress 和 branch miss，但这些信息只能进入 loss/metric，不得进入模型输入或状态更新器。

### 2.2 禁止输入

禁止进入模型或虚拟状态更新器：

- `commit_tick`、真实 issue/complete tick；
- gem5 load-to-use latency；
- 真实 L1/L2/LLC/TLB hit/miss outcome；
- 真实 DRAM path class；
- 真实 open row、read/write queue occupancy；
- 真实 request age、FR-FCFS winner；
- 真实 outstanding load/MSHR 数；
- core ID、固定 core embedding；
- nominal channel/bank/row ID 的 learned embedding。

### 2.3 静态特征与递归状态必须分层

建议把输入合同拆为两部分：

```text
functional_static_features
oracle_free_recursive_state
```

`functional_static_features` 可以离线预计算并缓存；`oracle_free_recursive_state` 只能在 rollout 中由 functional 请求和历史预测递推。

如果将约束解释为“模型输入必须是静态 trace prefix 的纯函数，连历史预测也不能使用”，那么真实仲裁状态存在不可消除的观测歧义，模型不可能稳定区分功能窗口相似但队列位置不同的核心。

## 3. 总体数据流

```text
Functional trace cache
        │
        ├── 离线：地址/依赖/长期历史特征
        │
Current predicted cursors ──> ordered cross-core context
        │
Previous virtual state ─────> virtual controller update
        │
        └──────────────> compact per-token/per-core state
                                │
                                v
                         v29 timing model
                                │
                         predicted prefixes
                                │
                         global-time scheduler
                                │
                    cursor/state incremental update
```

模型仍然一次处理所有 active cores 和每核 K 个 token。虚拟状态只增加 context 构造和 step 后的增量更新，不改变 GPU 内 per-core/per-token 并行结构。

## 4. P0：可离线预计算的有序 functional 特征

### 4.1 Per-token memory burst 与顺序特征

建议新增：

| 特征 | 语义 |
|---|---|
| `mem_burst_position` | 当前 memory UOP 在连续 burst 中的位置 |
| `mem_burst_length` | 当前 burst 的 memory UOP 数 |
| `prev_same_channel_distance` | 距上一次同 channel 请求的 memory ordinal |
| `prev_same_bank_distance` | 距上一次同 bank 请求的 memory ordinal |
| `prev_same_row_distance` | 距上一次同 row 请求的 memory ordinal |
| `next_same_bank_distance` | 当前 lookahead 内距下一次同 bank 请求的距离 |
| `same_bank_row_switch_run` | 同 bank 连续 row switch 的长度 |
| `same_row_run_length` | 同 row 连续访问长度 |
| `row_switch_rate_short/long` | 多尺度 row switch 比例 |
| `channel_transition_role` | channel 序列的 stay/switch/revisit 角色 |
| `bank_transition_role` | bank 序列的 stay/switch/revisit 角色 |

这些特征必须使用距离、equality 和角色编码，避免暴露 nominal channel/bank/row ID。

### 4.2 Functional MLP 与依赖前沿

真实 outstanding/MSHR 不可见，但可以构造 functional MLP proxy：

| 特征 | 语义 |
|---|---|
| `load_to_first_consumer_distance` | load 到首个 consumer 的 UOP 距离 |
| `dependent_memory_chain_depth` | 当前 memory dependency chain 深度 |
| `independent_loads_before_consumer` | consumer 前可独立存在的 load 数 |
| `ready_memory_frontier_size` | 按 producer relation 可并行暴露的 memory frontier |
| `serial_load_fraction` | 依赖链上的 load 占比 |
| `max_independent_load_burst` | 最大独立 load burst |
| `blocked_uops_per_head_load` | 头部 load 阻塞的后继 UOP 规模 |
| `distinct_banks_before_join` | dependency join 前覆盖的 bank 数 |

这些量不能声称等价于真实 issue queue、LQ 或 MSHR occupancy；它们只是 functional readiness/MLP proxy。

### 4.3 多尺度长期 locality

为 Redis 和 page/TLB 压力新增：

| 特征 | 推荐尺度 |
|---|---|
| `distinct_lines` | 1K/4K/16K/64K/256K memory refs |
| `distinct_pages` | 1K/4K/16K/64K memory refs |
| `reuse_distance_quantiles` | p50/p90/p99 |
| `far_reuse_fraction` | 超过多个离散阈值的比例 |
| `new_line_fraction` | short/medium/long |
| `new_page_fraction` | short/medium/long |
| `uniform_tail_score` | 低频新地址/低重用尾部比例 |
| `cross_core_last_touch_distance` | 其他核上次访问同 line/page 的 functional 距离 |
| `cross_core_shared_line_age` | 跨核共享 line 的多尺度年龄 |
| `page_set_pressure` | functional page 到 DTLB set 的工作集压力 |

优先使用近似 cardinality sketch、分桶 quantile 和 EWMA，避免保存无限历史。

## 5. P0：当前窗口的有序跨核竞争特征

### 5.1 Per-token ordered competitors

对于每个 memory token，基于当前各核 predicted cursor、窗口中 memory ordinal 和 dependency-ready rank，计算：

- `same_channel_before_count` / `same_channel_after_count`；
- `same_bank_before_count` / `same_bank_after_count`；
- `same_row_before_count`；
- `different_row_same_bank_before_count`；
- `nearest_same_bank_rival_offset`；
- `nearest_diff_row_rival_offset`；
- `same_row_support_ahead`；
- `different_row_blockers_ahead`；
- `functional_arrival_rank_in_channel`；
- `functional_arrival_rank_in_bank`。

`before/after` 表示 functional-ready 的相对顺序，不是真实硬件到达时间。

### 5.2 跨核 phase-lock 特征

比较两个核心未来 16/32/64 个 memory request 的 `(channel, bank, row-equality-role)` 序列，构造：

- `channel_sequence_phase_offset`；
- `bank_sequence_phase_offset`；
- `row_sequence_phase_offset`；
- `same_bank_phase_correlation`；
- `different_row_phase_correlation`；
- `competition_cohort_size`；
- `phase_lock_persistence_short/medium/long`。

实现应使用 equality、relative shift、correlation 和 cluster size，不输出对方 core ID。

### 5.3 计算复杂度控制

直接对 32 核执行 `O(C^2 * K)` Python 循环不可接受。建议：

- 离线预计算每核 memory sequence signature；
- 当前 step 只按 cursor 索引取定长 signature；
- 使用 NumPy/GPU 向量化比较 32×32 pair；
- 对 phase offset 使用小范围 shift 或哈希匹配；
- 模型只接收每核/每 token 的聚合结果，不接收完整 pair matrix。

## 6. P1：Oracle-free virtual FR-FCFS 状态

### 6.1 状态定位

virtual controller 的目标不是复现 gem5 的 cycle-accurate DRAM，而是为模型提供以下可辨识信息：

- 当前请求是否与 virtual open row 匹配；
- 当前核在同 channel/bank 请求中的相对排位；
- 是否持续被 row-hit 请求 bypass；
- 当前核是否积累了长期服务债务；
- 哪些竞争关系跨多个预测 step 持续存在。

### 6.2 内部状态

每个 channel/bank 内部维护：

- `open_row_key`；
- 固定上限的 virtual request entries；
- entry 的 core slot、UOP index、row key、functional arrival rank；
- virtual age/bypass count；
- last selected row；
- row-hit streak；
- steps since row switch；
- distinct queued rows；
- same-row/different-row queue counts。

内部可以使用精确 channel/bank/row key 做 bookkeeping，但这些 nominal key 不直接进入模型。

### 6.3 模型可见的 per-request 特征

- `virtual_open_row_match`；
- `virtual_queue_occupancy_bucket`；
- `virtual_same_row_queued`；
- `virtual_diff_row_queued`；
- `virtual_distinct_rows_queued`；
- `virtual_arrival_rank`；
- `virtual_frfcfs_rank`；
- `virtual_older_row_hits_ahead`；
- `virtual_older_diff_row_requests`；
- `virtual_request_age_bucket`；
- `virtual_bypass_count_bucket`。

### 6.4 模型可见的 per-core 状态

- `virtual_outstanding_loads`；
- `virtual_outstanding_banks`；
- `virtual_head_request_age`；
- `virtual_queue_rank_p50/p90`；
- `virtual_recent_service_count`；
- `virtual_service_debt`；
- `virtual_steps_since_service`；
- `virtual_win_rate_short/medium/long`；
- `virtual_service_debt_rank`；
- `virtual_competition_cohort_rank`。

推荐定义：

```text
service_debt(core) =
    virtual arrivals attributed to core
  - virtual services attributed to core
```

模型主要接收归一化 debt、active-core rank 和 EWMA，而不是无界累计值。

### 6.5 状态更新原则

最小实现可以采用离散 virtual arbitration event，而不是声称恢复真实 cycle：

1. 根据 predicted cursor 和 dependency frontier 找到首次进入 virtual-ready 区间的 memory UOP；
2. 每个 UOP 只 enqueue 一次；
3. 根据 virtual open row、row-hit role 和 functional age 计算 FR-FCFS priority；
4. 根据上一预测 step 的 delta/消费 prefix 推进有限个 virtual service event；
5. 更新 open-row belief、bypass、service debt 和 recent-win history；
6. 对已经越过 cursor 或完成 virtual service 的 entry 做一致性清理；
7. 不得用 oracle cursor/tick 修正状态。

virtual service budget 的精确定义需要通过训练前 probe 和吞吐 benchmark 选择，不能针对某个固定慢核调参。

### 6.6 初始化与恢复

- 有 pre-ROI functional warmup 时，从 warmup 请求流初始化；
- 没有 warmup 时从 empty/cold 状态开始，并显式提供 cold-state feature；
- trace resume 必须持久化 virtual controller state；
- 多 worker/shard 之间不共享状态；
- checkpoint 必须记录 state schema 和 update-policy hash。

## 7. P1：跨核相对进度与状态修正

建议增加：

- `cursor_uop_offset_from_active_median`；
- `memory_ordinal_offset_from_median`；
- `predicted_progress_rank`；
- `service_debt_rank`；
- `phase_cohort_progress_offset`；
- `previous_step_consumed_uops`；
- `head_residency_steps`；
- `no_progress_streak`。

其中：

- 所有 offset/rank 只相对于当前 active cores；
- 不包含 core ID；
- 使用 predicted cursor，不使用 oracle cursor；
- 应通过 core-slot permutation test。

同时修正当前重复状态：

- `head_age` 改为当前 functional head 连续停留在窗口首部的预测 step/time；
- `elapsed_since_last_commit` 保持为预测全局时钟距离本核上次消费 prefix 的时间。

## 8. 模型接口建议

### 8.1 不把完整队列送入模型

禁止将 8 channel × 64 request 的完整 queue tensor 直接加入 attention。推荐接口：

- 10–20 个新增 per-token categorical/continuous relation；
- 20–40 个新增 per-core compact state；
- 少量全局 controller summaries；
- virtual queue 仅在 CPU/state builder 内部存在。

### 8.2 保持 permutation equivariance

必须满足：

- core slot 置换后，per-core 输出等价置换；
- nominal channel/bank label 置换后，只要 equality/拓扑不变，预测不变；
- address relocation 不改变 equality/reuse/pressure 语义；
- internal queue 可以记录 core slot，但模型只接收相对 rank/debt/cohort 统计。

### 8.3 推荐 schema 分组

```text
STATIC_TOKEN_FIELDS
ORDERED_MEMORY_FIELDS
DYNAMIC_XCORE_FIELDS
LONG_HISTORY_SUMMARY
VIRTUAL_CONTROLLER_STATE
PREDICTED_RELATIVE_PROGRESS_STATE
```

每组独立版本化，便于消融、缓存兼容检查和 checkpoint 硬失败。

## 9. 并行度与吞吐设计

### 9.1 当前基线

当前 c32 free-running 大致耗时：

| workload | elapsed | context build | predict | scheduler |
|---|---:|---:|---:|---:|
| memory-seq | 305.4 s | 138.6 s | 164.4 s | 2.38 s |
| Redis heldout | 351.8 s | 160.3 s | 188.7 s | 2.72 s |

context 构造已经占约 45%，因此 virtual state 的主要风险是 CPU context builder，而不是 GPU attention。

### 9.2 并行度影响

不会被破坏：

- 同一步内所有 active cores 的并行 forward；
- 每核 K token attention；
- 多 GPU trace sharding；
- 不同 trace/worker 的独立执行。

新增串行部分：

- 每条 trace 每个 rollout step 的 virtual state update；
- 依赖上一预测 step 的 queue/debt/history 更新。

free-running 本来就按 step 串行，因此这是增量 CPU 延迟，不是新的 GPU 串行结构。

### 9.3 性能预算

以 memory-seq c32 的约 57 ms/step 为基线：

- `<1 ms/step`：目标总损失约小于 2%；
- `<2 ms/step`：目标总损失约小于 4%；
- `5 ms/step`：可能损失约 9%；
- `10 ms/step`：可能损失约 17%。

准入目标：

```text
state_update_p95 < 2 ms/step
```

实现要求：

- 固定大小 NumPy/C++ array，不使用大量 Python request object；
- 只处理新进入/离开的 request，不重扫完整历史；
- 每个 channel 固定最大 64 entries；
- phase 特征尽量离线缓存；
- 单独记录 `state_update_seconds`、p50/p95/p99；
- context/state update 与下一步 GPU 工作可在后续版本考虑流水化。

### 9.4 训练并行度

静态和有序特征离线缓存后，现有 DataLoader/DDP 并行不变。

如果 virtual state 在线依赖模型上一轮输出，则不能把任意窗口当成完全独立样本。直接在线展开会降低训练吞吐。推荐 rollout-cache/DAgger 流程：

1. 用当前 checkpoint 在训练 trace 上 free-run；
2. 记录 functional context、virtual state、当前预测和 oracle loss target；
3. 固化为 mmap rollout dataset；
4. 主训练仍使用随机 sampler、DDP 和普通 batch；
5. 新模型完成后仅刷新 1–2 轮 rollout cache。

oracle 只用于 target，不参与状态构造。

## 10. 完整训练前的特征有效性验证

### 10.1 验证原则

完整零训练只能验证相关性、可辨识性和机制一致性。要验证“在现有模型之上是否提供增量预测价值”，至少需要一个很小的 linear/GBDT/residual probe，但不需要重训主模型。

所有 probe 必须满足：

- 不使用 core ID；
- 特征构造不使用 oracle；
- oracle 仅作为 target/metric；
- 按 trace/core/phase/seed 分组切分，不能随机拆窗口；
- 报告逐核 signed error，不能只看 aggregate CPI。

### 10.2 阶段 A：零训练特征审计

对候选特征计算：

- 与 future `tau@256`、progress@256 的 Pearson/Spearman；
- 与逐核 load-to-use、CPI 的相关性，仅作为离线 metric；
- slow-core AUROC、PR-AUC、Recall@K；
- 逐核 CPI rank 的 Kendall/Spearman；
- 时间 block 间稳定性；
- seed0/seed1 间稳定性；
- grouped bootstrap confidence interval。

重点候选：

- `phase_lock_persistence`；
- `different_row_same_bank_before_count`；
- `functional_arrival_rank`；
- `virtual_frfcfs_rank`；
- `virtual_service_debt`；
- `virtual_bypass_count`；
- `functional_mlp_frontier`。

### 10.3 阶段 B：增量信息 residual probe

冻结当前 checkpoint，以当前模型的单步误差作为 target：

```text
target_residual =
    log1p(true_tau@256) - log1p(current_pred_tau@256)
```

对比：

```text
baseline probe:
    current prediction + current relation/state

candidate probe:
    baseline + new ordered/phase/virtual-state features
```

允许模型：

- Ridge/Lasso；
- shallow GBDT；
- 2 层、32–64 hidden 的 residual MLP。

必须报告：

- per-core residual MAE；
- signed bias；
- pairwise slow/fast ordering accuracy；
- slow-core Recall@K；
- core 9/13 等漏识别核心的误差；
- 普通核心的 false-slow rate；
- leave-core/phase/seed-out 泛化。

### 10.4 阶段 C：机制反事实

为了排除 ID shortcut 和偶然相关，必须执行：

1. **顺序打乱**：保持 channel/bank 直方图不变，只打乱请求顺序；当前 aggregate 特征基本不变，新 phase/queue 信号应改变。
2. **FIFO 对照**：关闭 virtual row-hit priority；如果 FR-FCFS 特征有效，慢核排序能力应下降。
3. **相位平移**：循环平移某个核的 functional 地址流；预测慢核 cohort 应随相位变化，而不是固定在 core ID。
4. **core permutation**：任意置换 core slot，特征和预测必须等价置换。
5. **channel/bank label permutation**：只置换 nominal label、保持 equality 和拓扑，预测不得改变。
6. **history reset**：清空 persistent state 后，phase-lock/slow-core 识别应按预期减弱。

### 10.5 阶段 D：小 residual head 闭环

probe 通过后，先只训练小型 correction head：

```text
corrected_commit_time =
    current_commit_time * exp(residual_head(new_state))
```

依次验证：

1. oracle one-step 是否改善；
2. free-running 是否仍改善；
3. 快估核心的 cursor lead 是否缩小；
4. 是否出现新的状态正反馈；
5. `state_update_p95` 是否满足性能预算。

如果 one-step 改善而 free-running 恶化，说明特征包含信息，但递归状态转移不稳定，应该修正 state update，而不是扩大主模型。

## 11. 数据划分与防泄漏

### 11.1 memory-seq

禁止随机窗口切分。推荐组合：

- seed0 构造、seed1 验证；
- leave-core-out；
- leave-phase-cluster-out；
- 时间 block 留出；
- synthetic phase shift 作为反事实测试。

由于 seed0/seed1 的慢核分组可能一致，仅做 leave-seed-out 仍可能保留相同 phase pattern，必须叠加 leave-core/phase-out。

### 11.2 Redis

新增长期 locality 特征应在通用 mechanism cube 上验证：

- working set 多档；
- Zipf/uniform-tail 比例；
- dependent lookup depth/MLP；
- private state 大小；
- 多 seed/address mapping。

当前 Redis heldout 已经用于开发诊断，不能继续充当最终 untouched test。最终验收需要新 seed 和新 binary variant。

## 12. 配套 loss 与评估更新

特征更新不能继续只依赖 workload aggregate CPI。建议增加：

- per-core `tau@stride` signed loss；
- pairwise core progress/rank loss；
- phase cohort 内的相对 progress loss；
- service-debt 与预测 progress 的一致性辅助项；
- per-core signed cycle error 报告；
- slow-core set precision/recall；
- cursor lead/lag 的 per-core 时间序列。

pairwise/rank loss 的标签可以使用 oracle，因为它仅在训练 loss 中使用；部署输入仍保持 functional-only。

## 13. 分阶段实施计划

### Phase 0：基线固化和审计工具

- 固化 memory-seq c32 逐核 CPI、load-to-use、cursor drift 基线；
- 固化 Redis base/heldout oracle one-step 基线；
- 增加 feature dump、state update timing 和 permutation audit；
- 不修改主模型。

交付：可重复的 feature-probe dataset 和报告。

### Phase 1：离线有序特征

- memory burst/order；
- dependency frontier/MLP proxy；
- ordered same-bank/different-row rivals；
- phase-lock signature；
- 多尺度 locality/page history。

交付：新版 functional cache schema、零训练审计和 residual probe。

准入：候选特征必须在 grouped split 上提供稳定增量信息。

### Phase 2：最小 virtual-state sketch

只实现：

- open-row belief；
- queue occupancy bucket；
- virtual rank；
- bypass age；
- per-core service debt；
- recent win/service history。

暂不实现完整 cycle-accurate DRAM 模拟。

交付：state builder、state schema、resume contract、吞吐 benchmark。

### Phase 3：小 residual head 和闭环验证

- 冻结主模型；
- 训练小 correction head；
- 先 oracle one-step，后 free-running；
- 比较 cursor drift 和逐核误差；
- 验证状态稳定性和吞吐预算。

### Phase 4：主模型集成

只有 Phase 1–3 通过后才：

- 把新状态接入主 timing head；
- 加入 pairwise/rank loss；
- 生成 rollout cache；
- 执行有限轮 DAgger refresh；
- 在新 final untouched 集上一次性验收。

## 14. 准入与验收门槛

以下为建议门槛，实施前可根据 probe 方差校准：

### 特征有效性

- leave-core/phase/seed-out residual MAE 改善至少 15%–20%；
- memory-seq slow-core Recall@6 至少达到 5/6；
- core rank correlation 明显改善；
- 漏识别慢核改善，同时 false-slow 不成批增加；
- Redis mechanism leave-one-out 上保持改善；
- 反事实和 permutation 测试全部通过。

### 闭环效果

- oracle one-step 和 free-running 同方向改善；
- free/oracle 改善比不过度退化；
- 快估核心 cursor lead 显著缩小；
- per-core signed error 不再靠正负抵消获得正确 aggregate CPI；
- 不新增 no-progress、overshoot 或不稳定反馈。

### 性能

- `state_update_p95 < 2 ms/step`；
- 总 free-running 吞吐下降目标小于 5%；
- virtual state 每 trace 保持固定上界；
- 不向 GPU 传输完整 request queue；
- 多 GPU sharding 和现有 per-step core/token 并行保持不变。

## 15. 风险与非目标

### 风险

- virtual controller 参数针对某个 workload/core group 过拟合；
- predicted state 误差形成新的闭环正反馈；
- Python 实现导致 context builder 成为主要瓶颈；
- 在线递归训练破坏随机采样和 GPU 利用率；
- nominal ID、oracle tick 或 cache outcome 意外泄漏；
- 相位特征在 seed0/seed1 相似模式上产生伪泛化。

### 非目标

- 不追求用 functional trace 精确复现 gem5 DRAM controller；
- 不把真实 queue/MSHR/cache state 加入模型；
- 不通过 core ID 记住固定慢核；
- 不用 current development heldout 代替最终 untouched test；
- 不在特征尚未通过 probe 前重训完整主模型。

## 16. 最小推荐版本

如果只做一个低风险版本，推荐以下最小集合：

1. `mem_burst_position/length`；
2. ordered same-bank/different-row before/after counts；
3. `phase_lock_persistence` 和 competition cohort size；
4. dependency-ready memory frontier；
5. 多尺度 line/page cardinality 与 reuse tail；
6. open-row belief；
7. virtual FR-FCFS rank；
8. bypass age；
9. per-core service debt 及其 active-core rank；
10. 独立的 head residency 与 elapsed-since-predicted-commit。

该版本足以验证“长期 locality + 有序竞争 + 持久服务债务”是否同时改善 Redis OOD 和 memory-seq 离散慢核，而不需要立即引入完整 DRAM 仿真器。
