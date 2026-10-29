# perf 退休事件统计修复与验证

10-15 [private-L2 低估定位](private-l2-pmu-underprediction-diagnosis-20261015.md)已确认：
下文保留的 native L2 上报只读桶遗漏了远端供给及写 demand miss；大幅低估不能
全部归因于缓存模型。本轮退休计数修复没有引入该旧问题，生产预测仍保持原值。

本文对应“开始实施并验证”。设计与历史取证见[语义审计](perf-pmu-semantic-audit-20261014.md)、[实施设计](superpowers/specs/2026-10-14-perf-retirement-repair-design.md)和[实施计划](superpowers/plans/2026-10-14-perf-retirement-repair.md)。本轮实施跨 2026-10-14/15，文件名保留启动日期。

## 修复范围

新增 `scope_metrics.perf_retirement`，将退休指令事件从按 UOP、cache line 和请求生命周期累计的旧 `perf_*` 中分离。计数使用已接受的功能指令前缀，跨 chunk 和预热保存宏指令状态，在 last-micro 退休时归并成员：

- 单条 split load/store 和含多个访存 UOP 的宏指令各计一次。
- atomic/RMW 独立具备 read 和 write 资格，分别计入 load/store。
- store 退休计数不依赖 prepare/publication 是否启用或完成。
- 重试、回滚和未接受的推演不重复累计；尾部未完成宏指令不补齐。
- 宏事件按退休边界的 CPL/线程归属分组，UOP 工作量按各成员的归属分组；分别输出 physical、user、kernel、workload_task、other_task。

原 timing、native tag-miss 和旧 `perf_*` 预测保留。新输出明确标注旧退休统计非权威、L2/LLC 为不完整请求代理。没有实现完整 wrong-path/code/prefetch 请求域，因此没有宣称修复所有硬件 perf 事件。

gem5 新增 oracle-only 的 `oracle/perf-retirement-coreN.json`，使用真实 committed DynInst 的 first/last-micro、load/store/atomic 资格和冻结的 owner；原生 cache 服务事实来自 Ruby/SLICC registry/response extension，以 context-id/inst-seq-num 关联。未读取 TaoTrace path-class 或软件 MSHR 预测作为原生真值。sidecar 不进入 FastSim 输入。

## L1 数据来源的适用边界

FastSim 的来源是模型估计；gem5 的来源是实际 native 服务事实。标量映射优先 any tag miss，其次有 miss parent 的 fill follower，其次 L1。普通 coalesced 也可能是热 L1 alias，不能一律算 FB_HIT；这一问题在独立审核中发现并用真实 CLI 回归修复。

FastSim 的 store-forward 暂按已取证的 SPR 标量估计处理；gem5 只有原生 forwarding 终止事实时不能证明 L1 residency，保留 unclassified。任何宏指令只要一个 load 成员的来源缺失，整个宏事件都归入 unclassified。两边任一方存在未分类 load 或 scope crossing，验证器不计算该范围的 L1 source 误差，避免对两个不同的分类子集评分。

这些分类是服务诊断代理。硬件事件未被证明是一个对所有 ISA 都成立的互斥分区；REP、gather、mask、fault/restart、prefetch、MMIO/port-I/O 和混合来源的硬件仲裁仍未认证。新输出保留 `perf_equivalence=unverified`。

首轮真实采集还暴露 x86 中断 ROM 的边界差异：ROM generator 没有 `IsFirstMicroop`，ERET 才标 last。采集器因此增加明确外部入口符号的识别，并分别记录 ROM pseudo-macro 与未知起点诊断。仅在原生入口证据完整时承认功能宏起点；任意 ROM 中间地址不算起点，入口前仍有开放宏则记录断裂并拒绝人口认证。未知起点的测量期开放尾部也会拒绝认证。这些 ROM 序列仍保留在功能流人口中，其访存来源标为 unsupported；不能把包含 ROM 的功能宏一致性写成真实硬件退休指令等价。

## 维护验证入口

[事件字典](../configs/perf-retirement-event-dictionary-v1.json)绑定本机 Sapphire Rapids family 6/model 143、Intel SPR V1.40 事件表。五个新事件全部为 `proxy`，与原生 tag-miss 字典分开。

[维护验证器](../tools/run_native_kernel_fastsim_validation.py)新增 `--perf-retirement`：要求新命名空间、精确的逐核 sidecar、共同起止、任务集合、功能流边界和独立退休人口。缺少任何必需证据即失败，不回退旧字段。[独立审计器](../tools/audit_perf_retirement.py)扫描完整功能前史，核对测量期宏指令、功能 UOP、load/store 和预热跨界/未完成尾部，重复使用已通过报告前仍重新检查当前 FST。

