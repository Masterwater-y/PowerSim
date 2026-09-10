# QEMU-FST 功能仿真主线状态

> 2026-09-10：以 `origin/FastSim@cf346fdf` 的 FST v7 与维护中的
> `configs/gem5-v28_1-fs-user.cfg` 为消费者基线。当前结论是：
> **QEMU raw→FST 功能转换与 FastSim 消费门禁通过；跨 producer 数值只作诊断。**

## 当前判定

| 判断域 | 状态 | 含义 |
|---|---|---|
| QEMU raw 事实完整性 | PASS | 宏边界、pre-state、控制流、访存与异常 disposition 均由 converter fail-close |
| gem5-native lowering | PASS | 普通宏与动态微码均使用固定 gem5 x86 frontend；动态路径受 QEMU 架构事实约束 |
| FST v7 与 companions | PASS | 热记录、宏边界、依赖、ASID、PA、token、syscall metadata 与 AS-scoped `.imap` 满足当前消费者合同 |
| FastSim 功能消费 | PASS | 正式十项、40 个 core stream 全部完成 user-only replay，measurement 人口守恒 |
| QEMU/TaoTrace 数值一致性 | DIAGNOSTIC | 两者是独立 producer 执行，不是 converter correctness oracle |
| FastSim 对 gem5 timing 精度 | 本报告不声明 | 需要独立、同口径的 gem5 timing/PMU oracle 和正式精度门 |

因此当前实现可以作为 `feature/fastsim/dr-fst` 的 **QEMU-FST 功能仿真开发主线**
正式提交。这里的“成功”是 evidence-constrained functional lowering 和 consumer
可用性，不是逐宏完整 post-state 证明，也不是 CPI/cache/coherence 数值透明替代。

## 数据布局

正式 QEMU 生产数据只位于：

```text
var/qemu_fst/runs/production/
  qemu/c04/<workload>/{raw,fst,replay}
  imap-audit.json
  syscall-audit.json
```

不会参与 QEMU 转换门禁的参考、对比和机制验证统一位于：

```text
var/qemu_fst/diagnostics/
  taotrace-reference/c04/<workload>/{fst,replay}
  comparisons/production/c04/pmu.{json,md}
  validation/threaded-multi-asid/
  taotrace-checkpoints/
```

`runs/production` 不再混放 TaoTrace 或跨 producer 对比。`runs/` 下新目录只作为
待验收 QEMU run；正式目录只接受显式提升且不可被 `run/lower/replay` 覆盖。
旧 `tmp/qemu-*`、日期轮次和重复正式目录不保留。

## 功能转换证据

唯一生产链是：

```text
QEMU TCG full-system
  → 每 vCPU 的 CPL3 宏指令与动态架构事实
  → 固定 gem5 X86Decoder / fetchMicroop / 必要动态微码执行
  → canonical FST v7 + .asmap/.vmap/.imap + manifest
  → origin/FastSim user-only replay
```

转换门禁不读取 TaoTrace 数值：

- 每条候选宏必须有完整 QEMU pre-state、ASID、编码和实际 successor；
- faulted macro 丢弃，retired syscall 必须由显式 disposition 关闭；
- memory UOP 必须由同地址范围、方向、大小、atomic、PA 和 load value 的 QEMU
  evidence 覆盖；
- 所有 QEMU memory evidence byte 必须被消费，禁止同方向任取、跨地址替换或缺值
  填零；
- 动态微码每个宏重新注入 QEMU pre-state，不跨宏继承 gem5 架构状态；
- unsupported register/MSR、缺 PA、未闭合宏、无 evidence 的动态访问均直接失败；
- FST reader 验证 `.vmap/.asmap` 的 token 首次 ordinal、ASID、PA 和全量消费；
- measurement 仅在完整宏边界关闭，manifest、boundaries、FST 和 replay 人口必须
  精确守恒。

正式十项真实负载共执行：

