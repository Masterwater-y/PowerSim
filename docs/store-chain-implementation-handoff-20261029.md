# Store 链路修复实施交接文档（2026-10-29）

本文给接手的 agent 从头继续工作使用。设计依据见 [store-chain-error-mechanism-and-design-20261029.md](store-chain-error-mechanism-and-design-20261029.md)（下称“设计文档”），历史背景见 [store-lifecycle-history-and-current-gap-20261028.md](store-lifecycle-history-and-current-gap-20261028.md)。

2026-10-29 接手更新见 [实施结果](store-chain-implementation-results-20261029.md)：已用 frozen gem5 冷流复现“CPU demand 已发送，年轻同 line prepare 仍先触发 I→SP_IE”的反例。下文 S1 两侧比例属于不同事件域，不能直接据此删除 NoAccess 屏障或认定其时序错误。V1 仍未验收，S2/S3 继续受停止条件约束。

**当前状态：S0 已完成；S1 进行到一半，已触发停止条件（Marian 机制指标严重偏离 gem5），因此不能下 CPI 结论。S2、S3 和 V3–V6 均未开始。** 代码中只新增了纯观测计数，没有任何时序改动，也没有 commit。

---

## 0. 接手前必读与硬约束

1. 先读 [AGENTS.md](../AGENTS.md)、[codebase-workflow.md](codebase-workflow.md)，以及 [accuracy-reporting-contract.md](accuracy-reporting-contract.md) 的第 0 节和第 8 节。
2. 用户约束（原话要点）：
   - 只做**通用机制修复**：禁止调参，禁止常数拟合，禁止按 workload 特判。
   - 保持 Bound–Weave 两阶段：一次 canonical 分类加一次 sparse 前向（`interval_reweave_passes=1`）；禁止 event_reference，禁止多轮 reweave 或 fixed-point。
   - S2/S3 的吞吐开销增量 ≤ 3%；bounded 的吞吐不得低于 legacy。
   - **停止条件**：
     - V1 机制指标偏离 gem5 时，不下 CPI 结论；
     - 任一 case 的 CPI 绝对误差变大超过 0.05 cycles/宏指令，且角色计数不能解释时，暂停分析，不调参；
     - 吞吐超标时先优化数据结构，不得降低证书精度来换吞吐。
3. 报告规范：
   - 使用中文。
   - 每个 case 同时给出相对误差和 **CPI 绝对误差** `|FastSim CPI − gem5 CPI|`（单位 cycles/宏指令）。
   - 聚合给出 CPI MAE、MAPE、WAPE 和 signed bias；MAPE 与 WAPE 必须区分。
   - native cache truth 只能来自 `tools/run_native_kernel_fastsim_validation.py`，禁止使用 `kernel-events*.json` 中 taotrace-path-class 的 cache 字段。
   - 用户偏好 LaTeX 公式、分级分析、物理路径解释。
