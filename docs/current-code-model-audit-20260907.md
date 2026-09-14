# FastSim 当前代码审查：CPI 建模与宿主吞吐

日期：2026-09-07。范围：当前工作区、maintained native-FS 配置、固定 Q=1024。

本轮结论：最需要处理的是基础调度与响应反馈之间的时间、资源和状态一致性。当前同时存在人为增加的等待和遗漏的等待；追加统一 penalty 无法同时解决两者。吞吐成本主要集中在 producer、基础调度、逐 UOP 响应反馈，不能把当前 sparse/materialized 命名理解成已经跳过大部分指令。

本轮没有修改生产源码、默认配置或历史优化决定。新增的是本报告及独立诊断产物。已有未提交改动保持原样。

## 审查结论速览

下表区分本轮复现、既有 gem5 对照和仍待完成的归因。证实局部建模错误不等于已经测得其对完整 ROI 或 P99 的贡献。

| 组件或路径 | 已确认的问题 | 关键证据 | 证据范围 |
|---|---|---|---|
| Load 响应反馈 | UOP 与 memory event 使用不同基础时刻，消费者可在响应前执行 | 微型 load completion=209、response=233；真实 TeaLeaf 窗口有 166/1,670 个 load 请求违例 | 本轮微型反例及完整 ROI 中的连续窗口；见第 2 节 |
| FU 调度 | 按程序序预约未来 FU，丢失预约之前的可用空档 | 同一 9-UOP 图，FastSim=55 cycles，合法调度=30 | 本轮校验过的调度见证，30 不是 gem5 实测；见第 3 节 |
| DTLB | 非单调查询时刻访问被较晚查询推进的 fill 状态 | cycle 5 的查询命中 cycle 17 才完成的翻译 | 本轮微型反例和真实功能流组件检查；见第 4 节 |
| Cache / store | tag 提前可见，store 的层次写入与 commit/send/SQ release 分离 | 同 line 父请求 response=221，后继 load completion=33 | 本轮重现与既有真实请求证据；见第 5.1 节 |
| 分支恢复 | 数据响应推迟 branch completion，却未同步正确路径恢复 | branch completion=414，正确路径 fetch=238 | 本轮重现；既有候选未通过正误差控制；见第 5.2 节 |
| StoreSet | same-PC 代理不具备完整 SSIT/LFST 训练、容量和清除语义 | Stockfish C16 关闭代理后误差从 +12.8490% 变为 −11.0339% | 既有单开关对照，本轮确认代码和开关仍在；见第 5.3 节 |
| DRAM | FR-FCFS 在 C4/C8 实际旁路，部分命令约束和 refresh 生命周期缺失 | 本轮 candidate epochs=0；既有 gem5 关键请求响应差分 | 本轮运行计数与既有 gem5 FS 证据；见第 5.4 节 |
| 宿主 CPU | producer、基础调度和反馈仍承担逐 UOP 工作 | 两个完整负载的三项 self CPU-cycle 占比为 64.34% / 65.21% | 本轮采样，不能当作可直接移除的 wall time；见第 6 节 |
| 宿主存储 | 五组基础调度历史随 trace 长度增长 | 20万→40万条，有效存储 5.09→10.18 MB，ROB 始终为192 | 本轮组件空间测量，未证明内存带宽饱和；见第 6 节 |

需求与架构见第 1 节；下一步优先级和未验证边界见第 7 节；输入、二进制指纹及复现命令见第 8 节。核心数值已写入本文，原始明细保存在项目 `tmp/` 下；重新执行实验仍需相应功能 trace 和配置。

## 1. 需求、测量口径与实际架构

FastSim 的目标是在允许精度损失的条件下，相对 gem5 FS 提高仿真吞吐。精度合同还包括同一微架构参数变化后的方向、幅度和配置排序；只在一个配置上拟合 CPI 不够。功能 trace 可以提供操作类型、地址、寄存器依赖和退休边界，但 gem5 issue/response/retire 等 timing label 只用于离线对照，不能成为部署输入。

