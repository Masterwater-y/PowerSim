# Buffer TSO：实验汇总、CPI 归因边界与下一步取证

日期：2026-09-24。本文整理既有实验及代码审计，不代表新实验、模型修改或新的吞吐测量。优先级是先找到当前配置下影响退休的具体错误边，再选择修复；保持高吞吐目标，保留原有回退选项，不调时延参数拟合 CPI。

## 1. 当前结论与范围

现有证据优先指向 load 服务响应、依赖就绪和退休之间的关键路径，但**尚未证明 load MSHR 或同地址合并解释了主要 CPI 误差**。store prepare/demand 的服务边已做修正，两个阶段的资源身份仍不完整；不能把后者视为已排除，也不能因为存在缺口就认定它是主因。

最近五臂对照仅包含 TeaLeaf/NAMD C4，8 条 core trace，user-plus-kernel、first-core-target-common-end-v1，CPI 单位为 cycles/macro instruction。这是已检查过的诊断小集，不是新 held-out 或完整 40-case 验收。

- 数据源：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 对应两案。
- 最终冻结实验：`tmp/buffer-service-local-final-20260924/`，包含二进制、源码指纹、配置、identity、各臂 summary/validation 和 bench 日志。
- 硬件身份检查 2/2；五臂维护 validator 均为 passed/scored 2/2、failed/pending 0，宏指令人口匹配。
- 缓存参考采用 conserved Ruby/SLICC native summaries，不用 path-class cache。branch/L1D/private-L2 映射仍为 proxy、LLC 为 diagnostic；source/replay 通过不等于准确性或严格硬件 PMU 认证。
- 历史 TeaLeaf 退休审计使用旧模型和不同测量口径，其分解比例不能转移到本次结果。

## 2. 实验沿革及其实际证明的内容

| 阶段 | 已完成与结果 | 不能由此推出 |
|---|---|---|
| [队头 demand](buffer-tso-head-demand-only-20260923.md)与[store 时序边](buffer-tso-store-timing-20260923.md) | 区分直接 demand、在途接管、prepare 完成后本地 write；修正 commit→send 和前驱 response→send | prepare/demand 资源准入已经分别精确建模 |
| [受限服务引用验证](buffer-tso-mshr-validation-20260923.md) | 合成反例支持绝对父响应引用；5 个完整 ROI 收益接近零；纠正 miss-only 行表及 follower 不占 CPU 槽的说法 | 完整 MSHR 已实现，或低覆盖的成本代表完整方案成本 |
| [有界事务](buffer-tso-service-repair-20260923.md) | 一次合并决定控制 cache/计数/响应，失败事务恢复；首版 native 因 controller guard 全回退 | 没有收益说明机制无关 |
| [FR-FCFS 接入](buffer-tso-service-frfcfs-20260923.md) | 移除按调度器名称拒绝；两案有效选择窗口为 1；覆盖仍受预算、冲突、特殊事件限制 | 增大预算能自动解决覆盖与吞吐 |
| [不回退](buffer-tso-service-no-fallback-20260923.md) | 每个非空访存 epoch 保留一次试算；预测明显改变，整体准确性和吞吐退化 | 这是收敛结果，或 CPI 变化就是正确的因果贡献 |
| [局部回退](buffer-tso-service-local-fallback-20260924.md) | 按请求降级，保留其他读服务；测试通过，仍完整重放 epoch；未取得精度收益 | 已实现稀疏增量求解、完整跨 epoch 服务身份或高吞吐生产方案 |

所有上述选项的历史结果按各自报告保存；本节不是将不同版本、不同小集混成一组评分。

## 3. 最新五臂结果

baseline 关闭新 repair；repair 开启有界整批回退；no-fallback 强制单次保留；local 开启局部回退；local-large 仅将 local 的 max_work 从 4096 改为 65536。累计工作比例仍为 10%，最大尝试次数仍为 3（no-fallback 实际只做一轮）。

### 3.1 CPI

相对误差带符号；绝对误差为 `abs(FastSim CPI - gem5 CPI)`。

