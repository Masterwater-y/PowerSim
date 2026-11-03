# 当前两阶段关键路径审查与机制修复设计

日期：2026-09-10。状态：代码审查与设计。第一项已有
[受限实现与验证](private-read-services-phase1-20260910.md)，没有 CPI 收益，默认关闭；
第二项已有 [独立读资源组件候选](shared-service-arrival-phase2-20260910.md)，两项 TeaLeaf C4
误差小幅改善，但吞吐下降 3.0% / 8.4%，默认关闭。一般共享服务闭合、中点核心恢复及
第三项仍未实施，整体设计尚未验收。
后续 [普通 store 与资源组回退](critical-service-repair-20260910.md) 已实施，但原 20 个
关键请求直接覆盖仍为 0，L1D64 CPI 误差 −17.8458%、吞吐下降 8.53%。不能据此将
第二单元视为完成；中点恢复和跨 Q 请求身份仍是未实现的关键能力。
依据是当前生产源码、实际配置及 [TeaLeaf 完整 ROI 对比](tealeaf-full-roi-error-audit-20260910.md)。
保持 `interval_weave + time_epoch`、并行 producer、共享批处理和 materialized core
feedback。设计不依赖 `causal_read`，不读取 gem5 时序作为推理输入，不增加负载/PC
系数，也不通过 Q 或统一延迟调节 CPI。

## 1. 推荐方向

修复的中心是 **第二阶段的服务契约**：目前缓存/共享阶段给核心一个已经选定的
`path + latency`；核心将请求推迟后，通常仍平移这个 latency。新契约需要区分：

1. 已可见、无额外共享冲突的稳定命中：继续用常数延迟。
2. 依赖某个在途事务的访问：引用其身份和对应完成边，不再复制父请求的旧延迟。
3. 需要争用共享资源的请求：保存服务约束及其有效条件，重新求值受影响的资源。

第一次完整核心反馈仍保持现有并行执行。若求得的实际到达使服务假设失效，只修复
相关内存事件、资源和受影响的核心依赖部分。**增量调度仍是必须实现的新能力，不能
把现有全核心 `compute_timing_feedback` 再调用一次称为稀疏修复。**

无法同时承诺任意争用/依赖下的精确闭合、永远一次扫描和固定常数成本。组件可能扩张；
应测量其覆盖和最坏工作量，暂不能处理的组件保持明确的旧模型回退，不能称已修复。

### 1.1 已验证的证据与解释边界

以下结果来自已有 [完整 ROI 审计](tealeaf-full-roi-error-audit-20260910.md)，本次整理
没有重跑模拟。CPI 使用固定的 cycles/user-UOP 分母。

| 证据 | 当前结果 | 能说明什么 |
|---|---|---|
| TeaLeaf L1D64 C4 完整 ROI | gem5 CPI 0.6138814，FastSim 0.50460785，误差 −17.8004% | 请求起点修复后仍存在明显欠估 |
| 全流指令对齐 | 40,169,786 条硬件记录；仅过滤 8 条无 O3 stage 的 syscall 辅助事实 | 结论覆盖四核完整 ROI，保留实际内核指令 |
| issued-load-head 等待差 | 少 3,487,465 elapsed cycles；考虑 idle 后，active 差占总缺口 70.47%–79.79% | 优先检查 load 服务及其退休传播；这是账面分解，不是预期修复收益 |
| 全 ROI data DRAM read 数量 | gem5 186,893，FastSim 187,054，差 +0.0861% | 不能将主因解释为大量 DRAM 请求漏采；计数接近不证明逐请求路径正确 |
| 独立 native-v7 窗口的 421 条同线 follower 见证 | gem5 平均 admission→response 111.07 cycles；FastSim 分配本地命中服务 2 cycles | 存在在途数据被当作已可见命中的具体反例；窗口不是全 ROI 比例估计 |
| 当前服务校验与 DRAM repair 激活 | service-fill checks=0；C4 effective selection window=1，repair 旁路 | 不能通过修改未激活 helper 宣称当前 case 已获修复 |

