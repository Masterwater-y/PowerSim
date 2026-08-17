# DynamoRIO Trace 转换体系分支报告

> 权威合同：`docs/gem5-trace-contract.md`。\
> 操作入口：`tools/drtrace/README.md`。\
> 当前报告只描述 DynamoRIO producer 和本分支验收状态，不替代全局
> FST/TraceRecord 合同。

## 0. 一页结论

1. **DynamoRIO path 已实现为离线 FST producer。** raw drmemtrace 不进入
   FastSim runtime；转换器用 gem5 x86 decoder/lowering 生成 canonical FST v6。
2. **gem5 reference 语义保持独立。** gem5 workload binary 仍使用 m5
   WORKBEGIN/WORKEND；DynamoRIO binary 使用 `record_function` marker。共享 ROI
   helper 归 `workloads/common/roi`，两个 ABI 分别输出到 `bin/gem5` 和
   `bin/dynamoRIO`。
3. **当前 strict physical 策略是 fail closed。** DynamoRIO capture 要求
   marker-backed PA；缺 PFN、masked PFN、映射冲突、跨地址空间或 cross-page
   memory record 都不会发布 strict FST。
4. **`uarch_first` 的 FST gate 覆盖 DIV evidence V2。** scalar `DIV/IDIV`
   由 DR 侧架构 evidence 驱动 gem5 原生 microcode replay；reference overlay
   不因该功能修改。
5. **V2 不接受缺失或旧 evidence。** decoded DIV/IDIV 必须逐 PID/TID/sequence
   消费证据并核对退休结果；faulted DIV 不发布为 FST。
6. **replay topology counters 仍是 diagnostic-only。** replay functional totals
   可用于一致性检查；cache/CHA/DRAM/timing 差异受 guest PA 与 host PA 命名空间
   影响，不进入当前 pass/fail gate。

## 1. 数据流和产物生命周期

当前分支保持两个 producer 的职责分离：

```text
gem5 records-only trace
        -> build/fastsim convert-gem5
        -> gem5 FST v6
        -> retain gem5 raw tao_trace JSONL until paired FST validation passes

DynamoRIO drmemtrace + PA markers
        + DIV/IDIV V2 architectural evidence
        -> gem5 x86 decode/lowering and native microcode replay
        -> DR FST v6 + address-provenance.json

paired gem5/DR FST manifests
        -> FastSim replay with producer-specific physical-memory configs
        -> exact functional-total comparison
        -> producer-local topology diagnostics
```

`tools.drtrace` 使用 `configs/workloads/uarch_first.json` 作为默认 matrix。
主要 artifact root 如下：

- raw capture root：`tmp/dr-traces/<matrix>/cXX/<workload>/{gem5,dr}`
- final FST root：`tmp/dr-fst/<matrix>/cXX/<workload>/{gem5,dr}`
- replay output：`tmp/dr-fst/<matrix>/cXX/<workload>/replay`
- replay execution report：`tmp/dr-fst/<matrix>/replay-report.json`
- replay comparison report：`tmp/dr-fst/<matrix>/replay-validation/report.json`

gem5 raw `tao_trace/*.records.micro.jsonl` 只是 FST 转换的中间输入。大负载使用
`accept-workload` 逐 workload 完成双 producer 转换和严格 FST 对照；仅当该 workload
验收通过后删除 raw `tao_trace` 目录，任一阶段失败均保留 raw trace 供定位。独立
`convert-gem5-fst` 不提前删除 raw。

## 2. 字段验收口径

当前 strict gate 分为 FST 结构和 `core_reconstructable` 字段。严格比较包括：

- FST header、record size、file size、manifest 顺序和 core ID。
- 控制流字段：`pc`、`target`、`next_pc`。
- 分支字段：branch/call/return/conditional/indirect flags、committed direction、
  `kTaken`、`kBranchOutcomeValid`。
- memory 形状：load/store/atomic classification、非零 `size`、
  `kPhysicalAddress`、`kVirtualPageToken`。
- UOP 和依赖：`op_class`、`n_src`、`n_dst`、`producer_dists`、
  `producer_classes`、destination class counts。
