# v22 fixed c04/c08/c16/c32 推理验证快照

快照时间：2026-07-06 17:18 CST

Checkpoint：`ckpt/v22_fixed_centered_soft_c32mix_8gpu_18000/step_014000`

主验证模式：`pred`，`query_placement=tail_local`，`local_fuse_mode=bind_concat`。这些诊断运行都打开了 hidden dump。

## 结果状态

| 核数 | 状态 | 结果目录 | 负载数 | windows | pred-vs-label 平均误差 | 中位误差 | 平均 window MAPE |
| ---: | ---- | -------- | -----: | ------: | ---------------------: | -------: | ---------------: |
| 4    | 完成 | `logs/v22_fixed_latest_c04_pred_hidden_diag_seq_20260706_134216` | 17/17 | 48,693 | 7.31% | 7.34% | 14.79% |
| 8    | 完成 | `logs/v22_fixed_latest_c08_hidden_diag_20260706_112349` | 17/17 | 43,248 | 6.64% | 6.35% | 18.86% |
| 16   | 完成 | `logs/v22_fixed_latest_c16_pred_hidden_diag_seq_20260706_134216` | 17/17 | 40,259 | 8.21% | 3.87% | 69.20% |
| 32   | 完成 | `logs/v22_fixed_latest_c32_pred_hidden_diag_seq_20260706_134216` | 17/17 | 40,656 | 18.23% | 8.39% | 267.16% |

c32 已完成。`W_stream` 已生成最终 workload summary，c32 的 aggregate、spread、hidden summary 也都已生成。

## 各核数最差负载

| 核数 | pred-vs-label 最差负载 |
| ---: | ---------------------- |
| 4    | `W_false_sharing` 14.09%，`W_stream` 14.06%，`W_ads_ranking_proxy` 13.16%，`W_fp_compute_dense` 11.46%，`W_ads_ctr` 10.69% |
| 8    | `W_false_sharing` 16.67%，`W_stream` 10.70%，`W_fp_compute_dense` 9.91%，`W_ads_ctr` 9.79%，`W_feed_ranking` 9.05% |
| 16   | `W_phased_mix` 45.15%，`W_false_sharing` 20.99%，`W_stream` 16.46%，`W_graph_recall_proxy` 10.81%，`W_search_index_proxy` 6.85% |
| 32   | `W_chase_dram` 63.67%，`W_phased_mix` 61.75%，`W_ads_ranking_proxy` 46.22%，`W_graph_recall_proxy` 45.90%，`W_mlp_light` 25.05% |

## spread 与 hidden 诊断

| 核数 | label range p50 | pred range p50 | capture p50 | spread corr | slow-hit | flat pred | hidden pair cos | hidden/label corr | 高 spread 且 hidden 过相似 |
| ---: | --------------: | -------------: | ----------: | ----------: | -------: | --------: | --------------: | ----------------: | -------------------------: |
| 4    | 0.168 | 0.033 | 30.1% | 0.066 | 28.8% | 78.9% | 0.9908 | 0.0649 | 3360/4379 |
| 8    | 0.264 | 0.043 | 31.2% | 0.049 | 16.3% | 74.2% | 0.9921 | 0.0474 | 3951/4916 |
| 16   | 0.342 | 0.050 | 27.6% | 0.055 | 11.3% | 70.8% | 0.9922 | 0.0499 | 5778/7275 |
| 32   | 0.444 | 0.085 | 29.7% | 0.074 | 13.4% | 53.2% | 0.9810 | 0.0634 | 8330/11897 |

c04/c08/c16/c32 的模式一致：核数增加后，每核 label spread 继续变大，但预测 spread 明显偏小，并且和 label spread 的相关性很弱。hidden pair cosine 仍然很高，hidden 与 label 的相关性也很低。这支持之前的判断：模型仍然没有充分区分每个核的状态，尤其在高 spread window 上更明显。

## Window MAPE 定义与分位数

`eval/eval_quota_cycles.py` 打印的 `win_mape_pct` 名字容易误解。它不是 top-level 的整窗聚合 APE，而是对 active `core-window` 样本做算术平均：

```text
abs(pred_core_cpi_uop - label_core_cpi_uop) / (abs(label_core_cpi_uop) + 1e-6)
```

dump 的 window JSON 里还有一个 top-level 的整窗聚合 APE：

```text
abs(pred_window_cpi_uop - label_window_cpi_uop) / (abs(label_window_cpi_uop) + 1e-6)
```

两种口径都需要看。整窗聚合误差回答“这个调度窗口整体 CPI 是否准确”；core-window 误差回答“每个核自己的 CPI 是否准确”，因此它更敏感于慢核/快核错配和每核预测塌缩。

