# pre-v16 方案设计：tail query + per-core local summary token

日期：2026-07-02

状态：实施稿。v16 目标是在 v15 的 per-core delta/rank 思路基础上，修复 `segment query` 带来的跨核可见性问题，同时保留每个 core 的局部指令范围锚点。

## 1. 背景

v15 使用 `segment` query：

```text
<C0_BEGIN> C0_uops <QUERY_C0> <C0_END>
<C1_BEGIN> C1_uops <QUERY_C1> <C1_END>
...
```

由于 Qwen 是 causal decoder，早序号 query 只能看到左侧上下文：

```text
QUERY_C0 只能看到 global + C0
QUERY_C1 只能看到 global + C0 + C1
```

这对 `W_ads_ranking_proxy` 的本核快慢识别有帮助，但会削弱需要全局 phase 判断的负载。当前 c08 seedB 上 `W_phased_mix` 从 v9_tq 的 `0.64%` 误差退化到 v15 的 `40.10%`，运行曲线显示前期正常，后期高估 CPI 并通过 planner 形成窗口偏移闭环。

## 2. v16 核心结构

v16 改为 `tail_local` placement：

```text
<SYS> cfg <TRACE> global_tokens

<C0_BEGIN> C0_summary C0_uops <LOCAL_C0> <C0_END>
<C1_BEGIN> C1_summary C1_uops <LOCAL_C1> <C1_END>
...

<TRACE_END>
<QUERY_C0> <QUERY_C1> ... <QUERY_CN>
```

语义分工：

- `<LOCAL_Ci>` 是真实 token，进入 backbone attention。它位于本 core 段尾部，负责形成本核局部摘要。
- `<QUERY_Ci>` 仍在全序列末尾，能看到所有 core 段、所有 local token 和 global tokens，负责跨核比较与全局 phase。
- head 输入使用 `hidden(QUERY_Ci) + local_proj(hidden(LOCAL_Ci)) + side_proj(side_feats_i) + tstart_proj(t_start_i)`。

`local_proj` 是零初始化线性层。它不是 side tensor；输入是经过 Transformer 后的 token hidden。零初始化保证训练初始行为接近原 tail-query 路径。

## 3. 本版代码改动

- `model/tokenizer.py`
  - 新增 `<LOCAL_C0> ... <LOCAL_C31>` special tokens。
  - 新增 legacy token 顺序 helper，用于从 v9-v15 checkpoint 按 token 名称迁移旧 special-token embedding。

- `data/build_windows.py`
  - `--query-placement` 新增 `tail_local`。
  - `tail_local` 在每个 core 段末尾插入 `<LOCAL_Ci>`，并保留 tail queries。
  - TQ budget overhead 计入每核一个 local token。

- `train/dataset.py`
  - cache schema 升为 `feat_version=16`。
  - 从 tokens 解析 `local_pos`，tensor cache 持久化 `local_pos`。
  - 旧样本没有 `<LOCAL_Ci>` 时，`local_pos` 回退到 `query_pos`。

- `model/llm_wrapper.py`
  - forward 新增 `local_pos`。
  - gather `hidden(<LOCAL_Ci>)`，通过零初始化 `local_proj` 融合到 query hidden。

- `train/train_lora.py`
  - DDP 训练传入 `local_pos`。
  - checkpoint 保存/恢复 `local_proj`。
  - 从旧 ckpt 初始化时按 token 名称迁移旧 special-token embedding，避免新增 LOCAL token 导致行号错位。

- `eval/eval_quota_cycles.py`
  - 在线 eval 支持 `--query-placement tail_local`。
  - planner token budget 计入 local token。

## 4. 训练数据路径

推荐新路径：

```text
data/windows_v16_v9core_tail_local_c01/
data/windows_v16_v9core_tail_local_c04/
data/windows_v16_v9core_tail_local_c08/
data/windows_v16_v9core_tail_local_c16/
data/windows_v16_v9core_tail_local_all/
```

合并训练集：

```text
data/windows_v16_v9core_tail_local_all/windows.jsonl
data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache/
```

## 5. 构建命令

```bash
nohup env CLEAN=1 JOBS=17 bash scripts/build_v16_v9core_tail_local_train600.sh > logs/build_v16_v9core_tail_local_train600.nohup.log 2>&1 &
```

## 6. 验证重点

v16 不应只看 aggregate CPI，还要固定比较：

```text
W_phased_mix:
  是否从 v15 的 40.10% 回到 v9_tq 级别

W_ads_ranking_proxy:
  是否保持 v15 对 v9_tq 的改善
  per-core pred_label_corr / slowest hit rate 是否不退化

全 17 workloads:
  mean / median / max CPI error
  是否出现新的 high-CPI 或 phase workload 退化
```

## 7. 后续分支

如果 v16 修复 `phased_mix` 但 `ads_ranking_proxy` 退回：

- 保留 `tail_local`。
- 继续补 functional gather/queue pressure side features。
- rank/spread loss 改成按 label spread 连续 gating。

如果 v16 仍然高估 `phased_mix`：

- 降低或关闭 rank/spread，先验证 local token 本身。
- 对比 `tail_local + direct cpi head`、`tail_local + delta only`、`tail_local + delta + gated rank/spread`。