同线 follower 窗口来自独立诊断采集，不替代正式 gem5 CPI 参考。原生 ROBFullEvents
在两端的计数单位和 dump 区间不同；验收优先采用全流对齐后的阻塞头、互斥等待周期
和请求生命周期，不直接相减这些同名计数。

### 1.2 当前采用的决定

- 保留 `interval_weave + time_epoch`、Q=1024、并行 producer、共享批处理和并行
  materialized feedback；不引入 causal_read、逐 UOP 全局事件重放或设备/MMIO 扩张。
- 保留已经实施的请求起点一致性、load response 下界和 WB 容量修复；不按 TeaLeaf
  负残差加回重复等待，不引入负载/PC 系数。
- 下一项实施是稳定组件内的服务身份与响应引用接入；随后扩大到共享约束与核心局部
  恢复，最后替换重复 DRAM 求解并补齐控制器状态。第一项后续已有受限候选，见文首
  实施报告；未进入默认配置，第二、三项仍未实施。
- 旧 pending-fill 和多轮 DRAM 实验已有失败证据；仅开启旧开关或更换容器不能视为
  新机制。允许复用数据结构、反例和事务 API，但必须修正服务关系及资源生命周期。
- 新机制的 CPI 收益、吞吐成本和覆盖范围尚未测得；局部重算不是现有免费的能力。

## 2. 当前热路径与具体断点

以下行号对应本次审查的源码版本。

| 位置 | 当前行为 | 修复含义 |
|---|---|---|
| `src/simulator.cpp:7590` 附近 producer | 每个 chunk 生成 UOP 下界、依赖距离与访存事件 | 保留解码和依赖生成，不引入逐指令跨线程握手 |
| `src/simulator.cpp:8080` | 同时保存原始 `producer_issue_q16` 与按程序序 clamp 的 `delta_q16` | 明确 transport/可用范围边界与硬件请求就绪的不同含义 |
| `src/simulator.cpp:4001` | 普通数据请求默认取 `delta_q16` 作为服务和位移起点 | 不能只切换 raw issue；需同时修改服务、准入、位移出口与边界合同 |
| `src/simulator.cpp:17495` | 将各核单调事件流按时刻归并，复杂度 O(E log C) | 保留按批归并；需要重排时只处理有关事件，不全量 sort UOP |
| `src/simulator.cpp:6273`、`:18580` | private cache access 先选路径并改变 tag/replacement | tag 已安装不代表数据在当前请求时刻已可见 |
| `src/cache.cpp:236` | `access_indexed` miss 后直接 `install` | 需要区分功能预测状态与已接受的时序可见状态 |
| `src/simulator.cpp:18621` | `replay_previewed_memory_event` 调用 `SharedSystem::access` | 同时提交 directory、CHA、MSHR、DRAM 日历并返回标量 latency |
| `src/simulator.cpp:12458` | `response = corrected_event_issue + feedback.latency_cycles` | 实际到达变化未必保持原服务延迟，缺少服务条件重验证 |
| `src/simulator.cpp:12630` 附近 | load fragments → data-ready → WB → completion | 保留已修好的数据下界和 WB 容量，不在这里额外加惩罚 |
| `src/simulator.cpp:11900`、`:12735` 附近 | completion 影响依赖、IQ；retire 影响 ROB/LQ；store response 影响 SQ | 更新服务必须传播到这些真实下游，不只改统计或最终 CPI |
| `src/simulator.cpp:14470` | 可选 source-order Sequencer 先跑一遍完整核心 proposal，再跑正式反馈 | 不能直接启用它充当低成本生产修复 |
| `src/simulator.cpp:15659` | canonical DRAM 之后做额外 FR-FCFS repair | 新方案应逐步替换这份重复求解，而不是继续叠加 |

### 2.1 两个 issue 时刻不能再混用

原始事件时刻来自 dependency/FU 的功能侧下界；`delta_q16` 额外吸收了程序序
访存 envelope。后一时刻还用于高效选取 resident prefix、归并各核事件。

