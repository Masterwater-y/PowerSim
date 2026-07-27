# v29 最新结果分析与 Frozen Residual Probe 实验方案

更新时间：2026-07-24

状态：

- 最新 long-history E1 结果已完成审查；
- frozen residual probe 的 opt-in 代码和配置已加入；
- 原 v29、现有 global-residual 路径默认行为保持不变；
- 正式 8-GPU、10k probe 已完成，单次运行、无重启；
- validation best 为 step9500，`val_total=0.4740878503`；
- step0 等价性与冻结 checkpoint 审计已通过；
- step500 checkpoint 审计确认 194 个 E0 state key 逐 tensor 完全不变，optimizer
  只包含 8 个 correction parameter tensor。

关联文档：

- `docs/v29_memory_gated_timing_heads_design.md`
- `docs/v29_long_history_improvement_and_60k_training.md`

## 1. 当前结论

当前结果同时说明了两件事：

1. 40 维 strict-prefix long-history 特征包含可部署的 memory timing 信号；
2. 把 long-history residual 广播到所有 token、再送入共享 QKVR 主干的 E1 路由
   会污染非访存 timing 和 branch，不能作为最终方案。

因此下一步不是继续扩大 E1，而是先执行 E2 的 frozen residual probe：

```text
exact E0 best checkpoint
    |
    +-- frozen StaticEncoder / QKVR / BaseTimingHead / BranchHead
    |
    +-- trainable MemoryHistoryProjection
    |
    +-- trainable MemoryCorrectionHead

final_logit =
    E0_base_logit
    + memory_mask * signed_history_correction
```

这个实验只回答：

> 在完全不允许 E0 主干和原输出头变化的前提下，long-history 特征能否改善
> memory timing？

它是信息量和路由的诊断，不替代后续严格配对的联合训练。

## 2. 最新结果摘要

比较对象：

- E0：`ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt`，step 59,000；
- E1：`ckpt/tcsim_v29_long_history_100m_8gpu_60000/last.pt`，step 60,000。

以下数值依次为 C4/C8/C16/C32，均为相对误差，越低越好。

| 指标 | E0 | E1 long-history last |
|---|---:|---:|
| 全 workload macro ROI mean | 5.08/4.62/4.45/5.27% | 7.11/7.92/7.17/8.41% |
| train/base | 2.48/2.09/2.01/2.38% | 4.60/5.74/4.79/6.86% |
| heldout | 11.02/10.42/10.02/11.88% | 12.84/12.91/12.60/11.96% |
| Redis heldout | 45.44/43.04/41.10/39.72% | 40.64/39.18/36.41/34.22% |
| SIMD SSE dense | 5.40/4.92/5.53/5.54% | 35.45/49.73/26.57/25.69% |
| heldout branch count MAPE | 83.63/76.27/71.63/66.70% | 172.97/176.06/174.45/164.58% |

结果不是覆盖缺失导致：最新评测覆盖 184/184 traces、约 2.293B ROI UOP，
没有 trace failure。

### 2.1 正向证据

- Redis 在所有核数上稳定改善约 3.9 到 5.5 个百分点；
- Flink heldout、memory random 和部分 memory sequential case 也改善；
- rollout seed0/seed1 的总体误差非常接近，收益和退化不是单个 trace seed 噪声。

这些结果证明 long-history summary 不是完全无效。

### 2.2 负向证据

- SIMD、Marine、coherence read-mostly、GoFeed 出现大幅回归；
- base signed bias 从 E0 的轻微低估转为 E1 的整体高估，并随核数增加；
- branch prediction 严重退化；
- Redis 改善后仍有 34% 到 41% 误差。

E1 的问题不是单纯容量不足，而是 memory-specific context 被注入共享 token state：

```text
hidden =
    hidden
    + long_history_adapter(history).unsqueeze(1)
```

因此每个 compute、SIMD、branch 和 memory token 都会改变。只读 adapter on/off
诊断也确认：

- 当前窗口没有 memory token 的 SIMD 样本，非访存 gap 仍会改变；
- memory workload 的非访存 gap 也会改变；
- branch probability 会改变。

同一 E1 checkpoint 的 adapter-off 不是 E0，因为共享主干已共同适配；该诊断只证明
直接跨路由影响存在，不把全部回归都归因于 adapter 的瞬时输出。

## 3. 当前实验方案的归因缺陷

当前 E0/E1 报告只能作方向性比较，不能作严格单变量因果结论。

### 3.1 last 对 best