### 为什么 p95/p99 很高但 mean 看起来还行

这不是矛盾，主要来自长尾分布和口径差异：

1. p95/p99 看的是尾部最差的 5%/1% 样本，不会被大量容易窗口稀释；mean 会被大批低误差窗口拉低。
2. core-window 的分母是每个核自己的 `label_core_cpi_uop`。某些核 label CPI 很小，或者某个核突然变成极慢核时，相对误差会被放大到数倍甚至数十倍。
3. 整窗聚合会把不同核的误差相互抵消。例如慢核低估、快核高估时，整窗总 CPI 可能还可以，但每核误差很大。
4. c32 的高尾部集中在 `W_phased_mix`、`W_stream`、`W_chase_dram`、`W_ads_ctr` 这类阶段性强、慢快核差异大或单核极端突发的负载。它们的 p95/p99 会很高，但不是所有窗口都这么差。

因此，mean 说明“多数样本或整体加权还没完全崩”，p95/p99 说明“尾部窗口/尾部核心有严重失真”。当前问题更偏向尾部和每核差异捕捉，而不是所有窗口都同等坏。

### 总体 MAPE 分位数

| 核数 | windows | 整窗 mean | 整窗 p50 | 整窗 p95 | 整窗 p99 | core-windows | core-window mean | core-window p50 | core-window p95 | core-window p99 |
| ---: | ------: | --------: | -------: | -------: | -------: | -----------: | ---------------: | --------------: | --------------: | --------------: |
| 4    | 48,687 | 8.97% | 4.68% | 27.78% | 53.30% | 189,578 | 14.06% | 6.44% | 41.82% | 96.99% |
| 8    | 43,248 | 8.61% | 4.03% | 27.54% | 51.20% | 331,800 | 16.52% | 6.60% | 53.52% | 174.24% |
| 16   | 40,256 | 11.23% | 4.28% | 42.79% | 90.02% | 578,396 | 44.63% | 7.19% | 97.67% | 405.70% |
| 32   | 40,647 | 24.74% | 7.93% | 95.76% | 98.79% | 924,769 | 156.29% | 10.56% | 403.37% | 3003.27% |

### c32 分负载 MAPE 分位数

这张表的 `core-window mean/p50/p95/p99` 与 `win_mape_pct` 使用同一粒度。

| 负载 | core-window mean | core-window p50 | core-window p95 | core-window p99 | 整窗 p50 | 整窗 p95 | 整窗 p99 |
| ---- | ---------------: | --------------: | --------------: | --------------: | -------: | -------: | -------: |
| `W_ads_ctr` | 218.35% | 35.56% | 1275.14% | 2016.59% | 8.87% | 38.07% | 61.72% |
| `W_ads_ranking_proxy` | 210.13% | 279.43% | 415.32% | 518.36% | 54.22% | 98.78% | 118.00% |
| `W_branch_storm` | 15.48% | 11.98% | 43.76% | 67.99% | 3.64% | 15.80% | 23.45% |
| `W_chase_dram` | 526.20% | 44.22% | 3584.89% | 7652.95% | 95.10% | 97.55% | 97.92% |
| `W_compute_int` | 1.66% | 0.94% | 3.91% | 6.74% | 0.31% | 3.09% | 5.21% |
| `W_false_sharing` | 17.52% | 15.16% | 42.03% | 63.22% | 15.53% | 70.15% | 98.77% |
| `W_feed_ranking` | 37.51% | 9.74% | 184.76% | 394.14% | 9.26% | 47.23% | 73.63% |
| `W_fp_compute_dense` | 23.77% | 10.34% | 33.86% | 260.12% | 9.16% | 23.65% | 54.49% |
| `W_fp_lite` | 9.52% | 3.68% | 16.82% | 76.72% | 1.39% | 13.11% | 42.93% |
| `W_graph_recall_proxy` | 181.36% | 68.76% | 809.94% | 1620.59% | 43.67% | 70.54% | 86.58% |
| `W_indirect` | 8.45% | 7.13% | 23.64% | 37.64% | 2.41% | 5.59% | 7.56% |
| `W_int_div` | 7.37% | 5.87% | 18.35% | 25.38% | 2.18% | 5.34% | 9.06% |
| `W_interest_graph_recall` | 143.65% | 22.90% | 771.75% | 1459.84% | 5.21% | 31.48% | 78.28% |
| `W_mlp_light` | 157.10% | 11.38% | 1155.90% | 1827.43% | 3.94% | 54.14% | 93.69% |
| `W_phased_mix` | 2168.59% | 93.51% | 13458.02% | 24070.99% | 92.04% | 96.65% | 97.20% |
| `W_search_index_proxy` | 50.62% | 27.24% | 115.13% | 438.03% | 18.36% | 29.45% | 39.25% |
| `W_stream` | 764.43% | 81.95% | 3416.98% | 6163.76% | 58.77% | 120.34% | 261.73% |

