# TAO 全局 Schema

本文件定义当前项目里两套必须明确区分的模型输入输出契约，作为跨 `taogen`、`tao_train`、`infer` 三阶段的统一口径：

- `生产基线 schema`
  - 以当前最新完整训练并已评估的 ckpt 为准。
  - 当前锚定 ckpt：`MTAO/ckpt/tao_v10_3_ma16.best.pt`
- `vNext schema`
  - 以当前工作区 `tao_train/ml` 代码为准。
  - 包含最近加入、但尚未形成完整生产 ckpt 基线的 branch-mispred 设计。

适用范围：
- 训练数据读取：`tao_train/ml/dataset.py`
- 训练模型与 loss：`tao_train/ml/model.py`
- 离线评估：`tao_train/ml/eval.py`、`tao_train/ml/eval_ddp.py`
- strict 推理：`infer/ml/infer.py`
- 端到端 driver：`infer/driver/inference_driver.py`
- functional / label 边界：`infer/functional_trace/schema.py`

如果其他文档与本文件冲突，以本文件、对应 ckpt 实测结果和对应代码实现为准。

---

## 1. Schema 层级

### 1.1 生产基线 schema

当前项目里“可部署、已完成训练、已跑过全量验证”的主基线是：

- ckpt 路径：`MTAO/ckpt/tao_v10_3_ma16.best.pt`
- 对应完整训练状态：`MTAO/ckpt/tao_v10_3_ma16.status.json`
- 训练完成度：`step=50000`、`total_steps=50000`、`finished=true`
- best checkpoint 对应步数：`train_step=45000`

从 ckpt `state_dict` 实测可知，这个生产基线的 `I_SIDE` embedding 实际包含 12 个键：

- `i_path_class`
- `i_coh_oracle`
- `i_mesi_before`
- `i_group_head`
- `i_group_pos`
- `i_mshr_depth`
- `itlb_hit`
- `i_walker_levels`
- `i_walker_dram_misses`
- `i_bank_id`
- `i_llc_set_residency`
- `i_llc_set_lru_pos`

并且：

- 不含 `i_group_bkt`
- 不含 `i_oracle_source`

### 1.2 vNext schema

当前工作区 `tao_train/ml` 代码已经引入下一代 branch-mispred 设计：

- `mispred_mask`
- branch-control 有效位口径
- branch-only 统计与评估输出

这套 vNext 设计已经体现在当前源码里，但尚未对应到一个“完整训练完成的生产基线 ckpt”。

后文会明确标注哪些部分是：

- `共同边界`
- `生产基线`
- `vNext`

### 1.3 共同输入边界

当前项目存在三种相关但不同的输入边界：

1. `records.micro` / 训练 parquet
   - 含 functional 字段、ref_sim d/i-side 字段、窗口派生特征、训练 label。
   - 供 `tao_train` 训练和 `eval.py`/`eval_ddp.py` 使用。

2. `functional.core<N>.parquet`
   - 仅含部署侧真实可见的 A 子集。
   - 不含 ref_sim 输出，不含训练 label。
   - 供部署侧 driver 和验证侧输入拼接使用。

3. `build_inference_input.py` 产出的 strict jsonl
   - 在 `functional` 基础上补入 ref_sim d/i-side 与窗口特征。
   - 供 `infer/ml/infer.py` 单步推理使用。

共同点上，模型仍然是一个以 `micro-op` 为样本粒度的多任务模型：
- 输入：单个锚点 `micro-op` 及其左侧 `context_len` 历史窗口。
- 输出：`fetch_lat`、`exec_lat`、`mispred_logit`、`head_logit`。

其中需要特别区分：

- `生产基线`
  - 已知前向接口包含 `mispred_logit`
  - 但不应把它误解释为“已经按 branch-control 口径重训完成的 head”
- `vNext`
  - `mispred` 语义已收紧为 branch-control 有效位，只在
    `is_branch && (is_last_microop || !is_microop)` 的行上训练、评估、输出和计数。

---

## 2. 模型输入边界

### 2.1 训练 / 验证 parquet 输入

