# v21 local-core direct CPI + cycles + light PMU

日期：2026-07-04

状态：实施稿。v21 是在 v20 和 CPI-only scratch 诊断之后的收敛方案：保留
`local_core` 表示和 direct CPI head，不再使用 delta/rank/spread/slowest/fastest
这类显式快慢核 loss，只保留每核 CPI 绝对误差、窗口 cycles 误差和很小的 PMU
辅助误差。

## 1. 诊断结论

CPI-only direct scratch 暴露了两个问题：

- hidden probe 显示每核 hidden/side 特征本身有信息量，线性 probe 能明显区分快慢核。
- 但训练出的 head 仍然会把 per-core CPI 拉向中间，c8 全量评估不如 v17；在
  `W_search_index_proxy`、`W_phased_mix` 上，即使用真实时间切窗，模型 CPI
  scale 仍然明显错误。

`tq_forward` 真实时间切窗诊断说明：

- 对 `compute_int`、`branch_storm`、`stream`、`chase_dram`、`ads_ranking_proxy`，
  free-running 的大误差很大一部分来自预测 CPI 引起的时间漂移。
- 对 `search_index_proxy` 和 `phased_mix`，主要问题是模型本身 CPI scale 错，
  不是切窗实现错。

所以 v21 的目标不是继续堆 spread/rank loss，而是让训练目标与部署闭环一致：
每核 CPI 要准，窗口总 cycles 也要准，PMU 只作为弱辅助特征监督。

## 2. Head 选择

使用 direct head：

```text
pred_log_cpi_i = head(h_i)
```

不再使用 delta head：

```text
pred_log_cpi_i = base(window) + zero_mean_delta_i
```

原因是 delta head 很容易学到一个还可以的窗口 base，然后把 delta 压到接近 0；
这正是“所有核心 CPI 都差不多”的退化路径。direct head 更直接地要求每个 core
hidden 自己解释该 core 的 CPI。

## 3. Loss

固定权重，不使用 uncertainty weighting：

```text
L =
  1.00 * L_cpi_abs
+ 1.00 * L_cycles_window
+ 0.05 * L_aux_pmu
```

显式关闭：

```text
lambda_delta   = 0.0
lambda_rank    = 0.0
lambda_spread  = 0.0
lambda_slowest = 0.0
lambda_fastest = 0.0
lambda_inv     = 0.0
lambda_phys    = 0.0
```

说明：

- `L_cpi_abs` 是主目标，逐核监督 `log(cpi_uop)`。
- `L_cycles_window` 不是新的快慢核启发式；它是用同一个 CPI 预测乘以每核 uops
  后约束窗口总 cycles，直接对应 online planner 的时间推进。
- `L_aux_pmu=0.05` 只做弱辅助，帮助 side/backbone 对 branch/cache/mem 语义保持
  可辨，不允许它主导训练。
- PMU 权重固定，不自动学习。当前阶段自动权重容易在早期把目标尺度带偏。

## 4. Optimizer

采用比 CPI-only scratch 更低的学习率，避免 step 500 后验证退化：

```text
lr_lora = 3e-5
lr_head = 1e-4
lr_emb  = 1e-4
```

其他默认：

```text
model_input_mode      = local_core
cpi_head_mode         = direct
core_adapter_layers   = 2
core_adapter_heads    = 8
core_adapter_ff_mult  = 2
core_adapter_dropout  = 0.05
use_tstart            = true
loss_weight_mode      = fixed
```

## 5. 训练命令

推荐从头训练 12000 step，watchdog 自动续跑：

```bash
cd /data00/yinhaolang/LLMSim
bash scripts/launch_v21_direct_cycles_aux_12k_watchdog.sh
tail -f logs/v21_direct_cycles_aux_scratch_8gpu_12000_watch.current.log
```

前台运行同一配置：

```bash
cd /data00/yinhaolang/LLMSim
bash scripts/run_v21_local_core_direct_cycles_aux_qwen3_0p6b.sh
```

默认输出：

```text
ckpt/v21_local_core_direct_cycles_aux_scratch_8gpu_12000
logs/v21_direct_cycles_aux_scratch_8gpu_12000_watch.current.log
```

## 6. 验收指标

不要只看总 loss。训练中优先看：

```text
L_cpi_abs
L_cycles_window
pred_core_cv / label_core_cv
per-core CPI MAPE
pred-vs-ROI CPI error
online alignment aggregate relerr
```

早期判断：

- 如果 `L_cpi_abs` 降但 `pred_core_cv / label_core_cv` 仍长期低于约 `0.6`，说明 head
  仍在压缩核间差异。
- 如果 `L_cycles_window` 不降，online 时间推进仍会漂。
- 如果验证最优点很早出现并持续恶化，优先降学习率或缩短训练，不回到 spread/rank。
