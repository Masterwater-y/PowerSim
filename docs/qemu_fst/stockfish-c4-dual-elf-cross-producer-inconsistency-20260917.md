# Stockfish C4 Dual-ELF 跨 Producer 不一致性分析

日期：2026-09-17

> 状态：历史不一致性证据。本文记录的 UOP 截断合同已由唯一的
> `first-core-macro-common-end` 采集合同替代；它保留用于说明为何不能再按每核
> `min(UOP)` 前缀比较。

## 1. 结论

本次 Stockfish C4/1M 对比已经证明两条 producer 均能生成可被 FastSim
消费的 FST v7，但当前结果**不能证明两条 producer 的动态指令流等价**。

`common-v7-distribution-fail` 的首要原因不是 ELF 地址偏移，而是两侧尚未使用
同一个多核动态窗口：

- gem5 TaoTrace 使用 `first-core-target-common-end-v1`。任一核心首先达到
  1M measured CPL3 UOP 后，四核在同一个 gem5 tick 关闭。
- QEMU-FST 当前仍按每核独立达到 1M measured CPL3 UOP 后分别截断。
- 比较器随后按每核较小的用户 UOP 数截取共同长度，但“相同长度”不等于
  “相同全局时刻”。Stockfish 的线程调度和任务分配会使这些前缀对应不同动态工作。

第二个主因是内核与 idle 过滤不一致：

- gem5 测量段只有 `20,862` 个内核 UOP。
- QEMU 测量段有 `4,688,356` 个内核 UOP，且四核
  `excluded_idle_kernel_uops` 均为零。

因此，完整 `user-plus-kernel` replay 的周期、分支和内存统计目前主要反映窗口与
内核人口差异，不能直接归因为 FastSim 时序模型或 QEMU lowering 错误。

在排除内核记录、只比较相同数量的 `3,196,160` 个用户 UOP 后，两侧
UOP/宏比仅相差约 `0.095%`，说明总体 x86 macro-to-UOP 展开接近；但 branch、
memory 和 dependency 分布仍分别相差约 `12.3%`、`3.7%` 和 `11.6%`。
这些剩余差异可能同时包含 Dual-ELF 动态路径差异和真实 lowering 差异。必须先统一
多核窗口与 idle 过滤，才能继续归因。

## 2. 分析对象

### 2.1 代码与 producer 身份

| 项目 | 身份 |
|---|---|
| minesim checkout | `df47fb150fff1e75174facea52e94bf08f273529` |
| `origin/FastSim` | `cd87bef1c76b3b9f258863c27c7f43ec03e12a92` |
| 当前开发分支 | `feature/fastsim/dr-fst` |
| QEMU | `67be5c847f983deb16aa0cb7538544745a47cd81` |
| qemu_tracer | `bcd8c779274dc2e5c32a70c244b5a2f6ec6eff0c` |
| QEMU 离线 lowerer 的 gem5 | `c8222cc67a399bfc01e8658dd14b30d5bfd634f9` |
| TaoTrace gem5 二进制 | SHA-256 `473ef0cb4792208f9a3ff60633142e4291da6816a693dcecc9aa50837b9c1471` |

TaoTrace 使用冻结的 first-core common-end gem5 二进制：

```text
/data00/yinhaolang/FastSim/tmp/first-core-common-end-20260911/gem5.opt
```

该文件和 `/data00/yinhaolang` 下的 checkpoint、磁盘、资源仅被只读使用。
新产物全部写入当前用户的 `minesim/var/`。

### 2.2 两个 ELF

本次明确采用 Dual-ELF 合同：

| 项目 | QEMU ELF | gem5 ELF |
|---|---|---|
| SHA-256 | `9c38fca8312dced3486a665667308343a71703773e38e6bed3850cdaa77d6c71` | `29b4eb30f90b171fa10f7e6e9fec5f6dd895064702e8a062728f1d414fcfba68` |
| 文件大小 | 19,557,640 B | 19,570,696 B |
| ELF entry | `0x408600` | `0x408580` |
| `.text` 大小 | 1,835,620 B | 1,835,812 B |
| `.rodata` 起始 | `0x5c2000` | `0x5c3000` |

两个文件都是静态链接 x86-64 ELF，但不是仅有统一常量偏移的同一二进制。
gem5 ELF 还包含 m5ops marker 相关状态。因此：