训练与 `eval.py` / `eval_ddp.py` 消费的是 `taogen` 产出的 parquet 数据集。当前真实读取列由
`tao_train/ml/dataset.py` 中的 `FEATURE_COLS`、`LABEL_COLS`、`ID_COLS` 决定。

#### 标识列

- `core_id`
- `thread_id`
- `pos_in_thread`

说明：
- 样本窗口按同一 `(core_id, thread_id)` 的顺序流构造。
- 锚点靠近 thread 起点时，左侧历史窗口用 pad 填充，`attn_mask=0`。

#### 原始 functional 列

布尔类：
- `is_load`
- `is_store`
- `is_atomic`
- `is_branch`
- `is_branch_cond`
- `is_branch_indirect`
- `is_call`
- `is_return`
- `is_int`
- `is_fp`
- `is_simd`
- `is_serialize`
- `is_microop`
- `is_last_microop`

小整数类：
- `n_src`
- `n_dst`
- `size`

依赖摘要：
- `d0` ~ `d3`
- `pc0` ~ `pc3`

地址类：
- `vaddr`
- `paddr`
- `cacheline_addr`
- `cacheline_paddr`
- `macro_pc`
- `micro_pc`

说明：
- `macro_pc` 和 `micro_pc` 只在 dataloader 内用于派生相对结构特征，不直接作为模型输入身份特征。
- `cacheline_paddr` 在训练侧允许兼容旧数据回退到 `cacheline_addr`；推理 strict 输入不允许缺失。

#### ref_sim D-side 列

- `mesi_before`
- `coh_oracle`
- `sharer_bucket`
- `owner_dist`
- `dirty_owner`
- `path_class`
- `inval_fanout`
- `same_line_recent`
- `oracle_source`
- `d_mshr_depth`
- `dtlb_hit`
- `d_walker_levels`
- `d_walker_dram_misses`
- `d_bank_id`
- `d_llc_set_residency`
- `d_llc_set_lru_pos`

#### ref_sim I-side 列

- `i_path_class`
- `i_coh_oracle`
- `i_mesi_before`
- `i_oracle_source`
- `i_mshr_depth`
- `itlb_hit`
- `i_walker_levels`
- `i_walker_dram_misses`
- `i_bank_id`
- `i_llc_set_residency`
- `i_llc_set_lru_pos`

说明：
- 当前训练代码仍会读取 `i_oracle_source`，但模型前向会通过 `_FEAT_EXCLUDE_FOR_MODEL=('i_oracle_source', 'macro_pc')`
  将其排除在真正送模的特征之外。
- `i_group_head` / `i_group_pos` 不在 parquet 原始列中，而是在 dataloader 中在线派生后送模。
- `i_group_bkt` 不属于当前训练生效 schema。

#### 窗口派生列

短窗口：
- `mem_density_W64`
- `branch_density_W64`
- `unique_cl_W64`
- `cl_reuse_dist_log`
- `pc_freq_W64`
- `time_since_last_branch_log`
- `bank_conflict_W64`

长窗口：
- `unique_cl_W256`
- `unique_cl_W1024`
- `dram_bank_id`
- `dram_bank_freq_W256`
- `dram_row_freq_W256`

### 2.2 dataloader 在线派生特征

以下字段不要求在 parquet 中物化，但会在 dataloader 中在线构造，并最终送入模型：

- `is_macro_head`
  - 当前 `micro-op` 是否为当前动态 macro 的首条。

- `uop_pos_in_macro`
  - 当前 `micro-op` 在当前动态 macro 内的 0-based 位置。

- `vaddr_bucket`
- `paddr_bucket`
- `cline_bucket`
- `cline_p_bucket`
  - 由地址类列做 hash bucket 离散化得到。
- `i_group_head`
- `i_group_pos`
  - 由 `macro_pc` 的 cacheline 边界在线派生，表示 fetch-group 起点与组内相对位置。

### 2.3 训练 / 验证 label

当前训练 label 只有四项：

- `fetch_latency`
- `execution_latency`
- `mispredicted`
- `is_fetch_group_head`

