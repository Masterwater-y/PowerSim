# v27.2 澄清方案：Oracle-First Fixed Chunk 训练与逐核 Loss

**日期**：2026-07-10  
**状态**：当前实现与后续修改的规范文档  
**替代内容**：本文替代 `v27_fixed_chunk_resident_mvp_plan.md` 中“首轮必须由模型生成 rollout”的解释，
并替代 `v27_1_results_and_loss_redesign.md` 第 4--6 节的 mean-collapse 与 loss 结论。

> **2026-07-14 实施决议（优先于本文此前涉及闭环的表述）**：当前版本保持
> fixed-`K=256` 的 **full QKVR**，并且训练、验证、评估均只使用 oracle-selected
> context。predicted rollout、由预测重排的跨核 context、以及基于其构造的新标签均不在
> 当前范围内。原因是预测改变各核推进关系后，重排 context 不再有可直接对应的真实跨核
> timing label，会把 duration 误差和 context 重排误差混为一项。
>
> **同日 loss 决议**：删除 `L_slow` listwise/rank loss。当前训练目标仅为
> `L_abs + 0.25 L_center + 0.10 L_branch`；本文后续关于 `L_slow` 的历史设计说明
> 不再是实现合同。

## 0. 决策摘要

首版目标是训练一个尽可能准确的、逐核逐 fixed-chunk 的 duration 模型：

```text
functional trace chunk(s)
        -> model
        -> per-core log-CPI
        -> per-core delta_cycles = n_uops * exp(log-CPI)
```

首版训练集不需要由已有模型生成。正确顺序是：

```text
functional trace --固定 K--> chunks + functional features
commit_tick      -----------> true delta_cycles labels
true delta       -----------> oracle cross-core context selection

functional-only model input + true labels
        -> train/evaluate oracle-context full-QKVR model
```

关键约束：

1. 真实 timing 可以用于生成监督标签，也可以用于首版 oracle fast/resident 上下文选择。
2. 当前 chunk 的真实 `delta_cycles`、由它计算的 `T/E` 不得进入模型输入。
3. `T_pred`、`E_pred`、`delta_hat` 是推理 scheduler 的内部变量；单阶段 duration model 不读取它们。
4. 当前阶段不生成或评估 model-generated rollout，也不做 hard-context 增广。
5. loss 可以强化已经存在的逐核信号，但不能从完全相同的可见输入中创造核间差异。
6. 当前 TCSim 的 opclass + 8 flags 不足；应迁移旧 TSim 中 provenance 合格的 functional 特征。

## 1. 三个容易混淆的对象

### 1.1 Functional chunk

每个 core 按 functional UOP index 固定切分：

```text
chunk[c,j] = trace[c][j*K : (j+1)*K]
```

边界与真实或预测时间无关。`chunks.parquet` 和 chunk feature cache 对所有模型可复用。

### 1.2 Oracle context rollout

如果模型只独立预测每个 chunk，不需要 rollout。如果模型需要跨核 interaction，则需要确定一个训练 sample
中各核应该出现哪个 chunk。首版使用真实 duration 运行 epsilon scheduler，得到 oracle chunk 组合：

```text
sample s:
  core0 -> chunk 35
  core1 -> chunk 18
  core2 -> resident chunk 18
```

oracle timing 只负责选择这些 chunk ID；模型真正读取的是这些 chunk 的 functional 特征。

### 1.3 Predicted scheduler rollout（当前不实施）

这曾是后续部署方向；当前阶段明确不实现、不训练也不评估。若未来重新讨论，部署 scheduler
才会使用模型输出维护：

```text
T_pred[c] = sum(previous predicted delta_cycles)
E_pred[c] = T_pred[c] + current model output
```

然后用 `E_min + epsilon` 决定 fast/resident。但其重排后的 context 没有直接真实 timing 标签，
因此不能与本版本 oracle-context duration 评估混用。

### 1.4 当前 full-QKVR 执行合同

每个真实 core-chunk 始终保留固定 `K=256` UOP 槽位（仅最后一个 tail chunk 使用
`valid_uop_mask`）。batch 将各 sample 的真实 active core 展平为 `[N,K,F]`，并用
`sample_ptr` 标识 sample 边界；不会为了对齐而补齐到 32 个 core。

模型保持每个 UOP 的 full QKVR：本核 `Q` 查询本核 K/V；跨核 `R` 查询同一
oracle context 中所有其他 core 的全部有效 UOP K/V。跨核执行按 active-core 数分桶，
并调用 batched PyTorch SDPA。当前 `cross_target_block=0`，即同一 context 的全部
target core 一次执行；若未来显存证据要求，可设正数分块。这不改变任何 attention pair
或标签语义。

