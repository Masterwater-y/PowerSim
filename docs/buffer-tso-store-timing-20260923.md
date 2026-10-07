# Buffer TSO：普通 store 发送与本地 callback 时序修正

> **2026-09-24 归因更正：** 当前结论以[实验汇总与下一步取证](buffer-tso-cpi-diagnosis-next-steps-20260924.md)为准。
> 下文 §8.4 的串行消融不能证明全部 CPI 差来自 MLP，§8.5 的 alias 缺口也未证明是主要 CPI 根因；
> store 模型内未覆盖服务估算不能排除所有资源竞争的间接影响。历史结果保留，强归因结论不再沿用。

> MSHR 后续验证：[共享响应、资源归属与局部性首轮验证](buffer-tso-mshr-validation-20260923.md)。
> 该验证纠正下文 8.6 的“仅 L1 miss 建 Sequencer 表”及“单趟必然逐条累加等待”的泛化：
> native 存在大量 L1-hit 父请求合并；已复现的重复等待与容量反压后未重新判定 alias 有关。
> 现有受限服务引用在 5 个完整 ROI 上收益接近零，不能视为完整 MSHR 修复；历史记录保留。
> 新增[有界服务事务实现与验证](buffer-tso-service-repair-20260923.md)及
> [FR-FCFS 接入](buffer-tso-service-frfcfs-20260923.md)：统一准入/PMU/响应及回滚已落地，默认关闭；控制器已加入事务，native 覆盖仍很低，完整高吞吐方案未完成。
> 后续新增可选的[不回退实验](buffer-tso-service-no-fallback-20260923.md)：两负载全部非空访存 epoch 提交，预测明显变化；整体 CPI 误差与吞吐均退化，保持默认关闭。
> 9 月 24 日新增[局部回退选项](buffer-tso-service-local-fallback-20260924.md)：按请求降级并保留其他合并，参考实现验证通过；仍受整批重算和预算限制，未取得 native 精度收益。

本次修正上一轮审计已经定位的三条边：commit→首次发送、本地 cache→store callback、前驱响应→下一条 FIFO 发送。验收依据是 gem5 源码顺序、采集配置和逐事件证据；不按 CPI 是否改善决定保留或撤销机制。

前置证据：[首轮机制审计](buffer-tso-mechanism-audit-20260923.md)。现有真实队头 demand 的实现与边界：[demand-only 报告](buffer-tso-head-demand-only-20260923.md)。本次仍不增加资源调度或扩大 epoch 回退。

## 1. 实现

仍由 `core.buffer_tso_prepare_lifecycle=true` 启用，默认 OFF。对普通 store：

```text
earliest_admission = retire + store_commit_to_iew_delay + 1
send = max(earliest_admission, predecessor_actual_response + 1)
```

`commitToIEWDelay` 来自 gem5 配置。额外的一拍来自 `IEW::tick` 先调用 `writebackStores()`、后处理 commit 通知并调用 `commitStores()` 的源码顺序；不是拟合参数。`completeStore()` 在响应时清除 `storeInFlight`，后继在下一次 IEW tick 才能发送。

`cache.l1d.store_hit_latency` 和 `cache.l2.store_hit_latency` 明确表示普通 store 的本地 admission→callback 服务。preview 与正式 replay 使用同一个选择函数。load、atomic、页表访问、I-side 不使用这两个参数。无法 optional prepare 的普通 store 同样遵守发送边和本地 store 服务；split/跨核/特殊指令的原有 prepare 排除规则保持不变。

prepare 的估算 ready 下界同步改为 `earliest_admission + 原服务`；原队头/prepare follower 的判定使用该最早发送边与 FIFO 前驱响应边比较。在途 prepare 仍由 demand 接管响应，已经 ready 的 follower 仍执行现有真实本地 write replay。SQ 在实际 demand 响应时释放。

没有启用 `store_post_commit_request`：该实验还会改变 replay 顺序和资源日历，超出本次范围。没有增加 prepare/demand 的资源请求计账，没有引入新回退。

## 2. 参数来源与失配拒绝

生命周期模式现在必须显式提供 `core.store_commit_to_iew_delay`、`cache.l1d.store_hit_latency`、`cache.l2.store_hit_latency`；缺少任一项即拒绝运行。参数写入 stats 的 `configuration`，不能再静默借用 generic/load latency。旧版本配置单独开启此开关时，需要补齐这三个字段。

维护配置 `configs/gem5-fs-native-kernel.cfg` 提供当前 native target 的值；它本身不启用 lifecycle 实验。历史 versioned 配置未改动。

| FastSim 边/字段 | 当前值（core cycles） | 同次采集 gem5 来源 |
|---|---:|---|
| `store_commit_to_iew_delay` | 1 | O3 `commitToIEWDelay=1` |
| commit→最早发送 | 2 | 上述通知延迟 1 + 下一次 `writebackStores` tick 1 |
| L1D `store_hit_latency` | 1 | Ruby L0 `mandatory_queue_latency=1`；`hh_store_hit`→`writeCallback`→RubyPort 同 tick response→LSQ `completeStore` |
| private L2 `store_hit_latency` | 5 | Ruby L0 mandatory queue 1 + L0 `request_latency=2` + Ruby L1 `l1_response_latency=2` |
| 前驱响应→后继最早发送 | 1 | response 清除 `storeInFlight` 后的下一次 IEW tick |

gem5 的 Ruby L0/L1 在此分别对应 CPU private L1/L2。普通 store 不经过 load consumer writeback，因而不能继续用包含该边的旧 L1=2、L2=6 作为 store 本地服务。

本轮逐案例、逐核心核对 8 个原始 `config.ini`，覆盖 76 个核心；上述参数、buffer=114、TSO 模式一致，CPU/Ruby 时钟均为 333 ticks/cycle。`store-parameter-provenance.json` 保存字段、section、源文件散列与映射。硬件身份仍单独通过维护 identity validator 验证。

## 3. 验证范围

定向测试先在旧实现上因 store 过早发送而失败，然后通过以下检查：

- configurable commit 通知延迟：使用非目标值 3，发送下界随之变成 4，防止硬编码样本数字。
- FIFO 前驱真实响应后至少一拍发送，覆盖跨 epoch；prepare ready 不早于本条最早 admission 加服务。
- generic L1=9、store L1=1 时，prepared demand 的实际服务为 1。
- 两条 warm line 在仅容纳一条 line 的 L1 中交替访问，64 次普通 store 均以 private L2 服务 5 完成；即使 prepare 不安全，也使用正确的 store 路径。
- 相同 L2 场景中，load/atomic 保留 generic 服务 11，开关 ON/OFF 的周期和请求数相等。
- 原有 fence drain、SQ=1/2/8/114、在途接管、warmup、有限 Sequencer、计数守恒及审计中性测试继续执行。

