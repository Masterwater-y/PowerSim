# QEMU-FST v11 主线（合同 + 计划 + 现状）

> 本文是 QEMU-FST v11 唯一现行合同、计划与验收状态。上一版历史证据见
> [history.md](history.md)。

## 1. 固定基线

```text
QEMU v11.0.0 MTTCG Full-System
  → qemu_tracer per-vCPU 已退休事务事实
  → gem5 v25.1.0.1 离线 ISA 功能投影
  → canonical FastSim FST v7 + companions
  → FastSim native-kernel replay
```

| 仓库 | 官方基线 | 当前集成 |
|---|---|---|
| FastSim | `origin/FastSim@3fdc307d` | 本仓源码 |
| gem5 | `v25.1.0.1@c8222cc6` | overlay `integrations/gem5/qemu_fst/` |
| QEMU | `v11.0.0@98b060da` | `/data00/xuhaoen/qemu` 工作树 |
| qemu_tracer | `origin/master@abd8dc2b` | `/data00/xuhaoen/qemu_tracer` 工作树 |

开发构建只要求 QEMU 与 qemu_tracer 的 HEAD 派生自上述官方基线，并允许未提交
工作树参与构建与验收。`patches/qemu_fst/` 已从官方基线刷新到本轮验收工作树，
但不作为开发态 ancestry gate。


FST v7 是唯一输出合同。QEMU 决定宏、branch、地址、成功访问、前态与事件事实；
gem5 决定 UOP、OpClass、src/dst、依赖与 Fault ROM 投影。TaoTrace 是同一 FST v7
的原生功能投影，不是本链路的逐条校准对象。

## 2. Producer 合同：QEMU + qemu_tracer

### 2.1 权威边界

QEMU 是以下动态事实的唯一权威：已退休宏顺序/PC/字节/长度/`successor`、branch
实际方向与目标、宏或事件入口 x86 架构状态、当前宏观察到的 RAM 字节及 VA→PA、
成功 MMIO/PIO 的地址/方向/宽度/load 返回值、interrupt/exception/hostcall/syscall
实际边界、CPL/CR3/ASID、idle/resume 与 ROI 边界。QEMU 不生成 gem5 UOP、
OpClass、依赖距离或 FST 字段。Full+Delta 只是宏入口架构状态的无损传输编码，
不是跨宏模拟状态。

### 2.2 官方 API 与只读 Observation

`include/plugins/qemu-plugin.h` 与 `v11.0.0@98b060da` 完全一致。官方 API 直接
负责：TB/指令 callback、成功 memory callback（含 VA、hwaddr、value）、
vCPU idle/resume、普通 discontinuity、plugin 生命周期。

私有 `include/plugins/qemu-fst-observation.h` 与单一
`qemu_fst_query_observation_api` vtable 只补充官方 API 无法表达且 consumer 实际
读取的只读事实：

- 一次性 x86-64 前态快照；
- structured event begin/end、vector、error code 与 disposition；
- direct-host helper 与 event delivery 中不经过普通 memory callback 的成功访问；
- 当前 frame 的 RAM snapshot 区域；
- QEMU 实际 PIO transfer；
- QEMU 译码得到的 REP/REPE/REPNE 类型与 address-size。

`read_x86_state()` 只允许在同一 vCPU 的 `R_REGS` callback 内调用；mapping、
event end、event object、PIO callback 使用 `NO_REGS`。接口只读，不提供写、
异常注入、事件过滤或影响 QEMU 控制流的能力。qemu_tracer 不再自行解析 x86 REP。

### 2.3 事务模型

raw 顶层只有两类转换单元。**宏事务**（下一指令边界或事件事实证明退休后原子发布）：

```text
MacroBegin
  State
  Instruction
  AccessFact*
MacroCommit(successor)
```

**事件事务**（`EventBegin` 位于 `x86_cpu_do_interrupt()` 取得恢复后 faulting PC
但 `do_interrupt_all()` 未改入口状态的时点；`EventEnd` 位于 delivery 完成后）：

```text
EventBegin
  State
  EventMetadata
  AccessFact*
EventEnd(handler_pc)
```

