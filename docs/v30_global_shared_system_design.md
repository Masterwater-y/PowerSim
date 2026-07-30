# TCSim v30：v29 模型骨架、显式 Global Shared-System 与 Branch Replay

状态：旧 ready-time rollout 已停止使用；commit-cycle serial-exact 与无中途全局同步的 parallel-relaxed 两套部署合同、持久 deadline、per-core starvation guard 已实现；需要重建 commit-clock cache 并重新训练后验证
日期：2026-07-29
适用范围：v30 训练与推理、serial free-running、单 trace 多 GPU 窗口并行、
后续多微架构训练

## 0. 执行结论

v30 定义为：

```text
v30
= v29 model skeleton
+ explicit Global Shared-System
+ configured standalone branch-predictor replay input
```

Global Shared-System，简称 GSS，由 functional memory stream 驱动，维护每条
trace 独立、跨核心共享的微架构状态，并向模型提供 cache-residency、替换压力和
shared-system 竞争信息。

该机制的定位是：

- 对齐 gem5 的地址映射、容量、组相联、bank 和替换策略；
- 在部署时只依赖 functional trace、目标微架构配置和模型自己的历史预测；
- 显式处理超过 256/512 UOP 窗口的长期 cache 历史；
- 帮助 Redis heldout、工作集变化、核心数变化和 cache geometry 泛化；
- 不把完整 gem5/Ruby 事件系统带入推理；
- 不声称从 commit-only trace 恢复 cycle-exact cache、MSHR 或一致性瞬态。

推荐的第一阶段范围是 D-side L1D、private L2 和 shared LLC tag/replacement
状态。第一阶段不实现真实 MSHR、TLB page walk、DRAM queue、NoC 和 MESI transient
state。

v30 保留 v29 的共享主干和单 timing head。Cache proxy 先作为 memory-token
条件输入进入主干，由 attention 将其影响传播到依赖指令；不重新引入独立且可被解释为
物理 memory latency 的 MemoryHead。

分支侧采用相同的“可确定机制显式计算、不可确定 timing 交给模型”原则。正式 v30
删除 neural `branch_head` 及其 branch BCE/count loss，以独立、配置驱动的 correct-path
branch-predictor replay 产生逐分支结果，并在第一个 full-QKVR block 前注入对应 branch
token。部署 branch PMU 也直接统计 replay 结果，不再使用 neural probability。输入只能是
functional trace 驱动的 replay 输出，严禁把 gem5 `mispredicted` 标签送入模型。

理论上，GSS/branch replay 提供了 v29 当前窗口无法唯一恢复的长期机制状态，因此 v30
的可达到误差上限优于只使用 v29 特征的模型；但这不等于第一版训练结果必然更好。
有限数据下的 feature shortcut、GSS proxy 误差、真实时间训练与预测时间推理的状态偏移，
都可能造成实际退化。第一版必须把 timing 路径构造成最佳 v29 的严格超集，并通过分项
消融和 serial free-running 验证收益。

### 0.1 v30 的正式版本边界

v30 不是 v29 的普通特征增量，也不命名为 v29.1。原因是它引入了跨窗口、跨核心、
跨推理 step 持久化的权威状态，改变了数据 schema、训练输入构造、推理 resume 和
多 GPU 协调合同。

v29 保留的模型骨架：

- global-time 多核联合窗口；
- full-QKVR Transformer 主干；
- per-UOP retirement gap cycle；
- monotonic prefix/global-time scheduler；
- core-slot permutation-equivariant 输入输出；
- 原 single timing head 和主要 timing/progress loss。

这里的“v29 模型骨架”不包含 neural `branch_head`。当前 v29 的 `branch_head` 与
`gap_head` 是两个并列输出，branch probability 没有进入 timing 路径；继续保留它只会
引入精度较差的辅助梯度和重复的部署预测器。v30 正式移除：

- `branch_head`、`branch_miss_logit` 和 `branch_miss_probability`；
- branch-token BCE/Brier loss；
- neural branch-count loss；
- 使用 neural probability 累计部署 branch PMU 的路径。

v30 新增的 Global Shared-System：

- 训练期真实时间戳全局 memory-event replay；
- 每条 trace 唯一的 canonical shared-system state；
- 每核 private L1D/L2 与跨核 shared LLC；
- gem5-aligned mapping、LRU/TreePLRU 和参数化 uarch profile；
- transactional shadow preview/delta；
- committed-prefix replay、rollback 和 resume；
- v30 state schema、order-policy version 和训练 sidecar；
- 后续可扩展的 coherence/TLB/MSHR/NoC/DRAM shared-state 接口。

第一阶段 GSS 只实现 cache 子系统，不等于第一版就实现完整 shared uncore。

Branch replay 不属于 GSS 的跨核共享状态。它是每核/每线程按 committed functional
program order 递推的 predictor state，不依赖模型预测时间，也不需要 canonical shared
state、transactional shadow 或 GPU lane rollback。它可以在训练和推理前确定性地计算为
逐 branch sidecar；GSS 则仍需按真实或预测的跨核事件顺序维护共享 cache state。

### 0.2 版本与兼容性

必须独立版本化：

```text
dataset_schema = tcsim-v30-...
feature_contract = v30-global-shared-system-...
state_schema = v30-gss-cache-state-...
branch_replay_contract = v30-configured-branch-replay-...
checkpoint_model_version = v30
```

v29 checkpoint 可以用于参数初始化或冻结结构 probe，但不能直接加载后假装成 v30
checkpoint。缺少 GSS contract/state metadata 时必须硬失败，不能静默退回 v29
输入语义。

启用 branch replay 输入的 v30 checkpoint 还必须校验 predictor family、predictor
configuration hash、replay implementation version、functional trace hash 和 cold/warmup
policy。未实现的 predictor family 必须硬失败，不能静默退回 neural head。当前已完整验证
的是 `TournamentBP + SimpleBTB + ReturnAddrStack + SimpleIndirectPredictor`；标准 TAGE
仍属于后续实现范围。完整 replay 语义见
[Standalone Branch Predictor Replay 设计](branch_predictor_replay_design.md)。

### 0.3 2026-07-28 G1 实施门禁结果

本轮没有修改 v29 权重，也没有启动 v30 训练。已完成：

- `tcsim/v30/gss.py`：cache-only reference engine；L1D 使用 LRU，private L2/shared
  LLC 使用 TreePLRU；所有模型可见状态均在当前访问修改状态前读取；
- `scripts/build_v30_gss_sidecar.py`：用 raw `ready_tick` 做跨核 teacher order，时间戳
  数值不写入模型输入；
- `scripts/audit_v30_gss_residual_signal.py`：冻结 canonical v29，通过 train lookup
  到 heldout 的解析 residual audit 判断增量信息；
- `scripts/bench_v30_gss_cache_engine.cc` 与
  `scripts/benchmark_v30_gss_throughput.py`：分别测状态机热路径与 GPU adapter 开销。

Redis C4/C8/C16/C32 的 base/heldout 共构造 8 条 sidecar：

| 项目 | 结果 |
|---|---:|
| memory events | 5,066,269 |
| memory-UOP physical-address coverage | 100% |
| all-UOP physical-address fraction | 约 5%（即访存 token 比例，不是地址缺失） |
| sidecar 大小 | 159.5 MiB |
| Python reference 构造速度 | 约 36.7 K event/s |

Python reference 速度只用于离线正确性构造，不能代表生产状态机吞吐。

冻结 v29 的 Redis-base -> Redis-heldout residual lookup 结果如下。`G1` 为 access-local
pre-state，`G2` 额外加入 lagged per-core LLC-miss EMA；负数表示比只使用原 v29 context
的 lookup 更好：

| 相对 UOP span | G1 相对 base | G2 相对 base | 解释 |
|---:|---:|---:|---|
| -64 | +0.05% | -3.65% | G2 能解释当前访问之前的误差，存在 shortcut 风险 |
| -32 | -0.19% | -3.31% | 同上 |
| -16 | +0.04% | -1.38% | G1 基本不影响过去残差 |
| 0 | -11.55% | -8.45% | 当前 memory gap 有强增量信号 |
| +16 | -2.70% | -3.22% | 正向增量信号 |
| +32 | -2.58% | -3.59% | 正向增量信号 |
| +64 | -2.58% | -2.06% | 正向增量信号 |
| +128 | -2.08% | -1.38% | 正向增量信号 |

G1/G2 在 heldout 的精确 lookup support 分别约为 92.7%/90.0%。结论是：

- **G1 access-only 通过无训练信息门禁**：它主要解释当前和后继残差，而不是过去；
- G1 在 C4/C8/C16/C32 的方向一致，但正向强度随核心数上升：`+32 UOP` 的相对改善
  约为 0.17%/0.86%/1.73%/3.67%；因此它更像 shared-cache/core-pressure 信号，C4
  收益很弱，后续 probe 不能只看 event-weighted C32 聚合；
- **G2 暂不进入第一轮训练**：当前 core-level EMA 带有明显的 workload/core residual
  shortcut，不能因为 aggregate MAE 更低就直接接入 FiLM；
- 下一步只做冻结 v29 + 零初始化 G1 causal residual adapter 小 probe；G1 probe
  通过非退化验收后，才决定最终从头训练时采用 pre-QKVR merge 还是保留 causal adapter；
- 这项 lookup audit 证明的是信息增量和方向性，不等于神经网络一定能实现相同收益。

完整结果：`logs/v30_gss_no_train_residual_redis_20260728/report.json`。

### 0.4 2026-07-28 P1 实施与运行状态

已构造正式 ready-clock teacher cache：

| 项目 | 结果 |
|---|---:|
| 唯一 trace | 108 |
| train manifest | 80/80 含 GSS |
| validation manifest | 64/64 含 GSS；与 train 使用相同 trace 的互斥 block split |
| development-heldout | 28/28 含 GSS |
| 核心数 | C1/C4/C8/C16/C32（heldout 为 C4-C32） |
| memory UOP | 53,761,726 |
| sidecar 大小 | 约 1.7 GiB |
| 16-worker wall time | 约 116 秒 |

数据入口为 `data/v30_gss_ready_dataset/manifest.json`，sidecar 位于
`data/v30_gss_ready_sidecars`。loader 强制验证 ready clock、pre-access、时间戳不可见、
字段/geometry/replacement contract 和窗口内 memory-UOP 对齐；训练 cache 中保留 G2
字段便于后续 audit，但 P1 模型只选择 G1 access-local 连续字段。

P1 代码路径为：

- canonical v29 59K best checkpoint 初始化；
- v29 static encoder、8 层 Full-QKVR interaction、timing head 和 branch head 全冻结并保持
  eval semantics；
- 只训练 307,416 参数的 `d_adapter=128`、4-head causal GSS residual adapter；输出层
  严禁 bias，保证没有 causal memory prefix 时残差恒为 0，不能退化成全局 timing 校准；
- Adapter 最终 projection 零初始化，实测 step 0 的 retirement gap、commit time 和 branch
  probability 与 canonical v29 最大绝对差均为 0；
- GSS 只进入 timing path，branch head 始终读取未调制的 frozen token，branch loss 权重为 0；
- 首步 22 个 Adapter parameter tensor 都在 autograd 图内，只有零初始化输出层具有非零
  梯度，主干无梯度；第二步开始上游 GSS encoder/QKV 获得有效梯度；
- 训练采用 coverage-first 无放回顺序。完整 coverage epoch 为 44,920 optimizer step；P1
  5K 仅覆盖约 11.1% sequence，用于机制门禁，不宣称完整训练收敛。

运行配置与入口分别为 `configs/v30_gss_p1_frozen_adapter.yaml`、
`scripts/run_v30_gss_p1_ddp8.sh` 和 `scripts/launch_v30_gss_p1_5k_watchdog.sh`。正式运行的
train/validation sequence 数为 359,354/34,457，验证固定抽取 1,024 条且逐 trace 均衡。
一次带 output bias 的预跑在 step 1,500 被主动终止并标记无效：该 bias 可以在没有 GSS
memory key 时学习 unconditional timing residual，属于归因 shortcut。下表仅保留为发现该
问题的无效预跑记录，不作为 P1 收益证据；正式无 bias P1 从 canonical v29 重新开始。

无效预跑 step 0 到 step 250 的结果为：

| 指标 | step 0 | step 250 | 相对变化 |
|---|---:|---:|---:|
| validation total | 0.476408 | 0.451457 | -5.24% |
| commit log MAE | 0.172849 | 0.168470 | -2.53% |
| progress MAE | 8.74745 | 8.54244 | -2.34% |

P1 是否通过须看正式无 bias 5K 最佳 checkpoint 的 development-heldout oracle-window
对照，尤其 Redis-heldout 与 memory-random 的改善，以及 compute/memory-seq 的非退化
门禁；随后仍需 online GSS 的 free-running rollout 才能给出部署结论。

### 0.5 P1 5K 最终结果

