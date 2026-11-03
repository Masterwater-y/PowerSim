# Store 链路误差机制分析与通用修复设计（2026-10-29）

本文接续 [store-lifecycle-history-and-current-gap-20261028.md](store-lifecycle-history-and-current-gap-20261028.md)。本轮只做分析和设计：**未修改模型，未启动实验**。第 2 节的比例由已有 gem5 `stats.txt` 和 FastSim `on.json` 现成字段计算，不是新的 CPI 测量。

约束沿用用户要求：
- 采用通用机制修复，不按工作负载调参；
- 保持 Bound–Weave 两阶段：一次 canonical 分类，一次 sparse 前向反馈（`reweave=1`），不做全窗口 fixed-point；
- 吞吐与精度同为验收门槛。

## 0. 结论

1. 本轮六项均为 `store_timing_model=legacy`。legacy 的 prepare 资格是批级启发式：同核后缀里只要同 L1/L2 set 出现另一条 line，就整条拒绝；批末任一 prepared store 失去驻留，就放弃整批 prepare 并重放。目标 L1D 为 32 KiB/8 路/64 set，冷 store 流每 64 条 line 就绕回同一 set，因此 memcpy、清页和激活写回这类流会被大量拒绝。被拒的 store 在 FIFO 队头支付完整服务，并且单飞串行。这是 XZ/Marian 正误差最主要的嫌疑机制。
2. 从机制指标看，偏差方向与 CPI 误差方向一致（第 2 节）。gem5 中进入 L0 的写 miss 有 85–98% 以 prepare 形式到达（XZ/Marian），FastSim legacy 只有 42–65%。Graph500 方向相反：FastSim 55%，gem5 33%，CPI 误差也为负；不过它的 store 量极小，不能把 −10.60% 归因于 store。
3. legacy 还有一个反向近似：prepared store 的 permission-ready 取的是 pre-retire 的访存响应，可能早于自身 canWB，于是这些 store 偏乐观。两种偏差会部分抵消，所以只修其中一边，误差可能被放大。
4. 主方案是**先用已实现的 `bounded_analytic` 在本轮来源上做单变量 A/B**。它用 canWB、FIFO 前驱和 lease 解析出 OriginalDemand、Takeover、Publish、Reacquire 四种角色，prepare 不再受启发式过滤，也不需要批级回滚；它本身就是单次前向，旧三项上吞吐 +16.7%~+26.6%。在它之上再补三项通用机制：按 SQ 容量界定 lease 视界、L1/L2 都用 LRU 证书、foreign write 按单条事件失效。
5. 有一个可能独立存在的前提问题：旧来源上，private L2 miss 相对 native 为 +117%/+122%（XZ/Marian）。这会直接拉长 store 服务时间 $L_i$。如果本轮仍然存在，修好 lifecycle 后仍会残留正误差。它属于分类问题，必须用 native validator 单独核验，不能并入 store 修复的收益。

## 1. gem5 目标语义与 legacy 实现对照

### 1.1 gem5 buffer-TSO 链路（本轮 config.ini：SQ=114，seq outstanding=16，TBE=256，ports=200，L0/L1/L2 全 LRURP）

| 边 | gem5 行为 | 资源 |
|---|---|---|
| commit → canWB | `commitStores` 设置 canWB | — |
| 队头真实写 | `writebackStores` 在 `!storeInFlight` 时发送队头，FIFO 单飞 | 占 Sequencer outstanding |
| prepare | 在 `writebackStores` 末尾从 SQ 头扫描：遇 `!canWB` 停止，遇 split/LLSC/atomic/uncacheable 停止；发出 `Store_Prepare`（PF_EXCLUSIVE） | **不占** 16 个 outstanding（`Sequencer::makeRequest` 直接 `enqueuePrefetch`），占 L0/L1 TBE 和下游带宽 |
| 队头遇到在途 prepare | L0 `SP_IE + Store → IM` 接管父事务，不再走第二次 L1 服务 | 同一个 TBE |
| 队头遇到已完成 prepare | 本地写，callback L1=1、L2=5 cycles | — |
| 权限在 publish 前丢失 | 队头 demand 重新取权 | 新事务 |
| SQ 释放 | 实际写完成；prepare 返回**不**释放 SQ | — |

