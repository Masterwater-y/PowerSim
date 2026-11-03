# SPR、gem5、FastSim 的 branch/cache miss 语义审计

本次结论：应以 SPR 官方事件合同为基准，gem5 按该合同产生独立观察值，FastSim 用自身预测状态计算同一事件。当前 branch miss 的退休计数方向正确，但仍是未完成 SPR 绑定与特殊指令认证的 proxy；退休 load cache miss 已有独立账本，服务资格与边界人口仍有缺口。不能声称三者已完整对齐。

本次只做分析、官方定义核对、已有结果复核和小型真机探针；没有修改 gem5/FastSim 模型、事件字典或历史计数，没有重采 FST，也没有重新测量 CPI/吞吐量。

## 统一的主事件

| 主指标 | SPR perf 事件 | Event/UMask | 触发与单位 | 当前 gem5 / FastSim |
|---|---|---|---|---|
| branch miss | BR_MISP_RETIRED.ALL_BRANCHES | C5/00 | 退休的误预测架构分支，包含方向/目标错误；不能数所有 squash | 原始 BPred sticky bit / 自己的预测器结果，均在保留的提交范围计数；仍需 SPR 合同绑定 |
| L1D load miss | MEM_LOAD_RETIRED.L1_MISS | D1/08 | 退休 load 的 L1 miss 事件资格 | 独立 retired_cache 的 retired_l1_load_misses |
| L2 load miss | MEM_LOAD_RETIRED.L2_MISS | D1/10 | 退休 load 的 L2 数据来源 miss 资格 | retired_l2_load_misses |
| L3 load miss | MEM_LOAD_RETIRED.L3_MISS | D1/20 | 退休 load 的 L3 miss 事件资格 | retired_l3_load_misses |

普通 x86 指令按架构宏指令归并；一个 load 的多个 gem5 UOP 或跨行请求不能各加一次。同一事件以成员资格归并；REP、gather、掩码、lock、软件预取、故障重启等须按目标 ISA/事件单独验证，不能用“每个 macro 一次”替代全部特殊规则。事件之间也不能仅靠软件分桶强制互斥或相加守恒。

