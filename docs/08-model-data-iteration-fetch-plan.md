# 模型与数据方案迭代记录及 Fetch Latency 下一步计划

日期：2026-06-06

本文档记录当前 TAO CPU 模型从 timing-functional 数据集、exec/fetch tail loss、fetch decomposition 到 warmup-window 验证的主要迭代过程，并给出下一轮 fetch latency 建模调整计划。

## 当前结论

当前大 CPI 误差不是单纯训练数据量不足导致。50M 数据已经能让 exec latency 和部分 workload 收敛，但 W11/W13/W14 的 CPI 误差仍主要来自 fetch/cycle deficit。

关键判断：

- d-side 输入已经改为训练/推理同源的 `TimingFunctionalBackend`，不是 Ruby/detailed cache oracle 透传。
- 新 fetch-decomp 模型对 W14/H01 有明显收益，但 W11/W13/H03/W15 有回退，说明 fetch 分解方向有效但当前 loss/推理 gate 设计不稳。
- warmup20k 后 H01/H02/H03 明显改善，说明 holdout 前缀 cold-start 会污染验证；但 W11/W13/W14 基本没修掉，说明核心误差仍是 fetch target/model/loss 设计。
- 当前最值得先改的是 fetch hard gate、fetch head 表达能力和 row-level loss 与 CPI/cycle 的对齐，而不是马上扩大数据量或放大 backbone。

## 迭代时间线

### A. Timing-functional backend 固化

目标：

- 训练数据采集和 driver 推理使用同一套 functional-only d-side feature generator。
- 去掉训练/推理模型输入中的 i-side oracle/cache 字段。
- 避免 train/serve d-side feature skew。

主要改动：

- 新增/接入 `timing-functional` backend。
- generator 使用 retired functional trace 中的 `paddr/is_load/is_store/is_atomic/size/thread_id/micro_seq` 生成 d-side feature。
- d-side feature 包括 `coh_oracle/path_class/mesi_before/d_bank_id/d_llc_set_residency/d_llc_set_lru_pos` 等。
- 模型输入删除 i-side oracle/cache 字段，仅保留可由 retired functional trace 派生的前端结构特征。

结论：

- 当前训练数据 d-side 不是 detailed/Ruby oracle 透传。
- 仍需注意验证 cut-window 的 cache cold-start 会让同源 generator 产生不同输入分布。

### B. 50M timing-functional 训练数据与 v10.3 best

数据：

```text
MTAO/datagen/tmp/timing_functional_50m_20260606_003055/final_balanced_50000000_pq_split_95_5
```

组成：

- W11-W15 每个 workload 约 10M rows。
- stable chunk-hash train/val split。
- guard band 防止 context window 泄漏。

模型：

- `TaoCoreTransformer`
- `context_len=128`
- `d_model=256`
- `n_layer=6`
- `n_head=8`
- 参数量约 4.93M。
- 加入 exec tail-aware loss。

训练：

```text
MTAO/ckpt/exp_tf50m_current_bs32768_w16_8gpu/tao_v10_3_ma16.best.pt
```

状态：

- 8 GPU, global BS=32768, 50000 steps。
- 最终 best 出现在 step 48000。
- 平均吞吐约 54k samples/s。

W11-W15 100k/core 验证：

| workload | CPI pred | CPI truth | error | worst-core fetch err |
|---|---:|---:|---:|---:|
| W11_stream_mix | 1.397 | 5.924 | -76.4% | -98.6% |
| W12_stencil2d | 0.513 | 0.581 | -11.7% | -12.2% |
| W13_graph_walk | 1.118 | 1.973 | -43.3% | -47.5% |
| W14_branch_state | 1.381 | 2.677 | -48.4% | -60.4% |
| W15_indirect | 1.285 | 1.246 | +3.1% | +6.3% |

H01-H03 100k/core 同窗口 holdout baseline：

| workload | CPI pred | CPI truth | error |
|---|---:|---:|---:|
| H01_mixed_service | 0.541 | 0.999 | -45.8% |
| H02_sharded_kv | 0.585 | 0.806 | -27.5% |
| H03_analytics_scan | 0.987 | 1.000 | -1.3% |

收益：

- W12/W15/H03 已经接近。
- 说明模型基本路径、d-side generator 和 infer ckpt 加载是可用的。

问题：

