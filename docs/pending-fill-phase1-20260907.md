# Pending fill 第一阶段：实现、反例和验收

**处置决定：暂停该候选，保留代码与反例，默认关闭。** 决策理由、避免重复的路径、重新研究条件及下一项 TeaLeaf 研究见 [优化决策记录](optimization-decisions.md)。本报告保留已经完成的实验，不代表继续推进该候选。

本轮完成了 pending-fill 响应约束候选的实现、两套 94-case 回放和三因子对照；原计划的请求及缓存事件状态统一尚未完成。**较窄的 fill-only 修复有小幅 P99 收益；同时加入 load admission 和 store 生命周期的组合未通过尾部误差验收。默认 baseline 保持不变。** 所有本轮回放固定 Q=1024，没有按 workload 调整系数。

当前可复现的实验入口是 [gem5-exp-pending-fill.cfg](../configs/gem5-exp-pending-fill.cfg)。普通 `core.response_pending_fill=true` 现在只启用响应等待修复。load admission、store commit/send 两项必须分别显式开启，供继续定位使用；它们没有进入默认配置。

**1. 验收结果。**

基线是同日架构审查重新跑出的当前二进制，不使用历史 FastSim 最佳分数。formal40 的误差基于 macro `perf_like_cpi`；DSE54 基于 `cycles_per_user_uop`，分别聚合。P99 使用 `(N−1)×0.99` 线性插值，以下数值均为百分比。

| 集合 / 方案 | MAPE | 绝对误差 P99 | 最大绝对误差 |
|---|---:|---:|---:|
| formal40 基线 | 6.8096 | 13.5930 | 13.7367 |
| formal40 fill-only | 6.3600 | 13.1052 | 13.2184 |
| formal40 完整组合 | 5.9749 | 15.6729 | 16.5058 |
| DSE54 基线 | 11.8022 | 17.6522 | 17.9910 |
| DSE54 fill-only | 11.7768 | 17.5539 | 17.7723 |
| DSE54 完整组合 | 11.7730 | 22.9537 | 30.5978 |

fill-only 的 P99 收益分别为 **0.4878 和 0.0983 个百分点**，小于此前预估的 1–3 个百分点。94 个 case 中有 37 个绝对误差略增，最大增量为 Neutron C4 的 0.5258 个百分点；不能描述为所有 case 都改善。两套回放的 user/native-kernel trace UOP 数均与基线一致。

DSE 的 fill-only direction 保持 39/48，参考变化至少 1% 的 direction 保持 34/39，pairwise order 保持 176/216；speedup MAPE 从 2.8288% 到 2.8097%。完整组合虽然 direction 变为 40/48、pairwise 变为 181/216，但最坏误差显著恶化，不能靠排序或平均误差掩盖。

完整 case 行、参考 CPI、计数核对及绝对路径见 [fill-gate.json](../tmp/phase1-pending-fill-20260907-121831/fill-gate.json) 和 [full-gate.json](../tmp/phase1-pending-fill-20260907-121831/full-gate.json)。两者各包含全部 94 个有效结果。完整组合的 Zstd C32、Neutron C32 曾超过并发任务的 180 秒超时，原失败记录保存在 `pending-timeout-180s`，随后使用同一二进制和配置、600 秒上限复跑成功；没有将超时计为零误差或剔除 case。

**2. 实际修复了什么。**

[PendingFillTable](../include/fastsim/pending_fill.hpp) 保存每 core、每 physical line 的已知响应可用时刻。校正后的 miss callback 约束后续 apparent L1 hit，并通过原有 response closure 传递到消费者、队列释放和退休。它集成在同一遍 core feedback 中，没有新增第二遍全 UOP proposal。

表随被接受的 timing state 跨 epoch 保留；候选 pass 使用独立副本，只有 commit 才发布。过期使用单调的 dispatch watermark，不能使用按程序顺序访问到的 OoO issue 时刻。generation 用于防止旧 expiry 删除后来发布的同 line 条目。读数据与写权限响应分别记录，避免把仅升级写权限的等待强加给已有数据的读请求。

在 LBM C4、core 0、sequence `[2000000, 2010000)` 的相同见证窗口中：

