# QEMU user-only → FST v7

## 目标

唯一生产链是：

```text
QEMU TCG full-system
  → 每 vCPU 一条 CPL3 候选宏与动态证据 raw shard
  → X86QemuUserFstLowerer 使用官方 gem5 x86 frontend lowering
  → FastSim BinaryTraceWriter 的 canonical FST v7 + vmap + asmap + manifest
  → FastSim user-only replay
```

QEMU 不生成 UOP、OpClass 或依赖；gem5 lowerer 不运行 guest、不访问 gem5 MMU、
设备或 timing model。TaoTrace 是另一个 FST producer，不参与 QEMU 转换验收或字段
对拍。FST 只包含远程 `origin/FastSim` 定义的功能字段，不接收 timing、cache、PMU
或 oracle sideband。每参与 ROI 的 vCPU 映射为一个 dense FastSim core stream；
不采集或验证 guest TID，不进行 task filtering 或 scheduler 级隔离。

producer 固定使用 QEMU `qemu64` CPU model。负载按 `-march=x86-64` 构建，不额外
暴露 SSSE3、SSE4、AES 或 POPCNT；否则 libc IFUNC 可能选择固定 gem5 frontend
明确未实现的指令。遇到这类指令必须收敛 producer CPUID 或补齐 gem5 官方语义，
不得伪造 UOP。

正式机器合同固定为 `pc-i440fx-10.0`、`3G`。i440fx 将 3 GiB guest RAM 全部放在
4 GiB 以下，并保留 `[3,4) GiB` PCI hole；该布局与参考 C4 基准的容量和地址域一致。
每核 raw envelope 达标后先完成 footer 与压缩，四核 `.capture-complete` 就触发
QMP 退出，不等待完整 benchmark 结束；最终 10M user UOP/core 仍由 lowerer 在完整
宏指令边界截断。workload 若在 envelope 达标前失败，则不会产生完整 capture。

## FST v7 数据面

FST v7 是唯一的 transfer 输出合同。`BinaryTraceWriter` 写入、FastSim
`BinaryTraceSource` 读取；QEMU lowerer 不维护另一套序列化或本地兼容格式。

- 文件头固定 72 B：`magic=FSTRC01`、version、header/record size、core ID、
  record count 和 feature flags；保留字段承载 syscall metadata table 的
  offset/count/row size，以及 `syscall_abi`。当前 producer 写
  `linux-x86_64`，wire format 仍可表达 x86-32、AArch64 和 ARM32。
- 热 `TraceRecord` 固定 64 B：`pc`、memory `address`、branch `target/next_pc`、
  四类 register `producer_dists`、memory `size`、`flags`、gem5 `op_class`、
  `n_src/n_dst`、producer/destination register-class metadata 和 `reserved`。
  普通 memory record 的 `address` 是 producer-local PA；syscall record 的
  `address` 复用为 syscall number。
- `flags` 表达 retire、load/store/atomic、branch/conditional/indirect、
  call/return、taken、micro-op/last-micro-op、physical address、serialize、
  branch outcome 和 virtual-page token。`op_class=-1` 是 user syscall
  transition；v7 还可通过负编码保留 kernel record，但本 user-only producer
  不写 CPL0 record。
- `reserved` 的 bit 31 是 destination-class-count marker，低 31 位是 opaque
  `virtual_page_token`。token 不替代 PA：它标识地址空间内一次 VA page → PA page
  映射世代，供 DTLB/页状态使用；PA 仍供 cache、coherence 和 DRAM 地址使用。
- syscall 不膨胀热记录：每个 syscall record 在 record stream 之后有一个固定
  128 B 稀疏 metadata row，携带 record/syscall ordinal、number、最多六个 ABI
  参数、return/errno/failure 及 validity bits。validity 区分“捕获值为零”和
  “producer 未观测到该字段”。
- `.asmap` 是 `record_ordinal → address_space_id` 的稀疏 transition；
  `.vmap` 是 `token → first_record_ordinal, VA page, PA page` 的映射。
  当前 QEMU 输出要求两者均存在；`manifest.txt` 将每个 `coreN.fst` 注册为一个
  FastSim core stream。`.ifmap` / `.imap` 是 v7 可选的 instruction companions，
  不属于当前 QEMU user-only 发布要求。

## Raw 事实

每个 ROI 内 CPL3 候选宏包含：

- vCPU、normalized CR3、PC、指令字节与长度；
- 必要架构 pre-state（V1 只为已证实的 `DIV/IDIV` 动态微码采集 operand-exact
  field bitmap）；
- 每次成功访存的 VA、PA、方向、大小、最多 16 字节值与 atomic 属性；
- `BEGIN / MEASUREMENT / END` 边界及 marker `112` structured event。

raw 沿用 drmemtrace `trace_entry_t` 和官方 trace version，不增加本地 contract 或
state 子版本。`112.value` 的低 48 位为 canonical PC，48–50 位为 event kind，
51–52 位为 disposition；仅接受 `BETWEEN_INSTRUCTIONS`、`RETIRED_TRANSFER` 和
`UNRETIRED_FAULT`。普通下一 CPL3 候选关闭前一候选；fault 丢弃候选；syscall
transfer 由 `SYSCALL` event 确认。缺失状态、未知 marker、CPL0、MMIO/PIO、缺 PA、
缺动态 load value 或部分完成的 faulted macro 均失败关闭。

