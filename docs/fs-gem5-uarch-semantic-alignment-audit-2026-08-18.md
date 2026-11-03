# FastSim 与 gem5 微架构语义对齐审计（2026-08-18）

> 规范性目标以
> [`project-goal-and-semantic-contract.md`](project-goal-and-semantic-contract.md)
> 为准。本文保存组件级证据。后续 PMU completeness 审计发现当前
> `taotrace-path-class-v2` 不能保证 FST committed memory coverage，因此本文中旧的
> cache PMU 正式性表述必须按新合同降级为 diagnostic，直到 P0 守恒门禁通过。

## 1. 结论和适用边界

当前 FastSim 与正式 C4/C8 gem5 采集目标尚未达到“微架构状态机语义完全
一致”。当前结果只能定义为：

> **对齐 gem5 主要几何参数、面向 committed functional trace 可观测子集的
> trace-driven 模型。**

不能把它定义为：

> **相同输入事件下与 gem5 BaseO3CPU + MESI_Three_Level 状态机等价的模型。**

因此，当前 CPI 误差混合了两类量：

1. FastSim 对已经定义为同一语义的状态转换所产生的近似误差；
2. FastSim 没有实现、没有启用，或 committed functional input 无法提供的
   gem5 状态转换。

这一区分决定了 baseline 的可声明范围。gem5 仍是唯一 CPI oracle，但当前
FastSim 结果不能被描述为“同一微架构的纯模拟误差”。更准确的名称是
`gem5-geometry-aligned committed-functional baseline`。

现有 `tools/audit_fs_profile_identity.py` 在 C4/C8 上都报告 129 个直接或派生
字段，其中 121 个相同、8 个不同，并明确给出 `full semantic equivalence: NO`。
`121/129` 不是整体语义对齐率，因为它：

- 只覆盖 FastSim 已显式表示的部分字段；
- 会把“数值存在但组件关闭”的字段计为匹配；
- 不检查完整 pipeline lifetime、ITLB、I-side request stream、Ruby transient
  states、SimpleNetwork 带宽/队列、DRAM refresh/turnaround 等状态机；
- 不检查正式运行中候选状态机是否实际 bypass。

机器可读结果位于：

```text
tmp/uarch-semantic-audit-20260818/c4/summary.{json,md}
tmp/uarch-semantic-audit-20260818/c8/summary.{json,md}
```

这些是项目内临时审计产物，不进入 Git；本文保存可长期引用的事实和结论。

## 2. 审计基准、身份和方法

### 2.1 正式 gem5 目标

本次只审计当前正式 FST v7 C4/C8 数据，不混入旧 SE、cold-slice、100K pilot
或其他实验结果。正式输入和 gem5 配置位于：

```text
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/source
```

代表配置为：

```text
fst-v7/cases/04c-706.stockfish_r/config.ini
fst-v7/cases/08c-706.stockfish_r/config.ini
```

正式目标身份是：

| 维度 | gem5 正式目标 |
|---|---|
| ISA / mode | x86 full-system |
| ROI CPU | `BaseO3CPU`，1 thread/core |
| core count | C4 或 C8 |
| nominal clock | 3 GHz；`config.ini` 实际 clock period 为 333 ps |
| memory | 3 GiB，8-channel DDR4-2400，64 B channel interleave |
| cache/coherence | `MESI_Three_Level` |
| shared slices | 8 shared L2/L3 controllers + 8 directories |
| network | `SimpleNetwork`，3 virtual networks |
| measurement | functional warmup 后每核 10M measured user records |

对每个 core-count 组做了归一化配置比较，只排除 TaoTrace 输出路径和
`uarch_profile_path` 等非微架构身份字段：

| 组 | cases | 比较的配置键值 | workload 间差异 | 归一化摘要 SHA-256 |
|---|---:|---:|---:|---|
| C4 | 10 | 48,528 | 0 | `4207fa7e64b8661a1dd078474c3890c73634d16d735e27786aebed13fe1ae187` |
| C8 | 10 | 82,368 | 0 | `ddc9a7c89d872f0d04ee3cd34536f595e7eeedff685c48d86d50e9634207fd5e` |

所以本次发现的差异不是某个 workload 意外使用了另一套 gem5 配置。C4/C8
之间的核数、私有 controller 和网络端点数量差异是目标拓扑本身的预期差异。

### 2.2 FastSim 正式入口和有效配置

正式 FastSim profile 是：

```text
configs/gem5-v28_1-fs-user.cfg
configs/gem5-v28_1-fs-user-plus-kernel.cfg
```

二者包含共享的：

```text
configs/gem5-v28_1-time-epoch.cfg
```

有效配置从正式报告读取，而不是只读基础 cfg。代表报告为：

```text
tmp/committed-ledger-repair-20260817/formal-default-profiles/
  cases/04c-706.stockfish_r/user.json
  cases/08c-706.stockfish_r/user.json
```

正式 runner 额外传入 3 GiB DRAM 和目标 core count。当前正式报告确认：