说明：
- `fetch_latency` 和 `execution_latency` 默认在 dataset 内做 `log1p`。
- `fetch_latency` 是 zero-inflated 目标，只在 `is_fetch_group_head=1` 时有物理意义。
- `mispredicted` 是 branch miss 事件标签，但在训练 / 验证 / 推理统计时只在 branch-control 有效位上参与。

### 2.4 vNext: branch-control 有效位

当前工作区 `tao_train/ml` 代码对 `mispred` 的 vNext 口径是：

```text
mispred_valid = is_branch && (is_last_microop || !is_microop)
```

含义：
- `mispred` 按 `micro-op` 流建模，但语义上代表 branch-control 事件。
- 对于被拆成多个 micro-op 的 branch macro，只在最后一个 control micro-op 上参与监督与计数。
- 非 branch 行或中间 micro-op 行：
  - 不参与 `mispred` loss
  - 不参与 `mispred` 评估指标
  - 推理输出时 `mispred_prob=0`、`mispred_hard=0`

这条规则当前适用于工作区中的新代码路径：
- 训练：`tao_train/ml/model.py`
- 单卡评估：`tao_train/ml/eval.py`
- 多卡评估：`tao_train/ml/eval_ddp.py`
- strict 推理：`infer/ml/infer.py`
- 生产 driver：`infer/driver/inference_driver.py`

但它**不应自动回推**为：

- `tao_v10_3_ma16.best.pt` 已经按这套口径完成训练

对当前生产基线 ckpt，应只把 branch-control 口径视为：

- 新评估脚本 / 新推理脚本的解释层
- 不是旧 ckpt 的训练语义声明

---

## 3. 部署侧 functional 输入边界

`infer/functional_trace/schema.py` 定义了部署侧唯一可见的 strict functional 子集。

### 3.1 functional.core 列

标识列：
- `core_id`
- `thread_id`
- `micro_seq`
- `seq_num`

静态 / functional 列：
- `macro_pc`
- `micro_pc`
- `vaddr`
- `paddr`
- `cacheline_addr`
- `cacheline_paddr`
- `size`
- `is_load`
- `is_store`
- `is_atomic`
- `is_branch`
- `is_branch_cond`
- `is_branch_indirect`
- `is_call`
- `is_return`
- `is_int`
- `is_fp`
- `is_simd`
- `is_serialize`
- `is_microop`
- `is_last_microop`
- `n_src`
- `n_dst`
- `producer_dists`
- `producer_classes`

说明：
- 这是部署侧真正允许看到的输入边界。
- `records.micro` 中 detailed-only 的微架构 oracle 列不得直接进入部署 driver。

### 3.2 labels.core 列

`labels.core<N>.parquet` 只用于验证，不属于部署输入。

字段：
- `core_id`
- `thread_id`
- `micro_seq`
- `fetch_tick`
- `issue_tick`
- `complete_tick`
- `commit_tick`
- `ready_tick`
- `ready_source`
- `mispredicted`

说明：
- 这是 trace-level 验证真值。
- 它和训练 parquet 中的 `fetch_latency` / `execution_latency` / `is_fetch_group_head` 不是同一层级的 label 表达。
- 不应把 `labels.core` 和训练 parquet label 混为一张 schema 表。

---

## 4. strict 推理输入边界

`infer/ml/infer.py` 不直接吃 parquet，而是消费 `build_inference_input.py` 产出的 strict jsonl。

单条 strict 输入由两部分组成：

1. `meta`
   - 至少包含 `core_id`、`thread_id`、`micro_seq`
   - 可带 `workload`

2. `input`
   - functional A 子集
   - ref_sim D-side / I-side 字段
   - 窗口派生特征

说明：
- strict 推理要求 `cacheline_paddr` 必须存在。
- strict 推理当前仍保留对旧 checkpoint 的兼容补丁，但这不改变输入 schema 本身。

---

## 5. 模型真实输入特征族

当前模型由 6 个特征族组成，每族嵌入到 `d_feat=64`，拼接后投影到 `d_model=256`。

### Family 1: OPCODE_LIKE

