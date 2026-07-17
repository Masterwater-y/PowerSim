# TCSim v29 方案与实现合同：共同时间推进与单调前缀退休预测

> 状态：已实现；当前数据合同为 packed-3。
>
> 日期：2026-07-16。
>
> 适用范围：替代当前 v28.1 的 fixed-256 whole-chunk CPI、独立每核预测时钟和
> epsilon-resident rollout；保留 full QKVR、每核 256-UOP functional lookahead、
> cold-start 全 ROI 和 functional-only 部署合同。

## 0. 结论

下一版不再回答：

```text
给定每核一个完整 256-UOP chunk，这个 chunk 需要多少 cycles？
```

而是回答：

```text
在共同虚拟时间 T，给定每核未来 256 条 functional UOP，
未来共同的 h cycles 内，每核能按顺序退休到哪个前缀位置？
```

模型的主输出是每条未来 UOP 的相对退休时间，或与之等价的单调前缀进度；
部署端只维护一个全局虚拟时间。Branch miss 改为每个退休 control-UOP branch token
上的逐事件概率，只累计本步实际退休前缀中的 branch。

这项修改解决的是旧方案的结构性异步上下文：不再把处于不同虚拟时刻的完整
chunk 当成同一并发状态。它不能自动消除模型 rate/progress 误差导致的 functional
cursor 漂移，因此闭环 oracle-head offset 仍是必须验收的主指标。

## 1. 为什么必须更换状态语义

### 1.1 固定 UOP chunk 不是共同时间窗口

当前 chunk 固定为 256 UOP，但不同核、不同阶段的 256 UOP 真实耗时不同。
epsilon scheduler 只能让 chunk 的预测端点接近，不能保证这些 chunk 的真实执行
区间存在共同交集。

定义某个跨核 context 中每核 chunk 的真实区间为：

\[
I_c=[S_c,E_c]
\]

定义共同非重叠 gap：

\[
G=\max(0,\max_c S_c-\min_c E_c)
\]

- `G=0`：所有 active chunk 至少共享一个真实时刻；
- `G>0`：不存在一个被所有 active chunk 同时覆盖的真实时刻。

2026-07-16 对 seed1 的 92 条 trace、306,064 个 oracle-scheduler context 审计表明：

| 核数 | context 数 | `G>0` 比例 | gap p50 | gap p90 | gap p99 |
|---:|---:|---:|---:|---:|---:|
| all | 306,064 | 78.96% | 672 | 1,529 | 1,817 |
| 4 | 74,949 | 67.21% | 295 | 1,451 | 1,721 |
| 8 | 75,726 | 77.96% | 580 | 1,502 | 1,820 |
| 16 | 76,873 | 84.66% | 840 | 1,441 | 1,691 |
| 32 | 78,516 | 85.57% | 1,054 | 1,623 | 1,842 |

这里使用的已经是真实 additive chunk duration，而不是模型预测。因此，约 79% 的
无共同交集首先是 fixed-chunk + endpoint/epsilon scheduling 的结构性问题，不是单纯
的模型误差。oracle gap 最大约 2,000 cycles，也与当前 `epsilon=2048` 的粗粒度一致。

### 1.2 预测闭环会把结构性错位放大

当前部署对每核整块预测并锁存：

\[
\hat E_c=\hat T_c+256\times\widehat{CPI}_c
\]

一次 CPI 偏差会同时影响该核的预测时钟、fast/resident 集合和下一次跨核 chunk
组合，随后再次进入模型，形成反馈。

`W_v28_memory_seq_moderate@seed1,c32` 的 free-running 审计是极端但明确的例子：

| 指标 | p50 | p90 | p99 | max |
|---|---:|---:|---:|---:|
| true non-overlap gap | 1,527,408 | 1,834,453 | 1,871,200 | 1,875,911 |
| true chunk-index spread | 289 | 369 | 383 | 385 |

该 trace 的 aggregate ROI-CPI 误差仍只有约 3.64%。这说明总 cycle 的正负抵消可以
看起来正确，同时每核 functional phase 和跨核联合上下文已经失真。类似的 10K--
300K cycles p99 gap 也出现在多个业务 base/heldout trace；问题并不只发生在 ROI 尾部。

## 2. 核心术语

### 2.1 共同 oracle 时间 `t`

训练样本以一个对所有 active core 相同的真实时刻 `t` 为锚点。对 core `c`：

\[
i_c(t)=\min\{i:\mathrm{commit\_tick}_{c,i}>t_{tick}\}
\]

`i_c(t)` 是该时刻第一条尚未退休的 UOP。模型输入是：

```text
functional_trace[c][i_c(t) : i_c(t) + 256]
```

快核和慢核的 cursor 可以不同，但它们都描述同一个时间 `t` 下的程序状态。

### 2.2 Horizon `h`

`horizon` 是从共同时间 `t` 向未来观察的时间距离，单位是 cycles：

```text
h = 64 cycles
```

它询问的是：

```text
从 t 到 t+64 cycles，每核能退休多少条未来 UOP？
```

它不是：

