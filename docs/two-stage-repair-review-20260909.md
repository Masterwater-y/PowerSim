# 原两阶段框架的机制修复审查

日期：2026-09-09。状态：源码与既有实验审查；本轮未修改模拟器、默认配置或运行新模拟。

后续进展：第一项完整动态依赖已接入原两阶段框架，见
[实施与必要验证](two-stage-dependencies-20260909.md)。下文保留审查时刻的源码状态。
第二项的 load 返回下界与写回容量已实施，见
[返回与写回修复](two-stage-response-completion-20260909.md)；移动请求的服务路径有效性仍待修。

用户明确重申：两阶段框架用于获得高吞吐，修复必须在这一约束内设计。本记录替代
此前继续扩展 `causal_read` 并最终作为生产求解器的推进方向。`causal_read` 保留为小规模
机制反例、差分测试的参考实现；其微架构覆盖、吞吐和 gem5 等价性均未完整验证。

## 1. 审查结论

最值得优先接入的是 **完整功能依赖、同一请求的时刻一致性，以及现有遍历中的阶段归属**。
这些修改不要求逐周期推进所有模拟核心。完整 cache/store 生命周期属于中等改造，必须
通过紧凑事务记录和既有批次边界接入；不能把新原型的全局事件队列直接搬进生产。

两阶段本身没有被反例否定。已确认的问题是：第一阶段候选时序可能包含人为等待，第二
阶段又以另一套请求时基计算延迟；校正后的到达还可能改变已经选定的 cache/DRAM 路径。
需要修复两阶段之间的输入、状态与反馈合同。

本报告中的“低成本”是对新增工作量和数据结构的判断，**不是已经测出的吞吐结论**。
阶段参数/功能输入修正也不能保证所有 case 的 CPI 都改善。正式准入同时约束绝对误差、
参数趋势和吞吐，遵循 [项目合同](project-goal-and-semantic-contract.md)。

## 2. 哪些成果值得移植

| 修复或发现 | 原框架实际状态 | 建议与成本判断 |
|---|---|---|
| producer 去重、完整 RAW 依赖 | reader 已支持 `.deps`；旧引擎拒绝非空扩展；base schedule 与反馈仍只消费固定 RAW 槽 | **第一优先，结构成本低**：稀疏扩展接入两遍，保留旧热记录布局和并行路径 |
| 同一 load 的 memory response 与 UOP completion 时基不一致 | producer 会单独抬高 memory event 时间；反馈按 base completion 加正增量 | **第一优先**：在既有反馈中统一 response 版本、时基及 data-ready 下界；算术部分低成本，WB/FU 冲突处理需要局部日历 |
| load 执行、cache 服务、WB、commit 混用 | 原模型有 minimum-load envelope、WB 分配和 commit 边；新模型的漏 memory-FU 一拍是原型自身问题 | **低成本语义整理候选**：逐路径建立边界对应，初始化时预计算路径延迟；不能照搬原型数值或统一加一拍 |
| 无共享者读后 E→M | 旧 compact directory 已有 owner/modified 和本地 store 快路径；普通读授 E 却绑定到 `ruby.sequencer_line_coalescing`，默认关闭 | **局部解耦候选**：授权规则与 coalescing 开关分离；代码成本低，但需检查在飞读/失效和 PMU。完整权限生命周期属于中等改造 |
| DRAM 已有命令约束参数未激活 | 原 `DramModel` 已有多项 bank/rank/calendar 逻辑；维护配置中 tRAS/tRTP/tRRD 等为零 | **低至中成本候选**：仅映射实际目标且已有实现的参数，工作主要按内存事务计；FR-FCFS 队列选择、refresh 不归入这项低成本清单 |
| 分支完成驱动正确路径恢复 | 已有 `response_branch_recovery`，可在同一反馈遍历内携带前端边界 | **复用已有候选，成本预计较低**；必须处理后继访存路径失效，不能只把 fetch 向后移就宣称闭合 |
| FU 未来预约遮住早期空档 | 旧引擎已有 gap-aware 候选，但扫描周期、扩大时间槽 vector、头部 erase | **中等成本**：改成按区间/环形容量表示的有界日历，保留批调度；不能直接开启现有实现并称为低成本 |
| 提前 hit、leader/follower 身份错误、各级释放混用 | 已有 pending-fill 表、LLC transient、cache prepare/commit 和事务；目前尚未绑定成一致的可见性合同 | **中等成本主线**：仅维护活跃 miss/upgrade 的紧凑记录；把 tag、data、permission 与资源释放分开，跨 Q 保留 |
| store 地址生成、commit、发送、SQ release 不一致 | 旧模型已有 TSO/SQ 和候选开关；完整组合曾严重恶化正误差 case | **中至高成本**：复用 store descriptor，按实际准入区间占用资源；不能仅移动 SQ release 或预占未来名额 |
| syscall、普通 kernel、域外访存策略 | 原 native 路径已经支持；此次 syscall 拒绝来自新引擎入口 | **沿用原实现**；它们不是因本轮研究而需要增加的生产组件 |
| 新原型的 pre-L1 固定 26-cycle lease 等待 | 旧 `SharedSystem::access()` 的读 hit 和已有 owner 的写 hit会直接返回 | **不移植**：这是修掉新原型自身缺陷，不能算作旧模型潜在的 26-cycle 收益 |
| 新原型每核最后 warmup retirement 的测量切点 | 与 gem5 时钟窗尚未证明相同，且不同于旧窗口接口 | **不移植切点算法**：只复用精确 record/macro 边界与人口核对，保留原窗口合同 |

