# perf-like PMU 语义与 micro/macro 计数审计（2026-10-14）

10-15 用户进一步固定了十个 `perf_*` 字段的三端同口径目标，重点为 L1 load
miss、L2 demand miss 和 LLC core miss。后续
[三端统一设计](superpowers/specs/2026-10-15-perf-cache-threeway-alignment-design.md)
绑定官方事件并记录当前缺口；设计尚待审阅，不能据此升级现有字段的认证状态。

后续已开始实施，当前范围、验证和未认证项见[退休事件修复报告](perf-retirement-repair-results-20261014.md)。下文保留修复前取证，不代表新实现仍有全部旧问题。

本次目标是对齐目标机器的 Linux perf 事件语义，而不是只让 FastSim 与 gem5 的
同名字段接近。结论：已有 `perf_*` 层确实实现过，但尚未完成真机事件等价认证；
当前同时存在退休指令/UOP/cache-line 单位混用、转发分类错误、buffer store 漏计、
请求事件人口不完整和 task 范围未接入等缺口。最近的 task PMU 修复只认证了新范围
的原生缓存诊断来源与守恒，没有修复这些 `perf_*` 问题。

本次完成文档与代码审计、7 个 FastSim 最小输入、现有四核 FST 人口扫描，以及
Xeon 8457C 上 11 种内核 × 2 组事件 × 3 次重复的真机计数诊断。未修改模型代码、
时序参数或历史预测；没有新采集 gem5 FST，也没有重新计算完整负载精度。

## 1. 文档顺序、有效范围与冲突

日期优先用于确认当前状态；较新的范围修复不能代替较早但尚未完成的事件语义目标。
硬件事件以目标 CPU 的官方定义与该机验证为准，不能由任何日期的内部文档重新定义。

| 日期 / 文档 | 已明确的事实 | 本次判定 |
|---|---|---|
| 10-14 [task PMU 结果](task-pmu-repair-results-20261014.md) | 完整物理流、新 workload-task owner、原生 Ruby/SLICC 来源与人口守恒；`perf_equivalence=unverified` | 是范围/来源修复，不是 `perf_*` 或真机事件认证；DRAM 参数仍部分未对齐 |
| 10-13 [scheduler 结果](recorded-scheduler-results-20261013.md)、[双周期设计](thread-state-active-elapsed-design-20261013.md) | task-onCPU、active/elapsed、switch-entry 边界 | 不等于 Linux perf 实际切换计数器的边界；不能把同一 owner 归属政策套给所有请求事件 |
| 10-12 [TeaLeaf 结果](tealeaf-4c-sync-trace-accuracy-20261012.md) | 明确是原生缓存语义诊断，proxy/diagnostic | 没有新增真机 `perf_*` 对照 |
| 10-07 [idle cycle 口径](sph-exa-idle-cycles-caliber-20261007.md) | active 与 elapsed/idle-inclusive 分开，宏指令分母 | 修复周期口径，不解决缓存事件单位；自旋仍是实际执行，不能一概当作 perf 不计数 |
| 09-30 [perf 指标目录](perf-aligned-pmu-catalog-20260930.md) | 十个 `perf_*` 字段、每千条指令、Fix A/C、32 个可计分 case | 保留其目标与历史数值；WAPE 不改名 MAPE；“authoritative”不构成真机认证 |
| 09-30 追加的 [deferred install 记录](l1d-deferred-install-alignment-20260928.md) | 文档名虽是 09-28，末尾已有 Phase 1 Step 1 的 24-case 结果，parent 默认关闭 | 同一文件必须读最新追加；20-cycle/密度门是历史近似，不是硬件 PMU 定义 |
| 09-28 [对齐层设计](perf-pmu-alignment-layer-20260928.md) | 退休事件与 SLICC 诊断分层；真机四例 pilot 仍 pending | 方向保留；UOP 单位、转发分类、L2 writeback 编码和全局分区约束需纠正 |
| 09-28 [FB_HIT 修复](perf-fb-hit-alignment-20260928.md)、[mirror](perf-mshr-hit-mirror-alignment-20260928.md) | 补漏与报告层 shadow 已实现，历史 gem5 对照曾改善 | gem5 数字改善不能证明 store forwarding 等于 FB_HIT；本次真机反例已否定这种统一映射 |
| 09-16 [原生来源修复](private-l2-pmu-source-repair-20260916.md)、08-19 原生审计 | 禁止把 path-class cache 字段当原生 Ruby 真值 | 继续有效；也不能反过来把原生 SLICC tag miss 当作硬件退休 load miss |