- 256-UOP lookahead 的长度；
- functional cursor 的 stride；
- 两个训练样本之间的采样间隔；
- 每核独立的预测时钟；
- 由真实 CPI 决定的变长输入长度。

训练可同时使用多个 horizon：

\[
\mathcal H=\{h_1,h_2,\ldots,h_m\}
\]

候选可采用对数间隔，例如 16/32/64/128/256/512/1024 cycles，但最终取值必须由
raw 数据中的退休时间分位数确定，避免绝大多数标签都饱和为 `N=0` 或 `N=256`。

部署步长 `Δt` 与训练 horizon 相关但不是同一个概念。`Δt` 可以固定为某个已训练
horizon，也可以根据预测退休曲线自适应选择。

### 2.3 Commit-time 与 prefix-progress

对未来第 `j` 条 UOP 定义真实相对退休时间：

\[
\tau^*_{c,j}=
\frac{\mathrm{commit\_tick}_{c,i_c(t)+j}-t_{tick}}
     {\mathrm{tick\_per\_cycle}}
\]

由于架构退休按程序顺序进行：

\[
0\le\tau_{c,1}\le\tau_{c,2}\le\cdots\le\tau_{c,256}
\]

给定 horizon `h`，前缀进度为：

\[
N_c(h)=\max\{j:\tau_{c,j}\le h\}
\]

二者互为等价表示：commit-time 回答“第 `j` 条何时退休”，prefix-progress 回答
“经过 `h` cycles 退休到第几条”。

## 3. 数据集合同

### 3.1 样本构造

1. 在共同真实时间轴上采样 `t`，而不是从某个核的 chunk endpoint 采样。
2. 每个 active core 取第一条未退休 UOP 和后续固定 256 条 functional UOP。
3. ROI 尾部不足 256 条时 padding，并用 `valid_uop_mask` 排除。
4. 保存每条有效 UOP 的 `τ*`。
5. 对每个 horizon 保存：

   \[
   y^*_{c,j,h}=\mathbf 1[\tau^*_{c,j}\le h]
   \]

   和：

   \[
   N^*_c(h)=\sum_j y^*_{c,j,h}
   \]

6. 采样必须覆盖“本时间片无 UOP 退休”的 stall 中间状态，不能只在 commit endpoint
   上采样；否则部署时同一 head UOP 长时间不动，模型无法学习剩余等待时间随时间下降。
7. 同一 trace 的高度重叠时间点不能随机拆到 train/validation。应按连续时间 block 或
   完整运行段划分。

### 3.2 允许的过去状态

绝对 `t` 不进入模型。可以输入部署能够同步维护的相对过去状态，例如：

- `elapsed_since_last_commit`；
- 当前 cursor/head 已等待的 cycles；
- cold/warm 与 ROI 起点状态；
- 已提交 functional branch history；
- 从已推进前缀维护的 predictor/cache/shared-resource 摘要；
- active/finished mask；
- 数值 uarch 与 topology 参数。

训练时这些状态只能由严格早于 `t` 的 oracle history 构造。部署时由虚拟时间和已预测
退休事件维护。它们形成 teacher-state/free-running-state gap，必须在闭环评估中测量，
但不能用预测错位窗口制造训练标签。

### 3.3 禁止输入和标签泄漏

禁止输入：

- raw `commit/fetch/issue tick`、真实 CPI、未来 `τ*`；
- 当前或未来 `mispredicted`、cache/TLB miss、MESI、MSHR oracle；
- workload ID、trace ID、raw core ID、chunk/progress ID；
- 由真实时间窗长度产生的 UOP count/fill-ratio shortcut；
- 用当前未来区间的真实事件更新后的 shared state；
- predicted cursor/context 对应的伪标签。

`commit_tick` 只用于离线选择共同 `t`、构造标签和闭环审计。

## 4. P0：资源解码正确性与 ID 置换不变性

共同时间推进不会自动修复输入错误。当前资源输入有两个相互独立的 P0 问题：DRAM
地址被错误解码，以及无物理语义的类别名称进入了 trainable embedding。两者必须在重建
共同时间数据集之前修复，否则新模型仍会学习伪造的资源冲突和 seed shortcut。

### 4.1 已确认的 DRAM 解码错误

当前 `PhysicalResourceMapper` 先计算：

```text
dram_channel = physical_line % num_channels
channel_line = physical_line // num_channels
dram_bank    = channel_line % banks
dram_row     = channel_line // (banks * lines_per_row)
```

但当前 gem5 配置为：

```text
addr_mapping          = RoRaBaCoCh
num_channels          = 8
interleave granularity= 64 B
banks_per_rank        = 16
ranks_per_channel     = 2
burst_size            = 64 B
row_buffer_size       = 8 KiB
bursts_per_row_buffer = 128
```

gem5 的 `DRAMInterface::decodePacket` 会先通过 `AddrRange::getOffset` 去掉 channel
interleave bits，再除去 64-B burst 和 128 个 column bursts，然后依次解 bank、rank、row。
对当前 `range=0:4294967296:0:64:128:256` 的连续 channel bits，等价解码为：