- syscall record 形状：`op_class == -1`、`address == syscall_number`、
  serializing semantics，且不携带 memory/physical-address flags。

两个字段只比较形状和可重建关系。地址来源在各 producer 内严格验证，不比较跨
producer 地址布局：

- `address`：gem5 FST 使用 guest PA，DR FST 使用 host PA；raw PA、cache-line
  placement 均不比较，DR side provenance 必须完整。
- `reserved`：低 31 位 virtual-page token 是 producer-local intern ID；当前要求
  token 存在和复用形状正确，不比较 raw token ID。

`address-provenance.json` 是 DR side 证据 sidecar，记录每个 logical core 接受的
PID 及 VA-page/PA-page/token 映射。FST v6 没有 ASID 字段，所以同一 output core
跨地址空间会被拒绝。

cache/CHA/DRAM/timing 的跨 producer 对比需要先定义共享地址投影和目标拓扑；在此
之前，它们只描述各自 producer 的地址现实，不进入转换验收 gate。

## 3. DIV/IDIV evidence 合同

stock drmemtrace 不记录 gem5 私有 microPC，也不提供执行 `DIV/IDIV` 动态微码路径
所需的寄存器值。DR wrapper 因此为每个线程写一条 V2 evidence stream，记录执行前
`RAX/RDX`、divisor、指令种类、操作数宽度和来源，以及退休后的 `RAX/RDX` 或 fault
结果。

同一次采集的所有 stream 共享 capture ID。`manifest.json` 只保存 schema/version、
capture ID、文件路径和 SHA-256；PID/TID、record count 与结构完整性由文件名、header
和文件大小推导。converter 必须逐 PID/TID/sequence 消费全部 evidence，用同版本
gem5 原生 microcode 执行动态路径，并核对退休结果。缺失、陈旧、混跑、未消费或结果
不一致的 evidence 一律 fail closed。

## 4. 当前验收边界

- FST gate 严格比较 structure、PC/控制流、branch outcome、memory shape、
  `op_class`、依赖与 destination class metadata。
- `address`、cache-line placement 和 raw virtual-page token 不跨 producer 比较；
  DR 侧必须独立通过 strict physical manifest 与 provenance 验证。
- FastSim replay 对 functional totals 做 exact check；cache/CHA/DRAM/DTLB/timing
  counters 只作 producer-local topology 诊断。
- 当前受控 ROI 刻意不包含 syscall、blocking、线程调度和同步行为；这些能力不是
  本转换 gate 的隐含要求。

正式结果保存在各 matrix 的 `tmp/dr-fst/<matrix>/validation` 与
`replay-validation`，不复制动态 workload 数量或计数到活跃文档。当前
`business_excitation` 已完成双 producer replay 和 functional-total 对照；其结果以
`tmp/dr-fst/business_excitation/replay-report.json` 与
`tmp/dr-fst/business_excitation/replay-validation/report.json` 为准。

## 5. 与历史第 18 章设计的目标匹配

`docs/gem5-source-aligned-p99-plan.md` 第 18 章是 2026-08-04 的设计快照。
它将 DR adapter 定义为离线 producer：native drmemtrace 先经过目标 ISA decode、
gem5-compatible UOP/OpClass lowering、dependency construction 和
address/branch/syscall/thread-event normalization，再写入 canonical FST，由同一个
FastSim timing engine 消费。当前分支已经实现这条主路径，但只应描述为：

> 面向受控 x86 ROI、通过双 producer functional differential gate 的
> DR-to-FST adapter。

它还不是不受限制的 `precision-supported` adapter。当前 precision 声明只覆盖
FST structure 和 `core_reconstructable` functional equivalence，不覆盖完整 OS
行为或跨 producer 物理拓扑等价。

### 5.1 逐项目标映射