- `582,833` 个动态微码宏；
- `7,751,425` 个动态生成 UOP；
- `164,535` 个内部控制流宏；
- `276,687` 个 atomic 宏；
- `64,462` 次 fragmented memory evidence 绑定；
- `126,675` 次仅限白名单 scalar-single 指令的 4B 架构 load 适配。

这覆盖 `DIV/IDIV`、`CMPXCHG`、`XADD`、字符串操作和长动态微码路径。十项 converter
均以 `x86 QEMU-FST conversion complete` 结束，没有语义降级后继续发布。

## 正式矩阵

- 10/10 QEMU workload、40/40 FST 完成 FastSim replay；
- measurement 共 `400,000,023` 条 user UOP，每核至少 10M；
- 40/40 `.vmap/.asmap` 在正式 reader 流中验证，共 `66,361` 个 token mapping；
- 40/40 AS-scoped `.imap` 通过审计，共 `192,018` 个静态 instruction rows，全部
  具有 architectural operand masks；
- 所有 replay 的 `unknown_addresses=0`、`branches_without_outcome=0`；
- 382 个 syscall 全部具有 arguments；376 个具有 return/failure；6 个
  `futex(202)` 在 trace cutoff 前未返回，semantic violations 为 0；
- 统一 replay 配置为 C4、3 GHz、32 KiB L1D、1 MiB private L2、64 MiB shared
  LLC、8-channel/3 GiB DRAM、`measurement_scope=user`。

Zstd 的 2,760 条和 NAMD 的 2 条 `page_fault_untracked_accesses` 是 reader 明确允许的
跨 4 KiB memory UOP：它们保留物理地址，只因单个 token 不能表示两个虚页而跳过
page-state/DTLB identity；不是未知 memory address 或静默丢失。

真实并发验证位于
`var/qemu_fst/diagnostics/validation/threaded-multi-asid/`。四个 stream 分别观察到
`3/2/2/2` 个 ASID 和 `72/182/184/165` 次切换，总计 `400,000` measurement user
UOP；每个参与 ASID 均观察到两个 FS_BASE，395 个 syscall 中 386 个具有返回，
9 个未返回 `pause(34)` 留在 cutoff，semantic violations 为 0，FastSim replay
完成。

## Replay 模型边界

QEMU-FST 与 TaoTrace-FST 均在 lowering 之后进入相同的
`configs/gem5-v28_1-fs-user.cfg`。其中：

```text
page_fault.event_model = false
page_fault.cache_state_model = true
page_fault.syscall_semantic_model = true
page_fault.roi_entry_page_state_model = false
```

`cache_state_model` 是远程 FastSim 当前维护的 user-only replay 模型，不属于 QEMU
converter，也不按 producer 选择不同实现。它统一读取 lowering 后的 memory UOP、
token 与 syscall metadata；不同 synthetic cache-state 结果来自两份 FST 的动态流
不同，而不是配置不对称。

精确 ROI-entry PTE overlay 在当前 user-only profile 中保持关闭。QEMU `.vmap` 的
相关 flags 保持 unknown 不影响当前转换门禁；只有未来启用
`page_fault.roi_entry_page_state_model` 时才必须补齐同口径 producer 事实。

## 跨 Producer 诊断

TaoTrace reference 位于
`var/qemu_fst/diagnostics/taotrace-reference/c04/`。九项、36 个 FST 均完成相同
FastSim 配置的 replay；Graph500 因 source warmup 未在 600 秒预算内到达
measurement marker，保持 `reference_unavailable`，不影响 QEMU 10/10 主线结论。

下表中的百分比均为
`(QEMU-FST replay - TaoTrace-FST replay) / TaoTrace-FST replay`：