固定 K 下，ragged UOP packing 仅能节省少量 tail padding，不能降低 full cross-attention
的 `O(C(C-1)K^2)` 主复杂度；因此当前优先使用 fixed-K batched SDPA，而非迁移 ragged
执行路径。

## 2. 数据合同

### 2.1 `chunks.parquet`

每个 unique chunk 一行：

```text
trace_uuid, workload, seed, uarch_hash
core_id, chunk_id, uop_start, uop_end, n_uops
per-UOP functional fields
per-chunk functional summaries
valid_uop_mask
```

`trace_uuid` 必须包含 raw trace、seed、collection recipe 和 uarch，不能只使用 workload 名。

全量数据默认写成同语义的 mmap packed cache（`fields/mask/summary/lines/access/scalar.npy`），避免将 259 万
chunk 的嵌套字段全部展开成 Python dict；Parquet 仅保留为小规模兼容格式。

### 2.2 `labels.parquet`

```text
trace_uuid, core_id, chunk_id
start_boundary_tick, end_boundary_tick
delta_ticks, delta_cycles_float, cpi
roi_contract_id, valid_label, quality_reason
```

必须使用 boundary-to-boundary 定义并验证：

```text
sum(delta_ticks[c,*]) == roi_end_tick[c] - roi_start_tick[c]
```

cycle 转换使用每个 trace 的 uarch profile；禁止逐 chunk `int()` 截断后再做守恒检查。

### 2.3 `oracle_context.jsonl`

首版可由真实 delta 运行 epsilon scheduler 生成：

```text
trace_uuid, rollout_id, step
chunk_id[core], active[core]
first_exposure[core], context_only[core]
exposure_count[core]
```

真实 `T/E/delta` 可以作为 `audit_*` 字段单独保存用于检查，但 dataset collator 不得把这些字段送入模型。

### 2.4 两种训练 view

为避免 resident 重复和上下文训练互相污染，训练集提供两个 view：

1. **Unique-chunk view**：每个 `(trace, core, chunk)` 恰好一次，用于绝对 duration 主监督。
2. **Oracle-context view**：保留同一 sample 的跨核 functional chunks，用于 interaction 与 centered loss。

若同一 resident chunk 在多个 context sample 中出现，context loss 的总权重必须按 exposure 归一，保证该 chunk
跨整个 rollout 的总权重不因 resident 次数增加。

## 3. 模型输入

### 3.1 第一版允许输入

模型只读取部署可获得的 functional 信息：

- per-UOP opclass；
- `n_src/n_dst`、producer class/distance 等依赖信息；
- load/store/atomic/fence、branch 类型；
- PC/macro-op position；
- functional address 的 reuse、stride、page/cacheline relation；
- 当前 sample 内基于 functional address 计算的 same-line/read-write/atomic relation；
- 每 chunk 的 load/store/branch/atomic、distinct line/page、reuse/stride 等 summary；
- `n_uops` 和显式 `valid_uop_mask`；
- active/exit mask、functional cursor/progress；
- 为多微架构泛化准备的数值 uarch 参数。

`core_id` 只用于分组和审计，不作为记忆核身份的 categorical shortcut。若拓扑非对称，应输入物理 topology/NUMA
位置而不是裸 core ID。

### 3.2 第一版禁止输入

- `commit_tick`、真实 CPI、真实 delta cycles；
- 由当前真实 delta 计算的 `T_true/E_true`；
- 当前 chunk 的 `delta_hat`；
- 当前 chunk 的 `E_pred`；
- 由真实时间窗长度产生的 `core_fill_ratio` 或变长 UOP count shortcut；
- 真实 cache/MESI/MSHR/TLB/PMU oracle；
- 用真实 commit order 更新的 shared-state proxy。

### 3.3 `T_pred/E_pred/delta_hat` 的正确位置

单阶段模型的接口为：

```text
delta_hat = model(functional chunks, functional relations, uarch)
T_pred/E_pred = scheduler.update(delta_hat)
```

它们不回灌到同一个 head。若后续引入 two-pass refinement，必须明确区分：

```text
delta_base = frozen/base_model(functional input)
delta_final = refinement(functional input, delta_base)
```

训练时也必须使用真实 base-model 输出，不能用 label 代替 `delta_base`。

## 4. 从旧 TSim 迁移哪些特征

