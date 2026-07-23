# v27 灾难性 CPI 误差：根因分析与修复方案

日期：2026-07-10

分析对象：

- 评估总结：`docs/eval_v27_ss_tw5000_20k_seedB_c04_c08_c16_c32_summary.md`
- checkpoint：`ckpt/v27_ss_tw5000_8l_t32768_bs1_20k_20260709_015028/best.pt`
- 训练 cache：`data/windows_v27_ss_tail_local_c01_c04_c08_c16_c32/windows.maxlen32768.tensor_cache`
- seedB 部署评估：c04/c08/c16/c32 共 64 个 workload-core cell

后续 fixed-chunk、resident slow chunk、并行 temporal solver 的详细语义和系统审查见
`docs/v27_functional_temporal_graph_design_review.md`。该文档细化并修正本文 7.4--7.5 的高层描述：
fixed chunk 是 functional accounting 单元，不是同步多核时间窗口；离线生产主路径优先采用 blockwise
temporal graph，顺序 event queue 仅作为 reference/关键子图投影器。

## 1. 结论

当前灾难性误差不是一个单独的“模型容量不足”问题，而是以下三个问题叠加：

1. **训练窗口由真实 `commit_tick` 对齐，窗口长度近乎直接泄漏 CPI；部署窗口长度却由上一窗预测 CPI 决定。**
2. **部署使用预测 CPI 累计每核时钟、切下一窗并更新 shared state，但模型完全看不到这个预测时钟及其漂移。**
3. **训练 shared state 是 true-time teacher state，部署是 pred-driven state；一旦时序错，切窗、访存排序和 owner/sharer 状态会形成正反馈。**

这条闭环可写成：

```text
当前窗 CPI 预测误差
  -> pred_start_cycle 累计误差
  -> 下一窗每核 UOP 数和 functional cursor 变化
  -> 不同真实 phase 被错误地拼在同一窗
  -> 跨核访存顺序变化
  -> owner/sharer/history proxy 变化
  -> 下一窗 CPI 再次偏移
```

最强反事实来自 `W_chase_dram@c16`：

| 模式 | aggregate pVr | core MAPE p90 | start error p90 |
|---|---:|---:|---:|
| 当前 pred-driven | 171.99% | 1866.95% | 4,837,897 cycle |
| label clock/planner/state 反事实 | 30.05% | 55.96% | 6,742 cycle |
| 旧 v26 no-shared-state，同一 trace | 5.91% | 22.70% | 145,358 cycle |

label 反事实把相对误差幅度削掉了约 82.5%，把 start p90 误差削掉约 99.86%。这证明
pred-driven 反馈是 c16 chase 灾难的主要放大器；剩余 30% 说明静态校准、输入状态和模型结构仍有问题。
该开关同时改变 clock、planner 和 shared-state replay，因此还不能区分三者各自贡献，后续必须拆成独立控制量。

不同 outlier 的主因并不完全相同：

- `c16 chase`：后段 rollout/shared-state 闭环发散为主；train/eval 真值几乎一致。
- `c32 search`：冷启动错配为主，前 5% 贡献约 92% 的最终净超额。
- `c32 stream/chase`：真实 phase/trace-length 分布偏移与 rollout 共同作用。
- `c04 false-sharing`、`ads-ranking`：数据边际接近，主要是持续校准误差和窗口/状态分布错配。

因此，**只扩数据、只加 pairwise/rank loss、只换更大模型或只接 C++ shared_system 都不能解决主问题。**

## 2. 约束和红线

部署输入只能来自 functional trace 及其可派生量。允许的部署输入包括：

- 指令、PC、寄存器依赖、branch direction/target、load/store/atomic/fence；
- 地址、cacheline/page、reuse/stride/working-set、same-line reader/writer 关系；
- functional cursor、剩余 UOP、active core、thread lifecycle、可识别的 barrier/join/spin；
- 模型过去的预测、预测时钟、预测不确定度；
- 由上述信息在线维护的有限容量 proxy state。

`commit_tick`、真实 CPI、真实 miss/coherence/MSHR/PMU 可以作为训练 label 或离线诊断，但不能：

- 决定部署训练样本的窗口边界或每核窗口长度；
- 生成最终部署模型的输入状态；
- 在部署时纠正时钟或重放顺序。

