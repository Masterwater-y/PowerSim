# FST 完整动态依赖扩展

日期：2026-09-09。为新事件模型保存动态 RAW 边，解除四槽记录限制。
不引入 workload 判断、时序标签或延迟补偿。

后续 [同一前缀接入与阶段验证](causal-core-memory-prefix-gate-20260909.md) 已完成既有
域外访存策略接入：14,560 UOP、27 条扩展边全部保留并通过事件守恒。本文下面的
APIC 拒绝是采集阶段当时的结果，已解除；没有新增设备模型，正式 CPI 改善仍未证明。

## 表示与空间

FST v7 主记录仍为 64 bytes，`producer_dists[4]` 和目的寄存器类别位布局不变。
新增主文件 feature bit 5 表示：整个功能流的受跟踪寄存器 RAW 依赖完整，且必须有
同名 `.fst.deps` 附件，附件可以只有头部。旧读取器遇到未知 feature 会报错。

采集端按 producer UOP 身份去重、按距离递增排序；前四条保存在主记录，剩余写入附件。
`n_src` 保持源寄存器计数，它不等于不同 producer 的个数；七个源寄存器可以只依赖一条
生产指令。因此此前仅凭 `n_src > 4` 判定的记录应称为“存在截断风险”，不能据此证明丢边。

| `.deps` 字段 | 字节 |
|---|---:|
| 文件头：magic/version/header_size/core_id/flags/record_count/extension_count/extra_distance_count | 48 |
| 每条扩展：record ordinal / extra count / 主记录 FNV-1a 指纹 | 16 |
| 每个额外 producer distance，uint32 | 4 |

新增大小严格等于 `48 × 文件数 + 16 × 扩展记录数 + 4 × 额外边数`。
单个 UOP 有 8 个不同 producer 时增加 32 bytes，有 16 个时增加 64 bytes；
其余记录没有逐记录空间增加。支持 8、16、20 及更多 producer，不在 8/16 处重新截断。
现有 `n_src` 的 uint8 上限仍为 255，distance 为 uint32；超范围采集明确失败，不再
饱和裁剪距离。“完整”指现有 Int/Float/Vec/CC 跟踪类别的寄存器 RAW 边；内存顺序、
系统操作和前缀前的初始寄存器状态仍遵守各自的合同。

## 接入

- [`fst_dependencies.hpp`](../include/fastsim/fst_dependencies.hpp) 定义磁盘布局，与
  [TaoTrace 补丁](../patches/gem5-taotrace-complete-dependencies.patch) 的头文件相同。
- BinaryTraceSource 流式读取附件，检查主文件身份、大小、行顺序、边数、距离和主记录
  指纹；不会把整份依赖表载入内存。InstructionSlice / Warmup wrapper 传递扩展，
  BinaryTraceWriter / FST upgrade 保留扩展，采集整理与数据复制工具随主文件搬运附件。
- `causal_read` 的 decode buffer 为每条记录保存自己的扩展，dispatch 注册全部
  producer，实际 writeback 才唤醒消费者；预热边界不重置依赖。
- 有完整动态依赖时不调用静态 macro operand map 推测额外边。旧 trace 保留原补全
  与拒绝条件；给历史数据补零或只改完整性标志不能恢复丢失的边。
- 尚未接入扩展边的旧 interval 求解器遇到非空扩展表会明确拒绝，不静默忽略第 5 条
  以后的依赖。新事件路径没有整数/单核限制。

## 验证与复现

构建与 `fastsim_tests` 通过。新增必要检查覆盖 8/16/20 fan-in、第 4/16 槽之外的慢
producer、切批/预热不变性、多寄存器共享一个 producer、FST 重写、缺失附件和主记录
错配拒绝。独立 Python 审计器也检查了有效 C++ roundtrip 和错配附件：

```bash
python3 tools/audit_fst_dependencies.py --require-complete \
  --fst /path/to/core0.fst --fst /path/to/core1.fst --output audit.json
```

真实采集只选 TeaLeaf：4 核、L1D 64 KiB、原 ROI checkpoint，每核 10,000 用户记录，
保留功能预热及期间的 native kernel 记录。固定各核前 2,000 宏指令接入事件模型，
前 1,000 预热、后 1,000 测量；该检查不是完整 ROI CPI 精度验收。

产物：[`tmp/fst-dependencies-20260909/`](../tmp/fst-dependencies-20260909/)。
`collect-command.json` / `collect-environment.json` 保存命令和环境，`source-hashes.json`
保存修改的源文件与共享 cache 头文件身份。采集器在已有隔离 gem5 checkout 构建，
使用 Python 3.11 和 `TAOGEN_SHARED=/data00/yinhaolang/taogen/shared`，未覆盖原 gem5-fs。

### TeaLeaf 实测结果