旧 TSim v26 的 clean14 schema 比当前 TCSim 丰富：

```text
opclass, register/dependency bucket, memkind, reuse distance, stride, branch,
PC, macro position, line identity/role, same-core history,
cross-core memory relation, functional coherence relation, fanout
```

迁移原则不是按字段名整体复制，而是逐字段做 provenance 审计：

| 类别 | 首版处理 |
|---|---|
| opclass、dependency、memkind、reuse、stride、branch、PC、macro position | 直接迁移 |
| per-chunk functional counts/density | 重新按 fixed K 计算 |
| same-line、reader/writer、fanout relation | 从当前 functional chunk 集合计算 |
| 绝对 line/PC hash | 改为局部 entity ID、relation edge 或 hash dropout，避免 seed shortcut |
| owner/sharer/history state | 只有不依赖真实跨核 commit order 时才可用 |
| 真实 MESI/path/MSHR/TLB/PMU | 只作 label/诊断，不作输入 |
| true-time window length、fill ratio | 删除 |

在严格 functional-only 的首版中，旧 v27 的 8 个 lagged shared-state 字段默认不启用；等能够从预测或纯 functional
顺序一致地维护 state 后再单独做 ablation。

当前实现已经扩展为：

- 17 个 per-UOP 字段：opclass、寄存器/producer、memkind、producer/reuse distance、stride、branch、局部 PC/line ID、
  macro position、same-core history、访问宽度/line offset、4K/64K recent working-set、xcore role/fanout；
- 27 个 fixed-chunk summary：细分 compute mix、branch、line/page、dependency、locality、stride、PC entropy、basic block；
- 14 个当前 functional context relation：shared/read-write/multiwriter/fanout/global working-set/memory density；
- 28 个数值 uarch 条件，包括 O3 width、ROB/IQ/LSQ、cache/TLB/MSHR/DRAM。

绝对 PC/line hash 已改成 first-touch 局部 ID；原始 line key 只在 dataset 内做 equality join，不作为模型数值输入。
仍未解决的采集缺口是：当前 raw 中 `is_atomic` 全零，且没有 branch taken/target、明确 barrier/spin/lifecycle 字段。

## 5. 为什么以前换很多 loss 仍然无法区分每个核

### 5.1 Loss 不能解决不可辨识输入

如果两个核对模型可见的输入完全相同，而 label 不同，则共享、置换等变模型必然输出相同值。任何 centered、rank、
spread loss 都只能产生无法满足的梯度，不能创造缺失的 dependency/address/relation 信息。

因此先做两个硬门：

1. **单 sample 过拟合门**：选择一个 functional 输入明显不同、真实 spread 较大的 sample，模型必须能把每核误差
   拟合到接近零。
2. **可见签名碰撞门**：按模型实际可见输入做 exact/near signature 分组，报告同 signature 的 label 方差。高方差是
   当前表示的不可约误差下界。

未通过这两门时，不继续调 loss。

### 5.2 逐元素 `.mean()` 本身不导致 collapse

```text
mean_i Huber(pred_i - true_i)
```

各项非负，正负误差不会抵消。均值/中位数预测成为最优解，是因为输入不能区分、模型欠拟合、标签含不可观测状态，
或大量低 spread 样本支配训练，而不是因为调用了 `.mean()`。

真正允许误差抵消的是：先把多个 chunk/core 的误差求和，再对总和做 loss。因此 aggregate、prefix、endpoint 项只能
作为低权重辅助，并且必须保留正确的 trace/core/sequence 边界。

### 5.3 旧 pairwise/rank 尝试为什么容易失效

- c32 有大量真实差异很小的 pair，全部 `C^2` 平均会稀释少量关键 slow/fast pair；
- rank 只约束顺序，不约束绝对 scale 和 gap magnitude；
- 输入缺少核间可辨识特征时，rank loss 无法被满足；
- aggregate cycle loss、raw-cycle loss 或梯度裁剪可能远强于 rank 项；
- resident 重复会让 slow chunk 权重随 exposure 非预期增加；
- 不连续随机 minibatch 上的 prefix/endpoint 会注入 batch-composition noise。

## 6. 推荐 Loss

### 6.1 输出参数化

对 sample `s`、core `c`：

```text
y_sc  = log(delta_cycles_sc / n_uops_sc)      # true log-CPI
z_sc  = model output                           # predicted log-CPI
dhat_sc = n_uops_sc * exp(z_sc)                # scheduler duration
```

不增加独立 raw-cycle head。逐点 `log(delta)` loss 与 log-CPI loss 数学等价，只会重复加权。