官方依据：[Intel SPR core 事件表](https://perfmon-events.intel.com/platforms/sapphirerapids/core-events/core/)、[SPR JSON](https://raw.githubusercontent.com/intel/perfmon/main/SPR/events/sapphirerapids_core.json)。本次下载为 v1.40（2026-09-09），SHA256 与已有 SPR v2 字典绑定值完全一致：`95caad89cbe4c96647a540897251e1998edc7eee8efb2dc84a5b13f7d94ce102`。完整源和选定事件元数据在[审计目录](../tmp/spr-pmu-semantic-audit-20261022/intel-event-extract.json)。

## 不能混用的 cache miss

| 事件域 | 对象 | 为何不能冒充上表的退休 miss |
|---|---|---|
| native Ruby/SLICC tag miss | 某级 cache 的 demand lookup/事务结果 | 单位、store/RFO 范围、合并及退休条件不同 |
| L2_RQSTS.DEMAND_DATA_RD_MISS（24/21） | 推测性的 demand data read true miss；排除并入在途 L2 miss 的请求 | 不是退休 load 的 D1/10 |
| L2_RQSTS.ALL_DEMAND_MISS（24/27） | demand 请求的 L2 miss；旧 perf_l2_demand_misses 绑定此事件 | 还包含 RFO/code 的请求域，不能误标成 data-read-only |
| LONGEST_LAT_CACHE.MISS（2E/41） | 核发起的可缓存 LLC miss 请求，含 code、RFO、推测请求及部分预取 | 不是 D1/20；committed data trace 无法完整提供这些直接请求 |

当前 Linux Intel 通用映射中，`branch-misses` 为 C5/00，`cache-misses` 为 2E/41；因此直接使用 `perf stat -e branch-misses,cache-misses`，不能得到“退休分支 miss + 退休 load L3 miss”的统一组合。[Linux Intel 事件映射](https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/events/intel/core.c)

真机主报告应明确选择上述四个 retired 事件，并记录本机 perf 实际编码。旧请求字段和 tag miss 保留各自含义，不删除、不改名、不合并为退休指标。这里聚焦的是数据 load cache miss；若另需指令 cache 或 store/RFO miss，须增加独立合同。

## Branch miss：当前正确的部分与未完成项

当前 gem5 来源为 `taotrace-retired-bpred-v1`：

- [Decode::squash](../gem5-fs/src/cpu/o3/decode.cc) 在直接目标修复覆盖 predPC 前保留误预测标记。
- [IEW](../gem5-fs/src/cpu/o3/iew.cc) 在执行发现方向/目标错误时保留标记。
- [accountTaskPmuCommit](../gem5-fs/src/cpu/o3/probe/tao_trace.cc) 只在提交回调、测量范围内，对 `isControl()` 的指令读取 sticky bit。被 squash 的年轻分支不进入提交统计；访存重排导致的 squash 不能自动充当 branch miss。
- FastSim 使用自己的预测器计算保留分支记录的结果，不从 gem5 的误预测标签取答案。

这比提交时重新调用 `DynInst::mispredicted()` 正确：Decode 已修复 predPC 后，重新比较会漏掉原始错误。该历史修复仍有效，见[原始分支标签修复](branch-miss-oracle-repair-2026-08-24.md)。

尚不能升级为已验证 SPR 等价：

1. [当前字典](../configs/pmu-event-dictionary-v1.json) 的 retired_branch_misses 仍是 `target-specific`、`encoding=unbound`、`mapping=proxy`，没有明确绑定 C5/00。
2. `isControl()` 是 gem5 静态指令分类，尚没有逐类证明与 SPR 架构分支事件一致。普通 conditional/direct/indirect/call/ret 是主要支持对象；ROM、syscall/iret、异常入口、特殊控制微操作需单独验证。不能仅为了去重就盲目加 last-micro 过滤，因为控制动作与宏指令完成成员可能不同。
3. Decode 提前目标修复、BTB miss、RAS 错误等是否与目标 SPR 事件逐类一致，需带子类的对照探针；Intel 公共事件定义不是 gem5 每条内部 redirect 的一一映射证明。
4. gem5 侧的 sticky bit/BPred 一致性审计证明 gem5 内部来源正确，不证明它的预测器等于 SPR。真实误预测数量还受预测算法、历史更新、BTB/RAS 和错误路径训练影响。
5. 分支计数须按架构完成规则与 ROI 一致处理。FLAC 共同终点切进 canonical syscall 时的宏/UOP/分支人口差异，不能通过修改分母或统一加减一掩盖。

## Cache miss：当前 gem5 实际怎样计数

[perf_retirement.hh](../gem5-fs/src/cpu/o3/probe/perf_retirement.hh) 的 Ledger 在提交路径归并宏成员，保存 read/write、owner、CPL、测量边界；异步服务事实通过 context/sequence 关联退休 load。`sourceCounts()` 对逐事件资格做 OR，不按缓存行简单相加。

事实由 [perfRetirementNativeFacts](../gem5-fs/src/cpu/o3/probe/tao_trace.cc) 从独立原生生命周期提取 admissions/responses、L1/L2/L3 lookup 和合并等，不读取 path_class 生成原生 cache 真值：

- `classify()` 依据 L1 原生 hit/miss 与响应关系区分 L1、primary miss、fill follower；不清楚的 forwarding、permission、remote 等保留未知。
- `classifyRetiredCache()` 将原生 L2/L3 hit/tag-miss 转换为退休成员的候选资格；合并、远端、未知服务或缺少对应层观察时保留逐层 unknown。
- `sourceCounts()` 再按完成的退休宏指令归并成员，得到独立 `retired_cache` 事件。原生 SLICC tag-miss 计数本身仍只是另一诊断域。

这个结构是可继续完善的方向，但目前只完成部分成员资格，不是完整 SPR 数据来源重建。

### 可直接定位的缺口

| 缺口 | 当前代码/证据 | 影响 |
|---|---|---|
| 合并 follower 的服务事实缺失 | classifyRetiredCache 遇到 `coalesced` 将下层资格留为 unknown | 大量 L2/L3 参考不是完整计数；不能以“参考较少”调低模型 |
| forwarding 的资格保守未知 | gem5 与 FastSim 都不将 store forwarding 无条件映射 FB_HIT | 保守性合理，但尚未完成硬件服务映射；不能直接当作实际零 miss |
| 软件预取/特殊 ISA 未完成 | gem5 `member.read` 排除 isDataPrefetch；unsupported 有 prefetch/REP/gather/mask/谓词等；FastSim read 同样排除软件预取 | “read=false”路径甚至不进入 load 资格 unknown，不能以 unknown 很小证明完整覆盖。官方 L1_HIT 对 SW prefetch/lock 有特殊规则，正常访存 hit level 不能替代 ISA 资格；miss 规则仍须逐类实测 |
| 边界与 canonical 展开不一致 | FLAC 原始 native 与 FST 多 1 macro/6 UOP/1 branch；退休 cache 宏人口也差 1 | 这是硬人口失败，不是允许暂定误差就可以跨越的缺证据项 |
| 上下文未严格绑定 perf 切换点 | task-oncpu-switch-entry 是项目 owner 合同；请求观察器显式 unknown_context | 不能把项目 owner 或异步请求原始 owner 自动称作 Linux perf 的计数上下文 |

本轮 C8 原始结果支持该判断，下面只列原生退休事件计数和未知量，不重算新的精度：

| 负载 | 事件 | gem5 已知 count | gem5 unknown | FastSim unknown |
|---|---|---:|---:|---:|
| Zstd | L1_MISS | 1,081,618 | 145,605 | 1,110,122 |
| Zstd | L2_MISS | 304,345 | 387,747 | 1,110,169 |
| Zstd | L3_MISS | 62,546 | 387,905 | 1,110,273 |
| Graph500 | L1_MISS | 713,716 | 2,004 | 9,352 |
| Graph500 | L2_MISS | 356,253 | 4,179,613 | 9,364 |
| Graph500 | L3_MISS | 355,360 | 4,179,631 | 9,399 |

Graph500 参考 FB_HIT=4,177,602，与 L2/L3 大规模 unknown 及当前合并路径的保守规则相符；这不是对每一个 unknown 的逐条归因证明。unknown 是每个事件独立的资格不确定数，不能跨事件求和当成缺失 load 总数。来源为[本轮维护验证结果](spec-c8-accuracy-results-20261022.md)。

## 本轮 SPR 实测：验证 FB 与 miss 不可盲目合并

本机确认为 Xeon Platinum 8457C，family 6/model 143/stepping 8。复用维护工具 `tools/run_perf_cache_probe.py`，执行 `clflush; mfence; load A; load A`，每组 100,000 轮、重复 3 次，两个计数器组共 6 次采集；单线程 user-only、固定 CPU、排除 kernel/hypervisor，无继承或缩放。

| 事件 | 每轮中位数 |
|---|---:|
| L1_MISS | 0.99984 |
| L2_MISS | 0.99979 |
| L3_MISS | 0.99979 |
| FB_HIT | 0.99972 |

6/6 次 `time_enabled==time_running>0`。见[探针 manifest](../tmp/spr-pmu-semantic-audit-20261022/hardware-fb/manifest.json)、[原始记录](../tmp/spr-pmu-semantic-audit-20261022/hardware-fb/runs.json)、[汇总](../tmp/spr-pmu-semantic-audit-20261022/hardware-fb/summary.json)。

解释限于这个探针：两条 load 并不是两次退休 miss，约一次 primary miss 加一次 FB hit；不能把 follower 直接继承父请求的全部 miss 资格。它也不能证明所有 L2 合并、所有 split 或所有 REP 的计数规则。各个事件组是独立重复，不能当作同一次运行的精确分桶守恒。

后续要分别验证：L1 fill follower、L2 层合并、已返回与未返回的 parent、partial forwarding、跨行混合服务、远端供给、fault/replay。gem5 Sequencer 合并、MSHR 合并和 Intel FB_HIT 不是凭名称自动等价。

## FastSim 应如何跟随 gem5 对齐 SPR

应共享事件合同，而不共享预测答案。FastSim 只从功能流/合法功能边界事实取得指令、地址、架构完成与线程信息；由自己的预测器和缓存/在途请求模型产生结果。gem5 PMU 标签、命中层级、绝对 tick 仅在验证端使用，不进入 FastSim 输入。

当前 [account_perf_retirement](../src/simulator.cpp) 已只在最终接受的 UOP 前缀归并服务结果，账本跨 chunk 和 warmup 保留宏成员，避免试算重复计数。主要剩余差别是：

- 仍使用自身 private_level/shared_timing 与合并标记推断硬件成员资格；没有完整的 parent fill readiness/逐成员供给描述。Graph500 模型 FB 正资格为 0，而参考超过 417 万，不能因为三个 miss 数接近就忽略分类缺口。
- 特殊 ISA 的硬件资格未认证；模型某些 unknown=0 只是功能输入上作了确定分类，不是实际硬件资格完整的证明。
- 准确性应报告已知 count、事件 unknown、原始分母、任务/权限/ROI，且区分完整参考与暂定子集。只匹配已有已知计数不能认证完整事件。

统计对象可对齐，数值仍有模型误差。当前 C8 gem5/FastSim 为 L1D 32 KiB、L2 1 MiB、LLC 64 MiB；本机 CPU0 sysfs 为 L1D 48 KiB/12-way、L2 2 MiB/16-way、每 socket LLC 99,840 KiB/15-way，见[缓存拓扑快照](../tmp/spr-pmu-semantic-audit-20261022/host-cpu0-caches.json)。分支预测器也没有被证明等于 SPR。若要验证真机数值，还必须冻结匹配的几何、预取策略、线程绑定、SMT、NUMA、输入和 ROI；不是将所有误差归到统计层。

## 建议的修复与认证顺序

1. **冻结四个主事件与范围**：将 branch 字典明确绑定 C5/00，保持 D1/08、D1/10、D1/20；新增逐类 ISA/控制流资格说明。编码绑定本身不升级 mapping 为 strict。退休分母保留 INST_RETIRED 宏指令；branch miss rate 另用同范围退休 branch 作分母。
2. **先修 gem5 的架构完成与成员事实**：处理共同终点里的 syscall/ROM 部分宏，保持共同终点不变，不通过每核延长到不同结束点掩盖人口差异。增加完整的 native parent/fragment/readiness/terminal-service 事实与可重算归属；不能直接把 parent miss 全灌给 follower。
3. **分开验证事件资格和性能模型**：同一汇编微内核、输入、工作量与 ROI，先 user-only，先 ordinary loads/branches，再逐类加入 merged/forwarded/split/atomic/prefetch/REP/fault；分支分别看 conditional/indirect/call/ret/目标修复。真机未知规则保持 proxy；native 侧每条已知资格均应能重算。
4. **再接 FastSim 同语义分类器**：用自有模型重建并评估 primary/follower/forwarded/remote 等服务资格，保持两阶段最终接受前缀，避免用 gem5 输出修正 FastSim 计数。按各事件单独修复，不改变 request/tag-miss 原定义。
5. **扩展到 task+kernel**：确认 perf scheduler hook 与项目 owner 锚点差异，处理迁移/IRQ/权限边界；所有 task/CPU、:u/:k、inherit、SMT 选择必须显式一致。硬件计数避免复用，保留 time_enabled/running。[Linux perf_event_open 合同](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)
6. **最后发布三方结果**：报告 gem5↔SPR、FastSim↔gem5、FastSim↔SPR 三组差异；来源完整度、语义认证与性能误差是分开的维度。只靠 FastSim≈gem5 无法证明 gem5≈SPR，不能用误差相互抵消认证三方对齐。

这不是重新设计全部 PMU：现有退休账本、原生服务注册表、功能边界和维护验证器可保留。修复的重点是补齐事件资格与完成边界，而不是修改历史计数使其接近。

## 本次验证边界

重新下载并核对 Intel JSON 及 SHA256；检查实际生产代码；重新运行退休 cache 资格回归 2 项与 branch oracle 审计回归 3 项，全部通过；完成上述 6 次非复用真机探针。回归证明当前实现行为及来源工具没有发生所测回归，不证明新完成了 SPR 等价认证。

源码/字典 SHA256 和硬件身份保存在[审计 provenance](../tmp/spr-pmu-semantic-audit-20261022/audit-provenance.json)。本次没有 full-workload 真机↔gem5 同 ROI 对照，因此没有新的三方精度数字。