需要严格区分：

- user-UOP 指标：sum core cycles / user trace UOP；
- native combined macro CPI：sum core cycles / user+kernel retired macro instructions；
- 宿主 throughput：本报告使用 measurement user-UOP/s，单独列出包含 warmup 的外部时间时不能混用分子。

当前 native-FS alias 依次组合 v28_6 → v28_5 → v28_4 → v28_2 → v28_1 native → time-epoch 配置。实际启用了 L1I、modeled physical I-fetch、fetch supply/shadow、same-PC StoreSet、sparse response scoreboard、materialized fast kernel、monotone IQ、parallel feedback 和 private preview。不能继续用旧文档中的“没有 I-side / 没有 ROB / 只有标量 miss penalty”描述当前实现。

生产关闭项包括 response pending-fill、response branch recovery、rename free-list、physical hierarchy page walk，以及 corrected-arrival 多遍重放候选。固定 DTLB walk 为 12 cycles。native trace 执行输入中的 CPL3/CPL0 指令，关闭 synthetic kernel service。

```text
每个静态绑定线程的 FST + 功能元数据
  → producer：解码、分支预测、基础 OoO schedule
  → 有界 chunk / resident buffer：UOP 时序与内存事件描述符
  → time-epoch 协调器：Q=1024，选择可接受前缀
  → private preview / canonical replay：cache、coherence、LLC、DRAM
  → 每核 response feedback：数据依赖、IQ/LSQ/ROB、顺序退休
  → 下一 checkpoint 与 CPI/PMU
```

软件线程/硬件核状态有分离；当前是静态、一线程对应一个核的绑定，没有完整的 oversubscription、迁移、阻塞唤醒调度模型。并行 private preview 的等价对象是 FastSim 的 canonical replay，不是 gem5 的真实事件次序。共享 replay 完成后再改 core 时间，并不自动改回先前已经选择的 cache 路径和 DRAM row 状态。

主要实现入口：`src/trace.cpp`，`src/predictor.cpp`，`IntervalCoreModel::schedule`（`src/interval_core.cpp:1210`），`produce_thread_chunk`（`src/simulator.cpp:6203`），`run_interval_weave`（`:16054`），`compute_core_timing_feedback_impl`（`:9136`），`src/cache.cpp`，以及 `src/main.cpp` 的 scope 输出。

## 2. 本轮新复现：load 可以早于自己的响应完成

这是直接的内部因果关系违例，尚未被现有阶段单调性检查覆盖。

代码链：

1. `src/simulator.cpp:7624` 将 data memory event 的时间压成 `max(issue, last_memory_issue)`，保持程序序；同时 UOP descriptor 保留原始 OoO issue。
2. `:3730` 的 `memory_producer_issue_q16` 默认取这个被压后的 event 时间。
3. `:11909` 附近只把 event 的额外 issue/latency 差值加到 UOP completion。
4. `:11991` 附近最终 completion 使用 UOP 自己的 base completion。两者的 base 不一致。

新构造的 5-UOP 反例关闭 DTLB 和 I-side，保留目标 FU/宽度/内存参数，避免用 TLB、错误路径或同 line 合并解释结果：

```text
长计算 → load A
独立 load B → 长计算 → 长计算
```

| 事件 | core issue | 层次请求 issue | 层次 response | core completion |
|---|---:|---:|---:|---:|
| load A | 29 | 29 | 233 | 233 |
| load B | 5 | 29 | 233 | **209** |

load B 的消费者在 209 发射，早于 B 的响应 233。最终退休为 257；若固定本次已经算出的响应，只施加消费者不得早于响应的必要关系，两段各 24-cycle 的计算应到至少 281 才结束。281 是固定服务条件下的推导，不是 gem5 实测、修复实现或全 ROI 收益。

