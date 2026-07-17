# v29 Context 构造性能优化方案

状态：Phase 1～2 已实施并通过等价性与真实 cache A/B；Phase 3 待端到端复测决定
基线日期：2026-07-17
适用范围：`tcsim/v29/dataset.py`、`tcsim/v29/inference.py` 和部署推理日志框架

## 1. 目标与约束

目标是在不改变模型输入、checkpoint、scheduler、stride 和虚拟时间语义的前提下，降低 free-running 每次 forward 前的 CPU context 构造时间。

必须保持：

- `K=256` 和 `target_stride=256` 的现有语义；
- pressure、38 维 `chunk_summary`、8 维 `dynamic_uop_fields` 和 22 维 `relation_features` 数值等价；
- physical line、LLC、DRAM resource key 的 equality-only 合同无碰撞；
- 相同 checkpoint、相同 cursor/context 下模型输出和 scheduler 消费前缀一致；
- free-running 不读取 oracle timing 作为模型输入；
- mmap、断点续跑、逐 trace 日志和 worker JSONL 标准不退化。

本方案不通过增大 dt、减少 forward 数量或改变模型输入分布来换取吞吐。此类改动属于推理语义或模型方案变更，不属于 context 等价优化。

## 2. 已完成：子阶段计时基础设施

计时合同为 `v29-context-phases-v1`，阶段互斥且可以和现有 `context_build_seconds` 对账：

1. `active_core_selection`：参数检查、state time 归一化和活跃 cursor 选择；
2. `per_core_window`：逐核 mmap 切片、padding、pressure、summary 和 CPU window cache；
3. `cross_core_features`：line/resource presence、owner/fanout、dynamic fields 和 relation；
4. `state_and_targets`：state features；oracle 模式还包括 timing/prefix/progress targets；
5. `tensor_assembly`：`np.stack`、dtype 转换和 CPU Torch tensor 组装；
6. `call_overhead`：外层 context 总计时与内部五段之差。

记录位置：

```text
free_running.timing_breakdown.context_build_seconds
free_running.timing_breakdown.context_phase_seconds
free_running.context_calls
free_running.context_timing_contract
```

实时和最终日志格式：

```text
context avg ms/forward total/select/window/cross-core/state+targets/tensor/overhead
context phases avg ms/forward select/window/cross-core/state+targets/tensor/overhead
context phase share select/window/cross-core/state+targets/tensor/overhead
```

`context_timing=v29-context-phases-v1` 已进入 evaluation/resume contract。旧结果没有该字段，不能与新结果在一次 resume 报告中混用。

## 3. 当前基线

### 3.1 全量推理墙钟分解

当前已完成 trace 的代表性汇总：

| 范围 | CPU 侧阶段 | context | GPU model | H2D+D2H | scheduler |
|---|---:|---:|---:|---:|---:|
| 全部 | 44.5% | 43.0% | 54.5% | 1.0% | 0.9% |
| c4 | 34.7% | 33.0% | 63.9% | 1.3% | 0.8% |
| c8 | 44.4% | 42.8% | 54.5% | 1.1% | 0.9% |
| c16 | 48.8% | 47.5% | 50.4% | 0.7% | 0.9% |
| c32 参考运行 | 46.0% | 45.0% | 53.5% | 0.5% | 0.8% |

这里的比例是单 trace 推理墙钟阶段占比，不是整机 `%CPU` 利用率。

### 3.2 真实 packed cache 子阶段微基准

每条 trace 选取 24 个分散 cursor，使用实际 `V29TraceStore`，不执行模型 forward：

| workload | cores | context internal | window | cross-core | tensor |
|---|---:|---:|---:|---:|---:|
| `fp_alu_dense` | 4 | 3.75 ms | 82.0% | 15.2% | 2.5% |
| `memory_random_mlp` | 4 | 6.86 ms | 72.0% | 26.5% | 1.3% |
| `fp_alu_dense` | 16 | 12.99 ms | 84.0% | 12.6% | 3.1% |
| `memory_random_mlp` | 16 | 24.88 ms | 74.1% | 24.7% | 1.1% |
| `fp_alu_dense` | 32 | 25.61 ms | 84.6% | 11.8% | 3.4% |
| `memory_random_mlp` | 32 | 49.11 ms | 75.1% | 23.9% | 0.9% |

12 组 c4/c8/c16/c32 微基准合计约为：

- `per_core_window`：78.3%；
- `cross_core_features`：20.2%；
- `tensor_assembly`：1.4%；
- selection 和 state：低于 0.2%。

绝对时间会受 workload、cursor 分布、OS page cache 和并发任务影响；阶段占比用于确定优化顺序。

### 3.3 cProfile 证据

c16 `memory_random_mlp`、30 次 context 的 profile：