`unretired-fault` 丢弃未退休候选宏；`retired-transfer` 将宏与事件原子处理；
`between-instructions` 先闭合前一宏。nested event 和无法严格表达的部分 REP
失败。每个 vCPU shard 独立有序，不存在跨 vCPU 快照或全局 RAM 镜像。

### 2.4 本核前态

每个 shard 首次发布完整字段，后续只发布相对上一事务的 bitmap delta；lowerer
先还原完整事务前态，再开始 lowering。固定字段只覆盖动态 UOP 路径与 Fault ROM
所需的长模式执行上下文：16 GPR、XMM、x87 与 MXCSR；RFLAGS、CPL；
CR0/CR2/CR3/CR4/CR8/EFER；CS/SS/DS/ES/FS/GS/LDT/TR 的
selector/base/limit/attributes；GDT 与 IDT base/limit；
STAR/LSTAR/CSTAR/SFMASK、kernel GS base、SYSENTER 状态与 TSC_AUX。
`RDTSC/RDTSCP` 的实际返回值只在对应宏执行时作为独立 observation 传递。

## 3. Frame memory 与 I/O facts

每个 MacroFrame/EventFrame 只携带当前 gem5 功能执行实际需要的输入，不保存完整
RAM 或跨事务状态：

- 普通 RAM load callback 与 helper 补充 RAM 在 qemu_tracer 提交事务前按 VA/PA
  合并为按地址索引的不可变 sparse-memory；相同字节去重；同一 VA 对应不同 PA 或
  不同值立即失败。
- gem5 `readMem()` 先查询当前 frame store overlay，再查询 frame RAM；允许相邻
  16-byte 片段组成一个 read；缺少 UOP 所需字节或 PA 立即失败。
- gem5 `writeMem()` 的 EA/宽度/方向/数据由 UOP 执行产生；PA 必须来自 QEMU 逐字节
  映射证据；写入 frame 临时 overlay，宏结束销毁；不回写 QEMU。
- MMIO/PIO 保持按成功顺序排列的 AccessFact；load 保留实际返回值，store 不保留
  value；frame 结束要求 MMIO/PIO 全部消费；RAM 允许未消费字节。
- `helper_fxrstor` / `helper_fxsave` 在 `access_prepare()` 成功后、修改/写入
  FPU 状态前发布完整 512-byte legacy area 作为 frame RAM；不携带 mnemonic 或
  字段表；不修改 gem5 官方微码，不补零，不按 callback 顺序重排。
- 跨页 UOP 保持 hot record 首 PA + 总宽度，无法提供单一 token 时使用既有
  tokenless cross-page 路径；不引入 `.mseg`。
- `PREFETCHW`/`PREFETCHT0` 等 advisory hint 保留官方 `isDataPrefetch()`
  UOP/OpClass，`readMem()` 对 `Request::PREFETCH` 直接返回 `NoFault`，不查询
  frame RAM，不生成 memory record，不按 mnemonic 建白名单。
- interrupt/exception 的 IDT/GDT/LDT/TSS 与目标栈检查输入由 QEMU event helper
  在成功 `access_prepare_mmu()` 后发布；`qemu_fst_observation_event_input()`
  任何失败必须立即终止 capture。

## 4. 离线 gem5 lowering

### 4.1 宿主对象

`X86QemuFstOfflineLowererHost` 继承 switched-out `BaseSimpleCPU`，仅复用官方
`SimpleThread`、x86 `Decoder`、`X86ISA`、`SimpleExecContext`、
`BaseSimpleCPU::preExecute()`、`StaticInst::execute()`、`advancePC()`、
`Fault::invoke()` 与 x86 Fault ROM。这是固定到 gem5 v25.1.0.1 的内部集成，不是
gem5 公共稳定离线 API。CPU 不激活、不 tick；inert ports 不发送 cache/TLB/MMIO
请求；不运行 O3/Atomic/Timing SimpleCPU；`m5.instantiate()` 只构造官方对象；
不调用 `m5.simulate()`；不推进 event queue 或 target tick。