E1 报告使用 60k `last.pt`，但 E1 最佳 validation 出现在 step 43k：

```text
best val_total = 0.5029477536 @ 43k
last val_total = 0.5321142340 @ 60k
```

E0 报告使用的是 `best.pt@59k`。必须用同一规则比较 E0 best、E1 best 和 probe best。

### 3.2 sampler 不一致

- E0：trace-balanced replacement sampler；
- E1：coverage-first，再进入 trace-balanced replacement。

Probe 使用 E1 已验证的 long-history manifest 和 coverage-first sampler，但 probe 的
结论只与其 step0 E0 初始化输出及后续 correction 变化比较。后续正式 E0/E1/E2
矩阵必须统一 sampler。

### 3.3 evaluator 版本不一致

最新 E1 报告使用 canonical retirement-gap 和新版 batched context builder；旧 E0
报告不是完全相同代码。最终判断前必须在当前串行 evaluator 下重跑 E0 best。

### 3.4 初始化未配对

E1 的 global adapter 在部分原模块之前构造，即使最后一层零初始化，也会消耗 RNG，
导致后续共享层和 head 的初始化改变。Frozen probe 不依赖随机配对：它直接加载 E0
的所有原参数，只允许新增 correction 参数缺失。

### 3.5 seed 不是独立训练复现

现有 seed0/seed1 是 rollout trace seed，不是两个独立训练 RNG seed。E2 通过初步
验收后，至少需要两个训练 seed。

### 3.6 诊断不足

现有 E1 正式报告缺少：

- best checkpoint 的 free-running 结果；
- memory-off；
- correction/feature/residual 分桶；
- feature JS divergence；
- targeted cursor/head drift；
- 独立训练 seed。

此外普通 `WINmean%` 会被接近零的真实 interval 放大到数千甚至数万百分比。窗口指标
应以 UOP-weighted WAPE、p90 和 ROI endpoint 为主。

## 4. Timing 标签合同修正

`docs/v29_memory_gated_timing_heads_design.md` 第 3 节当前把所有 gap 写成相邻 commit
tick 之差，并要求窗口首 UOP 读取上一条 UOP。这与现有 v29 common-time target
不一致。

正确合同为：

```text
d0 =
    (commit_tick[cursor] - state_time_tick)
    / tick_per_cycle

di =
    (commit_tick[cursor + i] - commit_tick[cursor + i - 1])
    / tick_per_cycle
    , i > 0
```

`d0` 是从当前公共时间到下一次 commit 的剩余时间，而不是完整的上一 commit gap。
否则在 no-commit/head-age 状态中会重复充值已经流逝的 stall 时间。

Frozen probe 沿用现有 `dataset.py` 和 loss，不改变标签，因此实际实验使用的是正确
common-time target。后续应单独修订原设计文档的公式。

## 5. Frozen Residual Probe 精确定义

### 5.1 模型公式

```text
base_logit[i] =
    FrozenE0GapHead(token_state[i])

correction_logit[i] =
    MemoryCorrectionHead(
        token_state[i],
        LongHistoryProjection(history[core(i)])
    )

memory_mask[i] =
    valid[i]
    AND mem_kind[i] in {load, store, atomic}

gap[i] =
    softplus(
        base_logit[i]
        + memory_mask[i] * correction_logit[i],
        beta=4
    )

commit_time =
    FP64_cumsum(gap)
```

`serialize` 的 `mem_kind=4` 不属于 memory gate。

### 5.2 冻结范围

冻结并保持 eval mode：

- StaticTokenEncoder；
- dynamic/side projections；
- full-QKVR interaction layers；
- final normalization；
- 原 `gap_head`；
- `branch_head`。

仅训练：

- `memory_history_projection`；
- `memory_correction_head`。

正式 100M 模型参数：

| 项目 | 数量 |
|---|---:|
| 总参数 | 112,351,173 |
| trainable correction 参数 | 144,849 |
| trainable 占比 | 0.129% |

冻结主干使用 eval mode 是必要条件。只设置 `requires_grad=False` 但保留
`model.train()` 会继续启用 backbone dropout，破坏 E0 精确基线。

### 5.3 初始化合同

Fresh probe 必须通过：

```text
--init-checkpoint ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt
```

`--init-checkpoint` 与 `--resume` 不同：