- 跨 producer 不比较 PC、branch target、虚拟地址或 ELF 内绝对符号地址。
- 允许地址偏移不等于允许任意动态行为差异。
- macro/UOP、OpClass、访存类型和依赖等仍可作为统计分布诊断，但不能在当前条件下
  要求逐桶整数完全相等。

### 2.3 产物路径

gem5 TaoTrace reference：

```text
var/qemu_fst/runs/production/gem5/c04/706.stockfish_r/
```

QEMU-FST candidate：

```text
var/qemu_fst/runs/production/qemu/c04/706.stockfish_r/fst/
```

主要证据：

```text
var/qemu_fst/runs/production/gem5/c04/706.stockfish_r/collection.json
var/qemu_fst/runs/production/gem5/c04/706.stockfish_r/trace.json
var/qemu_fst/runs/production/qemu/c04/706.stockfish_r/cross-producer-feature-report.json
var/qemu_fst/runs/production/gem5/c04/706.stockfish_r/fastsim-replay-comparison.json
var/qemu_fst/runs/production/gem5/c04/706.stockfish_r/replay/stats.json
var/qemu_fst/runs/production/qemu/c04/706.stockfish_r/replay-diagnostic/stats.json
```

## 3. 两侧采集合同

### 3.1 gem5 TaoTrace

配置：

- 4 个 O3 guest CPU。
- `user-plus-kernel` FST。
- 用户 UOP 目标为 1,000,000。
- source warmup。
- 完整 `.deps`、`.imap`、`.asmap` 和 `.vmap` companion。
- `first-core-target-common-end-v1`。

core 3 首先达到目标，公共结束 tick 为：

```text
22465384491969
```

各核测量人口：

| Core | 用户 UOP | 用户宏指令 | 内核 UOP | 总测量 UOP |
|---:|---:|---:|---:|---:|
| 0 | 882,389 | 428,859 | 0 | 882,389 |
| 1 | 624,011 | 305,382 | 0 | 624,011 |
| 2 | 689,759 | 334,234 | 20,862 | 710,621 |
| 3 | 1,000,001 | 484,071 | 0 | 1,000,001 |
| 合计 | 3,196,160 | 1,552,546 | 20,862 | 3,217,022 |

慢核低于 1M 是该合同的预期行为。TaoTrace 不填充记录，也不等待每个核心分别达到
目标。

### 3.2 QEMU-FST

当前 QEMU lowerer 仍采用 `qemu-per-core-user-uop-target`：

| Core | 用户 UOP | 用户宏指令 | 内核 UOP | 总测量 UOP | 目标 overshoot |
|---:|---:|---:|---:|---:|---:|
| 0 | 1,000,001 | 485,588 | 1,219,281 | 2,219,282 | 1 |
| 1 | 1,000,001 | 485,593 | 1,161,312 | 2,161,313 | 1 |
| 2 | 1,001,486 | 485,581 | 1,166,865 | 2,168,351 | 1,486 |
| 3 | 1,000,001 | 484,766 | 1,140,898 | 2,140,899 | 1 |
| 合计 | 4,001,489 | 1,941,528 | 4,688,356 | 8,689,845 | 1,489 |

这里的每个核心均独立扫描到自己的 1M 用户 UOP 目标。它与 gem5 的“首核触发、所有
核心共同关闭”不是同一个多核 ROI。

## 4. 当前比较器实际比较了什么

历史 `tools/fst_producer_gate.py` 曾对每个 core 取：

```text
min(gem5 measurement_user_records, qemu measurement_user_records)
```

因此本次共同用户 UOP 前缀为：

| Core | 比较 UOP |
|---:|---:|
| 0 | 882,389 |
| 1 | 624,011 |
| 2 | 689,759 |
| 3 | 1,000,001 |
| 合计 | 3,196,160 |

比较器在这个人口上聚合以下直方图：

- 每宏 UOP 数；
- OpClass；
- source/destination operand 形状；
- load/store/atomic 类型与访问大小；
- branch 类型与 taken 状态；
- serialize；
- syscall 编号；
- producer distance 与 producer class。

它不比较 PC 或具体地址，因此本次失败不是由 ELF 地址偏移直接触发。

但是，比较器只统一了每核 UOP 数量，没有证明 QEMU 前缀与 gem5 前缀来自同一全局
调度时刻、同一线程映射或同一任务分配。因此当前 `integer-exact` 是诊断强条件，
不是有效的生产 acceptance 条件。

## 5. 用户 UOP 前缀差异

### 5.1 聚合结果