`cmake --build build -- -j16` 和 `./build/fastsim_tests` 通过。相关 Python validator/配对/采集测试 61 项通过。只读独立代码审查未发现 P1/P2 问题。

生命周期 OFF 的等价检查另外完成 8/8：保持 residence certificate=ON，使用相同 `off.cfg` 分别运行修改前冻结二进制与新二进制。两组 native validator 均 8/8 passed/PMU scored、0 failed/pending。除新增且不生效的配置字段外，原有效配置相同；scope 指标、线程、全部核心字段、target totals、CHA、pending-fill 和目标 causal-frontier 字段完全相等。仅排除宿主 wall timers/throughput，以及 `acquire_chunk()` 的宿主线程 `frontier_waits`、`max_resident_chunks`。没有把历史 residence/lifecycle 同时 OFF 的不同配置当成对照。结果见 `off-equivalence.json`、`off/`、`off-control/`。

## 4. 尚不能据此宣称什么

这三条边的修正不等于完整 prepare/demand 协议已经认证。仍有：

1. prepare ready 使用 canonical service 和 post-commit 下界估算；native 日志还缺逐 store optional prepare 的 admission/response/drop/takeover 身份。
2. optional prepare 与 mandatory demand 仍沿用现有资源计账，尚未与 gem5 分别对齐。
3. 额外 demand 使用 canonical batch 的缓存状态；Graph500 的已知 L1/L2 层级选择分歧没有因此自动消失。
4. LLC/DRAM/coherence 路径保留原有近似；本次独立服务参数针对本地 L1/private L2，不认证所有长服务路径。

配对报告继续保留 `full_lifecycle_verified=false`。TeaLeaf、Graph500、NAMD 的整体低估需要继续用缓存驻留及 load/依赖/retire 见证定位，不能用统一 CPI 补偿项填平。

实验产物根目录：`tmp/buffer-tso-store-edges-20260923/`。完整 ROI 仍使用原 8 案例 source、record-bounded functional warmup、user-plus-kernel 和 first-core common-end，每端使用自己的 macro-instruction 分母。它是既有尾部案例的诊断小集，没有新的校准/held-out 划分，也不构成吞吐量比较。

## 5. 完整 ROI 小集结果

维护 native validator：8/8 passed、8/8 PMU scored、0 failed/pending；独立硬件身份 8/8 valid。每项 CPI instruction-population gate 通过，逐核心 instructions/uops/memory/branch 人口与修正前相同。`passed` 仅代表来源与守恒门，不是精度阈值或硬件 PMU 认证。所有案例 canonical fallback epochs=0，LBM C16/C32 均正常结束。

修正前为上一版 demand-only 的冻结结果。CPI 绝对误差及 MAE 单位均为 cycles/macro instruction；相对误差带符号。MAPE/分位数配置等权，Type-7 分位数；WAPE/bias 使用 scope 内退休宏指令数加权。未用新 CPI 结果调参。

| 案例 | gem5 CPI | 修正前 CPI | 前相对误差 | 前 CPI 绝对误差 | 修正后 CPI | 后相对误差 | 后 CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c04-782.lbm_r | 0.817326 | 0.775517 | -5.12% | 0.041809 | 0.776815 | -4.96% | 0.040511 |
| c08-782.lbm_r | 0.822077 | 0.755658 | -8.08% | 0.066419 | 0.756651 | -7.96% | 0.065426 |
| c16-782.lbm_r | 0.951678 | 1.051002 | +10.44% | 0.099324 | 1.051709 | +10.51% | 0.100031 |
| c32-782.lbm_r | 1.502989 | 1.760671 | +17.14% | 0.257681 | 1.756115 | +16.84% | 0.253126 |
| c04-706.stockfish_r | 0.668626 | 0.662194 | -0.96% | 0.006432 | 0.661987 | -0.99% | 0.006639 |
| c04-811.tealeaf_s | 1.004183 | 0.860937 | -14.26% | 0.143247 | 0.860056 | -14.35% | 0.144128 |
| c04-854.graph500_s | 1.305529 | 1.180965 | -9.54% | 0.124564 | 1.179437 | -9.66% | 0.126092 |
| c04-857.namd_s | 0.479341 | 0.389498 | -18.74% | 0.089843 | 0.389645 | -18.71% | 0.089696 |

| 范围/版本 | MAPE | CPI MAE | P50 APE | P90 APE | P99 APE | 最大 APE | WAPE | signed bias |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 全部 8 项/修正前 | 10.54% | 0.103665 | 9.99% | 17.62% | 18.63% | 18.74% | 13.96% | +9.26% |
| 全部 8 项/修正后 | 10.50% | 0.103206 | 10.08% | 17.40% | 18.58% | 18.71% | 13.80% | +9.11% |
| LBM 4 项/修正前 | 10.19% | 0.116308 | 9.26% | 15.13% | 16.94% | 17.14% | 14.42% | +12.53% |
| LBM 4 项/修正后 | 10.07% | 0.114773 | 9.23% | 14.94% | 16.65% | 16.84% | 14.22% | +12.36% |
| TeaLeaf/Graph500/NAMD C4/修正前 | 14.18% | 0.119218 | 14.26% | 17.85% | 18.65% | 18.74% | 12.92% | -12.92% |
| TeaLeaf/Graph500/NAMD C4/修正后 | 14.24% | 0.119972 | 14.35% | 17.84% | 18.63% | 18.71% | 13.00% | -13.00% |

整体改变量很小。TeaLeaf、Graph500 和 NAMD 三项 MAPE/MAE 合计略退化；LBM C16 也略退化，均保留。流水线发送边原先少算、store 服务原先多算，两类误差方向相反；修正阶段语义并不意味着端到端 CPI 必然大幅改善。不能从本表把剩余误差都归因于 store，也不能据此宣布完整协议正确。

## 6. 同一批 native store 的复核

复用上一轮的短窗口 native collection，重新运行新二进制的审计 ON/OFF，分别经维护 validator `--stats-dir` 重评分绑定输出散列，再运行 `audit_buffer_tso_lifecycle.py`。三个窗口均通过源/硬件/前缀身份、native hierarchy/drain 守恒、输出绑定和审计中性门。配对仍限 core 0，native 重采二进制与原始收集二进制不同的诊断限制不变；没有把短窗口替换成完整 ROI CPI 参考。

| 窗口 | store 数 | 核对前缀记录数 | 新 FastSim 原队头 / 在途接管 / 额外 demand | 本地服务证据 |
|---|---:|---:|---:|---|
| NAMD C4，offset 0–4096 | 64 | 647,676 | 64 / 0 / 0 | 62 条 L1：两端均 1；2 条 L2：两端均 5 |
| Graph500 C4，offset 0–4096 | 342 | 11,396,519 | 335 / 0 / 7 | 341 条 L1：两端均 1；首条层级仍不同 |
| TeaLeaf C4，offset 74433–78528 | 76 | 2,096,205 | 34 / 0 / 42 | 67 条 L1：两端均 1；其余 9 条长服务仍不同 |