### 6.2 绝对逐核主损失

```text
L_abs = macro_mean_group(
          mean_unique_chunk Huber_beta(z_sc - y_sc)
        )
```

建议 `beta=0.2~0.3`。当前实现先按模型可见 functional signature 建立等价组：同组核的 target/prediction 先聚合，
再按组内核数恢复 core-average 权重。这样对称核学习条件均值，而不是拟合随机 core ID。每个 unique chunk 的绝对
监督只在 first exposure 出现一次；训练 sampler 再按 trace 逆频率采样，避免较长的 atomic/skew trace 支配训练。

### 6.3 核间中心化差异损失

对同一个 oracle context sample：

```text
zbar_s = weighted_mean_c(z_sc)
ybar_s = weighted_mean_c(y_sc)

L_center = mean_s,c Huber_beta(
  (z_sc - zbar_s) - (y_sc - ybar_s)
)
```

它去掉全 sample 的共同 scale，直接监督“哪个核更慢、慢多少”。只对满足以下条件的 sample 启用：

```text
n_active >= 2
std(true log-CPI) >= spread_threshold
```

计算前先把同 functional signature 的核压成等价组；若只剩一个组，`L_center=0`。建议首轮
`spread_threshold=0.10`（约 10% log-scale spread），并按真实 spread 连续加权，避免在近似相等、对称或噪声样本上
制造假方差。

### 6.4 信息性 slow-core listwise loss

不再平均全部 core pair。对每个高 spread sample：

```text
p_true = softmax(center(y) / tau)
p_pred = softmax(center(z) / tau)
L_slow = KL(p_true || p_pred)
```

较大的 log-CPI 对应较慢 core。建议 `tau=0.2~0.4`，或者只采样 label gap 超过 margin 的 top-2 slow 与 bottom-2
fast pairs。该项强调 slowest-core 顺序，但不能代替 `L_abs/L_center`。

### 6.5 Spread calibration

初版将以下量作为指标而不是 loss：

```text
spread_ratio = std(pred log-CPI) / std(true log-CPI)
```

只有在 `L_abs + L_center + L_slow` 已能学习核间差异但幅度系统性偏小后，才加入小权重：

```text
L_spread = Huber(log(std_pred + eps) - log(std_true + eps))
```

否则它容易在不可辨识样本上强迫模型制造虚假方差。

### 6.6 Prefix/endpoint loss

只在连续、同 `(trace_uuid, uarch, core)` 的 chunk 序列上计算：

```text
L_prefix = Huber(
  log(sum_{j in prefix} dhat_j + eps)
  - log(sum_{j in prefix} delta_j + eps)
)
```

endpoint 同式，但只对完整 ROI trajectory 计算。禁止在 shuffle 后的随机 scheduler-sample minibatch 中按裸 `core_id`
拼接所谓 prefix/endpoint。

### 6.7 首轮组合

建议从最小组合开始：

```text
L = 1.00 * L_abs
  + 0.50 * L_center
  + 0.15 * L_slow
  + 0.00 * L_prefix
```

当前 sequence batcher 尚未提供连续 trajectory，因此 prefix/endpoint 权重必须为 0；连续 batcher 完成后再从
`L_prefix=0.10~0.25` 消融。首轮不开 `L_spread`。每项都记录对 `z` 和最后一层参数的裁剪前梯度
范数；调整权重使任何辅助项的中位梯度不超过 `L_abs` 的 1 倍，也不低于其 0.05 倍。禁止继续使用当前线性
raw-cycle Huber，因为经过 `dhat=n*exp(z)` 后它的梯度容易比 log-CPI 项大数百到数千倍。

在当前 raw-v27 的 9 区域分层抽样上，collapsed/zero baseline 的 chunk-weighted loss scale 为：

| 项 | 未加权均值 | 乘当前权重后的贡献 |
|---|---:|---:|
| `L_abs` | 0.430 | 0.430 |
| `L_center`（仅可辨识上下文） | 0.038 | 0.019 |
| `L_slow`（仅可辨识上下文） | 0.248 | 0.037 |

辅助项合计约为主项的 13%，适合作为首轮权重；它们不是最终最优值，仍需用 seed-held-out gradient norm 与
slow-group top-1 指标校准。约 30.0% 的抽样上下文满足“至少两个可辨识组且 spread>=0.10”。

## 7. Batch 与 reduction 合同

### 7.1 Context batch 保留二维结构

```text
z, y, core_mask: [B, C]
```

