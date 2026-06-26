# v6.2 最新方案与实验结果分析

日期：2026-06-24

## 1. 范围与对比对象

本文记录当前主线方案与最新实验结果，覆盖 3 组 checkpoint：

| 版本 | ckpt | 评估日志 | 说明 |
|---|---|---|---|
| v5 | `ckpt/v5_tq32k_schemeA_2000` | `logs/eval_parallel_v5_tq32k_schemeA_2000_full11_20260623_003518` | 旧版 Scheme A，11 个训练 workload |
| v6.2@1800 | `ckpt/phase0_ddp8_v6.2` | `logs/eval_parallel_phase0_ddp8_v6.2_20260624_001133` | 14 个 workload 训练集，best 在 step 1800 |
| v6.2@4000 | `ckpt/phase0_ddp8_v6.2_continue_to4000` | `logs/eval_parallel_phase0_ddp8_v6.2_continue_to4000_20260624_102553` | 从 step 1800 续训到等效总 step 4000 |

统一评估口径：

- `eval/eval_quota_cycles.py`
- `DT_TARGET=8000`
- `DT_MAX=12000`
- `MAX_LEN=32768`
- 主看 aggregate CPI 的 `pred_vs_label` 与 `pred_vs_roi`

## 2. 当前执行方案

当前主线不是 dedup，而是先把 v6.2 的训练收益和问题点跑清楚。

### 2.1 数据集

- 训练集：`data/windows_v6.2_tq32k/windows.jsonl`
- 总窗口数：16023
- workload 数：14
- 包含 3 个原 holdout 业务负载：
  - `W_ads_ctr`
  - `W_feed_ranking`
  - `W_interest_graph_recall`
- 当前训练实际使用的是**未物理去重**版本

### 2.2 训练策略

- 先跑 v6.2@1800，验证新增业务负载是否带来泛化收益
- 再从 `ckpt/phase0_ddp8_v6.2` 续训到等效总 step 4000，判断误差是 step 不足还是分布/特征问题

### 2.3 当前结论先行

- step 不足确实存在，而且影响不小
- 但 `W_false_sharing / W_int_div` 的问题不是单纯多训能解决
- 当前最合理的下一步是：
  1. 先做 workload-balanced sampler / 轻度 oversample
  2. 再补 `false_sharing` 与 `int_div` 所缺的 functional 特征

## 3. 总体结果

### 3.1 分组汇总

| 版本 | 全部 11 workload pred/label | 全部 11 workload pred/roi | 3 个业务负载 pred/label | 8 个旧机制负载 pred/label | 3 个问题负载 pred/label |
|---|---:|---:|---:|---:|---:|
| v5 | 11.36% | 10.95% | 36.21% | 4.43% | 3.40% |
| v6.2@1800 | 5.68% | 7.41% | 4.07% | 6.16% | 11.68% |
| v6.2@4000 | 4.06% | 4.90% | 1.57% | 4.76% | 11.12% |

分组说明：

- “3 个业务负载”=`W_ads_ctr / W_feed_ranking / W_interest_graph_recall`
- “8 个旧机制负载”=`W_branch_storm / W_chase_dram / W_compute_int / W_false_sharing / W_indirect / W_int_div / W_phased_mix / W_stream`
- “3 个问题负载”=`W_false_sharing / W_int_div / W_indirect`

### 3.2 关键信号

1. 从 v5 到 v6.2@1800，业务负载误差从 `36.21%` 降到 `4.07%`，说明新增业务训练样本的方向是对的。
2. 从 v6.2@1800 到 v6.2@4000，整体从 `5.68%` 进一步降到 `4.06%`，说明 step 不足确实是问题。
3. 但“问题负载组”只从 `11.68%` 降到 `11.12%`，几乎不动，说明 `W_false_sharing / W_int_div` 不是单纯靠多训就能修好。

## 4. 逐 workload 对比

下表为 `pred_vs_label` / `pred_vs_roi` 的逐 workload 对比。