全部 482 条新 FastSim 观测满足：`send=max(retire+2, FIFO 前驱真实 response+1)`、`release=send+service`，以及 prepare estimate 的 post-commit 下界。49 条在 FastSim 被 FIFO 前驱阻塞的样本均在前驱响应后 1 拍发送。native 相邻样本中，Graph500 4 条、TeaLeaf 33 条因前驱响应阻塞的发送间隔也均为 1 拍。两端的阻塞人口不同，不能将这个边的检查误报为全部 admission 时间或角色已经逐条相等。

NAMD 64 条的 commit→admission 现在均为 2，旧 FastSim 均为 0；本地服务从旧 62×2、2×6 改为 62×1、2×5。**这批已知阶段差异得到消除。** 但窗口末端 FastSim retire-from-ROI 仍为 1088，native active 为 1194，仍提前 106 周期；局部 store 阶段一致没有消除其他关键路径误差。

Graph500 首条 store（ordinal `11392422`、PC `4232699`、PA line `3744960`）仍为 native private-L2 hit 5、FastSim L1 hit 1。此次改正了所选路径的服务定义，没有修复为何两端选择不同层级。不能给这条 L1 hit 人为补 4 拍。

TeaLeaf 的 42 次额外 demand 已使用自己的 1 拍 L1 callback；9 条非 L1 长服务仍有明显差异，例如 native 185、FastSim 257。窗口末端仍为 FastSim 17736、native active 19286，提前 1550 周期。需要继续追缓存驻留、load/依赖和 retire 事件，不能把该进度差全算到 store。

逐条证据见实验目录 `paired-*.json`、`stage-evidence.json`、`stage-check.log`。脚本没有重新选择 CPI/cache oracle：CPI 来自维护 native validator，逐请求 native 事实来自维护配对审计。

## 7. 复现

本轮冻结二进制 `fastsim-store-edges` SHA-256：`0637ba3c7c0c3d65d544d48a6ca89800f1d269768a0da074bea4e86b871b57f5`。`before/` 保存本轮前文件，`changes.diff` 只包含本轮范围；没有回滚其他工作区修改。

```bash
cmake --build build -- -j16
./build/fastsim_tests

python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/buffer-tso-store-edges-20260923/source \
  --fastsim tmp/buffer-tso-store-edges-20260923/fastsim-store-edges \
  --config tmp/buffer-tso-store-edges-20260923/dual.cfg \
  --output-dir tmp/buffer-tso-store-edges-20260923/dual \
  --expected-cases 8 --jobs 1 --max-attempts 1
```

审计配置 `audit-*.cfg` 固定与上一轮相同的窗口。`run-followup.py` 保留三例 replay、审计/控制重评分及完整 native 配对命令；`summarize.py` 仅汇总维护 validator 的 CPI，`summarize-stages.py` 检查已配对观测的三条时序边。所有长路径近似和完整 lifecycle 未认证标记继续保留。

OFF 对照使用 `tmp/buffer-tso-mechanism-audit-20260923/fastsim-audit`（SHA-256 `5db76173f2248ef7d1d4d484f8d61a9f43fba81373fc6d0c048cedd59f59ce3f`），与新二进制均配 `off.cfg`，分别写入 `off-control` 和 `off`；其余 validator 参数与上述 8 例命令相同。`check-off.py` 核对两组有效配置与目标机结果。`configuration-provenance.json` 保留本轮 13 个 include-chain 文件的正文与散列。未提交或推送本轮工作。

## 8. 全 ROI 取证（2026-09-23）：否定"层级选择"，定位到 SQ 反压墙钟

此前第 4–6 节以及 Graph500/TeaLeaf 短窗口分析把剩余低估主要归到"L1/L2 层级选择分歧"。该结论只建立在几十到几百条局部指令上。本节改用 env 门控埋点 `FASTSIM_TSO_PREPARE_HIST`（默认 OFF、对生产零影响）对**完整 NAMD c04 ROI** 逐条取证：4 核共记录 **2,191,997 条** buffered store，字段含 core/sequence/ordinal、role、prepare feedback 的 `private_level`/`shared_timing.path`/`latency_cycles`、实际 store 记账时延、retire/send/sq_release。原始 CSV：`tmp/prepare-hist-20260923/namd-c04.csv`。

### 8.1 非本地 prepare 极稀少，且服务几乎全被 SQ 等待覆盖

| prepare 私有层级 | 数量 | 占比 | prepare 反馈时延合计 | 实际 store 记账合计 |
|---|---:|---:|---:|---:|
| L1 | 2,145,227 | 97.9% | 2,164,027 | 2,157,453 |
| L2 | 30,383 | 1.4% | 161,563 | 80,935 |
| LLC | 16,387 | 0.7% | 2,880,404 | 173,583 |

表面 LLC prepare 反馈 2.88M、只记账 0.17M，似乎等于缺失周期。按 role 拆开并问"服务是否已被本条 store 在 SQ 的自然等待 `send−retire` 覆盖"后，结论反转：

| role（LLC prepare） | 数量 | prepare 时延 | 已记账 | send−retire 等待合计 | **未被覆盖净缺口** |
|---|---:|---:|---:|---:|---:|
| 1 头部直发 demand | 291 | 56,324 | 56,324（全额） | 582 | 0 |
| 2 在途 prepare 接管 | 1,146 | 261,517 | 102,309 | 161,500 | ~100,098 |
| 3 发送时权限早已就绪 | 14,950 | 2,562,563 | 14,950 | 3,910,302 | **0**（0/14950 未覆盖，等待中位数 223 ≫ prepare 时延） |

非本地 prepare 的**真实净缺口 ≤ ~156K 周期**，只能解释 NAMD 缺失 2,462,656 周期的约 6%。**"prepare 走 L2/LLC 未建模"假设被否定。** role3 的 demand 走 L1=1 是正确的，不缺服务时延。

### 8.2 更正：gem5 后端不反压，退休由 ROB 头部就绪时延决定

> 更正说明：本节初稿曾根据 FastSim `sq_stall_cycles`=25.9M 与 gem5 rename Blocked 的对比，断言"SQ 反压事件→墙钟映射错误"。该对比不成立——FastSim 的 stall 计数是重叠记账的尝试量，不进墙钟；gem5 真实状态机显示后端几乎从不反压。以下为复核后的硬证据。

gem5 `stats.txt`（core0，占 3,270,478 周期）：