| 指标 | gem5 TaoTrace | QEMU-FST | QEMU - gem5 | 相对差 |
|---|---:|---:|---:|---:|
| 用户 UOP | 3,196,160 | 3,196,160 | 0 | 0% |
| 完整用户宏 | 1,552,514 | 1,551,048 | -1,466 | -0.094% |
| UOP/宏 | 2.058700 | 2.060645 | +0.001946 | +0.095% |
| branch | 144,076 | 126,417 | -17,659 | -12.257% |
| memory UOP | 473,641 | 455,948 | -17,693 | -3.736% |
| dependency edges | 5,535,526 | 4,896,125 | -639,401 | -11.551% |
| 被排除的跨域依赖 | 165,689 | 347,085 | +181,396 | +109.480% |

syscall 直方图两侧均为空并通过，但这只说明选取的用户前缀没有完整 syscall marker，
不能证明整个 FS 窗口的 syscall 行为一致。

### 5.2 每核归一化结果

| Core | Producer | UOP/宏 | branch/1K UOP | memory/1K UOP | dependency/UOP | 排除跨域依赖 |
|---:|---|---:|---:|---:|---:|---:|
| 0 | gem5 | 2.057570 | 42.805 | 142.578 | 1.749159 | 39,557 |
| 0 | QEMU | 2.059414 | 41.722 | 147.971 | 1.513878 | 96,432 |
| 1 | gem5 | 2.043459 | 47.656 | 155.704 | 1.723551 | 32,882 |
| 1 | QEMU | 2.055447 | 35.379 | 130.693 | 1.575030 | 63,567 |
| 2 | gem5 | 2.063732 | 43.566 | 143.592 | 1.739013 | 33,054 |
| 2 | QEMU | 2.063392 | 37.958 | 139.624 | 1.543607 | 75,405 |
| 3 | gem5 | 2.065840 | 46.517 | 151.627 | 1.717070 | 60,196 |
| 3 | QEMU | 2.063096 | 41.343 | 147.519 | 1.512741 | 111,681 |

观察：

1. 四核 UOP/宏都很接近，最大的相对偏差出现在 core 1，仍只有约 `0.59%` 的
   完整宏数量差。
2. branch 差异不是单核异常。四个核心的 QEMU branch 密度都更低，core 1
   差异最大。
3. memory 差异较小且方向不完全一致：core 0 的 QEMU 更高，其余三个核心更低。
   这更像动态路径与任务分配混合影响，而不是一个全局 memory flag 映射反转。
4. dependency/UOP 在四个核心上都更低，方向稳定；同时 QEMU 被排除的跨域依赖
   显著更多。这是当前最值得在窗口对齐后继续调查的 lowering 差异。

## 6. 原因分层

### 6.1 已证实：多核窗口合同不一致

置信度：高。

gem5 的共同结束点属于一个全局事件；QEMU 的结束点属于四个独立 per-core
目标。即使 gate 截取相同 UOP 数，core 0/1/2 的 QEMU 前缀也不是 gem5
公共结束时刻下对应的全局执行状态。

Stockfish 会在 worker 线程之间动态分配搜索任务。调度时序变化会改变：

- 每核执行到的函数和基本块；
- branch 密度；
- table/NNUE 数据访问；
- 同步和等待路径；
- 寄存器生产者距离；
- 各核完成的宏指令数。

这足以解释当前多数整数直方图不完全相等。

### 6.2 已证实：内核与 idle 人口不一致

置信度：高。

QEMU projector 仅在 raw stream 出现 `VcpuIdle/VcpuResume` 状态后，把 CPL0
macro 标记为 idle 并过滤。本次四核 `excluded_idle_kernel_uops=0`，说明该
机制没有排除任何测量段内核 UOP。

gem5 TaoTrace 则依据 O3 指令和 CPL 周期分类识别 idle/poll 路径。本次只有 core 2
保留了 20,862 个内核 UOP，其余核心测量段没有保留内核 UOP。

因此两侧完整 replay 的总 UOP 人口相差约 2.70 倍。完整 replay PMU 的大幅差异首先
是输入人口问题，而非缓存或分支预测模型问题。

### 6.3 已证实：Dual ELF 不只是地址偏移

置信度：高。

两个 ELF 的 hash、大小、入口和 section 尺寸均不同。gem5 ELF 还包含不同的 marker
支持代码。虽然核心 Stockfish 源码与参数相同，但静态链接结果和 marker 实现不同，
因此允许：

