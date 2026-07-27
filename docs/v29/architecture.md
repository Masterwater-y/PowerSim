# TCSim v29 系统、模型与推理框架设计

## 1. 目标与边界

v29 预测多核程序在目标微架构上的退休时间、进度和 branch miss，并在部署侧用预测结果
推进一个全局虚拟时钟。核心约束是 **functional-only input**：真实 commit tick、真实 cache
hit/miss、mispredicted、oracle cursor 和 CPI 只能作为 label/audit，不能进入模型或下一步
上下文。

v29 不是 gem5 替代品的 cycle-accurate 状态复制。它学习在固定 functional trace、目标
uarch profile 和当前跨核 functional context 条件下的时间分布，并以闭环 rollout 产生
ROI cycles、CPI 和 PMU 估计。

## 2. 端到端数据流

```text
v28 workload binary
  -> gem5 O3 + Ruby MESI_Three_Level + TaoTrace
  -> records.micro.jsonl + labels.micro.jsonl + roi_boundaries.jsonl
  -> aligned per-core Parquet
  -> v29 packed trace cache + non-leaky manifest
  -> common-time sequence dataset
  -> StaticTokenEncoder + Functional full-QKVR + timing/branch heads
  -> checkpoint-3
  -> one-global-time free rollout
  -> report.json / report.txt / per-trace logs
```

raw trace schema 是 `v28.1-branch-roi-percore`，packed dataset 是
`global-time-v29-packed-3`，checkpoint 是 `tcsim-v29-checkpoint-3`。三层任一不匹配都应
硬失败，不能自动猜测兼容。

## 3. Trace 与样本

### 3.1 每核功能流

一个 token 是一条退休 UOP。每核按 functional UOP index 排序，K 固定为 256。TaoTrace
保留实际执行路径所必需的 branch direction/target/history，但 predictor 是否预测失败只
进入 label。

资源地址不会直接嵌入模型。builder 用 `uarch_profile.json` 和 gem5 等价 decoder 把
地址转换为 exact equality keys（line、set、bank、row 等），再派生 pressure、fanout 和
冲突关系。exact key 只存在于 cache/context builder，不进入 learned embedding。

### 3.2 Common-time grid

训练样本在共同真实时间上每 64 cycles 取样，horizons 为：

```text
16, 32, 64, 128, 256, 512, 1024 cycles
```

对每个活跃核，输入从该真实时刻对应的 functional cursor 开始取 256 UOP。第一个目标
gap 是从共同时间到首个未退休 UOP 的剩余时间；后续 gap 是相邻 commit tick 差。这样
no-commit 核的 head age 不会在连续样本中重复充值。

block size 默认 65536 cycles。train/validation block 之间保留最大 horizon guard；尾部
不足完整 256 UOP 的窗口不进入训练/validation。sequence length/stride 都为 4，用连续
样本的累计 loss 约束同符号漂移。

### 3.3 Manifest 划分

- `train`：seed0 的 16 个 base workloads；
- `validation`：seed0 base 的独立时间 block，只用 c4/c8/c16/c32；
- `development_heldout`：seed0 的 7 个 heldout business variants；
- `seed0_inference`：seed0 全量推理集合；
- `deployment_inference`：seed1 c4/c8/c16/c32，不进训练和 checkpoint selection；
- `final_untouched`：预留，当前为空。

训练 sampler 做 trace-equal balancing，防止长 trace 支配梯度。最终报告按 workload 等权，
不以 pooled UOP 数掩盖长 workload 之外的误差。

## 4. 模型输入合同

当前 feature schema：

```text
v29-base12-branch9-resource5-dynamic8-state5-summary38-relation22-llcbankset2
```

