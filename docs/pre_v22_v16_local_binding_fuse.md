# v22: v16 Baseline + Local Binding Fuse

状态：实施稿。目标是在 v16 `tail_local` 基线上，只加强每核 query 与本核
`LOCAL_Ci` 的绑定，不改变训练数据切窗、不改变部署侧 planner。

## 1. 背景

v16 的有效结构是：

```text
<SYS> cfg <TRACE> global_tokens
<C0_BEGIN> C0_summary C0_uops <LOCAL_C0> <C0_END>
...
<TRACE_END>
<QUERY_C0> <QUERY_C1> ...
```

模型 head 目前使用：

```text
h_i = hidden(QUERY_Ci)
h_i += local_proj(hidden(LOCAL_Ci))
h_i += side_proj(side_feats_i)
h_i += tstart_proj(t_start_i)
```

问题在于所有 `QUERY_Ci` 都位于序列尾部，看见几乎相同的全局前缀，容易学成
相近的全局窗口表示。`LOCAL_Ci` 已经存在，但 v16 的 `local_proj` 零初始化，
训练早期本核局部路径很弱，head 可以继续依赖平均 CPI。

## 2. 本版目标

保留 v16 的全局上下文可见性，同时让每个核的 head 输入显式绑定本核局部摘要：

```text
q_i = hidden(QUERY_Ci)     # tail query, sees global/cross-core context
l_i = hidden(LOCAL_Ci)     # segment tail, anchored to this core sequence

h_i = q_i
    + alpha * LN(l_i)
    + MLP([LN(q_i), LN(l_i), LN(q_i)-LN(l_i), LN(q_i)*LN(l_i)])
    + side_proj(side_feats_i)
    + tstart_proj(t_start_i)
```

其中：

- `alpha` 初值为 `0.10`，保证训练一开始就有非零本核局部信号。
- MLP 最后一层零初始化，使新增残差初始为 0，避免一上来扰动过大。
- 旧 v16 模式保留为 `local_fuse_mode=add`，新模式为
  `local_fuse_mode=bind_concat`。

## 3. 是否需要重建数据集

不需要。

原因：

- v16 `tail_local` windows 已经包含 `<LOCAL_Ci>` token。
- `WindowDataset` cache 已经保存 `local_pos`，训练 batch 已经返回
  `query_pos`、`local_pos`、`side_feats`、`t_start`。
- 本版只改变 `hidden(QUERY_Ci)` 与 `hidden(LOCAL_Ci)` 的融合方式，不需要新的
  token 序列或新的 label。

需要重建数据集的情况只有一种：如果后续给 core 段内所有 token 注入
`core_slot_id` / per-token core embedding，就需要 cache 额外保存每个 token
属于哪个 core。当前实施不做这一步。

## 4. 兼容性

默认模式继续是 v16 旧行为：

```text
local_fuse_mode = add
```

这样旧 checkpoint 没有 `local_fuse_mode` 字段时，eval 自动按 v16 旧路径加载，
不会因为新增模块破坏已有 v16/v17 结果复现。

新训练脚本显式设置：

```text
--local-fuse-mode bind_concat
```

checkpoint 保存 `local_fuse_mode` 和 `local_bind_fuse` 权重；eval 先读取
`head_best.pt` 中的模式再构建模型。

## 5. 本版暂不改变的项

- 不改 windows/jsonl/cache。
- 不改 label schema。
- 不引入 local-core 架构。
- 不默认继承 v17 的 nophase 训练集。
- 不加重 rank/spread/fast-slowest loss。
- 不改部署侧 pred/label/tq_forward 切窗逻辑。

当前更新后的训练口径：

- CPI 使用 direct head，不再使用 `base + zero-mean delta`。
- Head 拆成三路 MLP：`cpi_uop`、`branch_miss`、cache misses。
- `PMU_KEYS` 从 8 维改为 7 维，移除 `dtlb_miss`。
- `rank` / `spread` 不再进入 loss。

这样做的原因是本轮目标已经从“验证 local binding”进一步收窄为：
每个 core 的 CPI 先直接学准，辅助 PMU 只通过独立 head 提供弱共享 backbone 信号，
不再让 delta/rank/spread 这类相对形状目标干扰 CPI。

## 6. 验证口径

训练后至少跑：

```text
c04 / c08 / c16 / c32 full eval
```

重点看：

- `pred vs label` per-core CPI，而不只平均 CPI。
- adsproxy / feed_ranking / graph_recall_proxy 等慢快核明显负载。
- hidden 诊断：`cos(QUERY_i, LOCAL_i)` 是否高于 `cos(QUERY_i, LOCAL_j)`。
- pred core-CPI CV 是否接近 label core-CPI CV。