### 1.2 统一记号

令 $C_i$ 为模型退休时间，$d=$`store_commit_to_iew_delay`，$S_i$ 为 local/special 扫描前沿，$L_i$ 为冻结的 canonical 服务时延（含一次排队），$\ell_i^{pub}\in\{1,5\}$ 为发布 callback，$\delta_{i-1}\in\{0,1\}$ 为前驱 local/real 边：

$$E_i=C_i+d+1,\qquad P_i^{s}=\max(E_i,S_i),\qquad D_i=\max\bigl(E_i,\;W_{i-1}+\delta_{i-1}\bigr),\qquad P_i^{d}=P_i^{s}+L_i$$

gem5 目标的 SQ 释放 $W_i$：

$$
W_i=\begin{cases}
D_i+L_i, & D_i=P_i^{s}\ \text{（OriginalDemand）}\\
P_i^{d}, & \text{lease}_i\wedge D_i<P_i^{d}\ \text{（Takeover）}\\
D_i+\ell_i^{pub}, & \text{lease}_i\wedge D_i\ge P_i^{d}\ \text{（Publish）}\\
\max(D_i,P_i^{d})+L_i^{re}, & \neg\text{lease}_i\ \text{（Reacquire）}
\end{cases}
$$

这正是 [bounded_store_timing.cpp](../src/bounded_store_timing.cpp) 267–280 行的解析式。

### 1.3 legacy 实际计算

[simulator.cpp](../src/simulator.cpp) 15713–15752 行与 `regular_store_response_cycle()`：

$$
W_i^{legacy}=\begin{cases}
D_i+L_i, & \neg\text{eligible}_i\ (\text{prepare 被拒})\\
\max\bigl(R_i^{pre},\;D_i+\ell_i^{pub}\bigr), & \text{eligible}_i
\end{cases}
$$

其中 $R_i^{pre}$ 是 canonical 访存响应（`memory_response_cycle`）。在本轮配置下（lifecycle=false、`local_scan_ready_cycle==0`），它**没有** $E_i+L_i$ 下界。

## 2. 机制证据：prepare 到达比例（gem5 L0 transition vs FastSim）

FastSim L1D（32 KiB/8 路）对应 gem5 MESI_Three_Level 的 L0。用下面的比例衡量“写 miss 中有多少以 prepare 形式到达”：

- gem5：$\rho^{g5}=\dfrac{\text{I.Store\_Prepare}}{\text{I.Store\_Prepare}+\text{I.Store}}$；接管占比 $\tau^{g5}=\dfrac{\text{SP\_IE.Store}}{\text{I.Store\_Prepare}}$
- FastSim：$\rho^{fs}=\dfrac{\text{l1d\_prepare\_misses}}{\text{l1d\_prepare\_misses}+\text{l1d\_write\_demand\_misses}}$

| case | gem5 I.Store_Prepare | gem5 I.Store | $\rho^{g5}$ | $\tau^{g5}$ | FS prepare miss | FS write-demand miss | $\rho^{fs}$ | CPI 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| XZ C4 | 147,446 | 11,401 | 92.8% | 46.7% | 54,261 | 29,729 | 64.6% | +27.83% | 0.211079 |
| XZ C8 | 168,237 | 30,050 | 84.8% | 44.0% | 49,136 | 54,521 | 47.4% | +30.66% | 0.206516 |
| Marian C4 | 234,824 | 4,616 | 98.1% | 99.5% | 92,054 | 121,178 | 43.2% | +18.96% | 0.935228 |
| Marian C8 | 419,520 | 11,045 | 97.4% | 99.5% | 157,480 | 218,282 | 41.9% | +14.37% | 0.745794 |
| FLAC C4 | 20,312 | 10,567 | 65.8% | 87.2% | 7,296 | 7,757 | 48.5% | +5.53% | 0.025042 |
| Graph500 C16 | 677 | 1,399 | 32.6% | 50.4% | 623 | 502 | 55.4% | −10.60% | 0.245141 |