| 分组 | 维度 | 生命周期 | 语义 |
|---|---:|---|---|
| base categorical | 12 | static | op class、依赖、memory kind、reuse/stride、macro position、局部历史 |
| branch categorical | 9 | static | branch kind、actual taken/successor、committed history、functional reuse |
| resource categorical | 5 | static | paddr validity、row reuse、L1/L2/LLC set pressure |
| dynamic categorical | 8 | 每 context | 跨核 line role、set/bank/channel fanout、same/different-row pressure |
| state float | 5 | 每 step | head age、距上次 commit、ROI age、cold-start、active-core fraction |
| chunk summary | 38 | 每窗口 | 指令/memory/branch 比例、working set、entropy、resource 分布 |
| relation | 22 | 每 context | shared/read-write、line/set/bank/row 竞争与跨核覆盖 |
| uarch | 29 | per trace | 核宽度/队列/cache/TLB/MSHR/DRAM 的 log-scaled profile |

禁止输入包括：core/workload ID、完整 PC/paddr、nominal set/bank/channel ID、真实 tick/CPI、
真实 cache/coherence outcome、mispredicted、预测后的 scheduler label 和 oracle cursor。

## 5. 模型结构

### 5.1 Static encoder

30 个 static categorical fields（12+9+5）分别 embedding，包含 functional position
encoding，经投影得到 `d_static=768` token。部署时按 trace/core/cursor/uarch/checkpoint
缓存；active peer 变化不应使 static token 失效。

### 5.2 Functional interaction / full QKVR

dynamic categorical embedding、38-d summary、22-d relation、29-d uarch 和 5-d state
投影到 `d_dyn=960`，与 static token 融合。8 个 interaction blocks、15 heads、FFN=3840：

- local Q/K/V：同核 256 UOP 内 attention；
- cross R：同一 sample 的其他 active core token；
- relation/state 驱动逐通道 cross gate；
- UOP 轴始终 K=256，core 轴展平后用 `sample_ptr` 恢复 sample 边界；
- attention backend 可选 auto/flash/efficient/math，正式默认为 auto + BF16。

每个 block 后保留 per-token state，不做旧版本的纯 mean-pool scalar timing。最终也产生
per-core pooled state供诊断。

### 5.3 Timing head

每个有效 token 输出 raw gap logit：

```text
gap_i = softplus(raw_i, beta=4)
tau_i = FP64_cumsum(gap_0 ... gap_i)
```

因此 `tau_i` 天然非负且单调。attention/MLP 可在 BF16 autocast 下运行，但 K≤256 的
退休前缀用 FP64 累积，避免大前缀上 FP32 ULP 导致表面回退。

每个 horizon h 的 soft prefix probability：

```text
p(commit_i <= h) = sigmoid((h - tau_i) / temperature), temperature=4
progress(h) = sum_i p(commit_i <= h)
```

free fast path 只返回 commit time/gap，不构造部署 scheduler 不需要的 horizon tensors。

### 5.4 Branch head

独立 MLP 对退休 control UOP 输出 miss probability。它与 timing head 共享 contextual token，
但最终头完全独立。训练机会定义为所有退休 control UOP，包括 conditional、direct、
indirect、call 和 return。PMU count 在部署时只对实际消费 prefix exact-once 累加。

## 6. Loss 与训练

基线 loss：

```text
L = 1.00 * commit-time log SmoothL1
  + 0.50 * prefix BCE
  + 0.50 * progress-count SmoothL1 / 256
  + 0.25 * contiguous cumulative drift
  + 0.10 * branch-token BCE
  + 0.10 * branch-count SmoothL1 / 32
```

commit loss 在 `log1p(time)` 空间计算，降低极长 stall 对梯度的统治；progress 与 prefix
约束不同 horizons 的完成数量；cumulative loss 沿连续 common-time sequence 惩罚累计
偏差；branch token/count 同时约束概率与 PMU 总量。

优化器 AdamW，学习率 1e-4、weight decay 0.05、gradient clip 5、dropout 0.1。每 1000 step
在确定性、trace-equal 的最多 512 sequences 上选择 checkpoint；每 500 step 保存。当前
60k 训练的最佳 checkpoint 位于 step 59000。

