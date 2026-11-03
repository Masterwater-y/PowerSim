# Marian C4 private-L2 与 writeback 计数审计（2026-10-30，D6）

## 结论

现有证据**不支持把 private-L2 的约 +112% 误差直接归因于“每次 writeback 又记了一次 demand miss”**。`424302 - 200130 = 224172` 来自 dynamic-translation 新采集窗口的 **workload-task data-demand tag miss**；`224247` 是其中 **P1 的 physical `totals.l2_writebacks`**，表示 L2 安装新行时驱逐的 dirty L2 victim 数。两者不是同一个请求人口，也不是同一个事件方向。

代码确实存在一条 L1 dirty victim 在 L2 不驻留时调用 `l2_.access()` 的 fallback，它会增加通用 `l2.misses`。但当前专用 `private_l2_tag_misses` 不直接读取该通用 counter，而根据原 demand 的 private lookup 结果单独计一次；writeback fallback 本身没有对专用 counter 的增量。不能据此把 `l2_writebacks` 从报告中减掉。

P2 保持相同的 task excess `224172`，但 `totals.l2_writebacks` 变成 `224091`。P1 的“只差75”和 P2 的“差81”都只是数值接近；没有逐请求身份、起因、时点和归属的配对证据。此次检查既有输出和源码、新增本证据文档，并修正事件字典的来源描述；没有生产代码修改、预测变化、重放、重新计分或吞吐测量。

## 1. 实际定义与代码路径

源码行号以本次工作区为准；归档 replay 的原始二进制身份见各 `runs/*/receipt.json`，源码检查不替代归档二进制逐版本证明。

| 字段/事件 | 实际定义 | 源码 |
|---|---|---|
| `totals.l2_accesses/hits/misses` | 通用 `CacheCounters` 的 lookup 人口；`probe_indexed` 每次增加 accesses，并在 tag 不存在时增加 misses。读写都可以进入。 | `src/cache.cpp:254-272` |
| `totals.l2_writebacks` | `SetAssociativeCache::install` 选择 **valid 且 dirty 的 victim** 时加一；这是 L2 replacement dirty-victim counter。不是 L1→L2 的写回请求数，不是 tag lookup 数，也不是退休宏指令事件。 | `src/cache.cpp:300-313`；输出 `src/main.cpp:3391-3392` |
| `totals.private_l2_tag_misses` / `scope_metrics.pmu.private_l2_tag_misses` | `CoreCounters.l2_data_demand_tag_misses`。接受原 data demand、private lookup 逃逸至 LLC/remote/memory 时加一；排除 I-fetch、PTE、prepare、sequencer alias 和 functional carry。远端供给/LLC merge 不取消已经发生的 local L2 miss。 | `src/simulator.cpp:24436-24446`；`src/main.cpp:484`、`:3373`；`include/fastsim/types.hpp:2293-2299` |
| `scope_metrics.task_pmu.pmu.private_l2_tag_misses` | 上述专用 counter 的请求归属 workload-task 子集；`full_pmu = pmu + other_pmu`。 | `src/simulator.cpp:334-352` (`TaskDataPmuScope`) |
| `totals.perf_l2_writebacks` | 在 data-side shared classifier 中，当 `preview.l2_evicted && preview.l2_evicted_dirty` 时加一。它还可捕捉因 L1 invalidation 发现 dirty 而传播的 L2 victim；因此不要求与 raw cache dirty-victim counter 相等。它也不等于新 `scope_metrics.perf_cache` namespace 的已认证硬件事件。 | `src/simulator.cpp:24575-24576`；`src/cache.cpp:580-586` |
| native `hierarchy.l2.tag_misses` | gem5 `MESI_Three_Level-L1cache`（命名为 L1Cache 的控制器实际是 private L2）的 `uu_profileMiss`；来自有效 trace identity 的 demand Load/Store。`I,Load` 和 `I,Store` 调用此 action；permission upgrade 进入独立 outcome。 | `gem5-fs/src/mem/ruby/protocol/MESI_Three_Level-L1cache.sm:913-940`、`:954-968` |

Native 路径由 `taotraceCacheOutcomeByIdentity(valid, context, sequence, level=1, outcome=1)` 记录；无有效 identity 则返回。`TaoTraceNativeHierarchyFacts::observe` 同时增加 accesses 和相应的唯一 outcome bucket，形成 `accesses = hits + tag_misses + permission_upgrades + merged_misses + remote_supplies`。见 `gem5-fs/src/mem/ruby/slicc_interface/RubySlicc_Util.hh:121-133`、`gem5-fs/src/mem/taotrace_response.hh:44-73`。TaoTrace 汇总复制 `facts.tagMisses[level]`，见 `gem5-fs/src/cpu/o3/probe/tao_trace.cc:2311-2320`。