- W11/W13/W14/H01/H02 明显低估。
- per-core diagnostics 显示 CPI deficit 基本由 fetch deficit 主导。

### C. Per-core deficit 诊断闭环

新增诊断：

- per-core fetch/exec/cycle deficit。
- ready-tail deficit。
- after-mispred fetch attribution。
- worst-core summary。

结论：

- 多数大 CPI 误差不是 exec ready-tail 主导。
- 主要是 `fetch_sum_pred` 明显小于 `fetch_sum_truth`。
- 因此后续优化重点从 exec latency 转向 fetch gap / front-end gap / backpressure gap。

### D. Fetch decomposition 数据集与模型

新数据：

```text
MTAO/datagen/tmp/timing_functional_50m_fetchdecomp_20260606_191529/final_balanced_50000000_pq_split_95_5
```

split：

- input rows: 50,000,000
- train rows: 47,330,128
- val rows: 2,373,911
- dropped rows: 295,961
- effective val ratio: 4.776%

新增标签：

```text
fetch_base_latency
fetch_after_mispred_latency
fetch_residual_tail_latency
fetch_after_mispred_k4
```

标签语义：

```text
fetch_total = fetch_base + fetch_after_mispred + fetch_residual_tail
```

其中：

- `fetch_after_mispred`: 同线程内距离最近 committed mispred macro branch 不超过 K=4 的 row。
- `fetch_residual_tail`: 非 after-mispred window 内的 p95+ fetch tail。
- 这是观测归因，不是硬件真实 stall reason oracle。

模型新增：

- fetch base/after-mispred/residual heads。
- fetch p95/p99 tail heads。
- fetch-tail BCE。
- fetch true-tail raw-cycle MAE。
- fetch decomposition Huber。
- decomposition consistency loss。

训练：

```text
MTAO/ckpt/exp_tf50m_fetchdecomp_bs32768_w16_8gpu_resume25000/tao_fetchdecomp_v10_3_ma16.best.pt
```

训练状态：

- 先 4 GPU BS=16384 到 step 12900 后因 SIGHUP 停止。
- 从 step12000 resume 到 8 GPU BS=32768，目标 25000。
- 暂停在 step18500。
- best loss 约 0.5225。
- 当前验证使用暂停时 best。

W11-W15 full 100k/core 对比：

| workload | old err | fetch-decomp err | 变化 |
|---|---:|---:|---:|
| W11_stream_mix | -76.4% | -81.2% | -4.8 pp |
| W12_stencil2d | -11.7% | -0.6% | +11.1 pp |
| W13_graph_walk | -43.3% | -55.0% | -11.7 pp |
| W14_branch_state | -48.4% | -34.2% | +14.2 pp |
| W15_indirect | +3.1% | +12.9% | -9.8 pp |

H01-H03 100k/core 同窗口对比：

| workload | old err | fetch-decomp err | 变化 |
|---|---:|---:|---:|
| H01_mixed_service | -45.8% | -10.2% | +35.7 pp |
| H02_sharded_kv | -27.5% | -23.7% | +3.8 pp |
| H03_analytics_scan | -1.3% | -31.6% | -30.3 pp |

收益：

- W12 明显改善。
- W14 明显改善。
- H01 大幅改善。

回退：

- W11/W13 更差。
- W15 高估变大。
- H03 明显变差。

判断：

- fetch-tail/decomp 对 branch/mixed 场景有帮助。
- 当前分解 loss 或 consistency loss 会干扰共享 backbone，导致 scan/graph/indirect 场景回退。

### E. D-side 输入分布诊断

诊断文件：

```text
MTAO/runs/diagnose_dside_dist_fetchdecomp_20260607.json
```

结论：

- 训练和推理 d-side generator 同源。
- 但验证 cut-window 从每核前缀开始，cache/directory 冷启动导致 driver 输入分布偏移。

关键分布：

| workload | train mem row | driver mem row | train DRAM/mem | driver DRAM/mem | coh TVD |
|---|---:|---:|---:|---:|---:|
| W11 | 20.9% | 33.3% | 0.07% | 6.30% | 0.143 |
| W12 | 14.2% | 17.4% | ~0% | 0.8% | 0.034 |
| W13 | 14.7% | 14.7% | 0.11% | 12.16% | 0.018 |
| W14 | 13.5% | 13.5% | 0% | 0.03% | 0.001 |
| W15 | 21.0% | 21.0% | 0.03% | 0.64% | 0.003 |
| H01 | 16.9% train-all | 11.2% | 0.05% | 10.21% | 0.070 |
| H02 | 16.9% train-all | 8.9% | 0.05% | 8.4% | 0.087 |
| H03 | 16.9% train-all | 9.5% | 0.05% | 10.82% | 0.084 |