- C4/C8 core count 正确；
- `dram.size_bytes=3221225472`；
- `dram.channels=8`；
- FS DTLB 为 `timing_walk/12`、1 walker、no coalescing；
- private L2/LLC 为 TreePLRU；
- L1I、rename free list 和 branch shadow 均关闭。

### 2.3 判定等级

本文不用单一百分比表达对齐程度，而按以下四类判定：

| 判定 | 含义 |
|---|---|
| 一致 | 数值和由输入驱动的状态转换语义相同；实现机制可以不同 |
| 条件一致 | 只在无竞争 lower bound 或当前可观测事件子集上相同 |
| 部分一致 | 几何/容量相同，但 lifetime、排队、恢复或输入事件集合不同 |
| 不一致 | 组件关闭、状态机缺失、正式运行 bypass，或有效参数不同 |

## 3. 全组件审计

### 3.1 目标拓扑、clock 和运行身份

| 组件 | gem5 | FastSim 有效状态 | 判定 |
|---|---|---|---|
| ISA/mode | x86 FS | 使用 FS trace/profile | 一致 |
| cores | C4/C8 | 正式 runner 覆盖为 4/8 | 一致 |
| SMT | 1 thread/core | 1 committed stream/core | 一致于当前目标 |
| nominal core clock | 3 GHz | cycle-domain 3 GHz 参数 | 条件一致 |
| exact timebase | 333 ps core period；memory timing 以 tick 表示 | 多数 timing 被量化为整数 core cycles | 部分一致 |
| memory capacity | 3 GiB | 正式 runtime 为 3 GiB | 正式结果一致，profile 不自包含 |
| coherence protocol | MESI three-level | compact MESI/directory | 名称相同，状态机部分一致 |

时间换算不能继续使用硬编码 round 值。例子：

- gem5 `tRAS=32000 ps`，相对 333 ps core period 是约 96.096 cycles；
- `tCS=1666 ps`，相对 core period 是约 5.003 cycles；
- `tBURST=3332 ps`，相对 core period 是约 10.006 cycles。

现有 direct audit 分别记为 96、5 和 10。gem5 在 tick 域调度，而 FastSim 在
整数 core-cycle 域调度，因此这是量化近似，不是精确时间语义。未来 identity
gate 应在 tick 域比较，或保留有理数时间。

### 3.2 O3 pipeline 和 functional units

| 组件 | gem5 | FastSim | 判定 |
|---|---|---|---|
| fetch/decode/rename/dispatch/issue/wb/commit width | 全部为 8 | 全部为 8，实际参与调度 | 一致 |
| fetch buffer / queue | 64 B / 32 entries | 64 B / 32 entries | 一致 |
| ROB/IQ/LQ/SQ | 192/64/32/32 | 192/64/32/32；正式报告达到这些最大 occupancy 并产生 stall | 一致且已激活 |
| load/store ports | 200/200 | 200/200 | 一致 |
| forward delays | fetch→decode 1，decode→rename 1，rename→IEW 2，issue→execute 1 | 1/1/2；issue→execute 折叠进 FU producer-ready latency | 条件一致 |
| backward/free-entry delays | IEW→rename 1，commit→rename 1 | 正式配置均为 0 | 不一致 |
| retirement visibility | `iewToCommitDelay=1` 等 timing-buffer edge | compact ordered-retire calendar | 部分一致 |
| trap/syscall timing | trap 13、fetch trap 1、syscall retry 10000 等 | 只有显式 system-UOP/drain/service/restart 抽象 | 部分一致 |
| physical registers | Int/Float/Vec 256，CC 1280 | 可派生 free entries 218/208/255/1275，但模型关闭 | 不一致 |
| wrong-path allocation | 进入 fetch/decode/rename/ROB/IQ/LSQ/FU 后 squash | 正式路径不分配 | 不一致 |

FastSim FU pool 与 gem5 目标的可见几何和时延一致：

| FU pool | units | 关键 op latency / pipelining |
|---|---:|---|
| IntALU | 6 | IntAlu 1，pipelined |
| IntMult/Div | 2 | multiply 3 pipelined；divide 1 non-pipelined |
| FloatSimple | 4 | add/cmp/cvt/bf16 2，pipelined |
| FloatComplex | 2 | multiply 4、FMA 5、misc 3；divide 12、sqrt 24 non-pipelined |
| SIMD | 4 | 1 |
| Predicate | 1 | 1 |
| combined memory | 4 | 1 |
| System | 1 | 1 |

这里把 gem5 `issueToExecuteDelay` 折叠到 producer-ready latency 是允许的机制
差异；不允许的是 free-entry release、squash 和在途 occupancy 的状态可见时机
发生变化。后者当前没有等价。

### 3.3 Rename、branch、memory dependence 和原子操作