| 行为 | `--init-checkpoint` | `--resume` |
|---|---|---|
| 加载 E0 原模型权重 | 是 | 加载 probe 自身权重 |
| 加载 optimizer | 否 | 是 |
| 恢复 step/history/best | 否，step 从 0 开始 | 是 |
| 允许缺失新 correction state | 仅允许这 8 个 state key | 否，strict |
| cache contract | 只允许新增 long-history contract | 必须完全一致 |

分布式 fresh init 只由 rank0 读取 1.3 GiB E0 checkpoint，随后由 DDP 构造广播全部
参数，避免 8 个 rank 同时加载包含旧 optimizer 的大 checkpoint。

### 5.4 初始等价性

Correction 最后一层权重和 bias 都为 0。因此 step0 必须满足：

```text
probe normal == probe memory-off == E0
```

精确检查对象：

- `retirement_gap`；
- `commit_time`；
- `branch_miss_logit`；
- `branch_miss_probability`。

新增配置在训练前执行 step0 validation，并把它写入 `metrics.json`。这样同一
sampler、同一 long-history manifest、同一 loss 下的 E0 初始指标成为 probe 的直接
内部基线。

### 5.5 训练目标

不构造：

```text
memory_label = true_total - predicted_base
```

Correction 直接通过最终真实输出接受现有 v29 loss：

- commit-time log loss；
- prefix BCE；
- progress count；
- cumulative drift；
- branch loss 保留在 total 中，但 branch 路径冻结，对 correction 没有梯度。

Correction 是 signed logit correction，可以增加或降低默认 memory gap；最终
softplus 保证 gap 非负。

## 6. 实验配置

配置：

```text
configs/v29_frozen_memory_probe.yaml
```

关键设置：

| 参数 | 值 |
|---|---:|
| manifest | `data/v29_long_history_dataset/manifest.json` |
| E0 init | `ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt` |
| train RNG seed | 1234 |
| sampler | coverage-first trace-balanced |
| learning rate | 3e-4 |
| weight decay | 1e-2 |
| AMP | BF16 |
| validation | step0，之后每 500 step |
| checkpoint | 每 500 step |
| pilot/formal target | 10,000 step |
| GPU/DDP | 8 GPU / 8 rank |

统一推理验证入口：

```bash
# 默认：完整 seed1 + heldout 验证（92 + 28 = 120 traces）
bash scripts/launch_v29_frozen_probe_inference_validation.sh

# 可选：targeted C4/C32 哨兵集
bash scripts/launch_v29_frozen_probe_inference_validation.sh targeted
```

入口脚本内部使用 `nohup` 后台启动，启动后立即返回；主日志和 PID 分别写入
输出目录下的 `launch.nohup.log` 与 `launch.pid`。默认 full 进度可用以下命令查看：

```bash
tail -f logs/v29_frozen_probe_best9500_full_s256_8gpu/launch.nohup.log
```

默认输出：

```text
ckpt/tcsim_v29_frozen_memory_probe_e2_10k_seed1234/
logs/tcsim_v29_frozen_memory_probe_e2_10k_seed1234_watch.current.log
logs/watchdog/tcsim_v29_frozen_memory_probe_e2_10k_seed1234_watch.nohup.log
```

旧模式不受影响：

- `long_history_dim=0`：原 v29；
- `long_history_dim>0` 且未指定 `long_history_mode`：保持现有
  `global_residual`；
- 只有显式设置
  `long_history_mode=memory_gated_timing_correction` 才进入新路径。

## 7. 运行阶段与停止规则

### 阶段 A：step0

必须满足：

- E0 所有 state key 成功加载；
- 仅 8 个新增 correction state key 缺失；
- trainable 参数为 144,849；
- memory-off 与 E0 输出相等；
- step0 validation 被记录。

任一不满足立即停止。

### 阶段 B：0 到 1k

检查：

- 无 NaN、无 monotonic violation；
- correction 正负比例不是永久单边饱和；
- `corr_abs` 没有快速爆炸；
- validation 不显著劣于 step0；
- throughput 和预计时间合理。

如 step500/1k validation 已持续恶化，优先停止并检查 LR、loss exposure 和
memory-token attribution。

### 阶段 C：1k 到 10k

选择 validation best，不使用 last 代替 best。训练结束后先做小型 targeted
free-running：

- Redis heldout C4/C32；
- SIMD SSE dense C4/C32；
- Marine heldout；
- coherence read-mostly；
- Flink heldout；
- memory random/sequential。

只有 targeted 通过，才运行完整 seed1 + heldout 120-trace 矩阵。

## 8. 验收标准

### 8.1 硬不变量