判断：

- W14 d-side 分布几乎一致，因此 W14 的改善/误差不是 d-side skew 主导。
- H01-H03 cold-start 明显，必须使用 warmup-window 验证。
- W11/W13 仍有输入分布或长期状态差异，但 warmup 后仍未根治，说明模型/target 仍是核心问题。

### F. Warmup-window 验证

新增验证口径：

```text
--eval-warmup-records-per-core N
```

语义：

- driver 跑完整 `warmup + measured`。
- eval 跳过每核前 N rows，只统计 measured rows。
- measured window 第一个同线程 row 的 cross-boundary fetch gap 置 0。
- PMU 目前仍是 full-window 口径。

warmup20k 后结果：

| workload | full err | warm20k err | 变化 |
|---|---:|---:|---:|
| W11_stream_mix | -81.2% | -81.1% | +0.1 pp |
| W12_stencil2d | -0.6% | +2.1% | -2.7 pp |
| W13_graph_walk | -55.0% | -54.2% | +0.8 pp |
| W14_branch_state | -34.2% | -33.6% | +0.6 pp |
| W15_indirect | +12.9% | +14.6% | -1.7 pp |
| H01_mixed_service | -10.2% | -3.9% | +6.3 pp |
| H02_sharded_kv | -23.7% | -15.8% | +7.9 pp |
| H03_analytics_scan | -31.6% | -28.7% | +2.9 pp |

结论：

- warmup 对 holdout 有明显帮助。
- warmup 对 W11/W13/W14 基本无效，核心问题不是前缀 cold-start。

## 当前 Fetch Latency 设计问题

当前标签：

```text
fetch_latency_i = fetch_tick_i - fetch_tick_{i-1}
```

当前模型方式：

```text
head_logit      -> 判断是否为 fetch/head row
fetch_pos_hat   -> 若为正 fetch gap，预测 gap 大小
fetch_hat       -> hard_head * fetch_pos_hat
```

主要问题：

1. `fetch_latency` 是混合观测量，包含 branch recovery、front-end stall、backend backpressure、memory stall 间接导致的 fetch 推迟。
2. trace 是 retired order，不是真实 front-end fetch bundle stream；branch miss latency 只是自然落到后续正确路径 row 的 fetch gap 上。
3. `is_fetch_group_head` 和 `fetch_tick` 差分相关但不等价。
4. hard gate 漏判会把真实大 gap 直接置 0，是 W11/W13 CPI 严重低估的放大器。
5. row-level log/MAE 与最终 CPI/cycle 累积目标不完全一致。

## 下一轮方案：v10_4_fetch_softgate_mlp

目标：

- 在不重新采集数据、不扩大 backbone 的前提下，先修复 hard gate 与 fetch head 表达能力问题。
- 优先改善 W13/W14/H01/H02/H03 的 fetch deficit。
- 保持 W12/W15/H03 不明显回退。
- 不引入 detailed oracle feature。
- 不先扩大 backbone。
- W11 当前 100k prefix 验证窗口存在 core0 非 steady-state 异常，暂不作为 v10_4 主选择指标；后续单独重建 ROI 稳定窗口后再纳入主判断。

当前二阶段范围：

- 做：driver soft gate 固化、fetch heads 小 MLP、弱化 fetch decomposition loss。
- 暂不做：fetch-sum segment loss、ctx/backbone 扩大、重新采集训练数据。

### P0：固定验证口径

所有新模型默认使用：

```text
warmup_records_per_core = 20000
measured_records_per_core = 80000
```

验证集合：

- W12-W15 warmup20k。
- H01-H03 warmup20k。
- W11 warmup20k 只记录为旁路观察，暂不参与 go/no-go。

主指标：

- `cpi_err_pct`
- `worst_core_fetch_err_pct`
- `fetch_sum_deficit`
- `cycle_deficit`

PMU 暂时标注为 full-window，不作为 warmup-window 主判断。

