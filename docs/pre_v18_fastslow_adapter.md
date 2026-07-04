# pre-v18 方案设计：强 fast/slow 监督 + cross-core adapter

日期：2026-07-03

状态：实施稿。v18 接在 v17 `v17_bc_split_heads_nophase` 之后，目标不是继续改数据切窗，而是在现有 per-core label 上让模型必须学习核间 CPI residual、快慢核排序和高 spread 窗口的极值核。

## 1. 结论：本轮不需要重新采数据

本轮只改模型结构和 loss：

- 新增可选 `CoreAdapter`，输入仍是 `query_hidden [B,C,D]`。
- 新增 `L_slowest` / `L_fastest`，标签由现有 `label[..., cpi_uop]` 在线计算。
- 提高 `L_delta` / `L_rank` / `L_spread` 权重。
- 继续使用现有 `side_feats`、`t_start_rel`、`uops`、`label`。

因此可以直接复用：

```text
data/windows_v17_bc_split_heads_nophase_all/windows.jsonl
data/windows_v17_bc_split_heads_nophase_all/windows.maxlen32768.tensor_cache
```

只有以下情况才需要重新 build windows/cache 或重新采 raw trace：

- 新增 gather/queue/MSHR/LLC pressure side feature。
- 修改 `SIDE_FEATURE_KEYS` 或 `label_keys`。
- 修改 raw trace schema 或 PMU label 口径。
- 引入新的 workload/seed/core-count 数据。

本轮不做这些，所以不重新采数据。

## 2. v17 失败点

v17 已实现：

- `cpi_head_mode=delta`
- `L_delta`
- `L_rank`
- `L_spread`
- window-level `L_cycles`
- split PMU heads

但 `W_ads_ranking_proxy` c08 seedB 仍低估：

```text
v16: pred_vs_label = 34.09%
v17: pred_vs_label = 31.82%
```

原因判断：

- `base + delta` 中 base 太容易学，delta 仍容易塌缩。
- `L_rank/L_spread` 权重过小，实际只是轻量 regularizer。
- `L_cycles` 约束窗口总 cycles，不强制哪个 core 慢。
- 当前 head 对每核独立输出，但没有显式 core-core 比较模块。

## 3. 模型改动

### 3.1 Cross-core adapter

在融合 local/side/tstart 后、PMU head 前插入 mask-aware adapter：

```text
query_hidden
+ local_proj(local_hidden)
+ side_proj(side_feats)
+ tstart_proj(t_start)
-> CoreAdapter
-> PMURegressionHead
```

实现位置：

```text
model/llm_wrapper.py
```

当前实现为 residual self-attention over cores：

- 支持 `core_mask`。
- 参数不依赖 core 数。
- attention out projection 和 FFN final projection 零初始化。
- 启用时初始近似 identity，便于从 v17 checkpoint 热启动。

默认 v18 参数：

```text
core_adapter_layers = 2
core_adapter_heads = 8
core_adapter_ff_mult = 2
core_adapter_dropout = 0.05
```

### 3.2 快慢核极值监督

新增：

```text
L_slowest
L_fastest
```

标签从现有 per-core CPI 计算：

```text
label_log_cpi_i = log(label_cpi_i)
slowest_label = argmax_i(label_log_cpi_i)
fastest_label = argmin_i(label_log_cpi_i)
```

只在 high-spread 窗口启用：

```text
active_core_count > 1
std(label_log_cpi) > spread_min_std
```

loss 直接作用在预测的 `pred_log_cpi_i` 上：

```text
L_slowest = CE(pred_log_cpi / tau, slowest_label)
L_fastest = CE(-pred_log_cpi / tau, fastest_label)
```

这不是新增一个独立分类头，而是直接让 CPI 输出承担快慢核排序责任。

## 4. Loss 默认参数

v18 默认比 v17 明显提高 fast/slow 相关项：

```text
lambda_delta = 2.0
lambda_cycles_window = 0.5
lambda_rank = 0.15
lambda_spread = 0.10
lambda_slowest = 0.05
lambda_fastest = 0.05
rank_gap = 0.08
rank_tau = 0.10
spread_min_std = 0.03
spread_ref = 0.10
spread_weight_max = 5.0
```

v18 训练脚本默认从零训练，保证与 v17 做干净对比。若只想快速验证代码路径或做增量 ablation，可显式设置 `WARM_START_V17=1` 从 v17 step_008000 热启动；这种模式默认使用 `--reset-loss-state`，避免继承 v17 已经学到的过强 `log_var_cpi/log_var_cycles`，否则新 fast/slow loss 仍可能被主 CPI/cycles 项压住。

## 5. 已实施文件

```text
model/llm_wrapper.py
  CoreAdapterBlock / CoreAdapter
  WrapperConfig core_adapter_* 参数

train/loss.py
  lambda_slowest / lambda_fastest
  L_slowest / L_fastest / slowest_acc / fastest_acc

train/train_lora.py
  core_adapter_* CLI 参数
  lambda_slowest / lambda_fastest CLI 参数
  --reset-loss-state
  checkpoint 保存/加载 core_adapter 与新 loss 参数

eval/eval_quota_cycles.py
  从 checkpoint 元数据恢复 core_adapter 配置并加载权重

scripts/run_v18_fastslow_adapter_qwen3_0p6b.sh
  v18 一键训练脚本
```

## 6. 运行命令

从零训练：

```bash
./scripts/run_v18_fastslow_adapter_qwen3_0p6b.sh
```

默认输出：

```text
ckpt/v18_fastslow_adapter_nophase_8gpu_8000
```

如果要从 v17 step_008000 热启动做增量 ablation：

```bash
WARM_START_V17=1 ./scripts/run_v18_fastslow_adapter_qwen3_0p6b.sh
```

如果要继承 v17 的 loss uncertainty 权重：

```bash
WARM_START_V17=1 RESET_LOSS_STATE=0 ./scripts/run_v18_fastslow_adapter_qwen3_0p6b.sh
```

不建议默认这么做。

## 7. 验收指标

不要只看 `val_loss`。v18 必须固定看：

```text
W_ads_ranking_proxy c08/c16:
  pred_vs_label_cpi_uop
  win_mape_cpi_uop

per-core diagnostics:
  pred_core_cv / label_core_cv
  pred_label_corr
  slowest_core_hit_rate
  fastest_core_hit_rate
  pred_true_start_err_mean_abs
```

最低预期：

- `W_ads_ranking_proxy` c08 明显优于 v17 的 `31.82%`。
- `pred_core_cv / label_core_cv` 明显上升。
- `slowest_core_hit_rate` 明显高于随机基线。
- 普通 low-spread workload 不因 fast/slow loss 明显退化。