raw totals 与 per-1K-macro 分别报告；每个 scope/event 分别聚合 MAPE/P50/P90/P99/WAPE/bias。0/0 贡献 APE=0，非零/0 不进入有限 APE 分布但保留绝对误差。native tag-miss 仍通过原维护 native auditor 选源，另行运行硬件身份门禁。

## 已完成的机制与真机验证

FastSim 七组最小输入全部满足字面预期，且修复前后既有目标计数逐值相同：

| 输入 | 旧 load/store | 新 load/store | 预期 |
|---|---:|---:|---:|
| 单 load | 1 / 0 | 1 / 0 | 1 / 0 |
| 单 split load | 2 / 0 | 1 / 0 | 1 / 0 |
| 一个宏指令含两个 load UOP | 2 / 0 | 1 / 0 | 1 / 0 |
| 单 store | 0 / 0 | 0 / 1 | 0 / 1 |
| 单 split store | 0 / 2 | 0 / 1 | 0 / 1 |
| 单 atomic | 0 / 1 | 1 / 1 | 1 / 1 |
| store 后接 64 ALU | 0 / 0 | 0 / 1 | 0 / 1 |

原始结果：[seven-probes.json](../tmp/perf-pmu-repair-20261014/task1/seven-probes.json)。另外覆盖跨 chunk、warmup member、部分 MMIO 来源缺失、热 L1 alias、冷 fill follower、真实重试接受前缀和线程切换。

SPR 真机重新执行 11 个内核 × 2 个事件组 × 3 次，共 66 次；CPU0 固定、当前线程、user-only、不继承，全部 time_running=time_enabled，无 multiplex。每轮约为：split load=1 load；CMPS=1 load；两条 MOV=2 loads；locked RMW=1 load+1 store；forward loop 主要 L1_HIT，FB_HIT≈0。软件 prefetch 在此机此内核也会计入退休 load/L1 hit，因此仍需目标特定的 ISA 规则。冷双 load 的 L1_MISS≈1、FB_HIT≈1，不能据此声称两者普遍包含或分离。

原始数据：[hardware-unit-runs.json](../tmp/perf-pmu-repair-20261014/hardware/hardware-unit-runs.json)、[汇总](../tmp/perf-pmu-repair-20261014/hardware/hardware-unit-summary.json)、[身份](../tmp/perf-pmu-repair-20261014/hardware/hardware-identity.json)。这些结果验证选定指令的计数单位，不构成完整硬件 PMU 精度认证。

## 四核新源及误差

主验证案例是 `diagnostic/fs-probe-rom-gdb`，4 核 `thread-state-fs-probe`，参数 `1000 2000`，首核 measured user-UOP target=300000，完整保留共同窗口。它在 GDB 下采集，同一冻结 gem5 二进制，RubyPort 失败断点从未命中，inferior 正常退出；不是普通启动采集成功率的证明。所有采集尝试都保留，见下表。

| 尝试 | 结果 | 进入误差统计 |
|---|---|---|
| 首轮 `fs-probe-perf-retirement` | native 来源有效；新退休门禁发现6/5/5个缺失 first 标记的合法 ROM 序列，拒绝新退休认证 | 不进入本节主汇总；旧 native 诊断另存 |
| `retry/source/...`、`retry2/source/...` | Ruby functional-read fatal，地址0xbc962ad8；未完成测量 | 否，失败现场与停止原因保留 |
| `diagnostic/fs-probe-rom-gdb` | 正常完成，独立逐核人口、共同窗口、身份及维护验证全部通过 | 是，本节唯一计分案例 |
| `retry3/source/...` | 不带调试器正常结束；core2 FST宏86169、native宏86168，严格门禁拒绝 | 否，不填补宏或改分母 |

最后一案已确认上述人口不一致，且[最后一个功能记录是 syscall marker](../tmp/perf-pmu-repair-20261014/common-cut-population-observation.json)；共同终点截断 collapsed syscall 是原生 last-micro 与 FST first-marker 口径不一致的已知风险，尚未获得该案精确原生尾部证明，不能直接认证通过。Ruby-read 故障原因尚未确认，也未宣称已修复。主案例是有界功能/语义诊断，不是 TeaLeaf 全量、全配置或生产采集稳定性认证。