当前实现与文档另有断层：09-28 设计拟输出 `native-summary:perf_pmu_by_scope` 并让
维护验证器切换来源；实际新四核文件没有该键，`perf_*` 仍在
`kernel-events-core*.json:pmu_user/pmu_user_plus_kernel`，维护验证器只计分 branch、
L1D/private-L2/LLC tag miss。事件字典也未登记这十个 `perf_*` 的完整硬件合同。
因此不能说维护的 native validator 已认证该层。

用户提供的“32-case per-1K MAPE → 10-case raw-total MAPE”原表仍未在当前工作区定位；
09-30 文档引用的临时原始目录已不在。样本和分母同时改变，箭头不能证明修复收益。
本次不补造原表，也不混算那组误差。

## 2. 硬件基准：分清三种单位

参考平台沿用旧设计的 Sapphire Rapids；本次现场确认机器为 Xeon Platinum 8457C，
family 6 / model 143 / stepping 8。官方事件表版本 1.40，发布日期 2026-09-09；
URL、整份源的 SHA256 和所用编码元数据保存于审计产物。

| 层次 | 单位与对象 | 不能采用的替代 |
|---|---|---|
| `instructions` / `INST_RETIRED.ANY` | 架构退休 x86 指令，作为 per-1K-inst 分母 | gem5/FastSim retired UOP 数、所有核心固定目标之和 |
| `MEM_INST_RETIRED.*`、`MEM_LOAD_RETIRED.*` | 按目标事件定义的退休内存指令及其数据来源属性 | 每个 gem5 memory UOP 或每条缓存行各加一 |
| `L2_RQSTS.*`、`LONGEST_LAT_CACHE.*` | 缓存请求；其请求类型、推测性和合并规则由各事件规定 | 只把退休数据 load/store 的最终 hit level 展开成请求总数 |