### 4.2 MacroFrame 与统一功能步进

`MacroFrame` 是 wire framing POD：PC、长度、≤15-byte encoding、完整入口状态、
QEMU successor 与 branch outcome、CPL 与 ASID、有序成功 memory observations、
当前正式消费的 TSC/TSC_AUX、warmup/measurement 归属。禁止在 MacroFrame 内保存
gem5 micro-PC、UOP 列表、matcher 游标或跨宏寄存器状态。

所有宏与 Fault ROM 统一走：

```text
preExecute() → StaticInst::execute() → FST sink → advancePC()
```

不调用 `postExecute()/countInst()`、`checkForInterrupts()/checkPcEventQueue()`、
`tick()/schedule()/reschedule()`、`translateAtomic/Functional()`、cache/device
port、branch predictor、gem5 timing/stall/oracle。QEMU 的 successor、branch
outcome、PA 与 AccessFact 始终是最终动态权威；缺少路径必需输入立即失败，不回退
静态展开或读取相邻事务。

### 4.3 EventFrame 与 INT3 边界

interrupt/exception 由 event metadata 构造官方 x86 `Fault`，调用
`Fault::invoke()` 进入 Fault ROM，再使用与普通宏相同的功能步进。对于 `INT3`
等已退休软件 trap：普通 MacroFrame 执行其官方 macro micro-op；当下一 micro-PC
进入 Fault ROM 时，该跳转 UOP 作为当前宏末 UOP 投影并结束 MacroFrame；实际
Fault ROM 只由紧随其后的 EventFrame 执行一次，禁止 MacroFrame 内提前执行后再
由 EventFrame 重复执行。syscall gateway 折叠为 `op_class=-1` marker，只更新
既有 sparse syscall metadata。

### 4.4 Scalar-single load 宽度修复

gem5 v25.1.0.1 的部分 scalar-single memory macro 将 `m32fp` 实例化为
`Ldfp(dataSize=8)`，而 QEMU 正确发布 4-byte load。集成层从官方 Decoder 的
`MacroopBase::getEmulEnv().dataSize` 取架构宽度；当且仅当当前 micro-op 是
`Ldfp`、其内部宽度大于该宏架构宽度、且当前 frame 在同一 EA 精确提供该宽度的
RAM load 时，重新实例化同一个官方 `Ldfp` 为该架构宽度。不按 mnemonic 建
白名单，不按缺失后 raw 长度猜测，不补零或读取相邻 frame RAM，保留原
micro-op 的地址操作数/目标寄存器/flags 与推进关系，FST 记录实际 4-byte 访问。

### 4.5 未实现指令

`WarnUnimplemented` 保留官方 UOP 并输出官方 warning（gem5 v25.1.0.1 对
`fnclex` 仍如此，本项目不增加本地 ISA 覆盖）；`FailUnimplemented` 立即失败；
不增加 opcode/mnemonic 白名单，不重建或替换官方 UOP。

## 5. FST v7 投影

每个实际经过的 UOP 立即写一条 64-byte hot record。字段来源固定：

- `StaticInst`：OpClass、load/store/atomic、control、src/dst、serialize、macro
  boundary；
- QEMU frame：macro PC、actual successor/outcome、CPL、ASID、PA、syscall facts；
- sink 状态：record ordinal、last-writer dependencies、page token、ASID
  transition、syscall association。

privilege 编码：

```text
user OpClass N    → N
syscall gateway   → -1
kernel OpClass N  → -(N + 2)
```

跨页 memory UOP 仍只有一条 retiring hot record，并使用既有 tokenless
cross-page 路径。FST v7 feature bit 7 要求 `.fst.io` v1：domain 1 为 MMIO，
domain 2 为 PIO。MMIO hot record 保存真实 PA；PIO hot record 保存 16-bit
port，宽度仅 1/2/4，不设置 PA/token/atomic。`trace.allow_mmio_escape=true`
保留 core/LSQ 生命周期，使两类 I/O 绕过 DTLB、page-fault、cache、coherence
与 DRAM；不模拟设备服务。`.deps/.asmap/.vmap/.imap/.io` 由
`BinaryTraceSource` 作为唯一 reader。