4. 仓库是 ByteDance Codebase，不用 `gh`。用户未要求时不 commit；工作区里有大量与本任务无关的 `M` 文件，不要动。
5. 运行环境：
   - 运行 FastSim 和测试需要 `LD_LIBRARY_PATH=/opt/gcc-11/lib64`。
   - 验证要求：`cmake --build build -- -j16` 编译通过，并且 `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 通过。

---

## 1. 实验基础设施（全部位于 `tmp/store-bounded-ab-20261029/`）

| 文件 | 作用 |
|---|---|
| `run-arm.py` | 在六个冻结 case 上运行一个 arm。参数：`--arm NAME --binary PATH [--set key=value ...] [--cases ...] [--blocks CPU段...]`。cfg 为 `config.include=<tmp/spec-speed-32g-fastsim-validation-20261028/cases/<case>/fastsim.cfg>`，再加 `core.store_private_callback_timing=true` 和各 `--set`。输出写到 `runs/<arm>/<case>/`，并在 `stats/<arm>/<case>.json` 建符号链接。 |
| `score.py` | `--arms legacy bounded ...`：逐 case 调用维护中的 native validator（CPU 24–31），汇总输出 `comparison-<arms>.md/json`。legacy 的 stats 目录固定为 `tmp/marian-xz-service-repair-20261028/final-stats-on`。 |
| `obs-table.py` | `python3 obs-table.py <arm>`：打印 S1 观测计数与 gem5 L0 transition 的对照，并写出 `obs-<arm>.json`。 |
| `bin/fastsim-base` | sha256 `5c5b9b57…01f7`：S0 使用的二进制，不含观测计数。 |
| `bin/fastsim-obs` | sha256 `0e687d82…cbec`：S1 观测计数二进制；时序与 base 逐字段一致。 |
| `comparison-legacy-bounded.md/json` | S0 结果。 |

六个 case：`c04-801.xz_s c08-801.xz_s c04-872.marian_s c08-872.marian_s c04-817.flac_s c16-854.graph500_s`。gem5 来源为 `tmp/spec-speed-32g-c4-c8-c16-20261027-final/source/<case>/`，其中包含 `stats.txt`、`config.ini` 和 `tao_trace/manifest.txt`。

bounded arm 的典型命令：

```bash
python3 tmp/store-bounded-ab-20261029/run-arm.py --arm <name> --binary <bin> --set core.store_timing_model=bounded_analytic
python3 tmp/store-bounded-ab-20261029/score.py --arms legacy <name>
```

单次运行墙钟时间（bounded）：XZ C4 约 88 s，XZ C8 约 177 s，Marian C4 约 60 s，Marian C8 约 95 s，FLAC 约 107 s，Graph500 约 213 s。这些数字不是 ABBA 吞吐测量。

---

## 2. 关键代码位置（行号以当前工作树为准）

| 位置 | 内容 |
|---|---|
| [bounded_store_timing.cpp](../src/bounded_store_timing.cpp) 267 起 | `estimate_nonbinding_store`：`prepare=max(eligible,scan_ready)`；`send=max(eligible,prior_response+(prior_local?0:1))`；`send==prepare`→OriginalDemand；`!lease`→Reacquire；`send<permission`→Takeover（response=permission）；否则→Publish（send+publish_cycles）。其中 `permission=prepare+service`。 |
| [simulator.cpp](../src/simulator.cpp) ≈15936–15980 | bounded 的调用点、角色计数，以及新增的 `// Observation only` 块。 |
| simulator.cpp ≈16085–16122 | 普通 store 之后执行 `local_access=false`。NoAccess（`bound.local_no_access`）分支：`sq_release=max(retire+d+1, drain_ready+next)`，并设 `local_access=true` 和 `local_scan_ready_cycle=sq_release`。special store（多 fragment、serialize、barrier、locked）会设置扫描屏障。 |
| simulator.cpp 9973 | trace 中 `MemoryRequestRole::NoAccess` 在此变成 `local_no_access_uops`。 |
| simulator.cpp 7114 | `BoundedStoreFeedback`（已扩展观测字段）。 |
| simulator.cpp ≈18455 | 观测字段累加到 `stats_`。 |
| simulator.cpp ≈22878–22881 | 批末 `bounded_lease_valid`（`contains(line)`）和 `bounded_publish_cycles`（L1 命中取 1，否则取 L2 store_hit_latency=5）的快照，即 **S2/S3 的替换点**。 |
| [types.hpp](../include/fastsim/types.hpp) ≈3346–3354 | stats 字段（已新增 `bounded_store_obs_*`）。 |
| [main.cpp](../src/main.cpp) ≈4073–4095 | 在 `causal_frontier` 下输出（已新增 `bounded_store_obs_*` 数组）。 |