9 月 9 日修复已经把普通请求位移改成同一时基下的差：

```text
ready = consumer_base_issue + dependency_extra
origin = memory_event_origin + interval_gap
event_extra = max(0, ready - origin)
```

这修正了重复计费，但请求仍可能被功能 envelope 约束；共享服务也仍按早期状态选择。
另一个 `corrected_shared_order` 路径还用 `last_core_issue` 做单调 clamp。只切换其中
一处 raw issue 会令其他地方继续按不同起点计算，并可能破坏真实 store/fence/atomic
顺序。后续应把 envelope 留作批次可用性信息，把目标内存顺序约束显式表示为依赖边。
本轮不主张直接删除全部 clamp 或对 load/store 做无条件乱序。

### 2.2 提前可见不只在 LLC

`SharedFill` 和 `validate_fill_service` 已有 LLC generation 校验，但：

- 它们主要保护可选 timing replay，普通反馈仍沿用标量 latency。
- `SharedTimingPath::kLocal` 的快速返回没有 private residency/data-ready 的校验。
- 生产的 `response_pending_fill`、`ruby_sequencer_line_coalescing` 都关闭。
- 已有 prepare/commit cache API 尚未成为生产访问方式；每 set 的 mutation generation
  在普通 hit 的 replacement 更新上也改变，不能直接当作持久的 line residency 身份。

因此“已有 generation 代码”与“当前 hit/merge 语义正确”是两回事。TeaLeaf 本轮
service-fill checks=0，与源码激活条件一致。

### 2.3 服务时间不是一个可以任意平移的常数

以一个简化的 FIFO 资源为例，服务占用 S、返回边 L，前一个请求的完成约束为 B：

```text
start(A) = max(A, B)
response(A) = start(A) + S + L
```

假设旧 A=10、B=100、S+L=20，旧 latency=110。A 改为 80 后，响应仍应是 120，
而不是 80+110=190。若前驱也移动，B 同样要从前驱的新状态计算。

这说明两类复用都需要支持：队列仍占用同一服务槽时复用**绝对** response；整组资源
依赖一起平移且外部边界不变时复用相对关系。既不能一律保留旧 latency，也不能一律
固定旧绝对 response。相对等待可能变短，不能只用 `max(old_response,new_response)`
累加延期；最终 response 仍必须满足真实 issue、数据和资源下界。

## 3. 三种身份、不同生命周期

建议采用以下概念，具体实现优先复用现有字段和数组，名称不是新增公开配置承诺。

| 对象 | 身份 | 拥有的状态 | 释放条件 |
|---|---|---|---|
| CPU request/fragment | core/thread、sequence、fragment | 准入、同线事务引用、completion 消费边 | 按协议 callback；LQ/ROB 等仍依各自规则 |
| 同线事务 | core、physical line、transaction generation | leader、read/write 等待者、Ruby callback | 对应请求队列处理完成 |
| 下层 fill/residency | cache domain、line、fill/residency generation | 数据可见、权限可见、替换和失效状态 | fill 完成及后续 eviction/invalidation |

同线事务可以没有 DRAM 访问，DRAM fill 也可能服务多个上层请求。它们不能共用一个
计数或生命周期。

**新确认的接入陷阱：Sequencer 的 16 个位置不是 16 条活跃 line。**
本地 gem5 `Sequencer.cc:309` 的 `insertRequest` 对 follower 也 emplace request 并
增加 `m_outstanding_count`；`:960` 在插入前检查总请求数；`:599` 的 readCallback
逐请求 markRemoved。`LineGenerationLedger::size()` 却是活跃 line/generation 数。
它可复用为事务索引，但不能直接替代 request 容量。这个差异不是本轮 TeaLeaf 已证
容量瓶颈，却是推广合并机制时必须避免的错误。

至少要区分 request admission、private data visibility、permission visibility、Ruby
callback、core completion/WB 五类边。一个服务对象为这些边提供一致的来源，不代表
把所有资源的释放时刻改成同一个 response。

## 4. 修复一：把在途服务变成可引用的完成条件

