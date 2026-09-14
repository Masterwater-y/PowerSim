# 全局代码审查：先证明收益空间，再进入模型实验

日期：2026-09-07。参考仍为 gem5；Q 固定 1024。本轮没有改变生产模型或默认配置，没有运行完整矩阵、长 ROI 或新增 gem5 采集。新增的是离线筛选工具、3/4-UOP 反例和基础调度器的短探针。

**决定：暂停继续推进 DRAM 参数/refresh/到达重放；精度优先筛查“延迟分支解析没有传到正确路径恢复”的事件边，吞吐优先考虑统计传递成本与有界历史存储。发现机制缺口还不等于批准实现。**

## 1. 此前方法哪里出了问题

1. 先写了候选，再检查运行路径是否激活。当前 C4/C8 根本没有执行批内 FR-FCFS repair；配置名不能证明可达性。
2. 把局部错误事件或等待之和当作 CPI 收益。pending-fill 修复了局部提前返回，仍几乎不改变 DSE P99；退休关键路径才决定总周期。
3. 没有先计算 P99 的覆盖上限。只修最差一个 case，并不能把整个尾部移走。
4. 没有在实现前约束新增工作量。保留两遍逐 UOP 计算，再叠加哈希、堆、状态复制和额外求解 pass，容易同时得到小收益和高成本。
5. 历史停止记录分散。Issue/FU/writeback、suffix carry、完整 response replay 等不能换名字后重新进入优先队列。历史记录只用于查重，不替代当前 Q/输入上的证据。

本轮也主动筛掉了刚发现的资源超容量方向，见第 4 节；没有因它是新观察就启动全量消融。

## 2. 审查覆盖与架构判断

审查覆盖主运行路径的状态归属、调用关系、关键约束及当前运行计数，并非声称逐行验证了所有配置组合。

| 路径 | 当前实现与证据 | 对优化的含义 |
|---|---|---|
| 输入/静态元数据 | `src/trace.cpp` 的 FST/JSONL、切片、AS/PC 元数据；`BinaryTraceSource::static_instruction` 每次查 unordered_map | 可复用已验证的静态信息，但需保持 AS/来源和指针生命周期；不能修改 trace 分母或依赖事实 |
| 基础 OoO 调度 | `IntervalCoreModel::schedule` 负责前端、依赖、FU、容量、分支恢复及五组全历史数组 | 这里既有宿主冗余，也有反馈不能撤销的基础时序约束 |
| producer/功能层次 | `produce_thread_chunk` 创建 timing、逐项汇总统计、产生 UOP/memory descriptor；cache 在 canonical 路径更新 | 改最终 response 时间并不会自动重做 tag/row/coherence 决策 |
| 共享层次重放 | `SharedTimingDescriptor` 保存已选路径；`replay_timing_impl` 使用已有路径和时序描述 | 纯 latency 调整无法修复错误的路径选择和事件可见顺序；完整重放又有已知成本和收敛问题 |
| response feedback | 默认 fast kernel 仍遍历全部 accepted UOP；传播依赖、IQ/LSQ、ROB 和 retire | 不是“只计算 materialized UOP”；增加一遍完整 closure 要计入全部工作量 |
| 分支生命周期 | producer 按基础 completion 安排恢复；`ChunkUopBound.branch_miss` 的消费者用于退休统计/窗口元数据，反馈内未以它恢复正确路径前端 | 有可独立复现的缺失控制依赖边，见第 5 节 |
| 控制器/DRAM | C4/C8 batch repair 全旁路；非零参数、refresh、选择器均有独立语义缺口 | 已暂停的修复不能因局部 RD tick 对齐就被提升为生产 P99 修复 |

精度问题不是一个统一的延迟比例。当前反馈保留基础时序下界，并主要传播非负位移；若基础层引入了错误依赖，后层不能靠增加等待把它消掉。反过来，基础层缺少某条控制依赖时，只修数据依赖也不足够。**下一步应核对具体事件边，而不是再加一张补偿表。**

对于固定事件图和固定资源顺序，max-plus 递推中增加非负边权只会令完成时间不减。这是有条件的单调性结论；涉及请求重排、路径变化的完整模型不能直接套用。它说明“普遍增加等待”没有同时降低正、负误差的理论理由。

## 3. 从当前 94 个结果计算 P99 的覆盖上限

复用同日冻结的 `tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json` 和 `runs/*/current/stats.json`，不重新模拟。DSE 用 user-UOP CPI，formal 用原标签的 macro CPI，分别计算 case 绝对误差的线性插值 P99。