源码入口：`src/interval_core.cpp::schedule/find_gap_aware_issue`；
`src/simulator.cpp::ChunkUopBound/produce_thread_chunk/compute_timing_feedback/SharedSystem::access`；
`include/fastsim/pending_fill.hpp`。具体位置随代码变更，以符号为准。

## 3. 第一项：完整依赖是最清晰的低成本入口

[新采集的 TeaLeaf](fst-complete-dependencies-20260909.md) 共 2,939,373 UOP，去重后
core 2 的 12,192 条记录补回 12,273 条 RAW 边，只有 83 条记录需要第五条动态边
（占整个输入约 0.002824%）。其余恢复的边仍放在原四槽中；附件共增加 1,852 bytes。
因此，不能把依赖修复收益只理解为“支持少量高 fan-in 指令”。也不能把该密度外推到
全部工作负载，磁盘增量不等同宿主运行开销。

接入范围必须完整：

1. `BinaryTraceSource` 当前扩展只在当前 record 有效；producer 在预读推进前消费或保存。
2. `IntervalCoreModel::schedule()` 计算 RAW ready 时读全部动态边。完整动态元数据存在时，
   不再用静态 macro operand 补全去添加另一套保守边。
3. `ChunkUopBound` 当前为 **4 条 RAW + 1 条 StoreSet 边**；第五槽不能改成第五条 RAW。
   在 chunk 中使用仅含扩展记录的 ordinal/offset/count 表和连续 distance 区域，避免
   将每个 UOP 固定扩大成 16 槽，也避免逐 UOP 分配 vector。
4. generic feedback、materialized fast kernel、跨 checkpoint ROB 环及 activity/transfer
   判定消费同一组边。若证书漏看扩展，错误地跳过反馈仍会丢依赖。
5. 保留原有无扩展快路径；带扩展 chunk 的额外工作与扩展边数相关。超过 ROB 年龄的
   producer 只能在“consumer 已准入、older producer 已退休”成立时吸收，不能按 transport
   chunk 的起点丢边。检查 warmup、slice、checkpoint 跨界身份。

这一项完成后，原引擎才能诚实地验证新采集的完整依赖数据。目前 C16 旧文件不能通过
补零、仅设置完整性标志或拼接其他核 `.imap` 升级成精确动态依赖。

## 4. 第二项：在原 feedback 中统一请求时基与完成值

当前 producer 对 data event 执行 `max(raw_issue, last_bound_memory_issue)`，UOP 保留
raw issue；反馈又主要用 `base_completion + completion_extra` 计算完成。已有 5-UOP
反例出现 modeled response=233、consumer issue=209，说明先要修复内部关系。

原结构已经带有 `source_core/source_sequence/source_ordinal`、producer issue、
shared-stage issue、shared response 和路径 descriptor。应先复用这些身份和字段，
对同一版本的服务结果明确普通 load 的关系：

```text
data_ready(u) = max(该 UOP 所有分片的有效 response)
writeback(u)  = WB_allocate(max(data_ready(u), core_execution_ready(u)))
RAW consumer 只能读取 writeback(u)
retire(u)     遵守 ordered commit 和完成到退休的目标边
```

这是替换原 feedback 中的完成计算，不要求为每个流水级创建独立 event。
算术下界本身新增成本很小；现有 response resource calendar 可作为带宽冲突处理的
实现起点，但还需审查其批量成本，不能把 `data_ready` 直接当 WB 时间。
原子操作、store、IFetch、page walk 保持各自语义，不套普通 load 公式。

必须同时承认其边界：