审计核和生产 fast kernel 均为 257 cycles，PMU 完全相同。

本轮另外完整复跑 TeaLeaf L1D64 C4，在 core 1 的 sequence 2,509,999–2,519,999 记录连续 10,001 UOP：

- 1,670 个 load-line event 中，166 个存在 UOP completion 早于自己 event response，最大提前 7 cycles；
- 例如 sequence 2,510,038：UOP issue/completion 为 2,031,907 / 2,031,911，event issue/response 为 2,031,916 / 2,031,918；
- 完整 ROI cycles、PMU 与冻结基线相同，signed error 仍为 −17.9910%。

这证明问题实际存在，但 166 次或各次提前量之和不能作为该 case 的 CPI 误差贡献。进一步归因需要检查这些值是否进入退休关键路径。

证据：[微型模拟输出](../tmp/current-code-audit-20260907.ffCPJM/micro.json)、[真实窗口摘要](../tmp/current-code-audit-20260907.ffCPJM/dense-summary.json)、[完整本轮审计](../tmp/current-code-audit-20260907.ffCPJM/tealeaf-dense/stats.json)。

## 3. 本轮新复现：FU 的未来预约制造额外等待

`src/interval_core.cpp:773` 的 `allocate_issue` 按 trace 程序序逐条处理，并用每个 FU 的单一 `ready` 尾时刻表示占用。它处理尚未就绪的老指令时会直接预约未来时刻；随后就绪更早的年轻指令无法使用该预约之前的空档。

这种表示不能区分“忙到 t”和“在未来 t 才有一项预约”。后续 response feedback 又把基础 issue/completion 当成不可提前的下界（`src/simulator.cpp:11199`、`src/simulator.cpp:12591`），因此不能撤销这类基础层制造的等待。

9-UOP 微型依赖图使用配置中的 6 个整数 ALU、2 个 nonpipelined FP complex FU，sqrt latency=24：

- UOP 0 在 cycle 5 执行长计算，到 29 完成；
- UOP 1–6 依赖它，各自把整数 ALU 预约在 29；
- UOP 7 是独立 ALU，dispatch=4、ready=5，却被推迟到 **30**；
- UOP 8 依赖 UOP 7，最终在 **55** 完成和退休。

对同一依赖图构造并检查了一个合法调度：独立 ALU 在 5，后续长计算在另一个 FP complex FU 上于 6–30 执行，全部指令在 **30** 退休。检查覆盖原 dispatch、RAW、操作 latency、FU 数量/非流水化、issue/writeback/commit 宽度和顺序退休。两种生产反馈入口都实际输出 55，因此这个所谓基础“lower bound”并非该图的可靠低延迟下界。

55 对 30 是模拟器与合法调度见证的比较，不是 FastSim 对 gem5 的误差。没有宣称可以在真实 workload 上获得同样改善。

从三个真实 FST 的 core 0 各读取最前 200,000 records，保留 maintained CPU/frontend/branch 配置，在每次基础 schedule 后检查：不移动已存在预约，是否仍有更早的 FU/issue/port 合法空档。

| 真实输入来源 | 有更早合法 issue 空档的 UOP | 最大局部间隔 | DTLB 未来 fill 命中，见下一节 |
|---|---:|---:|---:|
| ASTCENC C4 baseline | 5,869 | 34 cycles | 14 |
| TeaLeaf C4 LLC32 输入 | 3,301 | 83 cycles | 17 |
| Stockfish C4 baseline | 24,699 | 38 cycles | 28 |

这是从 cold prefix 开始的核心组件检查：保留真实功能流，调用 core 与 predictor，但不执行共享层次反馈，也不是对应 ROI 的 CPI 对照。这里所有输入使用 maintained CPU 配置；不能把表格当成三个 DSE 配置的公平性能比较。空档计数只是发现机制的证据，不是可删除 cycles，部分指令还可能受后续 same-PC 或共享响应约束。

