# TCSim v28.1 functional feature、branch 与 ROI trace 合同

> 状态：2026-07-15 起生效。本文覆盖 v28 旧数据中的全局 ROI gate、ROI 尾部
> `pthread_join/futex` 污染、伪 branch target/history，以及“仅条件分支”标签定义。
> 旧 raw、旧 tensor cache 和旧 checkpoint 不得与本合同混用。

## 1. 固定决策

- 一个 attention token 仍是一条退休 UOP，chunk 固定为 `K=256`。
- 保持 full QKVR；保持 `d_static=768`、`d_dyn=960`、15 heads、8 layers。
- 首版继续建模 cold-start 完整 ROI，不删除前几个窗口。
- 模型输入只能来自 functional record、同核已提交历史、目标静态微架构配置，
  以及当前 active functional chunks 之间可重算的关系。
- branch miss 统一为**所有退休分支指令**：分母是所有退休 branch，分子是其中
  所有 predictor prediction failure。不能只统计 conditional branch。
- 每个核心有独立 ROI 边界。某核执行自己的 `WORKEND` 后，不能继续输出 trace，
  也不能执行 `pthread_join/futex` 干扰仍在 ROI 的其他核心。

## 2. 同一个 UOP token 的四组特征

`base / branch / resource / dynamic context` 不是四种 token，而是同一 UOP 的
四个编码分支：

```text
x_i = W_base h_base(i)
    + W_branch h_branch(i)
    + W_resource h_resource(i)
    + W_dynamic h_dynamic(i, active_context)
    + W_side h_side
```

各分支投影到现有 `d_dyn=960` 后相加，不因为新增字段扩大 attention 宽度。

### 2.1 Base：指令与同核局部行为

来源是当前 UOP 和同核程序序历史，包括：

- op class、load/store/atomic、访问大小和 line offset；
- src/dst、producer class/distance、寄存器依赖；
- reuse distance、stride、近期 working-set；
- local PC、macro position、local line ID、同核 op history。

它回答“这条 UOP 自己是什么、同核依赖和局部性如何”。

### 2.2 Branch：真实控制流，不是 predictor oracle

允许的 per-UOP branch 输入：

- branch 类型：conditional/direct/indirect/call/return；
- `actual_taken`；
- 实际提交 successor/target 的有符号距离桶；
- 当前分支发生前最近 8/16 个**已提交实际方向**；
- chunk 内 retired branch fraction、taken fraction、indirect fraction、方向切换率。

这些是程序实际执行路径的一部分，与具体 predictor 无关。禁止输入预测方向、预测
target、BTB hit、counter、speculative GHR、mispredicted 和 branch penalty。

### 2.3 Resource：固定目标微架构下的资源映射

从 `paddr` 和 `uarch_profile` 派生：

- `paddr_valid`；
- L1/L2/LLC set 与 set pressure；
- LLC bank；
- DRAM channel、bank、row；
- DRAM row reuse；
- chunk 内 bank/channel HHI、set conflict fraction、row reuse fraction。

不向模型暴露完整 raw physical address。训练 sample 内应一致地随机置换 bank/channel
ID，使模型保留“是否相同”的关系，而不能记忆 bank 0 等绝对编号。

Resource 对固定 `(trace, core, chunk, target_uarch_hash)` 不变，可进入 static cache；
目标 cache/DRAM 映射变化后必须重算。

### 2.4 Dynamic context：当前 active chunks 的跨核竞争

每个 scheduler context 重算：

- 同 LLC set/bank 的跨核 fanout；
- 同 DRAM channel/bank 的跨核 fanout；
- same-row support 与 different-row conflict pressure；
- 当前跨核 shared-read/read-after-write/write-to-other-access 关系；
- active-core 数量和 aggregate memory density。

它回答“当前与谁共同运行、竞争哪些资源”。同一 chunk 在不同 active-core 组合下值会
改变，因此不能进入永久 static token cache。

### 2.5 Attention 和 cache 边界

- static cache：base + branch + resource；
- 每步重算：dynamic context、relation 和 active-core summary；
- local Q 继续看同核 K/V，cross R 继续看其他 active core 的 K/V；
- 增加由 relation/resource pressure 控制的 cross gate：

```text
output = O_local(local_ctx)
       + sigmoid(MLP(relation, resource_pressure)) * O_cross(cross_ctx)
```

