# QEMU user-only → FST v7

## 目标

唯一生产链是：

```text
QEMU TCG full-system
  → 每 vCPU 一条 CPL3 候选宏与动态证据 raw shard
  → X86QemuUserFstLowerer 使用官方 gem5 x86 frontend lowering
  → FastSim BinaryTraceWriter 的 canonical FST v7 + companions + manifest
  → FastSim user-only replay
```

QEMU 不生成 UOP、OpClass 或依赖；gem5 lowerer 不运行 guest、不访问 gem5 MMU、
设备或 timing model。TaoTrace 是独立的参考 FST producer：它不参与 QEMU raw→FST
转换门禁，只用于观察两种功能输入经过同一 FastSim 配置后的响应差异。FST 只包含远程
`origin/FastSim` 定义的功能字段，不接收 timing、cache、PMU 或 oracle sideband。
每参与 ROI 的 vCPU 映射为一个 dense FastSim core stream；
不按 guest task 过滤执行流。lowerer 使用每条 CPL3 pre-state 已有的
`ASID/FS_BASE/RSP` 区分同一 vCPU 上的 user execution context，仅用于 syscall
返回关联和依赖历史隔离，不写入 FST wire。

### 对齐边界

QEMU 遵循 `origin/FastSim` 的是下游数据编排：source warmup prefix 与
measurement 来自同一真实执行流，lowerer 输出每核精确 instruction/record 边界，
FastSim 在所有活动 stream 到达公共 barrier 后仅清零 measurement 时间和计数，
继续保留 warmup 建立的 cache/coherence、predictor、DTLB、DRAM 和依赖状态。

与参考 TaoTrace 运行同步的可控外部条件包括 C4、3 GiB、Ubuntu 24.04 base、同一
kernel 与基础 kernel arguments、UTC、禁网、workload 输入、argv/显式 env 和
`x86-64` 编译基线；QEMU 只追加 runner 所需的 `panic=-1`、`init=` 和 workload
选择参数。负载源码已有的 OpenMP binding、fork worker affinity 和 ROI wave
barrier 是参考 workload 行为，QEMU build 原样保留；它们不升级为 tracer 协议。
QEMU tracer 不按 TID/CR3 选择任务，也不增加一个在 measurement 边界阻塞 worker
的协调线程。CR3/ASID 作为地址空间事实进入 FST companions；FS_BASE/RSP 只在
lowering 期间识别同地址空间内的线程上下文。

这种同步仅指 guest-visible 实验条件。QEMU producer 固定为
`pc-i440fx-10.0 + qemu64 + TCG`；TaoTrace producer 使用 gem5 O3、MESI Three
Level cache/coherence 和 3 GHz 时钟。两者不是同一个 producer 微架构拓扑，不能
把 QEMU TCG 的执行时间或 cache 行为与 gem5 O3 对齐。可比较的性能模型位于 FST
之后：两侧 FST 必须用完全相同的 FastSim replay 配置执行。

该相同配置也包括 FastSim 的 `page_fault.cache_state_model`。模型在 lowering
之后统一消费两侧的 FST memory UOP、token 和 syscall metadata；它不属于 QEMU
converter，也不会按 producer 选择不同实现。同一模型对两份不同动态流产生不同
cache state 是诊断结果，不是配置不对称。

producer 固定使用 QEMU `qemu64` CPU model。负载按 `-march=x86-64` 构建，不额外
暴露 SSSE3、SSE4、AES 或 POPCNT；否则 libc IFUNC 可能选择固定 gem5 frontend
明确未实现的指令。遇到这类指令必须收敛 producer CPUID 或补齐 gem5 官方语义，
不得伪造 UOP。

正式机器合同固定为 `pc-i440fx-10.0`、`3G`。i440fx 将 3 GiB guest RAM 全部放在
4 GiB 以下，并保留 `[3,4) GiB` PCI hole；该布局与参考 C4 基准的容量和地址域一致。
600 秒是从真实 `start` marker 等待真实 `measurement` marker 的 producer 墙钟
预算，不是 FastSim warmup 指令数，也不会截短或伪造 source prefix。
每核 raw envelope 达标后先完成 footer 与压缩，四核 `.capture-complete` 就触发
QMP 退出，不等待完整 benchmark 结束。默认 raw envelope 与最终 FastSim target
相同，均为每核 10M：前者按已提交 CPL3 宏指令计数，后者由 lowerer 按 user UOP
在完整宏指令边界截断。每个成功提交的 x86 宏至少产生一个 UOP，因此 10M raw 宏
通常足以覆盖 10M UOP；若 fault 或边界丢弃导致不足，lowerer 必须失败关闭。
workload 若在 envelope 达标前失败，也不会产生完整 capture。

