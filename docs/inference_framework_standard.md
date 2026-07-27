# TCSim 推理框架标准（v29 及后续版本）

本文规定部署侧全量推理的公共工作流。新版本除非在设计文档中明确说明并完成兼容性验证，否则应复用这里的目录、日志、向量化、缓存、计时、断点续跑和验收约定。

当前参考实现：

- 单 trace 入口：`scripts/infer_v29.py`
- 8 GPU 调度与合并：`scripts/run_v29_eval_8gpu.sh`
- 推理和 scheduler：`tcsim/v29/inference.py`
- mmap、窗口、向量化和 CPU cache：`tcsim/v29/dataset.py`
- 当前 c8/c32 一键启动器：`scripts/tmp/launch_v29_packed3_c08_c32_free_s256.sh`

## 1. 固定语义

部署推理必须使用一个全局虚拟时钟。每次 forward 为每个活跃 core 读取从当前 cursor 开始的 `K=256` 个功能 UOP；模型预测每个 UOP 相对当前时刻的 commit time，scheduler 再推进全局时间和所有 core 的 cursor。

```text
mmap 功能流
  -> NumPy 批量窗口/pressure/summary/跨核关系
  -> CPU tensor
  -> GPU static encoder + timing/branch heads
  -> 只回传 commit_time 与 branch probability
  -> stride scheduler
  -> 新的全局时间和 predicted cursors
  -> 下一次 forward
```

`free` 模式是部署精度和吞吐量的主结果。它的下一步上下文只能由预测 cursor 构造，不能读取 oracle commit tick 作为模型输入。label 只允许在状态转移之后计算 running/final ROI-CPI 等误差。

`oracle`/`oracle_one_step` 是 teacher-conditioned 诊断：在真实公共时刻单步检查 timing、prefix/progress 和 branch head，不代表闭环部署误差。全量部署默认不运行 oracle；需要诊断 head 时再单独开启。

## 2. Scheduler 参数

- `K=256`：一次 forward 中每个活跃 core 的最大前视窗口，属于模型输入合同。
- `target_stride=256`：scheduler 在每个 core 的预测序列中选择第 256 个有效 UOP 的时间，取所有活跃 core 中最早者作为候选步长。它是目标前缀，不是固定推进量。
- `max_step_cycles=1024`：候选步长的安全上限；只向下截断，不把步长强行抬高。
- `min_step_cycles=4`：仅为效率告警阈值，不改变语义，否则可能越过尚未预测完成的事件。
- 实际消费量：每个 core 中满足 `commit_time <= delta` 的有效前缀，因此一次 forward 通常消费约 `active_cores * target_stride` 个 UOP，但允许因预测时间相同而略有 overshoot。

不要把 `stride` 和 `dt` 混为一个参数。stride 在 UOP 空间选预测位置，`delta` 在 cycle 空间由该位置的预测 commit time 动态产生。

## 3. 标准目录和命名

正式运行目录格式：

```text
logs/<evaluator>_<model>_<mode>_s<stride>_seed<seed>_c<cores>_<timestamp>/
  report.json
  report.txt
  trace_logs/
    c08/<workload>.log
    c32/<workload>.log
  .worker_state/
    worker_0.json
    worker_0.json.traces.jsonl
    ...
```

约定如下：

- `<evaluator>` 必须跟当前推理版本，例如 `v29`，不能因输入 workload 名称仍为 `W_v28_*` 就把 evaluator 写成 v28。
- `W_v28_*` 表示 workload/trace suite 的来源兼容名，不表示当前模型或推理器版本。
- 每条 trace 一个独立日志，8 GPU 主进程输出聚合到启动日志，结束后生成 `report.json` 和 `report.txt`。
- `.worker_state/*.traces.jsonl` 是 trace 粒度的断点续跑事实源；只有 checkpoint ID 和完整 evaluation contract 都一致时才能复用。
- 临时启动日志放 `logs/tmp/`，PID 和临时脚本放 `scripts/tmp/`。禁止使用项目外的根 `/tmp` 保存 TCSim 任务脚本、PID、报告或日志。

推荐运行名示例：

```text
v29_packed3_free_s256_seed0_c08_c32_20260717_142108
```

## 4. 标准 trace 日志

每条 trace 日志必须包含以下四组内容。

### 4.1 Header

至少记录：evaluator、workload suite、cache、split、UOP 数、checkpoint 路径/ID/step、device、AMP dtype、SDPA backend、mode、stride、step-cycle 范围、fast path、oracle drift、progress 间隔、context builder、CPU/GPU cache policy 和独立日志路径。

当前关键合同应显示为：

```text
evaluator=v29 workload_source=v28-compatible trace suite
mode=free target_stride=256 step_cycles=4..1024
free_fast_path=on horizon_outputs=off oracle_drift=off
context_builder=numpy-vectorized-v2
context_timing=v29-context-phases-v1
cpu_window_cache=last-window-per-core gpu_static_cache=last-window
```

### 4.2 Progress

进度行必须同时给出部署质量和性能，格式保持稳定：