## 7. 部署推理框架

### 7.1 单一全局虚拟时钟

初始化所有 core 的 predicted cursor=0、global time=0。每步：

1. 为每个 active core 从 predicted cursor 读取 K=256 functional UOP；
2. 构建 static/dynamic/relation/state context；
3. 模型输出每核单调相对退休时间 `tau[c, i]`；
4. 在每核第 `target_stride` 个有效 UOP 取候选时间，所有核取最小值作为 `delta`；
5. `delta` 只受 `max_step_cycles` 向下截断，`min_step_cycles` 是告警阈值，不强抬步长；
6. 每核消费 `tau <= delta` 的最长有效前缀；
7. exact-once 累加 UOP、macro、branch，更新 cursor/state/global time；
8. 真实 label 只在状态转移后用于 error/drift audit。

默认 full evaluation 的 `target_stride=256`、`max_step_cycles=1024`、
`max_no_progress_steps=64`。旧文档/配置中的 stride=32 是保守训练期默认，正式 packed3
报告使用 s256；运行命令必须显式记录实际值。

### 7.2 Oracle 与 free 的隔离

- `oracle_one_step`：用真实公共时刻和 cursor 诊断 timing/progress/branch head；
- `free`：下一上下文只用预测 cursor，是部署 headline；
- `oracle_drift_diagnostics`：free 转移后读取真实时间数组测漂移，不得影响调度。

模型 input dict 进入 runner 前会移除/拒绝 oracle-only keys。label-free functional cache
提供更强的物理隔离，用于实际部署。

### 7.3 Cache 和热点路径

三层 cache：OS page cache、每核最后窗口的 CPU cache、每核最后 static token 的 GPU
cache。两层应用 cache 都是有界的，并在新 trace 清空。context builder 使用只读 `.npy`
mmap、NumPy 连续切片、批量 pressure/summary/cross-core relation 和 `torch.from_numpy`，
避免逐 UOP Python object。

计时拆为 context、predict/model、D2H、scheduler；context 再拆 select、window、cross-core、
state+targets、tensor、overhead。性能优化必须通过逐元素等价和同 checkpoint/cursor 输出
一致性测试。

### 7.4 并行层次

- 常规 8-GPU：不同 trace 分片，每 GPU 一个 evaluator 进程；
- 单 trace parallel：unconditional 或 speculative 重叠窗口；
- context lane：默认 spawn process，绕过 GIL，各 lane 独立 last-window cache，共享 OS
  page cache。

单 trace parallel 会改变 model_forwards、ownership 和 resume contract，默认仍使用 serial。

## 8. 输出与指标

headline 必须是完整 full-ROI、workload-equal 指标，并同时保留：

- micro/macro ROI-CPI relative error；
- per-core endpoint、makespan、MAPE quantiles、signed bias；
- branch miss count/rate、AUC/Brier/ECE；
- exact-once counts、overshoot、no-progress、monotonic violations；
- cursor-interval offset 和 slope；head residual 只解释为下一退休距离；
- UOP/s、steps/s、GPU peak、context/model/D2H/scheduler 时间和 cache hit/miss。

不同核正负误差可能在 pooled CPI 中抵消，所以 aggregate CPI 正确不等于闭环 trajectory
正确。

## 9. 基线与实验分支

默认 E0：`long_history_dim=0`、neural branch head、packed3 checkpoint。以下均不是默认：

- E1 global long-history residual：改善 Redis，但严重损害 SIMD/branch 和总体结果；
- E2 frozen memory correction：负迁移受控，但总体改善约 0.059 percentage point；
- LLMSim semantic/LoRA adapter：外部研究快照，依赖 Qwen 和 semantic cache；
- v30 branch replay：后续 predictor functional replay 研究。

切换实验配置会改变 cache/checkpoint/evaluation contract，结果目录必须使用独立名字。