| 第 18 章目标 | 当前实现 | 状态 |
|---|---|---|
| FastSim runtime 只接受 canonical functional IR | DR 离线生成 FST v6；runtime 不读取 drmemtrace，也不执行 ISA decode | 已完成 |
| x86 target-ISA decode | 转换 overlay 使用 gem5 x86 `Decoder`，没有维护平行的简化 decoder | 已完成 |
| gem5-compatible UOP/OpClass lowering | 使用 gem5 `StaticInst`、`fetchMicroop()` 和 `opClass()` | 已完成 |
| 禁止固定一条指令对应一个 UOP | macro-op 按 gem5 micro-op 展开；动态 `DIV/IDIV` 由 evidence 驱动原生 microcode replay | 已完成 |
| instruction/UOP/dependency differential gate | `validate` 按 FST record 对照 structure 和 `core_reconstructable` 字段 | 已完成，范围受当前 gate 限定 |
| dependency-distance construction | 每线程维护 last-writer state，并从 gem5 source/destination registers 生成 producer distance/class | 已完成 |
| branch taken/target/next-PC normalization | 使用 DR 控制流记录、实际后继和动态 DIV 内部路径生成完整 branch facts | 已完成 |
| 一条指令多个 memory operand | 将 DR data references 与 gem5 memory micro-ops 按形状和数据宽度绑定 | 已完成于当前验收负载 |
| virtual-page token | 在 producer 地址空间内建立 VA page 到 token 的稳定映射 | 已完成 |
| physical address | 强制 marker-backed PA，并用 `address-provenance.json` 验证映射来源 | 已完成 producer-local 严格来源 |
| syscall normalization | syscall marker 写为 `op_class == -1`，syscall number 内联到 `address` | 实现完成，真实 ROI 覆盖不足 |
| atomic/fence/serialize | serializing 语义来自 gem5 lowering；atomic memory UOP 当前 fail closed | 部分完成 |
| block/wake/thread lifecycle sidecar | 已验证 ROI thread/core ownership 和 marker 配对，尚无 block/wake event sidecar | 未完成 |
| 静态 decode/lowering cache | 当前 `decodeMacro()` 仍按动态指令重新调用 decoder | 未完成 |
| Arm/RISC-V adapter | 当前仅有 `X86DrTraceConverter` | 未开始，符合最后扩展顺序 |
| runtime direct-DR reader | 没有新增；runtime 继续只有 canonical FST 入口 | 符合目标 |

FST 从历史设计中的 v5 演进到 v6，不改变第 18 章的核心边界。FastSim 热路径仍消费
固定 64-byte `TraceRecord`，不携带 raw instruction bytes，不在 runtime 重做 decode
或 UOP lowering。v6 补充的是当前模型实际消费的 physical-address、virtual-page
token、destination register class counts 和 syscall encoding。

### 5.2 已完成的关键能力

当前 converter 从 drmemtrace 的 instruction bytes 构造 gem5 `StaticInst`，按 gem5
micro-op 边界输出 `op_class`、source/destination register facts、producer distance、
branch outcome 和 memory shape。per-thread last-writer state 包含 flags、partial
register、implicit operand 和 micro-op temporary dependencies 在 gem5 lowering 后呈现的
register 关系；adapter 不依赖 `DR_ISA_REGDEPS` 恢复这些语义。

`DIV/IDIV` 是第 18 章静态 lowering 描述之外的重要动态边界。gem5 的 division
microcode 具有数据相关的内部循环，stock drmemtrace 不记录 microPC，也不提供选择该
路径所需的寄存器状态。当前 V2 sidecar 捕获架构输入和退休结果，converter 使用 gem5
原生 microcode 与 converter-local `ExecContext` 选择动态路径，并核对结果。它没有
用单条 synthetic `IntDiv` 代替严格 gem5 UOP stream，也没有在 adapter 中复制 gem5
除法算法。

同程序双 producer gate 已覆盖 FST header/core/record 顺序、PC 和控制流、branch
分类及实际结果、memory classification/size、UOP/OpClass、依赖、producer metadata、
destination register class counts，以及存在 syscall record 时的 syscall number。
具体 workload 状态和动态记录数不写入本报告，以各 matrix 的 canonical validation
artifact 为准。

### 5.3 物理地址目标的收敛