```text
physical_line = paddr // 64
channel       = physical_line % 8
channel_line  = physical_line // 8

column = channel_line % 128
x      = channel_line // 128
bank   = x % 16
x      = x // 16
rank   = x % 2
row    = (x // 2) % rows_per_bank
bank_id = rank * 16 + bank
```

因此当前 `dram_bank` 实际主要取了 column 的低四位，`dram_row` 又漏掉了 rank 因子；
`row_key=(channel, bank, row)` 也会把不同 rank 的访问误判为同一 bank/row。由此派生的
same-bank、same-row、row-reuse、row-conflict 和 fanout 都不可信。`uarch_profile.json`
中的 `banks_per_channel=16` 名称也不准确：该值实际来自 gem5 的 `banks_per_rank`，而
profile 完全没有保存 `ranks_per_channel` 和 `addr_mapping`。

上面的 `% 8`/`// 8` 只对当前连续 interleave bits 成立。正式实现不能写死该公式，必须
按 trace 对应的 gem5 `AddrRange`、mapping 和 DRAM 参数解码，并对不支持或信息不完整的
配置硬失败。

### 4.2 资源解码合同

下一版必须提供唯一的、可测试的 `Gem5AddressDecoder`，并由数据构造和审计共同调用：

- profile/provenance 显式保存 `addr_mapping`、地址范围和 interleave masks/match、
  `burst_size_b`、`row_buffer_size_b`、`bursts_per_row_buffer`、`banks_per_rank`、
  `ranks_per_channel`、`rows_per_bank` 和 channel 数；禁止用含混的
  `banks_per_channel` 替代。
- resource exact keys 至少包含 `channel/rank/bank/row/column`；bank competition key 使用
  `(channel, rank, bank)`，row key 使用 `(channel, rank, bank, row)`。
- same-row、different-row-conflict、row reuse、channel/bank fanout 和 pressure 全部只从
  修正后的 exact keys 派生。
- L1/L2/LLC 的 set/slice 映射也必须和对应 Ruby controller 做 differential test；当前
  简单取模在测试通过前只能视为假设，不能当作已验证事实。
- cache 构建时把完整 decoder 参数和 hash 写入 metadata；参数缺失、hash 不匹配或出现
  未支持的 mapping 时立即失败，不能回落到默认值静默构造。

必须增加两层正确性测试：

1. 对当前配置使用 golden address 边界，覆盖 column、bank、rank、row 和 channel 的每个
   翻转点。
2. 对随机物理地址，将 Python decoder 与 gem5 C++ decoder/exported tuple 逐项比较，
   要求 100% 相等。

只要 raw trace 保留了正确的 physical address，且每条 trace 的 `config.ini`/profile
可追溯，这项修复不要求重跑 gem5 raw；但 packed rollout、tensor cache 和 checkpoint
都必须升 schema 后重新构造/训练。

### 4.3 无物理语义的 learned ID 仍未消除

当前 `StaticChunkEncoder` 对全部字段统一建立 `nn.Embedding`，因此随机置换后的
`l1_set/l2_set/llc_set/llc_bank/dram_channel/dram_bank` 仍会作为类别名称直接影响输出。
“每条 trace 随机置换一次”只是一种有限的数据增强，并不提供数学上的置换不变性。

已有自然消融给出了直接证据：`memory_seq c32` seed0/seed1 的真实 cycles、summary 和
resource exact keys 完全相同，只有 set/bank/channel 的随机类别名称改变，模型预测的
逐核 spread 却从 778,628 变成 584,417 cycles。物理语义不变而预测大幅变化，说明模型
正在使用类别名称 shortcut；这不是正常的 seed 泛化误差。

### 4.4 下一版 ID 合同

- 不输入 workload/trace/chunk/core categorical ID。
- 对称硬件资源的 set/bank/channel/rank 编号不得进入 trainable categorical embedding；
  它们只作为 exact key 在 relation builder 中判断 equality、alias、fanout、occupancy 和
  conflict，随后输入有物理语义的 relation/pressure 数值。
- 若硬件确有 NUMA、controller 或 slice 非对称性，输入显式的距离、带宽、容量和拓扑
  参数，而不是让模型从任意编号猜测。
- `local_pc_id`、`local_line_id` 是每条 trace 内的 first-touch ordinal，同样不具有跨
  trace embedding 语义。P0 默认采用 equality/reuse/alias relation；随机双射只能作为
  测试或增强，不能代替结构上的不变性。
- branch predictor 的 PC 信息派生为 predictor index/tag/alias relation、reuse 和 history；
  不直接嵌入 raw PC，也不把 first-touch ordinal 当 predictor state。
- predictor hash 和 uarch hash 只用于 provenance/兼容性检查；若训练多种 predictor，
  输入其数值语义配置，而不是 hash embedding。
- position embedding `0..255` 表示 lookahead 中的程序顺序，具有稳定语义，可以保留。

### 4.5 强制不变性门禁

至少进行 local-PC relabel、local-line relabel、resource-ID relabel 和 core placement
permutation 四类测试。测试必须在同一有效输入语义下重命名所有 nominal IDs，并验证：