主源 FST **154,475,552 bytes**；共同测量期 **455,784 个功能宏、851,524 个功能 UOP、689,716 个 user UOP**。保留2个未完成宏的6个成员，没有补齐；2个宏跨预热。17个完整 ROM pseudo-macro 均来自明确入口，无未知前缀、scope crossing、boundary discontinuity。scope、逐核和独立 FST 人口核对通过。

[身份检查](../tmp/perf-pmu-repair-20261014/identity-primary.json) **1/1 valid**，[逐案例维护验证](../tmp/perf-pmu-repair-20261014/primary-validation-final/cases/fs-probe-rom-gdb/validation.json) **1 passed、0 failed、0 pending**。这是来源/人口门禁，不是误差阈值或硬件认证。主案例与历史四核采集的 CPU/Ruby/DRAM/时钟配置逐字段相同；FastSim 核心/TSO参数匹配，七项 DRAM 约束仍不匹配，详见[本次参数复核](../tmp/perf-pmu-repair-20261014/parameter-audit-primary.json)。

### 新退休事件：功能计数和 raw/rate 误差

下表 load/store 为 `gem5 / FastSim`，rate 是每1000个本 scope 功能宏；两边分母分别核对后相同。

| scope | 功能宏 | load | store | loads/1K | stores/1K |
|---|---:|---:|---:|---:|---:|
| physical | 455,784 | 50,885 / 50,885 | 15,247 / 15,247 | 111.642796 | 33.452249 |
| user | 382,670 | 28,701 / 28,701 | 803 / 803 | 75.001960 | 2.098414 |
| kernel | 73,114 | 22,184 / 22,184 | 14,444 / 14,444 | 303.416582 | 197.554504 |
| workload_task | 427,054 | 41,662 / 41,662 | 9,142 / 9,142 | 97.556749 | 21.407129 |
| other_task | 28,730 | 9,223 / 9,223 | 6,105 / 6,105 | 321.023321 | 212.495649 |

每个 scope 的两个 raw 和两个 rate 比较均为：有限APE **1/1**，MAPE/P50/P90/P99/WAPE/有符号bias均 **0%**。这是功能宏 read/write 成员归并正确的结果，**不是硬件 perf 精度0%**。ROM、MMIO和特殊ISA资格仍未认证。原有 physical 旧字段在本源仍为51,042 loads /8,040 stores；它们是旧 UOP/line/request代理，不再作为退休真值。

### L1 source 覆盖

| scope | gem5 unclassified loads | FastSim unclassified loads | L1 HIT/MISS/FB误差 |
|---|---:|---:|---|
| physical | 3,190 | 20 | unavailable，有限APE 0/1 |
| user | 356 | 0 | unavailable，有限APE 0/1 |
| kernel | 2,834 | 20 | unavailable，有限APE 0/1 |
| workload_task | 2,046 | 15 | unavailable，有限APE 0/1 |
| other_task | 1,144 | 5 | unavailable，有限APE 0/1 |

原生 physical 未分类3,190个 load 宏，其中3,058个带 store-forward 事实；这些事实不足以证明 native L1 residency。模型与原生的 forwarding 预测也未对齐。来源门禁保留这些缺口，不对不同分类子集计算缓存误差。L2/LLC 的完整 speculative/code/prefetch perf 请求事件仍未实现，不能用下列 tag-miss 数字代替。

### CPI、elapsed 与既有 native PMU

CPI 使用 `task-oncpu-switch-entry-v1` 的 workload-task active cycles /427,054个本任务功能宏。下表均为主源同一测量窗口；相对误差保留方向。

| 指标 | gem5 | FastSim | 相对误差 | CPI绝对误差（cycles/macro） |
|---|---:|---:|---:|---:|
| active CPI | 0.819819039 | 0.864942138 | +5.5040% | 0.045123099 |
| elapsed（µs） | 42.6536370 | 34.4052270 | -19.3381% | — |

单案例 CPI MAE = **0.045123099 cycles/macro**，CPI MAPE = **5.5040%**。这组值与首轮源不同，不能把跨源差异当成退休统计修复带来的精度提升；同一主源修复前后二进制的 CPI/elapsed逐值相同。

以下为 workload-task 的原生 cache-line/branch 诊断，修复前后预测不变；全部非严格硬件 PMU：