- 地址整体或局部偏移；
- marker 邻近指令不同；
- 链接器布局导致的直接分支布局变化；
- 静态库或 IFUNC 选择差异；
- 不同执行环境下的线程调度差异。

Dual ELF 合同只能说明地址不应直接比较，不能支持直方图整数完全相等。

### 6.4 强证据：dependency 仍有 producer 语义差异

置信度：中高，但需在窗口对齐后确认。

gem5 TaoTrace 从在线 O3 已退休 DynInst 生成寄存器生产者关系。QEMU 路径则：

1. 从 QEMU 已退休 macro 和 architecture snapshot 组装 transaction；
2. 使用 gem5 v25.1.0.1 decoder/microcode 离线投影 UOP；
3. 由 `QemuDependencyTracker` 重建 producer distance；
4. 对 warmup 边界或 ASID 域之外的 producer 排除依赖。

QEMU 的 dependency/UOP 比 gem5 低 `11.55%`，被排除的跨域依赖多
`181,396` 条。差异在四个核心方向一致，不像单纯线程调度噪声。

可能机制包括：

- QEMU per-core 窗口导致更多 producer 落在选取前缀之外；
- QEMU ASID transition 与 gem5 在线 thread/context 归属不同；
- 离线 architectural snapshot 无法完全复制 O3 DynInst 的隐式寄存器依赖；
- syscall/event transaction 清空或切换 dependency tracker；
- producer class 编码或寄存器展平规则仍有边界差异。

现阶段不能判定是哪一项，也不能据此修改 dependency 语义。应先统一窗口，再对
同一 macro 序列逐宏检查。

### 6.5 可能存在：branch 分类或动态路径差异

置信度：中。

QEMU 的 branch 密度比 gem5 低 `12.26%`，且四核方向一致。可能来源：

- Dual ELF 或线程调度导致真实控制流不同；
- QEMU successor/outcome 事实与 gem5 retired branch 标志组合不同；
- syscall/event gateway 替换改变宏边界；
- 部分 gem5 microcode control UOP 被 TaoTrace 计作 branch，而离线 projector
  没有同样分类。

在没有同一动态 macro 序列映射前，不能把该差异直接定性为 branch lowering bug。

### 6.6 较弱证据：memory 分类差异

置信度：中低。

memory UOP 总差为 `-3.74%`，但 core 0 的 QEMU 反而高于 gem5。该模式不支持
“所有 QEMU memory UOP 都被系统性漏标”的简单结论，更可能是：

- 多线程任务分配导致各核工作不同；
- Dual ELF 生成了不同的访存指令组合；
- 少量复杂指令或 atomic/RMW 的动态拆分不同；
- QEMU observation 与 gem5 ExecContext 消费粒度不同。

## 7. FastSim replay 差异为何暂不能归因

使用同一 `configs/gem5-fs-native-kernel.cfg` 得到：

| 指标 | gem5 TaoTrace | QEMU-FST | QEMU - gem5 | 相对差 |
|---|---:|---:|---:|---:|
| cycles/user UOP | 0.355426 | 0.737216 | +0.381790 | +107.42% |
| cycles/macro instruction | 0.726796 | 0.685839 | -0.040957 | -5.64% |
| retired UOP | 3,217,022 | 8,689,845 | +5,472,823 | +170.12% |
| retired instruction | 1,563,024 | 4,301,247 | +2,738,223 | +175.19% |
| memory UOP | 477,941 | 1,599,449 | +1,121,508 | +234.65% |
| branch | 145,981 | 533,501 | +387,520 | +265.46% |
| branch miss | 2,984 | 8,954 | +5,970 | +200.07% |
| L1D miss | 13,322 | 24,991 | +11,669 | +87.59% |
| L2 miss | 3,531 | 6,452 | +2,921 | +82.72% |
| LLC miss | 2,069 | 1,238 | -831 | -40.16% |

必须同时保留两个 CPI：

- UOP CPI 绝对误差：`0.381790 cycles/UOP`。
- 宏 CPI 绝对误差：`0.040957 cycles/macro instruction`。

二者方向相反，是因为两侧完整输入的人口和宏/UOP比例并不相同。当前数据不能用于
声称 QEMU-FST 使 FastSim “慢 107%”或“快 5.6%”；它只说明两个未对齐输入经过
同一模型后产生了这些结果。

## 8. 当前 gate 的适用边界

### 8.1 可作为硬门的内容