正式无 bias P1 完成 5,000 step，用时 1,436 秒（约 23 分 56 秒）。最佳 checkpoint 为
step 4,250；step 5,000 与最佳很接近，说明后半程主要是固定学习率下的平台震荡：

| validation 指标 | step 0 | best step 4,250 | 相对变化 | step 5,000 |
|---|---:|---:|---:|---:|
| total | 0.476408 | 0.429693 | -9.81% | 0.430241 |
| commit log MAE | 0.172849 | 0.163844 | -5.21% | 0.163883 |
| progress MAE | 8.74745 | 8.15839 | -6.73% | 8.14995 |

最佳 checkpoint：`ckpt/tcsim_v30_gss_p1_frozen_adapter_5k_seed1234/best.pt`。
训练后逐 tensor 复核：canonical v29 的 194 个 state tensor 全部 bit-exact 不变，P1
checkpoint 只多出 21 个 `gss_adapter.*` tensor，没有缺失或其他变化。

对最佳 checkpoint 使用同一个冻结 backbone、只开关 GSS Adapter，做了 ready-clock
teacher-order oracle-window 对照。development-heldout 覆盖 28 条 trace，每条时间均匀
抽取 128 个 window，共 3,584 个样本：

| weighting | commit log MAE | memory-token commit log MAE | progress MAE |
|---|---:|---:|---:|
| token weighted | -6.46% | -5.44% | -7.18% |
| trace equal | -4.63% | -4.26% | -5.15% |

Redis-heldout 的结果随核心数单调增强：

| 核心数 | commit log MAE | memory-token commit log MAE | progress MAE |
|---:|---:|---:|---:|
| C4 | -5.84% | -5.58% | -5.85% |
| C8 | -7.06% | -6.98% | -6.65% |
| C16 | -9.62% | -9.63% | -9.35% |
| C32 | -12.11% | -11.79% | -11.15% |

跨全部 heldout workload/core 的 trace-equal commit-log 改善也随核心数增强：C4/C8/C16/C32
分别约为 -1.37%/-2.99%/-5.68%/-8.22%。这与无训练 audit 的正向 span 和 core-scaling
结果一致，是 shared-cache/core-pressure 信号而非随机相关性的强证据。

但是逐负载非退化门禁没有通过：28 条 heldout 中 commit-log 改善 20 条、退化 8 条。
主要退化为 BVC-heldout（四个核心数聚合约 +3.20%）和 PyTorch-heldout（约 +1.65%）；
Gofeed/MySQL/Redis 的显著改善拉低了 aggregate，不能用 aggregate 掩盖这些回归。

validation oracle-window 评估实际覆盖 47 条有 eligible window 的 trace、3,008 个样本：

| weighting | commit log MAE | memory-token commit log MAE | progress MAE |
|---|---:|---:|---:|
| token weighted | -8.42% | -6.48% | -10.18% |
| trace equal | -6.03% | -4.51% | -7.74% |

关键非退化项如下：

- int-ALU 与 SIMD compute-only 路径严格 0 变化；日志中无 memory prefix 的 batch 也观测到
  `gss_delta_abs=0`，证明无 bias 约束生效；
- memory-seq：commit log -1.29%，progress -5.37%，但 memory-token commit log +0.19%，
  基本中性而非稳定改善；
- memory-random：commit log +0.37%，memory-token +1.08%，progress -0.11%，没有证明收益；
- coherence read-mostly 的唯一 eligible trace 明显退化：commit log +16.21%、memory-token
  +25.62%、progress +11.70%。当前 cache-only GSS 没有 coherence transient/owner/sharer
  状态，不能把这类负载的 proxy hit level 当成真实 service latency；
- BVC-base 聚合 commit log +5.46%，与 BVC-heldout 的退化方向一致。

因此 P1 的结论是：**G1 具有真实、可泛化且随核心数增强的增量信息，causal Adapter 能利用
它；但一个全负载共享的无约束 residual mapping 会把部分负载拉偏，不能直接升级为 v30
正式模型。** 下一步不应立即做 full rollout 或加入 G2；先做保留 memory-mask/访问位置的
mask-only control，以及对 Adapter 加小残差约束/保守 gate 的 P1b。只有 G1 明显优于
mask-only 且 BVC/coherence/memory-random 回归受控后，才进入 online canonical GSS
free-running rollout。

完整报告：`logs/v30_gss_p1_5k_development_oracle.json`、
`logs/v30_gss_p1_5k_validation_oracle.json`，并有对应 Markdown 文件。

### 0.6 mask-only、残差强度与 Gate-only 结论

为了区分 GSS 真正的 cache-state 内容、memory-event mask 几何和 residual 强度，完成了
三个逐步实验。

第一，正确的 mask-only control 删除全部 GSS categorical/continuous 内容，只保留因果可得
的 memory-event 位置、前缀 ordinal、前缀 density 和距上一个 memory event 的距离；模型
容量与 G1 Adapter 相同。完整训练 5,000 step 后，最佳 step 2,750 的 heldout trace-equal
commit log 仅改善 -0.07%，token-weighted 仅改善 -0.12%。因此 P1 的收益不是“见到一个
memory mask 就校准 timing”的 shortcut，主要信息来自真实 GSS cache-state 内容。

第二，对 P1 step 4,250 做无训练 residual scale sweep：

| residual scale | trace-equal commit log | token-weighted commit log | 改善 trace |
|---:|---:|---:|---:|
| 0.25 | -1.97% | -2.72% | 23/28 |
| 0.50 | -3.45% | -4.61% | 22/28 |
| 0.75 | -4.37% | -5.76% | 20/28 |
| 1.00 | -4.63% | -6.46% | 20/28 |

固定小强度更稳，固定大强度 aggregate 更好，但没有一个全局 scale 同时满足两者。这证明
下一步 gate 首先需要解决的是“GSS residual 在当前上下文应保留多大强度”，而不是把 GSS
硬限制到当前 memory UOP。GSS 会通过 retirement prefix、ILP/MLP 暴露影响后续非访存
UOP，因此 gate 必须逐 UOP 作用于所有有效 token。

Gate-only probe 保持 v29 和已训练 G1 Adapter 全冻结，只训练 135,623 个 gate 参数：

```text
h_out[i] = h_v29[i] + gate[i] * delta_gss[i]

gate[i] = 1 - 0.75 * sigmoid(score[i])
0.25 <= gate[i] <= 1.00
```

`score[i]` 只读取冻结的 v29 token、冻结的 GSS residual、当前 memory mask、因果前缀 memory
density 和 residual RMS；不读取 workload ID、trace ID、真实时间戳或标签。训练增加两项低
权重约束：逐 token 相对冻结 v29 的 regret 为 0.25 权重，局部 retirement-gap 辅助监督为
0.10 权重。配置和入口为 `configs/v30_gss_gate_only_2k.yaml`、
`scripts/run_v30_gss_gate_only_ddp8.sh` 和
`scripts/launch_v30_gss_gate_only_2k_watchdog.sh`。

8 卡 2,000 step 用时 587 秒（约 9 分 47 秒），一次完成且 watchdog 无重启；step 2,000
同时是最佳 checkpoint：

| validation 指标 | step 0（gate=0.95） | best step 2,000 | 相对变化 |
|---|---:|---:|---:|
| total | 0.429280 | 0.427429 | -0.43% |
| commit log MAE | 0.163543 | 0.162584 | -0.59% |
| progress MAE | 8.15005 | 8.11276 | -0.46% |

训练后逐 tensor 复核：来源 P1 checkpoint 的 215 个 tensor 全部 bit-exact 不变，只新增
14 个 `gss_exposure_gate.*` tensor。正式 development-heldout 四路对照结果为：

| 方案 | trace-equal commit | token-weighted commit | memory commit（trace equal） | progress（trace equal） | 改善 trace |
|---|---:|---:|---:|---:|---:|
| fixed scale 0.25 | -1.97% | -2.72% | -1.83% | -2.19% | 23/28 |
| fixed scale 1.00 | -4.63% | -6.46% | -4.26% | -5.15% | 20/28 |
| learned gate | **-4.98%** | **-6.86%** | **-4.76%** | **-5.57%** | 21/28 |

Gate 在有效 token 上的 mean/std 为 0.762/0.205，范围为 0.304 到 1.000；memory UOP 与
non-memory UOP 的均值分别为 0.928 和 0.752。它不是退化成一个常数，也没有只作用于
memory UOP。逐 workload trace-equal commit 结果为：

| workload | fixed 0.25 | fixed 1.00 | learned gate |
|---|---:|---:|---:|
| BVC heldout | -0.19% | +3.20% | +1.51% |
| PyTorch heldout | +0.05% | +1.65% | +0.48% |
| Redis heldout | -2.16% | -8.64% | -6.67% |
| MySQL heldout | -6.06% | -14.64% | -14.38% |
| Gofeed heldout | -5.04% | -14.27% | -14.43% |
| Flink heldout | -1.46% | -1.84% | -2.83% |
| Marine heldout | -1.57% | -2.85% | -4.14% |

结论是：Gate-only **通过了“条件强度有用”门禁，但没有通过正式非退化门禁**。它在
aggregate 上严格优于 fixed scale 1，并把 BVC/PyTorch 回归显著压小，同时保留多数
Redis/MySQL/Gofeed 收益；但 21/28 仍低于预设的 23/28，BVC 聚合仍有 +1.51% 回归，不能
直接升级为正式 v30。继续把同一 gate 多训几千 step 不能证明会解决该问题；下一步先做
regret-localization audit，定位 BVC/PyTorch 中 gate 应衰减但未衰减的 token/prefix phase，
再决定是否给 gate 增加显式 frozen gap-delta/sensitivity 标量并提高 group-level regret，
而不是加入 workload ID 或直接扩充 G2/coherence 状态。

正式报告：`logs/v30_gss_gate_only_2k_best_development_oracle.json` 和对应 Markdown；
scale sweep 为 `logs/v30_gss_p1_g1_scale_sweep_development_oracle.json`。

### 0.7 BVC、PyTorch、Redis 定向 Gate regret 定位

定向审计只包含 BVC/PyTorch/Redis heldout 的 C4/C8/C16/C32，共 12 条 trace；每条均匀
抽取 128 个 window，共 1,536 个样本和 5,853,036 个有效 UOP。对每个 commit prefix
同时计算 fixed scale 0.25、fixed scale 1 和 learned gate。若 fixed 0.25 与 fixed 1 的
绝对 commit-log 误差相差至少 0.01，则把该位置视为具有明确强度偏好。

核心定位结果如下：

| workload | 明确位置中更偏好 scale 1 | Gate 判为高强度 | 阈值正确率 | Gate score AUC |
|---|---:|---:|---:|---:|
| BVC | 36.7% | 57.8% | 46.9% | 0.490 |
| PyTorch | 39.6% | 43.8% | 51.4% | 0.509 |
| Redis | 79.2% | 70.8% | 61.8% | 0.480 |

三个 workload 内部 AUC 都约等于随机。Gate 的 0.62 到 0.75 workload 均值可以区分
“Redis 整体应更强”，但同一 workload 内 Gate 数值几乎不能排序具体 prefix 是否真的
更需要 scale 1。Gate calibration 也不单调：例如 Redis 从 gate 0.25–0.40 到
0.95–1.00，各 bin 的 oracle-scale-1 比率始终约 76%–80%；BVC 各 bin 始终约
32%–40%。因此它主要学成了 workload/core-level 强度，而不是 per-UOP exposure。

最明确的错误区域是高 memory-density prefix。该 density 已经是 Gate 的显式输入，但
Gate 给出的方向与 oracle 相反：

| 区域 | Gate mean | oracle 偏好 scale 1 | Gate 高强度 | commit 变化 | gap 变化 | 比 v29 更差的 active token |
|---|---:|---:|---:|---:|---:|---:|
| BVC density >30% | 0.738 | 13.8% | 67.4% | +2.87% | +5.00% | 84.7% |
| PyTorch density >30% | 0.737 | 29.8% | 81.0% | +2.31% | +5.13% | 58.4% |
| Redis density 15–30% | 0.802 | 13.0% | 75.9% | +5.06% | +8.35% | 84.1% |
| Redis density >30% | 0.659 | 16.0% | 51.5% | +7.13% | +8.11% | 82.5% |

另一个 shortcut 是 current-memory 位置。BVC/PyTorch/Redis 的 current-memory gate mean
分别达到 0.931/0.870/0.921，但明确位置的 oracle-scale-1 比率只有
36.2%/46.0%/75.9%。Redis 当前访存的 local gap 确实改善 -5.06%，而 BVC/PyTorch
分别退化 +2.11%/+5.04%；Gate 没有充分使用 frozen token 区分这两种 exposure，而是
过度依赖 current-memory 标志。