| Workload | QEMU CPI | TaoTrace CPI | CPI Δ | Memory UOP Δ | Branch Δ | LLC miss Δ |
|---|---:|---:|---:|---:|---:|---:|
| `706.stockfish_r` | 0.223867 | 0.241090 | -7.14% | -8.04% | -10.23% | -29.83% |
| `710.omnetpp_r` | 0.383263 | 0.335751 | +14.15% | -1.08% | -0.17% | +262.71% |
| `777.zstd_r` | 0.408059 | 0.409084 | -0.25% | +0.00% | -0.02% | -36.63% |
| `782.lbm_r` | 3.021382 | 2.709866 | +11.50% | +1.62% | -0.03% | +11.79% |
| `803.sph_exa_s` | 0.292267 | 0.333578 | -12.38% | -8.12% | +12.21% | -15.89% |
| `811.tealeaf_s` | 1.174549 | 0.520268 | +125.76% | +15.29% | -28.30% | +47.98% |
| `816.nab_s` | 0.231743 | 0.256160 | -9.53% | -2.50% | +6.73% | -40.00% |
| `857.namd_s` | 0.259045 | 0.270150 | -4.11% | -0.88% | +1.96% | -42.57% |
| `881.neutron_s` | 1.015106 | 1.008713 | +0.63% | +0.12% | -0.03% | +9.65% |

九项聚合：

| FastSim 输出 | MAPE | P50 APE | WAPE | Signed aggregate |
|---|---:|---:|---:|---:|
| Retired instructions | 0.57% | 0.43% | 0.61% | +0.30% |
| Memory UOPs | 4.19% | 1.62% | 3.46% | -0.65% |
| Branches | 6.63% | 1.96% | 7.78% | -2.12% |
| CPI | 20.61% | 9.53% | 18.32% | +15.20% |
| Branch misses | 18.62% | 8.63% | 2.94% | -2.03% |
| L1D misses | 16.07% | 13.14% | 12.49% | +10.14% |
| L2 misses | 70.20% | 28.29% | 21.32% | +19.53% |
| LLC/DRAM reads | 55.23% | 36.63% | 21.54% | +18.50% |
| DTLB misses | 8.18% | 9.62% | 1.98% | -1.10% |

完整 58 字段结果位于
`var/qemu_fst/diagnostics/comparisons/production/c04/pmu.{json,md}`，其状态固定为
`diagnostic_only`。这些数值不能单独区分：

- CPUID 与 libc IFUNC 选择；
- workload phase 和 source warmup；
- guest 调度、线程到 vCPU 的映射以及 measurement 内 ASID；
- ASLR、页表和 producer-local PA；
- FastSim 对访问顺序、cache/coherence 与 DRAM 地址映射的模型响应。

TeaLeaf 当前两侧 workload ELF 已核对为同一 SHA-256，因此该 workload 不应再泛称
“双 ELF”；其他 workload 的 binary identity 仍应逐项核验。TeaLeaf 的 40M user UOP
相等是固定截断条件，但 memory UOP `+15.29%`、branch `-28.30%`，且 TaoTrace core 2
存在 `ASID A → B → A`，说明动态执行与线程交错未等价。其 CPI `+125.76%` 主要沿
memory/cache response 链放大，不能据此反推 converter 错误。

## 发布结论

正式分支可以声明：

> QEMU 宏指令与动态架构事实已通过固定 gem5 x86 frontend 转换为 canonical FST v7，
> 并在十项真实 C4 workload 上完成 FastSim user-only 功能 replay。

正式分支不得声明：

- QEMU-FST 与 TaoTrace-FST 逐流、逐地址或 CPI/PMU 数值等价；
- FastSim 已复现 gem5 O3、Ruby 或 DRAM 的完整时序；
- 不同 producer 的物理页、线程交错或 source warmup 已被标准化；
- 当前 dynamic executor 已完成所有 destination register 的逐宏 post-state 证明。

后续 CPUID/IFUNC、线程交错和页面布局实验用于收紧跨 producer 可比性，不阻塞当前
功能转换主线提交；任何数值改善也不得通过改写 QEMU 事实、FST 字段或拟合统一
cache/CPI 系数获得。