| 热点 | context 占比 |
|---|---:|
| `_summarize_window_numpy` | 48.4% |
| `_context_features_numpy` | 20.8% |
| `_apply_window_pressure_numpy` | 10.8% |
| NumPy `.tolist()` | 7.6% |
| padding、复制和其他组装 | 约 12% |

该 profile 中 `np.unique` 调用 7,652 次，约 255 次/context，占总时间约 36%。主要来源是逐核 pressure/summary 和跨核 presence。

单个代表性 window chunk 深度内存约 232 KB，其中 NumPy payload 约 77 KB。c32 的 last-window cache 会保留约 7.4 MB；cursor 每次推进还会产生大量临时数组、嵌套 list 和 Python 整数。

### 3.4 Phase 1～2 实施结果（`numpy-vectorized-v2`）

已完成：

- deployment/oracle context 使用内部 NumPy-only window，公开 `window()` 保持 list 兼容；
- pressure 原地写 staging buffer，取消第二份 fields 全量复制；
- oracle timing/prefix/progress target 直接由 NumPy commit/valid 数组批量生成；
- summary 复用 histogram，并对有界整数 key 使用 `bincount`；
- cross-core presence 使用 context 内无碰撞 mixed-radix ID，row/bank 反查不再经过 `.tolist()`、tuple dict 和 `np.fromiter`；
- evaluation contract 和日志中的 builder 升级为 `numpy-vectorized-v2`。

相同 seed0 packed cache、每项 24 个分散 cursor、同一推理 Python 环境的 A/B：

| workload | cores | v1 median | v2 median | median 降幅 | v1 mean | v2 mean |
|---|---:|---:|---:|---:|---:|---:|
| `fp_alu_dense` | 8 | 6.146 ms | 4.421 ms | 28.1% | 6.347 ms | 4.754 ms |
| `memory_random_mlp` | 8 | 12.981 ms | 7.858 ms | 39.5% | 12.955 ms | 7.652 ms |
| `mysql_heldout` | 8 | 12.411 ms | 7.852 ms | 36.7% | 12.029 ms | 7.691 ms |
| `fp_alu_dense` | 32 | 24.042 ms | 16.144 ms | 32.9% | 24.817 ms | 17.030 ms |
| `memory_random_mlp` | 32 | 51.138 ms | 28.021 ms | 45.2% | 50.068 ms | 26.988 ms |
| `mysql_heldout` | 32 | 48.608 ms | 27.666 ms | 43.1% | 48.792 ms | 26.868 ms |

`per_core_window` 六项下降 35.6%～49.5%；memory c32 的 `cross_core_features` 下降 52.3%。compute c8 的 cross-core 从 0.839 ms 到 0.874 ms，有 0.035 ms 小幅波动，但总 context 仍下降 28.1%。

正确性结果：pressure/summary 与 reference 对齐；1/2/8/32 核 dynamic 和 relation 对齐；公开 window、NumPy-only window、oracle targets、free 模式不读取 oracle 均通过；`tests/test_v29.py` 与 `tests/test_v29_inference.py` 共 26 项通过。

上述数据是 CPU context 微基准，不包含 GPU model 和 scheduler。正式端到端收益必须由新进程的相同 checkpoint/full-ROI 复测确认；已经启动的 Python 进程不会自动加载本次代码。

### 3.5 c32 full-ROI 端到端验证

验证对象：seed0 `W_v28_memory_random_mlp` c32，checkpoint step 59000，free fast path，BF16，stride=256，完整 17,827,488 UOP。

| 指标 | v1 full 参考 | v2 full | 变化 |
|---|---:|---:|---:|
| context/forward | 42.33 ms | 22.01 ms | -48.0% |
| avg step | 84.06 ms | 64.55 ms | -23.2% |
| wall | 302.182 s | 232.054 s | -23.2% |
| UOP/s | 58,995.8 | 76,824.6 | +30.2% |
| context/step 占比 | 50.4% | 34.1% | -16.3 pp |

v2 context 分项为 window 16.55 ms、cross-core 5.10 ms、tensor 0.32 ms；model forward 为 41.45 ms。完整 rollout 的 3595 steps、predicted cycles、ROI-CPI、branch count、scheduler consumption 与 v1 逐项一致：ROI-CPI `4.001769 / 4.111292`、误差 2.664%，branch miss `490.892 / 492`，overshoot/no-progress 均为 0。这证明优化没有改变模型输入、预测或 scheduler 轨迹。

v1 参考来自早先多 worker suite，v2 是本次单 GPU worker，因此墙钟不是完全隔离的同并发实验；但 v2 model 段反而比参考慢约 2%，而 context -48.0% 与前述独立 CPU A/B 的 c32 memory -46.1% 基本一致，收益可以归因于 context v2，而不是 GPU 变快。