按 prefix phase 看，BVC 回归集中在最后 64 UOP（P3 commit +4.06%），PyTorch 回归集中
在 P2（commit +2.18%、gap +9.17%）；Redis P2/P3 的 commit 分别改善 -12.44%/-13.70%，
但 local gap 仍退化 +3.41%/+1.03%。这说明 Redis aggregate 收益的一部分来自前缀累计
校准，而不是逐 UOP service/exposure 全部建模正确。

为避免把 prefix 累计偏好误认为当前 UOP 因果标签，另外在同一 12 条 trace 上做了每条
32-window、共 1,429,356 UOP 的 local retirement-gap 核验：

| workload | learned Gate 的 gap-log 变化 | local-gap oracle scale-1 比率 | Gate score AUC |
|---|---:|---:|---:|
| BVC | +2.88% | 30.3% | 0.430 |
| PyTorch | +7.21% | 16.5% | 0.488 |
| Redis | +3.04% | 33.7% | 0.507 |

本地 gap AUC 同样没有有效排序能力，且三个负载的 gap error 全部退化。结合 density 已经
可见但方向仍学错，当前第一问题应判为 **loss/routing shortcut，而不是先判为 GSS 原始
特征不足**：训练目标允许 Gate 用 workload/core-level residual 校准降低累计 commit
误差，0.1 权重的 local-gap 约束不足以迫使它学习逐 UOP exposure，current-memory 标志则
提供了过强捷径。

因此下一版不应先增加 G2/coherence 或扩大主干。先做 Gate-v2 的可归因对照：去掉
current-memory scalar shortcut；显式输入 frozen timing sensitivity（同一 frozen head 下
full/low residual 的 local gap-logit delta）；增加低权重 counterfactual strength-ranking
与 trace/group-level regret。只有在使用相同 Gate 输入直接监督 oracle strength 的小 probe
仍接近 AUC 0.5 时，才能确认还缺 dependency criticality/ILP/MLP exposure 特征。

报告：`logs/v30_gss_gate_targeted_bvc_pytorch_redis_regret.json`（正式 128-window）和
`logs/v30_gss_gate_targeted_bvc_pytorch_redis_gapcheck32.json`（local-gap 补充核验），均有
对应 Markdown。

### 0.8 下一版正式实验：v30-G1-Joint-v2 60K

下一版只保留一个正式候选，名称为 **v30-G1-Joint-v2**。它不是把当前 Gate-only
probe 原样延长到 60K，也不是从随机权重重新训练；它从 P1 step 4,250 初始化，重置
optimizer、学习率、训练 step 和 best validation，并在同一个 60K 任务内逐步解冻完整
v29 主干。P1 checkpoint 中原有 v29 参数已经验证为 bit-exact，因此初始化来源同时满足
canonical v29 主干和已训练 G1 Adapter 两个要求；当前带 shortcut 的 Gate-only
checkpoint 不进入初始化链。

本实验先不把 GSS 直接注入八层 Full-QKVR。P1 已经证明真实 GSS 内容具有增量信息，当前
定向审计首先定位到 exposure routing/loss shortcut，而不是 GSS 表示无效；现在同时改变
注入位置会重新混淆机制收益。更重要的是，Full-QKVR 是双向 attention，若把每个位置的
GSS state 直接加入 K/V，较早 UOP 可以读取包含未来 cache transition 的较晚位置状态。
只有当前版本在 direct counterfactual 监督下仍不能学习 exposure 时，才进入下一阶段的
最后一层 Query-only GSS 调制或严格 base/GSS 双流设计。

#### 0.8.1 三锚点 timing router

令 `h` 为当前可训练 v29 主干的输出，`delta` 为 causal G1 Adapter residual，`H` 为
single timing head。对同一 token 计算三个正数 retirement-gap cycle 锚点：

```text
gap_0    = softplus(H(h))
gap_025  = softplus(H(h + 0.25 * delta))
gap_1    = softplus(H(h +        delta))
```

Gate-v2 不再输出作用于 hidden residual 的单一 scale，而是输出三锚点 softmax 权重：

```text
pi = softmax(Router(stop_grad(h), stop_grad(delta), sensitivity))

predicted_gap =
      pi_0   * gap_0
    + pi_025 * gap_025
    + pi_1   * gap_1

predicted_commit_cycle[i] = sum(predicted_gap[j], j <= i)
```

在正数 gap 上做凸组合可保持 retirement prefix 单调，并使 counterfactual 标签与 Gate
动作精确对齐；旧结构 `H(h + gate * delta)` 经过非线性 timing MLP 后不满足这种对应。
三锚点还同时提供无 GSS、已知保守强度和完整强度三个候选。Gate 初始分布设置为约
`(0.05, 0.90, 0.05)`，避免训练初期把完整 residual 强行注入所有负载。

Gate 删除显式 `current_memory` 0/1 scalar，但不删除 Adapter 和 GSS state 中的真实
memory-event 内容。Gate 读取 frozen/stop-gradient token、residual、prefix memory density、
residual RMS，以及以下当前模型可在训练和推理共同计算的 sensitivity：

```text
s_025 = log1p(gap_025) - log1p(gap_0)
s_1   = log1p(gap_1)   - log1p(gap_025)
```

同时提供 signed value、absolute value 和 strict-prefix cumulative sensitivity。Gate
ranking loss 的输入和 oracle target 均 stop-gradient，不能通过修改主干来让分类任务变得
容易；最终 timing loss 仍可沿三个 gap 锚点训练主干、Adapter 和 timing head。

#### 0.8.2 单次 60K 内的分阶段解冻

三个阶段属于同一次正式训练，不是三次独立实验：

| optimizer step | 可训练参数 |
|---:|---|
| 0–2,000 | Gate-v2 |
| 2,000–8,000 | Gate、G1 Adapter、timing head、Full-QKVR 最后两层 |
| 8,000–60,000 | static encoder、全部八层 Full-QKVR、G1 Adapter、timing head、Gate |

branch head 始终读取未注入 GSS 的 base token，branch loss 保持为 0；不加入 B2/B3
current-event/history、memory head、G2/coherence、512-UOP 窗口或 causal-QKVR 等额外
变量。训练继续使用 K=256、coverage-first、trace-balanced 和现有 immutable GSS cache。

当前一个 coverage epoch 为 44,920 optimizer step。正式配置将前两个 epoch 固定为同一
无放回 coverage permutation，第二轮结束后才切换有放回 trace-balanced sampling；默认
sampler 的单 coverage 行为不变。完整主干从 step 8,000 解冻，因此该重复顺序下，所有样本
至少在完整主干可训练时出现一次的里程碑为
`44,920 + 8,000 = 52,920`。正式脚本必须保存 `step_52920.pt`、`step_60000.pt`、`last.pt`
和 development-validation `best.pt`；60K 是约 1.34 个 coverage epoch，不假定最终 step
必然优于 earlier best。

参数组初始学习率为：Gate `3e-4`、Adapter `1e-4`、timing head `3e-5`、最后两层
Full-QKVR `1e-5`、前六层 `5e-6`、static encoder `3e-6`。每个阶段新解冻组从 0 线性
warmup 500 step；Gate/Adapter 在第一个 coverage 后衰减，旧主干在 full-trainable
coverage milestone 后衰减。旧参数不得与新 Gate 共享统一高学习率。

#### 0.8.3 监督目标（2026-07-29：移除 frozen teacher）

正式优化目标为：

```text
L = L_v29_timing
  + 0.20 * L_local_gap
  + 0.05 * L_counterfactual_rank
```

`L_counterfactual_rank` 使用真实 commit cycle 得到的 local gap 和 prefix error 比较
`gap_0/gap_025/gap_1`；仅对候选差异超过 margin 的 token 产生 soft/pairwise routing
监督，target 不回传到三个专家。step 38,000 暂停审查后决定删除逐 token/group teacher
regret、Adapter teacher-scale anchor 和全参数 L2-SP anchor。原因是训练已有真实 commit
cycle，永久的逐 token teacher 非退化会限制共享参数的必要取舍；同时在线 teacher 每个
batch 多执行一次完整 v29 QKVR 前向，而 33K–38K 实测 teacher-dependent loss 只占优化
目标约 1.7%。全参数 anchor 的加权量级约为 `5.8e-10`，几乎无约束效果，却每步扫描约
1.12 亿参数。

P1 checkpoint 现在只用于新 run 的模型初始化；resume 只恢复 student、optimizer 和 step。
训练与推理均不构造 frozen teacher。负载非退化不再作为逐 token 训练约束，而由
trace-balanced 采样、4,096-sequence validation、逐 workload full rollout 和 Pareto
checkpoint 选择负责。

#### 0.8.4 验收与后续分支

checkpoint 选择使用至少 4,096 条 trace-balanced development sequence；Redis heldout
不得进入训练或 best-checkpoint 选择。最终对 best、step 52,920 和 step 60,000 执行
seed1/heldout、C4/C8/C16/C32 serial 全量验证。门禁为：改善 trace 至少 23/28；aggregate
优于当前 Gate-only 的 -4.98%；BVC/PyTorch workload aggregate 回归不超过 +0.5%；
Redis 保留明确收益；BVC/PyTorch/Redis 的 counterfactual AUC 至少 0.55；三个定向负载的
local-gap 不再全部退化。

若 direct counterfactual 监督后 workload 内 AUC 仍约 0.5，才认为当前 token/Adapter
缺少 dependency criticality、ILP/MLP exposure 表示，并启动最后一层 Query-only GSS
调制；若 local-gap 改善而 serial commit/CPI 仍退化，则优先检查累计校准和
teacher-forced/free-running GSS order gap，而不是继续扩大 Transformer。

### 0.9 2026-07-29 serial online GSS rollout 实施状态

正式 free-running 路径已经接入 `tcsim/v30/rollout.py`，不再把 ready-clock 训练
sidecar 当作部署输入。当前实现合同为：

```text
predicted-commit-canonical-shadow-v1
```

每条 trace 创建一个 CPU canonical `GSSFeatureEngine`。每次 serial model forward 前：

1. 从 canonical 创建 touched-set copy-on-write shadow；
2. 用 `relative UOP ordinal -> core slot -> absolute UOP ordinal` 的稳定、无时间标签顺序
   preview 当前各核 256-UOP 窗口；
3. 产生 `gss_uop_categorical`、`gss_uop_continuous` 和
   `gss_memory_mask`；
4. 丢弃 preview shadow，不把完整窗口写回 canonical；
5. scheduler 决定实际消费 prefix 后，按
   `predicted commit cycle -> core slot -> absolute UOP ordinal` 排序其中的 memory UOP；
6. 在 canonical 上重放且只重放这些已消费访问。

cache set/tag/replacement state 使用父状态加 touched-set overlay，不复制完整 cache。
line seen/last-touch 历史也使用 parent lookup 加局部 delta；per-core EMA summary 仅按核心数
复制。当前报告显式记录 preview/commit event 数、无效 paddr、CPU 时间、canonical state
大小和 `gss_oracle_sidecar_consumed=false`。

checkpoint/cache 校验现在拆成两层：v29 基础输入 contract 必须完全一致；GSS checkpoint
contract 允许 free-running store 不加载 teacher sidecar，但 runtime engine 必须逐项校验
geometry、字段、dtype、pre-access 语义和 replacement policy。Oracle one-step 仍加载
ready-clock sidecar；`both` 模式的 free-running context 会在 forward 前强制覆盖 sidecar
GSS tensor，禁止其进入部署预测。

真实 `step 38,000` checkpoint 已在 seed1 BVC C4 上完成 full-trace smoke：

| 项目 | 结果 |
|---|---:|
| rollout completion | 100% |
| UOP / scheduler step | 3,532,088 / 6,175 |
| committed memory UOP | 255,116，与 raw `access>0` 数量逐条一致 |
| invalid paddr | 0 |
| canonical unique lines | 25,623 |
| preview / commit CPU time | 10.08 s / 5.06 s |
| total wall / throughput | 110.34 s / 32.0 K UOP/s |
| GSS state CPU time / wall | 13.72% |
| ROI-UOP CPI prediction / label | 1.3492 / 1.3946 |
| ROI-UOP CPI relative error | 3.25% |

这一结果验证了完整 trace 的 exact-once memory commit、canonical cursor、持久状态和
结果落盘。GSS 的 13.72% 是 preview+commit CPU 段占当前串行总 wall 的比例，不等于相对
v29 端到端吞吐下降；正式 throughput 回归仍需在同一 checkpoint/trace 上做等模型计算量
的 state-disabled control，避免把 GSS Adapter/Router 的 GPU 开销混入 CPU 状态机开销。

已使用同一 GPU、同一 seed1 BVC C4 trace、同一当前推理代码和 serial stride=256 补跑
canonical v29 59K best 对照：