方法：把候选**能影响的指定子集**误差理想化为 0，其他 case 完全不变，再算 P99。这是条件上限，不是可实现收益或未来总体统计保证。

| 理想化修复范围 | DSE54 P99 最大下降 | formal40 P99 最大下降 |
|---|---:|---:|
| DSE 最差 TeaLeaf/L1D64 C4 单个 case | **0.3926 pp** | 不适用 |
| 全部 TeaLeaf | **2.4475 pp** | **0.2025 pp** |
| 全部 Graph500 | 数据集中没有该 workload | **0.4273 pp** |
| 全部 Stockfish | **0 pp** | **0 pp** |
| 全部 ASTCENC | **0 pp** | 数据集中没有该 workload |
| 全部 TeaLeaf + Graph500 | **2.4475 pp** | **0.9237 pp** |

当前 P99 分别为 17.6521757% / 13.5929669%。上限为 0 不代表没有 MAPE、正尾部或 DSE 趋势价值，而是说明其他尾部不动时，不能宣传该项会降低当前 P99。此前仅凭一个 TeaLeaf/Graph500 窗口估计整体下降 1–3 pp，不成立。

同一工具检查了实际激活情况：

| 核数 | case 数 | 执行 FR-FCFS candidate 的 case 数 | bypass requests 合计 |
|---|---:|---:|---:|
| 4 | 37 | **0** | 3,318,455 |
| 8 | 37 | **0** | 3,916,012 |
| 16 | 10 | 10 | 0 |
| 32 | 10 | 10 | 0 |

因此，只改批内选择器而不改变 activation，对当前 74 个 C4/C8 case 的直接行为收益是 0。激活该路径属于另一个会增加工作的候选，必须独立评价。

## 4. 一个被低成本筛掉的方向：修正后的 issue/writeback 宽度

输入是已验证的 TeaLeaf/L1D64 C4 core 1 密集窗口，sequence 2,509,999–2,519,999，共 10,001 UOP。原始路径：`tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/fastsim-dense.json`。

| 阶段 | 配置宽度 | 峰值 | 超容量周期 | 超额 UOP |
|---|---:|---:|---:|---:|
| base issue | 8 | 7 | 0 | 0 |
| base completion | 8 | 8 | 0 | 0 |
| corrected dispatch | 8 | 8 | 0 | 0 |
| corrected issue | 8 | **13** | **390** | **962** |
| corrected completion | 8 | **13** | **424** | **678** |
| corrected retire | 8 | 8 | 0 | 0 |

进一步在已有数据上做 frozen-service 回放：保留内存服务时长、窗口外状态，沿窗口内四个寄存器边及已记录 ROB 前驱传播，重新预约 issue/writeback/commit 宽度。保留每条边原有 slack；不处理第五条 StoreSet 边的独立完成语义，不重建 FU、IQ/LQ/SQ、控制器或 gem5 动态 issue 顺序。

- 不增加资源约束的对照完全复现原时序，所有位移为 0。
- 增加约束后，issue 位移之和为 16,261 cycles，但末尾 retire **仅增加 3 cycles**，最大 retire 位移为 4。
- 该窗口 gem5/FastSim 退休跨度差为 **5,115 cycles**。不能将 16,261 当作修复了 5,115。

这不是完整修复的 CPI 上下界，也没有证明间接层次影响为零；但当前直接证据不足以支持重开昂贵日历。历史 `docs/uarch-generalization-debug-log.md` §7.3 和 `docs/fs-cpi-candidate-model-audit-2026-08-17.md` 也已记录收益很小、吞吐下降。**决定：维持关闭，不跑新的资源修复矩阵。**

## 5. 新的精度候选：分支解析到正确路径恢复的反馈边

### 5.1 代码和三条 UOP 的反例

`src/interval_core.cpp` 在 branch miss 时用基础 `completion_cycle + mispredict_penalty` 更新前端恢复。`src/simulator.cpp` 随后能把 load 的响应传播到分支 completion，但反馈核内没有读取 `bound.branch_miss` 来同步恢复正确路径 fetch/dispatch。

原 gem5 checkout `/data00/yinhaolang/gem5-fs/src/cpu/o3/iew.cc` 的执行路径在 `inst->mispredicted()` 后调用 `squashDueToBranch`，随后由 commit/fetch squash 路径恢复 PC。对于探针中的**预测不跳、实际跳转且依赖 load 的条件分支**，正确目标路径不能在该条件分支解析前继续执行。这只是必要先后关系，不依赖拟合 redirect 常数。