六项 CPI MAE = **0.394800**，MAPE = 17.99%（按上表 case 等权重算，数值来自 callback 修复后的已报告结果）。WAPE 需要宏指令权重，本表没有，不计算。

边界说明：
- 来源：gem5 为 `tmp/spec-speed-32g-c4-c8-c16-20261027-final/source/<case>/stats.txt`（单次 dump）；FastSim 为 `tmp/marian-xz-service-repair-20261028/final-ab/<case>/on.json` 的 totals。
- 两边的绝对计数口径未对齐（如 FastSim `perf_retired_stores` 与 gem5 `numStoreInsts` 量级不同），所以**只比较比例**。SLICC transition 计数未做 stall/retry 去重审计。本表是机制方向证据，不是 native cache truth，也不认证 cache PMU。
- Marian 的 $\tau^{g5}\approx99.5\%$ 表示：gem5 中几乎每个冷 store 都在 prepare 在途时被队头接管，SQ 释放约为 $E_i+L_i$，可与前后约 114 条重叠。legacy 下约 57% 的写 miss 在队头串行支付 $L_i$，SQ 吞吐由“每次服务 1 条”决定，与 SQ 触顶 114、`o3_sq_stall_cycles` 偏大的现象一致。

## 3. 误差机制分级

| # | 机制 | 代码位置 | 方向 | 证据 | 通用性 |
|---|---|---|---|---|---|
| M1 | 同 set 后缀一刀切拒绝 prepare（L1 64 set；证书开关仅对 L1 改用 distinct-count，L2 仍保守，注释的 TreePLRU 理由对本目标不成立） | simulator.cpp 7284–7383 | **正**（过多串行 demand） | 强：第 2 节比例；几何上 64 条 line 即绕回 | 结构性缺陷 |
| M2 | 批末任一 prepared store 失去私有驻留/owner，就整批放弃 prepare 并恢复重放 | 7385–7407、23284–23312 | 正；同时有吞吐成本 | 中：无计数器，频率未知 | 结构性缺陷 |
| M3 | 跨核同 line 时整条 line 拒绝（不区分先后，也不区分是否有 foreign write） | 7262–7271 | 正（共享数据） | 弱：XZ/Marian 主要写私有数据 | 结构性缺陷 |
| M4 | prepared store 的 permission-ready 用 pre-retire 响应 $R_i^{pre}$，没有 $E_i+L_i$ 下界 | 15713–15729 | **负**（prepared 偏乐观） | 强（代码语义）；量级未知 | 结构性缺陷 |
| M5 | prepare 服务时延 $L_i$ 冻结在 canonical 时刻，canWB 时刻不重算排队 | bounded/legacy 共有 | 双向 | 已登记的近似（canonical-queue-forecast-v1） | 已知近似 |
| M6 | lease 用批末驻留快照：可能误计 publish 后才发生的驱逐，跨批视界被截断 | 22841–22849 | 小幅正（$\ell^{pub}$ 5 vs 1）或负 | 中 | 已知近似 |
| M7 | private L2 miss 相对 native 偏高（旧来源 +117%/+122%） | 分类阶段 | 正（$L_i$ 偏长） | 旧来源已证实，本轮未复核 | 非 store lifecycle |

`page_fault_state_fill` 在代码中只有默认值 false，没有任何赋值点，所以 7266 行的整批拒绝分支和 bounded 的相应 throw 本轮都不会触发。