尤其不能简单地把 cache 中保存的 true `t_start_rel` 接回模型。正确做法是给模型输入
**pred-derived relative time state**，并让训练覆盖这个 state 的错误分布。

## 3. 当前方案的真实实现

当前实验更准确的名称应是 `v27_ss_proxy_teacher`，不是设计文档中的完整 v27。

### 3.1 数据

- raw：旧 seedA 的 16 个 workload；core count 为 c01/c04/c08/c16/c32；只有 A0。
- 样本：150,936 个 teacher TQ windows。
- core-count 样本数：27,409 / 31,699 / 30,087 / 30,807 / 30,934。
- 新 v27 workload cube、multi-uarch、deployable rollout dataset 和 scheduled mix 均未进入该 checkpoint。
- 训练/验证是同一 cache 的 sample-level 随机 95/5 split。

TQ builder 在 `data/build_windows.py:2807-2895` 中用真实 tick 定义公共
`T_start/T_end`。每个样本在同一真实时间跨度中，慢核自然包含更少 UOP，快核包含更多 UOP。
真实时间还用于 teacher shared state。

### 3.2 输入和 shared state

每条 UOP 共有 22 个离散字段：

- v26 functional clean14；
- 8 个 lagged owner/sharer/history proxy 字段。

side feature 为 48 维：34 个 functional side feature + 8 个 shared-core feature +
6 个 shared-global feature。global feature 为 19 维。

当前 `SharedStateFeatureEngine` 不是 cache simulator：

- line/core set 不淘汰；
- `local_present` 表示历史触碰过，而非仍在 cache；
- events/unique lines/active lines 单调增长；
- `ss_core_mem_ema` 只在 memory event 上用 value=1 更新，最终接近常数 1；
- sharer 数 capped at 7，c08/c16/c32 高 fanout 饱和；
- fixed alpha 按全局 event 衰减，core count 改变时物理时间含义也改变。

训练时，state 按真实 `commit_tick` 推进；部署时，`model/shared_state.py:485-544` 使用：

```text
t_pred = pred_start_cycle[core] + pred_cpi[core] * local_uop_index
```

排序当前窗全部 memory events，再更新下一窗 state。

### 3.3 模型

checkpoint 配置：8 层、d_model=320、8 heads、FFN=1280，总参数 25,509,209。

真实结构是：

```text
UOP field embedding
  + learned absolute position
  + per-core condition
  + global condition
  -> local self attention + all-other-core cross attention
  -> 每核所有 UOP mean pooling
  -> log CPI + 6 个 PMU rate head
```

有两个容易误判的点：

1. 结构化 v26/v27 模型没有 query token，也没有 tail-local readout；`query_placement=tail_local`
   只影响遗留 token-budget 估算。模型最后是 mean pooling。
2. eval 虽计算 `pred_start_cycle/t_start_rel`，但 model forward 不接收它。训练 cache 也保存
   `t_start_rel`，`train/dataset.py:704-714` 的 collate 却丢弃了它。

模型使用 32768x320 learned absolute position table，共 10,485,760 参数，占总参数 41.11%。
训练集 max per-core length 的 p50/p90/p99/max 为 312/1100.5/3612/16902；16903--32767
完全未训练，大部分高位置也极少访问。

### 3.4 Loss

checkpoint 的实际 loss 仍是 v26 loss：

```text
1.0 * core log-CPI Huber(delta=0.3)
+ 0.5 * top-20%-core CPI loss
+ 0.4 * all-pair log-CPI gap
+ 0.2 * window sum-cycle log Huber(delta=0.1)
+ 0.05 * 6 PMU count-log loss
```

并非 v27 文档拟定的 CPI + branch-only loss。cache/TLB PMU head 尚未退出。

### 3.5 部署

`eval/eval_quota_cycles.py:2003-2193` 的 planner 根据上一窗 `pred_cpi` 和累计
`pred_start_cycle` 给每核分配下一窗 UOP 数。窗口结束后，`eval/eval_quota_cycles.py:3345-3408`
同时用 CPI 推进 clock、规划下一窗、重放 shared state。

最终 aggregate CPI 是：

```text
sum(pred_cpi_c,k * uops_c,k) / sum(uops_c,k)
```