以下项目不依赖 ELF 地址一致，当前仍可严格检查：

- FST magic、version、header 和 record size；
- feature bit；
- 四核 dense identity；
- `.deps` 完整性；
- `.asmap` 与 source record count 一致；
- `.imap`、`.vmap` 的结构完整性；
- CPL3/CPL0 人口守恒；
- warmup + measurement = total；
- producer 内部 macro boundary 和 dependency distance 合法；
- FastSim 能完整消费 manifest。

### 8.2 当前只能作为诊断的内容

在 Dual ELF 且窗口未同步时，以下内容不应要求整数完全相等：

- macro UOP-count histogram；
- OpClass histogram；
- operand shape；
- memory access histogram；
- branch histogram；
- dependency distance histogram；
- FastSim CPI 与 cache/branch PMU。

当前 `common-v7-distribution-fail` 应解释为“发现待分析差异”，不能作为
QEMU-FST 生产链整体失败的结论。

## 9. 修复与验证顺序

### P0：统一多核关闭语义

QEMU raw capture 或 lowering 必须形成一份全局共同结束证据：

- 某一 vCPU 首先达到 1M measured CPL3 UOP；
- 固定一个全局事件序号或可比较的 capture epoch；
- 所有核心在该边界关闭；
- 非触发核保留实际 UOP 数；
- 不填充、不等待各核独立达到 1M；
- 边界 metadata 与 gem5 使用同一
  `first-core-target-common-end-v1` 语义。

只在 converter 中对每核独立截取数量不等价于共同结束。

### P1：修复 QEMU idle 观测

应先确认 raw stream 是否实际包含 `VcpuIdle/VcpuResume`：

- 若 raw 中没有 marker，修复 QEMU/plugin 的 vCPU idle 事件产生；
- 若 raw 中有 marker，检查 assembler 的每核状态传播；
- 重新采集后要求至少能解释为什么 `excluded_idle_kernel_uops` 为零；
- 与 TaoTrace 的 idle/poll 分类做事件级对照，而不是按最终总数猜测。

在此之前，不比较完整 `user-plus-kernel` PMU。

### P2：重新运行用户前缀分布门

窗口与 idle 对齐后，先看：

1. UOP/宏；
2. branch/UOP；
3. memory/UOP；
4. dependency/UOP；
5. 每核而非仅 aggregate 的差异。

若 UOP/宏仍稳定在约 `0.1%`、但 dependency 仍约 `10%`，再进入 dependency
专项。

### P3：dependency 逐宏定位

需要建立 producer-local macro identity，不要求 PC 相同：

- 按 ROI 内宏序号、指令 bytes、线程/core 身份关联；
- 先选择没有调度分叉的短连续区间；
- 比较每个 macro 的 micro-op 数、src/dst class 和 dependency producer；
- 单独记录因 warmup、ASID 或 event 边界被排除的边；
- 第一处差异必须能回溯到具体 macro bytes 和 gem5 microcode。

如果两边宏 bytes 已不同，应归类为动态路径差异，不进入 lowering 修复。

### P4：最后比较 FastSim CPI/PMU

只有满足以下条件后才重新解释 CPI：

- 同一 C4 common-end 策略；
- 相同 measured CPL3 UOP 人口；
- 可解释且相近的 kernel/idle 人口；
- macro/UOP 和主要分类分布达到约定容差；
- 两边均由同一 FastSim 配置消费。

## 10. 当前正式判断

| 问题 | 判断 |
|---|---|
| TaoTrace C4/1M 编排是否成功 | 是 |
| TaoTrace FST 是否可被 FastSim 消费 | 是 |
| 两侧 FST v7/companion 基础合同是否成立 | 是 |
| 当前差异是否由 PC 偏移直接导致 | 否 |
| 当前多核窗口是否一致 | 否 |
| 当前 kernel/idle 人口是否一致 | 否 |
| 当前 exact-distribution fail 是否可作为生产阻断 | 否，应作为诊断 |
| 是否已有 lowering 差异迹象 | 是，dependency 最强，branch 次之 |
| 是否足以确认 dependency lowering bug | 否，需先完成 P0/P1 |

最终结论是：**当前结果已经建立了可用的 TaoTrace reference 和清晰的不一致面，
但尚未建立可归因的跨 producer 等价实验。下一轮工作不应先改 OpClass 或依赖算法，
而应先统一 QEMU 的多核 common-end 和 idle 过滤合同。**