| 组件 | 已对齐部分 | 未对齐部分 | 判定 |
|---|---|---|---|
| rename geometry | 物理寄存器总数可映射 | `core.rename_free_list=false`；没有有限 free-list stall 和 wrong-path allocation | 不一致 |
| Tournament geometry | local history/counters 2048，global/choice 8192，2-bit | — | 一致 |
| BTB/RAS/indirect | BTB 4096×1/tag16，RAS16，indirect 256×2/tag16/path3/GHR13 | — | 一致 |
| predictor update | committed branch outcome 可重放 | gem5 多条在途分支、speculative histories、squash/commit update/repair 不存在 | 不一致 |
| branch recovery | 2-cycle minimum redirect lower bound | dynamic resolve depth、wrong-path occupancy、refetch/I-side state不存在 | 部分一致 |
| IQ memory lifetime | load/atomic 可延长至 response | gem5 的 replay/squash request 不在 committed trace | 部分一致 |
| StoreSet | committed dependency 和 TSO 顺序 | SSIT/LFST、violation、reschedule、replay 缺失 | 不一致 |
| LL/SC/atomic | trace atomic 被串行化并进入 compact coherence | gem5 Ruby LL/SC lock 和 16-cycle timeout reservation state 不存在 | 部分一致，缺少 activation counter |

给 gem5 backward edge 数值后直接启用并不等于语义修复。此前完整 92-case gate
中，只启用 `iew_to_rename=1`、`commit_to_rename=1` 会在现有 occupancy 抽象上重复
收费：

| 组 | baseline mean/P99 | backward-edge mean/P99 |
|---|---:|---:|
| C4 | 2.830% / 6.256% | 4.529% / 9.427% |
| C8 | 1.649% / 5.461% | 3.535% / 11.963% |

所以正式值保持 0 是一个经过 accuracy gate 的补偿性抽象，不是 gem5 状态机
等价。正确修复需要重定义 release/dispatch-visible lifetime，不能只填参数。

### 3.4 I-side 和地址翻译

| 组件 | gem5 | FastSim | 判定 |
|---|---|---|---|
| L0 I-cache | 32 KiB、8-way、64 B、LRU，timed | 几何值存在，但 `l1i_enabled=false` | 不一致 |
| committed fetch-block response | Ruby L0-I request/response | 64 B fetch block + 1-cycle refill lower-bound ledger | 条件一致 |
| wrong-path/refetch I-side | active | 输入无请求流，正式模型不生成 | 不一致 |
| ITLB | 64-entry fully-associative LRU + timing walker | 不建模 | 不一致 |
| DTLB geometry | 64-entry fully-associative LRU | 64-entry fully-associative LRU | 一致 |
| DTLB hit | target lookup lower bound | 0-cycle lookup | 条件一致 |
| DTLB miss | per-level x86 walker，经 Ruby/cache/network/DRAM | fixed 12-cycle service，1 walker，无 coalescing | 部分一致 |
| page-table traffic | 进入 cache/coherence/memory state | physical PTE addresses 不在 trace，不生成 | 不一致 |

基础 `configs/gem5-v28_1-time-epoch.cfg` 保留 `dtlb.miss_model=se_atomic`，但两个
受维护 FS overlay 都显式覆盖为 `timing_walk/12`；正式有效报告也确认该覆盖已经
应用。所以当前正式矩阵不存在“FS 路径意外走 SE ATOMIC”的部署问题。

仍有两个风险：

1. 直接以基础 cfg 启动而不带 FS overlay 时，会回到 `se_atomic`；
2. 固定 12-cycle walk 是有效模型，不是 gem5 walker 状态机等价实现。

### 3.5 Cache geometry、replacement 和 indexing

gem5 与 FastSim 使用的层级名称不同，正确映射如下：

| gem5 target | FastSim 名称 | 几何 | 判定 |
|---|---|---|---|
| L0-D | L1D | 32 KiB、8-way、64 B、LRU | 一致 |
| private L1 | L2 | 1 MiB/core、8-way、64 B、TreePLRU | 一致 |
| shared L2/L3 | LLC | 8 slices × 8 MiB、16-way、64 B、TreePLRU | 一致 |

其他直接状态：

| 组件 | gem5 | FastSim | 判定 |
|---|---|---|---|
| private indexing | `start_index_bit=6` | line/set low-bit mapping | 一致 |
| shared slice/local set | slice bits 0..2，local set bits 3..15 | `cha=line&7` + global set 分解 | 语义一致 |
| replacement | L0 LRU；private/shared TreePLRU | 对应 LRU/TreePLRU bit tree | 一致 |
| private inclusion | private hierarchy inclusive | private hierarchy inclusion | 一致 |
| LLC inclusion | non-inclusive | `cache.llc.inclusive=false` | 一致 |
| Sequencer capacity | 16/core | 16/core response calendar | 容量一致 |
| controller TBE | 256/controller | private/LLC miss lanes 256；LLC per CHA | 容量一致，lifetime 部分一致 |
| prefetch | `enable_prefetch=false` | 不生成 prefetch request | 停用语义一致 |
| cache array resource stall | `resourceStalls=false` | 无独立 array-bank contention | 停用语义一致 |