这表示 total core-cycles / total UOP，不等于并行程序 makespan。若最终目标包含 wall-clock
completion time，还必须额外预测和报告 `max(core_end_cycle)`。

## 4. 误差形态

以 workload aggregate 绝对误差 >20% 为灾难阈值，共 10/64：

| cell | signed error | cycle error | 主要形态 |
|---|---:|---:|---|
| c16 chase | +171.99% | +74.81M | 约 70% 后突然发散 |
| c32 search | +87.66% | +8.08M | 前 5% 冷启动主导 |
| c32 stream | -48.57% | -63.26M | 中后段持续低估 |
| c16 ads-ranking | -37.74% | -5.01M | 全程稳定低估 |
| c32 chase | -34.52% | -167.11M | 约 20% 后持续低估 |
| c32 graph | -30.17% | -8.88M | 随进度恶化 |
| c16 search | +26.98% | +1.23M | 前段过高，后续稀释 |
| c08 ads-ranking | -26.07% | -1.21M | 全程稳定低估 |
| c04 false-sharing | -25.97% | -5.67M | 冷启动更差，最终仍低估 |
| c32 ads-ranking | -21.60% | -5.93M | 全程低估 |

c16 的 global pVr=2.73% 是严重抵消后的结果：

- signed net cycle error：+14.29M；
- gross absolute cycle error：138.12M，即总真值 cycles 的 26.37%；
- chase +74.81M 被 false-sharing -51.83M 等负误差抵消。

c32 按 cycle 影响排序应先修 chase、false-sharing、stream；按稳健性则 search 也必须处理。

## 5. 根因证据

### 5.1 P0：oracle TQ 的窗口长度泄漏 CPI

训练 TQ 在固定真实时间跨度 `Delta T` 内切窗，因此近似满足：

```text
n_c ~= Delta T / CPI_c
log(CPI_c) ~= log(Delta T) - log(n_c)
```

而 `log1p_uops_core`、`core_fill_ratio`、`core_split` 明确进入模型；模型内部又重复加入
`log_core_uops` 和 `core_share`。

对最终 cache 实测：

- 跨核 `corr(log uops, log CPI)` 中位数为 **-0.988**；
- **87.6%** 的多核窗口相关系数小于 -0.8。

这使模型可以不理解 trace 机制，只从窗口长度推慢核。部署时关系反过来：

```text
n_c,k+1 = planner(previous predicted CPI, predicted clock)
```

于是预测慢 -> 下一窗分得短 -> 模型从短窗再次预测慢，形成自证循环。窗口长度在训练时是
oracle shortcut，在部署时是模型自身历史输出，不是新的观测。

### 5.2 P0：模型看不到累计时间误差

当前模型输入没有：

- `pred_start_cycle`；
- lag vs min/mean/median；
- previous CPI/EMA/trend；
- planner mode、planned count、predicted tail skew；
- cursor progress、remaining UOP、active-core lifecycle；
- state confidence/uncertainty。

因此每核时钟误差：

```text
E_c,k+1 = E_c,k + n_c,k * (pred_CPI_c,k - true_CPI_c,k)
```

持续积分，但模型没有观测 `E` 的任何 pred-derived proxy，也无法学习稳定纠偏。

训练和部署的时间分布相差数个数量级：

| slice | train true `t_start_rel` p90 | eval true start-skew mean | eval predicted skew mean |
|---|---:|---:|---:|
| c04 false-sharing | 98 | 1,735,822 | 75 |
| c16 chase | 223 | 146,488 | 1,517 |
| c16 ads-ranking | 529 | 39,618 | 227 |
| c32 chase | 2,390 | 1,502,723 | 56,469 |
| c32 stream | 1,326 | 1,578,066 | 40,144 |
| c32 search | 481 | 49,057 | 289 |
| c32 graph | 910 | 97,869 | 494 |

planner 在预测坐标中认为各核已对齐，真实 functional cursor 对应的执行时间却严重错位。

### 5.3 P0：teacher-state 与 pred-state 不一致

训练 state 使用 true `commit_tick` 排序所有历史访存；部署 state 使用一个窗口一个 scalar CPI，
并假定核内所有 UOP 匀速。即便窗口平均 CPI 完全正确，DRAM burst、atomic phase、barrier/spin
也会使真实窗口内时间非线性，跨核事件顺序仍可能错。

