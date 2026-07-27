# v29 单 trace 多 GPU 窗口并行

## 1. 目标与范围

现有 `run_free_running()` 对单条 trace 严格执行：

```text
build context -> one GPU forward -> scheduler commit -> next context
```

现有 8-GPU evaluator 按 trace 分片，不能降低单条 trace 的串行
forward 数。本方案在不读取 oracle timing 的前提下，为单条 trace 增加两个可配置的
重叠窗口并行分支：

- `unconditional`：无条件开环窗口并行；
- `speculative`：按窗口起点覆盖关系进行链式投机提交。

`serial` 继续作为默认模式和精度基线。三种模式共用相同的 UOP、macro、branch
exact-once 计数和最终报告合同。

## 2. 公共窗口布局

设：

- `K=256`：模型窗口长度；
- `D`：并行深度，通常等于参与单 trace 推理的 GPU 数；
- `P`：相邻窗口的 functional UOP 偏移，满足 `1 <= P <= K`；
- `A[c]`：本轮开始时 core `c` 的权威 cursor。

一轮同时构造：

```text
window 0 start[c] = A[c] + 0 * P
window 1 start[c] = A[c] + 1 * P
...
window D-1 start[c] = A[c] + (D-1) * P
```

`D=4, P=64` 时，每核相对窗口为：

```text
GPU0 [  0, 255]
GPU1 [ 64, 319]
GPU2 [128, 383]
GPU3 [192, 447]
```

`D=4, P=256` 时窗口互不重叠，每轮理论 functional 覆盖达到
`D*K=1024` UOP/核：

```text
GPU0 [  0,  255]
GPU1 [256,  511]
GPU2 [512,  767]
GPU3 [768, 1023]
```

这仍使用同一轮锚点的开环状态，因此扩大覆盖范围的同时也会放大后续窗口的 context
陈旧程度；它是 `unconditional` 模式的吞吐优先边界配置。

所有窗口都在本轮锚点的部署状态下构造，不读取 oracle cursor/tick。后续窗口的模型
输入因此是有意的开环近似。free-running 直接传输模型的 per-UOP
`retirement_gap`，对有效 token 应用 `1e-6 cycle` 数值下限，再由 CPU FP64
累计生成 canonical `commit_time`。窗口裁剪与交接始终从同一份 gap 重新累计，
避免 `gap -> FP32 prefix -> diff` 往返造成小 gap 丢失。

## 3. `speculative`：链式投机提交

窗口并行预测，但只允许按 `0 -> 1 -> ... -> D-1` 的顺序提交。

window 0 可以跨多次 scheduler transition 持续提交。准备从 window `k-1` 切换到
window `k` 时，当前权威 cursor 为 `C[c]`，该窗口起点为 `B_k[c]`。对每个仍 active
的 core 必须满足：

```text
0 <= C[c] - B_k[c] < valid_count[k,c]
```

其中：

- `C[c] < B_k[c]`：前序窗口没有提交到当前窗口起点，存在功能流缺口；
- `C[c] >= B_k[c] + valid_count[k,c]`：当前窗口已经被完全越过，没有可用预测。

任一 active core 不满足条件时：

1. 若前序 owner window 仍有预测可用，继续用前序窗口推进，而不是立即判 miss；
2. 若前序窗口已经耗尽但仍有 core 未到达 `B_k[c]`，window `k` miss；
3. window `k` 以及同轮所有更深窗口全部丢弃；
4. 已经提交的前序窗口不回滚；
5. 从最新权威 `global_time/cursor[]/last_commit[]` 开始下一轮。

命中时，每核切掉 `[B_k[c], C[c])` 对应的已提交重叠前缀，只提交从 `C[c]` 开始的
scheduler prefix。一个 UOP 可以被多个 GPU 预测，但只能由一个窗口提交。

### 3.1 投机指标

window 0 是每轮锚点，不计为投机。对 `k>0`：

```text
speculative_windows_issued
speculative_windows_accepted
speculative_windows_rejected
speculative_window_hit_rate
full_chain_hits
full_chain_hit_rate
first_failure_depth histogram
```

命中率定义为：

```text
accepted / issued
```

被较浅 miss 连带丢弃的更深窗口已经实际占用 GPU forward，因此计入 `issued`，但不计入
`accepted`。

## 4. `unconditional`：开环 gap lattice

无条件模式不验证中间 cursor 是否覆盖某个窗口起点。它将并行窗口转换为一条覆盖
functional 流的开环 gap lattice。

相对 UOP `r` 的 owner 为：

```text
owner(r) = min(floor(r / P), D - 1)
```