证据：[探针源码](../tmp/current-code-audit-20260907.ffCPJM/core_probe.cpp)、[基础微型输出](../tmp/current-code-audit-20260907.ffCPJM/core-probe.jsonl)、[合法调度校验](../tmp/current-code-audit-20260907.ffCPJM/fu-legal-witness.json)、[真实输入组件检查](../tmp/current-code-audit-20260907.ffCPJM/real-core-full.json)。

## 4. 本轮新复现：DTLB 状态可以被未来查询提前推进

`IntervalCoreModel::translate`（`src/interval_core.cpp:1110`）按当前指令的 dependency-ready 时刻查询。这个时刻不随程序序单调增加。然而 `retire_page_walks_through`（`:1091`）会立即把该时刻之前的 walk 结果装入共享的 timing DTLB；较年轻、ready 更早的 load 随后可看到这些尚不属于其查询时刻的结果。

4-UOP 反例：

- load A 在 cycle 5 查询，walk generation 1 应在 17 完成；
- 一个依赖长计算的 load B 在 cycle 29 查询，触发 A 的 fill 安装；
- 再处理独立 load A，查询时间回到 cycle **5**，却得到 generation 1 的 **hit**，issue=5、completion=9。

移除中间的未来查询，对照 load A 得到 timing miss 并进入 walker 队列。该对照只用于分离状态提前推进，不把两条不同程序的总 CPI 差称为修复收益。

本轮真实输入组件检查分别看到 14/17/28 个这种 generation 的 ready 晚于 lookup 的 hit。完整微型模拟也保留了最后 load 的 base issue=5、completion=9；但它的总 span 被更老 load 遮蔽，不能据此定量归因真实 CPI。

当前 hierarchy-walk 候选有根据 fill generation 检查提前可见性的代码（`src/simulator.cpp:11519`），但生产 `dtlb.hierarchy_walk=false`，不能拿候选代码存在来证明生产路径已修复。固定 12-cycle walker 与上述时间可见性错误是两个不同问题。

## 5. 本轮重现的已知问题，以及既有 gem5 对照

### 5.1 Cache pending fill / store 生命周期仍未统一

`SetAssociativeCache::access_indexed`（`src/cache.cpp:158`）在 miss 当次就设置 valid/tag；默认 pending-fill 和 Sequencer line coalescing 关闭。新双 load 同 line 微型模拟中，首个请求 response=221，第二个得到 L1 hit，response=31、UOP completion=33，早于父 fill。两条指令最终总 cycles 仍为 221，恰好说明局部提前返回不等于总 CPI 改善。

真实 LBM 的既有成对请求也有相同缺口：后继 hit 在校正后比父 miss 早返回 156 cycles。历史 pending-fill 窄修复把 formal40 P99 从 13.5930% 改为 13.1052%，DSE54 仅从 17.6522% 改为 17.5539%，吞吐却下降 1.20%–4.72%。这些是既有验收结果，本轮没有重跑全部矩阵。

store 还有另一套时刻：`:12146` 以 corrected retire/TSO drain 计算 send 和 SQ release，而默认 cache 写状态已经按更早的 canonical request 改过。gem5 的 `commitStores` / `writebackStores` 则先允许已 commit 的 store 写回，再发送请求。当前存在 store 队列和 TSO 模型，缺陷在于它们与层次状态的生命周期不一致；不能描述成“完全没建模 store”。

详见 [当前架构证据](architecture-evidence-audit-20260907.md) 与 [pending-fill 停止结果](pending-fill-phase1-20260907.md)。

### 5.2 Load 延迟传播到了分支，却没有同步正确路径恢复

`src/interval_core.cpp:2247` 按基础 branch completion 安排恢复。当前 response recovery 开关关闭。重新运行现有分支探针：load→mispredicted conditional branch 的 corrected completion=414，正确路径仍在 238 fetch、243 issue。去掉 load→branch 依赖或预测正确的控制均符合预期；生产 fast kernel 和审计核相同。