布尔键：
- `is_load`
- `is_store`
- `is_atomic`
- `is_branch`
- `is_branch_cond`
- `is_branch_indirect`
- `is_call`
- `is_return`
- `is_int`
- `is_fp`
- `is_simd`
- `is_serialize`
- `is_microop`
- `is_last_microop`
- `is_macro_head`

小整数键：
- `n_src`
- `n_dst`
- `size`
- `uop_pos_in_macro`

### Family 2: REGISTER_DEP

- `d0` ~ `d3`
- `pc0` ~ `pc3`

### Family 3: MEM_COH

小整数键：
- `mesi_before`
- `coh_oracle`
- `sharer_bucket`
- `owner_dist`
- `dirty_owner`
- `path_class`
- `inval_fanout`
- `same_line_recent`
- `oracle_source`
- `d_mshr_depth`
- `dtlb_hit`
- `d_walker_levels`
- `d_walker_dram_misses`
- `d_bank_id`
- `d_llc_set_residency`
- `d_llc_set_lru_pos`

地址桶：
- `vaddr_bucket`
- `paddr_bucket`
- `cline_bucket`
- `cline_p_bucket`

### Family 4: I_SIDE

- `i_path_class`
- `i_coh_oracle`
- `i_mesi_before`
- `i_group_head`
- `i_group_pos`
- `i_mshr_depth`
- `itlb_hit`
- `i_walker_levels`
- `i_walker_dram_misses`
- `i_bank_id`
- `i_llc_set_residency`
- `i_llc_set_lru_pos`

说明：
- **生产基线**和**当前 vNext 代码**在 `I_SIDE` 这件事上目前一致：
  - 都包含 `i_group_head` 与 `i_group_pos`
  - 都不包含 `i_group_bkt`
  - 都不包含 `i_oracle_source`
- 因此当前 production ckpt 的有效 I-side 送模字段总数是 12 个：
  - `i_path_class`
  - `i_coh_oracle`
  - `i_mesi_before`
  - `i_group_head`
  - `i_group_pos`
  - `i_mshr_depth`
  - `itlb_hit`
  - `i_walker_levels`
  - `i_walker_dram_misses`
  - `i_bank_id`
  - `i_llc_set_residency`
  - `i_llc_set_lru_pos`

### Family 5: CtxWindow

- `mem_density_W64`
- `branch_density_W64`
- `unique_cl_W64`
- `pc_freq_W64`
- `bank_conflict_W64`
- `cl_reuse_dist_log`
- `time_since_last_branch_log`

### Family 6: DramFeats

- `unique_cl_W256`
- `unique_cl_W1024`
- `dram_bank_id`
- `dram_bank_freq_W256`
- `dram_row_freq_W256`

---

## 6. 模型输出边界

当前模型前向输出四个主头：

- `fetch_lat`
  - 非负实数，表示 `log1p(fetch_latency_cycles)` 的预测值。

- `exec_lat`
  - 非负实数，表示 `log1p(execution_latency_cycles)` 的预测值。

- `mispred_logit`
  - `mispred` 二分类 logit。
  - 在 `vNext` 解释层中，再结合 `mispred_valid` 做 branch-control 口径统计。

- `head_logit`
  - `is_fetch_group_head` 的二分类 logit。

### 6.1 推理阶段对外输出

`infer/ml/infer.py` 当前对外写出的 jsonl 字段为：

- `workload`
- `core_id`
- `thread_id`
- `micro_seq`
- `fetch_lat`
  - 已从 `log1p` 逆变换回 cycles。
- `exec_lat`
  - 已从 `log1p` 逆变换回 cycles。
- `mispred_valid`
- `mispred_prob_raw`
- `mispred_prob`
- `mispred_hard`

说明：
- `mispred_prob_raw` 是未经 branch-control mask 屏蔽的 sigmoid 输出。
- `mispred_prob` / `mispred_hard` 才是可用于统计 branch miss 的正式输出。
- 当前 strict 推理不会单独输出 `head_logit`；`head` 主要用于训练与评估。

### 6.2 评估阶段输出

`eval.py` / `eval_ddp.py` 当前会输出：

- 基本损失：
  - `loss`
  - `mse_fetch`
  - `mse_exec`
  - `bce_mispred`
  - `bce_head`