| 指标 | v29 | v30 + online GSS | 变化 |
|---|---:|---:|---:|
| UOP/s | 39,750.7 | 32,010.4 | -19.47% |
| step/s | 74.83 | 55.96 | -25.21% |
| wall | 88.86 s | 110.34 s | +24.18% |
| UOP/forward | 531.2 | 572.0 | +7.68% |
| model forward | 9.48 ms | 11.39 ms | +20.07% |
| context build | 3.37 ms | 5.09 ms | +51.22% |
| scheduler | 0.10 ms | 0.92 ms | +787.64% |

其中在线 GSS preview/commit 分别为 1.63/0.82 ms/step，合计 2.45 ms/step；剩余主要增量
来自 GSS Adapter + 三锚点 Router 的 GPU forward。由于两个 checkpoint 的预测 prefix
不同，UOP/s 是真实端到端结果，step latency 更接近纯计算开销比较；要做严格模块消融仍需
为同一个 v30 checkpoint 增加 state-disabled、adapter-disabled 两个 benchmark control。

serial、`unconditional` 和 `speculative` free-running 现已共用同一个 CPU canonical
owner。多 GPU window 模式按第 8 节执行一次连续 multi-lane shadow preview，不会给每个
lane 独立冷启动 GSS state。

### P0 生产热路径实现与实测（2026-07-29）

P0 已将 serial online rollout 的逐访问 Python reference 路径替换为：

- 每核 compact memory-event index/descriptor；
- C++ flat L1D/L2/LLC canonical state 和 touched-set COW preview shadow；
- 批量 `preview[N,7]`/`commit[N,7]`，以及复用的 pinned CPU tensor workspace；
- 仅对 zero sentinel 和实际 memory event 形成 key/value 的 event-packed attention；
- 单次 batched timing-head 调用计算三个 gap anchor。

C++ 与 Python reference 的 categorical 输出逐值相等，continuous 在 `1e-7` 容差内
相等，preview rollback 后 canonical 不变。Dense masked 与 event-packed attention 也通过
数值等价测试。BF16 kernel 的归约顺序变化会让 free-running 路径不再 bit-exact，因此仍
需检查最终 CPI、event exact-once 和 canonical summary。

同机 H20、BVC seed1 C4 完整 trace：

| 路径 | UOP/s | wall | model/forward | GSS CPU/step |
|---|---:|---:|---:|---:|
| P0 前 v30 Python+dense | 32,010 | 110.34 s | 11.39 ms | 2.45 ms |
| P0 后 v30 C++ + packed | 36,416 | 96.99 s | 11.16 ms | 0.42 ms |

P0 吞吐提高 `13.76%`，GSS CPU 时间降低约 `83%`。ROI-CPI 误差从 `3.255%`
变为 `3.262%`，差 `0.007` 个百分点；两次 canonical commit 均为 `255,116`
个 memory event、`25,623` 条 unique line。相对同机 canonical v29 的
`39,751 UOP/s` 仍低 `8.39%`，剩余成本主要来自 adapter、strength router 和额外
anchor timing response，而非 cache 状态机。

构建并强制使用生产 backend：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/build_v30_gss_native.py
TCSIM_GSS_BACKEND=native ...
```

`auto` 仅用于开发环境，扩展缺失时会明确记录 Python reference backend。

### P0 单 trace 多 GPU coordinator

P0 已接通带 GSS checkpoint 的 `unconditional/speculative` coordinator：

- canonical state 由一个 CPU coordinator 独占；
- 每个 wave 只做一次连续 shadow preview，覆盖
  `K + (D-1)*shift`，再切出各 lane 的 compact tensor；
- 现有 process/thread worker 只构造 v29 只读 context；GSS preview 回到 coordinator
  统一执行，不能让每个 worker 复制一份 canonical LLC；
- speculative rejected lane 直接丢弃 feature slice/shadow，对 canonical 零写入；
- speculative accepted prefix 与 unconditional lattice 实际消费的 prefix，都按最终预测
  cycle exact-once `commit_batch`；
- GPU 只接收 compact feature tensor，不持有 tag state，因而 C++ hot path 不妨碍多卡
  model forward 并行。

实现包含 continuous-preview slicer、用 canonical cursor vector 校验的 wave state
version，以及两种 scheduler 共用的 accepted-prefix `commit_step`。中途 resume 的状态
序列化仍未实现；当前 evaluator 的 resume 粒度仍是一条完整 trace。不能把 D 个 lane
分别从同一个 canonical state 独立 preview：后续 lane 会遗漏 anchor 到该 lane 起点之间
的 cache transition。

4-GPU BVC C4、200-step smoke 已覆盖 `shift=64/256` 与两种模式。四组运行中，报告的
committed memory event、canonical event 和原始 trace 实际退休前缀的 memory event
逐项相等。`shift=64` 时：

- unconditional：`11,604 == 11,604 == 11,604`；union preview 12,823 event，若按
  lane 重复 preview 则为 29,217；
- speculative：`8,484 == 8,484 == 8,484`；union preview 21,495 event，lane 重复总量
  为 49,711；accepted/issued 为 68/513。

这证明 rejected lane 对 canonical 零污染，也证明 overlap event 只在连续 shadow 中推进
一次。当前多卡 smoke 不代表吞吐准入：`shift=64` 的 future-window 利用率较低，
`shift=256` speculative 命中率为 0；process context barrier 和现有 runner 发射开销也使
多卡暂未超过 serial。GSS preview/commit 仅占这些运行 wall 的约 1% 到 2%，后续性能优化
应优先处理 window policy、context pipeline 和 runner dispatch。

### 0.10 2026-07-29 时间合同修正（当前权威合同）

前述 0.3/0.9/P0 多卡段落记录的是历史实验实现，不再是可部署合同。审查确认旧路径存在
三个耦合问题：训练按 raw `ready_tick` 排 GSS event，部署 preview 按 relative UOP/core
slot 排序，canonical commit 又按 predicted commit cycle 排序；同一状态机实际使用了三种
时间。窗口每次重建还会重新预测未退休 UOP 的相对时间，导致某个核的 head deadline 被
反复“充值”。全局 no-progress guard 只能发现所有核都不退休，发现不了少数核长期饥饿。

当前唯一允许的新训练/部署合同为：

```text
training teacher clock = true commit cycle
deployment preview clock = retained deadline, otherwise base predicted commit cycle
canonical commit clock = final predicted commit cycle
tie break = architectural core_id, then absolute per-core UOP ordinal
time unit = cycle
QKVR interaction forward per scheduler step = 1
deadline = absolute cycle and survives overlapping-window rebuild
```

训练 cache 必须满足：

```text
clock_source = commit
order_policy = commit_tick_then_core_then_uop_v1
features_are_pre_access = true
timestamp_is_model_visible = false
```

旧 `ready` sidecar 和由它训练的 checkpoint 会被 loader/runtime 硬拒绝，不能仅修改 metadata
后复用。原因不是字段格式不同，而是 shared LLC 的访问历史和每个 event 的 pre-access
特征已经不同，必须重新 replay、重新训练。

#### 单次前向的精确定义

这里的“单次前向”指 full-QKVR 主干只执行一次，不是把因果环隐藏成第二次主干调用：

1. static encoder 与 full-QKVR interaction 产生一次 `base_token`；
2. 原 timing head 在同一 `base_token` 上给出 no-GSS provisional commit-cycle；
3. retained deadline 覆盖重叠 UOP，provisional commit-cycle 只给新出现的 tail UOP 定序；
4. CPU canonical shadow 按这个 commit-cycle 顺序构造 pre-access GSS；
5. GSS adapter/router 从同一 `base_token` 产生最终 retirement-gap/commit-cycle；
6. scheduler 使用最终时间，实际 accepted prefix 也按最终时间更新 canonical。

因此没有第二次 Transformer/QKVR 前向。额外工作是已有 timing head 的 provisional 投影、
一次必要的 GPU-to-CPU clock 同步和 GSS adapter/final head。不能声称 provisional 与 final
完全相等；它们分别承担“打破 GSS 输入因果环”和“权威 scheduler 输出”的角色。

#### Deadline 与保护

每核维护当前 lookahead 的 `(absolute_uop -> absolute_commit_cycle)`。下一窗口与旧窗口
重叠的前缀直接复用旧 deadline，不允许模型重新给它一个“从现在起”的完整延迟。新 tail
使用本次最终 gap，并接在最后一个 retained deadline 后。报告至少包含 retained/new UOP
数量和最大 retained prefix。

新增两层 fail-closed 保护：

- unretired deadline 落到当前 global time 之后会立即报错，防止静默时钟倒退；
- 任一活跃核心连续 256 个 scheduler transition 零退休时触发 per-core starvation guard，
  报告 core ID、cursor、global time 和 head deadline；阈值可由
  `--max-core-stall-steps` 调整。

#### 单 trace 多 GPU：parallel-relaxed 合同

严格的 `serial-exact` 路径保留上述 provisional timing 定序。为换取单 trace 多卡吞吐量，
另提供显式标记的 `parallel-relaxed` 路径。该路径不在模型 forward 中间执行全局同步：

1. CPU 同时构造 depth 个 future-window lane；
2. 从当前 canonical cursor 到最深 lane 尾部建立一次跨 lane continuous union；
3. 已有 UOP 按持久 absolute deadline 定序；尚无 deadline 的新 tail 按
   `(relative UOP ordinal, architectural core_id, absolute UOP ordinal)` 稳定定序；
4. canonical 的 transactional shadow 只对 union 中每个 memory event preview 一次，再把
   pre-access GSS feature 切回各 lane；
5. 各 GPU 对自己的 lane 完成一次完整 forward，无 mid-forward global sync；
6. scheduler 仍用最终 commit-cycle 和 deadline ledger；只有实际 accepted prefix 按最终
   predicted commit-cycle 更新 canonical，rejected future lane 不提交 canonical state 或 deadline。

该模式故意牺牲的是“新 tail memory event 按本次模型最终时间精确排序”：forward 前还没有
该时间，所以使用 relative-UOP 近似。它不是 `serial-exact` 的数值等价加速，报告必须写明
`gss_accuracy_mode=parallel-relaxed`、具体 preview order 和
`gss_mid_forward_global_sync=false`。architectural core ID 而非临时 lane/core slot 用作 tie-break，
deadline 时钟、accepted-prefix commit、时钟倒退保护与 per-core starvation guard 均保留。

重建正式训练 cache：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/build_v30_gss_sidecar.py \
  --manifest data/v29_global_time_dataset/manifest.json \
  --splits train,development_heldout \
  --workload-regex '' \
  --core-counts 1,4,8,16,32 \
  --clock commit \
  --workers 16 \
  --out-root data/v30_gss_commit_sidecars \
  --write-manifest data/v30_gss_commit_dataset/manifest.json
```

重建完成前不能启动新的 60K 正式训练；旧 38K checkpoint 只保留作历史退化审计，不能
resume 到 commit-clock 数据合同。

## 1. 为什么需要显式 Cache Proxy

### 1.1 当前模型缺少 cache-capacity-aware 历史

当前 v29 已包含 reuse distance、set pressure、窗口 locality 和多尺度 long-history
统计，但这些统计不能确定：

- 某条 line 是否仍在目标 L1/L2/LLC 中；
- 当前访问在 set 内的替换位置；
- 某次核心数或工作集变化是否跨过 cache capacity threshold；
- 其他核心是否已经把该 line 从共享 LLC 中驱逐；
- 相同 functional trace 在不同 cache size/assoc/bank 下如何改变命中路径。

256 UOP 或 512 UOP 窗口都远小于 64 MiB LLC 对应的历史长度。单纯增大
Transformer 参数量或层数无法恢复窗口外已经丢失的 tag/replacement 状态。

### 1.2 Redis heldout 的直接动机

当前源码与 gem5 统计审计显示，Redis heldout 相比 Redis base 同时具有：

- 更大的 private state 和 shared table；
- 更高的 uniform-tail 访问比例；
- 更多到达 LLC 的访问；
- 更高的 LLC miss rate；
- 更长的 load-to-use latency；
- 额外的串行 ALU dependency 和更高的 branch miss rate。

其中 cache proxy 能直接帮助解释的是“cache miss incidence”。它不能单独解释
dependency criticality、branch penalty 和 memory latency exposure，因此预期是必要的
结构性补充，而不是 Redis heldout 全部误差的单点修复。

### 1.3 纯模型学习微架构泛化的可辨识性问题

当前 v29 manifest 固定接受以下硬件配置：

| 项目 | 当前值 |
|---|---:|
| Private L2 | 1 MiB/core |
| Shared LLC | 64 MiB total |
| LLC banks | 8 |
| DRAM channels | 8 |

虽然模型输入包含部分 uarch 配置字段，但训练标签中没有 cache geometry 变化，模型
无法从单一配置识别“容量减半后哪些 reuse 变成 miss”。更大模型不能解决标签中没有
干预变化的问题。

Cache proxy 可以让不同 size/assoc/bank 配置立即产生不同的结构化命中结果；模型只需
学习这些结果如何与 OOO exposure、MLP 和最终 commit cycle 组合。它提高的是结构泛化
和样本效率，但仍不能代替多微架构 timing 标签。

