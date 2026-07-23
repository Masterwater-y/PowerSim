# v27.2 MVP 方案：Oracle-First 固定 Functional Chunk + Epsilon Resident 调度

> **2026-07-10 v27.2 澄清**：首版改为 oracle-first。真实 timing 用于 fixed-chunk label 和
> oracle 跨核 context 选择，但不进入模型输入；单阶段模型只读 functional-safe 特征，
> `T_pred/E_pred/delta_hat` 由外部 scheduler 在模型输出后维护。首模型不依赖 model-generated rollout，
> predicted rollout 仅在首模型完成后用于闭环评测与按需 hard-context 增广。逐核 loss 与完整数据合同见
> `/data00/yinhaolang/TCSim/docs/v27_2_oracle_first_functional_training_and_per_core_loss.md`。

**日期**：2026-07-10
**状态**：已按 oracle-first、functional-only 模型接口澄清，供第一轮实现和实验评审使用

相关文档：

- `docs/v27_catastrophic_cpi_root_cause_and_fix_plan.md`
- `docs/v27_functional_temporal_graph_design_review.md`（长期、严格语义审查）
- `docs/eval_v27_ss_tw5000_20k_seedB_c04_c08_c16_c32_summary.md`

## 目录

- [0. 结论先行](#0-结论先行)
- [1. 术语、状态和不变量](#1-术语状态和不变量)
- [2. Epsilon Resident Scheduler](#2-epsilon-resident-scheduler)
- [3. Resident 的计算优化](#3-resident-的计算优化)
- [4. 数据集设计](#4-数据集设计)
- [5. 预测目标和模型接口](#5-预测目标和模型接口)
- [6. 实现范围和分阶段计划](#6-实现范围和分阶段计划)
- [7. 正确性、并行性和吞吐风险](#7-正确性并行性和吞吐风险)
- [8. 验证矩阵和通过标准](#8-验证矩阵和通过标准)
- [9. 最终推荐](#9-最终推荐)

## 0. 结论先行

第一版采用以下最小方案：

```text
固定 functional chunk（K=256 UOP）
        ↓
每个 active core 每次只提供一个当前 chunk
        ↓
使用 predicted end-time skew 与 epsilon 判断 fast/resident
        ↓
slow chunk 可以跨多个 sample 保持 resident
        ↓
以 additive delta_cycles 为调度和主监督单位，CPI 作为兼容输出/辅助目标
```

这套方案的目标是切断原来的：

```text
predicted CPI → 下一窗长度 → 下一次 CPI
```

闭环，同时保留快核连续推进、慢核上下文不丢失的语义。

### 0.1 MVP 决策表

| 项目 | MVP 决策 |
|---|---|
| 模型输入 | functional-safe trace 特征；跨微架构时显式加入数值 uarch 参数；不输入当前 timing/prediction state |
| Chunk 边界 | 按 functional UOP index 固定切分，默认 `K=256` |
| 时间容忍度 | `epsilon`，表示当前 chunk 预测结束时间的最大允许偏差；不设固定 `H` |
| Sample 结构 | 每个 active core 一个当前 chunk；不在第一版拼接多个 fast chunk |
| 慢核处理 | 当前 chunk 保持 resident，可在后续 sample 作为 context 重复出现 |
| 主目标 | `delta_cycles`；`CPI=delta_cycles/n_uops` 为派生量或辅助目标 |
| 首版训练数据 | raw functional trace 固定切 chunk；真实 timing 生成 label，并可用于 oracle context/fast-resident 选择 |
| 真值时间戳 | 不参与 chunk 边界，不进入模型输入；首版可用于 teacher-forced/oracle sample 组织 |
| Predicted rollout | 首模型训练完成后用于闭环评测；只有 exposure gap 明显时才加入少量 hard context |
| 第一版不做 | 完整 temporal graph、精确 MESI/atomic 求解、chunk-level query transformer |

## 1. 术语、状态和不变量

### 1.1 术语

- **Functional chunk**：从一个 core 的 functional trace 中按连续 UOP index 切出的固定片段。
- **当前 chunk**：某个 core 当前尚未提交的 chunk。
- **predicted prefix time `T_c`**：core `c` 已提交 functional prefix 的预测 virtual cycle。
- **predicted duration `Δ̂_c`**：模型对当前 chunk 的预测 cycle 数。
- **predicted end `E_c`**：`T_c + Δ̂_c`，即当前 chunk 的预测结束 cycle。
- **Resident chunk**：由于 `E_c` 相对最早结束时间偏晚，暂不推进 cursor、继续留在后续 sample 中的 chunk。
- **Exposure**：同一个 chunk 被作为模型上下文使用了多少个逻辑 sample/forward；不是 cycle 数，也不是 cursor 推进次数。
- **Context-only**：chunk 仍出现在输入中，但本次不重复提交主监督、不重复推进 cursor。

### 1.2 必须保持的不变量

1. **固定边界**：chunk 边界只由 functional index 和 `K` 决定，不能由真实或预测时间改变。
2. **Exactly once cursor**：每个 chunk 的 functional cursor 只能前进一次。
3. **Exactly once commit**：一个 chunk 的正式 cycle/state/loss 提交最多发生一次。
4. **时间不重置**：不能把不同 core 的 `T_c` 强行改成同一个值，也不能凭空插入 idle cycle。
5. **真同步优先**：barrier、lock、join、atomic 顺序等已知同步语义优先于 epsilon 调度。
6. **模型接口一致**：训练和推理使用相同的 functional feature extractor、mask、uarch conditioning 和输出接口。
   首版 oracle context 必须显式标记为 teacher-forced；最终验收必须另跑完全 predicted scheduler rollout。

## 2. Epsilon Resident Scheduler

### 2.1 每核状态

```text
cursor[c]          当前 functional UOP 位置
T_pred[c]          已提交 functional prefix 的 predicted virtual cycle
chunk[c]           当前 chunk_id
E_pred[c]          当前 chunk 的 predicted end cycle
resident[c]        当前 chunk 是否因 skew 暂留
exposure[c]        当前 chunk 已被引用的次数
state_version[c]   动态/shared-state 版本
```

模型输入是每个 active core 的一个当前 functional chunk；`T_pred/E_pred/delta_hat` 由外部 scheduler 维护，
不作为单阶段 duration head 的输入：

```text
sample t:
    core0 -> c0[j0]
    core1 -> c1[j1]
    core2 -> resident c2[j2]
    ...
```

Sample 是模型计算容器，不表示所有 core 的 UOP 必须覆盖同一段真实 cycle 区间。

### 2.2 调度判据

对当前所有 active core 计算：

```text
E_min = min_c(E_pred[c])
limit = E_min + epsilon
```

- `E_pred[c] <= limit`：core `c` 属于当前 fast group，可以推进一个下一个固定 chunk。
- `E_pred[c] > limit`：core `c` 属于 slow/resident，保持当前 chunk。

推进 fast group 后必须重新计算 `E_min` 和 `limit`，不能一次性盲目展开很多 chunk。由于最快 core 总满足
`E_pred[c] <= E_min + epsilon`，每轮至少会推进一个 core；同时必须受 token/forward budget 约束。

### 2.3 一轮调度的伪代码

```text
while not all_cores_finished:
    for each active core c:
        if chunk[c] is empty:
            load fixed-K chunk at cursor[c]
            predict Δ̂[c]
            E_pred[c] = T_pred[c] + Δ̂[c]

    emit current sample:
        chunk_id[c], resident[c], context_only[c], exposure[c]
        predicted start/end, lag, state version

    E_min = min(E_pred[c]) over active cores
    fast = {c | E_pred[c] <= E_min + epsilon}
    slow = active - fast

    for c in slow:
        keep chunk[c] resident
        do not advance cursor or commit its cycle again

    for c in fast:
        commit current chunk exactly once
        T_pred[c] = E_pred[c]
        advance cursor[c] by one fixed chunk
        load/predict the next chunk for the next sample

    stop/split if token budget, forward budget, exit, or true sync event requires it
```

`T_pred` 和 `E_pred` 必须分开保存：resident chunk 被重新引用时，不能把它的 `Δ̂` 再次加到 `T_pred` 上。

### 2.4 具体例子

设慢核当前 chunk 的 `E_pred=1000`，快核当前 chunk 的 `E_pred=300`，`epsilon=500`：

```text
E_min = 300
limit = 800
```

慢核 chunk 保持 resident，快核可以推进到下一个 chunk：

```text
sample 0: slow=S0, fast=F0
sample 1: slow=S0, fast=F1
sample 2: slow=S0, fast=F2
```

如果 `F1` 的预测结束时间是 700，则与慢核相差 300，已经进入容忍范围；如果是 900，也只相差 100。
过程中不把慢核的 1000 改成 800，也不让快核额外空转 700 cycles。

### 2.5 多核和同步边界

MVP 使用全局 `E_min + epsilon` 判据：一个极慢 outlier 不会阻止其他较快 core 推进；结束时间明显偏晚的
core 保持 resident。多时间簇的层次聚类属于后续优化，第一版不引入。

如果 chunk 或 trace 明确包含 barrier、lock、join、atomic order 等同步事件，调度器必须在同步边界执行相应
约束，不能让 fast core 仅凭 predicted time 越过同步点。若 functional trace 没有这些标记，模型无法恢复
不可观测的真实跨核 cycle 顺序；epsilon 只能保证数据组织一致，不能提供 cycle-accurate 正确性证明。

### 2.6 `epsilon` 的含义和标定

`epsilon` 是 predicted end-time 的容忍偏差，不是等待时间，不是时间校正量，也不是固定时间窗 `H`。

第一轮建议扫描：

```text
epsilon ∈ {250, 500, 1000} predicted cycles
```

同时记录 chunk duration 分布。`K` 和 `epsilon` 必须联调：若典型 chunk 约 200 cycles，`epsilon=500` 可能过宽；
若典型 chunk 约 2000 cycles，同一个值可能过窄。

## 3. Resident 的计算优化

慢核 chunk 在多个 sample 中重复出现是语义上的重复引用，不应实现成每次都完整重复编码。

### 3.1 静态编码缓存

推荐把模型拆成：

```text
static_chunk_encoder(functional chunk, uarch) -> h_static
dynamic_interaction(h_static, shared/dynamic state) -> prediction
```

`h_static` 可按以下 key 缓存：

```text
(trace_id, core_id, chunk_id, uarch_config, checkpoint)
```

resident 再次出现时只重算动态交互层。如果 chunk 的输入混有可变 shared-state feature，只缓存不随窗口变化的
functional 部分；动态部分按 `state_version` 刷新。Transformer 后续可缓存 resident 的 K/V。

### 3.2 Exposure 统计和可选保护

`exposure[c]` 统计同一个 resident chunk 被作为上下文使用的逻辑 sample/forward 次数。chunk 真正推进后计数
清零。它不是 correctness rule。

第一版建议只记录：

```text
resident_exposure
unique_chunk_encode
cache_hit_rate
dynamic_interaction_time
```

如果极端 case 导致计算或显存失控，再增加可选的 `max_resident_exposure`（例如 16/32）作为 soft limit：

1. 优先合并有限数量的 fast chunks 为 bounded microbatch；
2. 或在允许的范围内增大 `epsilon`；
3. 只有同步语义或预算不允许时，才暂停 fast group。

不能为了满足上限而丢弃仍在执行的 slow chunk。若已有 static/KV cache 和 token budget，MVP 可以不设置硬上限。

## 4. 数据集设计

### 4.1 数据单位和三层存储

数据集的基本单位是 scheduler rollout sample，而不是由真实 tick 对齐出的时间窗。

建议拆成三层：

```text
chunks.parquet   固定 functional chunk 表，每个 chunk 一行
rollout.jsonl    epsilon scheduler 产生的 sample/transition 清单
labels.parquet   按 chunk_id 关联的 cycle/CPI/endpoint 监督
```

#### `chunks.parquet`

```text
trace_id, core_id, chunk_id
uop_start, uop_end, n_uops
functional features
load/store/atomic/branch/sync flags
```

#### `rollout.jsonl`

```text
trace_id, uarch_id, rollout_id, step
chunk_id[core], active[core]
resident[core], context_only[core], exposure[core]
T_pred[core], E_pred[core], relative_lag[core]
previous_pred_cpi[core]
sync/barrier mask
shared-state version/hash
```

#### `labels.parquet`

```text
trace_id, core_id, chunk_id
start_boundary, end_boundary
delta_cycles, cpi
endpoint/prefix labels
```

同一个 resident chunk 可以在多个 rollout sample 中出现，但 `chunk_id` 不变、cursor 不重复推进、exposure 递增。
重复出现的 `context_only=1` 不重复计算主监督；只有 chunk 真正提交时才使用一次 primary cycle/CPI label。

### 4.2 数据生成流程

#### Step A：按 functional index 固定切 chunk

```text
chunk_j = functional_trace[u_j : u_j + K]
key = (trace_id, core_id, start_uop, end_uop)
```

尾 chunk 可以短，但不能因为真实或预测时间改变边界。

#### Step B：固定边界后生成 teacher label

如果 raw trace 有 `_commit_tick` 或等价 boundary label，则在 functional chunk 边界已经确定后计算：

```text
delta_cycles[c, j] = boundary_tick[c, j+1] - boundary_tick[c, j]
cpi[c, j]          = delta_cycles[c, j] / n_uops[c, j]
```

真实 tick 不得参与 chunk 边界，也不得作为模型输入。首版允许使用 `delta_cycles` 运行 oracle scheduler，
只用于选择同一训练 sample 中的跨核 chunk 组合。

每个 core 都要检查：

```text
sum(delta_cycles) == 规定 ROI endpoint span
```

ROI endpoint、drain、store visibility 是否计入目标，必须在数据合同中固定。

如果完全没有 timing label，则无法进行有监督的 CPI/cycle 训练，需要离线 simulator/hardware teacher 或改成
自监督/蒸馏目标。

#### Step C：生成首版 oracle context

第一版不依赖已有模型。使用真实 `delta_cycles` 运行 epsilon scheduler，保存：

```text
chunk_id[core], active[core]
first_exposure[core], context_only[core], exposure_count[core]
```

oracle `T/E/delta` 可以用 `audit_*` 字段另存，但 collator 不得将其送入模型。模型只读取所选 chunk 的
functional-safe 特征。每个 unique chunk 的绝对 timing 主监督只计算一次；resident 重复 context 的总权重按 exposure
归一。

#### Step D：首模型完成后运行 predicted rollout

首模型训练完成后，用模型输出驱动与部署相同的 scheduler，报告 oracle/predicted fast-set disagreement、context OOD
和 prefix/end-time drift。只有 exposure gap 明显时，才把少量 model-visited chunk 组合加入 hard-context buffer；
chunks 和 labels 不重建。

### 4.3 数据分布和切分

首模型训练数据建议拆成：

- unique-chunk view：每个 fixed chunk 一次，承担绝对 timing 主监督；
- oracle-context view：真实 timing 只用于选择跨核 functional context，承担 interaction/centered/slow-core 监督；
- 当前 true-time 变长 TQ windows 只作诊断，不进入主分布；
- model-visited hard context 仅在首模型闭环评测显示 exposure gap 后加入，建议从 10--30% 起步。

train/val/test 必须按 workload、seed、raw trace、uarch/config group 切分，不能对同一 trace 的不同 rollout 做
sample-level 随机切分。跨微架构测试时至少保留一组完全 unseen 的 uarch/config。

## 5. 预测目标和模型接口

### 5.1 Cycle 为主，CPI 为辅

固定 chunk 下：

```text
delta_cycles = cpi * n_uops
cpi = delta_cycles / n_uops
```

两者在已知 `n_uops` 时信息等价。部署语义使用 cycle，因为它适合调度和长程累计：

- 可直接更新 `T_pred`；
- 可直接做 prefix/end-time/makespan loss；
- 可保持 cycle accounting、prefix 和 endpoint 的严格守恒。

CPI/log-CPI 更适合作为模型的数值参数化，因为它消除 chunk 长度的一阶尺度。模型输出 log-CPI，scheduler 消费
由它唯一派生的 `delta_cycles`；不建立相互独立的 CPI 和 cycle 双 head。

### 5.2 MVP 兼容实现

当前模型已有 per-core `log_cpi` head，第一版可以保留：

```text
raw output       -> log_cpi
pred_cpi         = exp(log_cpi)
pred_delta_cycle = pred_cpi * n_uops
```

首轮逐核 loss：

```text
L = 1.00 * L_abs_log_cpi
  + 0.50 * L_centered_cross_core
  + 0.15 * L_slow_listwise
  + 0.25 * L_prefix_logsum_cycle
```

其中：

- `L_abs_log_cpi`：每个 unique chunk 一次的 per-core log-CPI Huber，是绝对精度主损失；
- `L_centered_cross_core`：同一 oracle context 内去掉公共均值后监督每核 residual，直接惩罚可辨识样本的核间塌缩；
- `L_slow_listwise`：只在 high-spread sample 上用 softmax/KL 监督 slow-core 排序，避免全部 `C^2` pair 稀释；
- `L_prefix_logsum_cycle`：仅在连续同 trace/core 序列上监督累计 cycles。

不再使用线性 raw-cycle Huber；经 `delta=n*exp(log_cpi)` 反传后，它可能比 log-CPI 梯度大数百至数千倍。
逐点 `log(delta)` 与 log-CPI error 数学等价，也不重复加入。spread 首轮只作指标，确认幅度系统性不足后才加入小权重。
完整公式、spread gate、exposure weighting 和 batch 合同见
`/data00/yinhaolang/TCSim/docs/v27_2_oracle_first_functional_training_and_per_core_loss.md`。

### 5.3 输入特征和防泄漏

第一版保留：

- provenance 合格的 functional chunk 特征：opclass、dependency、memkind、reuse/stride、branch、PC/macro position；
- 当前 chunk 集合内由 functional address 派生的 same-line/read-write/atomic relation；
- 每 chunk functional summary、`n_uops`、显式 valid mask；
- active/exit/sync mask 和 functional cursor/progress；
- 跨微架构时的明确数值 uarch 参数。

第一版禁止作为模型输入：

- true tick 决定的 chunk 边界或窗口长度；
- 当前 sample/chunk 的真实 CPI 或 cycle；
- 当前 chunk 的 `delta_hat` 和 `E_pred`；
- 由当前真实 delta 累积的 `T/E`；
- 用真实 commit order 更新的 shared-state proxy；
- 暗示未来 chunk 数量的字段；
- 依赖本轮变长窗口长度的 pooling shortcut。

`T_pred/E_pred/delta_hat` 由外部 scheduler 在模型输出后维护，不回灌到同一个单阶段 duration head。`resident`、
`context_only`、`exposure` 默认只用于 sample 组织、mask 和权重，不作为 timing shortcut。若未来做 two-pass refinement，
第二阶段必须读取真实 base-model 输出，不能用 label 替代 preliminary prediction。

### 5.4 跨微架构泛化

cycle 和 CPI 都不是天然架构无关。issue width、pipeline、cache/TLB、memory system 都会改变结果，因此：

1. uarch 参数必须作为显式输入；
2. 统一学习 core cycles，wall-time 由 `seconds = cycles / frequency` 外部换算；
3. 用 per-chunk cycle 做 prefix/scheduler 监督；
4. 保留 log-CPI 作为归一化辅助目标；
5. 用 unseen-uarch/config split 验证泛化。

概念上可以写成：

```text
delta_cycles
  = intrinsic/base cycles(uarch, functional chunk)
  + bounded interaction residual(shared/resource state)
```

第一版可将两部分合并为一个 head，但不能只报告 aggregate CPI；至少要报告 per-chunk cycle、per-core endpoint、
aggregate core-cycle 和 makespan。

## 6. 实现范围和分阶段计划

### Phase 0：固定 chunk 和离线检查

- 实现固定 `K=256` 的 chunk builder；
- 检查 chunk key、尾 chunk、active/exit、sync marker；
- 生成 `chunks.parquet`；
- 不引入任何真实 tick 到边界逻辑。

### Phase 1：Functional 特征、标签与 oracle context

- 迁移并审计旧 TSim 的 functional-safe UOP/summary/relation 特征；
- 生成 boundary-to-boundary cycle label，检查 ROI endpoint 守恒；
- 使用真实 duration 生成 oracle chunk 组合，但不把 timing 数值送入模型；
- 生成 unique-chunk、oracle-context 和连续 sequence 三种训练 view；
- 按 workload/trace/seed/uarch group 切分 train/val/test。

### Phase 2：首模型与逐核 loss

- 保留单一 raw log-CPI head并派生 `delta_cycles`；
- 实现 `L_abs + L_centered + L_slow_listwise`；
- prefix 只在连续同 trace/core sequence 上计算；
- 通过 high-spread single-sample overfit 和 visible-signature collision 门；
- 报告 centered RMSE、std ratio、rank 和 slowest top-k，而不只 aggregate CPI。

### Phase 3：外部 scheduler 与闭环评测

- 模型输出后，外部 scheduler 维护 per-core cursor、`T_pred/E_pred` 和 resident ID；
- 实现 epsilon fast/resident、exactly-once accounting、active/exit、sync 和 budget；
- 比较 oracle/predicted fast set、context OOD、prefix/end-time drift；
- 只有 exposure gap 明显时才加入少量 model-visited hard context；
- 实现 inference/static embedding cache，并统计真实 encode hit 和动态交互耗时。

### Phase 4：有限交互增强

只有 Phase 1--3 已解决主要 outlier 后，再考虑：

- 1024-UOP static encoding block；
- bounded fast-chunk microbatch；
- resident K/V cache；
- 有限 interaction refinement；
- chunk-level query head 或更完整 temporal graph。

## 7. 正确性、并行性和吞吐风险

| 风险 | 典型表现 | MVP 防护/指标 |
|---|---|---|
| predicted skew 漂移 | fast core 无限向前、prefix 发散 | 每轮重算 `E_min`，token/forward budget，prefix drift |
| resident 重复累计 | cycle、state 或 loss 被重复提交 | exactly-once cursor/commit，context-only mask |
| 同步语义越过 | barrier/lock 顺序错误 | sync marker 优先，单独做同步 workload |
| resident 计算爆炸 | forward、显存、延迟上升 | static/KV cache，exposure 统计，bounded microbatch |
| oracle/predicted context 不一致 | offline 好、online 崩 | 首模型后测 fast-set disagreement；按需加入 model-visited hard context |
| 长度 shortcut | 模型依赖本轮总 token 数 | 固定 K，禁止泄漏字段，做 length ablation |
| 多核 outlier | 一个极慢核拖住全局 | 全局 `E_min+epsilon` 先让其他快核推进；报告 core 分桶 |
| uarch 外推失败 | unseen config 误差大 | 显式 uarch 输入，group split，cycle + CPI 双指标 |

注意：epsilon resident 只解决窗口组织和信息保留问题；在没有真实跨核时间或完整同步事件的 functional trace
上，不能宣称恢复精确的 cycle-level 并行顺序。

## 8. 验证矩阵和通过标准

### 8.1 对照组

依次比较：

1. 当前 true-time TQ / pred-driven variable-length baseline；
2. fixed `K=256`，每核一个 chunk、无 resident interaction；
3. fixed `K=256` + epsilon resident scheduler；
4. 方案 3 + functional relation/interaction features；
5. 方案 4 的 fully-predicted scheduler rollout；
6. 方案 5 + static embedding/KV cache 或一次有限 interaction refinement。

重点 workload：

```text
W_chase_dram@c16      W_stream@c32
W_chase_dram@c32      W_search_index_proxy@c32
W_graph_recall_proxy@c32
W_false_sharing@c04
```

### 8.2 必报指标

- aggregate core-cycle/CPI error；
- per-core CPI MAPE p50/p90/p99；
- centered log-CPI RMSE、pred/true std ratio；
- informative-pair rank accuracy、slowest top-1/top-2 recall；
- per-core prefix drift；
- predicted start/end skew；
- endpoint/makespan error；
- UOP/s、forward count、peak memory；
- unique chunk encode、cache hit rate；
- fast/slow ratio、resident exposure 分桶；
- 同步 workload 的 barrier/lock 顺序错误率（若有标记）。

### 8.3 首轮通过条件

- 误差不随 rollout 进度单调发散；
- c16 chase 不再出现 171.99% 级别 outlier；
- c32 p90 workload error 明显低于当前 41.55%；
- 模型输入不包含 true tick、当前真实 delta 或由它派生的 T/E；
- 首版 oracle context 与 fully-predicted rollout 分开报告；
- `sum(delta_cycles)` 与 endpoint label 守恒；
- static cache 后重复 chunk 编码次数明显下降；
- 吞吐至少达到当前 c32 `18.4k UOP/s` 的 0.5 倍，再继续扩大模型。

`epsilon` 至少做一轮 sweep；不能只报告单一 epsilon 的结果。`K=256` 稳定后，再评估 `K=1024` 对吞吐、误差和
resident exposure 的影响。

## 9. 最终推荐

先实现并验证：

```text
K=256 fixed functional chunk
+ rich functional-safe features and relations
+ true boundary label + oracle context selection
+ raw log-CPI head 兼容
+ per-core absolute + centered + informative slow-core loss
+ first accurate model
+ external predicted virtual-time epsilon scheduler evaluation
```

第一版不要同时引入多个 fast chunk 的复杂 readout、完整 temporal graph 或精确事件求解。若该 MVP 已经消除
主要 feedback outlier，再按 Phase 4 增强吞吐和跨核交互建模。