依赖、destination class、privilege、syscall、`.deps/.vmap/.asmap/.imap` 的
编码算法对齐 `origin/FastSim@3fdc307` TaoTrace。不修改 FST record 或 feature。
不再接受 `.fst.mseg`、`MemorySegment`、feature bit 6、将 domain 编入 OpClass
/地址/timing 结果、为 TaoTrace/DynamoRIO/模型配置添加本轮不需要的兼容分支。

## 6. 多核与窗口

- QEMU capture budget 是每核完整 measurement user macro envelope；
- FST target 是每核 measurement user UOP 下界；达到下界后完成当前宏，
  overshoot 只能来自该完整宏；
- 每个 shard 使用独立 lower worker，不共享架构状态；
- 全部 worker、FST writer 与 reader 审计成功后 `acceptance.json` 标记 `passed`；
- idle 只由 QEMU idle/resume 生命周期事实决定。

## 7. 性能不变量

以下合同不因性能优化改变：

- FastSim functional warmup 仍消费 ROI begin 到 measurement begin 的完整前缀；
- 当前唯一 transactional raw 结构、gzip transport 与默认 level 保持；
- MacroFrame/EventFrame/State delta/AccessFact/ROI/footer 的逻辑 entry 顺序
  不变；
- gem5 lower 继续以 QEMU 动态事实与官方 x86 microcode 为唯一功能输入；
- 缺失、冲突或未消费的必需事实继续立即失败。

优化不得通过删除 warmup、改写 ROI、缩短 producer envelope、按 mnemonic/workload
过滤事实、或制造第二套 raw/FST 格式达成。压缩字节相等不作要求；consumer 可见
raw 语义与最终 FST/replay 结果必须逐位一致。

性能诊断按真实阶段分离：

```text
launch → .measurement-started → .capture-complete → FST manifest → replay/stats.json
```

TeaLeaf-first 单 workload `--jobs 1` 采集：instruction/memory callback、`hwaddr`
与 load value 查询、x86 state capture、frame-RAM 收集/排序/合并与 transaction
buffer fallback、producer buffer backpressure 与 gzip writer、raw/warmup/
measurement/frame-RAM bytes。profile 只用于定位实现位置，不新增长期 metadata、
sidecar 或报告格式。

## 8. 重构轴（合同不变、四层同时）

### 8.1 采集层热路径（qemu_tracer）

- scoreboard 条件回调门控 measurement 采集：`g_native_roi_active` per-vCPU
  scoreboard 保持 `vcpu_insn_exec` 的 `COND_GE`，ROI 前不注册任何 `R_REGS`
  transaction callback。
- state capture 每宏一次：measurement 条件回调内进入指令边界时读取一次；
  删除 `system_emulation && enable_inline` 分支上与 native-kernel 无关的第二次
  `refresh_arch_state()`。
- `NativeRawWriter` 的 state delta bitmap 增量化：维护上一次已发布 state 的 128-bit dirty
  累积，仅对本宏实际写过的字段区间做比较；full-state 首发路径不变。
- `NativeRawWriter` 的 frame RAM 归一化保持 per-byte 展开 + `std::sort` 的 producer
  算法（VA 排序、PA/value 冲突失败），用预 reserve 的单一 buffer 承载
  frame_byte，避免每宏 `vector` 重新分配。

### 8.2 转换层数据结构

- `QemuFrameMemory` 的 `frameRam`/`ramMappings`：`std::map` → `absl::btree_map`
  （保留按地址有序语义）；`overlay`：`std::unordered_map` → `absl::flat_hash_map`。
- `reset()` 构建循环保持"冲突立即失败"语义。
- `QemuTransactionAssembler` 仅确保 `refs`/`encoding` 跨事务复用（已 `reserve`），
  不引入 abseil，不改 framing 与校验。
- abseil 以 `gem5_fastsim/ext/abseil` vendor 目标接入 `X86_QEMU_FST` target；
  producer 侧不链接。回退：STL 扁平化（排序 vector + `lower_bound`），禁止
  自定义容器或第二套格式。