新增观测字段（JSON 位于 `causal_frontier`）：
- `bounded_store_obs_role[4]`、`_role_l1_miss[4]`、`_role_l2_miss[4]`：顺序为 OriginalDemand / Takeover / Publish / Reacquire。L1 miss 判定为 `private_level != kL1`，L2 miss 判定为 `private_level ∈ {kLlc, kUnknown}`。
- `bounded_store_obs_od_cause[3]`、`_od_cause_l1_miss[3]`：OriginalDemand 的成因，依次为 idle（prepare==eligible）、local（前驱是 NoAccess 扫描屏障）、other（special 屏障）。
- `bounded_store_obs_publish_miss_gap[4]`：L1-miss 的 Publish 中 send−permission 的分桶，依次为 <8、<64、<512、≥512 cycles。
- `bounded_store_obs_takeover_miss_wait_cycles`、`bounded_store_obs_miss_service_cycles`。

---

## 3. 设计步骤与状态总览

| 步骤 | 内容 | 状态 |
|---|---|---|
| S0 | 只把 `store_timing_model` 从 legacy 改为 bounded_analytic，做单变量 A/B | **完成**（第 4 节） |
| S1 | 先看机制指标：ρ、τ、Reacquire 与 gem5 对照 | **进行中，停止条件已触发**（第 5 节） |
| S2 | lease 视界取 $H_i=(\mathrm{seq}_i,\mathrm{seq}(\text{store}_{i+N_{SQ}}))$，$N_{SQ}=114$；L1/L2 都用 LRU distinct-count 证书。L1 驱逐→publish=5，L2 驱逐→Reacquire。替换 22878 处的批末快照。 | 未开始 |
| S3 | foreign write 按单条事件失效：只有 $H_i$ 内其他核的写/RFO 才使 lease 失效并走 Reacquire；只读共享不失效；prepare 始终照常发出 | 未开始 |
| S4 | 资源有界性靠证明（TBE=256 ≥ 179，ports=200 ≥ 116），不加 TBE 日历 | 已在 S0 支持域核对中确认 |
| V0 | 六项 bounded 冒烟 | **完成**：rc=0，validator passed，instruction_population_match |
| V1 | 机制指标向 gem5 靠近 | **未通过**（第 5 节） |
| V2 | native validator 复核 private L2/L1D miss（M7） | legacy 部分完成（第 4.2 节） |
| V3 | CPI A/B：legacy vs bounded，再加 S2/S3 时逐项开关 | 只有 S0 那一组；S2/S3 未做 |
| V4 | 微基准：冷 memset、同 set 超 8 路、双核同 line 写、空 SQ 单 store | 未开始 |
| V5 | held-out：同来源其余 c08/c16 的 xz/marian/flac/graph500 | 未开始 |
| V6 | 吞吐 ABBA（同机同负载）：bounded ≥ legacy；S2/S3 增量 ≤ 3% | 未开始 |

---

## 4. S0 结果（bin/fastsim-base；V0 已通过）

### 4.1 CPI

| case | gem5 CPI | legacy CPI | legacy 相对误差 | legacy CPI 绝对误差 | bounded CPI | bounded 相对误差 | bounded CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| XZ C4 | 0.758505 | 0.969584 | +27.83% | 0.211079 | 0.729716 | −3.80% | 0.028789 |
| XZ C8 | 0.673482 | 0.879998 | +30.66% | 0.206516 | 0.654812 | −2.77% | 0.018670 |
| Marian C4 | 4.933260 | 5.868488 | +18.96% | 0.935228 | 6.051448 | +22.67% | **1.118188** |
| Marian C8 | 5.189270 | 5.935064 | +14.37% | 0.745794 | 6.255695 | +20.55% | **1.066425** |
| FLAC C4 | 0.453030 | 0.478073 | +5.53% | 0.025042 | 0.478073 | +5.53% | 0.025042 |
| Graph500 C16 | 2.311802 | 2.066661 | −10.60% | 0.245141 | 2.096360 | −9.32% | 0.215442 |