新增 `tools/probe_branch_response_frontier.py` 用当前维护配置构造单核 user-mode 功能流，保持 Q=1024，执行以下 3-UOP 程序：

```
load A
conditional branch (depends on A; initially predicted not taken, actually taken)
ALU at the correct target
```

当前二进制 SHA256：`f68c7ea21f68205be9d44312f034a409ac5833eb24c281232e8273adb83abf57`。

| UOP | corrected fetch | corrected issue | corrected completion | retire |
|---|---:|---:|---:|---:|
| load A | 204 | 209 | 413 | 413 |
| 依赖 A 的条件分支 | 7 | 413 | **414** | 414 |
| 正确路径 ALU | **238** | **243** | 244 | 414 |

分支基础 completion 是 29，校正后是 414；正确路径仍提前 176 cycles fetch、171 cycles issue。它不是“没有数据依赖传播”，而是**数据依赖已传播，控制恢复边未闭合**。表内 fetch 是现有 audit 给出的时间，不应拿不同 UOP 的值直接声称完整前端流水线已被精确建模。

两个机制对照：

- 去掉 load→branch 依赖，仍有 1 次 mispredict，分支 completion=210，下一条 fetch=238，没有这个先后违例。
- 将条件分支改为初始预测正确的不跳转，branch miss=0；允许年轻独立指令与尚未完成的分支重叠，不能把所有分支都串行化。

审计必须使用 generic feedback。另用生产 fast kernel 执行同样三条记录，确认实际进入 fast kernel 3 UOP，sum cycles、CPI、完整 scope PMU 与审计模式相同。

### 5.2 必须区分“提前执行”和“总周期收益”

三条 UOP 反例的最后 ALU 原本隐藏在分支等待内。即使在固定服务模型里加上必要的恢复边，末尾只需从 414 到至少 420；**不能把提前 176 cycles 直接称为 CPI 缺口**。

四条 UOP 对照将正确路径改成独立 `load B → consumer B`。当前分支 completion=414，B issue=243、completion=447，最后 consumer retire=448。若只将正确路径 fetch 推到 414，保留当前后续阶段时长和两条 load 服务时间，B issue=419、completion=623，consumer=624：这个**固定服务的离线反事实**增加 176 cycles。它说明缺失控制边可能错误地重叠下一条内存关键链；624 不是实际 gem5 测量，也不是生产修复的收益承诺。

### 5.3 先用必要周期预算筛查 workload，不马上实现

当前整个 ROI 的 `(gem5 cycles − FastSim cycles) / FastSim branch misses`：

| case | 当前周期缺口 | branch misses | 若全由该机制解释，平均每次必须补足 |
|---|---:|---:|---:|
| TeaLeaf L1D64 C4 | 4,417,741 | 4,531 | **975.00 cycles** |
| TeaLeaf ROB256 C8 | 6,203,028 | 11,536 | **537.71 cycles** |
| ASTCENC baseline C4 | 2,126,448 | 153,454 | **13.86 cycles** |
| Graph500 C8 | 12,583,227 | 213,027 | **59.07 cycles** |
| TeaLeaf C16 | 8,182,438 | 19,220 | **425.73 cycles** |
| TeaLeaf LLC32 C4 | −2,866,580 | 6,394 | 已高估，必须作为反向控制 |

这些是解释全部缺口所需的预算，不是每个 miss 的实测代价，也不是候选上界。它们使 **Graph500 C8 的有界阶段见证优先于继续追 TeaLeaf 的 DRAM 常数**；ASTCENC 可作机制覆盖检查，但单独修它的当前 P99 上限是 0。不能据此声称该机制已经解释 TeaLeaf 的全部误差。

下一步限定为已有坏窗口附近的稀疏 branch-miss 审计：记录 branch identity、校正 completion、首条正确路径 fetch/dispatch/issue 及关键消费者，区分数据等待和恢复后真正暴露的跨度。优先 Graph500 C8，附 ASTCENC C4 和 TeaLeaf LLC32 正误差控制；没有匹配见证就不进入模型实现。gem5 labels 只用于离线核对。

若进入实现，应在现有反馈遍历里维护随 checkpoint 持久化的 recovery frontier，只对真实预测错误的分支建立恢复边；传播到正确路径的前端/发射和必要资源状态。**不增加第二遍完整 closure，不宣称只移动 core 时间就已重放所有共享 cache 状态。** 首个候选必须明确这一边界，并通过正误差控制；发生新的层次状态反例则停止当前近似。