## 2. “与 gem5 同源”的准确边界

### 2.1 TSim 已有实现

`/data00/yinhaolang/taogen/shared/lru_banked.hh` 已提供：

- `BankedSetAssocLRU`；
- `TlbSim`；
- `PageWalkSim`；
- `MshrTracker`。

`/data00/yinhaolang/taogen/mesi_ref_sim/include/simulator.hpp` 已组合：

- 每核 L1D/L1I/L2；
- 共享 L3；
- 简化 MESI line state；
- cache/TLB/walker/MSHR 派生字段。

这套实现可以作为代码和 differential-test 的起点，但不能未经修改直接作为 TCSim
生产实现。

### 2.2 当前 TSim shadow 与真实 Ruby 的替换策略不完全一致

源码审计结果：

| 层级 | 当前 gem5 实际配置 | TSim shadow |
|---|---|---|
| L1I/L1D | 显式 `LRURP` | LRU |
| Private L2 | `RubyCache` 默认 `TreePLRURP` | LRU |
| Shared LLC | `RubyCache` 默认 `TreePLRURP` | LRU |

因此 TSim 文档中的“bit-exact”表示 gem5 probe 内的 shadow oracle 与离线 ref-sim
共享同一份 shadow 代码，而不是 shadow state 与 Ruby `CacheMemory` 事件级完全一致。

已有四个 workload 的聚合对账中，shadow 对真实 Ruby LLC load miss 的平均
acc% 约为 90.65%。这说明现有实现提供了有价值的机制信号，但不应被当作真实 Ruby
cache outcome。

### 2.3 第一阶段的同源合同

第一阶段必须对齐：

- cache line size；
- physical address 到 set/bank 的映射；
- 每层 total/per-bank size 语义；
- associativity；
- L1 LRU；
- L2/LLC TreePLRU；
- valid/tag/replacement metadata；
- 支持的 fill、touch、evict、invalidate 顺序。

下列信息无法仅凭当前 functional/commit trace做到真实同源：

- issue、request、fill 和 completion 的真实顺序；
- wrong-path access 和投机 cache pollution；
- hardware prefetch request/fill；
- Ruby transient coherence state；
- MSHR entry 的真实占用周期；
- NoC、DRAM queue 和仲裁结果。

因此正式名称应为 `gem5-aligned functional cache proxy`，而不是
`gem5 cycle-exact cache simulator`。

## 3. 第一阶段状态范围

### 3.1 每条 trace 的权威状态

每条 trace 维护一份独立状态：

```text
TraceCacheState
  uarch_profile
  core[0].L1D
  core[0].L2
  core[1].L1D
  core[1].L2
  ...
  core[C-1].L1D
  core[C-1].L2
  LLC.bank[0]
  ...
  LLC.bank[B-1]
  state_schema_version
  mapping_policy_hash
  replacement_policy_hash
```

这里的“全局状态”仅指同一条 trace 内共享 LLC 的权威状态，不是所有 trace 共用
一个状态。多条 trace 推理时，每条 trace 完全独立。

### 3.2 第一阶段模型可见字段

推荐的 per-memory-token 字段：

| 字段 | 语义 |
|---|---|
| `proxy_hit_level` | L1/L2/LLC/MEMORY/UNKNOWN |
| `l1_pre_access_position` | 访问前在 L1 set 内的替换位置 |
| `l2_pre_access_position` | 访问前在 L2 set 内的替换位置 |
| `llc_pre_access_position` | 访问前在 LLC set 内的替换位置 |
| `l1_set_residency` | 访问前有效 way 数 |
| `l2_set_residency` | 访问前有效 way 数 |
| `llc_set_residency` | 访问前有效 way 数 |
| `proxy_miss_kind` | cold/capacity-or-conflict/unknown |
| `proxy_eviction_level` | 本次 fill 引发的最高层级驱逐 |
| `other_core_recent_line` | 是否存在近期跨核同 line touch |
| `paddr_valid` | 当前物理地址是否可用于精确 set 映射 |

推荐的 per-core/global summary：

- L1/L2/LLC proxy hit-rate EWMA；
- recent LLC miss run；
- recent eviction rate；
- active-core union LLC footprint；
- per-bank occupancy/pressure 的相对统计；
- current core 相对 active-core median 的 miss-pressure rank。

模型输入不暴露 raw physical address、固定 core ID 或 nominal bank-ID embedding。
内部状态可以使用真实 tag 和 bank ID bookkeeping。

### 3.3 缺失物理地址

Cache set/bank 必须以 physical address 为准。若某条 memory UOP 缺少有效 paddr：

- `paddr_valid=0`；
- `proxy_hit_level=UNKNOWN`；
- 不允许用 vaddr 静默更新 shared LLC；
- 可以继续提供与地址 relocation 无关的 reuse/locality fallback 特征。

## 4. Canonical State 与 Transactional Shadow Delta

### 4.1 Canonical state

`canonical state` 是该 trace 唯一的权威 cache 状态，只反映 scheduler 已经真正
提交的 functional memory UOP。

它必须满足：

- 未提交窗口不能修改它；
- rejected speculative window 不能修改它；
- resume 时必须和 cursor、predicted global time 一起恢复；
- schema 或 uarch policy hash 不一致时必须拒绝恢复。

### 4.2 Shadow preview

为了给未来窗口生成 cache 特征，需要从 canonical state 出发试运行未来访存：

```text
canonical state
      |
      +-- preview future functional accesses
      |
      +-- emit per-token cache features
      |
      +-- keep changes in a temporary shadow overlay
```

preview 可以遍历完整 256-UOP 模型窗口，但不得把完整窗口写入 canonical state。

### 4.3 Transactional delta

不能为每个窗口复制完整 cache。Shadow delta 只记录被当前 preview 修改的 set：

```text
(cache_level, core_or_bank, set_id)
    old/new tags
    old/new valid bits
    old/new LRU or TreePLRU bits
```

读取时优先查询 delta；该 set 没有被修改时回退到 canonical state。实现可以选择：

- touched-set copy-on-write overlay；或
- compact undo/redo log。

禁止使用全状态深拷贝作为正式实现。

### 4.4 Commit 与 rollback

假设模型输入覆盖 UOP 0 到 255，但 scheduler 最终只提交 UOP 0 到 99：

- UOP 0 到 99 的实际访存按 canonical order 更新权威状态；
- UOP 100 到 255 的 preview 修改全部丢弃；
- 下一轮从新的权威状态重新 preview。

对 speculative lane：

- lane 被接受不代表其完整 256 UOP 全部提交；
- 只有 scheduler 实际消费的 functional prefix 可以更新 canonical state；
- lane 被拒绝时直接丢弃全部 shadow delta；
- 不能用 speculative lane 的最终 shadow state 覆盖 canonical state。

### 4.5 为什么 commit 阶段建议重放实际访问

Preview 的跨核次序是模型预测前的近似顺序，而 scheduler 最终消费的 per-core prefix
和预测 commit order 可能不同。最稳妥的第一版流程是：

1. shadow preview 只负责产生模型输入；
2. GPU 输出 per-UOP gap cycle；
3. scheduler 确定实际提交 prefix；
4. 收集实际提交的 memory UOP；
5. 使用模型预测的 commit cycle 排序，稳定 tie-break 使用 core slot 和 per-core UOP
   ordinal；
6. 在 canonical state 上重放这些访问。

整个过程不读取 oracle commit tick。

## 5. 跨核 Functional Order

### 5.1 不可避免的近似

真实 cache 在 request/fill 时更新，而当前 TCSim 部署输入是 committed functional
stream。多核共享 LLC 的真实 request order 又依赖正在预测的 timing，因此存在循环：

```text
cache state influences predicted timing
predicted timing influences cross-core cache order
```

第一阶段不使用第二次完整模型 forward 来消除该循环。

### 5.2 训练期顺序：真实时间戳 Teacher Forcing

训练数据已经包含 gem5 真实时间戳。训练期禁止用旧 v29 checkpoint 的 free-running
结果生成 memory order 或 cache state。应直接从真实 trace 重放。

当前 raw micro label 的时间语义是：

```text
issue_abs_tick = fetch_tick + issue_tick
ready_abs_tick = ready_tick
retire_abs_tick = commit_tick
```

其中 `issue_tick` 和 `complete_tick` 是相对 `fetch_tick` 的 delta；不能把原始
`issue_tick` 或 `complete_tick` 直接当作跨核绝对时间排序。

如果目标是尽量复原物理 memory request/fill 顺序，训练 replay 应使用：

1. `issue_abs_tick` 生成 request/check 事件；
2. `ready_abs_tick` 生成 data-return/fill 事件；
3. `commit_tick` 只用于退休边界、训练窗口和最终 timing 标签。

如果第一版只实现每条 memory UOP 一个 cache-touch 事件，优先按
`ready_abs_tick` 排序，因为 `onDataAccessComplete` 与数据返回时刻最接近；不得把这种
顺序称为 issue order。

注意：当前 `mem_events.jsonl` 中的 `request` 和 `commit` 行是在 UOP commit 路径
写出的，其 `commit_tick` 不是 data request 到达 cache 的时间。它适合复现现有
commit-ordered ref-sim，但不是最准确的物理 memory-access sequence。构造物理顺序时
必须将 `records.micro` 与 `labels.micro` 按 `(core_id, thread_id, micro_seq)` join。

### 5.3 推理期顺序：预测时间递推

推理时没有 gem5 真值，因此运行同一套 cache transition engine，但事件时间来源改为
模型预测：

- v29 模型骨架已直接给出 predicted retirement/commit cycle；
- 第一版可以用 predicted commit order 驱动 committed-prefix replay；
- 如果要让推理也使用 request/fill 双事件语义，则必须额外预测或估计 issue/ready
  cycle，不能在训练期使用真实 issue/ready、推理期却静默改成另一种未声明语义。

这属于标准的：

```text
training = teacher-forced true event time
inference = autoregressive predicted event time
```

两侧必须共用同一个 cache mapping、replacement、event transition 和 tie-break 实现；
区别只允许是事件时钟来自真值还是模型预测。必须记录 `order_policy_version` 和
`clock_source`。

### 5.4 因果与防泄漏边界

真实时间戳只能在训练数据构建器内部用于排序和推进 teacher state：

- event E 的 cache 输入必须取 E 发生前的 state；
- 不能把 E 的数值 issue/ready/commit tick 作为模型输入；
- 不能把 E 之后的 state 回填给 E；
- 不能用未来窗口的真实时间戳修正当前 canonical state；
- Ruby 的真实 hit/path/evict/prefetch 只允许作为标签或 differential metric，除非
  部署端也实现了同一事件来源。

使用真实顺序生成“访问前状态”是 causal teacher forcing；把当前或未来真实时间数值
暴露给模型才是 timing leakage。

## 6. 模型集成

### 6.1 总原则：在首个 QKVR 前进入对应 Token

GSS 和 configured branch replay 特征的主要注入位置都是第一个 full-QKVR block
之前，但使用彼此独立的动态 encoder 和 token gate：

```text
v29 static token encoder
        +
v29 dynamic/resource encoder
        +
memory_mask * GSSAccessEncoder
        +
branch_mask * BranchReplayEncoder
        +
valid_mask * BranchHistoryEncoder
        |
        v
v29-compatible shared full-QKVR trunk
        |
        v
original single timing head
        |
        v
retirement gap cycle
```

Cache/GSS 信息先进入对应 load/store/atomic token，再由 self-attention、dependency
interaction 和 cross-core interaction 传播到真正受影响的后继 UOP。这样不要求把
memory penalty 强行全部归到 memory UOP 自己的 gap，也不会无条件污染所有非访存
token。

同理，replayed miss 先进入对应 control-UOP，由主干学习它与 redirect/recovery 附近
token 的关系，而不是额外加在最终 gap 上的固定 branch penalty。需要注意，当前
Full-QKVR 的 local attention 是 `is_causal=False`；branch embedding 在同一窗口内理论上
可以影响分支之前和之后的 token，不能把它描述成天然的“只向后传播”。第一版不为此
修改整个 v29 attention mask，而是增加因果 branch-history 字段、执行窗口边界归因审计，
并保留现有 joint-window 推理合同。

相同的 functional lookahead 也适用于窗口内较晚 memory token 的 GSS feature。它不读取
未来 timing oracle，但可能让模型把较晚的机制事件错误归因到较早 gap；因此 branch/GSS
都必须做 window shift、prefix truncation 和 penalty-position audit。

### 6.2 Per-access GSS 特征：独立动态 Token Encoder

以下字段属于具体 memory UOP：

- `proxy_hit_level`；
- L1/L2/LLC pre-access replacement position；
- L1/L2/LLC set residency；
- `proxy_miss_kind`；
- `proxy_eviction_level`；
- `other_core_recent_line`；
- 当前访问对应 LLC bank 的相对压力；
- `paddr_valid`。