owner/last-writer 是离散状态。两个同 line event 的预测时间只要交换，后续 owner、sharer、conflict
类别就可能整体翻转，所以小 timing error 不一定产生小 feature error。

`W_chase_dram@c16` 的反事实已证明整套 feedback stack 是主要放大器。旧 v26 在同一 trace 上只有
5.91%，说明“functional trace 本身完全不可预测”不是这里的主要解释。

### 5.4 P0/P1：数据不是部署分布

当前 cache 仍是旧 16-workload seedA 数据，不是 v27 workload-set 文档规划的数据。

target-conditioned sampling 还改变了自然先验：

- CPI>=30、CPI<=0.35、CPI in [3,10] 被 hard-keep；
- 再按由真实 CPI 定义的 variant 做 cap；
- cap 只是上限，不保证稀有区间的最低覆盖。

例如：

- c16 chase 的 `[3,10)` 只有 59/2101；
- c32 chase 的 `[3,10)` 只有 16/2993；
- c32 stream 的 `[3,10)` 约 44/3213。

selected cache 的 UOP-weighted CPI 与自然 seedB ROI 差异很大：

| slice | selected-cache CPI | seedB ROI CPI |
|---|---:|---:|
| c16 chase | 5.023 | 3.288 |
| c32 chase | 40.47 | 21.253 |
| c32 stream | 18.44 | 5.336 |
| c32 search | 2.403 | 0.386 |
| c32 graph | 1.297 | 1.119 |
| c04 false-sharing | 7.370 | 7.228 |

高 skew c32 candidate 还被 32K budget 系统性丢弃：

| workload | drop_bad / 5001 |
|---|---:|
| ads-ranking | 3334 / 5001 = 66.7% |
| search | 2784 / 5001 = 55.7% |
| graph | 1703 / 5001 = 34.1% |
| fp-compute | 636 / 5001 = 12.7% |
| stream | 284 / 5001 = 5.7% |
| chase | 352 / 5001 = 7.0% |

这些被丢的正是“某慢核凑 256 UOP 时，其他核使总上下文爆炸”的窗口，也是部署最需要学习的窗口。

TQ 只采所有 core 共同存活区间：

```text
t_lo = max(first_tick_c)
t_hi = min(last_tick_c)
```

部署却从每核起点跑到各自终点并逐核退出。因此 startup、drain、C=31/30/...、尾段状态均 OOD。
seedA c32 被排除在共同区间之外的 UOP 比例约为：stream 13.9%、chase 14.2%、graph 20.1%。

### 5.5 c32 还存在采集和地址 domain shift

c32 train 使用 seedA、约 500K/core、FF-atomic；eval 使用 seedB、约 700K/core、O3 timing from start。
train config 为 `atomic_noncaching`，eval config 为 `timing`。FF-atomic 在首个 WORKBEGIN 切到 O3，
Ruby cache 从 cold state 开始；非 FF eval 在 ROI 前已有 timing warm state。

自然 full-trace CPI：

| workload@c32 | trainA | evalB |
|---|---:|---:|
| search | 0.609 | 0.386 |
| stream | 2.483 | 5.336 |
| chase | 16.808 | 21.253 |
| graph | 1.394 | 1.119 |
| ads-ranking | 0.908 | 0.888 |
| false-sharing | 60.814 | 63.092 |

因此 stream/chase 有真实 phase 偏移；c16 chase 的 train/eval 为 3.268/3.288，几乎相同，不能用
seed shift 解释。

模型还直接输入 8192 桶 absolute line hash。train/eval line-hash JSD 在 c32 search/graph 分别约
0.690/0.602，而 opclass、memkind、stride 的差异很小。search/graph 存在地址身份 shortcut OOD；
c04 false-sharing 的 line-hash JSD 近零，不能用它解释。

### 5.6 P1：模型 readout 和跨核关系不适合 rare stall

当前 mean pooling 会把少量 DRAM/remote writer/atomic/barrier 信号淹没在大量普通 UOP 中。
full cross attention 又要求模型从数千到数万 token 中自己发现 same-line、reader/writer 和同步关系，
没有 relation bias 或显式 edge。