- memory-off 与 E0 在同一 context 上一致；
- correction 不直接改变非 memory token gap；
- correction 不改变 branch probability；
- E0 原参数训练前后逐 key 不变；
- checkpoint 只含 correction optimizer state。

### 8.2 初步通过

- validation best 优于 step0；
- Redis C4/C32 明显改善；
- SIMD、Marine、coherence 哨兵不出现 E1 式大幅回归；
- correction 同时存在合理的正、负修正；
- best 明显优于 last 时只使用 best。

### 8.3 判定失败

- Redis 无改善或只在训练 split 改善；
- correction 只学习 workload identity；
- correction 在少数 memory token 上产生极端 logit；
- free-running 的 local validation 改善不能转化为 ROI endpoint 改善；
- memory-off 不再复现 E0。

Probe 失败意味着当前 long-history summary、token attribution 或 loss exposure 不足，
不应该通过解冻主干来掩盖。

## 9. 预计耗时

历史完整 E1 训练：

```text
60,000 step / 8 GPU = 37,293.6 s = 10 h 21 min 34 s
```

Frozen probe 仍需完整 backbone forward，但不做 112M 主干参数的 backward 和梯度
同步，只训练约 0.145M 参数。正式 checkpoint 也不再保存原 112M 参数的 AdamW
optimizer state，因此写盘量显著降低。

### 9.1 训练实测

正式运行已经得到：

```text
step0 val_total = 0.48318147265
step500 val_total = 0.47936207889
step1000 val_total = 0.47892462353
step9500 val_total = 0.47408785030  <- best
step10000 val_total = 0.47462768039
step10000 elapsed   = 2399.27 s
```

step0 与 E0 checkpoint 保存的 `best_validation=0.48318147902` 只差约
`6.4e-9`，验证了初始化等价性。step9500 相对 step0 改善约 1.88%；branch
validation 保持不变。训练总耗时约 39 分 59 秒。

step500 checkpoint 的逐 key 审计结果：

```text
E0 frozen state keys       = 194
E0 frozen tensor mismatch  = 0
new correction state keys  = 8
optimizer parameter tensors = 8
optimizer state tensors     = 8
```

### 9.2 Free-running 实测口径

历史 184-trace 基线评测不是让每张卡依次跑全部 traces，而是：

```text
184 traces
    -> num_shards=8
    -> 每张 GPU 一个独立 worker
    -> 每个 worker 约 23 traces
    -> 最后 merge 8 个 worker report
```

本次 frozen-probe 脚本默认不再包含 `seed0_inference`，只评测
`deployment_inference`（seed1，92 traces）和 `development_heldout`
（seed0 heldout，28 traces），共 120 traces；8 卡下平均每卡约 15 traces。

历史完整运行的真实墙钟时间：

| 运行 | 开始 | 完成 | 墙钟时间 |
|---|---:|---:|---:|
| E0 packed3，184 traces | 16:37:30 | 17:51:20 | 1 小时 13 分 50 秒 |
| E1 long-history，184 traces | 11:40:15 | 12:40:04 | 59 分 49 秒 |

因此之前把完整 8 卡评测估计为 8 到 12 小时是错误的：该估计混用了单 GPU
串行口径和各 trace `elapsed_s` 的求和，没有考虑 8 个 trace shard 同时运行。

修正后的预计时间：

| 阶段 | 预计墙钟时间 |
|---|---:|
| targeted C4/C32 哨兵集，8 卡 | 约 10 到 25 分钟 |
| 完整 seed1 + heldout 120-trace free-running，8 卡 | 约 40 到 70 分钟 |
| 同时做 E0 与 E2 的 120-trace 公平对照 | 约 1.5 到 2.5 小时 |

## 10. Probe 之后

如果 frozen probe 成功：

1. 用完全相同 sampler、初始化和 evaluator 做 E0/E1/E2 配对；
2. 至少增加一个独立训练 RNG seed；
3. 优先保持 Base/branch 冻结，或只用低 LR 解冻 timing trunk；
4. 加入 non-memory/branch anchoring；
5. auxiliary E3 只在 oracle 标签可由 functional 输入预测时尝试。

如果 Redis 仍高于约 30%：

- 增加机制平衡数据和 large-stall/signed-endpoint 目标；
- 检查 hard memory-token gate 是否遗漏 memory backpressure 的 consumer/dependency
  暴露位置；
- 考虑可部署的 cache/TLB/MSHR/coherence replay/state；
- 不再把更多全局 history residual 广播到共享 token。
