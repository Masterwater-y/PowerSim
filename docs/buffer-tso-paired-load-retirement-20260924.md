# Buffer TSO：逐事件配对与 load 退休分解（代表性案例首轮取证）

日期：2026-09-24。本文是对 [`buffer-tso-cpi-diagnosis-next-steps-20260924.md`](buffer-tso-cpi-diagnosis-next-steps-20260924.md) §8-A 的执行结果：用 native v7 逐事件时间戳，对代表性案例做同一动态指令身份的 load 退休跨度配对分解，定位首个事件分歧。本文所有数据来自诊断采集，**不能外推为全 ROI 的 CPI 因果归因，也不构成任何模型修改或晋升依据**。

## 1. 范围、数据与硬边界

测试范围按用户要求覆盖标准 9 负载 × 4 核数（36 案）矩阵的代表性子集，而非仅 TeaLeaf/NAMD。代表集定义：[`configs/buffer-tso-diagnosis-representatives-v1.json`](../configs/buffer-tso-diagnosis-representatives-v1.json)（LBM C4/C32、TeaLeaf C4/C16、NAMD C4、Graph500 C4/C32、Stockfish C8、nab C16，共 9 案）。

- 实验根目录：`tmp/buffer-tso-diagnosis-20260924/`。
- 9 案全 ROI baseline 全部 scored/passed；CPI、PMU 与各自历史冻结结果逐值一致。
- 配对所需 native 短窗口：用 `tools/collect_tail_timing.py` 以诊断 gem5 二进制（`tmp/se-fs-paired-c4-20260820/.../gem5.opt`）重采 100k user-uop 窗口，输出 v7 `native-response-core*.jsonl`。
- 配对工具：`tools/audit_load_retirement.py`（新增，9 个单测通过）；审计样本为 core 0、ROI 起点起 4096 条记录、frontier stride=1。

硬边界（全部保持）：

1. 诊断 gem5 二进制与正式采集二进制**不同**（`gem5_binary_same_as_source=false`）。即使功能前缀、硬件几何、协议时延一致，本批结果仍只能用于定位机制，不能晋升。
2. FastSim 的 `corrected_issue`（请求起点）**不是已验证的 Sequencer 准入时刻**。因此以"准入"为端点的 1 周期差异可能是相位/边对齐，不能直接当作纯服务时延错误。
3. 短窗口仅定位机制，不做全 ROI 加性归因；`actual_cpi_contribution=null`、`full_roi_attribution=false`。
4. 别名、分裂、coalesced、subcycle 的 native 请求无精确父身份，一律排除并计入 `exclusions`，不猜测。
5. native 守恒/排空门通过：`pending_without_response=0`，admission 与 response fragment 守恒；零 unknown-kernel 门不放松。

## 2. 退休跨度分区结果（core 0，ROI 起点 4096 条）

分区把每条退休指令相对上一条的墙钟区间，按 fetch/issue/request-start/callback/retire 切为互斥类别并守恒。下表为 FastSim 相对 gem5 的周期差（正=FastSim 更长）。

| Case | 配对普通 load 数 | 退休跨度差 | 主要差异类别（周期） |
|---|---:|---:|---|
| LBM C4 | 215 | **+1035** | acceptance→callback **+1202**；callback→retire −78；productive −43 |
| NAMD C4 | 423 | **−107** | callback→retire −113；acceptance→callback +64；productive −60 |
| TeaLeaf C4 | 342 | **0** | 各类别均 0 |

补充：

- LBM 该窗口 gem5 跨度 5299、FastSim 6334；idle 预算为 0，active 界 [1035,1035]。
- TeaLeaf 起点窗口跨度差为 0，但其**全 ROI CPI 仍低估约 14.35%**。这说明分歧不在 ROI 起点，而是在窗口之后累积；起点窗口不代表全 ROI。NAMD 起点窗口 FastSim 反而更短（−107），方向同样不能代表全 ROI。
- 大量记录被排除（如 LBM `not-ordinary-load` 3280、`native-timestamps-unavailable` 417、`native-fragment-identity-unavailable` 185）。这些 load 在退休分区中不产生独立区间（其 issue→retire 已与上一条退休重叠，落点为零跨度），故 `load_unresolved=0` 是守恒分区的正确结果，而非"问题已解决"。

## 3. 逐对服务时间差（按 native 落点分层）

为绕开退休墙钟分区"不可加"的限制，对每个成功配对 load 直接比较双边服务时长。这是机制定位，不是 CPI 分摊。

### 3.1 L1 hit：端点口径造成的 +1 相位差，issue→response 已对齐（已裁定）

| Case | n | gem5 准入→响应 | FastSim 请求起点→响应 | 表面差 |
|---|---:|---:|---:|---:|
| LBM C4 | 172 | 1 | 2 | 恒 +1 |
| TeaLeaf C4 | 342 | 1 | 2 | 恒 +1 |
| NAMD C4 | 378 | 1 | 2 | 恒 +1 |