- 只禁止 consumer 早于当前模型的 response，是内部一致性修复；它不会自动消除 producer
  的虚假等待，也不会修好已选错的 hit/merge/DRAM 路径。
- response 必须属于确定的请求和服务版本。如果 corrected admission 改变了服务所需的
  顺序/状态，就应标记 descriptor 失效。不能把旧绝对 response 或旧 latency 原封不动
  绑定到新的 arrival，也不能靠 `max(base, corrected)` 永久保留错误 base 等待。
- 原第五槽 StoreSet 等待的是 store 的对应执行边，不能机械改为等待其 cache callback。

## 5. 中等改造：压缩生命周期，不复制逐周期调度

### 活跃 miss/upgrade 记录

复用现有 pending/transient 和 cache prepare/commit 接口，围绕活跃事务保存最小信息：
line/层级/generation、leader、admission、data-ready、permission-ready、各级 slot release、
set/victim 版本。对没有活跃事务的 resident hit 保持现有快路径。

资源表示半开区间 `[admission, release)`；未来 store 不提前占据当前名额。
同拍 callback 与新准入的顺序固定。返回已有数据和获得写权限分别处理，permission
upgrade 不能凭空产生 DRAM read。跨 Q 保留尚未完成的记录，不因宿主切批 reset。

不能只在反馈末端增加一个 response hash 表：那只能延后表面 hit 的完成，功能 tag/
replacement 已经提前改变的问题仍在。已有 cache `prepare_lookup/commit_probe/
complete_fill` 可复用，但 directory、invalidations、CHA/DRAM 与 PMU 也要由同一个
接受/撤销范围管理。按 line 独立不能覆盖同 set 驱逐和同 channel 资源干扰。

### 局部重算的边界

第一阶段继续并行解码、生成基准时序和 descriptor；第二阶段在既有批次内解析共享
请求并反馈。阶段之间允许有界的受影响请求修正，但不默认增加第二遍完整 core closure。

是否复用已有服务结果，至少检查：请求身份；line/set/victim/owner 版本；是否跨过
fill/permission 的可见性边界；资源 admission/release 的约束是否改变。
**仅请求排序不变不够**：两个请求相对顺序相同，但后者从 fill 前移到 fill 后，merge
也会变成 hit。现有 `same_shared_resource_order` 不能单独作为完整路径证明。

路径不变时，只更新阶段时间和受影响的依赖/资源状态；路径改变时，在提交副作用前
使相关 descriptor 失效，并重算相连的请求范围。范围过大或无法证明时，使用明确记录
的原近似回退/停止该候选，不能默默切到全量 `causal_read` 或无限 reweave。保留原近似
回退意味着该次路径误差尚未解决，报告中不能把它记为修复成功。

### FU 和 StoreSet

FU 空档修复适合资源日历，未必需要逐周期 ready queue。现有 gap-aware 代码对未来
周期逐项扫描、扩容并 erase，不能直接视为高吞吐实现。可先用有界区间或容量日历
表达真实预约，合并连续占用；反馈改变预约时处理受影响冲突。日历改造还要考虑原
FIFO/程序序 IQ 占用的耦合，不能声称修一个 FU 容器就得到完整 O3 等价。

同 PC 的 StoreSet 假依赖是另一独立候选。原 C16 Stockfish 关掉该代理时误差从
+12.8490% 变为 −11.0339%，说明简单开/关都不足够。有限 SSIT/LFST 和 issue/clear
可用紧凑表实现，但 violation 必须来自模型允许的 load/store 交错；在没有该交错
模型时不能把 PC/地址重叠直接冒充 violation。此项不是本轮的低成本第一步。

## 6. 对两阶段性能设计的直接启发

1. **准确性需要正确的因果边，不要求每条边对应宿主 event。** 固定阶段边可在初始化时
   合并成路径常量，资源占用用日历，只有可见性/权限/冲突变化保留独立状态。
2. **第二阶段反馈不应只有 latency delta。** 还需分清 response 身份、服务是否仍有效、
   改变的是数据还是权限，以及哪个资源释放边受到影响。利用已有 descriptor，热字段
   与长审计字段分离；不要为了诊断扩大每个 UOP 的热结构。
3. **精确依赖可减少保守误差，但不保证省计算。** 新数据补回边既可能增加真实等待，
   也能让完整动态边替代宽泛的静态补全；最终成本与收益必须实测。