## Lowering

- 官方 `X86Decoder` 产生 macro，`fetchMicroop()` 产生 UOP/OpClass。
- 普通 macro 静态枚举 UOP；含内部控制的动态微码使用单宏临时 `ExecContext`。
- 临时 context 只读取 QEMU pre-state 和当前宏的 memory evidence，不跨宏保存状态。
- 每个 gem5 memory UOP必须匹配 QEMU 的地址范围、方向、大小、atomic 和 PA；每个
  QEMU memory evidence byte 也必须被且仅被一个实际 UOP消费。
- virtual-page token 标识 `(ASID, VA page, PA page)` 的一次映射世代；同一 ASID
  内 VA page 被回收后映射到新 PA 时分配新 token，既不拒绝合法重映射，也不允许
  已发布 token 的 PA 发生变化。
- `n_src/n_dst` 保留 gem5 operand multiplicity；producer distance 的
  last-writer identity 独立去重。每条 record 写完整 destination class counts。
- 动态 microcode 的 QEMU memory evidence 按字节精确绑定：每个实际 memory UOP
  必须消费相符的地址、方向、atomic、PA 和 load value；所有 raw evidence byte
  必须被消费，或仅在一个动态 memory UOP 唯一对应一条完整 QEMU access 时将该
  UOP 投影回完整 access。动态路径不跨 macro 保存 gem5 状态，下一条 macro 仍从
  QEMU pre-state 初始化。
- x86-64 syscall gateway 只产生一个序列化 user FST record。lowerer 从 `0F 05`
  候选 pre-state 读取 nr 与六个 ABI 参数，并在下一 CPL3 pre-state 读取真实 RAX
  return/failure/errno；没有观察到的返回字段保持 invalid，不构造 kernel PC 或
  timestamp。`mmap(9)`、`munmap(11)` 缺少 return/failure 一律失败。
- FastSim 功能 replay 使用 `configs/gem5-v28_1-fs-user.cfg`，保留真实 PA、token
  与普通 first-touch cache-state 模型，并启用完整
  `page_fault.syscall_semantic_model`；缺失 syscall return 不以猜值补齐。

## 当前实现结构

```text
tools/qemu_fst/{prepare,build,accept,capture,lower,workloads}.py
  ├─ QEMU FS launcher / workload assets / raw shard staging
  ├─ integrations/gem5/qemu_fst EXTRAS + qemu_fst_to_v7.py
  │    └─ X86QemuUserFstLowerer / QemuFstConverter
  │         ├─ QemuRawTraceReader + QemuMacroAssembler: block I/O and raw assembly
  │         ├─ QemuStateSlots: double-buffered per-macro architectural pre-state
  │         ├─ official X86Decoder + StaticInst + fetchMicroop()
  │         ├─ QemuMicroopDescriptorCache + QemuMemoryBinder
  │         ├─ QemuMicrocodeExecutor: per-shard storage, per-macro transaction
  │         ├─ QemuAddressResolver: VA-page → {PA-page, token}
  │         ├─ QemuDependencyTracker: dense last-writer / producer distance
  │         └─ BinaryTraceWriter(coreN.fst, .asmap, .vmap)
  └─ build/fastsim simulate --measurement-scope user
       └─ replay/stats.json
```

- `prepare` 幂等物化唯一 SPEC C4 workload 集并生成 guest assets；`build` 构建
  FastSim、QEMU、dumper 和 gem5 lowerer；`accept` 是唯一执行入口，每 workload 串行执行
  `capture → lower → strict fs-user replay`，成功后原子发布结果目录。
- raw 一条 shard 只能绑定一个 logical vCPU，一个 vCPU 只能产生一个 ROI shard；
  `BEGIN → MEASUREMENT → END` 必须各恰好一次。达到每核 user-UOP 目标后仍写完
  当前 macro，因此目标值是下限而非切断 micro-op 的硬上限。
- reader 以连续 `trace_entry_t` block 原地分派；state packet 使用两个固定 slot，
  pending macro 只保存 slot index，`refs` 容量跨 macro 复用。shard 状态与 core
  ownership 使用连续表，不在 raw 热路径做 thread hash lookup。
- micro-op descriptor 按稳定 `StaticInst*` 缓存 operand、producer identity 与 memory
  属性；静态多 UOP memory callback 的 displacement cover 只在 lowering 首次构建时
  计算。动态 executor 跨 macro 复用寄存器、generation tag、evidence mask 与输出
  buffer，但每次都从该 macro 的 QEMU pre-state 重新开始，不继承架构状态。
- converter 对 raw transport、header/footer、CPL、state packet、ASID、分支后继、
  PA、memory attributes/value、token 映射和所有 companions 做 fail-close；
  不以计数相等、缺失字段填零或跨地址替换来“修复”输入。