| arm | CPI MAE | MAPE | WAPE | signed bias | P50/P90 APE | max APE |
|---|---:|---:|---:|---:|---:|---:|
| legacy | 0.394800 | 17.99% | 14.74% | +4.28% | 16.66%/29.25% | 30.66% |
| bounded | 0.412093 | 10.77% | 12.85% | +2.91% | 7.42%/21.61% | 22.67% |

结论：
- XZ 的误差基本消除，Graph500 略有改善。
- Marian 两项的 CPI 绝对误差分别变大 0.183 和 0.321，超过 0.05 门槛，触发停止条件，所以 S0 的 CPI 不能作为修复结论。
- **FLAC 在两个 arm 下 CPI 完全相同**（0.478073）。这一点尚未核实，需要确认 bounded 在 FLAC 上确实生效：S1 计数显示它有 646,585 个 estimate，看起来是生效的。CPI 完全相同可能说明 FLAC 的 store 不在关键路径上，也可能是 legacy 的 stats 来源与本轮不同，需要查明。

### 4.2 口径与 V2

- bounded 下 `l1d_prepare_misses=0`：prepare miss 被并入 `l1d_write_demand_misses`。这是 PMU 口径变化，不是模型变化，报告中必须说明。
- private L2 miss 相对 native 的误差：legacy 为 XZ +46/+62%、Marian +66/+67%、FLAC +5.6%、G500 −2.3%；bounded 为 +142/+123/+112/+111/+28/−2.3%。bounded 变大的部分属于口径变化。即便只看 legacy，XZ 和 Marian 也都超过 ±20%，按 V2 规则，这两类 case 的 store 结论都要标注“受分类偏差影响”（设计文档中的 M7）。

---

## 5. S1 结果（bin/fastsim-obs，arm=`bounded-obs`）

**时序确认**：bounded-obs 与 bounded 逐字段比对，唯一差异是 `causal_frontier.frontier_waits`。这个字段是线程队列等待计数（simulator.cpp 10191 `acquire_chunk`），与时序无关。

### 5.1 指标定义

- gem5（L0 = FastSim L1D）：

$$\rho^{g5}=\frac{\text{I.Store\_Prepare}}{\text{I.Store\_Prepare}+\text{I.Store}},\qquad \tau^{g5}=\frac{\text{SP\_IE.Store}}{\text{I.Store\_Prepare}}$$

- FastSim：设 $m_r$ 为角色 $r$ 中的 L1-miss 数，则

$$\rho^{fs}=\frac{m_{TO}+m_{PUB}+m_{RE}}{\sum_r m_r},\qquad \tau^{fs}=\frac{m_{TO}}{m_{TO}+m_{PUB}+m_{RE}}$$

- gem5 计数的 grep 前缀为 `board.cache_hierarchy.ruby_system.L0Cache_Controller.<State>.<Event>`，各列求和。

### 5.2 数据

| case | 角色 OD/TO/PUB/RE | L1-miss OD/TO/PUB/RE | OD 成因 idle/local（其中 L1 miss） | $\rho^{fs}$ | $\rho^{g5}$ | $\tau^{fs}$ | $\tau^{g5}$ | miss 平均服务 |
|---|---|---|---|---:|---:|---:|---:|---:|
| XZ C4 | 637k/10.2k/2256k/441 | 40062/2884/40786/1 | 357k(2955)/281k(37107) | 0.52 | 0.93 | 0.07 | 0.47 | 87.6 |
| XZ C8 | 1113k/21.3k/4461k/262 | 45443/5305/52130/21 | 835k(8203)/278k(37240) | 0.56 | 0.85 | 0.09 | 0.44 | 80.5 |
| Marian C4 | 816k/661/1016k/12 | **211707**/522/798/0 | 32.6k(3038)/783k(**208669**) | **0.006** | 0.98 | 0.40 | 0.995 | 237.8 |
| Marian C8 | 1432k/1425/1801k/28 | **372966**/1195/1136/0 | 51.4k(5185)/1380k(**367781**) | **0.006** | 0.97 | 0.51 | 0.995 | 256.6 |
| FLAC C4 | 316k/1206/329k/0 | 12015/1206/1382/0 | 316k(11994)/77(21) | 0.18 | 0.66 | 0.47 | 0.87 | 5.1 |
| G500 C16 | 7399/234/15627/0 | 379/211/535/0 | 7399(379)/0 | 0.66 | 0.33 | 0.28 | 0.50 | 6.6 |