4. **摘要必须包含访存和资源边界。** 历史 LBM 32-UOP block 中 memory-free 仅约
   0.03%–0.06%，uniform timing block 约 8.82%。不能依赖“多数块没有访存”获得加速。
   可研究带 memory response 输入的 max-plus/依赖转移；资源赢家、外部 fill、fence
   改变时摘要失效。摘要维度、构建成本和命中率均需测量，不能预先承诺 O(1) 全块更新。
5. **统计实际处理工作，而不是只统计有变化的 UOP。** 原 profile 中 fast kernel 仍遍历
   全部 accepted UOP，`materialized_uops` 只是部分变化计数。新指标应包括实际访问 UOP、
   扩展边、重算请求/块、失效范围、复制字节和端到端耗时。
6. **保留已有并行收益。** 既有对照中关闭 parallel feedback，使 Graph500 C8、ASTCENC C4
   吞吐分别下降 42.80%、33.02%。吞吐瓶颈是协作的数据与重复计算，不能因此删掉并行。

已有失败方案是重要约束：[load-admission 的额外完整 proposal/feedback](architecture-evidence-audit-20260907.md)
使 TeaLeaf C4 从 7.1393M 降到 4.3952M user-UOP/s（−38.44%）；
[fill-only](pending-fill-phase1-20260907.md) 虽在原遍历里追加工作，四个 case 仍有
1.20%–4.72% 的吞吐下降，DSE P99 仅改善 0.0983 个百分点。
这些是历史已验证成本，不是本轮新结果，不重新开启这些开关冒充新方案。

## 7. 推荐实施顺序与最少验证

**第一项独立交付：完整依赖进入原两阶段引擎。** 保留 hot record，新增稀疏扩展消费，
补齐 base/feedback/fast kernel/certificate。先验证第 5/16 条慢 producer、StoreSet
槽独立、跨 warmup/checkpoint，再用已有完整依赖 TeaLeaf C4 跑一次真实输入。
不先重采十负载，也不为兼容测试截掉新依赖。

**第二项独立交付：修正同请求 response 与完成的时基。** 先闭合 memory-clamp 和
split-load/WB 的已有反例，在同一反馈遍历内实现；同时报告尚未解决的 shared 路径
变化。随后再审查 E 授权解耦和阶段参数映射，每项独立开关，避免混合归因。

**第三项：选择一个局部生命周期问题，连同压缩状态设计实施。** 优先数据可见性和
miss generation，而非立即扩大到完整 Ruby/OS。只修响应表的历史结果已存在，新的
实现必须在功能 tag/merge/资源区间上有实质差异。

本轮仅审查，不执行上述实验。实施后最小验证原则：

- 按改动运行构建和 `fastsim_tests`；新检查覆盖失效关系，generic/fast 必须一致。
- 先用已有机制反例和一份完整功能输入，验证指令/请求身份、边界与资源守恒。
- 只有该候选通过后，才补一个已知相反误差方向的控制和一个相关硬件参数变体；完整
  矩阵留到候选稳定。现有控制集用于回归，不能宣称 workload-held-out。
- 吞吐采用同输入、同核数/Q、同 warmup/ROI 计时口径、固定 NUMA 的最少交错 A/B；
  并行准确率跑数不能代替隔离吞吐。基线不能忽略扩展边来制造性能对照；无扩展旧输入
  可用于兼容成本 A/B，新扩展输入需使用消费相同全部依赖的 generic/fast 对照。
- 保留项目既有吞吐门槛和相对基线损失两项约束；既有正式推荐下限为 5M user-UOP/s，
  不能把它当成允许把更快 baseline 降到刚过线的预算，也不能为候选临时修改门槛。

## 8. 审查依据与交付边界

- [项目目标与允许的机制近似](project-goal-and-semantic-contract.md)
- [旧引擎反例和因果边界审查](global-cpi-model-review-20260908.md)
- [架构、性能 profile 与 proposal 成本](architecture-evidence-audit-20260907.md)
- [完整依赖的功能一致性和空间证据](fst-complete-dependencies-20260909.md)
- [load 阶段映射](causal-load-stages-roi-20260909.md)、[请求/返回/权限修复](causal-cache-permission-repair-20260909.md)
- [已有 block-transfer 低覆盖证据](fastsim-cpi-throughput-debugging-interview.md)
- [窗口与 DVFS 合同](dvfs-window-api.md)：保留 `interval_weave/time_epoch` 和原外部接口；
  任何内部阶段时刻变化需遵守 core-local/reference-time 转换，不能以新引擎单时钟替换。

审查输入指纹保存在 `tmp/two-stage-repair-review-20260909/inputs.json`。
本轮交付审查和实施顺序；没有新的 CPI、吞吐收益或默认开关变更。