需要注意，当前 outlier 不全是“spread 塌缩”：

- c32 stream 的 pred/label CV ratio=0.58，确实 spread 不足；
- c16 chase ratio=2.06，是少量核过度发散；
- c32 search ratio约 1 且 corr=0.48，但整体 scale 高估 87.7%。

所以只增强 pairwise/rank loss 会修错问题。

### 5.7 P1：loss 和选模没有优化 rollout 风险

当前 loss 的主要缺口：

- `window cycles` 是 log Huber(delta=0.1)，乘 0.2 后远尾 log-cycle 梯度上限约 0.02；
- 每个 core/window 近似等权，与最终 UOP/cycle contribution 不一致；
- all-pair loss 在 c32 被大量近似相等 pair 稀释；
- 没有 prefix timing、relative end-time、K-step cumulative drift 或 state corruption loss；
- 仍共享 6 个 cache/PMU auxiliary heads；
- checkpoint 由随机 teacher-window val loss 选择，而不是 full rollout worst-case 选择。

随机 split 也严重乐观：70.4% val window 与某个同-slice train window 在时间上重叠，29.6% 的
IoU>=0.5，最近 train label 的相对差异 p50 只有 1.7%。`val_loss=0.0209` 不能证明 seed、phase、
address、active-core 或 rollout 泛化。

### 5.8 指标口径需修正

当前 `win_mape_cpi_uop` 实际在 core loop 中逐 core 累加，64 个结果上与 `core_cpi_mape` 完全相等；
它不是真正的 per-window aggregate MAPE。

最终 `sum_cyc_label` 直接由每核 full-trace endpoint 差重算，因此 `label_vs_roi=0` 不能证明逐窗
`CPI*n_uop` 可加。per-window label 使用 `(last_tick-first_tick)/n`，连续窗口边界 interval 不严格
telescoping。后续应增加真正的 oracle window-cycle sum，并改用可加的 boundary-to-boundary cycle label。

## 6. 各灾难点的主因判断

| cell | 主因 | 次因 | 判据 |
|---|---|---|---|
| c16 chase | pred clock/planner/state 闭环 | static calibration、state proxy | 70% 后突发；label 反事实 171.99% -> 30.05%；train/eval 真值相同 |
| c32 search | cold-start/FF/seed 状态错配 | address hash OOD、55.7% budget drop | 前 5% 贡献约 92% 净超额；旧 v26 正常 |
| c32 stream | trace-length/phase shift | temporal rollout、tail OOD | train/eval 2.483 -> 5.336；中后段持续低估 |
| c32 chase | phase shift | rollout/static under-calibration | train/eval 16.808 -> 21.253；cycle 影响最大 |
| c32 graph | budget-drop + address OOD | rollout/shared state | train/eval主体接近，但随进度恶化 |
| c16/c08 ads | static conditional calibration | oracle/deploy cuts mismatch | 全程稳定低估，非突发 |
| c04 false-sharing | temporal/state/model mismatch | current-window interaction不可见 | train/eval/地址分布接近，仍持续低估 |

## 7. 修复方案

### 7.1 P0：先把诊断变量拆开

在任何新 20K 训练前，eval 必须把当前耦合的 `planner_state_source` 拆成独立开关：

1. `cut_source = fixed_functional | pred | oracle`；
2. `clock_source = pred | oracle`；
3. `ss_replay_order = functional | pred_scalar | pred_chunk | oracle`；
4. `ss_input = off | functional_proxy | pred_proxy | teacher`。

至少对 c16 chase、c32 search/stream/chase/graph、c04 false-sharing 跑小型 factorial。否则 label 模式
同时改变三个变量，无法定位收益来自哪里。

同时修正/新增指标：

- 真正的 per-window aggregate MAPE p50/p90/p99；
- additive oracle cycle sum；
- cold/startup/common/drain 分段；
- 按 active-core count 分段；
- signed cycle contribution 和 gross absolute cycle error；
- normalized cumulative drift；
- slowest top-k 相对随机基线 `1/C` 的 lift；
- makespan（若是最终目标）。

所有 outlier eval 默认保存逐窗/逐核 dump；当前 64 个正式日志的 `window_dump_path` 全为 null。