### 稳定命中保持快路径

缓存已可见且没有在途权限/失效依赖时，仍直接返回当前 hit latency。命中查询顺带
取出一个短的 pending-service 引用；没有 pending 引用时不做额外哈希和过期堆操作。
不要对所有数据请求重新复制 `PendingFillTable`。

可评估在 `CacheLine` 或其并行数组中保存短索引；具体布局需用 `sizeof` 和实际缓存
占用验证，不能预先承诺没有内存成本。完整服务对象只为在途事务分配，来自可复用的
有界池。引用需含可校验的代次，防止槽位复用后误关联旧请求。

### 在途访问引用父服务

若请求到达落在同线事务的半开活跃区间 `[admission, callback)`，且读/写类型与权限
允许附着，则响应来自该事务对应的完成边。follower 不新增下层 hierarchy transaction，
但仍按 gem5 规则占 CPU request 的 Sequencer 容量。

等于 callback 的请求必须先完成旧事务，再按新的缓存状态决定命中或新事务。不能用
CPU 看见 response 的较晚时刻替代 Ruby callback 作附着边界。

读数据已可见而 write upgrade 尚未完成时，读可以继续，写需要权限。store、atomic、
split line 和 PTE 不能一律套成 load follower：store 使用 commit/TSO/send 边，atomic
使用有序排他边，split UOP 在所有片段完成后才 WB，PTE response 驱动下一层 walk。

### 不把未来的 parent 当成已存在

程序序中先遍历到的请求未必先准入。child 的实际到达早于所谓 parent admission 时，
不能强制 child 等未来的 parent；应触发该 line 的准入重分类。这是旧 pending-fill
末端补等待无法解决的问题，也是只按程序序建立父子链接的反例。

### 与 tag/replacement 一起提交

功能 preview 可以保留，以维持当前吞吐，但其新 tag 是待验证的预测状态，不能直接
证明时序数据已可见。准备 lookup、分类请求、产生 fill/replacement/coherence 差异后，
只在该组件通过时序验证时原子提交。

复用 `prepare_lookup`、`commit_probe`、`complete_fill` 和现有 cache undo buffer；
为真正的 residency/fill 提供稳定身份。若 callback 与同 set 的另一个 fill/eviction
次序变化，必须重算这个 set 的后续行为，包括 dirty victim、writeback 和 PMU 差异。
仅更新一张“ready 时间表”，同时保留错误的 replacement/coherence 顺序，不算完成。

## 5. 修复二：共享服务按约束求值，而不是再次完整推理

### 服务描述符的变化

`SharedTimingDescriptor` 已有 source identity、fill generation 和 DRAM blocker 来源，
可扩展为运行时可用的服务约束。不能只存旧赢家：前驱变化后，原来次大的约束也可能
成为新的决定因素。至少需要当前模型实际涉及的 bank、bank group、channel/data bus、
rank、CHA、MSHR/队列，以及对应的版本/边界。

稳定 row/权限/服务选择下，约束以 `max(arrival+边延迟, resource_release+间隔)`
重新求值；相关资源完成后再传播到后继。这个公式同样覆盖周期增加和排队缩短，
不需要 CPI 参数。

这只解决**已验证的固定服务关系**。FR-FCFS 的选择、row hit/miss、read/write
切换和 hit/merge 分类是离散决策，不能无条件塞进同一个固定 max 公式。必须同时
验证：参与选择的到达集合、同 set/同 line 顺序、父事务身份、权限和跨 Q 状态。

### 第二阶段的执行顺序

1. 保留现有 producer 输出与批次归并；共享 preview 产生服务描述符和局部状态差异。
2. 保留现有并行 materialized feedback，同时写出必要的 request arrival 与依赖来源。
   这些数据已有 `memory_issue_extra_q16` 等载体，不另扫一次全部 UOP 来取 proposal。
3. 验证服务关系。纯命中直接通过；时序关系仍合法的部分求值完成条件；发生路径/选择
   变化的请求加入组件工作表。