| 观测 | 基线 | fill-only |
|---|---:|---:|
| 数据事件 | 3,889 | 3,889 |
| L1 hit response 早于前一同 line miss response | 388 | 0 |
| canonical issue 早于前一 canonical fill 的 hit | 744 | 747 |

第二行证明响应约束在这个窗口起效；第三行同时说明**功能缓存的提前可见状态仍然存在**。不能把本次改动称为已经完成整个层级的真实事件顺序回放。见 [frontier-check.json](../tmp/phase1-pending-fill-20260907-121831/frontier-check.json)。该窗口审计模式与快速内核的总目标周期一致。

LBM 全 case 的 `wait_cycles` 合计为 67,643,497，但总 core cycles 只增加 93,412，CPI 误差从 −6.3997% 到 −6.3193%。逐请求等待包含重叠和非关键路径，不能累加成 CPI 改善量。Graph500 C8 的误差则从 −13.7367% 到 −8.6960%，说明相同修复在不同依赖图上的暴露程度差异很大。

`pending_fill.parents` 统计表发布次数，不是唯一 DRAM 请求；`carried_parents` 是各次 checkpoint 继承条目的累计值，不是去重后的跨 Q 请求数；`max_entries` 是每 core 表大小峰值的最大值。`pre_admission_followers` 暴露程序顺序与实际准入顺序不一致的保守约束：例如 Graph500 C8 为 46,428，不能当作已消除全部 OoO 归属问题。

**3. 为什么没有推广完整组合。**

对 6 个代表 case 做了 fill wait / load admission / store lifecycle 的完整三因子对照。下表为 signed CPI error，完整 48 个有效组合见 [factorial.json](../tmp/phase1-pending-fill-20260907-121831/factorial.json)。

| Case | 基线 | 仅 fill | 仅 store | fill + store | 完整组合 |
|---|---:|---:|---:|---:|---:|
| LBM C4 | −6.40% | −6.32% | +5.59% | +5.98% | +5.99% |
| Graph500 C8 | −13.74% | −8.70% | −10.74% | −9.74% | −7.53% |
| Stockfish C16 | +12.85% | +12.93% | +14.27% | +14.39% | +14.37% |
| ASTCENC baseline C4 | −14.03% | −13.90% | −11.74% | −11.58% | −11.65% |
| TeaLeaf L1D64 C4 | −17.99% | −17.77% | −16.82% | −16.73% | −16.18% |
| TeaLeaf LLC32 C4 | +9.65% | +9.71% | +30.63% | +30.48% | +30.60% |

这直接定位了 TeaLeaf LLC32 的主要回归来源是本次 store 改动，而非 fill wait。仅 store 模式下，审计计数显示 IQ stall 从 283,349 到 18,237,482，critical dependency 从 22,942,884 到 28,335,768，critical Sequencer 从 0 到 1,216,604。相反，SQ stall 从 121,139,287 降到 854,524。不能把它简单归因为“SQ 更满”；证据见 [store-attribution.json](../tmp/phase1-pending-fill-20260907-121831/store-attribution.json)。这些计数有重叠，不能全部相加当作周期分解。

进一步构造的单 core、Q1024、Sequencer 容量 2 反例揭示了具体机制：

| 事件 | 可见时刻 |
|---|---|
| 较老 miss A | request 468，response 654 |
| 下一条普通 store B | 地址执行 468，retire 654，真正的模型准入 656，response 846 |
| 独立的年轻 L1 load C | producer 在 468 就绪，却被推迟到 654 request、656 response |

在 `[468,654)`，实际已准入的请求只有 A，2 个槽位中仍有一个空闲。C 的 L1 响应只需 2 cycles，能够放进 B 到达前的空档。但是程序顺序反馈先把 B 的未来 response=846 放进 release heap，年轻 C 随后看到最小 release=654，被错误阻塞。该反例的输入、输出分别为 [future-reservation.cpp](../tmp/phase1-pending-fill-20260907-121831/future-reservation.cpp) 和 [future-reservation.csv](../tmp/phase1-pending-fill-20260907-121831/future-reservation.csv)，mode 2 是显式 store 准入模式。