- exact relation、dynamic relation 和预测逐项相同或仅有规定的浮点误差；
- CPI/progress/commit-time、branch PMU 和跨核 spread 均不改变；
- 上述 `memory_seq c32` 自然消融在语义输入相同时得到相同预测，而不是仅要求统计均值
  大致接近。

只要 resource-ID relabel 会改变预测，这一版数据/模型就不得进入全量训练。

## 5. 模型结构

### 5.1 保留 full QKVR，但不能再先平均池化

当前 v28.1 `FunctionalInteraction` 最终把全部有效 UOP token 平均为一个 `h_dyn`，再由
两个 scalar linear head 输出 CPI 和 branch miss。这样会：

- 丢失 stall 在 256-UOP 前缀中的位置；
- 让 branch-sparse 窗口中的 branch token 被大量非 branch UOP 稀释；
- 无法输出逐 UOP commit-time 或逐 branch miss probability。

下一版 interaction 必须保留并返回：

```text
H_token: [N_core_rows, 256, d_dyn]
H_core:  [N_core_rows, d_dyn]        # 可选 summary，只作辅助
```

不同 batch sample 的 core 仍可在 collate 中展平，通过 `sample_ptr` 隔离 cross-core
attention；不能让不同 sample 互相 attention。

### 5.2 单调 commit-time head

每个 contextual token 输出一个非负退休间隔：

\[
\hat d_{c,j}\ge0
\]

并累计：

\[
\hat\tau_{c,j}=\sum_{i=1}^{j}\hat d_{c,i}
\]

`d_j` 表示相邻退休事件的时间间隔，不是孤立指令 latency。多个 UOP 可同周期退休，
因此实现必须允许零或近零 gap；长 stall 表现为某个较大的 gap。所有 `d_j` 都由完整
核内序列和跨核 context 联合预测。

也可用 ordered time-bin/CDF 参数化，只要从结构上保证 `τ_j` 单调，而不是仅靠一个
容易失效的 monotonic penalty。

### 5.3 多 horizon prefix 辅助输出

从 `τ` 构造可微的退休概率：

\[
p^{commit}_{c,j,h}=
\sigma\left(\frac{h-\hat\tau_{c,j}}{s}\right)
\]

预测进度：

\[
\hat N_c(h)=\sum_j p^{commit}_{c,j,h}
\]

沿 UOP 序列，退休概率应单调不增；随 horizon 增大应单调不减。

### 5.4 Per-branch PMU head

独立 branch MLP 作用于每个退休 control-UOP branch token：

\[
p^{br}_{c,j}=P(\mathrm{branch}_j\text{ mispredict})
\]

- 普通 UOP 不计算 branch loss；
- gem5 中每个实际退休且 `StaticInst::isControl()` 的 UOP 都是一次 branch opportunity；
- 一个 architectural macro 可以因 x86 微码循环包含并退休多个 control UOP，例如
  `IDIV`，这些事件不能合并或丢弃；
- branch opportunity/count 从 functional prefix 精确统计，不预测；
- `mispredicted` 只作为标签，并且必须满足 `mispredicted => is_branch`；
- 该口径与 gem5 `branchPred.committed/mispredicted` 对齐，是“退休 control UOP”口径，
  不应误称为只统计 architectural branch macro 的硬件 PMU 口径；
- timing head 已学习包含 branch recovery 的总退休时间，P0 不再显式加一次 branch
  penalty，避免 double count。

当前 branch 可辨识性缺口必须同步处理：predictor index/tag alias、counter/history state、
RAS/indirect-target reuse、speculative update/squash 的可部署近似。仅提高 branch loss 权重
不能补充缺失信息。应同时保留 deterministic predictor/replay baseline 做对照。packed
cache 没有保存 exact branch target，因此当前 replay baseline 只报告 direction-only
gshare；禁止用“下一条 architectural macro PC”伪造微码 branch target。

## 6. Loss

建议总损失：

\[
L=\lambda_tL_{time}
 +\lambda_nL_{progress}
 +\lambda_{cum}L_{cumulative}
 +\lambda_bL_{branch-token}
 +\lambda_{bc}L_{branch-count}
\]

### 6.1 Commit-time

\[
L_{time}=\mathrm{SmoothL1}(\log(1+\hat\tau),\log(1+\tau^*))
\]

使用 `log1p` 限制几百/几千 cycles 长尾对梯度的支配，并对有效 UOP mask 后归一。

### 6.2 Prefix progress

对多个 horizon 监督 token CDF/BCE 和 count Huber：

\[
L_{progress}=L_{prefix-BCE}+\alpha\,
\mathrm{Huber}(\hat N_c(h),N^*_c(h))
\]

### 6.3 长期有符号偏差

在连续、oracle 对齐的时间序列上增加累计 progress 误差：

\[
L_{cumulative}=
\left|\sum_k\Delta\hat U_{c,k}-\sum_k\Delta U^*_{c,k}\right|
\]

每个输入仍来自共同 oracle 时间；不把预测 cursor 的 UOP 组合拿来生成标签。该项的目的
是抑制单步小偏差长期同号累积，而不是训练 predicted-window label。

### 6.4 Branch

逐 branch token 使用校准的 BCE；在 horizon `h` 内的预期 miss 数为：