在计算 `L_center/L_slow` 前不能把所有 core flatten。sample 的键必须包含：

```text
(trace_uuid, uarch_hash, rollout_id, step)
```

实现允许 collate 后物理 flatten，但必须保留 `sample_ptr` 和 sample-local `functional_group_id`，loss 先恢复 sample
边界并按等价组聚合；裸 flatten 后跨 sample 求中心化是错误实现。

### 7.2 Sequence batch 显式连续

prefix batch 必须包含：

```text
(trace_uuid, uarch_hash, core_id, start_chunk, length)
```

并断言 chunk ID 连续。不能从随机 minibatch 中按 commit index 排序后冒充 prefix。

### 7.3 Exposure 权重

resident chunk 可以作为 context 重复出现，但：

- `L_abs` 只在 unique-chunk view 计算一次；
- context loss 对同一 chunk 的所有 exposure 权重之和为 1；
- eval 同时报告 unique-chunk 指标与 scheduler-sample 指标，不能混用。

## 8. 逐核可辨识性的必报指标

仅报告 aggregate CPI 无法判断 collapse。每个 workload/core-count/phase/uarch 至少报告：

- per-core/per-chunk log-CPI MAE 和 MAPE p50/p90/p99；
- centered log-CPI RMSE；
- `std(pred)/std(true)` 的 p50/p90；
- Pearson/Spearman；
- slowest top-1/top-2 recall；
- informative-pair order accuracy；
- constant-per-sample、workload-mean、own-chunk-only baseline；
- oracle-context 与 predicted-rollout 的 fast-set disagreement；
- predicted prefix/endpoint drift。

若 `L_abs` 降低但 centered RMSE、std ratio 和 slowest recall 不改善，说明仍在学共同 scale；若单 sample 过拟合失败，
优先检查特征、padding/mask、per-core readout 和 batch packing，而不是继续换 loss。

## 9. 训练与 rollout 阶段

### Phase A：Oracle-first 首模型

1. fixed-K chunk 和真实 boundary label；
2. 迁移 functional-safe 特征；
3. oracle context selection；
4. `L_abs + L_center + L_slow`；
5. teacher-context held-out group evaluation。

这一阶段不需要旧模型或 model-generated rollout。

### Phase B：闭环评测

用 Phase A checkpoint 驱动 scheduler，比较：

- oracle vs predicted fast/resident set；
- model rollout 访问到的 chunk-combination OOD rate；
- prefix/end-time drift；
- workload endpoint/makespan。

如果 fast-set disagreement 和 drift 已低于门槛，不重建主训练集。

### Phase C：按需 hard-context 增广

只有 Phase B 显示明显 exposure gap 时，才将 model-visited context 加入训练。它不产生新标签：fixed chunks 和真实 labels
仍复用，仅新增 chunk 组合。建议 model-context 占比从 10--30% 起步，保留 oracle 和 unique-chunk 主分布。

换模型时只需要按需重建这一小部分 context artifact，不需要重建 chunks/labels。

## 10. 微架构泛化

若同一 functional trace 在不同微架构下有不同 cycle label，则必须二选一：

1. 每个 uarch 训练独立模型；或
2. 将数值 uarch 参数作为条件输入。

完全相同的 functional 输入对应多个不同标签时，仅靠 loss 无法推断当前微架构。推荐输入 issue/commit width、ROB/IQ/LSQ、
cache/TLB/MSHR、memory latency/bandwidth、frequency、core-count/topology 等数值参数，并按完整 `uarch_hash` 做 holdout。

## 11. 当前代码修改优先级

已完成 1--4、等价组 loss、exposure normalization、trace-balanced sampler、显式 split manifest、packed mmap cache，以及
随机 prefix/endpoint/raw-cycle loss 的删除。下一优先级为：

1. 修复并重采 raw-v27 的 workload/atomic blockers（见配套 audit 文档）；
2. 对 seed0 的完整 oracle-context sample 做固定 hash 的 train/dev 切分（dev
   仅作训练监控）；seed1 完全保留给部署侧最终推理，不参与 checkpoint 选择；
3. 实现连续 sequence batcher 后再启用 prefix loss；
4. Phase A checkpoint 完成后实现真正 predicted rollout 与 fast-set disagreement；
5. 增加 single-sample real-trace overfit、seed/address permutation 和 multi-uarch holdout 测试。

在以上正确性门通过前，不继续扩大模型，也不以 teacher-conditioned 两步 smoke 或 aggregate CPI 作为方案有效证据。