几何和 replacement 一致不代表 cache state stream 一致。gem5 cache 还接收：

- wrong-path loads、speculative translation 和 replayed memory requests；
- L0-I、refetch 和 ITLB/page-walker 请求；
- kernel 请求；
- Ruby retry/replay/transient message 引发的访问。

FastSim 正式输入只有 committed data 加少量明确的 page-fault cache-state seed。
所以“相同 committed data access 的 tag/replacement 转移”可以一致，measurement
起点和完整运行期 cache state 仍不一致。

### 3.6 Ruby coherence 和 controller

FastSim 已建模：

- owner/sharer directory；
- remote supply；
- ownership upgrade 和 invalidation；
- private victim/directory removal；
- same-line compact transient merge；
- per-core Sequencer outstanding calendar；
- per-controller/CHA TBE-like capacity；
- memory response 后的 LLC fill visibility。

gem5 `MESI_Three_Level` 还包含 FastSim 未等价实现的：

- 完整 stable/transient states；
- request/response/unblock virtual networks；
- message buffer enqueue/dequeue；
- invalidation ack 数量与等待；
- retry/recycle；
- controller `transitions_per_cycle`；
- 完整 atomic/LLSC lock state；
- I-side/walker/kernel/wrong-path coherence traffic。

因此 Ruby 只能判定为“几何和部分功能转移一致”，不能判定为 controller
state-machine 等价。

### 3.7 Interconnect

gem5 正式网络实际是 `SimpleNetwork`，不是 Garnet：

| 维度 | C4 | C8 |
|---|---:|---:|
| external endpoints/routers | 26 | 34 |
| internal links | 650 | 1,122 |
| virtual networks | 3 | 3 |
| link latency | 1 | 1 |
| routing latency | 1 | 1 |
| link bandwidth factor | 16 B/cycle | 16 B/cycle |
| data/control message size | 64 B / 8 B | 64 B / 8 B |

无竞争 endpoint-to-endpoint path 可压缩为：

```text
external link 1
+ source routing 1
+ internal link 1
+ destination routing 1
+ external link 1
= 5 cycles one-way
```

FastSim `uncore.noc_one_way_latency=5` 与这个 lower bound 相同。该折叠在无竞争
时可接受，但 FastSim 没有：

- per-link serialization；
- per-vnet queue；
- source/destination router pressure；
- request/response/unblock 相互作用；
- endpoint 数量变化所产生的 link pressure。

正式 20 个 case 的 gem5 stats 中，所有 case 都有非零 network bandwidth
saturation。最明显的 Graph500：

| case | hottest-link `total_bw_sat_cy` | link utilization | 占测量 CPU cycles 的约值 |
|---|---:|---:|---:|
| C4 Graph500 | 1,016,387 | 10.09% | 8.59% |
| C8 Graph500 | 1,239,406 | 11.78% | 10.21% |

所以 NoC lower-bound latency 一致，但实际网络状态机不一致。配置和文档中把它
称为 Garnet 是确定的 provenance 错误，应修正为 SimpleNetwork。

### 3.8 DRAM geometry 和地址映射

| 维度 | gem5 | FastSim | 判定 |
|---|---|---|---|
| total size | 3 GiB | 正式 runtime 3 GiB | 一致 |
| channels | 8 | 8 | 一致 |
| interleave | 64 B，channel bits 6..8 | cache-line `line & 7` | 一致 |
| ranks/channel | 2 | 2 | 一致 |
| banks/rank | 16 | 配置名为 `banks_per_channel=16`，实现按 rank 乘开 | 语义一致，命名误导 |
| bank groups/rank | 4 | 4，`bank % 4` | 一致 |
| row size | 8 KiB | 8 KiB | 一致 |
| address mapping | `RoRaBaCoCh` | remove channel→column→bank→rank→row | 一致 |

`dram.banks_per_channel` 的名称不准确，但 `src/simulator.cpp` 的 decode 会为每个
rank 建立 16 banks，即 32 banks/channel，因此这不是当前地址映射错误。字段应在
后续兼容迁移中改名为 `banks_per_rank`，避免未来 profile 作者按字面填入 32。

### 3.9 MemCtrl、FR-FCFS、page policy 和 DDR command state

直接数值对齐部分：

| 参数 | gem5 | FastSim |
|---|---:|---:|
| read buffer | 64 | 64 |
| write buffer | 128 | 128 |
| write high/low threshold | 85% / 50% | 85% / 50% |
| min reads/writes per turn | 16 / 16 | 16 / 16 |
| max accesses per row | 16 | 16 |
| static frontend/backend latency | 10 ns / 10 ns | 30 / 30 target-core cycles |
| tCL/tRCD/tRP | 14.16 ns | 43/43/43 cycles |
| burst | 3.332 ns | 10 cycles |