M1/M2/M3 使 legacy 偏正，M4 偏负，两者部分抵消。这说明：
- legacy + certificate + lifecycle 历史上把旧 XZ C4 从 +41.15% 拉到 −3.43%，正是同时动了 M1 和 M4；
- 只放宽资格而不加 M4 下界，或者只加下界，都可能越过零点；
- Marian 的 +19%/+14% 小于机制规模（约 57% 串行），说明其他环节可能有负向补偿。修复 store 后需要预期部分 case 误差变号，不能为了保住原有 MAE 再加补偿。

## 4. 通用修复设计（保持两阶段）

### S0：在本轮来源上对 bounded 做单变量 A/B（不改代码）

- 只改 `store_timing_model: legacy → bounded_analytic`，其余（含 `store_private_callback_timing=true`）不变。callback 修复已经是当前基线，保持开启才是单变量对照。
- 支持域已逐项核对：`store_request_tbes=256 ≥ 179`，`cache_store_ports=200 ≥ 116`；`interval_causal_timing`、`response_event_only_approximation`、`store_post_commit_request`、`pending_fill`、`hierarchy_walk` 等均为 false；`interval_reweave_passes=1`；FR-FCFS 不在排斥列表。
- 运行时 fail-closed 风险有两个：UOP 超过 2 个 data fragment，以及 atomic 缺少 locked/barrier 资格。先做冒烟；一旦触发，报告边界，不能为了让它跑通而放宽检查。

### S1：机制指标先于 CPI

bounded 输出各角色计数。先比较 $\rho^{fs}$ 与 $\rho^{g5}$、Takeover 占比与 $\tau^{g5}$、Reacquire 占比与 I.Store 中由丢权引起的部分。**只有这些机制指标都向 gem5 靠近，CPI 的变化才算可信修复**；如果 CPI 变好而机制指标偏离，按补偿处理。

### S2：lease 视界由 SQ 容量界定，L1/L2 都用 LRU 证书（替换批末快照）

在 store $i$ 发布前，访问 cache 的同核年轻访存一定位于

$$H_i=\bigl(\mathrm{seq}_i,\ \mathrm{seq}(\text{store}_{i+N_{SQ}})\bigr)$$

理由：第 $i+N_{SQ}$ 条 store 必须等 $W_i$ 释放 SQ 才能 dispatch，按序 dispatch 又挡住其后的所有指令。这是由配置 $N_{SQ}=114$ 给出的**充分**视界，不是拟合参数。

$$\text{lease}_i=\Bigl[\,\bigl|\{\ell\ne \text{line}_i:\ \ell\in\text{set}_{L2}(i)\cap H_i\}\bigr|<A_{L2}\Bigr]\ \wedge\ \neg\,\text{ForeignWrite}(\text{line}_i,H_i)$$

$$\ell_i^{pub}=\begin{cases}1,& \bigl|\{\ell\ne\text{line}_i\in\text{set}_{L1}(i)\cap H_i\}\bigr|<A_{L1}\\5,&\text{否则}\end{cases}$$

- 目标 L0/L1/L2 全为 LRURP，所以 distinct-count 判据精确：MESI_Three_Level 中 L0 驱逐 M 行会写回私有 L1，权限仍在，所以 L1D 驱逐只把发布代价变成 L2 hit，不是 Reacquire。
- 若配置不是 LRU，证书退化为“未证明”，取保守值并单独计数，不静默套用。
- 实现：在 stage A 的批内逆序扫描中，每个 (core, set) 维护至多 $A+1$ 条带“后缀首次出现序号”的 distinct 表，每个事件 $O(A)$，空间为活跃 set 数乘以 $A$。视界越过批尾时截断为批尾，并输出截断占比。
- 这只改变 lease 判定，不加回滚，不重排 canonical 顺序，stage B 仍是单次前向。

### S3：foreign write 按单条事件失效（取代 M3 的整条 line 拒绝）