4-UOP 后继 load-chain 反例在开启已有实验 recovery 后由 448 变为 624 cycles。既有真实 pilot 中 Graph500 C8、ASTCENC C4 绝对误差改善 1.6895/1.0106 pp，但 TeaLeaf LLC32 C4 正误差恶化 0.8349 pp，因此该开关仍未通过生产门禁。这不是调大统一 branch penalty 的理由。

证据：[本轮重跑](../tmp/current-code-audit-20260907.ffCPJM/branch/summary.json)、[既有完整结论](branch-recovery-phase1-20260907.md)。

### 5.3 StoreSet 已有模型，但不是有限容量 SSIT/LFST 的等价实现

当前 `src/interval_core.cpp:1266` 从同 macro 的 load/store overlap 训练 PC 集合；`:1946` 附近依 same-PC、基础 issue 和 ROB 距离决定依赖。它没有实现 gem5 的完整 violation training、SSIT/LFST 容量、清除和 issue 生命周期。

既有单开关对照：Stockfish C16 关闭代理后，CPI 从 0.758162 到 0.597708，signed error 从 +12.8490% 变为 −11.0339%。这足以证明该代理影响显著，也说明全部删除依赖同样不正确。非重叠地址不等于 gem5 必然没有预测假依赖，不能直接按真实地址删除所有预测边。

这是已存在的真实 workload 因果对照，本轮审查确认对应实现和 maintained 开关仍在；没有重复运行 C16 消融。详见 [StoreSet 证据](architecture-evidence-audit-20260907.md)。

### 5.4 DRAM 支持名与实际运行路径有差距

`src/simulator.cpp:14922` 用 `cores × ranks / channels` 缩放 FR-FCFS candidate window。当前 4/8 核、2 ranks、8 channels 都降至 1，直接 bypass。新完整运行中 ASTCENC C4、Graph500 C8 分别有 8,303 / 283,949 个 bypass memory requests，candidate epochs 均为 0。仅修改未执行的 FR-FCFS 选择器无法改变它们。

生产多个 DDR command 约束为 0，没有 rank refresh 生命周期。既有 gem5 FS 逐请求对照已确认 TeaLeaf 的关键非 refresh 请求为 243–358 cycles，而 FastSim 为 161；最长一条 gem5 为 1,504，其调度等待与 refresh 恢复关联。不能把全部非 refresh 请求也归因于 refresh。

既有“恢复命令约束并实际启用新选择器”的 pilot 虽改善负误差，却使 LLC32 正误差从 +9.6487% 恶化到 +14.6878%，并损失吞吐。因此这些缺口需要和 arrival/共享状态一起解释，不能直接全部打开配置参数。详见 [gem5 DRAM 差分](gem5-dram-semantic-alignment-20260907.md)。

### 5.5 尚未定量归因的明确边界

ITLB、完整 speculative rename/wrong-path 数据请求、完整 Ruby transient/retry/ack、真实 FS 线程调度仍不完整。DTLB 当前不执行真实 PTE hierarchy walk；I-side 使用 modeled 物理映射，不能保证与数据侧共享真实物理页的全部关系。这些限制会约束支持的实验范围，但本轮没有量出各自的独立 CPI 贡献，不把它们列成已证实的主因。

## 6. 当前二进制的吞吐证据

本轮在固定 NUMA node 0 / CPU 0–47 上串行运行 ASTCENC C4、Graph500 C8 的完整 warmup+ROI，各执行一次无采样运行和一次 `perf record -F 199 -e cycles:u --call-graph fp`。四次 cycles 和完整 scope PMU 都与冻结 baseline 相同。采样 self CPU-cycle 比例如下，包含 warmup；不是 measurement wall-time 比例：