但是正式 C4/C8 的 demand FR-FCFS repair 实际全部 bypass。有效 selection window
由以下 topology-scaled 规则得到：

```text
producer_lanes = cores * ranks_per_channel / channels
effective_window = min(configured_window,
                       max(1, producer_lanes - 1))
```

C4 和 C8 都得到 1。单 entry window 不能重排，代码保留 canonical service order
并退出 repair。20 个正式 case 的 `dram_frfcfs_effective_selection_window` 都为 1，
`candidate_epochs=0`；例如：

- C4 Stockfish bypass 9,942 个 demand requests；
- C4 LBM bypass 696,911 个；
- C8 LBM bypass 1,206,769 个。

所以 `dram.scheduler=frfcfs` 字符串不能被解释为 demand traffic 已执行 gem5
FR-FCFS。FastSim 的 separate dirty-write queue 可以执行独立 write drain，但 read
arrival/admission/selection 仍不是 gem5 controller queue。

gem5 目标是 `open_adaptive` page policy；FastSim 的 full-queue page-policy 和
single-precharge 修复开关正式均关闭。以下 DDR4 状态也未完整实现：

- read/write direction-specific bus turnaround；
- tCWL、tRCD_WR、tWR、tWTR、tWTR_L、tRTW；
- refresh 的 tREFI/tRFC 和 rank refresh occupancy；
- command window；
- power-state/refresh 相互作用；
- 完整 ACT/PRE/RD/WR command calendar。

因此 MemCtrl/DRAM 的结论是：几何和部分 fixed latency 一致，controller
arrival/order、page policy、command/bus/refresh state 不一致。

### 3.10 inactive system features

| 功能 | gem5 状态 | FastSim 省略的影响 | 判定 |
|---|---|---|---|
| DVFS | disabled | 无频率切换模型 | 停用语义一致 |
| DRAM powerdown | disabled | 无 powerdown timing | 停用语义一致 |
| cache prefetch | disabled | 无 prefetch request | 停用语义一致 |
| cache array resource stalls | disabled | 无 array bank/port stall | 停用语义一致 |
| DRAM refresh | active | FastSim 无 refresh occupancy | 不一致 |

不能因为 DRAM powerdown 关闭，就把 refresh 也视为不影响 CPI；正式 stats 中
rank refresh residency 非零。

### 3.11 Warmup、measurement initial state 和 kernel scope

正式 trace 有 functional warmup；FastSim replay warmup 后保留自身 cache/TLB/
predictor 状态，并在 measurement boundary 重置测量计数。这解决的是
**committed-visible state 的冷启动**，不能恢复：

- wrong-path predictor、I-cache、ITLB、D-cache state；
- page-table walker/cache traffic；
- gem5 ROI 起点已有的 ROB/IQ/LSQ/FU in-flight state；
- Ruby transient messages/TBEs；
- SimpleNetwork queues；
- MemCtrl read/write queue、open rows、refresh position；
- kernel 在 checkpoint/ROI 前形成的状态。

所以这里只能判定为“有 warmup，但 measurement initial state 部分一致”。延长
committed warmup 不能生成缺失事件类别，这不是单纯 warmup 长度问题。

user 与 user+kernel profile 的基础硬件配置相同，但 user+kernel 使用 synthetic
syscall/page-fault/IRQ event profile，而不是让 gem5 kernel instruction stream 在
同一 BaseO3+Ruby 状态机上执行。因此：

- user scope 可讨论 committed-user observable contract；
- user+kernel scope 是校准过的系统事件模型，不能声明为 gem5 kernel
  microarchitecture semantic baseline。

## 4. 当前直接参数差异

现有 direct audit 明确报告的 8 个不一致项如下。表中的 cycle 值是当前 audit
使用的整数换算值；严格 tick-domain 差异见 3.1 节。

| 参数 | gem5 | FastSim 正式值 | 是否已实现候选 |
|---|---:|---:|---|
| `dram.activation_limit` | 4 | 0 | 是，关闭 |
| `dram.t_ras` | 96 | 0 | 是，关闭 |
| `dram.t_rtp` | 23 | 0 | 是，关闭 |
| `dram.t_rrd` | 11 | 0 | 是，关闭 |
| `dram.t_rrd_l` | 15 | 0 | 是，关闭 |
| `dram.t_xaw` | 64 | 0 | 是，关闭 |
| `dram.t_ccd_l` | 16 | 0 | 是，关闭 |
| `dram.t_cs` | 5 | 0 | 是，关闭 |

这 8 个值是 direct audit 能看到的差异，不是全部语义差异。ITLB、I-side
activation、FR-FCFS bypass、network bandwidth、refresh、StoreSet 等缺口没有
包含在“8”中。

不能仅为了让 direct gate 变绿而打开这些参数。现有 command candidate 作用于
未认证的 reconstructed controller arrival order，数值相同仍可能产生错误的
状态转换顺序。

## 5. 运行时证据：缺口不是静态文档问题

### 5.1 Branch speculative state 和 wrong-path occupancy