建议新增独立输入：

```text
gss_uop_categorical: [N, K, F_gss_cat]
gss_uop_continuous:  [N, K, F_gss_cont]
```

其中：

- `N` 是 batch 中所有 active-core rows；
- `K` 是每核 token 窗口，第一版保持 `K <= 256`；
- categorical 字段走独立 embedding；
- continuous 字段先做固定合同归一化，再走小 MLP；
- raw physical address、完整 tag、固定 bank/core ID 不进入模型。

GSS 字段不能追加到 `StaticTokenEncoderV29` 的 `per_uop_fields`。这些字段随
canonical state 和 rollout step 改变，放入 static encoder 会使现有 CPU/GPU static
token cache 失效或错误复用旧状态。

推荐计算：

```text
gss_access_hidden =
    GSSAccessProjection(
        categorical_embedding,
        normalized_continuous_features
    )

memory_mask =
    valid AND mem_kind in {load, store, atomic}
```

### 6.3 Per-core GSS 状态：第一版只做 Memory-token FiLM

Per-core GSS summary 包括：

- L1/L2/LLC proxy hit-rate EWMA；
- recent LLC miss run；
- recent eviction rate；
- private/shared working-set footprint；
- current-core miss-pressure rank；
- 跨核共享和 active-core union footprint summary。

建议新增：

```text
gss_core_features: [N, F_gss_core]
```

它不直接加到该核心全部 K 个 token，而用于调制 per-access embedding：

```text
core_condition =
    GSSCoreEncoder(gss_core_features)

gamma, beta =
    GSSCoreModulation(core_condition)

conditioned_gss_access =
    LayerNorm(
        (1 + gamma) * gss_access_hidden
        + beta
    )

input_hidden =
    base_hidden
    + memory_mask * conditioned_gss_access
```

同一 LLC miss 因而可以在不同 per-core working set、共享压力和 miss history 下得到
不同表示，但 beta/gamma 仍只作用于 memory token。

当前 v29 cross-core gate 是 core-row 级向量，会调节该核心全部 token 的 cross-core
interaction。如果第一版直接加入 GSS summary，memory pressure 仍可能间接影响 compute、
SIMD 和 branch token，重新形成 workload/core 级 shortcut。

因此第一版保持现有 v29 cross-core gate 不变：

```text
cross_gate =
    Gate(
        relation_features,
        scheduler_state
    )
```

`compact_gss_core_features` 只用于调制 `gss_access_hidden`，且最终仍受
`memory_mask` 限制。GSS-aware cross-gate 作为后续独立消融；只有 access-only + FiLM
已经证明非退化后才能启用，并应采用有界、低增益增量，而不是替换原 gate：

```text
cross_gate =
    clamp(
        existing_v29_cross_gate
        + alpha * gss_cross_gate_delta,
        0,
        1
    )
```

其中 `alpha` 从 0 初始化，且必须单独报告 compute/branch token 的回归。

### 6.4 Global GSS 状态：留在状态机内部

完整 LLC tag、replacement metadata、bank state 和跨核目录状态不进入 GPU 模型。
GSS 根据当前 memory access 查询全局状态，并把结果编译成与该访问相关的 compact
特征：

```text
global LLC state
+ current memory access
        |
        v
hit level / replacement position / residency /
bank pressure / cross-core sharing relation
        |
        v
current memory token
```

少量真正的 sample-global summary 可以保留为：

```text
gss_global_features: [B, F_gss_global]
```

例如 global LLC occupancy、active-core union footprint 和 aggregate miss pressure。
第一版只把它们用于 GSSCoreEncoder 对 memory access embedding 的 FiLM 条件，不进入
cross-gate，也不直接广播 residual 到所有 core/token。

如果后续 NoC、DRAM channel、shared MSHR pool 等状态无法合理归属于单条访问，可以
增加 LLC-bank/DRAM-channel/global-system resource token。该方案会改变 attention
合同和 variable-resource topology，属于 v30 后续阶段，不进入第一版。

### 6.5 保留 v29 Single Timing Head

第一版最终输出保持：

```text
token_state =
    FullQKVR(input_hidden)

gap_cycle =
    OriginalGapHead(token_state)
```

不增加能够直接对最终 gap 做加法的独立 `MemoryCorrectionHead`。原因是最终 head 才
读取 GSS 时：

- cache miss 信息不能参与 QKVR interaction；
- 无法影响依赖该 load 的 compute token；
- 无法影响跨核 token interaction；
- 容易把整个 memory penalty 强行堆到 memory UOP 自身；
- 容易重现 E2 中 workload 互相拉偏的问题。

### 6.6 明确禁止的注入路径

第一版禁止：

```text
hidden += GSSCoreProjection(gss_core).unsqueeze(token_dimension)
```

这种写法会把 LLC 压力同时加入 compute、SIMD、branch、serialize 和不相关 memory
token，等价于 workload/core 级 timing bias。它与此前 long-history global residual
的退化机制相同。

同时禁止：

- 把 GSS state 拼入 `chunk_summary + relation + uarch + scheduler_state` 后经现有
  `side_projection` 统一广播；
- 只把 GSS 连接到最终 MemoryHead；
- 把完整 cache tag、set contents 或 request queue 送进 attention；
- 把固定 core ID、bank ID、channel ID 当 learned identity embedding；
- 让非 memory token 直接读取 access-only cache outcome。

### 6.7 v30 第一版模型输入合同

推荐新增四组独立版本字段：

```text
GSS_UOP_CATEGORICAL_FIELDS
GSS_UOP_CONTINUOUS_FIELDS
GSS_CORE_FEATURES
GSS_GLOBAL_FEATURES
```

数据流为：

```text
base_hidden =
    static_projection
    + dynamic_projection
    + existing_non_gss_side_projection

gss_hidden =
    GSSAccessEncoder(
        gss_uop_categorical,
        gss_uop_continuous
    )

gss_hidden =
    GSSCoreFiLM(
        gss_hidden,
        gss_core_features,
        selected_gss_global_features
    )

input_hidden =
    base_hidden
    + memory_mask * gss_hidden
    + branch_mask * branch_event_hidden
    + valid_mask * branch_history_hidden

token_state =
    FullQKVR(
        input_hidden,
        existing_v29_cross_gate
    )

retirement_gap =
    OriginalSingleTimingHead(token_state)
```

必须在 feature/checkpoint metadata 中分别记录四组字段的名字、维度、归一化合同和
encoder version，不能把它们混入 v29 原字段后仅增加总维度。

Branch replay 字段单独使用 `BRANCH_REPLAY_*` 合同和 encoder version，不混入 GSS
四组字段，也不追加到 v29 static token cache。

### 6.8 初始化策略

- 训练数据和 cache state 一律由真实时间戳 replay 生成，不允许旧 v29 checkpoint
  参与 memory order 或状态构造；
- 旧 v29 权重最多只能作为参数初始化或冻结主干的结构 probe，不能作为训练数据生成器；
- 从旧 v29 参数做结构 probe 时，可以把 GSSAccessEncoder 的最终 projection
  零初始化，使初始 timing 输出严格等于所选 v29 baseline；
- `BranchReplayEncoder` 和 `BranchHistoryEncoder` 的最终 projection 同样零初始化；
- 最终从头训练时，GSS feature 直接通过独立 encoder 并入 QKVR 输入，不要求额外
  residual adapter；
- 保持原 gap head，避免同时改变主干、GSS 和 timing-head 分解。

“严格超集”要求被选作 B0 的最佳 v29 timing 路径、基础输入和原 cross-core gate 在
v30 中原样保留。删除 `branch_head` 不影响 timing forward；旧 long-history 字段是否
删除必须另做消融，不能与 GSS/branch replay 同时移除。结构等价测试必须让 v29/v30
加载相同的 shared timing weights，并将所有新增 projection 置零：

```text
max_abs(v30_timing(new_paths_zero) - v29_timing) <= numeric_tolerance
```

该测试证明的是 v30 hypothesis class 包含 v29 timing path。冻结/初始化 probe 可以直接
复制最佳 v29 权重；正式从头训练只要求结构和初始 shared weights 可对应，不声称一个
随机初始化模型已经等于训练完成的最佳 v29。若不能满足该结构等价性，就不能把后续差异
归因于新增机制输入。

### 6.9 训练期辅助监督

允许用 gem5 的真实 cache/path 字段做低权重辅助监督，但必须满足：

- oracle 字段只进入 loss/metric；
- 不进入 cache proxy 状态更新；
- 不进入部署输入；
- 辅助 loss steady-state contribution 建议不超过总 loss 的 2%；
- 保留 auxiliary weight 为 0 的消融。

第一阶段不使用 MSHR/TLB/coherence 辅助输出控制最终 timing，也不构造独立
Base/Memory timing 标签。

### 6.10 删除 Neural Branch Head，Replay 结果作为输入

#### 6.10.1 决策依据

当前 neural branch head 的输出没有进入 `gap_head`，只用于独立 branch loss、统计和
部署 branch count。seed1 C4/C8/C16/C32 共 92 条 trace、36,587,576 个 branch 的
configured replay 审计结果为：

| 指标 | All | Heldout |
|---|---:|---:|
| Event F1 | 99.824% | 99.716% |
| Event mismatch | 0.015% | 0.034% |
| Pooled miss-count error | 0.074% | 0.334% |
| 256-UOP window exact match | 99.914% | 99.811% |
| 256-UOP window rate MAE | 0.0197 pp | 0.0323 pp |

历史 neural head 的 aggregate miss count/rate error 为 all `30.56% / 1.56 pp`、heldout
`74.56% / 4.38 pp`；configured replay 分别为 `0.074% / 0.0032 pp` 和
`0.334% / 0.0199 pp`。完整证据见
[v29 packed-3 评估报告](v29_packed3_checkpoint_evaluation_report.md)和
`logs/branch_replay_event_window_seed1_20260720_full/summary.md`。

因此 v30 不再要求 Transformer 重复学习一个已经可以由 functional trace 和 predictor
配置高精度确定的机制。删除 branch head 本身不会改变当前 timing 计算；真正可能改善
CPI/window error 的改动，是把逐 branch replay 结果注入 full-QKVR 并重新训练 timing
模型。

#### 6.10.2 输入字段与注入位置

第一版把输入分为当前 branch event 和对所有 UOP 可见的 causal prefix history。
当前 event 字段为：

```text
replay_full_miss
replay_direction_miss
replay_target_miss
replay_cold_state
```

它们只在对应 branch token 上有效。Causal prefix-history 字段为：

```text
uops_since_previous_replay_miss
branches_since_previous_replay_miss
replay_misses_last_16_branches
replay_misses_last_64_branches
previous_replay_miss_kind
```

实现稳定后可以增加：

```text
replay_btb_miss
replay_ras_miss_or_unknown
replay_indirect_miss
replay_provider
replay_confidence_or_counter_margin_bucket
```

后五项只由当前 UOP 之前的 replay 结果构造，用来覆盖 branch penalty 归属和 256-UOP
窗口边界。所有 distance/count 字段必须截断、分桶或按固定合同归一化。随机窗口只能
读取从 trace 起点连续 replay 后的 history sidecar，不能从窗口起点重新计算。

两组字段使用独立轻量 encoder，避免把 current miss 回填到非 branch token：

```text
branch_event_hidden =
    BranchReplayEncoder(current_branch_event)

branch_history_hidden =
    BranchHistoryEncoder(causal_prefix_history)

input_hidden =
    base_hidden
    + memory_mask * conditioned_gss_access
    + branch_mask * branch_event_hidden
    + valid_mask * branch_history_hidden

token_state =
    FullQKVR(input_hidden, existing_v29_cross_gate)

retirement_gap_cycle =
    OriginalSingleTimingHead(token_state)
```

不增加独立 `BranchPenaltyHead`，也不为 base/branch 构造两套 timing 标签。单一 timing
head 继续直接监督真实 per-UOP retirement gap cycle，由主干学习 replayed miss 在当前
ILP、依赖关系、ROB 压力和后续控制流中的可见代价。

删除 branch auxiliary loss 可能移除少量控制流表征的正则化作用，因此必须用“只删除
head/loss、尚未加入 replay”的隔离实验测量。但正式 v30 不为这种潜在正则化保留一个
精度较差的生产 branch head；如需诊断，只能使用不进入正式 loss/checkpoint 合同的
detached linear probe。

#### 6.10.3 因果与防泄漏合同

允许进入 replay 的信息只有 functional trace 中部署时同样可得的 branch PC、kind、
实际功能方向、target/next-PC、thread 和 committed program order，以及目标 predictor
配置。对于每个 branch，必须先使用事件前 predictor state 产生预测，再与该 branch 的
functional outcome/target 比较生成 replay miss，最后更新 predictor state。

明确禁止作为模型输入或 replay transition 条件：