Publish-miss 的 send−permission 分桶（<8/<64/<512/≥512）：XZ C4 为 1984/29234/9560/8，XZ C8 为 5501/32596/14033/0；Marian 的量很小。“other（special 屏障）”成因在六项中均为 0。

gem5 Marian C4 的参考值：I.Store_Prepare 234,824；I.Store 4,616；SP_IE.Store 233,704；IM.Store_Prepare 35,098；M.Store_Prepare 889,265；`lsq0.storePrepareIssued` 四核之和约 117 万；`storePrepareBlocked` 为 0。

### 5.3 诊断

**Marian（主要偏差，触发停止条件）**
- FastSim 中 98% 的 L1-miss store 被分成 OriginalDemand，原因是 local：前驱是一条 NoAccess store，模型把它当作 prepare 扫描屏障，并把 `local_scan_ready_cycle` 设为该 NoAccess 的 SQ 释放时刻，于是后面的 miss store 只能在队头串行支付约 238 cycles。
- gem5 中几乎每条冷 store 都被 prepare 接管（τ=99.5%）。
- 这条规则本身与 gem5 源码一致：`gem5-fs/src/cpu/o3/lsq_unit.cc` 974–1037 行的 `issueStorePrepareRequests` 遇到 `isLocalAccess()` 就 break。local access 由 `cda` 微操作产生（`ldstop.isa` 665 行，`Request::NO_ACCESS`）。它出现在 MOVNTDQ/MOVNTPS（`move_non_temporal.py`：`cda X+8; stfp X; stfp X+8`）、POP_M/POP_P、CALL far 和中断等处；MOVNTI 不用 cda。`writebackStores`（830–971 行）在队头遇到 local access 时立即 completeStore 并 continue。
- **矛盾**：如果 Marian 的写真是 MOVNTDQ 流，gem5 里应该有大量无 prepare 的 I.Store，实际只有 4,616 条。可能的解释如下（尚未验证）：
  1. local access 在队头完成很快（gem5 中 local access 一进入 WB 头就完成，屏障随即解除），之后 `issueStorePrepareRequests` 在同一拍、或在 miss store 到达队头之前就扫到了它。FastSim 中屏障一直持续到 `local_scan_ready_cycle = NoAccess 的 sq_release`，而 sq_release 又被 `drain_ready + next`（即前一条 miss 的完成）推后，这可能把“前驱 miss 完成”错误地传递成了“后续 store 的 prepare 也必须等到那时”。gem5 中每个 store 的 prepare 在它前面所有 store 都 canWB 时就能发出；local access 只在**仍位于 SQ 中且未完成**时才挡住扫描，而它要等到成为 WB 队头才完成。所以在 `cda; stfp; stfp` 序列里，两条 stfp 的 prepare 确实要等 cda 到达队头，也就是等前一组 stfp 的 miss 完成。这一点需要逐拍核对。
  2. trace 中的 NoAccess 角色或位置与 gem5 实际执行不符，例如 NoAccess 记录在 FST 中的排序或归属有问题。
  3. gem5 中 cda 之后的 stfp 访问的是同一 line（X 与 X+8），前一组 stfp 已经让后续 line 处于 M.Store_Prepare 状态，即 prepare 远在更早时就已发出。FastSim 的屏障则按 store 粒度串行化。
