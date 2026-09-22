# QEMU-FST v11 上一版历史证据

> 本文只保留 QEMU-FST v11 主线在当前 `mainline.md` 之前的上一次历史状态，
> 用于对照。现行合同、计划与验收状态见 [mainline.md](mainline.md)。
> 2026-09-20 至 2026-09-22 的 guest Barrier/work-phase 路径已判定失败，
> 完整原因和废弃范围见
> [Barrier 跨仿真器进度对齐失败复盘](barrier-cross-simulator-alignment-retrospective.md)。

## 1. `v11-stockfish-r3`（早期 raw，lowering 失败）

生成过旧 raw shard，`.measurement-started` 与 `.capture-complete` 均存在。
四核 lowering 全部失败：

- core 0/1/2/3：静态 cursor 在不同 PC 耗尽 QEMU actual access；
- 后续局部修补暴露 `Cda`、SSE 子范围、split、MSR 与 syscall 等
  "宏访问数不等于 gem5 UOP 访问数"的结构性冲突。

该结果证明"callback 顺序消费与 mapping 反向绑定"不适合作为转换主线；它不证明
QEMU capture 失败。旧 raw 缺完整前态且含已删除 mapping，不能用 compatibility
path 重用。仅作为诊断，不构成验收，也不会自动提升到
`var/qemu_fst/runs/production/`。

## 2. `v11-accessfact-stockfish-c4-1m-r12`（重构前完整闭环）

完成全新 QEMU capture → 四个独立 gem5 lower worker → FST v7 → FastSim
native-kernel replay：

- 每核 QEMU measurement CPL3 macro envelope 均为 `1,000,000`；
- 每核 lowered CPL3 UOP 为 `1,000,001` / `1,000,001` / `1,000,001` /
  `1,000,000`（前三核完整末宏 `+1` overshoot）；
- 混合 privilege measurement 合计 `9,056,157` retired UOP，其中
  `4,000,003` 为 CPL3，`5,056,154` 为 kernel；
- `unknown_addresses`、`branches_without_outcome`、`mmio_escape_accesses` 与
  `page_fault_untracked_accesses` 均为零；
- strict physical address、virtual-page token、record retirement、功能 DTLB
  与 timing DTLB 守恒均通过。

原产物位于
`var/qemu_fst/runs/v11-accessfact-stockfish-c4-1m-r12/qemu/c04/706.stockfish_r`
（现已随诊断目录一并清理）。`r11` raw 仅用于定位 prefetch transaction 结束与
micro-fp scratch 边界，不是发布或验收结果。

### 2.1 转换数据与效果

producer 与 consumer 计数域不同，不能混为同一个"1M trace"：

| 阶段 | 真实数据 | 含义 |
|---|---:|---|
| QEMU capture | 4 × `1,000,000` CPL3 macros | 每核 producer measurement envelope |
| raw transport | 487.8 MB gzip | `bytes + Full/Delta pre-state + AccessFact[]` 四个 shard |
| FST measurement | 9,056,157 mixed-CPL UOP | replay 实际消费的 measurement 流 |
| 其中 CPL3 | 4,000,003 UOP | 每核 1M target 与完整宏 overshoot 之和 |
| 其中 kernel | 5,056,154 UOP | 不消耗 CPL3 target 的 native-kernel population |
| FST 与 companions | 4.16 GB | 四个 hot stream、`.deps/.vmap/.asmap/.imap/.io` 与 manifest |
| source warmup | 55,586,502 UOP | ROI begin 到 measurement 的真实功能前缀 |

每个 measurement core 含 `876–931` 个 MMIO UOP，PIO 为零。该结果使用 `.io`
扩展，但 raw 不含 frame-RAM capability，因此不能作为当前主线发布结果。r12
replay 的 functional warmup 为 `8.264 s`，measurement 为 `1.248 s`；这是该机、
该产物的消费时间，不代表 QEMU capture 或 gem5 lowering 的通用性能。

该结果证明的是转换功能闭环：QEMU 动态事实可被 gem5 官方 x86 decode/microcode
投影为可消费且严格地址化的 FST v7。不证明与 TaoTrace/gem5 的 CPI、cache 或
DRAM timing 等价。

## 3. 已删除的旧设计要点

进入当前主线前必须删除、不保留兼容分支：

- `QemuTransactionMemory`；
- 跨 frame 的 byte-address memory state；
- 普通 RAM observation 顺序游标；
- RAM observation 耗尽校验；
- 缺失字节回退；
- `QemuLoweredMicroop`、`QemuLoweredTransaction`；
- 整笔 `std::vector` UOP 累积；
- `requiresDynamicEvaluation()`、静态/动态双 lowering、`generateLinear()`、
  `evaluateDynamic()`；
- `lowerRetiredTransfer()`；
- 每事务 `4096` UOP 语义上限；
- `.mseg` 及其 reader/writer/runtime 分支；
- converter 手写的 `fetchMicroop()/fetchRomMicroop()` 调度循环。

raw reader 可保留块读取、gzip、capability/header 校验与 Full+Delta 重建，但输出
对象必须只是 frame 与边界事件。I/O 有序事实仍需在 frame 结束时全部消费。