\[
\hat M_c(h)=\sum_j
p^{commit}_{c,j,h}\,b_{c,j}\,p^{br}_{c,j}
\]

再与真实 miss count 做 Huber。训练使用 soft commit gate；部署才使用硬 prefix。

所有 loss 先按有效 token/event/core/trace 正确归一，再确定权重。不能通过简单放大
`L_branch`、center/slow/scheduler loss 掩盖输入不可辨识或数据 OOD。

## 7. 部署 rollout

### 7.1 唯一状态时间

部署维护：

```text
global_time T
cursor[c]
active_cycles[c]
retired_uops[c]
retired_macros[c]
elapsed_since_last_commit[c]
predictor/cache/shared dynamic state
256-UOP lookahead[c]
active/finished mask
```

不再维护用于联合 context 的独立 `T_pred[c]`，也不锁存一个完整 256-UOP chunk 的
duration 等待其他核追赶。

### 7.2 单步状态转移

1. 在共同 `T` 对所有 active core 联合预测 `τ[c,j]` 和 branch miss probability。
2. 选择共同 `Δt`。一种候选策略是设目标 stride `S=32/64`：

   \[
   \Delta t=\min_c\hat\tau_{c,S}
   \]

   并加入合理的上下界/尾部处理。这样最快核推进约 `S` 条，其他核推进不超过 `S`，
   不会一次耗尽 256 条 lookahead。
3. 每核计算：

   \[
   n_c=\max\{j:\hat\tau_{c,j}\le\Delta t\}
   \]

4. 只消费 `cursor[c]` 开始的前 `n_c` 条 UOP，补充 lookahead 尾部。
5. 只累计该 prefix 中的 branch PMU；更新可部署的动态/共享状态。
6. 对仍 active 的核累计本步 cycles；若核在步内结束，按预测最后退休时间截断。
7. `T ← T + Δt`。

慢核可以在某一步 `n_c=0`，但它的等待年龄和动态状态必须继续前进。若下一次把相同
lookahead 当成“刚开始等待”，模型可能反复预测同一个剩余等待时间并永久不退休。
因此 common-time grid 中的 no-commit 样本和 `elapsed_since_last_commit/head_age` 是硬要求。

## 8. CPI、cycles 与 PMU 统计

共同时间推进后，cycles 是 rollout 的积分变量，不需要独立预测：

\[
cycles_c\mathrel{+}=\Delta t_c
\]

\[
uops_c\mathrel{+}=n_c
\]

按照项目已确定的定义：

\[
CPI_{uop,ROI}=\frac{\sum_c cycles_c}{\sum_c uops_c}
\]

\[
CPI_{macro,ROI}=\frac{\sum_c cycles_c}{\sum_c macro\_inst_c}
\]

Branch：

\[
\hat M_{ROI}=\sum_{steps,c}\sum_{j\le n_c}b_{c,j}p^{br}_{c,j}
\]

\[
BranchMissRate_{ROI}=\frac{\hat M_{ROI}}
 {\sum_{steps,c}\sum_{j\le n_c}b_{c,j}}
\]

先累加 count 再求 rate；不得平均无权重的逐窗 rate。无 branch 的时间片不定义 rate，
但 miss count 为零。

## 9. 偏移分析中发现、不得遗忘的其他问题

### 9.1 Endpoint 近似对齐不等于区间并发

epsilon 只约束预测端点。即使端点差不大，只要 chunk 持续时间不同，start/midpoint 仍可
相差很大，也可能完全无交集。减小 epsilon 只能在吞吐量和粗粒度上折中，不能从语义上
修复 fixed-256 interval mismatch。

### 9.2 闭环漂移不只是 trace 尾部

旧 raw 确实曾有某核 `WORKEND` 后仍采集 `pthread_join/futex` 的 ROI 尾部污染；v28.1
已通过 per-core ROI gate、`M5_QUIESCE` 和最后一核直接结束修复。但 full-core window 和
ROI 中段仍观察到大 gap，说明尾部污染不是当前 10K--1M cycles 漂移的主因。

### 9.3 Aggregate ROI 正确可以掩盖每核轨迹错误

不同核的正负 cycle 误差会在 `sum(cycles)/sum(uops)` 中抵消。必须同时报告 per-core
endpoint/makespan、signed progress bias 和 oracle-head span；不能用 pooled ROI-CPI 证明
闭环 trajectory 正确。

### 9.4 相同可见输入对应不同 timing

同一 functional lookahead 的退休时间还依赖 cold/warm cache、ROB/LSQ/MSHR、在途 miss、
branch predictor、memory queue 和其他核压力。缺失这些可维护状态时，模型只能学条件
均值。Loss 不能解决不可辨识输入；训练前必须做 exact/near visible-signature 的 label
conditional variance 审计和单样本过拟合门。

### 9.5 Mean pooling 与标量 head 丢失位置语义

当前 `h_dyn` 对全部 UOP 平均池化后预测 scalar CPI/branch rate。即使 token encoder 能
区分指令，状态转移仍只看到整窗平均值。新方案必须保留 per-token hidden state，才能
区分“miss 在 prefix 开头”和“miss 在 prefix 末尾”。