在相同正式窗口中：

| case | FastSim branch miss | gem5 commit branch mispredicts | 差异 |
|---|---:|---:|---:|
| C4 Stockfish | 17,798 | 22,229 | -4,431 / -19.9% |
| C4 NAMD | 39,973 | 71,929 | -31,956 / -44.4% |
| C8 Stockfish | 26,306 | 32,113 | -5,807 / -18.1% |
| C8 NAMD | 69,832 | 99,216 | -29,384 / -29.6% |

几何相同但计数显著不同，说明问题位于 speculative predictor state、ROI warm
state 和错误路径更新语义，而不是 predictor table 大小。

另一个 gem5 反事实只把 Tournament 改成 TAGE-SC-L 64 KiB：branch miss 减少
14,194 次，squashed UOP 从 1,323,020 降到 693,874，UOP CPI 从 1.65535 降到
1.55874，约 17.1 cycles/miss。这是 wrong-path occupancy 对 CPI 的因果证据，
不能用固定 2-cycle redirect penalty 完整代替。

### 5.2 I-side 和 translation exposure

正式 frontend ledger 中：

| case | gem5 I-cache stall/uop | FastSim→gem5 CPI gap |
|---|---:|---:|
| C4 Stockfish | 0.0551 | +0.072126 |
| C4 NAMD | 0.1051 | +0.040948 |
| C8 Stockfish | 0.0703 | +0.060132 |

这些 gem5 stall counters 彼此重叠，不能直接相加成 CPI 归因；它们证明 timed
I-side 是活跃组件。committed-PC L1I candidate 对 Stockfish gap 只覆盖
1.18%/1.59%，并使 Graph500 C8 回归 0.66 pp，说明缺失的是完整 request stream
和 overlap，不只是打开一个 32 KiB tag array。

DTLB 也存在同样边界。例如正式 C4：

| case | gem5 DTLB miss | FastSim DTLB miss |
|---|---:|---:|
| Stockfish | 16,970 | 3,721 |
| omnetpp | 250,207 | 109,075 |
| NAMD | 51,694 | 1,494 |
| Graph500 | 1,681,909 | 1,218,889 |

这里同时含有 wrong-path/kernel/request-scope 差异，不能把 miss count 差额直接
乘固定 penalty；它证明固定 12-cycle committed-only DTLB 不是等价 walker。

### 5.3 SimpleNetwork contention

Graph500 的 hottest-link saturation 已在 3.7 节给出。其他正式 case 也有明显
暴露，例如 C4 TeaLeaf 约 5.95%、C8 TeaLeaf 约 5.61%、C8 LBM 约 3.69% 的
测量 cycles 落在 hottest-link bandwidth saturation 中。固定 5-cycle NoC 只能
覆盖无竞争下界，不能忽略这些 active states。

### 5.4 MemCtrl/DRAM state

C8 LBM 的 8 个 DRAM interfaces 聚合后有：

- 2,553,900 read bursts；
- 1,129,088 write bursts；
- 32.61% combined row-hit rate；
- 55.29 target-core-cycle average queue latency；
- 91.72 target-core-cycle average memory access latency；
- 140,668 次 read/write direction turnarounds；
- 约 4.49% rank refresh residency。

这里用 DRAM-interface counters 计算 burst、row-hit 和 latency，只对
`board.memory.mem_ctrl*.dram.*` 聚合，避免同时加上 MemCtrl 的镜像 burst counters
而重复计数；tick latency 按 333 ps target-core period 转换。单 channel 0 的
MemCtrl 也有 319,341 read bursts、141,238 write bursts、平均 write queue length
39.96，以及 8,793 次 read→write 和 8,793 次 write→read 切换。FastSim 的
demand FR-FCFS 在该 case 却因 effective window 1 全部 bypass。这证明 controller
差异会真实进入 LBM、TeaLeaf、Graph500 的 CPI，不是未触发的配置字段。

### 5.5 已实现候选不能冒充语义修复

当前正式 C4/C8 user mean APE 为 8.508%/8.008%。既有候选实验给出：

- TreePLRU 是直接 state-machine 修正，已经默认开启；
- committed-PC L1I 改善 pooled mean，但没有修复 Stockfish tail；
- partial DRAM command calendar 在旧同口径矩阵改善 user mean 约 0.366 pp，
  仍不能解决 P90/max，且没有完整 controller semantics；
- four-ACT 子集对当前正式矩阵只改变约 +0.021/+0.008 pp mean APE，说明这 8
  个 direct mismatch 不是当前 pooled CPI 的唯一主因；
- C8 unscaled FR-FCFS window=8 的五个 pilot 全部回归，mean APE 增加
  1.806 pp，LBM 从 2.727% 变成 7.746%；
- committed rename free list 在正式 destination-class trace 上产生零 stall，
  不能恢复 wrong-path allocations；
- anonymous branch shadow 在跨微架构 gate 中严重回归，已拒绝默认开启。