| 状态机 | running | blocked | 其它 |
|---|---:|---:|---|
| commit.status | **94.53%** | — | trapPending 5.47% |
| iew.dispatchStatus | **98.68%** | 0.25% | unblocking 0.32%，squashing 0.75% |
| rename.status | 57.23% | **1.44%** | Idle 32.96%，Unblocking 6.30% |

- LSQ/IQ 容量事件虽有计数（lsqFullEvents 9804、iqFullEvents 446、rename SQFullEvents 28972），但 dispatch/rename **Blocked 占比仅 0.25%/1.44%**，即这些事件不形成显著墙钟反压。
- 退休指令数两端一致：gem5 core0 commit `numInsts`=6,869,749，FastSim core0 `instructions`=6,865,138，差 4,611（0.07%，ROI 边界微差，可忽略）。**2.46M 周期差不是指令数差异，而是每条指令的墙钟时延差异。**
- gem5 每周期退休 6,869,749/3,270,478 ≈ **2.10 条**，远低于 `commitWidth=8`。既非宽度受限、也非容量反压，唯一可能：**ROB 头部指令尚未就绪**——退休速率由长延迟操作（load/FPGA/DTLB walk，带 MLP）的就绪时间决定。

### 8.3 修正后的结论与下一步取证方向

1. NAMD 剩余 **−18.7%** 低估，既不是访存 store 服务边（8.1 已证 ≤6%），也不是 L1/L2 层级选择，更不是 SQ 反压墙钟（gem5 后端 94–99% running）。
2. 它落在**退休指令流的就绪关键路径**上：FastSim 让 ROB 头部（或其依赖链）比 gem5 更早就绪，从而在同样的指令数下用更少周期退休。候选来源：load 数据就绪时延、DTLB walk 暴露量（FastSim 记 dtlb_walk 274K/核）、长延迟 FPU/依赖解析、以及这些延迟与 MLP 的叠加方式。
3. 旁证：FastSim 每核 `fetch_block_response_wait`≈0.67M，gem5 `icacheStallCycles`=0.82M（FastSim 前端等待也偏短），但前后端账需分开，不能直接相减。
4. 下一步取证（仍不调参）：在退休流上按 ROB head 逐条记录"该 head 为何周期才就绪"的关键边（load completion / DTLB / FU 依赖），统计其未被并行掩盖的净等待，定位是哪一类就绪时延被压缩。可用现有 env 埋点机制扩展，默认 OFF。

本节为取证中间结论，已撤回初稿的"SQ 墙钟"错误论断；在 8.3 第 4 步取证完成前，不据此改动生产时间模型。

### 8.4 决定性消融：根因是 MLP（并发 load miss）被过度重叠