### 9.6 ID shortcut 与跨 seed/binary 不稳定

first-touch local PC/line ID、固定资源编号、core ID 都可能让模型记忆训练 trace 身份，
而不是学习 equality、alias 和 topology。必须执行第 4 节的 ID 重标号与置换测试。

### 9.7 Branch 是独立的可辨识性失败

现有 heldout branch miss 系统性过预测，不是单纯 CPI timing 误差。原因包括 branch token
被平均池化稀释、predictor index/alias/state 缺失，以及 local-PC ordinal shortcut。
per-token branch head 是必要条件，但若 predictor 状态仍不可见，仍只能输出平均概率。

### 9.8 数据机制覆盖和 seed 敏感性

Redis heldout 暴露 dependent lookup depth、16/32/64 MiB shared working set、Zipf +
uniform tail、private-state size 的机制缺口；MySQL/Marine 也有稳定的分布外偏差。seed1
还显示部分 c32 base 对地址映射、branch history 和局部顺序过敏。新 scheduler/head 不会
自动修复 OOD，仍需 mechanism cube、多 seed 等价样本和一致性验证。

由于 seed1 已经被用于多轮方案诊断，它已是 development evidence；下一版最终一次性
泛化测试应另留未参与调参的新 seed 和新 binary variant。

### 9.9 True-time 变长输入会泄漏 throughput

若训练窗口用共同真实区间切出每核不同数量的 UOP，那么 per-core token count 本身就由
真实 throughput 决定，部署前并不知道。下一版固定 256 条输入，只把真实时间用于标签，
避免 UOP count/fill ratio 成为 CPI shortcut。

### 9.10 Predicted window 不能产生训练标签

预测 cursor 错位后，不同核 UOP 可能没有共同真实区间，无法定义一个有意义的联合 timing
标签。Predicted context 只用于部署诊断；训练输入和标签始终来自共同 oracle `t`。可以在
连续 oracle 样本上计算累计误差，但不能用预测窗口强行对齐真实 tick。

## 10. 数据分布与采样仍需保留的合同

- 训练 workload、core-count、trace 等权或受控采样，不能由 trace 长度/窗口数支配梯度。
- 保留真实业务范围内的内存压力，但不重新引入 CPI 数十/上百的纯 DRAM chase 主分布。
- Redis mechanism cube 作为通用机制训练，不把 heldout binary 直接偷渡进训练。
- c4/c8/c16/c32 分开报告，不能用 global pooled aggregate 替代 workload-equal 指标。
- development validation 与最终 untouched test 分离。
- raw schema、feature contract、predictor hash、uarch hash、horizon set 都写入 cache 和
  checkpoint metadata；不兼容时硬失败。

## 11. 验收指标

### 11.1 Oracle one-step

- 每 UOP commit-time log-MAE、p50/p90/p99；
- 每个 horizon 的 progress count MAE、signed bias、`N=0/K` 饱和率；
- prefix 单调违规数必须为零；
- branch token BCE/AUC/Brier/校准；
- branch count/rate absolute percentage-point error；
- visible-signature conditional variance 与 single-sample overfit。

### 11.2 Free-running

虚拟时钟在所有 active core 间天然相同，但还要用 seed0/开发 trace 的真实 tick 审计。
不能只使用：

\[
head\_residual_c(T)=commit\_time(cursor_c(T))-T
\]

因为即使 cursor 完全正确，下一条 UOP 也通常尚未退休，`head_residual` 仍大于 0。
真正的 cursor 时间错位应按该 cursor 对应的真实区间计算。令：

\[
I_c=[commit\_time(cursor_c-1),\ commit\_time(cursor_c))
\]

若 `T` 位于 `I_c` 内，interval offset 为 0；若预测 cursor 超前，则取区间左端减
`T`；若预测 cursor 落后，则取区间右端减 `T`。实现同时报告 head residual 和
cursor-interval offset，但 headline 与漂移斜率使用后者。

必须报告：

- cursor-interval offset p50/p90/p99/max 和随 ROI 长度的增长斜率；
- head residual p50/p90/p99/max，作为下一退休事件距离而不是漂移；
- 跨核 oracle-head span；
- 每核 cumulative progress signed error；
- ROI micro/macro CPI、makespan、per-core endpoint；
- branch miss count/rate；
- 每 workload、每核心数、train/base 与 business heldout；
- 推理 steps/s、UOP/s、GPU memory 和 static embedding cache hit rate。

新方案只在以下条件同时满足时通过：虚拟共同时间语义成立、oracle-head drift 不持续
发散、heldout CPI/PMU 改善、且吞吐量可接受。仅消除 resident 计数或保持 aggregate CPI
不构成通过。

## 12. 实施顺序

1. 冻结当前 v28.1 checkpoint、结果和偏移审计作为 A/B baseline。
2. P0 修复 gem5 resource decoder/profile 合同；用 golden boundary 和随机地址 differential
   test 验证 DRAM 与 Ruby cache 映射。
3. P0 从模型 token 输入移除 nominal resource/local entity ID embedding，改用 exact-key
   relation，并通过四类 permutation invariant tests。