- gem5 `mispredicted`；
- gem5 BTB hit、provider、预测方向或 predictor table snapshot；
- squash/wrong-path、真实 recovery latency；
- fetch/issue/ready/commit tick；
- 真实 branch penalty cycle；
- 固定 PC、core ID 或 predictor table identity embedding。

真实 `mispredicted` 只能在 replay 完成后用于 event/count/rate differential audit。由于
TCSim 是 trace-driven simulator，功能方向和 target 属于 architectural trace 事实，不是
timing oracle；但 correct-path replay 仍无法恢复 trace 中不存在的 wrong-path predictor
污染，该近似必须在报告中保留 cold/steady 分项。

#### 6.10.4 训练、推理与 PMU 合同

训练和推理必须调用同一 production replay engine，使用同一配置和初始状态策略：

```text
functional branch stream + predictor config
                    |
                    v
       configured predictor replay
                    |
      per-event + per-UOP prefix sidecar
                    |
                    +--> BranchReplay/HistoryEncoder --> Full-QKVR timing
                    |
                    +--> deterministic replay miss accumulation --> branch PMU
```

训练 sidecar 可以离线生成；推理也可以在模型 rollout 前按每核 branch ordinal 一次性
生成，因为 replay 不依赖预测的 global time。窗口随机采样和单 trace 多 GPU 只能读取
预先对齐的 sidecar，不能在窗口边界把 predictor 冷启动。PMU 字段应改名为
`replayed_branch_misses` / `replayed_branch_miss_rate`，如为兼容保留旧
`predicted_*` 名称，metadata 必须声明其来源为 configured replay。

sidecar metadata 至少包含：

```text
branch_replay_contract
predictor_family
predictor_config_hash
replay_implementation_version
functional_trace_hash
cold_or_warmup_policy
branch_ordinal_alignment_version
```

有 ROI 前 functional stream 时应先 warmup predictor；没有时显式标记 cold start，不能
用 oracle predictor snapshot 补齐。当前审计中 steady-state event mismatch 为 `0.002%`，
cold 部分为 `0.085%`，因此 cold/warm policy 不能省略。

## 7. 训练数据与状态 Cache

### 7.1 真实时间戳重放是训练数据的唯一正式来源

训练期 cache state 的正式来源是 raw gem5 trace，不是任何旧模型预测。构建流程为：

```text
records.micro + labels.micro
        |
        +-- join by core/thread/micro_seq
        |
        +-- derive issue_abs_tick and ready_abs_tick
        |
        +-- global stable sort of memory events
        |
        +-- replay the production CacheProxyEngine
        |
        +-- emit pre-access proxy features per memory UOP
        |
        +-- write immutable training sidecar/cache
```

排序 tie-break 必须固定并版本化，例如：

```text
event_tick
event_kind: fill before/after request according to declared policy
core_id
thread_id
micro_seq
```

### 7.2 训练 sidecar

为保持 8 卡随机训练吞吐，离线为每个 UOP 保存模型真正可见的 compact 字段：

- proxy hit level；
- 各层 pre-access position；
- set residency；
- miss kind；
- eviction summary；
- compact per-core/global pressure summary；
- `paddr_valid`；
- cache schema、uarch hash、order-policy hash。

常规 DataLoader 直接 mmap sidecar，不在线恢复完整 cache，也不从 trace 起点重复重放。

Branch replay 使用独立的 immutable sidecar。它按 per-core/per-thread branch ordinal 与
UOP index 对齐，存放第 6.10 节定义的 compact replay 字段和 contract metadata。它不
需要保存完整 predictor state 的逐窗口副本，也不需要按真实时间戳做跨核排序；同一核的
committed functional order 即为唯一 replay 顺序。DataLoader 必须将 GSS sidecar 和
branch sidecar 分别校验后再组合为模型输入。

Branch causal-history 字段必须在完整 per-core replay 流上一次生成，再投影到所有 UOP。
`BranchHistoryEncoder` 可以作用于 valid non-branch token，但每个 UOP 的字段只能聚合其
program-order prefix。当前/future branch 的 `replay_full_miss` 仍只能通过对应
`branch_mask` 和 `BranchReplayEncoder` 注入，不能直接回填到更早的 UOP。由于主干保留
非因果 joint-window attention，这不构成严格的 channel-level 因果隔离；第一版必须显式
承认 functional lookahead，并通过窗口截断/位移一致性实验检查模型是否错误前移 penalty。

### 7.3 Oracle 信息的隔离

下列信息可以用于构造 teacher-forced state，但不能作为显式模型字段：

- `issue_abs_tick`；
- `ready_abs_tick`；
- `commit_tick`；
- 全局 event ordinal。

gem5 `mispredicted` 不能用于构造 branch replay 输入或更新 replay state，只能在 replay
完成后作为 differential 标签。功能性的 `branch_taken`、target 和 next-PC 可以驱动
predictor update，因为训练和部署消费的是同一条 functional trace。

下列信息只允许作为辅助标签或审计指标，默认不参与 proxy transition：

- Ruby 真实 hit/path；
- Ruby evict/invalidate；
- hardware prefetch；
- 真实 MSHR depth；
- coherence transient state。

原因是部署端没有这些真实事件。若训练 state 消费它们而推理 state 不消费，就会产生
另一种更隐蔽的 train/inference skew。

### 7.4 Teacher-forcing gap 的处理

训练使用真实时间、推理使用预测时间会产生正常的 autoregressive exposure gap，但不
应通过旧 v29 rollout 构造训练输入来解决。第一版采用：

- 对接近同 tick 的跨核事件做小范围 order-jitter augmentation；
- 对少量 set state 做受控 dropout/corruption；
- 同时报告 true-ready-order、true-commit-order 两种 replay 的特征敏感度；
- 验证轻微 event swap 对最终 CPI 的影响；
- 推理时始终由当前模型预测时间在线递推，不读取旧模型轨迹。

此外必须在同一 trace 上记录 true-order teacher GSS 与 predicted-order serial GSS 的：

- per-access hit-level mismatch；
- set/tag/replacement-state divergence；
- 首次 divergence 的 step 和原因；
- divergence 后 CPI 误差增量；
- 随核心数变化的 mismatch 曲线。

该审计用于区分“模型 timing 本身错误”和“timing 错误进一步拉偏 GSS 状态”的闭环
放大。不得只用 teacher sidecar 对 Ruby 的命中率评价 GSS。

该 exposure gap 只适用于依赖跨核预测时间排序的 GSS。Branch replay 不依赖预测时间，
训练与推理应逐事件完全同源，不应为 branch replay 引入 teacher-forced/free-running
两套状态。

### 7.5 状态 checkpoint

训练 sidecar 一次性由真实时间戳顺序生成，不需要保存每个样本的完整状态。仅为构建
任务的断点续跑和推理 resume 保存：

- 稀疏周期性 canonical checkpoint；
- checkpoint 之间的 compact event/delta log；
- schema、uarch 和 order-policy hash。

禁止按每个训练窗口保存一份完整 20 到 55 MB 状态。

## 8. 单 Trace 多 GPU 语义

当前单 trace 多 GPU 模式会从同一轮 anchor 构造多个偏移窗口。引入 cache state 后，
后续 lane 不能简单把自己的起点直接套在同一个 canonical state 上，否则会漏掉 anchor
到该 lane 起点之间的 functional cache 更新。

推荐流程：

1. CPU canonical owner 从当前 anchor 状态开始；
2. 用一次连续 shadow preview 覆盖最深 lane 所需的 functional 范围；
3. 在每个 lane 起点记录特征切片或 touched-set checkpoint；
4. 将各 lane 的小型 feature tensor 分发到各 GPU；
5. GPU 并行 forward；
6. scheduler 按 serial/speculative/unconditional 合同消费预测；
7. CPU 只重放真正提交的 memory UOP；
8. rejected lane 的 shadow 数据全部丢弃。

这使 preview 复杂度更接近：

```text
K + (D - 1) * shift
```

而不是：

```text
D * K + D copies of full cache state
```

### 8.1 状态所有权

- Canonical state 放在 CPU coordinator；
- private L1/L2 按 core 分区；
- shared LLC 按 bank 分区；
- GPU 不保存完整 tag state；
- 多 trace 并行时每条 trace 独立；
- 单 trace 内第一版无需全局 mutex，由 coordinator 串行 commit 即可。

### 8.2 对多卡扩展性的影响

Cache update 本身是短 CPU 串行段，不会改变 GPU 模型 forward 的并行性。主要风险是：

- preview 完成前 GPU 必须等待 feature tensor；
- 过多 CPU/GPU barrier；
- 每个 lane 复制完整状态；
- process worker 间传输大状态；
- 为了得到精确 cross-core issue order 做第二次模型 forward。

第一版明确禁止后四种实现。

Branch replay 不进入上述 transactional 协调路径。它可以在发起多 GPU window forward
前一次性顺序 replay，并按 UOP index 切片给各 lane；重叠窗口读取同一 immutable 结果，
不会重复更新 predictor state，也不存在 rejected lane rollback。

## 9. 状态大小与吞吐开销

### 9.1 当前 TSim LRU 实现实测

在本机使用 TSim 当前 `BankedSetAssocLRU`，配置为：

- C32；
- 每核 32 KiB L1D、8-way；
- 每核 1 MiB L2、8-way；
- 共享 64 MiB LLC、8 banks、16-way；
- 500 万次随机三级 cache event。

实测结果：

| 指标 | 结果 |
|---|---:|
| 三级 cache event 延迟 | 约 0.778 microsecond/event |
| 单 CPU 处理能力 | 约 1.285 million events/s |
| 最大 RSS | 约 55.6 MB |

该结果只覆盖 tag/LRU 主体，不代表完整 MESI unordered-map、真实 MSHR 或 DRAM
状态成本。

### 9.2 对当前 C32 推理的估算

当前 Redis-heldout C32 serial free-running 大致为：

| 阶段 | 每 forward |
|---|---:|
| Context build | 约 20.4 ms |
| Model | 约 40.8 ms |
| Scheduler | 约 0.52 ms |
| Total step | 约 62.5 ms |

按 8192 input token/forward 估算：

| Memory-token 比例 | Cache update 估算 | 相对 62.5 ms step |
|---|---:|---:|
| 约 5% | 约 0.3 到 0.6 ms | 通常低于 1% |
| 约 30% | 约 1.9 到 3.0 ms | 约 3% 到 5% |

额外 8 个 float feature 在 C32、K=256 时原始 tensor 约 0.25 MB/forward，
传输量不是主要瓶颈。

### 9.2.1 2026-07-28 C32 分项实测

本轮按 residual audit 通过的 G1 路径测量：C32 Redis-heldout，32×256=8192 token，
380 个 memory event（4.64%），canonical v29 60K best checkpoint，BF16 autocast。
该次 GPU 微基准使用 `d_adapter=128`、4 heads、308,376 参数的 causal residual
adapter，最终 projection 零初始化。它包含后来在正式 P1 中禁止的 960 维 output bias；
正式无 bias Adapter 为 307,416 参数，因此这里的延迟是保守近似，不会低估正式路径。

| 分项 | 结果 |
|---|---:|
| v29 model-from-static median | 39.404 ms |
| v29 + G1 causal adapter median | 39.734 ms |
| adapter 增量 | +0.330 ms / +0.84% latency |
| model-only throughput change | -0.83% |
| zero-init timing tensor max absolute delta | 0 |
| compact GSS tensor H2D median | 0.092 ms |
| flat LRU/TreePLRU C++ state engine | 0.168 microsecond/event median |
| 当前 380 event 的 state update | 约 0.064 ms |

组合 adapter、state update 和 H2D 后，增量约为 0.486 ms：

- 相对 39.404 ms 的 model-from-static 路径，估计 latency 增加约 1.23%，吞吐下降约
  1.22%；
- 相对当前约 62.5 ms 的完整 serial step，估计 latency 增加约 0.78%，吞吐下降约
  0.77%。

这里的 0.77% 是**基于分项实测的组合估计**，不是已经完成 GSS rollout 接线后的
端到端测量。C++ 状态机微基准覆盖 flat tag、L1 LRU 和 L2/LLC TreePLRU，不覆盖
transactional overlay、coherence/TLB/MSHR。模型 adapter 则是在真实 C32 batch 上的
完整 forward 实测。

若每 step 直接从 32 个 memmap sidecar 文件做 Python `np.load/search/slice`，单次约
10.09 ms；这是明确禁止的 naive 路径，不计入生产估算。正式实现必须由 canonical C++
engine 直接写入复用的 compact/pinned buffer，训练 cache 则由 DataLoader 顺序读取。

原始结果：`logs/v30_gss_throughput_c32_20260728.json`。

### 9.3 生产实现目标

TSim 当前使用 `std::list` 维护每个 set 的替换顺序，适合验证，不适合最终热路径。
生产实现推荐：

- flat tag array；
- compact valid bits；
- L1 LRU age/order bits；
- L2/LLC TreePLRU bits；
- touched-set overlay；
- batched C++ API；
- 无 per-access Python object。

