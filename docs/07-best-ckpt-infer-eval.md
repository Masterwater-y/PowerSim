# 最新 Best Checkpoint 推理验证结果与改进计划

日期：2026-06-06

本文档整理当前最新 timing-functional 训练出的 best checkpoint 在两组负载上的推理验证结果，并基于误差形态给出后续改进计划。

## 模型与后端

Checkpoint：

```text
MTAO/ckpt/exp_tf50m_current_bs32768_w16_8gpu/tao_v10_3_ma16.best.pt
```

推理后端：

```text
timing-functional
```

当前 CPI 统计口径已修正为多核 sum/sum：

```text
cpi_sumsum = sum(per-core inferred cycles) / sum(per-core macro instructions)
```

旧口径仍保留为 `cpi_wall`，但不能用于和 gem5 per-core-summed CPI 对比：

```text
cpi_wall = max(per-core inferred cycles) / sum(per-core macro instructions)
```

## 实验集合

### W11-W15 训练类负载验证

结果目录：

```text
MTAO/runs/best_ckpt_infer_eval_u100000_20260606_120647
```

规模：

```text
4 cores x 100000 uops/core = 400000 rows/workload
```

设备：

```text
GPU
```

baseline：

```text
cut-window labels / mem_events oracle
```

注意：这不是 full-run `stats.txt` 对比。原始 gem5 `stats.txt` 是 full-run 统计，而当前实验只覆盖 100k/core 的 cut-window。因此本文中 W11-W15 的 CPI/PMU baseline 是同窗口 labels 和 oracle events，不是 full-run stats。

### H01-H03 Holdout 混合负载验证

结果目录：

```text
MTAO/runs/holdout_mixed_25k_gpu_20260606_164854
```

规模：

```text
4 cores x 25000 uops/core = 100000 rows/workload
```

设备：

```text
GPU via nsenter host namespace
```

使用 `nsenter` 的原因：工具 sandbox 里 PyTorch CUDA 初始化失败，错误为 `cudaGetDeviceCount() Error 304`，但 `nvidia-smi` 能看到 GPU。进入 host namespace 后 PyTorch 能正常看到 8 张 H20。

Holdout workload 源码：

```text
MTAO/datagen/workloads/holdout_mixed_service/holdout_mixed_service.c
MTAO/datagen/workloads/holdout_sharded_kv/holdout_sharded_kv.c
MTAO/datagen/workloads/holdout_analytics_scan/holdout_analytics_scan.c
```

验证脚本：

```text
MTAO/scripts/09_validate_holdout_mixed_100k.sh
```

## 吞吐结果

### W11-W15

| workload | rows | elapsed_s | rows/s | macro/s |
|---|---:|---:|---:|---:|
| W11_stream_mix | 400000 | 87.32 | 4580.85 | 2515.16 |
| W12_stencil2d | 400000 | 86.68 | 4614.67 | 2309.09 |
| W13_graph_walk | 400000 | 86.97 | 4599.29 | 2849.38 |
| W14_branch_state | 400000 | 86.12 | 4644.68 | 2370.78 |
| W15_indirect | 400000 | 86.78 | 4609.36 | 2698.73 |

### H01-H03

| workload | rows | elapsed_s | rows/s |
|---|---:|---:|---:|
| H01_mixed_service | 100000 | 47.62 | 2099.96 |
| H02_sharded_kv | 100000 | 47.51 | 2104.82 |
| H03_analytics_scan | 100000 | 47.73 | 2095.12 |

H01-H03 的 rows/s 低于 W11-W15，主要因为每个负载规模更小，固定启动和文件 I/O 开销占比更高。该结果仍是 GPU 推理路径；此前 CPU holdout 结果只有约 `286-360 rows/s`。

## CPI 结果

### W11-W15

| workload | CPI pred | CPI truth | error |
|---|---:|---:|---:|
| W11_stream_mix | 1.397 | 5.924 | -76.4% |
| W12_stencil2d | 0.513 | 0.581 | -11.7% |
| W13_graph_walk | 1.118 | 1.973 | -43.3% |
| W14_branch_state | 1.381 | 2.677 | -48.4% |
| W15_indirect | 1.285 | 1.246 | +3.1% |

### H01-H03

| workload | CPI pred | CPI truth | error |
|---|---:|---:|---:|
| H01_mixed_service | 0.516 | 1.113 | -53.6% |
| H02_sharded_kv | 0.592 | 1.037 | -42.9% |
| H03_analytics_scan | 0.963 | 1.091 | -11.7% |

## Latency 与 Branch 指标

### W11-W15

| workload | fetch MAE | exec MAE | branch precision | branch recall |
|---|---:|---:|---:|---:|
| W11_stream_mix | 3.50 | 125.15 | 0.243 | 0.324 |
| W12_stencil2d | 0.10 | 0.44 | 0.348 | 0.111 |
| W13_graph_walk | 1.08 | 10.85 | 0.270 | 0.733 |
| W14_branch_state | 1.39 | 3.82 | 0.316 | 0.423 |
| W15_indirect | 0.46 | 0.63 | 0.636 | 0.995 |