结论是：不能把“参数来自 gem5”当作默认开启的充分条件。还必须证明 arrival、
state lifetime 和输出语义相同。

## 6. 配置应用和 metadata 问题

### 6.1 基础 profile 不是自包含的正式目标

`configs/gem5-v28_1-time-epoch.cfg` 当前写着：

```text
sim.cores = 4
dram.size = 4GiB
```

正式 runner 会把 C8 覆盖为 8 cores，并把 DRAM 覆盖为 3 GiB，所以当前正式
报告是正确的。但是只拿 cfg 文件不能重现正式 target identity。这带来两类风险：

- 直接调用 FastSim 时静默使用 4 GiB；
- 审计基础文件而不是 effective report 时得出错误结论。

应建立单一 effective target manifest，或生成 C4/C8 锁定 profile。baseline
identity gate 必须读取最终有效配置并保存 provenance。

### 6.2 FS DTLB overlay 已应用，但基础路径仍可误用

FS overlay 正确覆盖 `timing_walk/12`。这说明之前的 DTLB 修复已经应用到当前
正式矩阵，不是“修改存在但未生效”。风险在于基础 cfg 仍是 `se_atomic`，其他
脚本如果绕过两个 FS 入口仍可能误用。门禁应要求 FS scope 必须显式解析为
`timing_walk`。

### 6.3 `uarch_profile.json` 与实际 gem5 config 不一致

正式 case 的 `tao_trace/uarch_profile.json` 中：

| 字段 | sidecar | 实际 `config.ini` |
|---|---|---|
| private L2 replacement | `lru` | `TreePLRURP` |
| shared L3 replacement | `lru` | `TreePLRURP` |
| DRAM queue window | 256 | read 64 / write 128 |

这个 sidecar 不改变已经生成的 gem5 CPI，但会污染依赖 sidecar 的 TaoTrace/FST
path classification 和后续身份判断。

`tools/validate_fs_oracle_identity.py` 当前只检查 core/frequency、cache
size/associativity、protocol、DRAM size/channel/interleave，没有检查：

- replacement policy；
- read/write queue；
- page policy；
- timing values；
- network type/vnets/bandwidth；
- component activation；
- runtime bypass。

sidecar 应直接由 `config.ini` 生成，并在转换前做 fail-closed identity gate。

### 6.4 network provenance 名称错误

FastSim profile 注释和 `audit_fs_profile_identity.py` 的 semantic limitations 把
目标网络称为 Garnet，实际 `config.ini` 是 `SimpleNetwork`。固定 5-cycle lower
bound 数值碰巧相同不能掩盖 provenance 错误。该名称必须修正，后续网络模型也
必须按 SimpleNetwork throttle/message-buffer 语义设计。

## 7. 可接受的 baseline 声明

在修复完成前，正式报告应使用以下合同：

### 7.1 可以声明

- gem5 是唯一 CPI oracle；
- C4/C8 workload 内 gem5 target config 稳定；
- FastSim 正式 effective config 对齐 core count、width、ROB/IQ/LQ/SQ、FU、
  cache geometry/replacement/indexing、CHA 数量、DRAM capacity/channel/rank/bank/
  row/address map；
- FastSim 对 committed functional data stream 的上述显式状态进行确定性重放；
- 不使用 workload ID、gem5 online timing/path/PMU oracle 或逐 case CPI 常数。

### 7.2 不可以声明

- FastSim 与 gem5 BaseO3CPU cycle/state-machine 等价；
- `121/129` 表示 93.8% 微架构已对齐；
- L1I、rename free list、FR-FCFS 因为有配置字段就已生效；
- 固定 12-cycle DTLB 等价于 gem5 timing walker；
- 固定 5-cycle NoC 等价于 SimpleNetwork；
- 当前 compact DRAM 等价于 gem5 MemCtrl + DDR4；
- user+kernel profile 等价执行 gem5 kernel instruction stream；
- 仅延长 committed warmup 可以恢复完整 ROI microarchitectural state。

### 7.3 正式报告建议命名

建议在结果表和文档中统一使用：

```text
gem5-derived C4/C8 committed-functional FastSim baseline
```

并在同一位置列出 exclusions：

```text
wrong path, full I-side/ITLB, timed page-table traffic,
full Ruby/SimpleNetwork transient queues, full MemCtrl/DDR4 state,
exact ROI in-flight microarchitectural snapshot
```

## 8. 修复优先级

### P0：先修 baseline 身份和审计门禁

1. 修复 PMU oracle completeness，建立 perf/gem5/FastSim event dictionary，并要求
   committed memory UOP、cache-line request 和 scope accounting 分别守恒。
2. 增加可由 FST 宏指令边界严格计算的 user perf-like macro CPI，把当前
   common-user-UOP 指标显式标为 user-work-normalized metric。gem5 保存精确的
   user/user+kernel perf-like CPI；FastSim combined CPI 在没有 kernel instruction
   stream 时只能是 profile-derived proxy。