这证明仅移动 store 的 commit/send 时间、继续以 release heap 表示准入状态是不够的。下一步需要带 **admission 和 response 区间**的请求日历及受影响事件的重排；对于会反过来改变已预约 store 的年轻请求，必须增量重算依赖与资源预约。还需同步功能 cache/coherence 的可见状态。把这些问题藏进一个固定延迟或直接去掉第二遍 proposal，都不构成等价修复。

**4. 吞吐验收。**

固定 CPU 0–47、NUMA 0，结束矩阵任务并检查宿主机相关任务后，逐 case 串行执行 before / off / fill 三个 arm，每个各 3 次，用轮换顺序控制测量漂移。before 是修改前二进制，off 是最终二进制关闭修复，fill 是最终二进制开启窄修复。速度使用 measurement 的 M user-UOP/s；进程总耗时也保存在原始结果中。

| Case | before | 新二进制 off | 新二进制 fill | fill 相对 off |
|---|---:|---:|---:|---:|
| LBM C4 | 4.1013 | 4.0607 | 3.9681 | −2.28% |
| Graph500 C8 | 8.6184 | 8.5756 | 8.1709 | −4.72% |
| ASTCENC baseline C4 | 8.2683 | 8.4332 | 8.3321 | −1.20% |
| TeaLeaf LLC32 C4 | 6.0146 | 6.0067 | 5.8664 | −2.33% |

36 次的 `totals/cores/threads/cha/instruction_cha` 及总目标周期核对全部通过：before/off 对应旧基线，fill 对应 v4 的同配置准确率输出。开启修复的成本以同二进制 on/off 对照为准；旧/新二进制自身的变动不能算作本修复的算法加速。表中仅覆盖这 4 个 case，不代表 C16/C32 的吞吐成本上界。

吞吐原始结果和逐次目标状态检查见 [bench-results.json](../tmp/phase1-pending-fill-20260907-121831/bench-results.json)。准确率矩阵的并发 wall time 不用于此结论。这里没有实现或宣称整体吞吐加速；本阶段要核实准确率修复的实际成本。

对上述同一批串行样本进一步取各阶段计时中位数，得到 [feedback-cost-breakdown.json](../tmp/phase1-pending-fill-20260907-121831/feedback-cost-breakdown.json)：

| Case | feedback 耗时变化 | feedback core task 数变化 | fast kernel 访问 UOP 数变化 |
|---|---:|---:|---:|
| LBM C4 | +7.51% | −0.32% | 0 |
| Graph500 C8 | +11.59% | +6.69% | 0 |
| ASTCENC baseline C4 | +7.97% | +0.09% | 0 |
| TeaLeaf LLC32 C4 | +6.86% | +0.20% | 0 |

实现保留原有逐 UOP feedback，同时为数据请求增加 line 查找、pending 条目发布/过期堆维护，以及活跃 core checkpoint 的表复制。LBM 有 12,445,813 次查询入口、1,168,754 次表发布；Graph500 分别为 11,365,941 和 4,461,593。它没有增加第二遍全 UOP feedback，但也没有减少原来的遍历。LBM 的任务数略减、阶段耗时仍增，说明新增成本不能全解释为目标时钟变长；Graph500 还叠加了任务数增加的成本。当前计时定位到 feedback 阶段，尚未独立量出 hash、堆维护、复制各自的耗时占比，不能将其中一项断言为唯一宿主瓶颈。

准确率收益不匹配这笔成本的另一原因是尾部没有明显移动：DSE 最坏的 TeaLeaf L1D64 C4 从 −17.9910% 到 −17.7723%，第二差的 TeaLeaf ROB256 C8 从 −17.3517% 到 −17.3602%。Graph500 C8 的改善不会降低 DSE54 的 P99。大量提前响应只能证明局部时序不一致，缺少关键路径归因时不足以支持之前的 1–3 个百分点收益预估。

**5. 代码、验证与边界。**

新增实现集中于 [pending_fill.hpp](../include/fastsim/pending_fill.hpp)、[simulator.cpp](../src/simulator.cpp)、配置解析及 JSON 计数；新增 [analyze_pending_fill_gate.py](../tools/analyze_pending_fill_gate.py) 从原始 stats 重算两种 CPI 口径的矩阵和 DSE 排序。失败 run 即使残留旧 stats 也不会被计入有效结果。