结果目录：`logs/v29_context_v2_seed0_c32_memory_random_full_20260717_184636/`。

## 4. 根因判断

### 4.1 第一瓶颈是逐核 window，不是 tensor/H2D

`window()` 对每个活跃核分别执行：

- 创建和填充多个 `K=256` 定长数组；
- 将 packed `fields` 扩展为 int64；
- 再复制一次 fields 以写入三个 pressure 字段；
- 多次调用 `np.unique`/`np.isin` 生成 summary；
- 把同一内容转换成 Python list，同时保留 `_numpy` 副本。

这些操作随 core 数近似线性增长。tensor assembly 只有约 1%，因此 pinned memory 或 H2D 不是当前首要方向。

### 4.2 第二瓶颈是重复 equality 统计

跨核路径分别为 reads、writes、accesses、LLC set/bank、DRAM channel/bank/row 建立 unique key 和 owner 表。部分 key 使用二维 `np.unique(axis=0)`，之后又通过 tuple dict 和 `np.fromiter` 做反向查询。

当前特征合同只依赖 key 相等关系，不依赖 key 的绝对数值，因此可以用离线无碰撞紧凑 ID 代替多列 tuple。

### 4.3 last-window cache 不解决正常 rollout

当前 CPU/GPU exact-cursor cache 加权命中率约 0.57%。`target_stride=256` 下 cursor 单调推进，历史窗口通常不会重访。扩大 LRU 只增加内存，不能解决主体开销。

### 4.4 直接线程化不可行

c32 `memory_random_mlp` 的 window-only 对照：

| worker threads | 平均 window 时间 |
|---:|---:|
| 1 | 37.1 ms |
| 2 | 53.8 ms |
| 4 | 58.7 ms |
| 8 | 61.5 ms |

小 NumPy 调用、Python list 分配、GIL 区段、线程调度和内存带宽使 naive `ThreadPoolExecutor` 明显变慢。必须先减少对象和算子数量，再评估并行。

## 5. 分阶段实施方案

### Phase 1：部署 numpy-only window

优先级：P0
风险：低
目标：减少 8%～15% context
实施状态：已完成

实施内容：

1. 为 `context_from_cursors()` 增加内部 numpy-only window 路径；
2. free-running 跳过 `per_uop_fields/resource/lines/access/...` 的 `.tolist()`；
3. `read_lines/write_lines` 不再在部署路径重复生成；
4. oracle target 直接读取 `_numpy` commit/valid 数组，不依赖 list；
5. 保留兼容的公开/legacy window 路径，避免影响训练或诊断调用方；
6. 将 pressure 写入已有输出 buffer，取消 raw fields 到 pressure fields 的二次全量复制。

验收：所有输入 tensor、summary、relations、预测结果逐元素一致；c8/c32 三类 workload 的 `window` 阶段均下降。

### Phase 2：合并 summary histogram

优先级：P0
风险：低到中
目标：累计减少 25%～40% context
实施状态：已完成；六组 median 实测累计下降 28.1%～45.2%

实施内容：

1. 同一组 values 只计算一次 histogram/counts；
2. 由同一 counts 同时派生 distinct count、repeated fraction 和 HHI；
3. L1/L2 等有界整数 key 使用 `np.bincount`；
4. LLC pair 使用无碰撞 packed/dense ID 后再计数；
5. 消除重复 `resource_values()`、`np.unique()` 和 Python `counts.tolist()`；
6. 将 producer sum/max、bit count、opclass count 等简单归约合并为少量批量操作。

真实 K=256 微基准中，L1/L2 set 的 `bincount` 比当前 `np.unique` 快约 6 倍。最终收益必须以完整 context 而不是单算子 microbenchmark 为准。

### Phase 3：批量多核 window

优先级：P1
风险：中
目标：减少逐核 Python 调度和数百次小型 NumPy 调用

实施内容：

1. 引入 `[C,K,...]` batched staging；
2. mmap slice 仍按 core 读取，但集中填入复用 buffer；
3. pressure 分组 key 编码为 `(core_slot, resource_id)`，一次完成全部活跃核；
4. summary 使用 batched/grouped reductions；
5. staging buffer 只用于严格串行的部署路径，不得复用于可能并发/预取的训练 Dataset；
6. buffer 生命周期覆盖一次 predict，下一次 context 构造前模型必须已完成输入拷贝。

不使用 per-forward ThreadPool；并行化只能在 batched 路径稳定后重新评估。

### Phase 4：packed cache 紧凑资源 ID

优先级：P1
风险：中，需要重建 cache

实施内容：