## c08 label-cut 检查

最新 c08 诊断同时包含 `pred` 和 `label` planner 模式：

| 模式 | 负载数 | 平均误差 | 中位误差 | 最差负载 |
| ---- | -----: | -------: | -------: | -------- |
| pred | 17 | 6.64% | 6.35% | `W_false_sharing` 16.67%，`W_stream` 10.70%，`W_fp_compute_dense` 9.91% |
| label | 17 | 14.02% | 10.69% | `W_ads_ctr` 30.17%，`W_ads_ranking_proxy` 28.09%，`W_fp_compute_dense` 27.94% |

这次 c08 上 label-cut 比 pred-cut 更差。因此问题不只是切窗位置。模型/head 仍然没有在 hidden/input representation 里保留足够的每核差异。

## c32 分负载结果

下面是完整 c32 `pred` 结果。

| 负载 | windows | pred-vs-label 误差 | pred CPI/uop | label CPI/uop | window MAPE |
| ---- | ------: | -----------------: | -----------: | ------------: | ----------: |
| `W_ads_ctr` | 2656 | 8.39% | 1.3249 | 1.4463 | 218.35% |
| `W_ads_ranking_proxy` | 3003 | 46.22% | 1.2981 | 0.8878 | 210.13% |
| `W_branch_storm` | 2660 | 2.94% | 0.4473 | 0.4345 | 15.48% |
| `W_chase_dram` | 2506 | 63.67% | 7.7218 | 21.2526 | 526.20% |
| `W_compute_int` | 2771 | 1.34% | 0.4235 | 0.4179 | 1.66% |
| `W_false_sharing` | 2591 | 17.27% | 52.1981 | 63.0916 | 17.52% |
| `W_feed_ranking` | 2039 | 1.80% | 1.2677 | 1.2909 | 37.51% |
| `W_fp_compute_dense` | 2034 | 3.66% | 0.8835 | 0.9171 | 23.77% |
| `W_fp_lite` | 2085 | 3.51% | 0.8058 | 0.8351 | 9.52% |
| `W_graph_recall_proxy` | 1549 | 45.90% | 0.6052 | 1.1187 | 181.36% |
| `W_indirect` | 2731 | 2.05% | 0.9131 | 0.8948 | 8.45% |
| `W_int_div` | 2824 | 1.63% | 0.7527 | 0.7406 | 7.37% |
| `W_interest_graph_recall` | 2078 | 10.93% | 1.2008 | 1.3482 | 143.65% |
| `W_mlp_light` | 3021 | 25.05% | 0.6356 | 0.5083 | 157.10% |
| `W_phased_mix` | 2569 | 61.75% | 5.9539 | 15.5659 | 2168.59% |
| `W_search_index_proxy` | 1175 | 4.03% | 0.3708 | 0.3864 | 50.62% |
| `W_stream` | 2364 | 9.76% | 4.8151 | 5.3361 | 764.43% |

## 当前判断

1. c08 是 c04/c08/c16/c32 里整体指标最好的完整结果，但 hidden 诊断仍然不好：hidden pair cosine 很高，hidden/label spread 相关性很低。
2. c16 更清楚地暴露了当前失效模式。平均误差主要受 `W_phased_mix`、`W_false_sharing`、`W_stream` 影响；中位误差仍低，说明问题集中在高 spread 或 phase-heavy case。
3. c32 完整结果在 `W_chase_dram`、`W_phased_mix`、`W_ads_ranking_proxy`、`W_graph_recall_proxy` 上明显变差。`W_stream` 聚合 CPI 误差是 9.76%，但 window MAPE 很高，因此也是窗口级不稳定信号。
4. c08 label-cut 不能救回来。当前证据仍指向模型 representation/head 对每核行为区分不足，而不只是 planner 切窗问题。

## 异常 CPI 诊断

### `W_false_sharing`

c32 的极高 CPI 是真实现象，不是 label 错误。c32 ROI PMU coverage 是 100%，label vs ROI CPI 相对误差约 `1e-5`。源码明确构造所有线程写同一 cacheline 的不同 word，因此会产生 ownership bouncing：

```c
line[slot] += i;
line[slot] ^= line[(slot + 1) % neigh_mod];
```

ROI/label CPI 随核数快速上升：