| 当前函数热点 | ASTCENC C4 | Graph500 C8 |
|---|---:|---:|
| response feedback fast kernel | 25.88% | 27.32% |
| IntervalCoreModel::schedule | 19.83% | 19.01% |
| produce_thread_chunk | 18.63% | 18.88% |
| 三项合计 | **64.34%** | **65.21%** |
| BinaryTraceSource::static_instruction | 5.14% | 2.42% |
| BinaryTraceSource::next | 2.93% | 4.07% |

无采样 measurement 吞吐分别为 **8.3865 / 8.5428 M user-UOP/s**；外部整次调用为 5.248 / 13.361 秒。这里各一次无采样测量仅供重现当前量级，不用于声称百分之几的性能变化或相对 gem5 speedup。本轮未测 gem5 宿主吞吐。

具体成本与架构不足：

1. **实际仍遍历所有 accepted UOP。** ASTCENC accepted=40,141,165，Graph500=90,859,941；fast-kernel UOP 计数分别与之完全相等。`src/simulator.cpp:9484` 的入口计数与 `:10284` 的循环支持这一结论。materialized 数不是实际 loop iteration 数。
2. **Producer 同时调度、组装描述符并累计大量统计。** 这些职责占用相同逐指令路径；静态指令查询目前是 `src/trace.cpp:2013` 的 hash lookup，多处调用。5.14%/2.42% 是整个查询函数的采样成本，不代表全部可通过去重移除。
3. **基础调度历史随整条 trace 增长。** completion/retirement/dispatch 三个 uint64 数组每 UOP 永久追加，load/store history 另外追加（`src/interval_core.cpp:2237`）；issue/writeback 等数组还按绝对 cycle 扩展。chunk/resident buffer 有界不等于整个模拟器内存有界。
4. **并行反馈已有实际价值。** 既有 3 次交错实验关闭它，在 LBM C4 / Graph500 C8 / ASTCENC C4 吞吐损失 4.36% / 42.80% / 33.02%。不应仅凭“有线程同步”就建议删除并行。

第三项又做了当前库链接的独立小验证。ASTCENC 同一 FST 处理 200k → 400k records，五组历史的有效元素存储 **5,092,064 → 10,180,800 bytes**，vector capacity **6,684,672 → 13,369,344 bytes**。ROB 固定 192，并没有随记录数增加。200k/400k 中 ROB 外依赖分别 47,643/98,684 条，晚于当前 dispatch 的老 producer 均为 0；这是已有 ROB 有界读集证明的组件复核，不是环形实现已通过完整等价测试。上述 bytes 不是 RSS，也没有证明宿主 DRAM 带宽饱和。

证据：[本轮性能与等价输出](../tmp/current-code-audit-20260907.ffCPJM/profiles.json)、[ASTCENC CPU 采样](../tmp/current-code-audit-20260907.ffCPJM/dse-baseline-c04-731.astcenc_r-perf/perf-report.txt)、[Graph500 CPU 采样](../tmp/current-code-audit-20260907.ffCPJM/formal-08c-854.graph500_s-perf/perf-report.txt)、[历史空间实测](../tmp/current-code-audit-20260907.ffCPJM/history-summary.json)。

## 7. 优先级与结论边界

**后续方向（2026-09-08）：** 新增两窗连续成对验证、正侧 kernel load 路径分歧、
固定服务退休传播与 owner 级替换方案，见
[成对事件账本与建模优化方案](paired-event-model-plan-20260908.md)。已证实 FU 的局部
retire 提前可能放大 UOP/event 时基错误；不能预设整体回归由共享排队变差导致。
以下保留原审查及首轮尝试的历史依据，不作为重开已停止候选的指令。