1. builder 在整条 trace、所有核范围内为 equality key 分配无碰撞 ID；
2. 至少覆盖 physical line、LLC bank+set、DRAM bank 和 DRAM row；
3. 跨核 read/write/access 共用一个 line table，通过分类计数派生 reader/writer；
4. 用持久紧凑 ID 进一步消除每个 context 的 mixed-radix 编码和 unique/grouping；当前 v2 已删除 Python tuple dict 和 `np.fromiter`；
5. 优先使用 int32/uint32，并在 schema 中记录 ID 合同；
6. cache schema/version 必须升级，禁止静默读取旧布局。

这属于表示等价优化：同一 key 必须得到同一 ID，不同 key 绝不能碰撞。随机 hash 碰撞方案不可接受。

### Phase 5：可选 GPU/Fused cross-core

优先级：P2
风险：高
前置条件：Phase 1～4 后 `cross_core_features` 仍为主瓶颈

只有在 resource ID 和 cursor gather 能常驻或高效进入 GPU 时，才考虑 Torch/Triton 的 sort、segment count 和 owner/fanout kernel。不能只把现有 CPU 中间结果额外传到 GPU；kernel launch 和传输可能抵消收益。

GPU 版本必须与 NumPy reference 逐元素对齐，任何近似、hash 碰撞或特征删除都视为模型输入合同变更。

## 6. 不采用的方案

- 不通过固定大 dt 或跳过中间 prefix 来减少 context 次数；
- 不扩大 exact-cursor LRU 期待正常 rollout 命中；
- 不优先优化 tensor assembly、pinned memory 或 D2H；
- 不直接复制 TSim 的 aggregate/lagged shared-state proxy 替换 v29 exact relation；
- 不加载 TSim 风格的巨大整 trace `.pt` 到每个 worker 内存；继续以 mmap 和紧凑字段为基础；
- 不使用 naive per-core Python 线程池；
- 不改变 K、stride、horizon、模型宽度或 checkpoint 来冒充 context 等价优化。

## 7. 预期收益

以 context 占当前总墙钟约 44% 估算：

| 累计目标 | context 降幅 | 预计端到端提升 |
|---|---:|---:|
| Phase 1 | 8%～15% | 约 4%～7% |
| Phase 1～2 | 30%～45% | 约 15%～25% |
| Phase 1～4 理想目标 | 约 50% | 约 1.28 倍 |

这是 Amdahl 估算，不是承诺值。正式结论以相同机器、相同 checkpoint、相同 cursor/trace、相同 GPU 并发下的中位数为准。

## 8. 验收与回归流程

每个 Phase 独立提交和验收，不把多种优化合在一次不可归因的改动中。

### 8.1 正确性

1. `tests/test_v29.py`、`tests/test_v29_inference.py` 全通过；
2. pressure、summary、dynamic IDs、relations 与 reference 逐元素对齐；
3. short/full/tail window、active core 减少、no-progress、oracle/free 都覆盖；
4. 同 checkpoint、同 cursor 的 commit time 和 branch probability 对齐；
5. scheduler 的 delta、消费前缀、overshoot、exact-once 计数一致；
6. c8/c32 至少 300-step smoke，无 monotonicity 和 no-progress 新异常。

### 8.2 性能

至少覆盖：

- compute：`W_v28_fp_alu_dense`；
- random memory：`W_v28_memory_random_mlp`；
- business mixed：`W_v28_mysql_base`；
- core count：c4、c8、c16、c32；
- 每项预热后重复至少 3 次，报告 median 和离散度。

必须同时记录：

- total/context/model/UOP/s；
- 六个 context 子阶段；
- CPU/GPU cache 命中率；
- model forward 数和 useful UOP/forward；
- RSS、GPU peak memory；
- ROI-CPI、branch miss 和 scheduler 轨迹是否一致。

只报告单个 workload 或只报告 `np.unique` microbenchmark，不能作为 Phase 完成依据。

## 9. 实施顺序

当前执行状态和下一步顺序：

1. Phase 1 numpy-only window 和 in-place pressure：已完成；
2. Phase 2 histogram/bounded `bincount`/exact row lookup：已完成；
3. c8/c32 三 workload CPU context A/B：已完成，median 下降 28.1%～45.2%；
4. 下一步用新进程执行相同 checkpoint 的 c8/c32 300-step smoke，确认预测、scheduler 轨迹和端到端 timing；
5. 再执行 full-ROI 或代表性 trace A/B，根据新日志中 window/context 的墙钟占比决定是否进入 Phase 3；
6. 只有 batched window 后 cross-core 成为绝对主导，才实施 packed resource ID/GPU 方案。

每一步都以 `v29-context-phases-v1` 的阶段数据决定下一步，不凭总 UOP/s 猜测瓶颈。