| 核数 | pred CPI/uop | label CPI/uop | 聚合误差 | window 中位误差 | 低估窗口占比 |
| ---: | -----------: | ------------: | -------: | --------------: | -----------: |
| 4    | 6.2098 | 7.2001 | 13.8% | 16.7% | 88.2% |
| 8    | 13.5947 | 16.2556 | 16.4% | 13.9% | 97.2% |
| 16   | 23.4851 | 29.6327 | 20.7% | 23.1% | 92.7% |
| 32   | 52.1981 | 63.0916 | 17.3% | 15.5% | 99.0% |

模型抓住了数量级，但系统性偏低。c32 PMU 指向 store-side coherence miss：`l1d_st_miss` 低估 `18.1%`，`l2_st_miss` 低估 `14.0%`，和 CPI 低估量级一致。这个 workload 应作为 coherence stress case 看待，60+ CPI 不代表普通负载分布异常。

### `W_graph_recall_proxy`

这是另一类失效。这个 workload 的 ROI 内没有显式共享写；每线程有私有 CSR、embedding、query buffer，因此不应像 false sharing。更可能的机制是 32 核并发随机 graph/embedding walk 带来的共享 LLC/DRAM/MSHR 压力。

核数趋势很清楚：

| 核数 | pred CPI/uop | label CPI/uop | 聚合误差 | window 中位误差 | 低估窗口占比 | label range p50 | pred range p50 |
| ---: | -----------: | ------------: | -------: | --------------: | -----------: | --------------: | -------------: |
| 4    | 0.4878 | 0.4836 | 0.8% | 7.5% | 44.6% | 0.355 | 0.339 |
| 8    | 0.5263 | 0.4937 | 6.6% | 15.3% | 26.7% | 0.440 | 0.434 |
| 16   | 0.5342 | 0.5989 | 10.8% | 13.5% | 57.1% | 0.971 | 0.902 |
| 32   | 0.6052 | 1.1187 | 45.9% | 43.7% | 88.0% | 3.339 | 2.792 |

c16 聚合上仍然可以接受，但 c32 label 几乎再次翻倍，而预测只小幅上升。c32 PMU 说明 label 是可信的：ROI coverage 是 100%，label vs ROI CPI 约 `1e-5`，missing label uops 只有 `291 / 26,308,724`。

最可疑的 PMU 项是 LLC miss：c16 `llc_miss` 接近真实值（`5222` pred vs `5453` label，误差 `4.2%`），但 c32 明显低估（`11968` pred vs `27170` label，误差 `56.0%`）。c32 的 branch miss 和 L1 load miss 相对正常（`1.5%` 和 `7.7%`）。因此 c32 graph 退化主要是没有学到共享内存/LLC 压力缩放，而不是分支或普通 L1 locality 问题。

### 跨负载模式

c32 其他 outlier 也有类似形状：

| 负载 | c32 pred CPI/uop | c32 label CPI/uop | 聚合误差 | window 中位误差 | label range p50 | pred range p50 |
| ---- | ---------------: | ----------------: | -------: | --------------: | --------------: | -------------: |
| `W_chase_dram` | 7.7218 | 21.2526 | 63.7% | 95.1% | 70.623 | 2.550 |
| `W_phased_mix` | 5.9539 | 15.5659 | 61.8% | 92.0% | 36.471 | 0.344 |
| `W_graph_recall_proxy` | 0.6052 | 1.1187 | 45.9% | 43.7% | 3.339 | 2.792 |

当真实 per-core CPI spread 和 memory stall amplification 在 32 核非线性放大时，失效最明显。模型虽然使用了 c32 数据，但当前输入/head 仍不能稳定地把并发随机访存压力转成正确 CPI 尺度。这与 c04/c08/c16/c32 的 hidden 诊断一致：每核 hidden state 仍然过于相似，预测 spread 与 label spread 的相关性很弱。

### 修复方向

不建议用 workload name calibration 修。下一版应该加入显式的、从 functional trace 可得的 pressure 特征/损失，覆盖：

- active-core 缩放后的随机访存强度；
- 每 window 的 working set、distinct line、reuse distance 压力；
- load/store 拆分，尤其覆盖 coherence-sensitive case；
- cross-core spread/delta 监督，避免 head 把相似 token summary 的核心预测塌缩。

评分上建议保留 `W_false_sharing` 作为单独 coherence-stress 分榜。production-like 回归应优先关注 `W_graph_recall_proxy`、`W_ads_ranking_proxy`、`W_chase_dram`、`W_phased_mix`，因为它们暴露的是真实多核内存压力缩放，而不是单 cacheline bouncing 这种刻意构造的极端点。