4. 只对相关 line/set、CHA/DRAM channel 和依赖部分修复。独立 channel 可用已有
   `run_parallel_channel_tasks`；独立核心部分沿用 worker pool。
5. 服务与核心出口稳定后，原子提交 cache/directory、时序日历和计数；不稳定的组件
   保留未提交状态或回到显式旧路径，不能把部分新状态混入旧 response。

组件关系必须包含同 line、replacement set、coherence、真实共享资源依赖及核心的
RAW/WB/dispatch/ROB/LQ/SQ/branch/fence 边。只追寄存器消费者会漏掉 ROB 退休推进后
对无 RAW 关系指令的入场影响。

### 核心侧真正需要新增的能力

当前实现能够做顺序 feedback，能够记录依赖/阻塞来源，但尚无通用“任意中点恢复并
重算受影响依赖部分”的生产接口。诊断中的 winning owner 字段也不是完整依赖图。

应在已有微批边界保存紧凑 checkpoint；补反向依赖索引以及容量/有序退休边。优先在
受影响的连续部分恢复运行，出口的语义队列、producer completion 和 carry 与原出口
重新一致时停止。数值 digest 可用于筛选，但不能仅凭 hash 相等作精确正确性证明。
若要跳过中间大段，只能使用已验证的转换摘要，不能把所有指令统一平移一个常数。

这些 checkpoint 和索引只维护当前 resident 范围及必要的跨批 carry，不为完整 ROI
常驻一张指令图。先比较“已有 checkpoint 后局部连续恢复”与“额外建立反向索引”的
实际成本，再决定索引粒度；建立和维护索引本身也有开销，不能按内存事件占比推断
全部新增成本。复用 producer-distance 字段不等于已经免费获得反向消费者索引。

没有这个能力时，第一版服务引用可做严格稳定组件内的修复，但应明确覆盖范围。
不能先写一个每 epoch 两遍全 UOP 的实现，再称“以后优化掉第二遍”满足吞吐约束。

### 边界与收敛

- actual admission 超出当前合法提交边界的请求留在 resident suffix，保留身份；不提前
  消耗容量，不在新 epoch 重发下层请求。既有 suffix carry 的数据结构可以复用，
  但其“功能效果已经提交”的状态不能跳过新的 service-validity 检查。
- 已经合法准入的请求可以预约未来 RD/response，这是硬件行为；禁止提前占容量指的是
  **尚未准入的未来请求**，不是禁止一切未来命令预约。
- core-local 和共享 reference 时钟使用现有转换 API；不能在异频核心间直接比较本地
  cycle。现有部分 timing replay 只允许同频，不能未经扩展测试宣称 DVFS 已支持。
- 组件有环或选择不断变化时，扩大到包含这些依赖的局部事务；不能固定迭代次数后
  强行接受。工作预算到达上限时应报告回退覆盖，不伪装为精确结果。
- 不用缩小 Q 掩盖问题。Q=1024 保持作为同一精度合同。

## 6. 修复三：使批处理 DRAM 服务成为唯一服务路径

当前 `SharedSystem::access` 已调用 canonical `dram_.access`；随后
`apply_frfcfs_dram_repair` 再快照、构建队列、排序和多轮求解。C4 的 topology-scaled
window=1 会旁路整个 repair，因此只修改其内部算法不会改变当前 TeaLeaf。

在 corrected arrival 契约成立之后，应把唯一 DRAM 请求直接交给按通道的批处理控制器，
逐步替换 canonical+repair 的重复工作。保留请求到达、选择事件、已预约命令和返回
队列；使用真实已准入队列，而不是 `(cores*ranks/channels)-1` 推断硬件候选数。
空队列/单个真正合格候选可以快路径，不以核数决定是否执行语义。

`DramModel::access_batch` 中 `controller_time` 目前是每次调用的局部变量；bank 和
bus 日历虽然会保存，但这不等于保存完整的 `nextReqTime`、待调度请求和响应队列。
下一实现需要把它们纳入跨 Q 的 controller state。