SPR 的 load 事件描述使用指令及“至少一个 UOP 满足条件”的表述；软件预取、lock
也有专门的包含规则，不能通用排除。`L2_RQSTS.REFERENCES` 包含 hit 与 true miss，
排除并入在途 L2 miss 的 miss；LLC reference/miss 还覆盖 code、RFO、推测请求和部分
硬件预取类别。[Intel 官方事件定义](https://perfmon-events.intel.com/platforms/sapphirerapids/core-events/core/)

旧文档 `L2_TRANS.L2_WB=0xF0/0x40` 不适用于此 SPR 表；这里是 **0x23/0x40**。
原始 `perf stat` 编码必须绑定 CPU，不能从其他微架构沿用。
[Intel SPR 事件 JSON](https://raw.githubusercontent.com/intel/perfmon/main/SPR/events/sapphirerapids_core.json)

三种单位不能互相换算成同一个计数。跨行 load 可以产生两个 line 请求而只退休一条
load 指令；`cmpsq` 可以有两个读 UOP，却只计一条 load 指令；lock RMW 同时具有
load 和 store 事件资格。REP、gather/scatter、掩码、故障/重启等特殊指令仍须单独
确认，不能把“每个 macro 一次”升级为适用于一切 x86 指令的无条件规则。

## 3. 真机最小验证

源码与原始记录均在 `tmp/perf-semantic-audit-20261014/`：

- `perf_units.c`、`run_hardware.py`：真实 x86 汇编内核和重复运行器。
- `hardware-unit-runs.json`、`hardware-unit-summary.json`、`hardware-identity.json`。
- 同一冻结二进制、CPU0、当前线程、不继承子任务、仅用户态、排除 hypervisor。
- 两组各三个可编程事件加 `instructions`，避免所有事件争用 SPR 的 counter0..3。
  66/66 次 `time_running == time_enabled > 0`，没有 multiplex scaling。
- 热例每次 1,000,000 轮；两个 cold 例每次 100,000 轮；下表是三次中位数/轮的近似。
  保留 enable/dispatch/disable 的少量用户指令开销，未做不成立的精确基线相减。

| 内核 | ALL_LOADS/轮 | ALL_STORES/轮 | L1_HIT/轮 | L1_MISS/轮 | FB_HIT/轮 | 说明 |
|---|---:|---:|---:|---:|---:|---|
| aligned `movq` load | 1.000026 | 0.000009 | 1.000020 | 0.000003 | 0.000003 | 一条 load 指令 |
| 跨 64B 行的 `movq` load | 1.000013 | 0.000007 | 1.000010 | 0.000003 | 0.000002 | 两行不等于两条退休 load |
| 两条独立 `movq` load | 2.000031 | 0.000010 | 2.000018 | 0.000004 | 0.000000 | 对照：确实有两条指令 |
| 一条 `cmpsq` | 1.000039 | 0.000011 | 1.000036 | 0.000003 | 0.000001 | 双读不等于两条退休 load |
| `movq` store | 0.000043 | 1.000012 | 0.000041 | 0.000003 | 0.000000 | store 应在退休层独立计数 |
| 同址 store→load 热循环 | 1.000049 | 1.000013 | 1.000047 | 0.000004 | 0.000000 | 不支持把 store forwarding 一律映射为 FB_HIT |
| `lock incq` | 1.000061 | 1.000014 | 1.000051 | 0.000003 | 0.000000 | 同时计 load 和 store |
| `prefetcht0` | 1.000061 | 0.000015 | 1.000058 | 0.000002 | 0.000000 | 软件预取不能从上述退休事件一概剔除 |
| flush+两个同行 load | 2.000950 | 0.000160 | 0.001280 | 0.998660 | 1.000290 | primary miss 与 FB follower 在此例中分开 |
| 只 flush 后半行的 split load | 1.000950 | 0.000160 | 0.000850 | 1.000060 | 0.000120 | 仍是约一条退休 load |

这里没有通过 PEBS 逐条证明每次 store→load 都实际转发；结论严格限于：旧代码的
“这类负载必须计 FB_HIT”缺乏依据，且该最小内核在此机上的测量主要属于 L1_HIT。
同样，本次 cold 结果不支持声称 L1_MISS 必然包含 FB_HIT；此前仅从定义文字推断
包含关系是不充分的。旧 `hits+miss+fb==loads` 可以保留为历史软件分桶的内部守恒，
不能直接充当所有指令、所有 CPU 的硬件验收恒等式。

Linux 的 task/CPU 选择、继承、privilege 排除与 multiplex 时间是独立的采集条件，
须随原始计数保存；用户态验证不能认证 `:k` 或任务切换边界。
[Linux perf_event_open 文档](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)

## 4. gem5 采集实现：当前仍是 UOP/路径代理

主要证据：`gem5-fs/src/cpu/o3/probe/tao_trace.cc:3376–3559`、
`6454–6535`，以及 `.hh:859`。overlay 与 gem5-fs 整个文件已不相同，但本次
检查 `accountCplDataPmu`、`onDataAccessComplete` 函数相同，缺口不是只存在于旧文档。

1. `accountCplCommit` 对每个 data-touching DynInst 调用计数；宏分母使用 last-micro
   判定，`perf_retired_loads/stores` 却按 memory UOP 加一，没有独立 macro 聚合。
   `CMPS_M_M` 的 gem5 ISA 定义确实有两个 `ld`（`general_purpose/string/compare_strings.py:44`），
   因而其直接逐 UOP 累计与真机 `cmpsq` 诊断不一致。
2. `is_store_commit = isStore || isLockedAtomicMicro`；load 用 `isLoad && !is_store`，
   `PerfMemClass` 还明确把二者规定为互斥。lock RMW 的读写资格必须分别按架构语义
   聚合，不能把 store 资格当作取消 load 资格的理由，也不能按锁微操作数重复计 store。
3. `perf_retired_*` 每 UOP 加一，而 L2/LLC 用 `line_requests`；gem5 本身分开了这两层，
   但退休层的单位依然未对齐。split 响应的某个最终路径也不足以描述各 fragment 的状态。
4. 所有 L2/LLC `perf_*` 仍在 `accountCplDataPmu` 根据 `path_class` 推导，覆盖的是
   已退休数据访存；没有接成官方事件要求的完整 request stream。I-side、硬件预取和
   wrong-path 请求不能从该层自然恢复。也不能因两侧默认没建模预取就声称真机贡献为0。
5. `mshr_same_line_outstanding` 在 **DataAccessComplete 回调**中读取 TaoTrace 自有的
   `MshrTracker`，随后 insert，在 `accumulateMicro` 的 commit 中 retire。这个 tracker
   还有 capacity/seq-window 淘汰（`taogen/shared/lru_banked.hh:366`），不是实际 Ruby L1
   admission→data-ready 的 MSHR 生命周期；FB 分类不能据此称为硬件事实。
6. 四个 `PerfMemClass` 构造点的 `l2_writeback` 均是 false，gem5 该字段缺独立 writeback
   事件接线。输出0只能表示当前实现没采到，不能认证真实 writeback 为0。
7. 旧 CPL 人口在 `!cpl_initialized_ && !decoded_user` 时返回，漏过首次用户提交前的内核
   前缀。新 task native 收集从共同起点开始，但并未为 `perf_*` 增加同样的 task ledger。

`path_class` 字段不能作为 native Ruby cache 真值，既有禁令保持。
退休指令事件可以接来自真实服务的观测属性，但须独立定义事件合同、完整覆盖和
fallback/unavailable 规则；不能因 `perf_pmu_schema=perf-aligned-v1` 就免除这些门控。

## 5. FastSim 实现与可复现的单位反例

证据链：`src/simulator.cpp:9833–9950` 把一条 memory UOP 展开为各 line 的
`ChunkMemoryEvent`；`22773–22853`、`23336–23438` 的退休 perf 加法发生在这些
line event 的 replay/classifier 上。`source_sequence/uop_index` 标识 UOP，未提供
跨 UOP、跨 chunk 的独立架构内存指令聚合层。

`probe_units.py` 使用当前 binary（SHA256
`7655662232c388232617ed2ea35f25a191071eaf82562f5fe6ddd332c272f05b`），继承本次
四核的冻结配置、缩为单核 user；关闭 synthetic kernel，并为最小 JSON 输入关闭
virtual-page-token 必需门。数据物理地址有效。这是软件计数单位诊断，不是假称
这些 JSON 记录经过真实机器解码，也不是新的 workload accuracy scorer。

| 最小输入 | macro | memory UOP | line 请求 | FastSim retired load / store | 判定 |
|---|---:|---:|---:|---:|---|
| 一条 aligned load | 1 | 1 | 1 | 1 / 0 | 简单路径一致 |
| 一条跨行 load | 1 | 1 | 2 | **2 / 0** | 退休 load 被 line 放大 |
| 同一 macro 两个 load UOP | 1 | 2 | 2 | **2 / 0** | 缺少 macro 聚合；上面的真实 cmpsq 可作为硬件对照 |
| 一条 aligned buffer store | 1 | 1 | 1 | **0 / 0** | 已退休 store 漏计 |
| 一条跨行 store | 1 | 1 | 2 | **0 / 2** | 同时存在路径依赖与 line 放大 |
| 一条 atomic record | 1 | 1 | 1 | **0 / 1** | generic atomic 分类只计 store；不是实际 lock 指令完整解码认证 |
| 一条 store 后加64条 ALU | 65 | 1 | 1 | **0 / 0** | 漏计不是仅靠追加非访存尾部就能消除 |

buffer store 根因已定位：prepare 分类用 `!prepare` 跳过 retired-store bump；
`apply_buffered_tso_publications` 只在 `buffer_tso_prepare_lifecycle` 的部分分支补计，
关闭该开关时直接 `publish_buffered_store` 而没有退休计数。该开关在当前冻结配置为
false；不能为修正统计而顺便开启改变模型行为的生命周期选项。退休 store 应由独立
架构提交账本计数，不依赖 prepare/publish 是否走了某条 cache replay 路径。

其他明确缺口：

- `bump_perf_forwarded_load` 和 private-hit forwarded 分支统一加 FB_HIT；真机热循环
  不支持这种映射。必须保留 store forwarding 与在途 fill 合并的不同来源。
- `bump_perf_private_hit_classifier` 不走 shared-hop 的 FB mirror；同类 load 在不同
  replay 路径上的分类条件也不一致。mirror 不能替代统一的事件事实采集。
- shared-hop `perf_*` 被 `!event.instruction_fetch` 包围，因此不能冒充包含 code 的
  L2/LLC 硬件事件；L2 request merge 与 LLC follower 也不是同一层的合并规则。
- deferred-install 的报告层可凭 shadow 增加 L2/LLC 计数而没有真实下发该请求。
  可以作为有名称的模型估计，但不能称已观测的物理事件。当前默认 parent 关闭。
- FST 的 load/store/atomic 和 last-micro 位不足以证明 software-prefetch、REP、
  gather/mask 等 ISA 特殊事件规则已覆盖；缺分类时应明确 unavailable/proxy。

## 6. 现有四核真实 FST 的人口核查

`scan_population.py` 只扫描 measurement 记录并按 last-micro 归组，不读取 oracle
cache labels，不生成新的误差报告。文件尾的 syscall metadata 按 FST v7 布局排除。
完成宏数与 memory UOP 数分别断言等于保存的 FastSim 输出。

| 物理范围统计 | 数量 |
|---|---:|
| measurement records / 完成 macro | 843932 / 454188 |
| memory UOP / line 请求 | 70175 / 70175 |
| load UOP / 含 load 的完成 macro | 53641 / 53475 |
| store UOP / 含 store 的完成 macro | 16534 / 15302 |
| 多 memory UOP 的 macro | 1591 |
| 多 load UOP / 多 store UOP 的 macro | 33 / 58 |
| 同时含 load/store UOP 的 macro | 1532 |
| 跨行 memory UOP | 0 |

以上是物理流的功能形状，不等于真机 load/store PMU truth；含双读写、REP 等仍须
依据 ISA 分类。这个实际输入已存在多 memory-UOP macro，不能认为该缺口只在合成例
出现；它没有跨行请求，不能用它验证跨行计数正确。

保存输出仍有旧 `scope_metrics.pmu.perf_*`（load=53622、store=6563），但
`scope_metrics.task_pmu.pmu` 八个字段中没有这些指标。两者不能拼接：物理 perf 计数
不能除以 task macro 分母，旧 CPL 的 gem5 perf 计数也不能拿来当新 task 的参考。
本次不对不匹配范围的这组数字计算 MAPE。

## 7. 应采用的采集与验证合同

1. **逐事件字典**：绑定 CPU family/model/stepping、官方事件版本、实际 perf 编码、
   privilege、task/CPU scope、计数单位、trigger、speculation/merge/preload 规则、
   测量窗口、缺失规则。不要用全局 committed-only/cache-line 规则定义所有 PMU。
2. **退休事件与请求事件分账**：退休事件汇总同一架构指令的相关 UOP/fragment，按该
   ISA 类别提交资格；读写资格可同时为真。请求事件在真实 admission/lookup/writeback
   边界累计，不在 retirement 时凭最终 hit level 伪造。native SLICC 继续单列诊断。
3. **范围分开验证**：先同一线程 `:u`，再完整 task `:u/:k`，最后多核/切换。
   task-owner、privilege 与事件发生时刻分别记录。退休归属与异步请求发生时归属不能
   共用未经证明的冻结规则；switch-entry 代理边界保持 perf-unverified。
4. **统计同时保存**：每侧 raw total、其自身同范围的 macro denominator、
   `rate=1000*count/retired_macro_instructions`。原始人口一致时 raw 与 rate 误差应一致；
   不一致时先解释窗口/人口，归一化不是单位或 ROI 不匹配的补救。MAPE/P50/P90/P99、
   WAPE/bias 分事件、分范围，固定相同 case 集后才做修复前后比较。
5. **三方验证分层**：事件单位微基准 → gem5 原始服务事实覆盖 → FastSim 各接受路径、
   rollback/重复 replay 守恒 → 真机/gem5/FastSim 同 ROI 对照 → 固定配置与未见配置
   的响应验证。不能把“两个模拟器同样计错”当成硬件验证通过。

微基准后续必须覆盖：split load/store、cmps/movs、lock/xchg/cmpxchg 成功/失败、
store forwarding、primary/follower、software/hardware prefetch、wrong-path、
REP/gather/mask、TLB/page fault、store 在窗口内退休而在窗口后发布、跨 chunk macro、
其他任务/IRQ/切换与核迁移。第一阶段以本次已复现的 split/cmps/store/forward/atomic
反例作为修复门槛；特殊指令未确认前不得扩张 certified 范围。

两阶段框架可保留：在接受的 producer prefix 上维护轻量架构指令账本，并把服务事实
归并回该账本；仅接受的事件提交计数，回滚时同快照恢复，跨 chunk 保留未闭合 macro。
per-line replay 负责资源和请求事件，不负责退休 load/store 加法。functional trace 应
携带 ISA/架构边界等事实；gem5 hit/miss/MSHR oracle 仍不得作为 FastSim 输入。
QEMU/DynamoRIO adapter 也应遵循这个功能合同，而非依赖 gem5 专用 UOP 编号。
只有 committed trace 时，完整 wrong-path/hardware-prefetch request PMU 的能力边界
必须明确：新增独立模型或保持 proxy，不能只通过重命名达到 strict。

## 8. 本次验证与下一步边界

本次证据目录为 `tmp/perf-semantic-audit-20261014/`；FastSim probes 7/7 正常退出，
真机66/66次运行完成且无 multiplex，真实四核人口断言通过。只生成小型 JSON/日志
及临时探针，没有复制或新增 FST，不改变20GB FST预算。

尚未完成：`perf_*` 模型修复、task perf ledger、完整 native request perf ledger、
正式事件字典/维护验证器接入、三方同 ROI 精度和吞吐评估。不能把本审计标为 PMU
修复完成或严格硬件认证，也不能推算修复后的 MAPE/吞吐量。

修复次序应先固定事件合同与单位、修正已复现的退休/转发/atomic 缺口，再补齐
task 与 native 服务事实及验证器，最后才重跑负载矩阵。DRAM 参数对齐与事件口径
是两个独立门，二者都通过后才能讨论可归因的模型精度。