预计纯 tag/replacement 状态可压缩到约 20 到 30 MB/trace，并比当前 list 版本更快。

如果后续加入 coherence，不应照搬 TSim 中无界的
`unordered_map<line, unordered_set<core>>`。C32 可以使用 compact owner/state 和
32-bit sharer mask，并对状态范围设置明确上界。

## 10. 微架构泛化实验

### 10.1 Cache proxy 能直接帮助的维度

- L1/L2/LLC size；
- associativity；
- LLC bank 数和 set mapping；
- 核心数变化引起的 shared LLC footprint；
- capacity/conflict miss threshold；
- private/shared cache 压力。

### 10.2 仍需要标签变化的维度

- cache hit latency；
- ROB/IQ/LQ/SQ size；
- MSHR 数；
- NoC bandwidth/latency；
- DRAM channels、queue、timing；
- branch miss 的 recovery penalty 与流水线可见性；
- issue/commit width。

已支持 predictor family 内的表大小、counter、BTB、RAS 和 indirect 配置变化由
configured replay 显式处理，不要求 neural head 从 uarch scalar 猜 miss incidence。
但是不同 predictor 对 fetch/redirect/recovery timing 的影响仍需多配置 timing 标签；未
实现的 predictor family（当前包括标准 TAGE）不能靠修改配置名自动泛化。

### 10.3 理论收益与预期边界

若 `X` 是 v29 输入、`Z` 是 GSS/branch replay 的额外机制状态、`Y` 是真实 retirement
timing，则 Bayes-optimal 风险满足：

```text
E[Var(Y | X, Z)] <= E[Var(Y | X)]
```

成立的工程前提是 v30 保留完整 `X` 路径、允许模型把新分支权重学为零，并且 `Z` 在
训练与部署具有相同语义。它只说明最佳可达到误差不会增加，不保证有限样本 SGD 得到的
test error 更低。实际收益的置信度分层如下：

| 对象 | 预期 | 主要原因或风险 |
|---|---|---|
| Branch PMU | 高置信度显著改善 | configured replay 已完成逐事件验证 |
| Branch-heavy timing | 中高概率改善 | miss incidence 近乎确定，penalty 仍由 timing head 学习 |
| Redis heldout timing | 中等概率改善 | 主要依赖 GSS；branch 不是当前 42% CPI 误差主因 |
| Memory-random | 可能改善 | miss incidence 更明确，但 MLP/DRAM latency 仍是学习问题 |
| Memory-seq/compute | 应保持非退化 | access gating 和零初始化；作为 shortcut 检查 |
| 核心数泛化 | 有潜力但风险较高 | shared state 更真实，同时 predicted-order skew 随核数增加 |
| 新微架构 | 机制泛化改善 | recovery/cache latency 映射仍需要多配置 timing 标签 |

GSS 与 branch replay 解决的是长期 cache/predictor state，不解决超过 256 UOP 的任意
dependency、ILP 或 ROB 历史。因此第一版保持 `K=256` 有利于公平归因，不应把窗口扩到
512 与机制输入同时修改；若 v30 通过后仍存在长依赖误差，再单独做 K=256/512 消融。

建议采集小规模、多配置正交训练集，而不是只在当前单一配置训练：

- L2：512 KiB、1 MiB、2 MiB；
- LLC：32 MiB、64 MiB、128 MiB；
- LLC assoc：至少两档；
- LLC banks：至少两档；
- 核心数：覆盖 C1/C4/C8/C16/C32，并保留 leave-core-count-out；
- cache latency 与 capacity 分开变化，避免模型把容量直接当延迟 ID。

正式结论使用 leave-one-uarch-out，而不是只使用 leave-workload-out。

## 11. 实施顺序

### Phase 0：语义与 differential audit

- 固化现有 v29、Redis base/heldout 和 mechanism cube 基线；
- 从 gem5 config 自动导出 cache mapping/replacement policy；
- 修正 L2/LLC TreePLRU 语义；
- 用 functional event stream 对账 proxy 与 Ruby；
- 分别报告 event-level hit-path accuracy 和 aggregate miss count；
- 固化 configured branch replay 的 event/window/cold/steady 基线及 sidecar contract；
- 不修改模型。

### Phase 1：Serial D-side Cache Proxy

- 实现 compact C++ L1D/L2/LLC state；
- 实现 canonical state；
- 实现 touched-set shadow overlay；
- 实现实际 committed-prefix replay；
- 增加 state save/resume 和 policy hash；
- 增加 state-update p50/p95/p99 统计。

### Phase 2：模型 Probe

- 使用真实时间戳 sidecar；
- 可以冻结旧 v29 主干做结构 probe，但旧模型不得参与 memory order/state 构造；
- 比较冻结的 v29 baseline 与完整 v30 candidate；
- 保持原 single timing head；
- 删除 neural branch head/loss，以 configured replay 同时提供 branch-token 输入和 PMU；
- 新增 encoder 最终投影零初始化，并验证 v30/v29 timing forward 等价；
- 第一版 GSS 只做 access encoder + memory-token FiLM，不修改 cross-core gate；
- 测试辅助监督 weight 为 0 和低权重两档；
- 先 one-step，再 serial free-running；
- 不先跑完整 60k 主训练。

### Phase 3：全量时间戳 Sidecar 与完整训练

- 为训练集建立 immutable timestamp-replay feature cache；
- 建立独立 immutable branch-replay sidecar，并验证 branch ordinal/UOP 对齐；
- 验证 `issue_abs_tick/ready_abs_tick/commit_tick` 语义和 join coverage；
- 使用 coverage-first sampler；
- 训练相同步数和相同数据覆盖的公平对照；
- 检查 Redis heldout 改善是否伴随其他 workload 退化；
- 执行 leave-workload、leave-seed 和 leave-uarch 验证。

Branch 与 GSS 至少保留以下可归因消融：

| 方案 | 新增机制输入 | Branch head/loss | 目的 |
|---|---|---|---|
| B0：最佳 v29 | 无 | 保留 | 正式基线 |
| B1：head removal | 无 | 删除 | 隔离辅助监督影响 |
| B2：current-event replay | 当前 branch replay event | 删除 | 测 miss incidence 输入收益 |
| B3：branch causal-history | B2 + prefix history | 删除 | 测 penalty 归属和跨窗口历史 |
| G1：GSS access-only | per-access GSS | 与所选 branch 基线一致 | 测 proxy 本体 |
| G2：GSS FiLM | G1 + per-core memory-token FiLM | 同左 | 测 core memory context |
| G3：GSS cross-gate | G2 + 有界 GSS gate delta | 同左 | 后续高风险消融 |
| BG：正式候选 | 最佳 branch + 最佳 GSS | 删除 | 最终联合方案 |

B2/B3 必须同时检查 branch-heavy、Redis heldout、compute 和 memory workload 的
CPI/window 误差，并报告 cold/steady 和窗口边界分项。不能因为 branch PMU 已接近精确，
就直接推断 timing 一定改善；当前 neural head 原本没有进入 gap 路径，timing 收益必须由
B0/B1/B2/B3 重新训练对照证明。G1/G2 通过前不运行 G3；BG 只能组合已经分别通过
非退化验收的 branch 与 GSS 版本。

### Phase 4：单 Trace 多 GPU（parallel-relaxed coordinator 已恢复）

- unconditional/speculative 均复用 continuous transactional shadow；
- union 已接入 retained absolute deadline，fallback 使用 relative UOP + architectural core ID；
- 每 lane 单次完整 forward，不要求 mid-forward global sync；
- accepted prefix 才提交 canonical，rejected lane 对 canonical/deadline 零污染；
- 单元回归覆盖 overlap exact-once、非连续 core ID 与两种 parallel mode；
- 待完成 D=2/4/8、shift=32/64/128/256 的 serial-exact/parallel-relaxed 精度与吞吐矩阵；
- 待实现 mid-trace GSS canonical state 与 deadline ledger 的联合 resume serialization。

### Phase 5：可选扩展

只有前四阶段通过后才考虑：

- I-cache；
- compact MESI stable state；
- TLB functional proxy；
- predicted-time MSHR；
- DRAM row-buffer/FR-FCFS virtual state。

这些扩展必须独立消融，不能一次性与 cache proxy 合并。

## 12. 验收标准

### 12.1 正确性

- 不读取 oracle commit tick、真实 hit level 或真实 queue state；
- 未提交 token 不修改 canonical state；
- rejected speculative lane 对 canonical state 零影响；
- resume 前后 cache state 与预测结果一致；
- serial 下相同 functional order 可重复；
- core-slot permutation 后结果等价置换；
- 缺失 paddr 不污染 shared LLC state；
- branch replay 在读取 gem5 `mispredicted` 前完成，逐事件保持 predict-before-update；
- branch sidecar 的 PC/kind/taken/target、branch ordinal 和 UOP index 全量对齐；
- 训练/推理 predictor config hash、replay version 和 cold/warm policy 完全一致；
- window 边界和多 GPU lane 不重置或重复更新 predictor state；
- 不支持的 predictor family 硬失败，不回退 neural head；
- 新增 encoder 为零时，v30 与 B0 的 timing tensor 在数值容差内一致；
- branch causal-history 在随机窗口边界与从 trace 起点连续 replay 的结果一致；
- true-order/predicted-order GSS divergence 有逐事件记录，不能只报告 aggregate hit rate。

### 12.2 精度

- Redis heldout C4/C8/C16/C32 同方向改善；
- Redis base、memory-random、memory-seq、compute/branch 不系统退化；
- 逐 workload、逐 core 报告 signed CPI error；
- proxy hit-path 对 Ruby 的 event-level accuracy 明显高于现有 LRU shadow；
- leave-one-uarch-out 优于只提供 uarch scalar feature 的基线；
- 改善不能依赖 workload ID、core ID 或地址 identity shortcut；
- configured replay 的 branch PMU 保持现有 event F1、window exact 和 count/rate 精度；
- B1/B2/B3 相比 B0 分别报告 timing 改善或退化，不能用 branch PMU 精度替代 CPI 验证；
- B1/B2/B3 和 G1/G2/G3 分项报告，联合 BG 不能替代单机制归因；
- GSS access-only/FiLM 通过前，不把 GSS 接入全 token cross-core gate。

### 12.3 性能

- Serial `cache_state_update_p95 < 2 ms/step`；
- Serial 总吞吐下降目标小于 3%；
- 单 trace 多 GPU 总吞吐下降目标小于 5%；
- canonical state 有固定或可审计上界；
- 不复制 `state_size * GPU_count`；
- 不把完整 cache tag/state tensor 传入 GPU；
- state feature builder 不使用 per-access Python object；
- branch sidecar 读取与 `BranchReplayEncoder`/`BranchHistoryEncoder` 的吞吐开销单独
  报告；目标是不引入 rollout-time predictor 状态同步或 GPU 间通信。

## 13. 明确的非目标

- 不在 TCSim 内运行完整 gem5/Ruby；
- 不声称 functional commit order 等价于真实 issue/fill order；
- 不把 TSim 当前 LRU shadow 直接称为真实 gem5 cache；
- 不用 cache proxy 替代 OOO/MLP/criticality 学习；
- 不用 256/512 窗口大小代替长期状态；
- 不因为 proxy 参数化就跳过多微架构训练；
- 不在第一阶段实现 path/MSHR/TLB/coherence/DRAM 的完整 timing 状态机；
- 不训练 neural branch head 去复刻已支持的 configured predictor；
- 不把 gem5 `mispredicted`、真实 recovery latency 或 wrong-path oracle 当作输入；
- 不把 correct-path replay 宣称为 cycle-exact wrong-path predictor simulation。

## 14. 最终推荐架构

```text
Functional trace + target uarch profile
                 |
        +--------+---------------------------+
        |                                    |
        v                                    v
configured branch replay -> v29 static/full-QKVR trunk (exactly once)
                                      |
                                      v
                               base token state
                                      |
                     provisional commit-cycle projection
                                      |
             +------------------------+-----------------------+
             |                                                |
 retained absolute deadline                         new-tail provisional time
             |                                                |
             +------------> canonical shadow preview <--------+
                                      |
                              compact GSS features
                                      |
                         GSS adapter + single timing head
                                      |
                       final retirement/commit cycle
                                      |
                           global-time scheduler
                                      |
                        accepted functional prefix
                                      |
               final-time canonical replay + deadline retention
```

这套 v30 架构把可以确定计算的 cache mapping/replacement 交给 GSS 显式状态机，把不能从
functional trace 唯一确定的 OOO exposure、MLP、NoC/DRAM timing 和最终 commit
行为交给模型；分支 miss incidence 则交给同源 configured replay，模型只学习它的
timing 可见性。它比纯模型同时记忆长期 cache 历史和分支预测器更容易泛化，也比在推理
中嵌入完整 gem5 更符合 TCSim 的吞吐目标。