### H01-H03

| workload | fetch MAE | exec MAE | branch precision | branch recall |
|---|---:|---:|---:|---:|
| H01_mixed_service | 0.85 | 17.09 | 0.243 | 0.079 |
| H02_sharded_kv | 0.73 | 23.78 | 0.139 | 0.313 |
| H03_analytics_scan | 0.72 | 5.78 | 0.308 | 0.203 |

## PMU 误差

### W11-W15

| workload | l1d.load_misses | l1d.store_misses | l2.misses | llc.load_misses | llc.store_misses | cha.dir_lookup.snp |
|---|---:|---:|---:|---:|---:|---:|
| W11_stream_mix | -0.2% | -5.9% | -3.9% | +171.6% | +33.9% | +0.0% |
| W12_stencil2d | +2.6% | +0.4% | +14.8% | n/a | n/a | +0.0% |
| W13_graph_walk | -3.9% | n/a | +1.3% | n/a | n/a | +0.0% |
| W14_branch_state | +79.1% | +72.3% | +86.5% | +1200.0% | n/a | -22.5% |
| W15_indirect | +24.4% | -99.7% | -22.6% | n/a | n/a | +0.0% |

### H01-H03

| workload | l1d.load_misses | l1d.store_misses | l2.misses | cha.dir_lookup.snp |
|---|---:|---:|---:|---:|
| H01_mixed_service | +3.3% | -36.5% | +13.0% | -15.4% |
| H02_sharded_kv | +1.2% | -82.1% | +10.1% | -9.0% |
| H03_analytics_scan | +1.5% | -4.8% | +11.2% | -5.6% |

## 结果解读

当前 infer 路径已经可以加载最新 best checkpoint，并使用 `timing-functional` backend 完成 W11-W15 与 H01-H03 的推理验证。GPU 推理也已确认可用，只是在当前工具 sandbox 下需要通过 `nsenter` 进入 host namespace。

从 CPI 看，结果分三类：

1. 表现较好：`W12_stencil2d`、`W15_indirect`、`H03_analytics_scan` 的 CPI 误差在约 `12%` 或更低。
2. 明显低估：`W13_graph_walk`、`W14_branch_state`、`H01_mixed_service`、`H02_sharded_kv` 的 CPI 低估约 `43-54%`。
3. 严重低估：`W11_stream_mix` 是最大异常点，CPI 低估 `76.4%`。

从 PMU 看：

1. `l1d.load_misses` 和 `l2.misses` 在 W11/W12/W13 以及 H01-H03 上整体还可以。
2. store miss 仍弱，尤其 H02 的 `l1d.store_misses` 低估 `82.1%`。
3. W14 是最明显的 timing-functional backend/PMU 退化点，L1/L2 miss 全面高估。
4. W15 CPI 很接近，但 store miss 很差，说明 CPI 准确不等于 PMU 准确。

当前主问题不是 CPI 聚合口径。sum/sum 修正后，W12/W15/H03 已经比较接近 baseline。剩余大误差是 workload-dependent 的，主要出现在混合 branch、memory、backpressure 行为上，说明当前模型用局部 `fetch_lat` / `exec_lat` 回归还不足以表达全局 stall。

## 详细改进计划

### P0：先补诊断闭环

目标：把 CPI 误差分解成可定位的来源，避免继续凭总 CPI 调参。

需要新增或固化以下 report 字段：

```text
per-core fetch_sum_pred / fetch_sum_truth
per-core exec_sum_pred / exec_sum_truth
per-core ready_clock_pred / ready_clock_truth
per-core cycle_deficit
after_mispred_fetch_sum
nonbranch_fetch_tail_sum
top-k latency tail rows
```

验收：

```text
每个 workload 能直接回答 CPI 低估来自 fetch gap、exec latency、branch 后 gap，还是 PMU/backend 行为。
```

优先级：最高。没有这个诊断，后续训练和 backend 修改很容易互相掩盖。

### P1：补 fetch-tail loss

当前 tail-aware loss 主要覆盖 `exec_lat`，但已有分析显示 CPI 经常由 `fetch_latency` / inter-fetch gap 支配。尤其 W11/W13/W14/H01/H02 的 CPI 低估，不能只靠 exec tail 修复。

建议在 dataset 和 model 中加入：

```text
fetch_tail_p95
fetch_tail_p99
fetch_tail_p95_thr
fetch_tail_p99_thr
fetch true-tail raw-cycle MAE
```

训练 loss：

```text
loss =
  existing losses
+ w_fetch_tail_bce * BCE(fetch_tail_p95/p99)
+ w_fetch_tail_mae * normalized raw-cycle MAE on true fetch-tail samples
```