### 8.3 多核驱动模型

- worker 单 shard 直读：`lower.py` 的 `_run_converter` 传 `--worker-core-index`
  与该 worker 唯一的 `--input-shard`；`convertWorker` 只打开/解压/解析被分派
  的 shard；`assembler.coreId()` 必须等于 `workerCoreIndex`，否则失败。
- 唯一 shard→core 映射 owner 是 producer 写入的
  `TRACE_MARKER_TYPE_CPU_ID`；`lower.py` 不猜测映射。staged shard 数与
  `num_cores` 不符或映射缺失立即失败，不回退全量扫描。
- 每 shard 仍独立有序、独立 gem5 worker、无共享架构状态；`acceptance.json`
  仍在全部 worker + writer + reader 审计成功后标记 `passed`。

### 8.4 FST 投影与依赖

- `QemuDependencyTracker::producerFacts` 用固定容量栈上小数组代替临时 vector；
  distance/class 去重在插入时完成；逻辑输出（按距离升序、每距离唯一、classes
  前 4 项）保持不变。
- `FstProjector::projectMicroop` 的 `segments` 复用 projector 级别可重用
  buffer；写入 `BinaryTraceWriter` 的 64-byte hot record 路径不变。
- `canonicalStaticRegister` 保持纯函数；FST v7 record 布局、privilege 编码、
  companion 与 `BinaryTraceSource` 唯一 reader 契约不变。

## 9. 当前实现与验收状态

### 9.1 源码状态

- `include/plugins/qemu-plugin.h` 与 v11.0.0 完全一致；私有接口只读；
  动态符号生成同时读取两份头，私有导出仅
  `qemu_fst_query_observation_api`。
- qemu_tracer 构建显式复制两份头，仅 `fst-native-kernel` 安装时查询私有 vtable。
- event begin 使用 `R_REGS`，event end 与 event memory 使用 `NO_REGS`。
- helper fault 不再在 `raise_interrupt2()` 提前 `cpu_restore_state()` 或发布
  event；统一在官方 unwind 返回 `x86_cpu_do_interrupt()` 后、delivery 前发布。
- 普通 RAM callback 与 helper 补充 RAM region 在宏提交前合并为一份 frame-local
  sparse-memory；相同字节去重、冲突立即失败。
- `helper_fxrstor`/`helper_fxsave` 在访问检查完成后发布完整 512-byte legacy
  FXSAVE area；不修改 QEMU 架构访存 footprint，不要求 converter 维护 FPU 布局
  表。
- MMIO/PIO 有序 AccessFact，frame 结束要求全部消费；gem5 官方 `isDataPrefetch()`
  UOP 保留 OpClass 但不消费功能 AccessFact。
- 所有宏与 Fault ROM 走统一功能步进；每个 UOP 立即写 FST；frame RAM 与普通
  store 的映射按字节合并，重叠同映射去重、冲突立即失败；load 优先当前 frame
  store overlay。
- QEMU `discons` 与 x86 softmmu Makefile 已恢复官方 v11 测试边界；cutoff-event、
  `native-event`、x87/FNCLEX 专用测试与构建产物已清理；无
  `QemuTransactionMemory`、`QemuLoweredTransaction`、动态分类与 4096 UOP 上限；
  无 `m5.simulate()`、CPU tick、MMU translation、cache/device port；普通 QEMU
  memory 只有官方 callback 一条路径。

本 checkout 的 FastSim、QEMU、qemu_tracer、gem5 EXTRAS 已完成重建，
`fastsim_tests` 通过。QEMU 最终动态符号表只新增 `qemu_fst_query_observation_api`。

QEMU 与 qemu_tracer 两仓当前 producer 差异已从各自官方 baseline 刷新为
`patches/qemu_fst/` 下同名唯一 patch；gem5 侧仍以
`integrations/gem5/qemu_fst/` 源码 overlay 提供。


唯一正式状态文件
`var/qemu_fst/runs/production/qemu/c04/706.stockfish_r/acceptance.json`
为 `status=passed, stage=complete`。