第 18 章要求没有可信 `paddr` 时只能进入 exploratory mode。当前分支选择更严格的
实现：DR FST publication 要求真实 PA marker，并在 PFN 缺失或 masked、VA/PA offset
不一致、remap 冲突、cross-page reference 或同一 output core 跨地址空间时 fail
closed。这满足了每个 producer 内部的严格物理来源要求。

但 gem5 FST 的 guest PA 和 DR 原生执行的 host PA 属于不同命名空间。因此当前
differential gate 不比较 raw `address`、cache-line placement 或 raw virtual-page
token ID。取得真实但彼此独立的 PA，不等于两次执行具有相同 cache set、page color、
CHA home 或 DRAM mapping。当前 replay 只把 functional totals 作为 exact gate，
cache/CHA/DRAM/DTLB/timing counters 保持 diagnostic-only。

要满足第 18 章更强的跨源 PMU precision 目标，仍需先定义共享地址投影或共同物理布局，
再单独建立 topology acceptance layer。现有 producer-local provenance 不能被解释为
gem5/DR 物理拓扑等价。

### 5.4 当前验收不能外推的能力

当前矩阵使用受控 ROI，刻意避免把 OS 和同步行为混入 converter gate。现有通过结果
不能证明以下能力已经完整支持：

- atomic memory UOP；converter 当前明确拒绝该路径。
- fence/serialize 的专项 differential workload；字段可由 gem5 lowering 产生，但尚未
  建立独立覆盖。
- ROI 内 syscall arguments、return values、blocking 和 kernel duration。
- 按 `(thread_id, retired_ordinal)` 锚定的 block/wake/thread lifecycle event。
- oversubscription、migration、跨地址空间线程执行和 ASID-aware replay。
- 其他数据相关 x86 microcode path；没有专用 evidence 的 dynamic internal control
  继续 fail closed。

syscall 指令和 FST encoding 需要分层理解：converter 能验证真实 syscall gateway 和
DR syscall-number marker，并生成 canonical syscall record；但当前受控 ROI 没有用真实
blocking syscall 验收调度或内核行为。

### 5.5 剩余实施门槛

若继续按第 18 章的 `precision-supported` 目标推进，顺序应保持：

1. 为 atomic、fence 和 serialize 建立真实双 producer differential workload，并为
   atomic memory UOP 实现明确 ordering/scope lowering。
2. 为 syscall、block/wake 和 thread lifecycle 定义 retired-ordinal anchored event
   sidecar；不把这些事件塞入普通 memory 或 serializing record。
3. 缓存静态 decode/lowering 结果，避免同一静态编码按动态实例重复调用 gem5 decoder；
   此优化不得改变输出 FST。
4. 对仍具有数据相关 microcode control 的 x86 指令逐项审计；需要 runtime state 时采用
   与 DIV V2 同类的最小 evidence，无法证明时继续 fail closed。
5. 若需要 cache/CHA/DRAM/DTLB PMU 等价，先定义共享地址投影和 topology gate，再把
   当前 diagnostic counters 升为 acceptance contract。
6. x86 合同和验收稳定后再扩展 Arm、RISC-V；每个 ISA 都必须重新编译并采集对应 trace，
   不能把 x86 动态执行流直接解释为另一 ISA。

因此，当前分支与第 18 章的架构方向高度匹配：DR adapter 的 functional conversion
主链路已经从未来设计变成可验收实现。能力定位应保持两层：

- **DR 到 canonical FST functional adapter：已实现。** x86 decode、gem5 UOP
  lowering、dependency、branch、memory、producer-local strict PA、DIV dynamic
  microcode 和双源 functional gate 已落地。
- **通用 `precision-supported` adapter：尚未完成。** atomic/fence、OS event、
  decode cache、其他动态 microcode 和跨源物理拓扑 PMU gate 仍是显式边界。

## 6. 未决格式边界

- FST v6 单 memory record 只能携带一个 address 和一个 virtual-page token；
  cross-page memory 当前 fail closed。
- FST v6 没有 ASID；同一 logical core 跨 address space 当前 fail closed。
- `docs/gem5-source-aligned-p99-plan.md` 第 18 节保留 2026-08-04 的历史设计快照；
  当前 DR producer 合同以 `docs/gem5-trace-contract.md` 和本工具实现为准。