**后续状态（2026-09-07）：第一项已按本节门禁实施并停止。** gap-aware FU
calendar 通过 9-UOP 机制测试，但 TeaLeaf LLC32 C4 正误差恶化 0.5758 pp，L1D64 C4
仅改善 0.0063 pp，因此保持默认关闭且未扩 formal40/DSE54。实现、完整 ROI 数据和
下一证据入口见 [FU future-reservation 第一阶段报告](fu-gap-aware-phase1-20260907.md)；
以下文字保留实施前的选择依据。

建议下一步先检验本轮新发现的“多余等待”：FU 未来预约是否在 TeaLeaf LLC32 等正误差窗口的退休关键路径上。这与历史失败的 response 端追加资源约束方向不同：本轮问题发生在 producer 基础层，后层保留该下界导致无法消除。当前只是合法空档和微型 span 证据，不能据此直接启动另一轮资源日历全矩阵。

同时，应把 load 的 completion 与它自己的 request/response 统一到同一时基，并将 generation 的时间可见性作为约束。实现路线必须说明替代哪些现有工作，避免在两次逐 UOP 处理之后再增加第三次完整闭包。必要关系修复并不自动保证整体 CPI 更准：应先用坏窗口和正误差控制验证关键跨度，再决定是否进入 formal40/DSE54 与未参与选择的窗口。

吞吐方向优先做保持目标状态完全等价的统计传递/静态元数据复用、五组历史有界化。性能收益尚未实现和测量；本报告不预报提速比例。精度和宿主优化分开测试，固定 Q，不按 workload 名称或当前误差符号添加开关。

可引用的整体精度仍是既有冻结 94-case 审计：formal40 MAPE/P99/max=6.8096%/13.5930%/13.7367%，DSE54=11.8022%/17.6522%/17.9910%，DSE direction=39/48、pairwise=176/216。本轮只完整复核 ASTCENC C4、Graph500 C8、TeaLeaf L1D64 C4，分别仍为 −14.0319%、−13.7367%、−17.9910%；前后两类 CPI 分母不同，不能混算原始 CPI。本轮没有重跑其余 91 case，也没有新 P99 改善声明。

## 8. 复现与验证

本轮根目录：`tmp/current-code-audit-20260907.ffCPJM/`。二进制 SHA256：`0cdf8a0811d7ac6747bf949d45b7ee4a2faeae695ff281f494b3404b83dda961`。源码与配置指纹见 `provenance.json`。真实输入路径由 `run_audit.py` 从既有冻结 inventory 读取，不覆盖原实验产物。

```bash
cmake --build build -- -j16
./build/fastsim_tests

g++ -std=c++17 -O2 -Iinclude \
  tmp/current-code-audit-20260907.ffCPJM/core_probe.cpp \
  build/libfastsim_lib.a -pthread \
  -o tmp/current-code-audit-20260907.ffCPJM/core_probe

python3 tmp/current-code-audit-20260907.ffCPJM/run_audit.py micro
python3 tmp/current-code-audit-20260907.ffCPJM/run_audit.py real full
python3 tmp/current-code-audit-20260907.ffCPJM/run_audit.py dense
python3 tmp/current-code-audit-20260907.ffCPJM/run_audit.py profile
python3 tools/probe_branch_response_frontier.py \
  --output-dir tmp/current-code-audit-20260907.ffCPJM/branch

g++ -std=c++17 -O2 -Iinclude \
  tmp/global-screening-20260907/history_probe.cpp \
  build/libfastsim_lib.a -pthread \
  -o tmp/current-code-audit-20260907.ffCPJM/history_probe
python3 tmp/current-code-audit-20260907.ffCPJM/run_audit.py memory
```

构建与 `fastsim_tests` 通过；四个新增微型场景均比较 generic/fast 两种反馈入口的 cycles 和 PMU，全部相同；分支探针的依赖/独立/预测正确控制通过；三条真实 FST 各 200k 的核心检查及两点历史空间检查完成；三个完整 workload 的当前目标输出与冻结输出相同。没有 commit/push，也没有将任一实验开关提升为默认。