执行 `cmake --build build -- -j16` 和 `./build/fastsim_tests`，全部通过。新增定向测试覆盖提前命中复现、消费者传播、旧 generation 的 expiry、候选状态隔离、冷 store fill 跨 Q、admission 越过 Q 的边界、小容量 IQ/ROB/LQ/SQ、atomic/serialization，以及 generic/fast kernel 一致性。6 个代表 case 在关闭新开关后，`totals/cores/threads/cha/instruction_cha` 与修改前逐项一致，见 [disabled-target-checks.json](../tmp/phase1-pending-fill-20260907-121831/disabled-target-checks.json)。

尚未满足的推广条件：完整请求顺序、cache replacement/coherence generation 与 pending 条目的绑定、跨 core 的真实状态可见性，以及未参与机制选择的窗口验收。本次 generation 是响应表生命周期标记，不是功能 cache 的 replacement generation。pre-admission follower 仍采用保守等待，不能把窄修复宣称为完整 OoO 事件模型。

上述实验期间默认 [gem5-fs-native-kernel.cfg](../configs/gem5-fs-native-kernel.cfg) 没有修改。处置时在该 alias 增加了主开关及 admission/store 子开关的显式 `false`，与原默认值一致；没有切换基线模型。没有调整 StoreSet、Q 或 workload 系数，没有提交或推送混合工作区。

**6. 复现与版本。**

所有新实验位于 `tmp/phase1-pending-fill-20260907-121831/`。输入使用同日审查冻结的配置、manifest 和 gem5 标签；没有重新运行 gem5。大体积 FST 仍依赖原输入路径，见原审查的 [输入指纹](../tmp/architecture-evidence-20260907.hlrSNO/input-provenance.json)。

| 二进制 | SHA-256 |
|---|---|
| before / 当前 baseline | `42f7c689c38ce5b2b15fd17704275bc7a63f04a92eeb8d3f00f450414c5bbbb3` |
| v2 / 完整组合 94 case | `ae3223e4c20f6bccc97461806bfe2fb2105d69b56165b8371eca3208c5df8ccf` |
| v4 / fill-only 94 case、补齐消融 | `d0348b05d06343133ee71d35173c5f65ca79e684726142ec618ae84565cac376` |
| v5 / 最终默认开关语义、串行吞吐 | `c5875a45a03c86edf8394385e00e32d669b0fe0ab8e9a71d1b28c7058f6ecaf6` |

v5 将 admission/store 子开关默认改为 false；v4 的 fill-only 回放已显式设置二者为 false。最终吞吐回放逐次核对 v4 fill-only 或原基线的完整目标状态。早期失败原型和日志保留在 v1/v3，最终聚合只采用有效输出。

```bash
# 使用最终冻结二进制重新跑窄修复；每条命令固定 Q=1024。
python3 tmp/phase1-pending-fill-20260907-121831/v5/audit.py --mode formal40 --variant verify-fill --set 'core.response_pending_fill = true'
python3 tmp/phase1-pending-fill-20260907-121831/v5/audit.py --mode dse54 --variant verify-fill --set 'core.response_pending_fill = true'

# 从本轮完整矩阵重新计算结果。
python3 tools/analyze_pending_fill_gate.py --experiment tmp/phase1-pending-fill-20260907-121831/v4 --baseline tmp/architecture-evidence-20260907.hlrSNO --variant fill --output tmp/phase1-pending-fill-20260907-121831/fill-gate.json
python3 tools/analyze_pending_fill_gate.py --experiment tmp/phase1-pending-fill-20260907-121831/v2 --baseline tmp/architecture-evidence-20260907.hlrSNO --variant pending --output tmp/phase1-pending-fill-20260907-121831/full-gate.json
```

在最终二进制中复现完整组合时，除主开关外还必须显式设置 `core.response_pending_fill_load_admission=true`、`core.response_pending_fill_store_commit=true`。该组合仅保留为可检验的实验方案，不作为推荐 baseline。