- 基本指标：
  - `acc_mispred`
  - `acc_head`
  - `mae_fetch_log`
  - `mae_exec_log`

- branch-control 口径 mispred 指标：
  - `branch_mispred_eval_count`
  - `true_branch_mispred_count`
  - `pred_branch_mispred_count`
  - `branch_mispred_count_abs_error`
  - `branch_mispred_count_rel_error`
  - `mispred_precision`
  - `mispred_recall`
  - `mispred_f1`

说明：
- `acc_mispred`、`precision`、`recall`、`f1` 全部只在 `mispred_valid` 子集上计算。
- 当前不再推荐使用“全样本 mispred accuracy”作为判断依据。

---

## 7. loss 契约

### fetch latency

`fetch_latency` 使用 zero-inflated / hurdle 风格设计：

- `head_logit` 负责预测当前样本是否是 fetch group head。
- `fetch_lat` 负责预测正分支幅度。
- loss 包含两部分：
  - 在 `head=1` 子集上的 `mse_fetch`
  - `sigmoid(head_logit) * fetch_lat` 与目标之间的一致性项 `mse_fetch_cons`

### execution latency

- `exec_lat` 直接做回归损失 `mse_exec`。

### mispred

这里必须区分两代语义：

- `生产基线`
  - 暴露 `mispred_logit` 前向输出
  - 但 `tao_v10_3_ma16.best.pt` 不应被文档误标成“branch-control 口径重训完成的 ckpt”
  - 在没有独立旧版训练代码快照的情况下，本文件只把它定义为：
    - 当前可部署的 legacy mispred 头
    - 其输入 embedding 口径由 ckpt 实测确定

- `vNext`
  - `mispred_logit` 只在 `mispred_valid=1` 的子集上做 BCE / focal BCE
  - `mispred_pos_weight` 只在 branch-control 有效位子集上估计

因此：

- 当前 workspace 代码的训练语义是 branch-control 版
- 当前 production ckpt 的部署语义是 legacy ckpt，经由新推理 / 评估脚本解释

### head

- `head_logit` 对 `is_fetch_group_head` 做带 `pos_weight` 的 BCE。

---

## 8. Checkpoint 契约

当前真实 checkpoint 格式来自 `tao_train/ml/train.py` 的 `collect_state()`，字段如下：

```python
{
  "model": model_state_dict,
  "optim": optimizer_state_dict,
  "sched": scheduler_state_dict,
  "step": int,
  "ema_loss": float,
  "best_loss": float,
  "cfg": TaoConfig.__dict__,
  "args": vars(args),
  "rng": {
    "torch_cpu": ...,
    "numpy": ...,
    "python": ...,
  },
}
```

说明：
- 这才是当前真实生效的 ckpt 契约。
- 旧文档中写成 `model_state/meta/config` 的格式已不准确。
- `infer/ml/infer.py` 和 `tao_train/ml/eval.py` / `eval_ddp.py` 都按 `ck["model"]` 和 `ck["cfg"]` 加载。

### 8.1 当前生产基线 checkpoint

当前用于统一 schema 的生产基线 checkpoint 是：

- [tao_v10_3_ma16.best.pt](train/ckpt/tao_v10_3_ma16.best.pt)

对应状态：

- [tao_v10_3_ma16.status.json](train/ckpt/tao_v10_3_ma16.status.json)
  - `step=50000`
  - `total_steps=50000`
  - `finished=true`
- [tao_v10_3_ma16.best.full_val.ddp.eval.json](train/ckpt/tao_v10_3_ma16.best.full_val.ddp.eval.json)
  - `train_step=45000`

说明：

- 后续若出现新的“完整训练完成 + 正式验收”的 ckpt，应更新本节锚点。
- `smoke_bs*` 目录下的 300-step smoke ckpt 不属于生产基线。

---

## 9. 当前统一规则

为了让整个项目保持一致，后续新增代码应遵守以下规则：

1. functional 边界只认 `infer/functional_trace/schema.py` 的 `FUNCTIONAL_TRACE_COLS`
   - 不允许把 detailed-only label 或 ref_sim oracle 直接混入部署输入。