### 7.2 P0：构造部署一致 rollout dataset

这是最优先的数据改动，不应先继续增加 teacher TQ 样本。

推荐流程：

1. 用 fixed functional chunk 或当前模型做 bootstrap；窗口边界不读取 true tick。
2. 在 seedA functional trace 上运行与部署相同的 planner、active-core exit 和 shared-state replay。
3. 保存 rollout 实际访问到的 cursor、pred clock、planner state、corrupted shared state 和 functional slice。
4. slice 确定后才离线查询 `commit_tick` 生成 cycle/CPI label。
5. 用新模型再 rollout，迭代 2--3 轮 DAgger/scheduled sampling。
6. 对 previous CPI、clock、shared state 注入可控噪声，覆盖模型尚未自然访问到的错误状态。

训练组合建议同时包含：

- fixed-UOP functional anchors，防止长度 shortcut；
- pred-driven rollout 主样本；
- high-error/corrupted-state hard buffer；
- 少量 teacher sample 只作蒸馏或 upper-bound，不作主分布。

### 7.3 P0：把可部署的预测时间状态真正输入模型

加入以下 per-core 连续特征，并做逐字段 normalization：

- normalized `pred_start - min/mean/median(pred_start)`；
- previous log-CPI、EMA、trend、uncertainty；
- planned UOP count/share、planner mode、nmin_eff、pred tail skew；
- functional cursor fraction、remaining UOP、active/starting/draining；
- state age/confidence。

建议归一化形式：

```text
time_lag_c = log1p((pred_start_c - min(pred_start)) / (pred_horizon + eps))
```

这些值全部来自历史预测和 functional trace，符合部署约束。必须用 rollout/corrupted state 训练；只在
teacher TQ 上接 true relative time 会重新引入泄漏。

### 7.4 P0：从单窗 scalar CPI 改为单调 chunk timing

每核把当前 functional window 切成固定 32/64/128-UOP chunk，模型预测正 cycle increment：

```text
delta_cycle_c,b = softplus(raw_c,b)
window_cycle_c = sum_b delta_cycle_c,b
CPI_c = window_cycle_c / n_uop_c
```

训练用 chunk boundary 的 `commit_tick` 监督 `delta_cycle`；部署只使用预测值。memory event 的时间由
chunk prefix 累积并在 chunk 内插值，不再假设整窗匀速。

低成本实现可采用 two-pass：

1. pass1 预测 chunk timeline；
2. 在 shared-state shadow copy 上按预测 timeline replay 当前窗；
3. pass2 使用 current-window functional state/refined relation 预测最终 timing；
4. 只 commit pass2 state。

false-sharing、DRAM burst、atomic phase 会直接受益。

### 7.5 推荐的长期推理框架：fixed-chunk 事件驱动

保留 soft-nmin planner 的最小修复仍有“输出决定下一输入长度”的反馈。更彻底的方案是：

```text
每核维护 functional cursor 和 predicted virtual time
选择 virtual time 最小的一组 active core
每个被选 core 固定取 K 个 functional UOP
模型预测每个 chunk 的正 cycle increment / subchunk timeline
按预测时间更新 relation/shared state
推进 cursor，重复
```

其他 core 提供固定 lookahead summary/relation memory，不通过预测 CPI 改变本次 chunk 长度。这样：

- 标签不再通过长度泄漏；
- 一次 CPI 误差只影响 virtual time/order，不会同时改变该核下一次输入长度；
- sequence loss 和事件调度更自然；
- 上下文预算可由固定 K、active group 和 sparse relation 控制。

### 7.6 P1：模型结构

建议按以下顺序改：

1. learned absolute position -> RoPE/relative position/normalized functional progress；
2. mean pool -> typed pools（compute/memory/shared/branch/tail）+ per-core learned timing query；
3. full cross attention -> core memory + relation-aware sparse cross attention；
4. 显式 same-line/same-page/read-write/atomic/sync edge；
5. local/cross/state 分支加 learned gate；
6. absolute line hash 只用于引擎内部实体匹配，模型侧用窗口内 entity ID、first-touch rank、relation edge，
   或至少使用 per-run random salt/hash dropout；
7. 输出采用稳定的 residual decomposition：