本轮实跑依次暴露并原位修复：

- gem5 内部 PIO 地址在右移前必须清除移位后的 `IntAddrPrefixMask`，否则
  `0x3f9/0x3fa` 会被误解码为 `0x7f`；
- `0f 0d /1 PREFETCHW` 为 advisory；保留官方 `isDataPrefetch()` UOP/OpClass，
  但 `readMem()` 不查询不存在的成功 RAM fact；
- `FXSAVE64` 的 QEMU store callback 不覆盖 gem5 官方微码写入的所有 legacy-area
  字节；`helper_fxsave` 在 `access_prepare()` 成功后发布完整 512-byte frame
  RAM，converter 以逐字节唯一映射处理 snapshot 与实际 store 的重叠。

| 项目 | 结果 |
|---|---:|
| QEMU measurement CPL3 macros | 每核 `1,000,000` |
| FST measurement UOP | `8,949,949` |
| 其中 CPL3 UOP | `4,000,002` |
| 其中 kernel UOP | `4,949,947` |
| measurement MMIO records | `3,532` |
| measurement PIO records | `118` |
| warmup PIO records | `795` |
| frame RAM bytes | `46,310,492` |
| source warmup UOP | `54,611,154` |
| raw transport | `474,716,588` bytes |
| FST 与 companions | `4,090,526,369` bytes |

`unknown_addresses=0`、`branches_without_outcome=0`、
`page_fault_untracked_accesses=0`，record retirement、privilege population、
I/O bypass、功能 DTLB 与 timing DTLB 守恒均通过。

### 9.3 十负载矩阵（2026-09-16）

外层 workload 并行度 5；每个 workload 内部使用 QEMU MTTCG 与四个独立 gem5 lower
worker。9 项 `passed/complete`，各项 measurement 人口以
`var/qemu_fst/runs/production/qemu/c04/<workload>/acceptance.json` 为准：

| workload | meas UOP | CPL3 UOP | MMIO | PIO | frame RAM (B) |
|---|---:|---:|---:|---:|---:|
| 706.stockfish_r | 8,949,949 | 4,000,002 | 3,532 | 118 | 46,310,492 |
| 710.omnetpp_r | 11,548,721 | 4,000,003 | 4,931 | 338 | 11,807,555 |
| 777.zstd_r | 34,466,007 | 4,000,000 | 13,627 | 780 | 92,230,025 |
| 782.lbm_r | 12,100,968 | 4,000,000 | 5,903 | 0 | 15,606,391 |
| 803.sph_exa_s | 10,062,184 | 4,000,005 | 4,274 | 23 | 8,803,746 |
| 816.nab_s | 9,350,540 | 4,000,003 | 3,742 | 0 | 242,204,450 |
| 854.graph500_s | 23,024,386 | 4,000,001 | 8,841 | 118 | 104,249,907 |
| 857.namd_s | 16,123,214 | 4,000,003 | 7,395 | 76 | 16,682,321 |
| 881.neutron_s | 9,281,342 | 4,000,002 | 3,810 | 0 | 34,494,031 |
| **合计** | **134,907,311** | **36,000,019** | **56,095** | **1,453** | **572,388,918** |

两项原转换失败已原位修复：

- `777.zstd_r`：raw encoding `0xcc` 的已退休 `INT3` MacroFrame 曾继续进入
  Fault ROM，与独立 EventFrame 重复执行；现以进入 ROM 作为 MacroFrame 边界，
  复用同一 raw 完成 lower/replay/validate。
- `857.namd_s`：固定 gem5 的 scalar-single `Ldfp(dataSize=8)` 与架构 `m32fp`
  不一致；按官方 Decoder `EmulEnv.dataSize=4` 实例化同一 `Ldfp`，不补零、
  不使用 mnemonic 白名单。其唯一 tokenless RAM 记录是可由地址与宽度证明的
  跨页访问，不再重复计入 `page_fault_untracked_accesses`。

剩余 1 项：

- `811.tealeaf_s`：`running/capture`，600 秒 warmup deadline 内未发布
  `.measurement-started`。归因数字与后续动作按 §7 诊断链路补齐。