3. 生成单一 effective target manifest；3 GiB/core count/FS DTLB 不再依赖隐藏
   command-line override。
4. 修正 Garnet→SimpleNetwork 的注释和审计文本。
5. 从 `config.ini` 自动生成 `uarch_profile.json`，修正 TreePLRU 和 queue 字段。
6. 扩展 identity gate，至少覆盖：
   - active/inactive state；
   - all pipeline forward/backward delays；
   - physical register model activation；
   - L1I/ITLB/DTLB walker；
   - cache controller latency/TBE/transition bandwidth；
   - network type/topology/vnets/link bandwidth；
   - MemCtrl queue/page policy/refresh/complete timing；
   - atomic/LLSC；
   - runtime effective window 和 bypass counters。
7. 把门禁拆成四个独立结果：geometry、state machine、input observability、
   runtime activation，禁止汇总成一个误导性的 match ratio。

### P1：现有 functional facts 可以继续实现的状态

1. 建立 `request create → controller enqueue → select → command/bus → response`
   可守恒 ledger，先认证 arrival/order，再打开 DRAM command constraints。
2. 实现完整 read/write direction、open-adaptive page policy、refresh 和必要 DDR4
   command calendar。
3. 实现 SimpleNetwork per-link/per-vnet bandwidth calendar 和 controller
   `transitions_per_cycle`。
4. 使用 gem5 tick 或有理数时间，消除整数-cycle 硬编码换算。
5. 补充 atomic/LLSC activation counters，判断正式 workload 的影响上界。

### P2：committed functional trace 不能严格恢复的状态

以下组件必须扩展 producer contract，或者明确归入不可观测残差：

- wrong-path ROB/IQ/LSQ/FU/cache/TLB occupancy；
- speculative branch history 和 squash repair；
- StoreSet violation/replay；
- 完整 fetch/refetch/L1I/ITLB request stream；
- page-table walker physical requests；
- ROI 起点的 in-flight core/Ruby/network/DRAM snapshot。

若终极输入仍限定为 committed functional trace，则不能以匿名统一 shadow、固定
penalty 或 workload-calibrated scalar 宣称等价；只能建立可审计的统计近似并明确
confidence boundary。

## 9. 复现命令和验证状态

Direct identity audit：

```bash
python3 tools/audit_fs_profile_identity.py \
  --gem5-config \
  tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7/cases/04c-706.stockfish_r/config.ini \
  --fastsim-report \
  tmp/committed-ledger-repair-20260817/formal-default-profiles/cases/04c-706.stockfish_r/user.json \
  --output tmp/uarch-semantic-audit-20260818/c4 \
  --report-only

python3 tools/audit_fs_profile_identity.py \
  --gem5-config \
  tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7/cases/08c-706.stockfish_r/config.ini \
  --fastsim-report \
  tmp/committed-ledger-repair-20260817/formal-default-profiles/cases/08c-706.stockfish_r/user.json \
  --output tmp/uarch-semantic-audit-20260818/c8 \
  --report-only
```

本次审计期间执行的默认代码测试：

```text
./build/fastsim_tests
all FastSim tests passed
```

该结果只证明 FastSim 内部单元/回归测试通过，不证明跨模拟器语义等价。

## 10. 证据索引

| 内容 | 路径 |
|---|---|
| FastSim 正式基础配置 | `configs/gem5-v28_1-time-epoch.cfg` |
| FS user overlay | `configs/gem5-v28_1-fs-user.cfg` |
| FS user+kernel overlay | `configs/gem5-v28_1-fs-user-plus-kernel.cfg` |
| direct identity audit 工具 | `tools/audit_fs_profile_identity.py` |
| sidecar identity validator | `tools/validate_fs_oracle_identity.py` |
| FastSim DRAM decode/FR-FCFS bypass | `src/simulator.cpp` |
| FastSim committed O3 lifetime | `src/interval_core.cpp` |
| FastSim predictor state | `src/predictor.cpp` |
| 当前 FS CPI 总方案 | `docs/fs-cpi-current-status-and-plan-2026-08-17.md` |
| 候选模型消融 | `docs/fs-cpi-candidate-model-audit-2026-08-17.md` |
| 参数/API 覆盖 | `docs/gem5-parameter-coverage.md` |
| wrong-path/backward-edge 证据 | `docs/uarch-generalization-debug-log.md` |
| trace 可观测性合同 | `docs/gem5-trace-contract.md` |
| FST/drmemtrace portability | `docs/fst-v7-drmemtrace-conversion-contract.md` |
| C4/C8 frontend ledger | `tmp/committed-ledger-repair-20260817/frontend-ledger/{c4,c8}/summary.md` |
| 正式 effective reports | `tmp/committed-ledger-repair-20260817/formal-default-profiles/cases/` |

本文是 2026-08-18 时点的语义对齐审计。后续每个组件修复后应更新本报告中的
判定和运行时证据，不能只更新配置字段或 direct match count。