```text
log_CPI = stateless_functional_baseline + bounded_gate * state_interference_delta
```

当前 v26 在 c16 chase 上明显更稳定，因此 no-state baseline 应作为 anchor；低置信/OOD 时自动收缩 state residual，
不能让错误 shared state 单独决定 20x CPI。

连续 side/global feature 应逐字段保存 mean/std、clip 后再输入；不要依赖一个跨异质量纲向量的 LayerNorm。
删除重复的 n_core/uops/global 字段。

### 7.7 P1：loss

新目标应直接对应 rollout：

```text
L = L_core_chunk_cycle
  + alpha * L_window_aggregate_relative_cycle
  + beta  * L_prefix_timeline
  + gamma * L_relative_end_time
  + delta * L_K_step_cumulative_drift
  + eta   * L_tail_CVaR_or_groupDRO
  + lambda * L_branch
```

具体原则：

- 主预测改为 additive cycle increment，CPI 作为派生量；
- aggregate loss 直接优化 relative core-cycle error，不使用强饱和的小-delta log Huber；
- K-step 以 4/8/16 窗 rollout 监督累计 virtual time 和相对 lag；
- slow core 使用 listwise soft target 或少量 slow/fast anchor pair，不对全部 C^2 pair 平均；
- 同时报 natural UOP-weighted risk 和 balanced/CVaR risk；rare sample 过采时带 importance weight；
- 使用 workload-mechanism/core-count/phase group DRO，防止 c16 global cancellation；
- cache/TLB/coherence PMU heads 退出；branch 仅在增加 functional branch-history proxy 后保留；
- 输出 uncertainty/quantile，用于部署 gate 和 adaptive chunk size。

checkpoint 只能由完整 pred-driven rollout 指标选，不再由随机静态 val composite loss 单独选。

### 7.8 P1：shared-state proxy

在 timing 一致性修复前，不应优先投入复杂 C++ cache 状态机。否则只是更精确地执行错误事件顺序。

proxy 至少要改为：

- 有限容量和 recency eviction；
- age/EMA 按 per-core event 或 predicted normalized time，而非固定全局 event alpha；
- fanout/sharer bucket 覆盖到 32 core；
- state feature 带 confidence；
- shared-state dropout/corruption training；
- 对预测时间区间重叠的 same-line event 保存 may-owner/may-sharer set 或顺序无关统计，避免一次 hard sort
  决定唯一 owner；
- Python 内部 replay 与外部 C++ mem-event sink 统一同一 chunk-timing 语义。

C++ `peek`/cache/TLB/MSHR 可以在上述接口稳定后替换 proxy，但它属于 P2，而不是当前第一修复项。

### 7.9 P1：部署保护

这些只能止损，不能替代 rollout training：

- stateless baseline + bounded state residual；
- OOD/uncertainty 高时缩小 chunk、做 second pass 或退回 baseline；
- 限制相邻窗口 residual/clock update 的变化率，而非固定全局 CPI 上限；
- 在 functional trace 可识别的 barrier/thread join/start 上重置相对 lag；
- shared state 使用 shadow commit，异常预测不立即污染正式 state。

不能粗暴把 CPI clamp 到统一小范围，因为 c32 false-sharing 的真实 CPI 约 63、c32 chase 约 21。

## 8. 数据集重构

### 8.1 采集

- 至少两个训练 seed，一个整 run/seed holdout；
- train/eval 的 FF、warmup、scale、target/core、trace length 策略统一；
- 显式覆盖 cold/warm、startup/common/drain、任意 active-core count；
- phase workload 按 functional progress 覆盖，不按真实 wall-clock 均匀 anchor；
- 加入真正的 non-uniform/hot-cold 和 producer-consumer 场景。

### 8.2 采样

- per-bin cap 改为 minimum quota + maximum cap；
- natural stream 和 balanced hard stream 分开，loss 通过 importance weight 恢复部署先验；
- 复用部署 dynamic floor，要求每个 slice 的 budget-drop <5%；
- high-skew 样本不能因 256/core 固定 floor 被丢弃；
- manifest 保存 raw root、seed、FF mode、scale、builder args、progress block、git SHA。

### 8.3 split