```text
[workload c08] window=200 progress=5.8% uops=400588/6881888
uops/s=94761 running ROI-CPI pred=0.1500 label=0.1575 err=4.72%
active=8 forwards=200 useful_uops/step=2002.9
global/delta=7513.2/35.6 avg_step=21.14ms wall=4.2s
```

`progress_every` 只控制打印频率，不能改变 forward 或 scheduler 行为。正式全量运行推荐 200，短程 smoke 可用 100。

### 4.3 Final result

最终必须记录：

- complete、metric scope、总 steps、global cycles、UOP/macro 覆盖率；
- ROI-CPI pred/label/error、per-core MAPE 分位数和 signed bias；
- cycles、makespan、branch miss count/rate；
- stride、UOP/forward、overshoot、min-step warning、no-progress；
- UOP/s、step/s、平均 step、wall time、GPU peak；
- 每个 core 的 UOP、cycle/error、branch rate 和 final progress error；
- 结果 JSON 的持久化路径。

不完整的 `max_free_steps` smoke 必须标成 `scope=consumed_functional_prefix`，不能作为 full-ROI headline。

### 4.4 Timing 和 cache

每条 trace 必须输出以下可加总计时：

```text
timing avg ms/forward context/predict/model/D2H/scheduler
context phases avg ms/forward select/window/cross-core/state+targets/tensor/overhead
context phase share select/window/cross-core/state+targets/tensor/overhead
```

- `context`：mmap 切片、CPU 特征和 tensor 装配；
- `predict`：H2D、模型、D2H 和预测合同检查的总时间；
- `model`：CUDA event 计量的模型 forward；
- `D2H`：commit time 和 branch probability 回传；
- `scheduler`：stride 选点、exact-once 计数和 cursor/time 更新。

`context` 子阶段采用 `v29-context-phases-v1` 合同：

- `select`：参数校验、活跃 core/cursor 选择和状态时间归一化；
- `window`：逐核 mmap 切片、padding、pressure、summary 和 CPU window cache；
- `cross-core`：物理 line/resource presence、owner/fanout、动态 UOP 字段和 relation；
- `state+targets`：部署 state features；oracle one-step 还包括 timing/prefix/progress target；
- `tensor`：`np.stack`、dtype 转换和 CPU Torch tensor 组装；
- `overhead`：外层 `context_build_seconds` 与内部五段之差，用来保证分项可与总时间对账。

进度日志显示从 rollout 开始累计的平均值，最终日志和 JSON 保存全程累计值。JSON 路径为
`free_running.timing_breakdown.context_phase_seconds`；各子阶段之和必须等于
`context_build_seconds`（允许浮点舍入误差）。

cache 必须分别记录 CPU-window 和 GPU-static 的 hits/misses/evictions/entries，不能把两层混成一个命中率。

## 5. CPU 上下文构建标准

当前 profiling 结论、分阶段实施方案、预期收益和验收矩阵见
[`v29_context_optimization_plan.md`](v29_context_optimization_plan.md)。标准框架规定不变；优化方案只能做输入语义等价的实现替换。

窗口构建采用 `numpy-vectorized-v2`：

1. 对每个 core 从只读 `.npy` mmap 做连续 `[cursor:cursor+K]` 切片；
2. 一次性装入定长 NumPy 数组并补齐 tail；
3. pressure 直接写入 staging buffer；L1/L2 使用有界 `bincount`，LLC pair 使用无碰撞 mixed-radix ID；
4. 用共享 histogram 批量计算 38 维 window summary，不重复做 distinct/repeated/HHI 统计；
5. 跨所有活跃 core 批量计算 exact-key presence、22 维关系和 8 维动态 UOP 特征；row/bank 反查不经过 Python tuple/list；
6. deployment/oracle context 走内部 NumPy-only window，不生成整窗 Python list，也不重复生成 `read_lines/write_lines`；公开 `window()` 仍保留兼容接口；
7. 保留已构造的连续数组，用 `torch.from_numpy` 组装 tensor，只把模型输入 tensor 传到 GPU。

禁止在热点路径中恢复以下模式：逐元素 mmap `__getitem__`、每个 UOP 的 `list(map(...))`、每次 forward 把同一数组在 list/NumPy/tensor 间反复解析，或按 core 两两扫描全部 K 行。

向量化属于语义等价优化。任何修改都必须与旧实现比较：pressure 逐元素一致，summary/relations 数值一致，dynamic IDs 逐元素一致。

## 6. Cache 标准

框架有三层 cache：

1. OS page cache：由 mmap 和操作系统管理，不单独做应用级复制。
2. CPU window cache：每个 core 只保留最后一个 `(cursor, include_oracle, numpy_only)` 窗口。它主要处理 no-progress 或相同 cursor 重试；cursor 正常推进时低命中率是合理现象。
3. GPU static cache：每个 core 只保留最后一个 static encoder 结果，key 至少包含 trace ID、core ID、cursor、uarch hash 和 checkpoint ID。

CPU/GPU cache 都必须在新 trace 开始时清空，容量随 core 数线性增长，禁止无界保存所有历史 cursor。缓存命中只能改变性能，不能改变模型输入和结果。

## 7. GPU fast path

free-running scheduler 只消费：