## FST v7 数据面

FST v7 是唯一的 transfer 输出合同。`BinaryTraceWriter` 写入、FastSim
`BinaryTraceSource` 读取；QEMU lowerer 不维护另一套序列化或本地兼容格式。

QEMU user-only stream 按 vCPU 输出，但不额外实施目标进程过滤。若全局 ROI 内同一
vCPU 观察到 CR3 切换，lowerer 将切换写入 `.fst.asmap`，并按
`(address_space_id, virtual_page)` 隔离 page token，以遵循远程 FastSim 的多地址
空间合同。lowerer 从同一 raw 指令字节与 gem5 lowering 生成唯一当前
AS-scoped `.fst.imap`，以 `(address_space_id, pc)` 记录 instruction size、
fallthrough、control-flow、may-access-memory 和 architectural operand masks。
该 companion 是对 `origin/FastSim@cf346fd` 文档中未来 AS-scoped 目标的本地
前向补齐；远端该提交的实际 reader 仍是 PC-only。
ASID 或 FS_BASE 切换同时清空 register producer-distance history，禁止跨进程或
线程建立依赖。syscall record 在 entry 按原流顺序写出；pending syscall 以
`(ASID, FS_BASE, entry RSP)` 隔离，返回时精确匹配；仅为支持 `arch_prctl` 改变
FS_BASE，允许唯一 `(ASID, entry RSP)` 回退。return/failure 回填尚未落盘的 v7
metadata table，不移动 hot record。trace cutoff 前未观察到 return 的普通 syscall
允许缺少可选 return 字段；`mmap/munmap` 仍要求完整返回语义。

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
  `first_record_ordinal` 必须指向第一条实际携带该 token 的 FST memory UOP，
  不能使用更早的 QEMU memory callback 或 macro 起始 ordinal。
  当前 QEMU 输出要求两者均存在；`manifest.txt` 将每个 `coreN.fst` 注册为一个
  FastSim core stream。`.ifmap` / `.imap` 是 v7 可选的 instruction companions。
  当前 `.imap` 只有一种 AS-scoped 布局；每行包含 ASID、PC、instruction size、
  fallthrough、branch/direct-target、may-access-memory 与 x86 architectural
  operand masks。旧 `FSTIMP1/2` companion 不再被 reader 接受。

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
- 该路径是 **QEMU 架构事实约束的 gem5-native lowering**：它验证动态微码路径
  可执行、实际控制流和全部 memory evidence 可解释，但不声称对每个宏的全部
  destination register 做逐项 post-state 等价证明。FST 的消费者合同需要 UOP、
  OpClass、依赖、分支结果和访存事实，不要求保存完整架构 post-state。
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
  │         ├─ QemuAddressResolver: (ASID, VA-page) → {PA-page, token}
  │         ├─ QemuDependencyTracker: dense last-writer / producer distance
  │         └─ BinaryTraceWriter(coreN.fst, .asmap, .vmap, optional .imap)
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
- lowerer 在原子发布前检查 FST 与必需 companions 存在，并对可选 `.imap` 做
  静态几何审计。FastSim reader 在正常 replay 流中验证 `.vmap/.asmap` header、
  token 首次 ordinal、ASID、PA 和 token 全量消费；不再由 Python 对 FST 做第二次
  全量扫描。destination-class marker 与 `n_dst` 守恒在 C++ producer 写出边界
  检查，并由 FastSim consumer 再次验证。
  `boundaries.json` 仍只负责 converter 与 manifest 生成器之间的精确 phase 边界。
- lowerer 在 `converter.stdout` 输出紧凑的动态路径 aggregate（触发原因、UOP 总数、
  最大 microcode 长度、mnemonic 次数和 scalar-single padding 次数）；它不是 FST
  字段、sidecar 或发布报告。

## 验收

正式结论分为两个互不替代的域：

1. **QEMU-FST 功能转换门禁**：raw 的边界、pre-state、控制流和访存事实由 lowerer
   fail-close；动态微码必须由 QEMU evidence 完整约束；FST 必须通过远程 FastSim
   reader/replay；measurement、宏边界、依赖和 companions 必须守恒。该门禁通过即可
   将 QEMU-FST 作为功能仿真主线。
2. **跨 producer 诊断**：TaoTrace-FST 只用于观察功能人口和同模型响应。QEMU 与
   TaoTrace 来自两次独立 guest 执行，受 CPUID/IFUNC、source warmup、线程调度、
   地址布局和 producer-local PA 影响；CPI、cache、coherence 或 DRAM 数值不得反向
   作为 converter 正确性的硬门禁。

