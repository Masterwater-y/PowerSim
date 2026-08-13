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
4. **`uarch_first` 的 supported cases 已闭合到 FST 功能字段。** 清理 raw
   gem5 JSONL 后，11 个可转换 workload 的 FST pair validation 仍为 pass。
5. **`v28_int_div_serial` 仍是 unsupported。** 原因是
   `dynamic_internal_microcode_control`，不是普通字段 mismatch，也不能用近似
   macro trace 伪装成 strict match。
6. **新 `business_excitation` 负载组把同一缺口扩大为组级阻塞。** 12 个 DR
   binary 中静态检查有 11 个在 ROI 附近包含运行时整数 `div`；已正式采集的 3 个
   case 均在 DR FST 转换时命中同一 reason code。
7. **replay topology counters 仍是 diagnostic-only。** replay functional totals
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
        -> gem5 x86 decode/lowering
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

当前本地 `uarch_first` 清理后状态：

| Artifact | 状态 |
|---|---:|
| `tmp/dr-traces/uarch_first` | 约 693M |
| `tmp/dr-fst/uarch_first` | 约 9.8G |
| raw `*.records.micro.jsonl` | 0 |
| FST `core*.fst` | 93 |

93 个 FST 对应 48 个 gem5 FST 加 45 个 DR FST；`v28_int_div_serial` 没有完整 DR
FST。

## 2. 字段验收口径

当前 strict gate 分为 FST 结构和 `core_reconstructable` 字段。严格比较包括：

- FST header、record size、file size、manifest 顺序和 core ID。
- 控制流字段：`pc`、`target`、`next_pc`。
- 分支字段：branch/call/return/conditional/indirect flags、committed direction、
  `kTaken`、`kBranchOutcomeValid`。
- memory 形状：load/store/atomic classification、非零 `size`、
  `kPhysicalAddress`、`kVirtualPageToken`、cache-line byte offset。
- UOP 和依赖：`op_class`、`n_src`、`n_dst`、`producer_dists`、
  `producer_classes`、destination class counts。
- syscall record 形状：`op_class == -1`、`address == syscall_number`、
  serializing semantics，且不携带 memory/physical-address flags。

两个字段只比较形状和可重建关系，不比较跨 producer raw value：

- `address`：gem5 FST 使用 guest PA，DR FST 使用 host PA；当前比较 cache-line
  byte offset，并要求 DR side provenance 完整，不比较 raw PA equality。
- `reserved`：低 31 位 virtual-page token 是 producer-local intern ID；当前要求
  token 存在和复用形状正确，不比较 raw token ID。

`address-provenance.json` 是 DR side 证据 sidecar，记录每个 logical core 接受的
PID 及 VA-page/PA-page/token 映射。FST v6 没有 ASID 字段，所以同一 output core
跨地址空间会被拒绝。

## 3. 当前验收结果

清理 raw trace 后，现有 FST 产物可复验 11 个 supported workload：

| 阶段 | 结果 |
|---|---:|
| supported FST pair validation | 11/11 pass |
| full read-only validation | 11 pass + 1 missing DR FST |
| replay functional totals | 11/11 exact in existing report |

Supported workload 当前结果：

| Workload | Records | FST gate | Replay status |
|---|---:|---|---|
| `v28_int_alu_dense` | 5,161,060 | pass | diagnostic_only |
| `v28_fp_alu_dense` | 5,275,920 | pass | diagnostic_only |
| `v28_simd_sse_dense` | 8,290,620 | pass | diagnostic_only |
| `v28_cache_L1_mixed` | 6,822,028 | pass | diagnostic_only |
| `v28_cache_L2_mixed` | 6,822,028 | pass | diagnostic_only |
| `v28_memory_seq_moderate` | 6,684,820 | pass | diagnostic_only |
| `v28_memory_random_mlp` | 6,684,844 | pass | diagnostic_only |
| `v28_coh_readmostly_sparse` | 6,293,636 | pass | diagnostic_only |
| `v28_gofeed_base` | 6,610,856 | pass | diagnostic_only |
| `v28_pytorch_base` | 6,265,212 | pass | diagnostic_only |
| `v28_mysql_base` | 7,642,467 | pass | diagnostic_only |

`v28_int_div_serial` remains unsupported:

- reason code：`dynamic_internal_microcode_control`
- PC：`0x402123`
- 语义：stock drmemtrace 没有 gem5 动态内部 microPC 证据，无法 strict 重建。

当前 `uarch_first` ROI 内没有实际 syscall record；syscall encoding path 存在，
但本批 workload 没覆盖 syscall-rich 行为。

## 4. `business_excitation` 的 DIV 阻塞

新负载组不是偶然出现一条未支持指令。静态检查
`configs/workloads/business_excitation.json` 对应的 12 个 DynamoRIO binary，结果
是 11 个含 ROI 内运行时整数 `div`，只有 `gofeed_graph_pages_80` 未发现该模式。
共同来源是 `business_excitation.c` 中的运行时取模：

```c
static inline size_t uniform_line(size_t lines, uint64_t key)
{
    return (size_t)(key % lines);
}
```

`lines` 不是编译期常量，因此 x86-64 编译结果使用 `div`。目前正式执行过的三项
结果如下：