验证日志新增：

```text
val_fetch_tail_p95_mae_cycle
val_fetch_tail_p99_mae_cycle
val_fetch_sum_err_by_workload
```

预期收益：

```text
W13/W14/H01/H02 CPI 低估收敛；
W12/W15/H03 不应明显退化。
```

### P2：把 fetch_latency 从混合 target 拆成可解释分量

当前数据定义是：

```text
fetch_latency_i = fetch_tick_i - fetch_tick_{i-1}
execution_latency_i = ready_tick_i - fetch_tick_i
```

这意味着 branch miss recovery、前端 redirect、后端 backpressure、MSHR/ROB 阻塞、同步等待等都会混进 `fetch_latency`。这对平均 MAE 友好，但对 CPI 泛化不友好。

建议逐步拆分为：

```text
fetch_latency_total
fetch_base_gap
fetch_after_mispred_gap
fetch_backpressure_or_residual_gap
```

短期不要直接改变主 target，而是先做多任务监督：

```text
model predicts:
  fetch_total
  fetch_tail
  branch_mispred_prob
  after_mispred_fetch_gap
  residual_fetch_gap
```

中期再切到 decomposed clock：

```text
fetch_clock += base_fetch_gap
fetch_clock += branch_mispred_prob * branch_recovery_gap
fetch_clock += residual/backpressure_gap
ready_clock = max(ready_clock, fetch_clock + exec_lat)
```

注意：不要简单在当前 `fetch_latency` 之外再加固定 branch penalty 作为最终方案，因为 branch miss latency 已经被当前 `fetch_latency` target 吸收，直接叠加可能 double count。固定 branch penalty 只适合作为诊断实验。

### P3：修 W14 timing-functional backend

W14 的问题不是单纯模型问题。当前 PMU 误差：

```text
l1d.load_misses  +79.1%
l1d.store_misses +72.3%
l2.misses        +86.5%
llc.load_misses  +1200.0%
snp              -22.5%
```

这说明 W14 的 d-side feature generator 本身对这个访问模式存在系统偏差。继续用偏差很大的 backend 输出训练模型，会污染模型输入。

建议单独做 W14 backend debug：

1. 对比 timing-functional backend 与 mem_events oracle 的 per-address / per-core miss 分布。
2. 检查 private L1/L2 capacity、store miss、write-allocate、coherence invalidation 处理。
3. 针对 W14 增加小规模 regression case，避免修一个负载破坏 W11/W12/W13/H01-H03。

验收：

```text
W14 l1d.load/store/l2 miss error 收敛到 < 25-30%；
其他负载 PMU 不明显退化。
```

### P4：补 store-miss 建模和验证

H02 与 W15 的 store miss 误差很大：

```text
H02 l1d.store_misses -82.1%
W15 l1d.store_misses -99.7%
```

这说明当前 load-side 行为相对稳定，但 store-side 行为不够稳。需要单独检查：

```text
store address stream
store hit/miss classification
write allocate policy
store/load alias
dirty eviction
ownership transition
```

这一步优先级低于 CPI，但如果目标包括 PMU 对齐，必须做。

### P5：构建更合理的训练/验证矩阵

当前 W11-W15 同时承担训练覆盖和验证评估，H01-H03 已经暴露出泛化不足。下一版建议：

```text
train:
  W11-W15 + 新增若干非 holdout 混合负载

validation:
  W11-W15 split val
  H01-H03 holdout fixed

do-not-train:
  H01-H03 暂时保留为泛化测试，不进入训练集
```

同时把 validation report 固化为两张表：

```text
seen-like validation: W11-W15
unseen holdout validation: H01-H03
```

否则训练集指标改善可能只是记住训练负载形态，不能证明泛化提升。

### P6：训练路线

推荐执行顺序：

1. P0 诊断字段接入。
2. P1 fetch-tail loss 接入，先用当前 50M split 小训 smoke。
3. 用 latest best ckpt 的同一验证脚本跑 W11-W15 + H01-H03。
4. 若 H01/H02/W13/W14 CPI 明显改善，再正式 8 卡训练。
5. 并行推进 P3/P4 backend PMU 修复，但不要在 backend 未稳定时混入大规模重训。

第一阶段验收目标：

```text
W12/W15/H03 CPI err 继续保持 < 15%
W13/W14/H01/H02 CPI err 从 43-54% 降到 < 25-30%
W11 CPI err 从 -76% 先降到 < 45%
H01/H02/H03 l1d.load_misses 继续保持 < 5%
W14 PMU 不继续恶化
GPU infer 吞吐不低于当前同规模结果太多
```

第二阶段验收目标：

```text
W13/W14/H01/H02 CPI err < 20%
W11 CPI err < 30%
W14 l1d/l2 PMU error < 25-30%
H02/W15 store miss error 显著收敛
```