| Case | 模式 | gem5 CPI | FastSim CPI | 相对误差 | 绝对误差 cycles/macro |
|---|---|---:|---:|---:|---:|
| TeaLeaf C4 | baseline / repair / local | 1.004183 | 0.860056 | -14.352728% | 0.144128 |
| TeaLeaf C4 | no-fallback | 1.004183 | 1.225388 | +22.028365% | 0.221205 |
| TeaLeaf C4 | local-large | 1.004183 | 0.859395 | -14.418517% | 0.144788 |
| NAMD C4 | baseline / repair / local | 0.479341 | 0.389645 | -18.712294% | 0.089696 |
| NAMD C4 | no-fallback | 0.479341 | 0.394087 | -17.785649% | 0.085254 |
| NAMD C4 | local-large | 0.479341 | 0.389617 | -18.718165% | 0.089724 |

| 模式 | CPI MAPE | CPI MAE cycles/macro |
|---|---:|---:|
| baseline / repair / local | 16.532511% | 0.116912 |
| no-fallback | 19.907007% | 0.153230 |
| local-large | 16.568341% | 0.117256 |

local-large 的 scope cycles 相对 baseline：TeaLeaf 21,255,117→21,238,790，减少 16,327；NAMD 10,619,256→10,618,489，减少 767。原本已低估，因此这点变化使误差略增，而非完全没有时序作用。

### 3.2 PMU 与合并