### P1：Fetch hard gate 改 soft expected gate

当前 hard gate：

```text
head = 1 if sigmoid(head_logit) >= 0.5 else 0
fetch_hat = head * fetch_pos_hat
```

建议改为：

```text
fetch_hat = sigmoid(head_logit / T) * fetch_pos_hat
```

建议：

- 当前先固定 `T=1.5` 作为 baseline。
- 新 ckpt 训练完成后再 sweep `T in {1.0, 1.25, 1.5, 2.0}`。
- head 任务保留为辅助分类。
- 推理 clock 使用 soft expected fetch。

预期收益：

- 避免 head 漏判把 tail gap 直接归零。
- 对 W13/W14/H01/H02/H03 的 fetch deficit 最直接。

风险：

- W12/W15 可能从接近准确变为轻微高估。
- 需要用 warmup-window 验证回归。

已完成的 driver-only 验证：

- soft gate `T=1.5` 已使 W12-W15/H01-H03 平均绝对 CPI 误差明显下降。
- H01/H02/H03/W14 收益最大，W12 小幅高估但仍可接受，W15 改善。
- W11 仍由异常 core0 主导，不能用当前 prefix 窗口判断 soft gate 是否失败。

### P2：Fetch heads 改小 MLP

当前 heads 基本是线性层。fetch 是当前最难任务，建议只增强 fetch head，不放大整个 Transformer。

结构：

```text
Linear(256, 512) -> GELU -> Dropout(0.1) -> Linear(512, 1)
```

应用到：

- `fetch_pos`
- `fetch_base`
- `fetch_after_mispred`
- `fetch_residual_tail`
- `fetch_tail_p95`
- `fetch_tail_p99`

预期收益：

- 增强 fetch 的非线性表达。
- 参数增加很小，不明显增加训练/推理成本。

### P3：弱化 fetch decomposition loss

当前 fetch-decomp 结果显示收益和回退并存，因此下一版不应继续强压分解一致性。

建议权重：

```text
w_fetch_tail_bce = 0.15
w_fetch_tail_mae = 0.05
w_fetch_decomp = 0.03
w_fetch_decomp_cons = 0.0
```

含义：

- 保留 tail-aware。
- 分解 head 只作为弱辅助和诊断。
- 不强制 `base + after_mispred + residual` 解释全部 `fetch_total`。

### P4：暂不加入 fetch-sum segment loss

虽然当前主要误差表现为 `fetch_sum_deficit`，但第一轮二阶段暂不加入 fetch-sum/segment loss。

原因：

- soft gate 已经证明推理侧 gate 设计有收益，应先把训练侧最小闭环做干净。
- fetch-sum loss 直接面向累计量，若实现不谨慎，可能学习 workload/window 统计特征，增加过拟合风险。
- 当前 fetch-decomp 本身已经有收益与回退并存，先减少约束数量，便于判断 MLP head 与 loss 权重的真实作用。
- W11 当前验证窗口异常，若此时加入 fetch-sum loss，容易把异常 prefix tail 当作训练目标方向。

保留为后续 ablation：

```text
segment_len = 128
loss = Huber(log1p(sum(fetch_pred_segment)),
             log1p(sum(fetch_truth_segment)))
weight = 0.02 ~ 0.05
```

只有当 v10_4 softgate+MLP 后仍出现稳定的 non-W11 fetch_sum deficit，且 W12/W15/H03 没有系统性过估时，再启用该 ablation。

### P5：暂不改 context/backbone

当前：

```text
context_len = 128
params ~= 4.93M
```

暂不做：

- context 128 -> 192/256。
- d_model 256 -> 384。
- layer 6 -> 8。

原因：

- 当前已有 W256/W1024 派生统计特征。
- 直接拉长 context 计算成本高，且不能修复 hard gate/head 表达能力问题。
- 应先验证 soft gate + fetch head MLP + 弱化 decomp loss 是否能收敛 fetch deficit。

若 v10_4 后 W13 仍稳定低估，再做 ctx=192 ablation。

### P6：暂不重新采集训练数据

当前不建议马上重新采集或扩大训练数据。

原因：