2. 训练 parquet 与 deploy functional 是两套不同 schema
   - 训练 parquet 可以包含 ref_sim D/I-side 与训练 label。
   - deploy functional 只能包含 A 子集。

3. `mispred` 需要区分生产基线与 vNext
   - `生产基线`：
     - 以 `tao_v10_3_ma16.best.pt` 的真实 embedding 和真实部署行为为准
     - 不应被误写成“已按 branch-control 口径完成训练”
   - `vNext`：
     - 统一按 branch-control 有效位处理
     - 统一公式：
       `is_branch && (is_last_microop || !is_microop)`

4. `macro_pc` 只能用于 dataloader 派生相对结构特征
   - 不能恢复绝对 PC id 类 shortcut 特征。

5. 当前生产基线与当前 vNext 代码的 I-side 共同口径包含 `i_group_head` / `i_group_pos`
   - 但不包含 `i_group_bkt`，因为它属于由绝对 PC 派生的身份桶特征。
   - 若未来要彻底移除 `i_group_head` / `i_group_pos`，必须把这件事视为一次新 schema 版本升级，而不是当前现实。

6. checkpoint 统一按真实 `collect_state()` 格式读写
   - 新工具不应再假设 `model_state/meta/config` 旧格式。

7. strict 推理当前带有历史兼容层
   - `infer/ml/model.py` 仍保留 `i_group_bkt` 相关兼容代码。
   - 当 ckpt 缺失 `i_group_bkt` embedding 时，`infer/ml/infer.py` 会把对应权重 zero-pad。
   - 因此“当前能跑起来的 strict 推理 schema”与“当前 production ckpt 的真实训练 schema”不完全相同。
   - 对现有 ckpt 做正确解释时，应以 ckpt 实际包含的 embedding 和训练侧 `tao_train/ml/model.py` 为准。

## 10. vNext 设计备注

本节只描述下一代模型设计，不代表当前生产基线已经完成训练。

### 10.1 目标

下一代模型的目标是把 `mispred` 从“legacy 分类头”明确收紧为：

- branch-control 事件头
- 只在 branch-control 有效位上监督
- 更适合 branch-miss 数量统计与 branch-only precision/recall/F1 评估

### 10.2 vNext 关键变化

- 新增 `mispred_mask`
- `mispred_valid = is_branch && (is_last_microop || !is_microop)`
- 训练时仅在 `mispred_mask=1` 子集上计算 `bce_mispred`
- 评估时输出：
  - `branch_mispred_eval_count`
  - `true_branch_mispred_count`
  - `pred_branch_mispred_count`
  - `branch_mispred_count_abs_error`
  - `branch_mispred_count_rel_error`
  - `mispred_precision`
  - `mispred_recall`
  - `mispred_f1`
- 推理时输出：
  - `mispred_valid`
  - `mispred_prob_raw`
  - `mispred_prob`
  - `mispred_hard`

### 10.3 vNext 与生产基线的关系

- `tao_train/ml` 当前源码已经包含 vNext branch-mispred 逻辑。
- 但当前生产锚点 ckpt 仍然是 [tao_v10_3_ma16.best.pt](train/ckpt/tao_v10_3_ma16.best.pt)。
- 只有当新的 branch-mispred 版本完成一次正式全量训练并产出验收 ckpt 后，才能把“生产基线 schema”切换到 vNext。

---

## 11. Source Of Truth

当前 schema 的最终来源文件如下：

- functional / labels 边界：
  - `infer/functional_trace/schema.py`

- 训练 parquet 输入与 dataloader 派生：
  - `tao_train/ml/dataset.py`

- 模型结构、特征族、loss：
  - `tao_train/ml/model.py`

- checkpoint 真实保存格式：
  - `tao_train/ml/train.py`

- strict 推理输出：
  - `infer/ml/infer.py`

- 单卡 / 多卡评估输出：
  - `tao_train/ml/eval.py`
  - `tao_train/ml/eval_ddp.py`

如果将来修改上述任一文件里的输入输出字段、label 语义或 ckpt 格式，必须同步更新本文件。