因此：

- window 0 提供 `[0, P)`；
- window 1 提供 `[P, 2P)`；
- ...
- 最后一个窗口提供其起点后的完整剩余 `K` token。

同一 UOP 的非 owner 预测被忽略。每个窗口的 `commit_time` 转换为 gap 后再拼接，因此
不会把不同窗口的相对时间原点直接相加。scheduler 可以在该 lattice 上执行多次状态
转移，直到任一活跃核不再有预测 token；随后从最新权威状态开始下一轮。

该模式保持功能流无缺口和 exact-once 计数，但允许不同 core 在同一步使用来自不同
owner window 的 gap，且不刷新中间 cross-core context，因此是比 speculative 更激进的
精度/吞吐分支。

## 5. 多 GPU runner

每张 GPU 保存一份相同 checkpoint。每轮：

1. 为每个窗口 lane 建立私有 context workspace；trace mmap/metadata 只读共享，
   last-window cache 和 runtime counters 不共享；
2. 默认用 `spawn` 的独立 CPU process 构造至多 `D` 个联合 context；worker 内只构造
   CPU context，不初始化 CUDA，结果以 NumPy 数组返回主进程；
3. `predict_free_many()` 用独立 CUDA device 并行执行模型；
4. CPU 协调器按所选模式提交；
5. 新一轮重新锚定权威状态。

报告区分：

- `steps`：真正提交的 scheduler transitions；
- `model_forwards`：所有实际发射的窗口 forward，包括投机 miss 后被丢弃的窗口；
- `parallel_waves`：并行发射轮数；
- `retired_uops_per_model_forward`：真实提交 UOP / 所有发射 forward。
- `context_build_parallel_wall_seconds`：并行 context 构造的实际墙钟时间；
- `context_build_worker_seconds`：所有 CPU lane 的工作时间之和；
- `context_effective_parallelism`：worker CPU 时间之和 / context 墙钟时间。

## 6. CLI

```text
--window-parallel-mode serial|unconditional|speculative
--window-parallel-devices cuda:0,cuda:1,...
--window-parallel-shift 64
--window-context-backend serial|thread|process
```

并行深度由 devices 数量决定。`serial` 忽略 shift，并继续使用 `--device`。多 GPU
模式默认 `window-context-backend=process`；`thread` 仅用于诊断，因为 c32 热路径主要
受 GIL 和内存带宽限制，线程不保证加速。

示例：

```bash
python scripts/infer_v29.py \
  --ckpt ckpt/.../best.pt \
  --manifest data/v29_global_time_dataset/manifest.json \
  --splits seed0_inference \
  --out logs/v29_single_trace_speculative \
  --mode free \
  --window-parallel-mode speculative \
  --window-parallel-devices cuda:0,cuda:1,cuda:2,cuda:3 \
  --window-parallel-shift 64
```

一次顺序验证 4-GPU `unconditional/speculative` 两种模式：

```bash
cd /data00/yinhaolang/TCSim
GPUS=0,1,2,3 bash scripts/run_v29_window_parallel_4gpu.sh
```

默认 checkpoint 是
`ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt`。先用单条 trace 冒烟：

```bash
MAX_TRACES=1 GPUS=0,1,2,3 \
  bash scripts/run_v29_window_parallel_4gpu.sh
```

`GPUS` 是物理 GPU ID；进程内统一映射为 `cuda:0..cuda:3`。可用 `CKPT`、
`MANIFEST`、`SPLITS`、`CORE_COUNTS`、`WORKLOADS`、`MODES`、`WINDOW_SHIFT`、
`WINDOW_CONTEXT_BACKEND`、`TARGET_STRIDE` 和 `OUT` 覆盖默认配置。

例如只运行四张 GPU 的无条件、非重叠窗口配置：

```bash
MODES=unconditional GPUS=0,1,2,3 WINDOW_SHIFT=256 \
  bash scripts/run_v29_window_parallel_4gpu.sh
```

## 7. 准入检查

至少比较：

1. `serial/unconditional/speculative` 的 complete、remaining UOP 和 exact-once 断言；
2. ROI UOP-CPI、per-core endpoint、makespan、branch count/rate；
3. speculative hit rate、full-chain hit rate、failure depth；
4. steps、model forwards、parallel waves、UOP/s；
5. `D=2/4/8` 与 `P=32/64/128/256`；
6. compute/private、memory-random、memory-seq 和 coherence workload；
7. active-core tail、短 trace 和 label-free functional cache。

并行模式是新的 evaluation/resume contract，不能静默复用 `serial` 或不同
`D/P/mode` 的已有结果。