已有 `frfcfs_causal_selection` 三请求回归可复用：在时刻 100,000 已选择请求并预约
未来命令后，110,000 才到达的 row hit 不能回头抢占。新增跨 Q 切分版检验同一输入
仅改变 host batch 边界时服务选择不变。

读容量包括 waiting read 与 response queue；后者在目标 DRAM data-ready 时释放，
不是一旦发 RD 就释放。write queue 使用独立的准入与 drain 规则。配置字段相同不等于
其生命周期等价。已采 TeaLeaf 窗口没有达到 64-entry 容量，因此这不是已证主要瓶颈，
但也是新控制器必须满足的通用约束。

### 命令约束与 refresh

到达/选择正确后，再逐项验证 tRAS、tRTP、tRRD、tXAW、tCCD_L、tCS 等已经存在的
命令约束，不一次打开所有开关后按 CPI 选组合。原 gem5 的 1,010 请求离线差分可复用。

refresh 使用 rank 生命周期：due → drain → PRE/等待在途数据 → tRFC → closed-row
恢复 ACT。另一个 rank 可以继续执行其合法操作。相位来自 FastSim 自己的初始化和
warmup 状态，不导入 gem5 请求/refresh 时间戳。若输入缺少真实初始相位，不能承诺
重现同一条动态 load 的 1,504-cycle 长尾；可验证事件率、阻塞条件和分布。

## 7. 哪些代码可复用，哪些不能直接开启

| 现有能力 | 可复用部分 | 不能直接当成修复的部分 |
|---|---|---|
| `SharedFill` / `validate_fill_service` | generation、callback 代次检查、无副作用拒绝 | 当前只保护部分 LLC replay；不覆盖全部 private hit |
| `prepare_lookup` / `commit_probe` | lookup proposal、set 版本与拒绝原子性 | set mutation 不是稳定的 line residency generation |
| `LineGenerationLedger` | 半开事务、读/写可见性、代次和 callback 延长 | 活跃 line 容量不能替代 Sequencer request 容量 |
| `PendingFillTable` | 原有反例和失效原因 | 全量哈希/过期堆/复制，以及只在末端追加等待 |
| `DramModel::estimate` | 无副作用的 bank/command 约束计算 | scalar canonical 服务不能代表真实 FR-FCFS 选择 |
| channel worker pool | 按通道并行独立时序计算 | 全阶段重复计算不会因为并行就变成低成本 |
| cache/directory transaction | touched-state undo 与计数回滚基础 | 全 epoch 多次快照/回滚不能冒充局部闭合 |
| sparse core scoreboard / block summary | 序号化 completion、ROB/WB/LQ/SQ 与出口摘要 | 当前名称中的 sparse 不代表已有完整增量依赖求解器 |

旧 pending-fill 完整组合曾把 DSE54 P99 推到 22.95%；仅等待版本的吞吐也下降
1.20%–4.72%。旧 DRAM 实际激活实验也出现精度控制失败和明显成本。新方案必须改变
这些失败的契约/重复工作来源，不能简单启用老代码或换容器后重跑矩阵。

## 8. 吞吐约束与最小实施顺序

当前 TeaLeaf 生产运行有 40,169,794 条 core feedback UOP、5,649,768 个 batch memory
events，最大 batch 2,913；DRAM 请求约 188,124。固定成本应优先落在内存/共享事件上，
避免再给全部 4,000 万条 UOP 增加完整扫描。稳定 L1 hit 保持原循环，owner 状态只覆盖
在途事务。无逐 UOP 全局优先队列、跨线程 request/response 同步或全指令流重放。

该次生产日志的 feedback 计时约 2.58s、measurement 约 6.60s，来自同一次非独立性能
诊断，只用于指出热点；计时互有嵌套，不相加、不用它估算新方案吞吐收益。

建议拆成三个可单独判定的实施单元：

1. **服务身份与响应引用接入。** 修复稳定组件内的 private pending visibility，分开
   request 容量与 line 事务；同一服务来源驱动 completion 及各类释放边。保持 generic
   与 materialized 两条内核行为一致，不能藏在 `cpi_attribution` 或默认关闭的专用路径。