> **此标题及下文“根因已证明”的判断已撤回（2026-09-24）。** 实验仅支持并发敏感性。
> 从同一个串行 CPI 分别减去两个模型的 CPI，再比较两项，必然得到原 CPI 差；这不是因果归因。
> 当前需要逐请求准入、响应、依赖与退休的配对证据，见[归因边界](buffer-tso-cpi-diagnosis-next-steps-20260924.md#5-cpi-归因事实假设与更正)。

判别实验（不改生产、不调参）：临时将 `ruby.sequencer_max_outstanding` 由 16 设为 1，强制同一核一次只有一个 Ruby load 请求在飞，其余结构不变（`tmp/prepare-hist-20260923/nomlp.cfg`）。

| NAMD c04 配置 | CPI | 相对 gem5 误差 |
|---|---:|---:|
| gem5 | 0.479341 | — |
| FastSim sequencer=16（生产） | 0.389645 | −18.7% |
| FastSim sequencer=1（去 MLP） | **0.8253** | +72.2% |

量化（以"完全串行"0.8253 为基准）：

- FastSim sequencer=16 的 MLP 收益 = 0.8253 − 0.3896 = **0.436** CPI；
- gem5 真实 MLP 收益 = 0.8253 − 0.4793 = **0.346** CPI；
- **FastSim 比 gem5 多重叠 ≈ 0.090 CPI，与缺失量 0.4793−0.3896=0.090 几乎完全相等，方向一致。**

判定：NAMD 剩余 −18.7% 低估的主因是 **FastSim 高估了 load miss 的 MLP 重叠**——让过多在 gem5 中不（完全）并发的 miss 并发完成，从而过度压缩 ROB head 的就绪等待。store 服务边、层级选择、SQ 反压均已排除。

定量注意：sequencer=1 是把所有 Ruby 请求（含 L1 hit）一并串行的极端情形，"0.090 全部由 MLP 解释"在该基准下成立；但要落到代码，仍需找出 FastSim 相对 gem5 **多放行并发**的具体条件，而不是简单把 sequencer 改成更小的值（那会同时误伤正常并发，且属于调参拟合，禁止）。

已排除容量上限差异：gem5 Ruby L0（私有 L1）sequencer `max_outstanding_requests=16`、Ruby L1（私有 L2）=64；FastSim=16，与 L0 一致，没有放宽总 outstanding。因此过度 MLP 来自**并发准入条件**（哪些 miss 被允许同时在飞），不是容量数字。

下一步：逐条对比一个真实 miss 窗口内两端实际在飞 miss 的世代/就绪关系，定位 FastSim 多并发的准入边（怀疑在 sequencer/MSHR 对 L2-hit 短 miss 的复用或地址/依赖就绪判定）。

### 8.5 定位到具体准入边：gem5 的 line-alias 合并在 buffer 模式被整段禁用

直接对比同一次 gem5 采集的 native 计数（`oracle/native-summary-core0.json`）与 FastSim 生产结果，差异落在"对**已在飞行 miss 行**的重复访问"上：

| core0 指标 | gem5 (native) | FastSim 生产 |
|---|---:|---:|
| ruby aliased admission fragments | 491,263 | 0 |
| sequencer coalesced fragments | 490,004 | 0 |
| `ruby_sequencer_line_coalescing` | （gem5 原生行为） | **false** |

gem5 Ruby sequencer 语义：一条 miss 未回调时，同一 cache line 的后续 demand 不发新的 hierarchy 请求，而是挂到父请求（alias / coalesce），在父请求 readCallback 时一起完成。这意味着这些"第二次以后"的访问，其完成时间被**父 miss 的真实回调**钳住，而不是再享受一次独立的短服务。NAMD core0 约 49 万次 admission（占总 admission 约 23%）属于此类。

FastSim 的对应机制 `replay_sequencer_line_alias()`（simulator.cpp:21594）确实存在，但入口在 :21598 直接判定 `!config_.ruby_sequencer_line_coalescing || !sequencer_line_runtime_enabled_` 返回 `nullopt`；而该 runtime 仅在一条独立的 functional-replay 多趟修正路径里使能（:18207），该路径与 buffer TSO 互斥（启动校验在 :1466、运行期 `sequencer_line_runtime_enabled_=false` :18047）。因此在 buffer-114 生产配置下，这些本应 alias 到在飞父 miss 的访问被当成**全新独立请求**，各自分到一个私有层级并按命中时延（L2-hit 恒为 6 周期）完成。

机制后果（与 8.4 的过度 MLP 方向一致、且给出了具体来源）：

- 对一个长 miss（LLC/DRAM）行的重复 load，在 gem5 中必须等父长 miss 回调；在 FastSim 中却以 6 周期 L2-hit 提前返回，等于让"挂在长 miss 上的等待"凭空消失。
- 这些提前返回的 load 提前释放 sequencer/MSHR 槽并提前唤醒 ROB head，等价于多放行、多重叠并发——正是 8.4 消融测到的额外 ~0.090 CPI 的来源方向。

修复方向（机制接入，非调参、非简单开开关）：

1. 在 buffer TSO 路径维护"在飞 miss 行 → 父请求（层级、callback 周期、generation）"的有界驻留表，使对在飞同线的访问走 alias 完成（沿用 gem5：read follower 合并；首个 write 在 readCallback 后重发一个本地 L0 边）。
2. 父 callback 必须来自该父请求在 buffer 路径的**真实服务完成**（同次 replay 的层级/时延），不得借用 load 时延或统一加常数。
3. 按 generation 处理行失效/重取；表项有界，逐出时正确失效，避免 L2-only `contains()` 真而 `mark_dirty()` 只改 L1 的旧缺口。
4. 保留回退：任何开关 OFF 与当前生产 bit-identical。先在 NAMD c04 反例上验证 alias 计数与完成时刻向 gem5 靠拢，再跑 Graph500 / lbm 与全 39 案例。

注：现有 `ruby.sequencer_line_coalescing=true` 直接在 buffer 配置上会被启动互斥校验拒绝（`core.tso_store=buffer cannot be combined with ... Sequencer admission experiments`），证明不能靠打开旧开关复用，必须在 buffer 路径内重新接线。

### 8.6 反证：单趟 buffer 路径直接接线不成立（欠合并 + 过串行）

8.5 把准入边定位正确（line-alias 确为差异来源），但低估了该机制对**多趟收敛**的依赖。本节给出在 buffer 单趟路径内直接接线的完整实现与全 ROI 反证。状态：诊断/实验（diagnostic），默认 OFF、未改生产。

**实现（已编译，`fastsim_tests` 全过）**

- 新增独立开关 `ruby.buffer_line_coalescing`，复用现有 `sequencer_line_requests_` /
  `sequencer_line_expiry_` 表，走 buffer 单趟路径，不触发被禁的 functional 多趟 epoch。
- 修正 1：父表项只在**真实 L1 miss** 建立（`track_sequencer_parent` 增加
  `private_level != kL1` 门）。gem5 仅在请求 miss L1、向下发行时才占 sequencer 行表；
  若不修正，连 L1 hit 也建表，core0 合并数虚高到 3.68M。
- 修正 2：在 buffer 权限回滚（`:21291`）与 page-walk 回滚处快照/恢复该 timing-only
  行表与 `stats_.sequencer`，保证回滚一致。
- 修正 3：certified buffer head demand（`buffered_head_demand=true`）跳过 alias 入口。
- OFF 时与当前生产 bit-identical。

**全 ROI 结果（NAMD c04，口径 `perf_like_cpi_user_plus_kernel`，gem5=0.479341）**

| 配置 | CPI | 相对 gem5 | core0 合并数 |
|---|---:|---:|---:|
| gem5 | 0.4793 | — | 491,263 |
| FastSim 生产（buffer-114） | 0.3896 | −18.7% | 0 |
| 单趟 alias 接线 | **1.0808** | **+125.5%** | 155,088 |

| core0 接线后 | 值 |
|---|---:|
| 合并数 | 155,088（gem5 491,263，约 0.32x） |
| 合并等待合计 | 6,210,071 cycles |
| 平均每次等待 | 40.0 cycles |

**失败机制（关键判断）**

1. **欠合并与过串行同时发生**：FastSim 只识别出 155K 个 follower（gem5 491K），
   却把 CPI 推高到 1.08。每个 follower 的父等待（均约 40 周期）被当成**独立 stall
   逐次裸加**到关键路径。
2. gem5 中同一父 miss 的所有 follower 在父 readCallback 时**一起**完成，整段只产生
   一次阻塞；follower 等待与父 miss 完全重叠，不额外计墙钟。
3. 现有排除门（`:14128`、`:17087` 用 `!sequencer_coalesced`）让 follower 不占 MSHR、
   不进普通 load 服务模型，但也使其**脱离 MLP 重叠记账**——`exposed_cycles` 直接落在
   关键路径上，没有"与父 miss 已存在的阻塞取 max"的折叠。
4. **本质是一个反馈环**：正确识别在飞父 miss 需要已与 gem5 一致的准入时刻表；而准入
   时刻又取决于 alias 决策本身。旧机制靠多趟收敛到不动点解开此环（seed pass
   `:18047` 关表建立 response-aware 时刻 → 第二趟按修正顺序建表 → `:18223` 稳定校验）。
   **单趟 buffer 路径在结构上无法复现，与是否漏接某条边无关。**

结论：8.5 的"准入边"判断成立，但"在 buffer 路径内重新接线即可"的修复路径被本反证
否定。任何修复都必须解开 alias 决策与准入时刻之间的环，代价是额外的遍历或 lookahead，
需先评估其对两阶段吞吐框架的影响（见 8.7）。

### 8.7 修复方案对两阶段吞吐框架的影响（高吞吐是核心目标）

状态：基于实测计时的**分析/估算（analysis）**，非新吞吐测量。目的：在投入实现前判断
解开 8.6 反馈环的代价值不值。

#### 8.7.1 实测成本结构（NAMD c04 全 ROI，4 核）

单趟生产运行：13.33s wall / 23.69s user，峰值 RSS 1.53 GiB。行表本身几乎零成本
（ON/OFF：13.45s vs 13.33s，+0.1s 在噪声内）。内置阶段计时器（CPU 时间口径）：

| 阶段 | ms | 占比 |
|---|---:|---:|
| `interval_weave`（阶段2 weave） | 10,446 | 52.1% |
| `timing_feedback`（逐 UOP 反馈扫描） | 7,763 | 38.7% |
| `interval_schedule_batch`（阶段1 interval） | 1,801 | 9.0% |
| 其余（commit 等） | 50 | 0.3% |

要点：**阶段2（weave + feedback）占约 91%，阶段1 只占 9%。** 任何"额外遍历"若重做
weave，代价直接落在 91% 的主体上；只有纯增量、可并入阶段1 的办法才可能便宜。

#### 8.7.2 三类方案的吞吐代价

**方案 A：buffer 路径内 timing-only 多趟收敛到不动点**

每多一趟需重排批次 weave + 重算反馈（即重做约 91% 的阶段2 工作）：

- 若 2 趟收敛：阶段2 成本约 ×2 → 总墙钟约 **1.8–2.0x，吞吐降到约 50–55%**。
- 瘦身后的 timing-only 趟（跳过 functional private/shared cache 访问与层级事务，只在
  已存描述符上重排/重扫）仍须支付 weave 排序与逐 UOP 反馈扫描这两大头，现实估计
  **+50%–70% 墙钟**，且不增加额外层级内存副本（保住 1.5 GiB 量级）。
- 风险：趟数随 workload 变化。框架已记载 C4 有 127,426 个未修复 same-line 乱序对，
  tail/高核案例可能 >2 趟或不收敛 → fail-closed 串行回退，代价进一步放大且不可预测。
- case 级并发（此前最大、且 bit-identical 的吞吐来源）不能吸收该成本：多趟增加的是
  每 case 的 CPU 工作量，在 CPU 打满的 jobs=N 聚合下吞吐近似按额外 CPU 比例下降。

**方案 B：有界 lookahead 预算 miss 生存期，再单趟 weave**

若能把 lookahead 并入阶段1（仅 9%），理论上最便宜（可能 +10%–20%）。但 **miss 的真实
完成时刻依赖全局 LLC/DRAM 调度，而调度又取决于准入**——这与 8.6 是同一个环。不 weave
的 lookahead 拿不到父 callback，只能给估计，等于换一种近似，可能重新引入"按 workload
拟合"，违背方法论。判：便宜的版本不闭合，闭合的版本退化成方案 A。

**方案 C：只对 alias 相关的冲突分量（conflict component）局部收敛，不重做整批**

沿用现有 `plan_cross_core_line_conflicts` / component 思路，额外遍历只覆盖含同线在飞
miss 的连通分量，空 epoch 与无关分量零成本（复用 P0 零进度跳过）。

- 代价取决于分量大小，最坏仍接近 A，常态因仅 ~23% admission 为 alias、且局部化，
  **现实区间约 +15%–45%**，是三者中代价/风险最可控的。
- 仍需解决分量跨 epoch 边界与不收敛时的 fail-closed 回退。

#### 8.7.3 结论与建议

1. 全额多趟（方案 A 朴素版）**与高吞吐核心目标直接冲突**：2 趟即可能腰斩吞吐，且 tail
   趟数不可控；不应作为默认。
2. 便宜的纯 lookahead（方案 B）不闭合，不能选。
3. **建议优先评估方案 C（分量级 timing-only 收敛）**：先量化 NAMD/Graph500/lbm 中
   alias 冲突分量的实际大小分布，用真实分量规模把 +15%–45% 这个区间收敛成确定数字，再
   决定是否值得为消除 0.090 CPI（−18.7% 反例）支付该吞吐代价。
4. 决策门禁（建议）：只有当 C 的实测吞吐损失显著小于其带来的全矩阵 CPI-MAE 收益，且
   C1/C13 控制窗不回退时才进入实现；否则应把 line-alias 缺口显式记为"为吞吐主动保留的
   已知结构项"，而非反复尝试。

下一步最小动作（仍不实现模型改动）：写一个只读统计，在不重放的前提下度量各 workload
中 alias 冲突分量规模，据此把方案 C 的吞吐代价定量化。

#### 8.7.4 澄清：gem5 并不"多趟收敛"，多趟是 FastSim 回放侧的产物

> 重要澄清：本节前文若把"多趟收敛"说成 gem5 的行为，是不准确的。gem5 是**逐拍/事件
> 驱动、严格只向前推进一次**的仿真器，从不为一个决策反复重算。需要多趟的是 **FastSim
> 离线回放 trace 时，为了重建 gem5 在前进中自然得到的准入决策而引入的近似手段**。

gem5 为什么一遍就够：仿真时间真的在向前走。周期 0，A 发出 miss，Sequencer 在该行建一个
在飞表项；稍后 B、C 在**同一个仿真时刻**到达，Sequencer 直接查到"A 的表项此刻就在"，
于是把 B、C 挂上去；周期 200 A 的数据回调，B、C 同拍解除。**"A 正在飞"是当时的真实
状态，不需要预知未来，也不存在鸡生蛋的循环。**

FastSim 为什么会遇到环：它不是逐拍推进，而是把一段 trace 成批读入、按时间排序后在
**一次 weave** 里批量做决策。处理 B 时要判断"A 是否在飞"，但 A 在这趟批量计算里的
完成时刻、以及 B 相对 A 的到达先后，本身正是这趟 weave 要算出来的东西——决策依赖它
自己的输出。旧实现用"先按无表粗算一趟拿到 response-aware 时刻，再按该顺序建表算第二
趟"来**模仿** gem5 前进时天然拥有的状态（见 8.6 的 seed/第二趟）。这是用离线多遍去
逼近在线一遍，多遍是 FastSim 侧的成本，gem5 没有这个成本。

### 8.8 通俗例子：line-alias、为什么修了反而更糟

**(1) 什么是 line-alias**

CPU 按"一行"（cache line，如 64 字节）取数据。假设程序连续读同一行（地址 1000）三次：

```
读 A: load 1000
读 B: load 1000   ← 同一行
读 C: load 1000   ← 同一行
```

gem5（逐拍向前）：周期 0 A 出发去远处内存（假设 200 周期），B、C 查到 A 的在飞表项，
不发新请求、挂在 A 身上；周期 200 数据回调，A、B、C **同拍一起完成**。

```
周期0:   A 出发(200)；B、C 挂在 A 身上
周期200: 三者一起完成  → 总共只等一次 200
```

FastSim 当前（无 alias）：把 B、C 当全新独立请求，各自查缓存、以短命中（如 6 周期）
提前返回：

```
周期0:   A 出发(200)
周期6:   B 返回(6)
周期12:  C 返回(6)
```

B、C 比真实早一两百周期"假装完成"，CPU 提前得数据、提前推进 → 程序算太快，即 NAMD
**−18.7% 低估**的来源。

**(2) 为什么加了 alias 反而更糟**

加回"挂在 A 身上等"后，每个 follower 单独看都正确（B、C 都等到周期 200）；但 FastSim
的算账方式把每段等待**逐条独立相加**，不知道这三段是重叠的同一段时间：

```
错误账：A 等 200
       +B 又等 200
       +C 又等 200
       = 600          （真实应是重叠的一个 200）
```

于是从"太快"变成"太慢"，CPI 0.39 → 1.08，误差 −18.7% → +125.5%。

**(3) 一句话矛盾**

gem5 靠"时间真实向前走"天然知道 A 正在飞，一遍即可；FastSim 成批离线决策，B 的判断
依赖这趟本来要算的 A 的结果，形成决策依赖自身输出的环。解开它要么多遍 weave（损失
8.7 所述吞吐，朴素 2 遍可能腰斩），要么只对真正冲突的小分量局部多算（方案 C，估计
+15%–45%）。这不是"bug 没修好"，而是**精度遍历量与 FastSim 高吞吐核心目标之间的
取舍**。


### 8.9 完整负载矩阵验证（2026-09-23）：38/38 通过，CPI MAPE 7.17%，PMU 大百分比主要是口径差

> **⚠️ 本节关于 PMU 的结论已被 8.10 更正：那不是报告口径问题，而是切到 buffer-114 后暴露的真实建模缺口。**
> **请连同 8.10 一起读；本节仅保留原始计数与实验记录，关于"同口径 7%/15%"的论断在 8.10 撤回。**

**对象与方法（production，非实验分支）**

- 被测二进制：冻结生产版 `tmp/buffer-tso-store-edges-20260923/fastsim-store-edges`，
  SHA-256 `0637ba3c7c0c3d65d544d48a6ca8800f1d269768a0da074bea4e86b871b57f5`。
  8.6 用过的实验 alias 接线不在此二进制内。
- 信源：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source`，即
  `first-core-target-common-end-v1` common-end 矩阵。该根有 39 个目录，其中
  `c08-857.namd_s` 是 inventory 记录的 exit-1 失败采集（无 `tao_trace/`、无 `oracle/`），
  没有参考真值，按 fail-closed 原则从评分集剔除，**有效案例 = 38**（c04 9 + c08 9 + c16 10 + c32 10；
  c32 zstd 因已知 checkpoint 问题本就不在矩阵）。
- 流程：先 `validate_fs_oracle_identity.py`（38/38 valid），再
  `run_native_kernel_fastsim_validation.py`。结果落盘
  `tmp/store-edges-full-validate-20260923/native/`：**discovered=38 passed=38 failed=0 pending=0**。

**CPI 准确性（n=38，硬指标）**

- **CPI MAPE = 7.17%**，**CPI MAE = 0.0598 cycles/inst**，中位 |相对误差| = 6.42%。
- 方向仍以低估（FastSim 偏快）为主，与 8.4 过度 MLP / 8.5 缺 alias 合并一致。
- 尾例（相对% / 绝对 CPI）：c04-namd −18.7 / 0.090；c32-lbm +16.8 / 0.253；
  c32-tealeaf −14.2 / 0.073；c04-tealeaf −14.4 / 0.144；c08-tealeaf −13.8 / 0.107；
  c16-namd −13.2 / 0.060。c32-lbm 的 +0.253 绝对误差是全矩阵最大单点。
- 最准：c16-stockfish +0.1 / 0.001、c32-stockfish −0.4 / 0.003、c04-neutron −0.6 / 0.011。
- 注意：CPI 是 cycles/inst 且 instruction population 已逐 case 校验 match，
  **这 7.17% 是真实模型误差，不能用下文的 PMU 口径差洗掉**。

**PMU：报告大百分比主要是口径不匹配，不是真把缓存算错几百 %**

参考侧同一 case 同时存在两套口径，FastSim 只产出其中一套：

| 口径 | 来源 | 计数单位 |
|---|---|---|
| native 控制器动作（validator 采用） | `native-ruby-slicc`，`uu_profileDataMiss`，alias follower 并入父 miss | 唯一真实 line miss |
| per-uop path-class | `pmu_source=taotrace-path-class-v3` | 每个 uop 的 tag 查询 |
| FastSim 输出 | `CoreCounters.l1d.misses` 等（逐 uop） | 每个 uop 的 tag 查询 |

即 FastSim 数的是"每 uop 一次 tag 查询"，native truth 数的是"控制器里真实分配/合并的唯一
line miss"；buffer 模式 alias 合并被禁用（8.5），FastSim 自然只有 per-uop 口径。直接拿
FastSim 对 native 相减，等于拿两套定义相比。全矩阵 APE 均值对照：

| 指标 | 对 native 口径 | **同口径（FastSim 对 path-class）** |
|---|---|---|
| L1D tag misses | 30.5% | **6.98%** |
| private-L2 tag misses | 77.7% | **18.54%** |
| LLC tag misses | 170.5% | **14.91%** |
| branch misses | 3.58% | 3.58%（口径本就一致） |

典型：c04-lbm 对 native 为 +178%/+296%/+298%，同口径下仅 +0.1%/+0.1%/+0.0%，
几乎 100% 是口径差；c04-namd 同口径 L1D/L2/LLC = +1.9%/+4.6%/+3.7%，
zstd = −5.1%/−10.2%/+5.1%，这些 2–10% 才是真实残差。

仍需单独说明的两类：

1. **omnetpp 各级、nab 的 L2/LLC（同口径仍 30%–160%）**：这些不是口径差，参考绝对计数很小、
   且涉及 inclusion / permission-upgrade 分类，需另行按 native 子类核对，不能并入"口径问题"结论。
2. LLC 在 validator 中标记为 `diagnostic`（非 promotion 口径），且其高 APE 未等比例传导到 CPI。

**吞吐量（核心目标，维持）**

- 38 例单进程 end-to-end 均值 ≈ **1.98 MIPS**（c32-omnetpp 1.09 最低，多数 1.8–2.6 MIPS）。
- 本次前 25 例误用 `--jobs 1` 串行；切到 `--jobs 16` 后剩余 13 例并行，机器为
  192 核 / 2 TB（当时 load 仅 ~5），已通过 fingerprint 自动复用已通过案例、不重跑。

**结论**

- store-edges 在完整 common-end 矩阵 **38/38 通过**：CPI MAPE 7.17% / MAE 0.0598，吞吐 ≈1.98 MIPS。
- PMU 的几百 % 主要是 **per-uop vs native-controller 口径差**（同口径 L1D 7.0% / L2 18.5% / LLC 14.9%）；
  要让 FastSim 直接按 native 口径出数，仍需 8.5 的 line-alias 合并，与 CPI 低估是同一条边。
- 后续报告对缓存 PMU 应同时给"native 口径"与"同口径"两列，并把 omnetpp / nab 的小计数真实残差单列。


### 8.10 更正（2026-09-23）：PMU 变差不是报告口径，是切到 buffer-114 暴露的真实 alias 建模缺口

8.9 把 PMU 的几百 % 归为"FastSim 逐 uop 口径 vs native 控制器口径"的报告错位，并据 path-class
字段算出"同口径 L1D 7.0% / L2 18.5% / LLC 14.9%"。经对历史产物的实查与受控交叉实验，**该结论错误，在此撤回**。

**核实路径（全部读原始文件，非推断）**

1. 0912 报告真实目录 `tmp/load-ready-default40-20260912.Ydiy65`。其 cache PMU 的
   `reference_source` 就是 `native-ruby-slicc:*.tag_misses`，**不是 path-class**。
2. 该报告的 `result_dir` 指向另一套 gem5 采集 `tmp/first-core-common-end-20260911/source`，
   其 `config.ini` 为 **SQEntries=32 / needsTSO（serial-32）**；今天对照的是 0918 采集的 buffer-114。
   即两期参考来自**不同几何的两次采集**。
3. 在同一批 38 个匹配 case 上，用**同一个 native-Ruby 参考族、同一种 pooled WAPE** 重算：

| 事件 | 0912（对 serial-32 采集） | 现在（对 buffer-114 采集） |
|---|---|---|
| l1d_tag_misses | 2.12% | 20.13% |
| private_l2_tag_misses | 2.15% | 29.72% |
| llc_tag_misses | 1.95% | 131.36% |
| branch_misses | 1.51% | 1.56% |

4. **受控交叉实验**（隔离"二进制退化" vs "几何切换"）：用**当前同一个** store-edges 二进制
   （SHA `0637ba3c…`），配**旧 serial-32** 配置，对**旧 0911 采集**重跑 8 个代表 case
   （namd/lbm/zstd/tealeaf/stockfish/graph500/omnetpp/nab），8/8 通过：

| 事件 | pooled WAPE | mean APE |
|---|---|---|
| l1d_tag_misses | **2.24%** | 2.55% |
| private_l2_tag_misses | **2.14%** | 5.19% |
| llc_tag_misses | **1.63%** | 6.59% |
| branch_misses | 2.46% | 3.55% |

namd c04 计数与 0912 几乎逐位一致（L1D 272464 vs 272478；LLC 27882 vs 27882）。
产物：`tmp/store-edges-serial-crosscheck-20260923/`。

**更正后的结论**

- **当前二进制未退化**：在 serial-32 下精确复现当年 ~2%。
- **报告参考也没选错**：0912 与现在用的都是 native Ruby 真值。故 PMU 不是"统计口径假象"，
  而是**真实变大**。
- 触发点是几何从 serial-32 切到**正确的 buffer-114**。serial 时序高度串行，load 执行时父 miss
  多已回调，逐 uop 计数天然≈native 唯一-miss；buffer-114 高度解耦，大量同行访问在父 miss
  在飞期间到达——gem5 把它们**合并**为一个 miss，FastSim buffer 路径因 8.5 禁用 alias 而把
  follower 当独立新请求 → 同时高估各级 miss、低估 CPI。**与 CPI 低估是同一条边、同一个修复。**
- 8.9 用 path-class 字段算出的"同口径 7%/15%"属循环自证（那是 contract 明令永不可当真值的
  代理字段），**不能用于主张 PMU 误差小，撤回**。
- branch 两期几乎不变（1.51%→1.56%），反证出问题的专门是访存 alias 这条边。

**对下一步的含义**：PMU 与 CPI 的主要残差同源，靶点唯一且明确——在 buffer 路径恢复
line-alias/MSHR 合并（8.5 / 8.6），需在单趟约束内谨慎接线，不能借旧多趟开关，也不能再用
path-class 对比来评估收益；验收须直接对 native Ruby 计数。


### 8.11 "逐 uop 天然≈native"是什么意思，buffer TSO 为什么破坏它

本节解释 8.10 结论里"serial-32 下 per-uop 计数天然≈native 唯一-miss，buffer-114 破坏这一
吻合"的具体机制。核心是：**两套计数的差别不在标签本身，而在 follower uop 被执行（即被判定）
的时刻——父 miss 回调之前还是之后。**

**两个计数各自的判定时刻**

- native（Ruby 控制器 `tag_misses`）：只有真正发到 L0 控制器且标签不在的请求才算。父 miss
  在飞时，同行 follower 在 sequencer/MSHR 层就被拦下合并，不发往控制器，不算 miss。数的是
  **唯一 miss 事件**。
- FastSim 逐 uop：对每个 load uop，在它被发射/执行的那个模拟时刻查模型里的 L1D 标签，
  标签不在即记一次 miss。

因此逐 uop 的结果取决于 follower 是在父 miss 回调之前还是之后被执行。

**"天然≈native"的含义**

若时序保证——同一 cache line 上，父 miss 回调之前只有一个 uop 去查 L1D，其余同行 uop 都在
回填之后才执行——则：

```
第 1 个 uop：行不在 → miss（计 1）
第 2、3…个 uop：行已回填 → hit（计 0）
逐 uop 合计 = 1 = native 的唯一 miss
```

逐 uop 不需要专门 alias 表，仅靠串行时序 + 维护标签状态就**碰巧**等于唯一 miss 数。这就是
"天然≈"。

**serial-32 满足该条件**

一条 load miss 后核心停住等回调，后续 uop（含同行 follower）要等 miss 回来、行回填后才执行，
follower 看到 hit。故 FastSim serial 逐 uop 合计自然收敛到 native 唯一 miss（namd c04：
FastSim 272,464 vs native 259,439，≈2%）。

注意：gem5 在 serial 下同样大量做 MSHR 合并（该次采集 aliased≈192 万次），但 FastSim serial
把 follower 排到回填之后，两边时序虽不同，**"每行 miss 只被数一次"的计数结果恰好相等**。
这是结果上的吻合，不是 FastSim 真的建了 MSHR。

**buffer-114 破坏这一吻合**

buffer TSO 的意义就是把发射/执行与 miss 完成解耦以隐藏延迟：

```
serial:  uop1 miss ──等200──回填 · uop2(hit) uop3(hit)
buffer:  uop1 miss · uop2(行仍不在→miss) · uop3(→miss) ··· ──回填
                                  ↑ 父 miss 还在飞
```

follower 在父 miss 在飞窗口内即被执行，标签尚未回填，FastSim 各记一次新 miss；native 的 MSHR
仍把它们合并成一个。于是逐 uop = N、native = 1，高估 miss（namd c04：FastSim 375,498 vs
native 337,080，多约 3.8 万，正是被 buffer 提前到 miss 窗口内又未被合并的 follower）。

**结论**

- "天然≈"= 串行时序让同行 follower 都在 miss 回调之后才执行，逐 uop 查标签自然每行只 miss
  一次，恰好等于 native 唯一 miss，无需显式 MSHR。
- buffer 没有改变 native 的定义（gem5 始终合并），它改变的是 FastSim 的发射时序：把 follower
  从"回填后"挪到"在飞窗口内"，缺 alias 合并由此暴露——逐 uop 把 N 个 follower 数成 N 次 miss，
  native 只数 1 次。
- 故修复不是"把计数改成唯一 miss"，而是让 FastSim 在解耦发射前提下也能识别"这些 uop 共享同一
  在飞 miss"（8.5 的 alias/MSHR 表）；这与 CPI 低估同源。