### 5.4 后续执行结果：机制闭环，正误差控制失败

上述门禁随后已按顺序完成，详细结果见
[分支恢复第一阶段报告](branch-recovery-phase1-20260907.md)。Graph500 C8 core 2 的完整
ROI 稀疏审计找到 26,935 个成对 miss，其中 9,058 个正确路径 fetch 早于 corrected
completion；连续 20,001-UOP 关键窗口的固定服务最大 retire 位移为 137 cycles。
因此取得真实见证后才实现默认关闭的持久 recovery frontier。

候选把 Graph500/ASTCENC 两个真实窗口的必要顺序违例从 10/11 分别降为 0；微基准的
依赖 miss、独立 miss、预测正确、load-chain 和 generic/fast-kernel 对照全部通过。
全 ROI pilot 将 Graph500 C8 和 ASTCENC C4 的绝对误差改善 1.6895/1.0106 pp，却把
TeaLeaf LLC32 C4 正误差从 +9.6487% 恶化到 +10.4836%（+0.8349 pp）。

**决定：候选代码、审计、测试和实验配置保留，`core.response_branch_recovery` 默认关闭；
不运行完整矩阵或生产吞吐门禁。** 当前 checkpoint 已选 cache/coherence 路径没有重放，
后续 checkpoint 的交错会随新时间变化；结果不得描述为完整共享状态闭环。若继续该方向，
必须先有能减少正误差的独立事件证据，不能按 workload 或误差符号选择性应用必要控制边。

## 6. 吞吐候选一：从逐 UOP 返回值中拆出统计累计

实测 `sizeof(IntervalTiming)=1136 B`，其中 `BranchPopulationAuditCounters=176 B`。`IntervalCoreModel::schedule` 构造 timing；`produce_thread_chunk` 还构造一个对象、接收返回值，再逐项累加大量统计。编译后的汇编仍包含初始化、成员传递和统计加法，不是仅凭 C++ 外观猜测。

复用当天四份 perf.data，在其对应冻结二进制 `42f7c689…` 中选取已核对的两段统计累计指令区间 `[0x48690f,0x486c05)`、`[0x486cb6,0x486f6a)`：

| workload | 这些统计块的 sampled CPU cycles 占比 | 落入块内的样本 |
|---|---:|---:|
| ASTCENC C4 | 5.1757% | 239 |
| LBM C4 | 3.5665% | 183 |
| Graph500 C8 | 4.1229% | 621 |
| LBM C32 | 3.4279% | 1,780 |

这只是选中指令块的采样权重，受 IP skid 影响；不含所有统计、构造、复制或 callees，也不是 wall-time 可移除比例。不能把整个 producer+schedule 的约 35%–42% CPU 都当成可优化收益。

方案：操作时序 payload 与累计 PMU/audit 分离；必要计数在状态所属 core/事件发生处累加，在 chunk 边界交付一次。sum、max、warmup reset、部分退休窗口必须分别保持语义；不能直接关闭统计、删除 PMU 或用总计差分替代区间最大值。

收益判断：已有 3.43%–5.18% CPU 指令块证据，适合做小组件原型；还没有端到端吞吐增益测量。在“被移除工作位于 wall 关键路径、占比等于上述 CPU 比例、没有新开销”的理想假设下，删除一半对应约 **1.74%–2.66%** 加速，全部删除约 **3.55%–5.46%**。这些是成本模型场景，不是置信区间或承诺，不能预报成 20% 提速。目标 CPI/PMU 改变量应严格为 0。

## 7. 吞吐候选二：将基础调度历史限制在可读取范围

这是 `IntervalCoreModel` 的历史，不是已经优化过的 `ResidentCoreBuffer`。

当前 `completion_`、`retirement_`、`dispatch_history_` 每个 UOP 永久追加；`load_retirement_`、`store_retirement_` 每个内存 UOP 追加。仅前三项，按当前 accepted UOP 数计算：ASTCENC C4 至少 **0.8972 GiB**，LBM C32 至少 **7.2447 GiB** 有效元素存储；尚未包括 warmup、vector capacity、LSQ 和其他状态。不是总 RSS 或内存总线流量测量。

可证明的读集边界：设当前 UOP 序号为 i、ROB 容量为 R，j≤i−R，则代码的 ordered retire 与 ROB dispatch gate 给出：