Native incoming L0 `PUTX` 映射为 `WriteBack`（`MESI_Three_Level-L1cache.sm:270-277`）；`transition({M,E}, WriteBack, MM)` 只写回数据并弹队列，没有 `uu_profileMiss`（`:1056-1059`）。Outgoing private-L2 replacement 经 `g_issuePUTX`，也不调用 `uu_profileMiss`（`:638-655`、`:1050-1053`）。因此不能把 native demand-tag-miss 总数理解为所有 writeback tag lookup。

FastSim 的 L1→L2 dirty writeback 在 `PrivateHierarchy::finish_l1_eviction` 中有三条路径（`src/cache.cpp:489-509`）：

1. 原 demand 的 L2 victim 正好也是 L1 victim：把 dirty 事实随原 L2 eviction 传下去，不重新查找。
2. L1 victim 仍驻留 L2：`mark_dirty`，不增加 lookup/miss counter。
3. L1 victim 不驻留 L2：`l2_.access(victim, true, counters.l2, ...)`；此 fallback 增加通用 accesses/misses，并可能驱逐另一个 dirty victim。返回的 `PrivateAccessResult.level` 仍是原 demand 的结果；没有额外调用专用 demand-miss increment。

第3条是需要保留定义边界的真实代码路径，但现有归档没有独立的 fallback 触发计数，不能把 outgoing dirty-victim 数当成其调用次数。五个归档输出均有 `totals.l2_accesses == totals.l1d_misses`（下表），也没有显现额外约22万次 writeback lookup 的总量证据。这个等式是支持性总量检查，不是逐事件不可达证明。

**描述文本漂移及修正：** 原 `configs/pmu-event-dictionary-v1.json:199` 和归档 validator 的 `pmu.private_l2_tag_misses.fastsim_semantics` 写 `CoreCounters.l2.misses`；当前输出实现和 P2 原始值证明二者已经不同。本次只修改字典中该事件的 `fastsim` 字符串为 `CoreCounters.l2_data_demand_tag_misses`，明确排除 instruction fetch、PTE walk、buffered-store permission prepare、sequencer-coalesced alias 和 functional-carried request，保留“后续 LLC merge / remote supplier 不取消本地 miss”的层次定义。`unit`、`speculation`、native 来源、`mapping: proxy` 及其他事件均不变；JSON 解析验证通过。该修正不改变预测、计分公式、精度或硬件等价状态，也未重写归档报告。读取归档时仍应引用实际 `fastsim_counter_path` 和原始字段，不能把旧描述当作相等证明。

## 2. 五个既有输出的实际计数

对象均为 `c04-872.marian_s`。表中 miss 为 workload-task 子集；后三列是 physical 原始总量，特意分列，不能相减解释为相同人口误差。

| 归档 variant | FS task private-L2 miss | native task private-L2 miss | task signed excess | task relative error | physical `l2.misses` | physical `private_l2_tag_misses` | physical `l2_writebacks` |
|---|---:|---:|---:|---:|---:|---:|---:|
| store `demand-frfcfs-final` | 423759 | 199944 | +223815 | +111.938843% | 423827 | 423827 | 223670 |
| store `d1b-bounded-se-atomic` | 423764 | 199944 | +223820 | +111.941344% | 423834 | 423834 | 223672 |
| store `d1c-p1flags` | 423759 | 199944* | +223815* | +111.938843%* | 423827 | 423827 | 223670 |
| dynamic `p1` | 424302 | 200130 | +224172 | +112.013191% | 424370 | 424370 | 224247 |
| dynamic `p2` | 424302 | 200130 | +224172 | +112.013191% | 424586 | 424370 | 224091 |

`*` d1c 的 raw prediction 与 demand-frfcfs 同源对应字段相同；native 数引用该旧 source 的 demand-frfcfs 已存 validator，本审计没有发现独立 `validation-d1c-p1flags/cases/.../validation.json`，也没有为 d1c 新运行 validator；这三格是同参考代数差值，不宣称新增独立 validation passage。

| 归档 variant | physical `l1d_misses` | physical `l2_accesses` | physical `perf_l2_writebacks` | task `other_pmu` private-L2 miss |
|---|---:|---:|---:|---:|
| store demand-frfcfs-final | 470155 | 470155 | 223702 | 68 |
| store d1b | 470163 | 470163 | 223704 | 70 |
| store d1c | 470155 | 470155 | 223702 | 68 |
| dynamic p1 | 470171 | 470171 | 224277 | 68 |
| dynamic p2 | 470663 | 470663 | 224121 | 68 |