若只比较"准入/请求起点 → 响应"，会看到 FastSim 恒为 2、native 为 1。但这两个端点并非同一时刻，需回到完整边 issue→response 裁定（诊断脚本 `diag_l1_phase.py`）：

| 边（L1 hit，三案一致） | gem5 | FastSim |
|---|---:|---:|
| issue → 准入 / 请求起点 | 1 | 0 |
| 准入 / 请求起点 → 响应 | 1 | 2 |
| **issue → 响应** | **2** | **2** |

结论：**完整边 issue→response 两边都恰为 2 周期，零误差，固定服务时延本就与 gem5 对齐。** 所谓 +1 是端点口径相位差——gem5 的 Sequencer 准入比 issue 晚 1 周期，FastSim 的请求起点与 issue 同周期、把这 1 周期并入请求起点→响应。因此**不得据此修改 `ordinary_load_latency` 等常量**（LBM 有 5/172 个请求请求起点比 issue 晚 1，属极少数边界，不改变结论）。

### 3.2 LBM LLC-miss（mem）：双向离散，主矛盾

- 43 个 mem 请求；服务时间差之和 **+1289**，均值 **+29.98**，区间 **[−145, +298]**。
- native 服务时长高度聚集于 **227 / 142** 两档（典型 LLC-miss/在途接管），而 FastSim 从 **161 发散到 440**，且与 FastSim 自身的准入周期强相关。
- 首个配对 mem load（record 229049）：native 227 vs FastSim 161，差 **−66（FastSim 更短）**。即总体 +1289 并非从第一条起单向累积，而是后续请求服务时长错配的混合结果。
- 方向双向（既有 −145 也有 +298），说明这不是单一固定时延偏差，调一个常量无法同时收敛两侧。

### 3.3 NAMD L2 hit

45 个 L2-hit 请求，差之和 +41，均值 +0.91，区间 [−3, +1]：绝大多数 +1，与 L1 的边偏移同源，少数 −3。

## 4. 并发集合一致：分歧在每请求服务归属，不在"缺 MLP"

对 LBM 每个 mem 请求，统计其准入时刻仍在途的其他 mem 请求数：FastSim 与 native 的并发数逐条完全相等（0/1/2 一一对应），在途请求集合相同。

含义：

- 在该窗口内，FastSim 并未"少并发请求"或"漏 MSHR 合并"。这与此前撤回的"串行消融证明 MLP 是主因"判断一致——并发集合不是该窗口分歧来源。
- 真正的差异是**对同一在途请求集合，每个请求分到的服务时长不同**：native 按 227/142 等真实在途/接管生命周期服务，FastSim 则按其请求起点发散为 161..440。
- 因此下一步应核对"请求起点→在途父服务/接管→响应"的服务归属，而非继续增加 MSHR 容量或并发开关。

## 5. 首个事件分歧（当前判断）

1. ~~L1-hit +1~~ **已裁定为端点口径相位差，issue→response=2 已对齐，不是分歧**（见 §3.1）。
2. **LBM LLC-miss 服务时长错配（当前首要待查）：** 同一在途集合下 FastSim 不落在 native 的 227/142 档，而随请求起点发散，首个分歧 load（229049）为 −66，整体双向离散。
3. TeaLeaf 的全 ROI 误差不在起点窗口，首分歧需到 ROI 中/后部窗口才能定位。

## 6. Graph500 C4 采集未能完成

Graph500 C4 在 ROI 起点的 trace 体量异常：100k-uop 窗口产出约 53G、20k-uop 窗口产出约 42G trace 后 gem5 进程死亡，均无 `completion.json`。已清理半成品。该案例的 v7 配对本轮搁置；其全 ROI baseline（FastSim 侧）已正常通过，故阻塞仅在 native 逐事件重采，不影响其余结论。

## 7. 未解决问题与下一步（不调常量拟合）

1. ~~裁定 L1 +1~~ **已完成：是端点口径相位差，issue→response 已对齐，不改常量。**
2. LBM mem 服务归属（首要）：核对 FastSim 请求起点与 native 在途父服务/接管 generation 的对齐，复现 227/142 两档；先做机制反例测试，再谈修复。
3. TeaLeaf/NAMD 需在 ROI **中部和靠近结束处**另取 4096 窗口配对，定位真正的累积分歧点；起点窗口结论不外推。
4. Graph500 native 重采需先解决 trace 体量（更短窗口或精简记录字段），再纳入配对。
5. 任何修复成立后，仍须用正式采集二进制重采，并在完整代表集（直至全矩阵）按 `first-core-target-common-end-v1` 验收，同时列相对与绝对 CPI 误差、MAPE 与 MAE。

本轮不产生模型改动、不调时延参数、不承诺 CPI 或吞吐改善幅度。