| workload | v5 | v6.2@1800 | v6.2@4000 | 结论 |
|---|---:|---:|---:|---|
| W_ads_ctr | 62.89% / 62.15% | 4.04% / 4.45% | 0.39% / 0.32% | 持续大幅变好 |
| W_feed_ranking | 5.69% / 3.83% | 0.18% / 1.32% | 0.88% / 0.63% | 显著变好 |
| W_interest_graph_recall | 41.00% / 39.81% | 8.10% / 7.76% | 3.45% / 2.66% | 持续大幅变好 |
| W_phased_mix | 4.30% / 3.70% | 3.06% / 5.76% | 1.28% / 2.00% | 4000 step 后明显变好 |
| W_branch_storm | 4.82% / 5.02% | 3.28% / 3.50% | 0.94% / 0.75% | 持续变好 |
| W_chase_dram | 5.87% / 4.67% | 5.33% / 7.79% | 2.13% / 4.11% | 4000 step 后明显变好 |
| W_compute_int | 8.04% / 8.10% | 0.89% / 0.81% | 0.22% / 0.29% | 持续变好 |
| W_false_sharing | 2.24% / 4.23% | 11.76% / 19.23% | 16.70% / 24.35% | 持续变差 |
| W_indirect | 4.94% / 5.04% | 10.35% / 10.47% | 4.52% / 4.39% | 4000 step 后基本恢复 |
| W_int_div | 2.99% / 3.05% | 12.60% / 13.13% | 12.21% / 12.34% | 几乎无改善 |
| W_stream | 2.59% / 1.21% | 4.32% / 8.99% | 2.95% / 4.02% | 4000 step 后基本恢复 |

## 5. 4000 step 最终结果

| workload | windows | pred_cpi | label_cpi | roi_cpi | pred/label | pred/roi |
|---|---:|---:|---:|---:|---:|---:|
| W_ads_ctr | 784 | 1.6887 | 1.6822 | 1.6833 | 0.39% | 0.32% |
| W_branch_storm | 1187 | 0.8518 | 0.8439 | 0.8455 | 0.94% | 0.75% |
| W_chase_dram | 905 | 6.9797 | 6.8342 | 6.7042 | 2.13% | 4.11% |
| W_compute_int | 857 | 0.5260 | 0.5272 | 0.5275 | 0.22% | 0.29% |
| W_false_sharing | 802 | 28.1743 | 24.1431 | 22.6575 | 16.70% | 24.35% |
| W_feed_ranking | 825 | 1.5452 | 1.5318 | 1.5550 | 0.88% | 0.63% |
| W_indirect | 881 | 1.6666 | 1.5945 | 1.5965 | 4.52% | 4.39% |
| W_int_div | 1238 | 2.2694 | 2.0225 | 2.0200 | 12.21% | 12.34% |
| W_interest_graph_recall | 796 | 3.6679 | 3.7988 | 3.7679 | 3.45% | 2.66% |
| W_phased_mix | 1764 | 6.9885 | 6.9004 | 6.8517 | 1.28% | 2.00% |
| W_stream | 879 | 2.7454 | 2.6667 | 2.6392 | 2.95% | 4.02% |

v6.2@4000 汇总：

| 指标 | 值 |
|---|---:|
| weighted pred_vs_label | 4.06% |
| weighted pred_vs_roi | 4.90% |
| total cycle vs label | 8.91% |
| total cycle vs roi | 0.10% |

## 6. 训练步数影响

### 6.1 续训曲线

从 `ckpt/phase0_ddp8_v6.2` 的 step 1800 继续训，rank0 验证 loss 如下：

| 等效总 step | val_loss |
|---:|---:|
| 1800 | -16.1515 |
| 3000 | -24.0258 |
| 3200 | -25.4905 |
| 3400 | -27.0461 |
| 3600 | -28.4855 |
| 3800 | -30.1431 |
| 4000 | -31.0662 |

### 6.2 结论

step 不足是实打实存在的，因为以下 workload 在 4000 step 后明显恢复：

- `W_ads_ctr`: `4.04% -> 0.39%`
- `W_interest_graph_recall`: `8.10% -> 3.45%`
- `W_branch_storm`: `3.28% -> 0.94%`
- `W_chase_dram`: `5.33% -> 2.13%`
- `W_indirect`: `10.35% -> 4.52%`
- `W_phased_mix`: `3.06% -> 1.28%`

但 `W_false_sharing / W_int_div` 没跟着恢复，因此需要单独诊断。

## 7. 问题 workload 诊断

### 7.1 W_false_sharing

关键现象：

- v5 很好：`2.24%`
- v6.2@1800 变差：`11.76%`
- v6.2@4000 更差：`16.70%`

标签口径：

| 指标 | 值 |
|---|---:|
| pred_vs_label | 16.70% |
| pred_vs_roi | 24.35% |
| label_vs_roi | 6.56% |

说明：

1. 这不是训练集和评估集分布漂移导致的。训练集里 `W_false_sharing` 的 `label.cpi mean ≈ 24.6`，评估 `label_cpi ≈ 24.14`，量级一致。
2. ROI 与 label 本身有固定约 `6.56%` 的差，但模型又在此基础上继续高估到了 `28.17`，属于模型校准错误。
3. `W_false_sharing` 在训练集内部几乎没有多样性：`same_workload_nn_p50 = 0.0127`；同时它又是孤岛：`cross_workload_nn_p50 = 3.161`，最近非本 workload 邻居是 `W_fp_compute_dense`。这说明它既窄、又缺邻居支撑。

更关键的是输入缺口：