| Workload | DR capture | DR FST conversion | 首个阻塞 PC |
|---|---|---|---:|
| `mysql_hot_index_48k` | pass | unsupported | `0x40221d` |
| `gofeed_shared_hotspot` | pass | unsupported | `0x40221c` |
| `gofeed_graph_48m_random` | pass | unsupported | `0x40221c` |

三项均报告 `dynamic_internal_microcode_control`。这里不是 DynamoRIO 对 x86 `div`
解码错误：architectural instruction、encoding 和宏指令顺序都已存在。失败发生在
当前 converter 试图把它转换成 gem5 的动态 micro-op 流时。

gem5 的 x86 `DIV/IDIV` 使用 `Div1`、多次 `Div2`、内部条件 `br`、`Divq` 和
`Divr`。`Div2` 根据运行时 dividend、divisor 和 remaining bits 更新内部状态，
因此实际 microPC 路径不能由静态 `fetchMicroop(0..last)` 或下一条 architectural
PC 推导。stock drmemtrace 也不记录 gem5 私有 microPC。当前 fail closed 避免了
以下字段被近似值污染：

- micro-op record count、`op_class`、`kMicroOp` 和 `kLastMicroOp`；
- 内部 branch 的 direction、target 和 next microPC；
- micro-op 级 `n_src`、`n_dst`、producer distance/class 和 writer history。

## 5. 可行语义路线

当前没有官方接口能把 native DynamoRIO execution 直接导出为 gem5 私有动态
microPC。可行设计只有两条，需先决定哪一层是公共合同。

### 5.1 DynamoRIO architectural FST

使用 DynamoRIO 官方 core-simulation 能力作为 DR producer 的权威语义：

```text
drmemtrace
  -> decode_cache_t / DR_ISA_REGDEPS
  -> architectural FST
  -> FastSim architectural record consumer/lowering
```

`decode_cache_t` 负责 embedded encoding、module mapping、JIT/self-modifying code
的 `encoding_is_new` 和 decode cache 失效；`DR_ISA_REGDEPS` 提供 operation
category、architectural source/destination register dependencies、operand size 和
flags dependency。drmemtrace 本身继续提供宏分支方向/目标、内存引用和调度 marker。

在这条路线中 `DIV` 是一条 architectural integer-division record，FastSim 为它
分配自身的 latency/resource 模型，不声称复刻 gem5 `Div2` 循环。gem5 FST 先按
macro instance 聚合成 architectural projection，再与 DR FST 比较。该方案需要
调整 FastSim consumer 和 validation contract，但不需要改 DynamoRIO client，也
不需要在 converter 中维护 gem5 division state。

### 5.2 gem5-style dynamic micro-op FST

若仍要求当前逐 micro-op strict pair validation，则必须补充 stock drmemtrace
没有的动态状态：`RAX`、`RDX`、divisor value、operand size/signedness、commit 或
fault outcome，并按 thread instruction ordinal 与主 trace 对齐。随后还要用同版本
gem5 microcode 执行器重放 microPC path。

这条路线需要自定义 DynamoRIO sideband 和 gem5-compatible replay，两侧版本耦合
最强、维护成本最高。把 `div` 直接压成单条 synthetic `IntDiv` 只能作为 relaxed
profile，不能继续发布为 strict gem5-style FST。

### 5.3 当前建议

如果目标是让 FastSim 接受真实业务负载并进行架构性能研究，优先选择 5.1：保留
统一 FST 容器，但显式区分 architectural 与 gem5-microop producer semantics。
如果目标是证明 DR producer 与 TaoTrace 逐 micro-op 完全相同，只能选择 5.2。
通过 power-of-two mask、常量取模或 reciprocal multiply 消除 workload 中的
`div` 可以用于临时解阻，但它改变指令流，不是 converter 的正式支持方案。

官方参考：

- DynamoRIO Core Simulation Support：
  <https://dynamorio.org/sec_drcachesim_core.html>
- DynamoRIO `decode_cache_t`：
  <https://dynamorio.org/classdynamorio_1_1drmemtrace_1_1decode__cache__t.html>
- DynamoRIO `DR_ISA_REGDEPS`：
  <https://dynamorio.org/dr__ir__encode_8h.html>
- gem5 x86 Micro-op ISA：
  <https://www.gem5.org/documentation/general_docs/architecture_support/x86_microop_isa/>
- gem5 TraceCPU / Elastic Trace：
  <https://www.gem5.org/documentation/general_docs/cpu_models/TraceCPU>

## 6. 仍需讨论的问题

1. **raw PA topology gate。** 当前只证明 marker-backed PA 存在、offset 可比和
   DR provenance 完整；若要比较 cache/CHA/DRAM topology，需要先定义 guest PA 与
   host PA 的可比投影。
2. **virtual-page token topology。** token ID 是 producer-local；DTLB/page-walk
   若需要更强验收，应定义 token 拓扑诊断，而不是比较 raw `reserved` 值。
3. **cross-page memory。** FST v6 单 record 只能携带一个 address 和一个 token；
   当前 converter fail closed。
4. **dynamic microcode control。** 先选择 architectural FST 或 gem5-style
   micro-op FST；在语义合同未确定前，不新增 `div` 特化 lowering 或 sideband。
5. **syscall-rich workload。** 需要单独验证 syscall number、marker placement，
   以及是否需要 args/retval/blocking/kernel-service sidecar。
