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
        -> delete gem5 raw tao_trace JSONL after manifest validation

DynamoRIO drmemtrace + PA markers
        + DIV/IDIV V2 architectural evidence
        -> gem5 x86 decode/lowering and native microcode replay
        -> DR FST v6 + address-provenance.json
```

`tools.drtrace` 使用 `configs/workloads/uarch_first.json` 作为默认 matrix。
主要 artifact root 如下：

- raw capture root：`tmp/dr-traces/<matrix>/cXX/<workload>/{gem5,dr}`
- final FST root：`tmp/dr-fst/<matrix>/cXX/<workload>/{gem5,dr}`
- replay output：`tmp/dr-fst/<matrix>/cXX/<workload>/replay`

gem5 raw `tao_trace/*.records.micro.jsonl` 只是 `convert-gem5-fst` 的中间输入。
转换成功且 FST manifest 通过 strict validation 后会删除 raw `tao_trace` 目录；
失败时保留 raw trace 供定位。

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
`replay-validation`，不复制到活跃文档。

## 5. 未决格式边界

- FST v6 单 memory record 只能携带一个 address 和一个 virtual-page token；
  cross-page memory 当前 fail closed。
- FST v6 没有 ASID；同一 logical core 跨 address space 当前 fail closed。
- `docs/gem5-source-aligned-p99-plan.md` 第 18 节保留 2026-08-04 的历史设计快照；
  当前 DR producer 合同以 `docs/gem5-trace-contract.md` 和本工具实现为准。