- 按 seed/run/workload-mechanism/连续时间块 group split；
- 相邻 split 中间留至少一个最大窗口跨度的 guard band；
- train/val 时间区间重叠必须为 0；
- rollout validation 必须完整跑到每核退出。

## 9. 新 v27 raw 数据不能直接开训

仓库在 7 月 10 日已出现：

```text
data/raw_v27_ffatomic_seed0_c{01,04,08,16,32}
```

但它尚未构建 windows，也没有进入当前 checkpoint。实际包含 16 train + 2 heldout，设计文档中的 21 个
训练 workload 仍缺 5 个：`int_mul_dense`、`fp_alu_dense`、`random_LLC`、`coh_read_share`、
`skew_producer_amp`。

更重要的是机制门尚未通过。c01 full-trace CPI 实测：

| workload | CPI |
|---|---:|
| stream_seq_L2 | 1.4854 |
| stream_seq_DRAM | 1.4851 |
| chase_LLC | 26.806 |
| chase_DRAM | 27.011 |
| int_div_dense | 0.7589 |

L2/DRAM pair 基本不可分，int-div 也未达到设计目标 3--5。应先修 workload working set、依赖结构、
warmup/ROI 和采集配置，再过 signature/label separation 门；现在直接重训只会把新的数据问题带进模型。

## 10. 实施顺序和晋级门

### Phase A：诊断和指标（先做）

1. 拆分 cut/clock/ss-order/ss-input 四个 eval 开关。
2. 修正 window MAPE 和 additive label cycles。
3. 对六个关键 cell 跑 factorial 和逐窗 dump。
4. 增加 cold/common/drain、active-count、signed-cycle 报表。

退出条件：能够定量分解 planner、clock、state、static model 四部分误差。

### Phase B：v27.1 稳定化

1. 构建 pred-driven DAgger cache；
2. 加 pred-time/planner/cursor features 和 normalizer；
3. 移除 cache/TLB/coherence PMU auxiliary head；branch head 仅在 functional branch-history proxy 就绪后保留；
4. stateless baseline + bounded state residual；
5. chunk timing + two-pass shadow replay；
6. K-step drift loss。

建议首轮晋级门：

- 任何 workload aggregate pVr <25%；
- workload p90 <15%；
- pred-mode 与 label-mode aggregate error 差 <5 个百分点；
- critical workload 无随进度单调爆炸；
- start drift p90 / 每核 ROI cycle span <5%；
- budget-drop <5%；
- cold/common/drain 各段单独过门。

### Phase C：v28 结构化改造

1. fixed-chunk event-driven scheduler；
2. relative/RoPE position；
3. typed pool + timing query；
4. relation-aware sparse cross attention；
5. set-valued/uncertainty-aware shared state。

### Phase D：新 workload cube

1. 先修并验证新 raw workload 机制；
2. multi-seed、统一采集配方；
3. group split；
4. rollout dataset + natural/balanced 双权重训练；
5. 最后再扩 multi-uarch。

## 11. 不建议的单点方案

- **只加更多 teacher TQ 数据**：会强化长度 shortcut 和 teacher-state 分布。
- **只加 pairwise/slowest loss**：无法修复整体 scale、过度发散和时钟不可观测。
- **只扩大模型**：当前 position table 已占 41%，且主要问题是目标/闭环不一致。
- **只接 C++ shared_system**：事件时间仍错时，更真实的状态机反而可能放大离散顺序错误。
- **直接使用 true `t_start_rel`**：部署不可得，属于新一轮 teacher forcing。
- **全局 CPI clamp**：会破坏真实高 CPI workload。
- **直接训练 7 月 10 日的新 raw**：当前 workload 机制分离度尚未达标。

## 12. 最终判断

当前最值得优先修的不是“再找一个更强的 CPI head”，而是让训练和部署具有同一个状态转移系统：

```text
functional-only cuts/state
+ pred-derived time visible to model
+ rollout/corrupted-state training
+ additive chunk timing
+ sequence-level drift objective
```

其中最关键的结构修复是打断：

```text
true-time window length -> CPI shortcut
predicted CPI -> next window length -> predicted CPI
```

在这个闭环被打断或被 rollout training 正确建模之前，静态 val loss、global aggregate CPI 和更复杂的
shared-system 特征都不足以证明模型可部署。