在 canonical 顺序中，只有**位于 $H_i$ 内且来自其他核的写/RFO**才使 $\text{lease}_i=\text{false}$，并走 Reacquire（$L_i^{re}$ 用同一冻结的权限缺失服务 recipe，记为 estimated）。只读共享不失效。prepare 永远照常发出（与 gem5 一致：gem5 不因未来冲突放弃 prepare）。

### S4：资源有界性靠证明，不加日历

gem5 的 prepare 不占 16 个 outstanding。在 bounded 证明域内（TBE ≥ max(SQ,buf)+2·LQ+1，ports ≥ SQ+2），每核活跃 prepare 不超过 SQ，所以不需要 TBE 日历。证明不成立的配置直接拒绝 bounded，不回退到 fixed-point。

下游 DRAM 竞争沿用 canonical-queue-forecast-v1（M5）。这些 store 请求在 canonical 遍历中已经各预约一次，再按实际 canWB 预约就是双计。M5 在本设计中保持为已登记近似，单独测量（见 §5 V3）。

### 不做的事

- 不调整 `store_hit_latency`、SQ 大小、证书阈值或任何常数去对齐 CPI。
- 不恢复 event_reference 或多轮 reweave；Marian 64 轮不闭合已经证明此路不可行。
- 不在 legacy 上继续叠加局部修补作为主线。legacy 的 M2 需要整批恢复，本身就是吞吐负担；仅当 S0 因支持域原因不可用时，才把 `buffer_tso_residence_certificate`（扩展到 L2）+ `buffer_tso_prepare_lifecycle` 作为过渡 A/B。

## 5. 验证顺序与停止条件

| 步骤 | 内容 | 通过条件 |
|---|---|---|
| V0 | 六项 bounded 冒烟（同二进制、同来源） | 无 fail-closed 异常；如有，报告触发的 UOP/事件类别 |
| V1 | 机制指标：$\rho$、$\tau$、Reacquire 占比 | 每项都向 gem5 靠近；Graph500 不应被推向更高的 $\rho$ |
| V2 | native validator 复核 private L2/L1D miss（M7） | 列出各 case 偏差；>±20% 的 case，store 结论标注“受分类偏差影响” |
| V3 | CPI A/B：legacy vs bounded（再加 S2/S3 时逐项开关） | 表格必须同时给出相对误差、CPI 绝对误差、MAE、MAPE、WAPE、bias，并单列每个 case |
| V4 | 微基准：冷 memset 多页（Takeover 为主）、同 set 超 8 路驱逐（Publish 变 L2/Reacquire）、双核同 line 写（foreign write）、空 SQ 单 store（OriginalDemand） | 角色判定与 gem5 transition 计数方向一致；不用于拟合 |
| V5 | held-out：本来源其余 c08/c16 的 xz/marian/flac/graph500 | MAE 不劣于 legacy；不允许只在六项上变好 |
| V6 | 吞吐 ABBA（同机同负载） | bounded ≥ legacy（历史为 +16.7%~+26.6%）；S2/S3 增量开销 ≤ 3% |

停止条件：
- V1 机制指标偏离 gem5 时，不进入 CPI 结论；
- 任一 case 的 CPI 绝对误差变大超过 0.05 cycles/宏指令，且角色计数不能解释时，暂停并分析，不调参；
- S2/S3 吞吐开销超过门槛时，先优化数据结构，不能降低证书精度来换吞吐。

## 6. 尚不确定的事项

- legacy 下 M1/M2/M3 各自的拒绝次数没有计数器。如需在 legacy 上定量，可只加观测计数（`prepare_reject_{l1_set,l2_set,cross_core,batch_abandon}`），不改时序。
- 模型内部的 SQ/依赖/响应等待分类（如 XZ C4 core0 SQ 1,482,022）只是模型自身的诊断归类，不是相对 gem5 的误差分解。
- 本轮没有对 bounded 做任何新测量。上文关于 bounded 的预期都是机制推断，S0 之后才能给出数值。