| counter | gem5 native | FastSim | 有符号误差 |
|---|---:|---:|---:|
| branch_misses | 775 | 792 | +2.1935% |
| l1d_tag_misses | 429 | 313 | -27.0396% |
| private_l2_tag_misses | 287 | 44 | -84.6690% |
| llc_tag_misses | 21 | 25 | +19.0476% |

每个事件独立的 finite-APE、MAPE/P50/P90/P99/WAPE/bias，以及 raw/rate完整退休统计均见[维护验证汇总](../tmp/perf-pmu-repair-20261014/primary-validation-final/summary.md)、[退休事件汇总](../tmp/perf-pmu-repair-20261014/primary-validation-final/perf-retirement-summary.md)、[完整JSON](../tmp/perf-pmu-repair-20261014/primary-validation-final/summary.json)。

复现主验证：

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/perf-pmu-repair-20261014/diagnostic \
  --stats-dir tmp/perf-pmu-repair-20261014/stats-primary-quiet \
  --output-dir tmp/perf-pmu-repair-20261014/primary-validation-final \
  --expected-cases 1 --jobs 1 --perf-retirement
```

## 吞吐和目标行为守恒

主源冻结 before/after 二进制、相同配置include链和FST，CPU0–7固定、串行交替各5次；测量期间本任务没有并行编译、采集或离线审计。共享宿主不独占、单次测量短，因此只报告本案例的开销诊断，不推广全负载吞吐。

| binary | mean M user-UOP/s | P50 | P90 | P99 | min | 整体wall P50(s) |
|---|---:|---:|---:|---:|---:|---:|
| 修复前 | 4.083986 | 4.093259 | 4.121520 | 4.126694 | 4.030582 | 0.615515 |
| 修复后 | 3.868479 | 3.869607 | 3.904024 | 3.918140 | 3.828486 | 0.644759 |

P50变化 **-5.4639%**。当前此配置此案例约 **3.870 M measured user-UOP/s**。完整10次运行的既有目标时序/native/legacy计数逐值相同；仅新增退休域和宿主运行诊断有差异。

[原始运行/命令/配置链/SHA](../tmp/perf-pmu-repair-20261014/performance-primary-quiet/summary.json)。首次主源测量曾与离线审计重叠，另存于 `performance-primary/`，未进入此表；重测只用于消除本任务自身干扰，没有模型或配置变更。

FastSim before SHA=`7655662232c388232617ed2ea35f25a191071eaf82562f5fe6ddd332c272f05b`；after SHA=`f957aa38e4ed9c9320a339a9587d76f48a93fc7ab2b12c33860fdf44948c6777`；最终 gem5 SHA=`5fc37702d9dfbd41660c41e472204323fdcab58cd3c6c25312753aecb99c9fff`。

所有新采集单次FST均低于300MB；包括失败现场和诊断源在内，总FST **19,184,545,640 bytes（19.185 GB）**，低于20,000,000,000 bytes。未复制TeaLeaf大FST。

## 测试、审核与剩余限制

FastSim 默认 `cmake --build build -- -j16`、直接 `fastsim_tests` 和独立 retirement tests 已通过。gem5 按当前 affinity 192 核的 80%，即 `-j153` 编译，显式使用 GCC11 和 Python3.11。完整 Python **344项：336 passed、5 skipped、3个既有环境错误**（缺 fastsim_py 和两个外部 TCSim wrapper），没有断言失败。最终构建/测试/采集证据均在 `tmp/perf-pmu-repair-20261014/`。

三次任务审核已实际发现并修复：热 L1 alias 被误记 FB、原生逐 owner 输出失效门禁不完整、0/0 APE 处理、oracle 单位/来源门禁和尾部/预热覆盖核对。[最终整体审核](../tmp/perf-pmu-repair-20261014/reviews/final-review.md)已通过，无剩余阻断项；独立复核了当前主源人口及全部10次配对运行的既有目标统计守恒。

后续实测及复核还补齐了 ROM 入口、未知起点开放尾部、逐核 optional coverage/native-UOP 诊断的总和，以及无测量记录时预热未闭合尾部的诊断保留。全部修复都保持未完成宏不产生退休事件。

本轮不调任何时延常数。沿用配置中已有的 DRAM 部分对齐限制：tRAS/tRTP/tRRD 等参数仍未完整启用，不能把身份门禁或 native validator passed 写成完整微架构对齐。CPI/elapsed 的既有时序误差不会因统计账本修复自动消失。完整推测请求事件、特殊 ISA 的硬件资格以及 forwarding 的真实硬件来源归属仍需独立补齐。