- 已有 50M timing-functional fetch-decomp 数据，足以验证 head/loss/gate 方向。
- soft gate driver-only 改动已经带来明显收益，说明当前瓶颈至少部分来自推理/训练目标设计，而不是数据量不足。
- 重新采集数据成本高，且如果不先修正 hard gate、fetch head 与 decomp loss 约束，新增数据可能仍被同样的训练目标压成保守 fetch 预测。
- W11 的主要异常来自当前验证切片，不应因为该 prefix 窗口直接扩大训练集。

需要重新采数据的触发条件：

- 重建 W11 ROI 稳定窗口后，W11 仍显示与训练分布一致但长期大幅低估。
- v10_4/v10_4_ablation 后，W13/H03 等 non-W11 holdout 仍有稳定 fetch tail 缺口，且 top deficit rows 显示训练集中 tail 样本覆盖不足。
- d-side 分布诊断显示某类合法 functional-only 特征组合在验证集中高频出现，但 50M 训练集中极少或缺失。

若触发重新采集，优先做定向补充而非直接 100M 全量扩张：

- 补充 W13/H03/W14 中 fetch-tail-heavy segment。
- 补充 W11 ROI 稳定窗口，而不是当前 prefix core0 异常窗口。
- 继续保持 no detailed/Ruby oracle feature。

## 执行计划

### Step 1：代码改动

文件范围：

```text
train/ml/model.py
infer/ml/model.py
train/ml/train.py
infer/driver/inference_driver.py
```

改动：

1. 固化 fetch soft gate 推理路径，默认验证使用 `fetch_gate_mode=soft, fetch_gate_temp=1.5`。
2. 推理 clock 使用 soft expected fetch。
3. fetch heads 改 MLP。
4. 关闭或降低 decomp consistency 默认权重。
5. 不加入 fetch-sum segment loss。

### Step 2：训练 smoke

配置：

```text
ctx=128
BS=32768
8 GPU
steps=1000
```

验收：

- dataset load 通过。
- model forward/loss 通过。
- ckpt 可被 infer driver 加载。
- 日志显示 fetch head MLP 与 decomp loss 权重生效。
- 推理报告记录 `fetch_gate_mode=soft` 与 `fetch_gate_temp=1.5`。

### Step 3：短训验证

配置：

```text
ctx=128
BS=32768
8 GPU
steps=20000
workers=16
```

预计耗时：

```text
约 3.3-3.8 小时
```

验证：

```text
W12-W15 warmup20k
H01-H03 warmup20k
W11 warmup20k sidecar only
```

成功标准：

```text
W13 worst-core fetch err 明显收敛
W14/H01/H02 保持改善
W12/W15/H03 不明显回退
```

### Step 4：决策

若成功：

- 继续训练到 50k。
- 再考虑 ctx=192 ablation。
- 重建 W11 ROI 稳定窗口并纳入下一轮主验证。

若失败：

- 回退 fetch head MLP 或调整 dropout/hidden size。
- 进一步降低 fetch-tail/decomp 权重。
- 对 W13/H03 top fetch-deficit rows 做分布诊断。
- 再决定是否启用 fetch-sum segment loss ablation。

## 当前不建议做的事情

- 不直接扩大训练数据到 100M。
- 不直接上 ctx=256。
- 不把 decomposition head 接入推理 clock。
- 不用 detailed/Ruby oracle feature 修 CPI。
- 不只看 row-level validation loss 选 best。
- 不用当前 W11 prefix core0 异常窗口作为二阶段 go/no-go。

## 参考路径

旧 best：

```text
MTAO/ckpt/exp_tf50m_current_bs32768_w16_8gpu/tao_v10_3_ma16.best.pt
```

新 fetch-decomp best：

```text
MTAO/ckpt/exp_tf50m_fetchdecomp_bs32768_w16_8gpu_resume25000/tao_fetchdecomp_v10_3_ma16.best.pt
```

新数据集：

```text
MTAO/datagen/tmp/timing_functional_50m_fetchdecomp_20260606_191529/final_balanced_50000000_pq_split_95_5
```

W11-W15 fetch-decomp 验证：

```text
MTAO/runs/w11_w15_100k_fetchdecomp_best_20260607_011335
```

H01-H03 fetch-decomp 验证：

```text
MTAO/runs/holdout_mixed_100k_fetchdecomp_best_20260607_012458
```

D-side 分布诊断：

```text
MTAO/runs/diagnose_dside_dist_fetchdecomp_20260607.json
```