这保留 full QKVR，但不再强制 local/cross 永远等权相加。暂不引入 dense pairwise
attention bias；在 c32、K=256 下它会制造过大的 bias tensor，并可能破坏 SDPA 快路径。

## 3. Branch 统一定义

对一个 chunk：

```text
B = count(retired control-flow instructions)
M = count(retired control-flow instructions where mispredicted == 1)
branch_miss_rate = M / B, if B > 0
```

这里的 `B` 包含 conditional、unconditional direct、indirect、call 和 return。
训练 head 输出的是每次退休 branch 的 miss probability/rate，部署侧锁存：

```text
branch_miss_hat(chunk) = pred_branch_miss_prob(chunk) * B(chunk)
```

resident chunk 可多次进入 attention context，但预测和真实 branch count 都只能在该
chunk 首次加载时锁存、最终提交时 exact-once 累计。

`mispredicted` 依赖具体 predictor。v28 当前标签绑定 gem5 `TournamentBP`、4096-entry
BTB、16-entry RAS 和当前 indirect predictor。若改变 predictor：

- functional branch 输入保持不变；
- 标签必须重新采集；
- 单一 predictor checkpoint 必须记录 predictor hash；
- 若一个模型覆盖多种 predictor，必须增加 predictor profile，并用多种 predictor
  数据训练；仅增加 profile 而没有跨 predictor 数据不能产生泛化。

## 4. Raw branch schema

每条 `records.micro` 至少包含：

| 字段 | 语义 | 模型可见 |
|---|---|---:|
| `is_branch` | 该退休 UOP 对应一个退休控制流指令 | 是 |
| `is_branch_cond/indirect/is_call/is_return` | 静态 branch 类型 | 是 |
| `branch_taken` | 架构实际方向；非 branch 为 0 | 是 |
| `branch_next_pc` | 分支执行后的实际 committed successor PC | 派生后使用 |
| `branch_target` | taken 时等于实际 successor，否则 0 | 派生后使用 |
| `branch_history` | 当前 UOP 前最近 16 个退休 branch 的实际方向 | 是 |
| `mispredicted` | 当前配置 predictor 是否预测失败 | **仅标签** |

`branch_history` 必须先写当前 UOP 的 history，再在 branch 退休后移入
`branch_taken`。不得使用 predictor 的 speculative GHR。`branch_target_delta` 从
`branch_target - macro_pc` 计算并分桶，完整 PC/target 不直接嵌入模型。

旧采集器把 `branch_target` 写成 branch 自身 PC，并把 history 每次固定移入 1；这些
字段无效。旧 aligned parquet 也没有保存它们。新 schema 必须重新采 raw。

## 5. Per-core ROI 合同

### 5.1 采集闸门

TaoTrace 维护：

```text
roi_depth[core_id]
global_roi_depth = sum(roi_depth)
emit(core_id) = !require_roi || roi_depth[core_id] > 0
```

第一次 `WORKBEGIN(core)` 打开该核，自己的 `WORKEND(core)` 立即关闭该核。全局
depth 只用于 first-begin/last-end 生命周期，不能决定某个核心是否 emit。

首 chunk 的 cycle label 必须从该核 `WORKBEGIN tick` 开始，而不是从首条 UOP 的
`fetch_tick` 开始。流水线可能在 `WORKBEGIN` 退休前预取后续 ROI 指令；若沿用
`first_fetch`，会把首 chunk 起点错误地放到 ROI 外，也会漏掉 cold-start 前缀。
后续 chunk 仍以前一 chunk 的 commit endpoint 为起点，保证逐核 cycle 可加。

单独输出 `roi_boundaries.jsonl`：event、core、thread、work ID、tick、per-core depth
和 global depth。tick 仅用于审计，不能进入模型。

### 5.2 完成核不能干扰未完成核

worker 执行：

```text
WORKBEGIN
kernel
WORKEND
M5_QUIESCE
```

仿真控制器统计 `WORKEND`：前 `N-1` 次继续仿真，让完成核进入 quiesce；第 `N` 次
直接结束仿真。这样不会进入主线程的 `pthread_join`、futex、析构和输出路径，也不会
让已完成核心继续争用共享 cache/DRAM。

### 5.3 ROI 审计硬门禁

每个 workload × core-count 必须满足：