4. 审计现有 raw 是否包含完整逐 UOP commit tick、physical address、retired control-UOP
   branch 和 mispred label；字段足够时不重采 raw，只新建 dataset/cache 版本。
5. 实现共同时间采样器、horizon 分布审计和无泄漏检查。
6. 让 full QKVR 返回 per-token state；加入 monotonic commit-time/progress head。
7. 加 per-branch token head、predictor feature/replay baseline 和 PMU count 聚合。
8. 替换 loss；先做 toy trace 和小样本 overfit。
9. 实现 single-global-time rollout、stall age/dynamic-state 更新和 ROI 尾部截断。
10. 先在少量 c4/c8 trace 做 oracle/free-running 语义验证，再重建全量 tensor cache。
11. 8 卡训练并运行 seed0 全量 A/B；通过 development gate 后，使用新的 untouched seed/
    binary variant 做最终验证。

当前 v28.1 数据、cache 和 checkpoint 在新方案通过前不删除，也不得静默加载到新模型。

## 13. 当前实现状态（2026-07-16）

### 13.1 已实现的硬合同

数据构建、训练 checkpoint 和推理 cache 使用独立的 v29 schema，旧 v28 packed
rollout 不能静默加载。训练 cache 构建时逐 trace fail-fast 检查：

- 固定 `K=256`，`sample_period=64 cycles` 必须属于 horizon 集；
- 每核完整 ROI 为 `0.5M--1M UOP`；
- 每核完整 ROI micro-CPI 不高于 10；
- ROI 内 ISA atomic UOP 数为 0；
- `commit_tick` 单调且位于本核 ROI 内；
- 所有 core 的 ROI begin tick 完全相同，与部署“所有流在 T0 active”一致；
- branch miss 只能附着在退休 control-UOP branch token；一个 macro 允许包含多个微码
  control UOP，且全部保留并逐事件计数；
- common-time smoke 截断只能取连续网格，禁止用 `linspace` 伪装成 64-cycle 邻接样本；
- train/validation 时间 block 之间有最大 horizon guard，且 active core 的完整
  256-UOP lookahead 必须位于本 block 内；trace 末端不足 256 个有效 UOP 的 padding
  样本不进入训练/验证；
- cache 覆盖采用临时目录和原子替换，失败时保留旧 cache。

这里有两个名称相近但完全不同的概念：

```text
FFATOMIC / --ff-atomic
  = gem5 在 ROI 前使用 AtomicSimpleCPU 快速启动；第一次 WORKBEGIN 切到 O3+Ruby

ROI atomic UOP = 0
  = 被建模 ROI 中没有 LOCK/CAS/XCHG 等同步原子指令
```

当前训练 raw 必须同时从 `collect.meta`、gem5 命令行和切换日志证明：

```text
AtomicSimpleCPU -> first WORKBEGIN -> O3+Ruby ROI
```

Atomic 阶段为 `atomic_noncaching`，所以 Ruby cache 在 ROI 入口是 cold。这与当前确定的
“不 drop、统一建模 cold-start 全 trace”一致。cache metadata 中分别记录
`collection_provenance.ff_atomic_verified=true` 与 `quality.roi_atomic_uops=0`，不再用
一个含糊的 `atomic=0` 表达两者。

### 13.2 数据划分

- seed0 的 16 个训练 workload：按连续时间 block 划分 train/in-distribution validation；
- validation 只使用 c4/c8/c16/c32，不使用 c1；
- seed0 的 7 个 business variant：`development_heldout`；
- seed1 的 c4/c8/c16/c32：仅 `deployment_inference`，不进入训练和 checkpoint 选择；
- seed2 及以后：单独命名为 `final_untouched`，不得与开发结果混合；
- manifest 对请求的 raw root、23 个 workload 目录、重复 root 和构建失败做完整性检查，
  缺失时状态为 `fail`，训练入口拒绝加载。

训练用 `WeightedRandomSampler` 做 trace-equal sampling。由于每个 workload 在 seed0 有
相同核心数组合，这也避免长 trace 或窗口多的 workload 支配梯度。checkpoint 选择默认
使用确定性、时间分散、trace-equal 的 512 个 validation sequence；完整验证由训练后的
8 卡推理任务独立执行，避免每 1000 step 被全量 eval 长时间阻塞。

### 13.3 模型和 loss

当前模型保留 full QKVR：

- local `Q/K/V` 在每核 256-UOP token 内建模；
- cross-core `R` 对同一共同时间样本中的其他 active core 全部有效 token 做 attention；
- batch 内不同 core count 只在 core 轴展平，通过 `sample_ptr` 恢复样本边界，UOP 轴固定
  为 256，不做 32-core UOP padding；
- nominal `core/workload/PC/line/set/bank/channel` ID 不进入 embedding；精确 key 只用于
  构造 equality/fanout/conflict relation；
- timing head 输出正 gap 的累积和，因此每条 UOP retirement time 天然单调；
- branch miss 使用独立 MLP head；它与 timing head 共享 contextual token，但不共享最终
  标量头，也不需要训练两套完整模型。