关键反例是 dynamic P2：`424586 - 424370 = 216`，等于保存的 `totals.dtlb_hierarchy_shared_requests`；P2 有483个 dynamic path walks、1841个 physical PTE 请求。专用 data-demand miss 未随 generic/PTE miss 同增，而 outgoing raw L2 writebacks 比 P1 减少156。P1 与 P2 的 task miss 差值完全相同，不能从接近的 writeback 数建立一一对应。

原先“只差75”的算式为 `224247 - (424302 - 200130) = 75`。同样写法在 P2 为 `224091 - 224172 = -81`；旧 store demand-frfcfs 为 `223670 - 223815 = -145`。这里的差值都混合了 task-demand 与 physical replacement 人口，只能作为假设线索，不能成为修正公式。

## 3. 参考来源、窗口与守恒

旧 source：`tmp/spec-speed-32g-c4-c8-c16-20261027-final/source/c04-872.marian_s/`。

新 source：`tmp/dynamic-translation-consumer-20261029/source/c04-872.marian_s/`。

两者不是同一个 frozen functional window。旧 task macro/UOP 人口为 `9455100 / 20389752`，新为 `9417394 / 20313569`；core0 FST SHA256 分别为 `1e43360370e8baa85bc5739c29854e1804c3b42090097a752f478136a721e34e` 和 `95432909693c3bbfca0281c745609edd987b4440b5f9734b7c8d8a068ae8e4d8`，来自各保存输出的 `functional_source_identity.sources[0].artifacts[role=fst]`。不得跨窗口把旧199944与新424302拼表计误差。

Native 参考采用已存维护入口 validation 内的 `task_pmu_audit.native.hierarchy.l2`，来源是 `oracle/task-pmu-core*.json` 中独立 native Ruby/SLICC task 事实及 native summary 审核；**未读取 kernel-events 的 path-class cache 字段充当真值**。

| native task 窗口 | accesses | hits | tag_misses | permission_upgrades | merged_misses | remote_supplies |
|---|---:|---:|---:|---:|---:|---:|
| 旧 store source | 246036 | 46070 | 199944 | 22 | 0 | 0 |
| 新 dynamic source | 245743 | 45590 | 200130 | 23 | 0 | 0 |

两行五类 outcome 分别严格求和为 accesses。新 native L1D 有 `455594` 个 tag miss，但 native L2 accesses 只有 `245743`，不能假定 native 每个 L1D miss 必然对应新的 L2 demand lookup。FastSim 的同层人口关系与 native 存在显著差异，仍需逐请求解释 prepare、合并、请求角色和状态差异；这比把 outgoing writebacks 直接作误差修正更符合现有证据。

原始预测路径：

- `tmp/store-bounded-ab-20261029/runs/{demand-frfcfs-final,d1b-bounded-se-atomic,d1c-p1flags}/c04-872.marian_s/c04-872.marian_s.json`
- `tmp/dynamic-translation-consumer-20261029/runs/{p1,p2}/c04-872.marian_s.json`

已存维护入口报告路径：

- `tmp/store-bounded-ab-20261029/validation-demand-frfcfs-final/cases/c04-872.marian_s/validation.json`
- `tmp/store-bounded-ab-20261029/validation-d1b-bounded-se-atomic/cases/c04-872.marian_s/validation.json`
- `tmp/dynamic-translation-consumer-20261029/validation-{p1,p2}/cases/c04-872.marian_s/validation.json`

Dynamic P1/P2 的 receipts 记录相同 binary SHA256 `e37aaa9d9d03ad2b906a253b4d9f0c8a677dbc02bc0f7cda13e8f2f9efc0386f`，相同 source-artifact-hashes SHA256 `1c93c329cfa980c0c433340ade597288abbddfbfdb174eb1cade752ed176dc29`。这支持 P1/P2 同源配置对照；本次没有重哈希大型FST，也没有把 `status=passed` 解释为硬件 PMU 等价、精度阈值通过或吞吐验收。

## 4. 尚缺的因果证据

现有总量可排除“224247就是已证明额外记入专用 demand counter 的 writeback 次数”这一说法；不能完成224172条 excess 的逐条归因。需要同一短窗口的逐请求 witness，至少包含 demand identity/task/core、read/store/prepare/PTE/writeback 角色、private L2 tag 驻留、native SLICC action、counter 增量、原 victim 及 outgoing dirty-victim identity，才能区分：原 demand 的真实模型 miss、额外 protocol request、状态污染、错误重复需求与通用 counter fallback。没有该配对证据前，不修改生产 counter，也不主张按 writeback 数扣减 miss。

该字段依旧是 native diagnostic 的 `proxy` 映射，并非 SPR `MEM_LOAD_RETIRED.L2_MISS` 的硬件事件计数；本审计不改变三个退休load miss主验收项的定义或 unknown 门禁。