- 单调的 per-UOP `commit_time`；
- per-branch `branch_miss_probability`；
- valid mask。

因此 free 模式调用 `predict_free`，不构造或回传 horizon 的 commit probability/progress。oracle one-step 仍调用完整输出，用来验证训练 head。BF16 和 SDPA/fast attention 只影响模型段；如果 `context` 显著大于 `model`，应先检查 CPU 特征路径，不能把总吞吐问题归因于 attention。

模型返回后必须检查 finite 和 token 方向单调性。发生 monotonicity violation 时应报告 row、相邻 token、tau、diff 和 predicted gap，禁止静默排序预测值。

### 7.1 单 trace 多 GPU 重叠窗口

默认 `window_parallel_mode=serial` 保持原有闭环语义。需要用多张 GPU 加速同一条 trace
时，可显式选择：

- `unconditional`：将偏移重叠窗口拼为开环 gap lattice；
- `speculative`：按窗口深度链式交接，任一活跃核没有提交到下一窗口起点时截断该窗口
  及全部更深窗口。

两种模式都允许同一 UOP 被预测多次，但 UOP、macro 和 branch 只能由 owner window
提交一次。日志和 JSON 必须区分 scheduler `steps`、实际 `model_forwards` 和
`parallel_waves`；speculative 模式还必须报告 accepted/issued window hit rate、
full-chain hit rate 和首次失败深度。

多 GPU 模式的 context 也必须按窗口 lane 并行构造。默认使用独立的 spawn CPU
process，从而绕过 Python GIL；worker 只构造 CPU context，不接触 CUDA。lane 之间依靠
OS page cache 共享只读 trace mmap，但使用独立的 last-window-per-core cache、context
phase counters 和临时数组；禁止用一把 store 全局锁把 context 热路径重新串行化。
报告同时保留 context wall time、worker CPU time、backend 与 effective parallelism。

完整布局、ownership、失败语义与命令行合同见
[`v29_single_trace_multi_gpu_window_parallel.md`](v29_single_trace_multi_gpu_window_parallel.md)。

## 8. 验收门槛

推理框架或特征优化合入前至少通过：

1. `tests/test_v29.py` 和 `tests/test_v29_inference.py`；
2. 随机 short/full window 的 pressure、summary、dynamic、relations 旧/新等价测试；
3. 同 checkpoint、同 cursor 的模型输出和 scheduler 消费前缀一致；
4. c8 和 c32 各至少 300 step smoke，无 monotonicity、exact-once、no-progress 异常；
5. 日志包含 running ROI-CPI、完整 timing、两层 cache 和 persisted path；
6. 正式报告只聚合 complete full-ROI trace，按 core count 和 workload 等权展示，禁止用全局 pooled 数掩盖 workload 差异。

当前 `W_v28_fp_alu_dense` 300-step 基准（GPU 有其他任务，作为回归参考而非硬 SLA）：

| cores | 优化前 avg step | `numpy-vectorized-v1` | context | UOP/s |
|---:|---:|---:|---:|---:|
| 8 | 55.84 ms | 20.43 ms | 4.49 ms | 98,362 |
| 32 | 232.21 ms | 67.93 ms | 16.70 ms | 116,989 |

c8 的相同进度点仍得到 `pred=0.1500, label=0.1575`，说明性能优化没有改变闭环预测轨迹。

`numpy-vectorized-v2` 的纯 context A/B 见
[`v29_context_optimization_plan.md`](v29_context_optimization_plan.md)。它是新的 evaluation/resume contract；旧 `v1` 结果不会被静默复用。正式端到端 UOP/s 仍以新进程的 full-ROI 报告为准，不能把纯 context 微基准直接当作整条推理加速比。

c32 seed0 `memory_random_mlp` 的首个 v2 full-ROI 验证完成：context 42.33→22.01 ms/forward（-48.0%），总步时 84.06→64.55 ms（-23.2%），UOP/s 58,996→76,825（+30.2%）。3595 steps、预测 cycles、ROI-CPI、branch count 和 scheduler 轨迹与 v1 完全一致。完整数据见优化方案文档第 3.5 节。

## 9. 当前一键运行和监控

当前只跑 seed0、c8/c32、free、stride=256：

```bash
cd /data00/yinhaolang/TCSim
bash scripts/tmp/launch_v29_packed3_c08_c32_free_s256.sh
```

启动器会打印本次 `OUT_DIR`、launch log 和 PID 文件。按它打印的命令 `tail -f logs/tmp/<run-tag>.log` 监控即可。

通用 8 GPU 入口示例：

```bash
cd /data00/yinhaolang/TCSim
CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
SPLITS=seed0_inference,development_heldout \
MODE=free CORE_COUNTS=8,32 TARGET_STRIDE=256 MAX_STEP_CYCLES=1024 \
ORACLE_DRIFT_DIAGNOSTICS=0 RESUME=1 \
bash scripts/run_v29_eval_8gpu.sh
```

新版本复用该框架时，应只替换版本模块、checkpoint、manifest 和显式变更的 evaluation contract；目录、日志、计时、cache 统计与验收流程保持兼容。