loss 为：

```text
L = L_commit_log_huber
  + 0.5 L_prefix_BCE
  + 0.5 L_progress_count
  + 0.25 L_contiguous_cumulative
  + 0.1 L_branch_token_BCE
  + 0.1 L_branch_count
```

不存在 slow/center/scheduler loss。prefix 与 branch-count 使用可导 soft gate；部署消费
prefix 时才使用单调 retirement time 的 hard comparison。branch miss probability 只在
branch token 上训练，并只对每次实际消费的 branch token累计一次。

默认正式模型为 112,206,324 参数、BF16、`sdpa_backend=auto`。第一次 rank-0 CUDA batch
开启一次 attention profiler，打印实际 SDPA kernel；可通过 `PROFILE_ATTENTION=0` 关闭，
也可用 `SDPA_BACKEND` 和 `AMP_DTYPE` 覆盖。

### 13.4 部署推理

部署推理的公共目录、日志、NumPy 向量化、CPU/GPU cache、计时和验收规范统一见
[`inference_framework_standard.md`](inference_framework_standard.md)。后续版本默认复用该框架。

已实现两种严格分离的输入容器：

- labeled v29 cache：仅供 oracle one-step 和部署闭环后的误差审计；
- label-free functional cache：磁盘上没有 `commit_tick.npy` 和 `branch_miss.npy`，可直接
  从 functional parquet 构建并运行部署 rollout。

free-running rollout 从所有 core 的 functional cursor 0 开始，只维护一个全局虚拟时钟。
上下文由预测 cursor 构造，模型输入字典中禁止出现 oracle label。每步按最快核第
`target_stride` 条预测退休时间选择共同 `Delta t`，每核只消费 `tau<=Delta t` 的前缀。
UOP、macro 和 branch 事件均做 exact-once 断言；完整 rollout 还断言每核积分
`active_cycles` 等于其预测 endpoint，防止等待核 cycle 被重复累加。

报告同时包含：

- 每 workload、每 c4/c8/c16/c32 的 ROI micro/macro CPI；
- makespan、逐核 endpoint；
- branch miss count/rate；
- oracle one-step commit/progress/branch 指标；
- cursor-interval drift、head residual、cumulative progress error；
- steps/s、UOP/s、分段 timing、GPU peak memory、CPU window cache 与 GPU static
  token cache 的独立命中率。

核心数 headline 是 workload-equal mean，不生成无意义的全局 pooled headline。

### 13.5 已完成验证与尚未完成项

已完成：

- Python/Torch v29 回归测试；
- 10K 随机/边界地址与 gem5 C++ decoder differential test，0 mismatch；
- 全部 207 个 seed0/seed1 raw slice 的 FFATOMIC provenance 检查，0 missing、0 fail；
- 全部 207 个 raw slice 的 per-core ROI start span 为 0 tick；
- 真实 c4 trace 的 `build -> backward/train -> checkpoint -> oracle one-step -> predicted
  rollout` smoke；
- label-free functional cache 物理删除 oracle array 后的部署推进 smoke；
- 8 卡训练和 8 卡分片评估脚本参数 dry-run。

尚未完成，因此不能提前宣称 v29 精度通过：

- 全量约 207 个 v29 tensor cache 构建；
- 8 卡 30K-step 正式训练；
- seed0 base/business heldout 全量 A/B；
- seed1 部署验证和新的 seed2/final variant 一次性最终验证。

### 13.6 一键命令

全量 cache：

```bash
cd /data00/yinhaolang/TCSim && mkdir -p logs/v29 && nohup env WORKERS=64 MIN_UOPS_PER_CORE=500000 MAX_UOPS_PER_CORE=1000000 MAX_FULL_UOP_CPI=10 bash scripts/build_v29_full.sh > logs/v29/build_full.log 2>&1 &
```

8 卡 watchdog 训练：

```bash
cd /data00/yinhaolang/TCSim && GPUS=0,1,2,3,4,5,6,7 NPROC=8 TARGET_STEPS=30000 SDPA_BACKEND=auto AMP_DTYPE=bf16 PROFILE_ATTENTION=1 bash scripts/launch_v29_100m_30k_watchdog.sh
```

同机脚本使用 `torchrun --standalone`，由 torchrun 自动选择本机 rendezvous TCP
port；不需要手工设置 `MASTER_PORT`，也不会把固定端口冲突带入 watchdog 重启。

seed0 base + heldout 全量评估：

```bash
cd /data00/yinhaolang/TCSim && CKPT=ckpt/tcsim_v29_global_time_100m_8gpu_30000/best.pt SPLITS=seed0_inference,development_heldout GPUS=0,1,2,3,4,5,6,7 MODE=both RESUME=1 bash scripts/run_v29_eval_8gpu.sh
```

seed1 仅部署评估：

```bash
cd /data00/yinhaolang/TCSim && CKPT=ckpt/tcsim_v29_global_time_100m_8gpu_30000/best.pt SPLITS=deployment_inference GPUS=0,1,2,3,4,5,6,7 MODE=both RESUME=1 bash scripts/run_v29_eval_8gpu.sh
```