`.imap` 与 syscall table 只做小 companion/稀疏 metadata 审计。`.vmap/.asmap` 的
token、首次 ordinal、ASID、PA 与全量消费由 reader 在正式 replay 流内验证，不再以
独立 Python 全量扫描 hot records 重复门禁。不生成第二份自定义 acceptance schema。

正式 C4 验收使用 `configs/fst_pipeline/spec2026_c4.json` 中十个 SPEC CPU 2026
负载。它们的历史 build/run tree 仅从 `/data00/yinhaolang/TCSim` 读取，复制到
`var/qemu_fst/workloads/{build,run}` 后替换同名 marker header，重编独立静态
QEMU ELF；采集时使用本地 workload image 的临时 qcow2 overlay。

### 当前验收

`854.graph500_s` 属于正式 QEMU-FST 生产矩阵。它的参考 TaoTrace source warmup
超过 600 秒预算，因此替代一致性报告将其标记为 `reference_unavailable`；该参考
限制不改变 QEMU-FST 的十项生产分母。

当前正式 QEMU-FST 结果根为：

```text
var/qemu_fst/runs/production/qemu/c04/
```

该根只包含当前代码重新 lower 的十项 QEMU canonical FST 及其
`raw/fst/replay`。矩阵级 `.imap` 与 syscall 审计位于
`var/qemu_fst/runs/production/`。QEMU 10/10 replay 的 warmup/measurement records
与 manifest、boundaries 精确守恒，`measurement_scope=user`，DRAM 为 3 GiB。

TaoTrace reference 与跨 producer 数值报告不是 QEMU 生产物，统一位于：

```text
var/qemu_fst/diagnostics/taotrace-reference/c04/
var/qemu_fst/diagnostics/comparisons/production/c04/
```

TaoTrace 九项已按唯一 AS-scoped `.imap` 合同重新采集并 replay。比较按字段域聚合：
功能人口保留所有结构有效 reference；前端/CPI 只纳入 static-span 完整项；
cache/TLB/coherence/DRAM 固定为 producer-sensitive diagnostic。

同 ASID 多线程 syscall 验收根为：

```text
var/qemu_fst/diagnostics/validation/threaded-multi-asid/
```

该用例在 4 个 vCPU 上同时运行多进程和同进程 pthread，并让 blocker 线程保留
未返回 `pause(34)`，用于证明其他线程不会误消费其返回值。

TaoTrace 重采所需且不能从最终 FST 推导的 ROI checkpoint 位于
`var/qemu_fst/diagnostics/taotrace-checkpoints/`。checkpoint 使用的 workload
disk 直接引用只读稳定外部资产；本地不保留重复 disk image。

最新数值、模型输入/标签边界和剩余问题只维护在
[当前状态](current-status.md)。

构建与运行：

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh
python -m tools.fst_pipeline prepare
python -m tools.fst_pipeline build
python -m tools.fst_pipeline run --run-id qemu-c4
```

正式 `run` 固定 workload 串行且不提供覆盖选项；已有发布或 staging 目录一律
拒绝继续执行，必须使用新 `--run-id`。capture 不自动重试，失败 raw/log 保留在
原目录。复用 production 中已有 raw 或 FST 必须显式调用
`lower --raw-run-id ... --run-id ...` 或 `replay --run-id ...`。
`run/lower/replay` 禁止将 `production` 作为目标 run-id；TaoTrace collector 也
必须显式指定非 production 输出且不提供覆盖。production 只接受验收后的显式提升。
`--user-fst-target` 仅用于小规模机制验证；默认值是每核 10M。

raw marker、本地 ELF 或数据盘布局变更后必须重新采集；TaoTrace FST 必须显式传给
`tools.fst_pipeline compare`，作为 reference input 比较同一 FastSim 的响应，不能
反向参与 QEMU raw→FST 转换或跨 producer 逐流地址对拍。

当前正式判断是：QEMU-FST 功能转换与 FastSim 消费门禁通过；跨 producer 数值报告
保持 `diagnostic_only`。这两项结论允许同时成立，数值差异不用于否定已经通过的
raw→FST 语义门禁，也不用于宣称 FastSim 已达到 gem5 timing 精度。

官方 x86 decoder 的少数 scalar-single memory microcode 会把架构 `m32fp` 源操作数
读入 8B 内部临时寄存器。converter 仅对显式白名单中的 scalar-single 指令，且仅在
同地址、唯一完整 4B QEMU load evidence、非 PIO/MMIO/atomic 时填充内部高位零值。
FST 始终保留 QEMU 的 4B address、size、PA、token 和 value；任何未列指令、错位、
缺值或额外访问仍保持失败。