```
completion[j] ≤ retirement[j] ≤ retirement[i−R] ≤ dispatch[i] ≤ issue[i]
```

因此这类老寄存器 producer 不可能提高本条 UOP 的依赖就绪时间。仍需保留它“存在一条依赖”的分类和 audit 计数，不能简单修改输入 producer distance。近期 completion/retirement 保存 R+1 项；dispatch 保存 max(R,fetch_queue_entries)+1 项；load/store retirement 各保存容量+1 项。绝对 sequence 计数独立保存，不能再用 vector 长度兼任它。

本轮三个基础调度短探针，各从实际 FST 读取 200,000 UOP，只验证读集和内存布局：

| 配置/流 | ROB 外依赖 | 晚于当前 dispatch 的 ROB 外 producer | 五组现有 capacity bytes | 建议环形数组 bytes |
|---|---:|---:|---:|---:|
| TeaLeaf L1D64 C4，R=192 | 0 | 0 | 6,553,600 | 5,160 |
| ASTCENC ROB96 C4 | 58,019 | **0** | 6,684,672 | 2,856 |
| TeaLeaf ROB256 C8 | 3,250 | **0** | 6,848,512 | 6,696 |

三次 schedule 循环合计约 0.095 秒。探针将 branch miss 固定为 false，没有执行完整 Simulator；不是 ROI 精度验证，也不能拿该时间计算生产吞吐收益。证明仍需覆盖 branch ROB 扫描、syscall、warmup 边界和容量极值。

先只处理这五组历史。按 cycle 索引的 issue/writeback/IQ/port arrays 与 audit-only `dependency_audit_producer_seen_` 有不同生命周期，不能一并换环而不检查。静态映射等其他状态也仍保留，不能宣称整个模拟器内存已降为 O(ROB)。

预期存储收益有明确依据；**吞吐收益未知**，先要求状态完全等价和内存降幅达标。若加 modulo/边界判断抵消了宿主收益，记录结果，不继续靠扩矩阵寻找提速。

## 8. 执行顺序与进入长验证的条件

1. **精度：先做 branch recovery 的稀疏窗口见证。** 新的 3/4-UOP 因果反例已成立；还缺真实尾部覆盖与关键跨度。暂不实现、暂不承诺 P99 降幅。Graph500 优先，ASTCENC 机制控制，TeaLeaf LLC32 反向控制。
2. **吞吐：先做统计传递的小组件原型，其后处理五组历史。** 两者均以 CPI/PMU 完全一致为约束，分别测，不与精度变化混在一起。前者有 CPU 成本依据，后者有存储上限证明。
3. **暂停：** pending-fill、统一恢复 DRAM 非零参数、refresh 直接加等待、旧资源日历、完整额外 closure、suffix carry；不扫描 Q。

每个候选进入长验证前必须给出：当前运行是否激活；尾部覆盖上限；匹配的必要事件关系；零改动回放对照；实际关键跨度而非等待之和；新增/替代工作的成本说明；一个正误差控制。缺任一项，停在诊断阶段。

小窗口必须取自既有坏区间并带入正确的前态；不能用冷启动前 100k 预测完整 ROI，已有 TeaLeaf 前缀误差反号反例。先让坏窗口和控制决定是否淘汰；只有候选通过后才跑 formal40/DSE54 与独立吞吐复测。全矩阵用于验收，不再用于第一次发现候选没收益。

## 9. 复现与验证范围

```bash
python3 tools/screen_optimization_candidates.py \
  --inventory tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json \
  --runs tmp/architecture-evidence-20260907.hlrSNO/runs \
  --dense-audit tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/fastsim-dense.json \
  --core 1 --output tmp/global-screening-20260907/screening.json

python3 tools/probe_branch_response_frontier.py \
  --output-dir tmp/global-screening-20260907/branch-repro
```

离线矩阵/窗口筛选约 0.67 秒；五个 3/4-UOP 探针命令合计约 0.036 秒（一次观察，非性能结论）。原始结果及输入 SHA 保存在 `tmp/global-screening-20260907/`：`screening.json`、`branch-repro/summary.json`、`history-probe.json`、`history_probe.cpp`、`layout.txt`、`counter-block-profile.json`。

本轮没有修改 C++、默认配置或重建生产二进制，因此没有重跑 `fastsim_tests`。新增工具经过实际输入执行；branch probe 检查审计与 fast kernel 的目标输出一致，frozen replay 检查零改动恒等。它们不替代未来生产修改后的默认 build/test、完整准确率与吞吐门禁。