- 每核恰好一个 begin 和一个 end，且 begin < end；
- 每个 records/labels 行都位于该核自己的边界内；
- 自己的 end 后无 commit record；
- trace 尾部无 `pthread_join`、futex、thread exit、libc cleanup；
- ROI 内无 atomic、serialize、lock、barrier、yield、sleep 和调度指令；
- 每个 branch subtype 都必须蕴含 `is_branch=1`；subtype 允许重叠（例如 indirect
  call 同时是 indirect 和 call），因此不能把 subtype 数量简单相加作为退休 branch
  数；并满足 `0 <= misses <= branches`；
- functional `branch_taken/target/history` 自洽；
- records/labels 逐 UOP 完整对齐。

任何一项失败时 manifest 必须标记 `blocked`，训练入口必须拒绝启动。

## 6. 禁止输入与 provenance

禁止输入：raw core ID、raw paddr、raw chunk/progress ID、workload ID、真实/预测 tick、
CPI、mispredicted、path/coherence oracle、MESI、cache residency/LRU、MSHR 深度、TLB
hit、fetch/issue/commit timing 和任何预测后的 T/E。

新增候选特征进入 100M 模型前必须通过：

1. provenance 审计；
2. exact-input fingerprint 的 label conditional variance/不可约误差审计；
3. trace-level 独立划分的小型 linear/GBDT/MLP probe；
4. address-base、resource-ID、core-placement 的置换试验；
5. heldout ROI CPI、per-core MAPE、fast/slow ordering、scheduler Jaccard 和真实端点
   drift 联合验收。

## 7. 版本与迁移

本合同至少要求以下版本联动：

- raw trace schema：`v28.1-branch-roi-percore`；
- aligned parquet schema：新增四个 branch functional 字段；
- packed cache：branch opportunities 改为 all retired branches；
- model/checkpoint metadata：记录 feature schema、branch metric contract、predictor hash；
- deployment report：输出 `retired_branches`，不再输出或解释为
  `conditional_branches`。

旧 v28 raw 可用于历史结果复现，但不能训练或评价本合同的新 branch head；旧 tensor
cache 和 checkpoint 必须保留旧版本标识，不能静默加载到 v28.1。

## 8. 代码落地后的精确 schema

当前实现使用以下强版本合同：

- `feature_schema=v28.1-base14-branch5-resource11-dynamic8-summary38-relation22`；
- `packed_schema=functional-v28.1-packed-3-resource-context`；
- static token 为 `base14 + branch5 + resource11 = 30` 个 categorical field；
- 每个 active context 重算 `dynamic8 + relation22`；
- `summary38` 和 `uarch28` 作为 chunk side input；
- `resource.npy` 保存每 UOP 的 8 个 int64 equality key：physical line、L1/L2/LLC
  set、LLC bank、DRAM channel/bank/row。它只参与 dynamic/relation 构造，绝不送入
  embedding；`fields.npy` 才是可进入 static cache 的离散模型输入。

训练、teacher-conditioned eval 和 deployment inference 都会严格核对 feature schema、
packed schema、branch contract、维度和 predictor hash。旧 cache 缺少
`resource.npy`，旧 checkpoint 缺少 `contracts`，入口会直接报错并要求重建/重训，
不会尝试兼容加载。

模型实现为三个独立 static encoder（base/branch/resource）和一个 context-only dynamic
encoder。每层 full QKVR 分别投影 local/cross 输出，并使用
`sigmoid(MLP(summary, relation))` 的逐通道 cross gate；static cache key 只包含
`trace/core/chunk/uarch/checkpoint`，不再包含 active-context signature。

## 9. 采集器与 m5 pseudo-op 不变量

- 每核自然 ROI 必须在 500K–1M UOP；probe 和 final 都同时检查 core min/max。
- `REUSE_PROBE_IF_SUFFICIENT=0` 表示不能把 probe 文件直接晋升为正式数据，不表示要
  重新估算 scale。若 probe 已合格，final 必须使用同一个 `PROBE_SCALE` 完整重跑；
  只有 probe 小于 500K 时才允许按观测量估算更大的 scale。
- x86 gem5 pseudo-op 通过 `RAX` 返回。WORKBEGIN、WORKEND 和 QUIESCE 的 inline asm
  都必须声明 `rax` clobber，否则编译器可能让活跃指针跨越 pseudo-op 保存在 `RAX`，
  gem5 返回后会把它当成空指针继续解引用。
- 正式启动前的 source audit 会检查三处 `rax` clobber、ROI 顺序、ROI 内禁用同步/
  atomic，以及业务负载只读共享表和 cache-line 隔离的 per-core 输出。