2. **共享约束与核心增量闭合。** 把 request proposal、服务验证、资源约束重新求值和
   受影响核心部分接起来，支持跨 Q。入口一并修正 raw issue/envelope 的职责；不单独
   删除 clamp。第一单元无法闭合的组件在此扩大覆盖。
3. **替换重复 DRAM 求解并补控制器状态。** 一次批次服务、真实队列选择和跨 Q 状态；
   在相同到达流检查通过后逐项接入命令约束和 refresh。

第一单元只完成结构但没有任何目标事件实际使用时，不宣称精度改善；第二单元只修改
了所有 case 都旁路的 helper 时，也不宣称已接入。每个单元同时给出激活、覆盖、退出
原因和实际新增工作量。

后续实施按以下条件记录验收结果，而非仅以新增代码或单个 CPI 改善判定完成：

| 单元 | 必须证明 | 尚未满足时的处理 |
|---|---|---|
| 服务身份与响应引用 | 目标 follower 实际使用新机制；callback/代次与 request 容量守恒；generic/materialized 一致 | 明确未覆盖组件及原因，不把只建立元数据称为精度修复 |
| 共享约束与核心局部恢复 | 等待可增可减；completion/RAW/ROB/LQ/SQ 传播一致；跨 Q 只提交一次 | 报告重算 UOP 数、组件扩张、回退比例；不以额外完整扫描替代局部恢复 |
| 唯一批处理 DRAM 服务 | 同一到达流仅改变 host batch 边界时，服务选择、预约和回调不变 | 先修请求/队列状态，不叠加参数或 refresh 延迟追 CPI |

## 9. 必要验证

### 机制反例

- miss parent 未返回的同线 load；callback 同时刻边界；child 早于候选 parent admission。
- read 尚未有数据与已有数据但 write permission 未到，分别验证。
- 同 line 两代 fill、同 set eviction、失效后迟到 callback，确保旧代不覆盖新代。
- 单 line 的多个 follower 达到 request capacity；未来 store 不提前占用容量。
- 同一已到达请求的排队缩短、整组件一致平移、相同顺序但跨 fill 边界。
- split load 的最后片段决定数据就绪、WB 只分配一次；RAW、ROB/LQ/SQ 的真实依赖传播。
- 同一事件流跨不同微批边界与跨 Q，命令预约/回调只提交一次；路径改变的计数事务回滚。

这些是行为不变量测试，不照抄实现公式。能复用现有 gem5 三请求/同到达流结果的地方
直接复用，不先重新采集大矩阵。

### 真实负载顺序

先跑 TeaLeaf L1D64 C4 的**完整 ROI**，复用全量阶段导出和已采 gem5，检查全程
load-head 差、路径人口及分阶段误差；原有两个密集窗口用于定位请求，不代表全部。
增加同 workload 的 LLC32 C4 正误差控制；若独立机制通过且没有反向恶化，再按改动
类型选 ASTCENC 的 DRAM 控制或 Stockfish 的 store/frontend 控制。

必须同时看 CPI、PMU/请求身份、同线附着正确性、资源守恒和新增工作量。待真实组件
验证有收益后，再做适当的完整 generalization gate；不以反复全矩阵代替机制分析。
吞吐用同二进制/相同配置的可比 baseline、固定宿主资源交错测量，精度诊断插桩不用于
性能结论。当前没有数据可承诺新方案固定百分比的吞吐损失或 CPI 收益。

## 10. 本轮产出边界

本节记录设计审查当轮的边界；后续受限实现及其未通过收益验收的结果见文首链接。

本轮完成源码审查和修复设计，没有实施新时序路径、运行新负载矩阵或改动生产配置。
最关键的未实现能力是：private service 身份/可见性、request 与 line 双层生命周期、
共享服务关系验证后的重新求值，以及真正有界的核心增量恢复。它们分别有明确入口和
反例，下一轮实现应据此推进，而不是追加延迟补偿或启用 causal_read。