在同一 checkpoint、同一 10,000 用户记录/核目标下，原 gem5 与新增依赖的 gem5
各采集一次；功能预热保留。共 2,939,373 UOP，其中 core2 包含 231,267 条内核记录。

| 核 | UOP 数 | `n_src > 4` | 最大不同 producer 数 | 扩展记录数 | `.deps` 字节 |
|---|---:|---:|---:|---:|---:|
| 0 | 2,100,218 | 350,036 | 3 | 0 | 48 |
| 1 | 300,195 | 28,574 | 3 | 0 | 48 |
| 2 | 241,267 | 13,836 | 5 | 83 | 1,708 |
| 3 | 297,693 | 28,336 | 3 | 0 | 48 |
| 合计 | 2,939,373 | 420,782 | 5 | 83 | **1,852** |

FST 主文件共 **188,120,160 bytes**，附件增加 **0.000984477%**。
这里是未压缩逻辑文件大小；当前文件系统为四个附件实际分配 16 KiB。
短前缀的扩展密度更高：各核前 2,000 宏指令合计 14,560 UOP，主文件 932,128 bytes，
27 条扩展共增加 732 bytes（0.07853%）。不能把某一前缀的百分比外推到所有负载。

去重不仅节省附件空间，还恢复了此前重复 producer 占满四槽后被挤掉的边：core2 的
**12,192 条记录补回 12,273 条不同 RAW 边**，其中只有 83 条记录最终需要第五槽；
其余恢复的边均可放回四个内联槽。这是功能依赖的修复，不是延迟补偿。

独立全流审计通过；旧依赖均包含在新依赖集合内。两次采集的 PC、地址、大小、flags、
opclass、源/目的计数、目的类别、ASID/静态表/页表附件、功能测量边界、CPI/native/kernel
汇总完全一致。`stats.txt` 排除宿主运行性能字段后 **16,489 项统计无差异**。
这证明本次采集元数据修改未扰动该次 gem5 执行，不等于 FastSim CPI 已对齐。

固定前缀经 C++ BinaryTraceSource/Writer 重写并独立校验，14,560 条主记录与新采集
逐字节一致，core2 的 27 条扩展保留。事件模型越过此前 core2 ordinal 7 的依赖完整性
门禁，但在 **ordinal 579** 拒绝 `0xffffffff810b5f82` 的四字节 store：物理地址
`0x20000000000020b0` 是 **Local APIC 2 的 EOI 寄存器（offset 0xb0）**。
身份依据是当前 gem5 的 `src/arch/x86/x86_traits.hh` 中 APIC 地址构造，以及
`src/arch/x86/interrupts.cc` 中 offset/EOI 映射。这是未支持的设备 MMIO，未改写为
RAM、未过滤指令、未追加固定等待。**该前缀未产出 CPI，也未完成事件守恒验收。**
**后续计划更正：无需因此新增 APIC/MMIO 设备组件。** 这条记录在原采集与新采集中
完全相同，而且位于 core2 正式测量边界（ordinal 229,364）之前的内核预热段。
既有 native-FS 配置已启用 `trace.allow_mmio_escape=true`；`src/simulator.cpp` 对
RAM 外访存保留 core/DTLB 时序、计入 `mmio_escape_accesses`，并跳过 cache/coherence/
DRAM。新 `causal_read` 既要求关闭此配置，又限制所有访存必须落在 RAM，因此暴露的
是既有边界策略尚未接入，而不是动态依赖扩展要求增加设备仿真。
此前把“遇到域外访存”直接推导成“下一步需要设备与中断状态模型”的计划撤回。
后续先对齐原输入、预热/ROI 和域外访存策略，保留 UOP、依赖与资源生命周期，并明确
现有近似；本轮未证明该访问导致 TeaLeaf CPI 误差，也未实现设备组件。
完整 native-FS 仍须面对既有其他未支持语义，当前默认模型不切换。

采集过程首次使用的旧隔离 checkout 在恢复时失败，产物已保存在
`tealeaf-collect-baseline-failed/` 并排除。随后将隔离目录中与当前采集源码不同的
11 个文件保存快照、同步到 `/data00/yinhaolang/gem5-fs` 基线，再应用本补丁构建。
`gem5-source-hashes.json` 记录同步身份；实际成对采集证据如下：

- `dependency-audit.json`：全流依赖、fan-in 和精确空间。
- `collection-pair-audit.json`：功能、原有附件、gem5 统计与恢复边数。
- `tealeaf-prefix/dependency-audit.json` / `input-gate.json`：固定前缀及 APIC 阻塞。
- `tests.log` / `gem5-build.log`：必要机制测试及采集器构建。

没有重跑完整 CPI 矩阵，也没有用 gem5 issue/response 时刻作为求解输入。