- `qemu_fst_types.hh` 只定义 raw transaction、架构状态、memory evidence 和动态
  lowering result 的值对象；不定义第二套 wire format。raw transport、gem5 x86
  执行和 FST dependency projection 分别由独立组件实现，converter 只编排状态机。
- FastSim reader/replay 是 FST 的唯一语义 consumer gate。converter 只输出
  `boundaries.json` 供 `manifest.txt` 精确划分 warmup/measurement；当前流程不再
  增加 Python 全量扫描、本地 acceptance report，也不与 TaoTrace/DR producer
  做逐字段对拍。
- lowerer 在 `converter.stdout` 输出紧凑的动态路径 aggregate（触发原因、UOP 总数、
  最大 microcode 长度、mnemonic 次数和 scalar-single padding 次数）；它不是 FST
  字段、sidecar 或发布报告。

## 验收

raw 的边界、状态和访存事实由 lowerer fail-close；FST 的唯一语义 gate 是远程
FastSim reader/replay。没有 Python FST 全量扫描、`boundaries.json`、本地 report
或二次字段对拍。

正式 C4 验收使用 `configs/qemu_fst/spec2026_c4.json` 中十个 SPEC CPU 2026
负载。它们的历史 build/run tree 仅从 `/data00/yinhaolang/TCSim` 读取，复制到
`var/qemu_fst/workloads/{build,run}` 后替换同名 marker header，重编独立静态
QEMU ELF；采集时使用本地 workload image 的临时 qcow2 overlay。

### 已完成的 SPEC CPU 2026 C4 验收

2026-09-07 已按每核 10,000,000 user UOP 的正式配置完成 10/10 个独立静态 QEMU
ELF。所有已发布 replay 的 `native_kernel_trace_uops=0`；下面的 FST 体积为四个
core stream 的总和，UOP/指令数来自各例 `replay/stats.json`。

| Workload | User UOP | User instructions | FST | Result |
|---|---:|---:|---:|---|
| `706.stockfish_r` | 61,921,796 | 30,098,418 | 3.69 GiB | PASS |
| `710.omnetpp_r` | 40,044,384 | 20,074,275 | 2.39 GiB | PASS |
| `777.zstd_r` | 59,123,214 | 38,942,479 | 3.52 GiB | PASS |
| `782.lbm_r` | 42,492,782 | 32,565,983 | 2.53 GiB | PASS |
| `803.sph_exa_s` | 40,032,784 | 24,169,409 | 2.39 GiB | PASS |
| `811.tealeaf_s` | 40,484,591 | 25,896,991 | 2.41 GiB | PASS |
| `816.nab_s` | 125,631,600 | 71,743,116 | 7.49 GiB | PASS |
| `854.graph500_s` | 66,800,660 | 35,963,002 | 3.98 GiB | PASS |
| `857.namd_s` | 40,008,259 | 27,218,479 | 2.38 GiB | PASS |
| `881.neutron_s` | 50,118,844 | 27,798,691 | 2.99 GiB | PASS |

每例的发布目录为
`var/qemu_fst/runs/c04/<workload>/`，包含 4 个 raw shards、4 个
`coreN.fst`、对应 `.asmap/.vmap`、`boundaries.json`、`manifest.txt` 和 replay
`stats.json`。`manifest.txt` 使用 FastSim 的
`fastsim-binary-warmup-slice` 合同，在完整宏边界分别记录 warmup 和 measurement
的 instruction/record 数，避免 warmup 被计入正式统计。

Stockfish 还验证了跨边界控制流：producer 在 measurement marker 和显式
`kBetweenInstructions` discontinuity 前写出待决宏及其控制流事实；lowerer 使用
raw branch-target 或 gem5 可推导的 direct/fallthrough successor 完成该宏，并
继续对没有边界证据的 indirect target mismatch 失败关闭。

这组结果证明当前 C4 QEMU producer、official gem5 lowering、canonical v7 writer
和 FastSim user-only consumer 能完成端到端功能转换；它不等价于与 TaoTrace/DR
的逐字段相等，也不证明 host PMU、cache/DRAM/CPI 的外部准确性。

目录重构后的公开入口另以 `706.stockfish_r`、每核 1,000 user UOP
执行了一次完整 `accept` 回归；四核共回放 4,006 records，且
`branches_without_outcome=0`、`unknown_addresses=0`。上表 10M/core 数据来自重构前
同一 user-only 数据合同，本轮未重新执行完整十 workload。

构建与运行：

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh
python -m tools.qemu_fst prepare
python -m tools.qemu_fst build
python -m tools.qemu_fst accept
```

raw marker、本地 ELF 或数据盘布局变更后必须重新采集；历史 TaoTrace FST 只用于
确认远程 v7 wire/consumer合同和部署规模，不参与逐流或地址对拍。

官方 x86 decoder 的少数 scalar-single memory microcode 会把架构 `m32fp` 源操作数
读入 8B 内部临时寄存器。converter 仅对显式白名单中的 scalar-single 指令，且仅在
同地址、唯一完整 4B QEMU load evidence、非 PIO/MMIO/atomic 时填充内部高位零值。
FST 始终保留 QEMU 的 4B address、size、PA、token 和 value；任何未列指令、错位、
缺值或额外访问仍保持失败。