以下是 FastSim 同一 scope 的模型计数，不是新增 native 评分。完整 PMU MAPE、WAPE、分位数、bias 和覆盖见[最新报告 PMU 节](buffer-tso-service-local-fallback-20260924.md#pmu)，不能混用 MAPE 与 WAPE。

| Case / 模式 | L1D accesses | L1D tag misses | private L2 tag misses | LLC tag misses |
|---|---:|---:|---:|---:|
| TeaLeaf baseline | 6,956,490 | 479,840 | 354,458 | 193,088 |
| TeaLeaf no-fallback | 4,915,063 | 476,745 | 354,455 | 193,088 |
| TeaLeaf local-large | 6,944,596 | 479,811 | 354,454 | 193,088 |
| NAMD baseline | 10,761,570 | 375,498 | 20,181 | 18,066 |
| NAMD no-fallback | 8,000,823 | 375,705 | 20,210 | 18,066 |
| NAMD local-large | 10,721,501 | 375,496 | 20,171 | 18,066 |

合并减少重复 lookup，不一定减少大量 miss；miss 数不变也不能证明响应时序正确。local-large 的 Sequencer 诊断中，NAMD 的 40,109 次合并有 36,465 个 L1 父、3,589 个 L2 父，仅 55 个 escape 父；TeaLeaf 的 12,212 次中则有 8,293 个 escape 父。因此不能统一解释为“只是短命中”。这些 totals 诊断不与 scope PMU 人口混算。

### 3.3 吞吐

单位为宿主 M measured user UOP/s。最终冻结二进制，五臂对称顺序串行运行，每案每臂 2 次，共 20 次；宿主非独占。下面为既有测量，不是本次整理重新测得。

| 模式 | TeaLeaf | 相对 baseline | NAMD | 相对 baseline |
|---|---:|---:|---:|---:|
| baseline | 3.2192 | — | 2.7836 | — |
| repair | 3.1737 | -1.41% | 2.7768 | -0.24% |
| no-fallback | 2.6592 | -17.40% | 2.2957 | -17.53% |
| local | 3.1887 | -0.95% | 2.7508 | -1.18% |
| local-large | 3.0528 | -5.17% | 2.6620 | -4.37% |

小样本小差异不能宣称稳定吞吐收益。不回退减少了撤销，却让每个 epoch 都执行额外服务回放，所以不必然更快。

## 4. 为什么局部回退作用仍然有限

| Case / 模式 | epoch | 预算预跳过 | 成功提交 | 试算后整批回退 | 保留合并 |
|---|---:|---:|---:|---:|---:|
| TeaLeaf local | 5217 | 5058 | 159 | 0 | 1013 |
| NAMD local | 2681 | 2681 | 0 | 0 | 0 |
| TeaLeaf local-large | 5221 | 4697 | 524 | 0 | 12212 |
| NAMD local-large | 2680 | 2570 | 108 | 2 | 40109 |

1. 试算后回退少，不代表覆盖高。local-large 仍跳过约 90.0%/95.9% 的 epoch；默认 local 的 NAMD 完全未试算。
2. 当前局部条件把 active 请求的 `solved issue != candidate issue` 直接转为 baseline，并恢复旧请求起点。合理的依赖传播也可能触发降级。late-issue 标记目前还可能使同核剩余 active 请求降级。
3. 所谓局部，是决定降级范围局部，计算仍完整重放 epoch。预算用 UOP、访存及控制器处理工作量估计宿主额外开销；不是硬件周期、MSHR 容量或带宽预算，也不保证 10% 工作量只增加 10% 墙钟。
4. local 进入前保守预留最多两轮，若上界超过额度池，可能连一遍即可成功的 epoch 也跳过。单纯扩大额度不能解决重算成本与合理时间变化被拒绝的问题。
5. 成功混合事务保留在途身份，但预算跳过和整批回退仍有丢弃行服务身份的近似；完整跨 epoch 一致性尚未解决。

## 5. CPI 归因：事实、假设与更正

| 假设 | 证据与当前判断 |
|---|---|
| store prepare 的非本地服务漏算是 NAMD 主因 | 既有全 ROI 分解显示大部分服务与 FIFO 等待重叠；约 156K 未覆盖服务周期相当于当时周期差约 6%。这是模型内服务分解，不能当作所有 store 间接效应的严格因果上界。 |
| SQ full 计数大，所以 SQ 反压是主因 | 不成立。尝试次数或重叠 stall 位移不能直接换算墙钟；原报告已经撤回这一判断。状态统计也不足以唯一确定 ROB head 的具体等待来源。 |
| sequencer=1 证明全部误差来自 MLP | 不成立。极端串行改变所有请求（含 hit），只能证明并发敏感性。以同一个串行基准分别相减，差值必然等于原 CPI 差，是代数恒等式，不是归因证明。 |
| 缺少同地址合并证明 MSHR 是主因 | 缺口真实，但全部 alias 不等于 miss alias，更不等于退休关键等待。native 存在 L1-hit 父服务；必须对齐实际父身份和回调。 |
| load 响应/依赖/退休关键路径值得优先调查 | 支持。历史 TeaLeaf 全流审计定位到 issued-load-head 等待不足；但版本与口径不同，比例不可套到当前两案，当前 NAMD 尚无完整配对分解。 |
| no-fallback CPI 变化证明修复收益 | 不成立。它保留未收敛服务关系；NAMD 小幅改善、TeaLeaf 过冲都不是合法因果收益比例。 |
| 前端、DTLB、FU 或依赖解析可排除 | 尚不能。需要按相同动态指令区分准入前、服务中、响应后及退休等待，避免把所有 load-head 等待归到 MSHR。 |

因此，旧 store timing 报告 §8.4 的“决定性消融/根因”与 §8.5 将全部缺失 CPI 归到 alias 的表述应视为已被本次证据审查收窄。旧实验保留，不能继续引用其强归因结论。

## 6. store 阶段与资源范围

启用 lifecycle 时，普通 eligible store 区分可选 prepare 与必须 demand，不保证每条发两个请求。直接队头只做原 demand；prepare 在途时 demand 接管响应；prepare 已完成时 demand 再做本地 write，实际 demand 响应释放 SQ。prepare 的 ready 当前仍由 canonical 服务和提交后下界估算，不是完整的实际可选准入模型。

必须区分 CPU/Sequencer outstanding、每行 waiter、各级 MSHR/TBE、SQ。普通 read follower 可不新增下层事务，却仍占 Sequencer 请求容量；optional prepare 的入口规则需要单独核对。

本轮服务修复主要支持 read，试算时 write/atomic 不进入新读服务表；local 还预排除写涉及的行。prepare/demand 仍沿用既有资源账，额外本地 demand 未新增独立 Sequencer 槽位预订。故不能宣称两个 store 阶段的 MSHR 已修复。其竞争可能经 SQ 排空、后继 store、屏障等影响进度，需要单独证据。

## 7. zsim 对照与设计启示

zsim bound 固定访问路径并提供无竞争时间下界，weave 通过依赖事件处理资源竞争；时间推迟是正常结果，资源不可用时 hold/requeue，完成后推动后继。它主要近似掉路径改变的干扰，不是任意重新求解命中和合并关系。见[原论文 §3.2](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf)。

公开 [TimingCache](https://raw.githubusercontent.com/s5z/zsim/master/src/timing_cache.cpp) 区分 miss start、response、收尾释放 MSHR；[TimingEvent](https://raw.githubusercontent.com/s5z/zsim/master/src/timing_event.h) 提供依赖及等待生命周期。它们支持借鉴资源驱动的时间传播，但不能当作已实现按最终到达时间重建所有同地址合并的证据。

FastSim 的潜在优势是可重判服务关系并统一计数，现有劣势是整批重放及保守降级。候选方向是保持批量 CPU 快路径，对访存准入、服务响应、资源释放及受影响依赖做增量传播。**仅时间改变应传播；服务身份/权限/路径确实失效才重分类。** 这仍是待设计验证的方向，不是已经实现的高吞吐方案。

## 8. 下一步工作与验收顺序

> 进展（2026-09-24）：§8-A 的第 1–3 步已在代表性 9 案子集上执行，逐事件配对与退休分解结果见 [buffer-tso-paired-load-retirement-20260924.md](buffer-tso-paired-load-retirement-20260924.md)。要点：起点窗口在途并发集合与 native 一致（不支持“缺 MLP”为主因）；L1-hit 表面的 +1 已裁定为端点口径相位差、完整边 issue→response=2 已与 gem5 对齐（不改常量）；当前首要待查是 LBM LLC-miss 服务时长不落在 native 的 227/142 档而双向离散；TeaLeaf 的全 ROI 累积分歧不在起点窗口。该结果为诊断采集，尚未进入 §8-B 修复。

### A. 先完成当前配置的归因，暂不新增回退旋钮

1. 冻结上述 baseline、两案输入及采集硬件参数；复用维护 validator 与独立 identity 工具。若现有 native 逐事件字段不足，应标记缺失并补采对应诊断，不能从聚合 PMU 推造事件真值。
2. 在同一 common-end 人口按动态指令身份及访存 fragment 对齐。记录 load 的地址/操作数就绪、实际准入、CPU 槽等待、行服务 generation/父身份、下层事务、callback、核心数据就绪/writeback、消费者唤醒和退休。区分 native tick、核心周期及参考时钟。
3. 先构建完整退休时间差分解，再在贡献最大的区段找首个事件分歧；短窗口只用于定位机制，不替代完整 ROI 贡献分布。unknown、无法匹配、边界和 idle 单列并守恒。
4. 对齐响应前后：准入过早、父服务选择错、服务响应过早、response→ready 错误、消费者依赖过早、退休约束不足分别分类。不要把所有 issued-load-head 等待都叫 MSHR 等待。
5. store 单列 prepare admission/response/drop、demand admission/takeover/response、SQ release 和实际资源占用；检验其是否造成同一关键 load 的资源等待，避免按指令类型错误切断因果链。

产物：可复现取证命令、来源/时钟/人口说明、首分歧见证、互斥退休周期账和未解释残差；只给出证据支持的贡献，不把所有请求等待求和当作 CPI。

### B. 根据首分歧选择最小修复

- 若实际准入改变而父服务仍有效，更新时序并传播，不能仅因 issue 不等而降级。
- 若已离开父服务生命周期，重新选择 hit/新事务；失效只传播到依赖该决定的请求和共享资源后继。
- 若响应正确但 writeback/依赖/退休过早，优先修 CPU 边，不扩大 MSHR 模型。
- 若 store 准入争用被证明关键，再补 prepare/demand 各自资源生命周期，接管不能重复分配事务或重复计数。

保留旧路径和显式 fallback；先做能复现 native 分歧的机制测试，并验证同一决定控制响应、资源和 PMU。跨 epoch 保持父身份及释放事件，不能用清表掩盖遗漏。

### C. 准确性与吞吐共同验收

1. 先在相同完整 ROI 核对首分歧及退休账改善，再评估 CPI；每案同时列相对误差与绝对误差，聚合列 MAPE 和 MAE。PMU 用原生守恒来源，MAPE/WAPE 分开。
2. 输出新模型事件覆盖、跨 epoch 引用、重新分类、实际重算节点、预算预跳过、局部降级及整批回退，不能只报告提交率。
3. 用同二进制/同配置的对称串行重复测量宿主吞吐，记录噪声；诊断埋点默认关闭，诊断运行不冒充生产吞吐。停止以提高预算或降低覆盖作为性能达标办法。
4. 两案机制成立且成本有依据后，再回归 Graph500、LBM C4/C32 等历史负例，按已有验证流程扩大范围。生产修改后运行 `cmake --build build -- -j16` 和 `./build/fastsim_tests`。

没有预先承诺 CPI 改善幅度或吞吐百分比。重开模型修复的依据应是可定位、可复现的关键边，而不是“存在很多合并”或“关闭并发后 CPI 变大”。