- 当前 token 只编码单条 uop 的 `OP/RG/MK/RD/ST/BR`
- 当前 `core_summary` 只统计 per-core 的 `rd/stride/branch/dep` 摘要
- 没有任何**跨核共享同一 cacheline** 的显式特征

因此模型看得到“store-heavy + line reuse 很极端”，但看不到“8 个 core 是否在抢同一条 line”。这正是 false sharing 的核心机制。

### 7.2 W_int_div

关键现象：

- v5 很好：`2.99%`
- v6.2@1800 变差：`12.60%`
- v6.2@4000 仍差：`12.21%`

标签口径：

| 指标 | 值 |
|---|---:|
| pred_vs_label | 12.21% |
| pred_vs_roi | 12.34% |
| label_vs_roi | 0.12% |

说明：

1. 这不是 label/ROI 口径问题。`label_vs_roi ≈ 0.12%`，几乎完全一致。
2. 模型就是系统性把 CPI 预测高了：`2.02 -> 2.27`。
3. `W_int_div` 同样是孤岛：`cross_workload_nn_p50 = 3.593`，最近邻几乎全部落到 `W_false_sharing`，说明它在现有 feature 空间中没有好的“慢整数算子”邻居。

更关键的是输入缺口：

- 当前输入只有 `is_int`
- 没有 `int_div / int_mul / add / shift` 级别的 opcode class
- tokenizer 把所有 integer op 都并到一个整数类

所以模型无法稳定分辨“慢除法”与“普通整数 ALU”，只能依赖 branch、依赖距离、寄存器结构去猜。

## 8. 当前对 trace 能力的判断

### 8.1 当前 records.micro 是 commit/retired 流

当前训练输入可视为 retired/commit 流：

- 包含真正退休的 µop
- 包含那条最终退休的 mispredicted branch 本身
- 不包含被 squash 的 wrong-path 指令

因此当前模型看不到错误路径展开本身，只能通过 branch 结果相关的标签和上下文间接学习分支代价。

### 8.2 当前 raw trace 不直接包含 opcode class

当前 schema 没有 `opcode / mnemonic / opClass` 字段，只有：

- `is_int`
- `is_fp`
- `is_simd`
- `is_load / is_store / is_atomic`
- `is_branch / is_call / is_return`

所以：

- 对当前已有 trace，如果要细分 `div/mul/add/shift`，只能离线补
- 最稳妥的补法有两条：
  1. 用 `macro_pc -> binary disassembly` 离线补 `op_subclass`
  2. 直接改 gem5 tracer，让 raw trace 输出 `opClass`

## 9. 当前推荐方案

当前建议已经从“先 dedup”切到“先保住 v6.2 主线收益，再修残留机制孤岛”。

### 9.1 短期：先做采样权重，不动数据集

目标：验证 `W_false_sharing / W_int_div / W_indirect` 的回退是否主要来自训练竞争。

建议：

- 加 workload-balanced sampler
- 或者对以下 workload 做 1.5x-2.0x oversample：
  - `W_false_sharing`
  - `W_int_div`
  - `W_indirect`

判据：

- 如果 2000-3000 step 内这些 workload 明显恢复，而业务负载不退化，则主因是采样/校准问题。

### 9.2 中期：补 true functional 特征

#### 针对 W_false_sharing

加跨核共享线特征，例如：

- `shared_line_rate`
- `multi_core_store_line_rate`
- `max_writer_cores_per_line`
- `same_line_cross_core_reuse`

这些都可以从各核 `vaddr >> 6` 跨核聚合得到，不依赖 timing/oracle。

#### 针对 W_int_div

补 opcode subclass，例如：

- `int_alu`
- `int_mul`
- `int_div`
- `int_shift`

优先级：

1. 最好改 gem5 tracer 直接输出 `opClass`
2. 如果短期不改 tracer，就用 `macro_pc` + 二进制反汇编离线补

### 9.3 dedup 当前不是第一优先级

当前证据表明：

- step 不足对多数 workload 的影响已经被 4000-step 实验验证
- `false_sharing/int_div` 的核心问题是特征表达，不是窗口重复

因此 dedup 暂时不作为当前主线动作，优先级低于 sampler 和特征补充。

## 10. 当前决策

当前建议按以下顺序推进：

1. 以 `windows_v6.2_tq32k` 为固定训练集，不做新的物理 dedup 实验
2. 实现 workload-balanced sampler / per-workload weight
3. 先验证 `W_false_sharing / W_int_div / W_indirect` 是否恢复
4. 若仍恢复有限：
   - 给 `W_false_sharing` 补跨核 shared-line 特征
   - 给 `W_int_div` 补 opcode class / op subclass
5. 若 tracer 可改，直接在 raw trace 中输出 `opClass`，避免长期依赖离线反汇编