## 10. 验收顺序与恢复规则

固定顺序：

1. 静态门禁：官方 `qemu-plugin.h` 未变、私有接口只读、lowerer 无 CPU
   `tick()`/端口请求/`m5.simulate()`；raw 只保留 frame RAM、实际 I/O 与
   event-input facts；无旧 mapping/matcher 状态；Observation ABI major 1
   声明 frame-RAM 与 PIO capability；无 `.mseg` 或 feature bit 6；bit 7 与
   `.fst.io` 必须成对。
2. 构建：abseil vendored 目标、gem5 `X86_QEMU_FST`、qemu_tracer dumper、
   QEMU 官方 plugin tests、FastSim/gem5 components、`fastsim_tests`。
3. 功能回归：全新 `706.stockfish_r` C4 每核 1M capture → 四核 lower → replay
   逐位一致于当前 production `acceptance.json`。
4. 性能门禁：`811.tealeaf_s` C4 每核 1M 在 600 秒内发布 `.measurement-started`
   并完成全部功能门禁。
5. 矩阵：全部十 workload C4 1M production matrix。

每项要求 raw/FST/replay 人口、ROI 边界、privilege、地址、MMIO/PIO、功能 DTLB
与 functional consumption 守恒；`unknown_addresses`、`branches_without_outcome`、
`page_fault_untracked_accesses` 均为零。运行中、失败与成功都只写入唯一正式目录
`var/qemu_fst/runs/production/qemu/c04/<workload>/`。

`acceptance.json` 是唯一状态权威：`running`/损坏禁止自动覆盖；`failed` 可由下一次
完整 capture → lower → replay 原位重建；`passed` 永久不可覆盖。若 `failed`
已保留 `.capture-complete`、`.measurement-started` 与四个完整 raw shard，转换
修复后的恢复复用同一 raw 目录，只在该 workload 目录内原位重建
`fst/`、`replay/` 与临时 `.fst.staging/`。恢复不重新 capture，不创建新
run-id、`diagnostics/`、`tmp-lowerdiag/`、`reused-raw/` 或 raw 副本；状态按
`failed → running/lower → running/replay → running/validate → passed` 原子更新。
只有 raw 完整性门禁失败，或既有失败已证明 producer 未发布 consumer 必需的动态
事实时，才允许删除该失败 workload 目录并执行一次完整 capture → lower → replay。

## 11. 失败边界（禁止事项）

失败：事务 framing/初始 Full/后续 Delta/footer 或 ROI envelope 不成立；
Observation 合同缺少必需能力；动态 UOP 路径缺少寄存器或实际 load value；
memory UOP 无唯一 mapping、方向不兼容或跨页分段不完整；event-object 不完整、
nested event 或部分 REP 无法严格表达；`FailUnimplemented` 或临时求值器不能终止；
FST 人口/macro boundary/privilege/地址/companion 或 replay 守恒失败。

禁止：补零、猜值、跨地址替代、静态展开回退、跨事务状态读取、store-value 对拍、
QEMU/gem5 内部 callback 粒度对拍；恢复 callback cursor、地址搜索或匹配求解器；
新增 raw/FST 派生版本号；mnemonic 白名单；固定 UOP 上限掩盖功能步进错误；
修改 `/data00` 下其他用户仓库；大规模验收前跳过 Stockfish C4 1M 基本门禁；
同时保留新旧两条转换入口；性能优化通过删除 warmup、改写 ROI、缩短 producer
envelope、按 mnemonic/workload 过滤或制造第二套 raw/FST 格式达成。

### 回退

- abseil 无法在任一构建树干净接入时，转换层容器回退到 STL 扁平化（排序 vector +
  `lower_bound`）。
- scoreboard 门控在某 workload 暴露官方 API 不足以提供必需事实时，保留该事实的
  当前获取路径并如实报告阻塞，不得用私有覆盖或补零掩盖。
- 单 shard 直读发现 producer 未写 `CPU_ID` 或映射不唯一，立即失败，不回退全量
  重扫。
- 任一层重构导致 §10 守恒失败时立即停止。