- 需要的证据：
  - Marian 的 NoAccess 对应的宏指令、PC 和地址。FST mreq 文件为 `tao_trace/*.micro.fst.mreq`，头部格式 `<8sIIIIQQQII`，记录格式 `<QIIB7s`（ordinal、?、?、role），role=1 表示 NoAccess；可参考 [fst_memory_request_roles.py](../tools/fst_memory_request_roles.py)。
  - NoAccess 与其后 miss store 的间距分布。
  - 若可行，用 gem5 的 `LSQUnit` debug trace 跑一个小区间，看 cda/stfp/prepare 的逐拍时序。

**XZ（次要偏差）**
- 约 4 万条 L1-miss store 走了 Publish，其中多数在 permission 到达后 8–64 cycles 才发出；gem5 里约一半是被接管的。
- 这说明 FastSim 中 prepare 相对队头到达偏早，或者服务时延偏短。候选原因：M5 的冻结 canonical 服务时延，或者 eligible/canWB 时刻有偏差。
- 另外约 3.7 万条 miss 因 local 屏障成为 OriginalDemand，与 Marian 是同一机制。

**Graph500**：$\rho^{fs}$ 为 0.66，高于 gem5 的 0.33，方向相反；但 store 量很小，不能把它的 −9% CPI 误差归因于 store。

**FLAC**：$\rho^{fs}$ 为 0.18，gem5 为 0.66；FastSim 的 OD 几乎全是 idle 成因（SQ 空闲时 prepare 与 send 同拍，按定义直接算作 OriginalDemand）。gem5 中，即使 SQ 空闲，队头也会先发 prepare 再发 demand 吗？需要核对 `writebackStores` 与 `issueStorePrepareRequests` 的先后：前者先发队头 store 并设 storeInFlight，后者从 SQ 头扫描时会跳过已 committed 的队头。按源码，空 SQ 的单条 store 不会发 prepare，所以 I.Store 应该占多数。FLAC 中 gem5 的 I.Store 为 10,567（34%），FastSim 的 OriginalDemand miss 占比为 82%，偏差方向与 XZ 相同。

---

## 6. 下一步（按顺序）

1. **解开 Marian 的矛盾**（第 5.3 节的假设 1–3）：
   - 用 FST mreq 统计 NoAccess 对应的宏指令类型，以及 NoAccess 到下一条 miss store 的距离；
   - 逐拍推演 gem5 中 `cda; stfp; stfp` 流的 prepare 时刻，并与 FastSim 的 `local_scan_ready_cycle` 语义对照；
   - 必要时在 gem5 上跑一个 MOVNTDQ 冷流微基准（V4 的一部分），读取 I.Store_Prepare、I.Store 和 SP_IE.Store。
2. 若确认 FastSim 的 local 屏障语义与 gem5 不一致（例如屏障解除时刻应为 NoAccess 到达 WB 队头的时刻，而不是被 drain_ready 推后的 sq_release），就做通用修复，修改 simulator.cpp ≈16095–16110 处的 `local_scan_ready_cycle` 定义。修复后重跑 bounded-obs 和 S1 表格，要求 V1 通过：ρ、τ 向 gem5 靠近，且 Graph500 不被推高。
3. 分析 XZ 的 Publish 与 Takeover 偏差（M5 服务时延，或 canWB 时刻）。仍然只做机制修复。
4. V1 通过后，重做 V3（score.py，legacy vs 修复后的 bounded），给出完整的 CPI 表和 MAE/MAPE/WAPE/bias。
5. 实施 S2（视界 + LRU 证书，替换 simulator.cpp ≈22878 处的快照）和 S3（foreign write 按单条事件失效），各自作为独立开关做 A/B，并输出视界截断占比和非 LRU 退化计数。
6. V4 微基准，V5 held-out（同来源其余 c08/c16 的 xz/marian/flac/graph500），V6 ABBA 吞吐（bounded vs legacy，以及 S2/S3 的增量）。
7. 结果写成新文档 `docs/store-chain-implementation-results-<date>.md`，用中文向用户汇报。未经用户要求不要 commit。
