# FastSim 的 CCF-A 论文可行性与投稿定位

日期：2026-09-17。性质：**公开文献调研、当前证据审计与研究建议，不是论文实验验收。**

用户指定的两份飞书资料已在取得只读授权后读取：

- 文件 `仿真器调研报告v2.md`，本地保存于
  `tmp/ccfa-research-20260917/仿真器调研报告v2.md`；
- Wiki《【面向自研硬件的能耗软硬件协同方案设计与探索】技术调研报告》，
  底层 Docx `X63MdwLt0ochNGxp9wpcQQpMnKd`，读取 revision 6828。

同时下钻了 Wiki 嵌入的《仿真器技术路线》、同步语义、CounterPoint 解读和
两张 PMU 表。它们是内部技术调研/方案材料，不是经过同行评审的事实来源；
其中会刊归属、FastSim 历史数字和“研究空白”仍须用正式论文、当前仓库产物
及维护验证器校正。综合与校正见第 2.5、3、4.3 节。

## 1. 核心判断

**FastSim 有发展为 CCF-A 论文的基础，但当前证据还不足以支撑一篇完成度高、
贡献清楚的 A 类论文。** 最主要的障碍不是平均 CPI 再降低一个百分点，而是：

1. 没有把新机制与 Sniper 的 interval/IW-centric、ZSim 的 bound-weave、
   早期 FastSim 的 memoization 明确分开，并证明机制自身的不可替代性。
2. 当前精度是反复研究过的 gem5 回归集合上的结果，不是独立工作负载泛化、
   真实硬件精度或目标微架构未知时的部署验证。
3. 现有 Sniper/ZSim 三方实验有价值，但冷 slice、UOP 归一、内核支持差异及
   自定义 frontend 改造，使其不能直接成为正式 macro-CPI 优势主表。
4. 设计空间探索（DSE）、跨 producer 可迁移性和端到端成本还没有闭环。
5. 局部无损优化、FastSim 模型误差、对真实硬件的误差，是三个不同问题。
   不能把前者的 bit-identical 当作后两者的正确性证明。
6. 两份参考资料强化了“多核 trace-driven、显式共享状态、动态窗口、
   可解释 PMU”这一问题组合的价值，但也暴露出两个尚未兑现的核心前提：
   FastSim 目前没有功耗/电压/温度模型，也没有可重排 pthread 竞争结果的
   block/wakeup 调度语义。不能把接口或规划写成已实现贡献。

续审还发现：当前回归集合的 **response-activity 证书只覆盖 0.0000362% 的
accepted UOP**；不能用早期合成流或旧语料的收益解释当前整体吞吐。
private-preview 证书有较广事件覆盖，但历史开关消融已经显示收益随负载变化；
materialized kernel 则有约 1.7% 的历史配对净收益。第 4.5、4.6 节据此把
“稀疏执行”从既有优势叙述收紧为待筛选的研究路线，不能再把已有消融说成未做。

**推荐顺序：**

- **期刊优先：ACM TACO；其次 IEEE TC。** TACO 已列入 CCF 2026 第七版
  体系结构 A 类期刊，不能继续依据旧分类把它排除。它也已有高层机制型核心
  模型的直接先例 [R3]。期刊允许更充分呈现模型、边界、消融与长尾，但不降低
  创新性要求。
- **会议主攻：HPCA / MICRO。** 前提是形成明确的仿真算法贡献和可信的
  精度—速度联合优势，而非仅整理工程功能。
- **ISCA 是更高风险的冲刺目标。** 需要能改变社区仿真方法、可复用的核心机制，
  或足够强的应用牵引和开放生态证据。
- **DAC / TCAD 是有条件的第二主线。** 必须把 DSE 的设计选择质量、搜索成本
  和功耗/面积/性能决策收益做实，不能只把 CPI 预测工具改名为 EDA 工具。
- **ASPLOS / SC 不建议按现状优先。** 前者需要突出的体系结构与软件/OS 交互
  贡献，后者需要 HPC 工作流和规模化价值；CPU 参数可配、多跑几个进程不够。

结合指定资料后，**近期最可信的是“快速、可审计的多核性能/DSE”论文，
不是“性能+功耗一体化仿真器”论文**。后者只有在完成真实功耗标签、事件域迁移、
动态 DVFS 误差传播和同步语义后，才适合作为 DAC/TCAD 或 ASPLOS 主线。

这是审稿式判断，不是录用概率预测。没有普遍适用的“MAPE 达到某数就能中 A”
门槛，发表可行性也不等于论文现在可以投稿。

**命名风险：** Eric Schnarr、James R. Larus 已在 ASPLOS 1998 发表
*Fast Out-of-Order Processor Simulation Using Memoization*，系统也叫 FastSim
[R1]。建议论文采用可区分的新名称或明确限定词，并引用前作；本次不改仓库名。

## 2. 当前有哪些能站得住的证据

### 2.1 本轮核验范围

已阅读项目工作流、[报告合同](accuracy-reporting-contract.md) 第 0、8 节，
并核对以下实际产物，而不是只引用历史对话：

| 证据 | 本轮检查 | 能支持什么 / 不能支持什么 |
|---|---|---|
| `tmp/baseline-40-20260916/native-rescored/summary.json` 和 40 份 `cases/*/validation.json` | 案例数、状态、来源、人口标记、归档 provenance；从已评分逐案字段重算 CPI 聚合 | 支持既有预测的 gem5 诊断精度；不认证原始二进制、独立泛化和硬件 PMU |
| `tmp/first-core-common-end-20260911/source/` | 40 案均声明功能预热、combined scope、共同终点；600 个逐核条目 | 支持本批输入范围声明；声明字段本身不等于逐事件完整证明 |
| `tmp/ccfa-research-20260917/identity.json` | 本轮运行维护身份验证器；显式比对预期 10×4 案例集合，40/40 有效，无漏案、无多案 | 仅证明实际 gem5 目标身份门通过 |
| `tmp/throughput-analysis-20260916/cmp-full-40/c{4,8,16,32}/summary.csv` | 每档 30 行，合计 120 行；核对计量字段和中位吞吐；查看 binary/config manifest | 历史定制 trace-replay 比较，不是新计时，也非官方原生运行方式的全面比较 |
| `tmp/uarch-generalization-c4-c8-20260917/collector-state.json`、`smoke/inventory.json`、`formal/collection-finished.json` | failed；smoke 34 完成/102 失败；formal 标记为 incomplete | 新 136 案没有完成泛化验证 |
| `configs/gem5-fs-native-kernel.cfg`、`src/simulator.cpp`、相关机制文档 | 当前入口包含 v28.9/load-ready、store-wait relief v2；pending-fill 关闭；materialized 反馈路径存在 | 实现存在不等于其所有候选设计、正确性主张和论文指标都已兑现 |
| 用户指定飞书文件 `APiDb2gmjoMqrAxnEF5cTgBqnSg` | 下载原始 Markdown，SHA256 `31ea20fb...d63ad80` | 提供 2026 相关工作与研究机会线索；其 FastSim 行和部分 venue 需要校正 |
| 用户指定 Wiki `XFOnwGbVCiwNUkkdScwcyVMRnlf` | 解包为 Docx `X63M...MnKd` revision 6828；读取整篇、CPU/PMU/功耗/多核章节及参考文献 | 提供性能—功耗需求、基准和 PMU 目标；不是当前 FastSim 功能/精度证明 |
| 嵌入路线/表格 | 读取技术路线 revision 1699、同步语义 revision 20、CounterPoint revision 6、PMU 表 revisions 9/38 | 用于识别尚未实现的调度、功耗和校准语义，不把未来设计算作贡献 |

本轮不改模型、不重新运行 gem5/FastSim、不重写参考选择逻辑、不做吞吐实验。
身份验证命令：

```bash
python3 tools/validate_fs_oracle_identity.py \
  --result-root tmp/first-core-common-end-20260911/source \
  --output tmp/ccfa-research-20260917/identity.json
```

### 2.2 CPI：不错的研究起点，但不是独立测试集

集合为 stockfish、omnetpp、zstd、lbm、sph_exa、tealeaf、nab、graph500、
namd、neutron，C4/C8/C16/C32，共 40 案、600 条逐核 trace。
scope 为 `user-plus-kernel`，有功能预热，`first-core-target-common-end-v1`。
它已用于模型研究及回归，**不能称 workload-held-out**。

以下均来自既有输出，非本轮新预测。CPI 为 active cycles / macro instructions；
绝对误差为 `abs(FastSim CPI - gem5 CPI)`，单位 cycles/macro instruction。

| 集合 | 案例数 | CPI MAPE | CPI MAE | 解释 |
|---|---:|---:|---:|---|
| 汇总宏指令人口一致子集 | 33 | 5.0614% | 0.054931 | 总量一致不自动证明逐核/逐事件一致 |
| 完整集合，仅诊断 | 40 | 5.6999% | 0.055726 | 包括 7 案人口差异，不丢弃也不强改分母 |

按原始已评分数值摘取的尾部例子：

| 案例 | gem5 CPI | FastSim CPI | signed 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| LBM C32 | 3.906000 | 4.158580 | +6.4665% | 0.252580 |
| TeaLeaf C16 | 0.646776 | 0.560665 | −13.3139% | 0.086111 |
| TeaLeaf C32 | 0.501256 | 0.441014 | −12.0183% | 0.060242 |
| NAMD C32，人口不一致诊断 | 0.539028 | 0.482635 | −10.4621% | 0.056394 |

LBM 不是相对误差最坏点，却是重要的绝对周期误差尾部。不能只优化 MAPE，
也不能只用平均值掩盖不同核和不同机制的误差抵消。

### 2.3 PMU：参考来源修复后的正确起点

使用维护验证器选择的原生 Ruby/SLICC cache summaries；branch 为原始退休
BPred 事件。均为 combined scope 的 40 案既有评分；百分位为 Type 7。

| 事件 | finite APE cases | MAPE | P50 | P90 | P99 | WAPE | signed bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| Branch miss | 40/40 | 3.2198% | 2.1042% | 9.3708% | 13.1128% | 1.7054% | −0.6745% |
| L1D tag miss | 40/40 | 2.4059% | 2.1234% | 4.8914% | 8.1627% | 2.0290% | +0.9871% |
| Private-L2 tag miss | 40/40 | 3.4008% | 1.5709% | 9.1444% | 17.7623% | 2.1627% | +1.4126% |
| LLC tag miss | 40/40 | 4.5141% | 3.0728% | 11.7173% | 19.9927% | 1.9662% | +1.9662% |

`mapping` 仍为 branch/L1D/private-L2 的 `proxy` 和 LLC 的 `diagnostic`。
这里“原生参考源”与“可严格对应硬件 PMU”不是一回事；不能写成硬件 PMU
误差仅几个百分点。MAPE 是配置等权，WAPE 是计数加权，不能交换名称。

[9 月 16 日来源纠错](private-l2-pmu-source-repair-20260916.md) 已撤回 nab
“inclusion/owner-supply 导致巨大 L2 误差”的旧判断。旧 cache path-class
代理不是原生 truth。**论文不能复用已被撤回的根因故事，也不能把换正确参考源
产生的报表改善当成模型创新。**

### 2.4 三方对比：是线索，不是可直接放摘要的优势倍数

[现有三方文档](sniper-zsim-fs-trace-driven-comparison.md) 第 0 节采用冷 slice、
`cores × max(core cycles) / mixed UOPs`，并放宽含内核记录的 branch 守恒检查。
它虽引用报告合同，但不满足当前正式 active macro-CPI 定义。不要把其 UOP
MAE 换个列名当 macro MAE，也不要与上面有预热的 33/40 案数字拼接比较。

归档 CSV 的 simulator-only **mixed UOP/s** 中位数如下；不含采集成本，不是
正式 `scope_metrics.throughput.user_uops_per_second`，本轮未重新计时：

| 目标核数 | FastSim，M mixed UOP/s | Sniper，M mixed UOP/s | ZSim，M mixed UOP/s |
|---|---:|---:|---:|
| C4 | 6.35 | 1.68 | 9.42 |
| C8 | 7.83 | 2.71 | 8.89 |
| C16 | 7.69 | 5.57 | 9.50 |
| C32 | 7.55 | 8.92 | 9.77 |

因此**当前归档数据连“FastSim 总是最快”都不支持**。它提示可能存在有价值的
精度—速度联合点，但尚不能宣称严格 Pareto 支配。
原生 Sniper/ZSim 与本地定制版本也必须分开命名；FST 适配、依赖、FU、DRAM
改造和单线程离线 weave 可能影响结果，不能把改造版表现等同于原论文上限。

### 2.5 指定参考资料的综合与校正

两份资料提出的核心需求可以归纳为：

1. 多核 OoO、非逐周期、trace-driven，并显式建模 cache/coherence/NoC/DRAM；
2. 输出能支撑组件级功耗模型的 PMU/活动指标；
3. 通过窗口 API 支持 DVFS/DPM 控制；
4. 解决原生 PMU/功耗与插桩 trace 难以严格时间对齐的问题；
5. 在新微架构 timing 下重新处理同步、阻塞与唤醒，而非回放原机器竞争结果。

这些需求大多是合理的，但“项目需求”不等于“当前能力”。对照如下：

| 资料主张/需求 | 当前 FastSim 实证状态 | 论文影响 |
|---|---|---|
| trace-driven × interval × 多核显式共享状态是稀缺组合 | 已有 OoO interval、目录/MESI 近似、CHA/LLC/DRAM 和静态 C4–C32 回归；但 BZSim、ChampSim、SiNUCA 等缩小了“无竞争者”表述空间 | 可作为系统组合定位，不能仅凭象限空白宣称算法创新 |
| FastSim 支持 C4–C64、mean 4.6–7.7%、PMU WAPE <0.3%、5–51.5M UOP/s | 当前维护证据只正式审查 C4/C8/C16/C32；33 案 CPI MAPE 5.0614%、MAE 0.054931；四个 PMU WAPE 为 1.7054%–2.1627%；严格吞吐和旧 51.5M 不是同一口径 | 资料中的 FastSim 行已过时，不得直接放论文摘要或对比表 |
| 输出 PMU 即可支撑功耗模型 | 当前 formal PMU 输出含 branch、L1D/L2/LLC、DRAM、DTLB 等，但维护 validator 只评分 branch 与三类 cache tag miss；ITLB、NoC bytes/packets/congestion、directory forward/entries、同步等待等目标特征未形成完整正式合同 | 必须先定义功耗特征字典、来源和覆盖；“字段存在”不等于活动精度或功耗精度 |
| 当前可做性能+功耗联合仿真 | FastSim 没有 voltage、power、energy、thermal、clock-gating 或 transition-latency 模型 | 只能称“为外部功耗模型提供候选活动输入”；不能称功耗仿真器 |
| 动态 DVFS 是差异化机会 | C++/Python 窗口 `advance()` 与 per-core frequency action 已实现；但异频周期和不是 makespan，跨窗口 outstanding shared response deadline 尚未完全物理化，也无 OPP/电压/功耗模型 | API 是资产，不是 DVFS 精度结果；论文需报告窗口级性能、功率、能量及控制 regret |
| drmemtrace 可作为用户输入 | 原始 drmemtrace 仍不是当前输入；DR→FST lowering/companion 路线有已完成部分，也有 `.imap`、strict physical address 和一致性门缺口 | 不能把规划中的 portable producer 当作已部署能力 |
| 捕获 pthread 后可在新 timing 下重排竞争 | 资料正确指出纯 trace 固化原机器 CAS/spin/futex 路径；当前 FastSim 仍是 `threads <= cores` 的静态注入绑定，无 time-slice、migration、block/wakeup 或 context-switch cost | 这是重要研究问题和硬限制；未实现前，工作负载泛化应排除 timing-sensitive synchronization 或明确为条件回放 |
| 用全局 PMU 一致性绕开 trace/功耗时间对齐 | 可用于整段语义/模型反驳，但不能生成窗口级功耗标签，不能验证事件的时间位置，也不能替代 DVFS 瞬态标签 | 适合作为验证门或 CounterPoint 集成方向，不足以解决动态功耗训练 |

资料中的 **方案一**（native PMU→功耗训练、FastSim PMU→推理）是近期更可行
的功耗路线，但存在 covariate shift；需分别测“真实事件→功耗”“仿真事件→功耗”
以及 PMU 校准层的增益。**方案二**（marker/统计锚点实现 trace—PMU—功耗窗口
对齐）信息更强，但尚无已验证入口，不能作为当前论文贡献。整段统计一致性可以
作为辅助 gate，不能取代窗口对齐。

指定资料还扩大了基准范围：除了当前 SPEC/SPEChpc 子集，最终功耗论文应至少
覆盖 core-dense、memory/uncore-dense、irregular/synchronization、kernel-heavy
和服务型负载；DCPerf/CloudSuite、STREAM/HPCC、Graph500/TailBench 等用于补足
功耗与尾延迟特征，而不是简单把更多 benchmark 名称堆进表格。

## 3. 相关工作：创新主张必须越过哪些前作

检索覆盖 interval/IW 模型、并行 bound-weave、memoization、trace-driven、
采样、学习型和 FPGA 仿真，以及功耗工具链。以论文/作者页面、正式目录和
官方会议页面为主；是针对性的相关工作调研，不宣称穷尽所有论文。
不同论文的 MIPS、UOP/s、目标核数、宿主预算、硬件参考和 gem5 参考不能横比。

| 工作 | 已有核心思想 | 与 FastSim 的关系及论文必须回答的问题 |
|---|---|---|
| **FastSim，ASPLOS 1998** [R1] | speculative direct execution；缓存微架构状态与动作的 memoization | 无损跳过重复仿真不是新概念；新工作须说明在线证书与状态缓存的区别、状态覆盖及多核共享依赖 |
| **Sniper，SC 2011** [R2] | interval 解析模型与并行仿真结合，权衡速度和精度；有硬件验证 | “非逐周期、显式资源、快速多核”不足以新；必须覆盖当代实现而非只打 2011 年数字 |
| **IW-centric，TACO 2014** [R3] | 比 interval 更细的指令窗口机制模型 | dependency/ROB/IQ/FU 不能独立算新贡献；需要解释新增抽象、状态与误差收益 |
| **ZSim，ISCA 2013** [R4] | instruction-driven timing、bound-weave、用户态虚拟化；区分 path-preserving / path-altering interference | 最直接的算法前作；不能称 ZSim 只有简单 penalty、无 OoO 或无共享争用；应比较路径改变、跨区间反馈和修复边界 |
| **Graphite，HPCA 2010** [R5] | 分布式并行与宽松同步的大规模多核模拟 | 工作者并行、批次同步和大核数都已有先例；独立 case 并行更不是目标机多核扩展算法 |
| **gem5，2011 / 20.0+ 2020** [R6] | 可配置系统/CPU/内存模型、完整软件栈和多种精度层级 | 是参考和高细节基线，不是真实硬件本身；FastSim 回放已退休内核流不等于重新执行 OS |
| **ChampSim / 行业 trace 研究，IISWC 2023** [R7] | trace 驱动的分支/cache/预取等研究生态 | 应做适用子域比较和公开 trace 复现；不能强求其承担不支持的完整 OS/coherence 功能然后判输 |
| **SimPoint / SMARTS，2002/2003** [R8] | phase 代表性和统计采样，减少需详细模拟的指令量 | 必须比较“采样 gem5”而不仅完整 gem5；采样与快速内核可组合，不能把正交方法说成互斥 |
| **LiveSim，HPCA 2016** [R9] | checkpoint、并行采样和在线置信估计，强调交互式 time-to-result | “秒级返回”并不等于更快逐指令仿真；要比较相同精度和代表性预算 |
| **SimNet，POMACS/SIGMETRICS 2022** [R10] | instruction-centric 学习延迟模型及 GPU 推理 | FastSim 可能优势在无训练、可解释状态和参数化；不能假设学习模型不做动态状态建模 |
| **GPU 学习型仿真，SC 2022** [R11] | 子 trace 并行、数据移动优化、误差分析和修正 | 学习型基线成本要含 GPU、训练、数据；不能只用本地 TCSim 的一个慢版本代表全领域 |
| **Tao，POMACS/SIGMETRICS 2024** [R12] | functional-only inference、多指标预测、跨微架构迁移学习 | “只吃 functional trace”“同时预测 CPI/PMU”“可跨参数”均已有强前作；差异应落在无再训练及共享状态语义 |
| **FireSim，ISCA 2018** [R13] | FPGA 加速、RTL/系统级 cycle-accurate 仿真 | 有更高保真和完整执行，代价是硬件和工具链；FastSim 的价值是 CPU 软件可部署性，不是保真度胜出 |
| **McPAT；CoMeT 工具链** [R14] | 活动统计驱动功耗/面积/时序或热建模；已有 Sniper 集成 | “输出 PMU 接功耗模型”本身不是贡献；需证明活动/时间误差传递、域迁移和实际决策收益 |
| **Nugget，HPCA 2026** [R15] | LLVM IR 级可移植程序片段，跨 binary/ISA/架构采样及硬件验证 | 新的可移植评估前作；FST 格式统一不等于语义可迁移，也不等于采样代表性得到证明 |
| **BZSim，ISPASS 2024** [R17] | 延续 ZSim 的大规模微架构仿真，并加强详细互联建模 | “ZSim 已停滞所以空白”不成立；若主张 NoC/规模优势，必须纳入当代对照 |
| **Concorde，ISCA 2025** [R18] | 解析组件边界分布 + ML 融合，约 2% CPI 误差、极高速度 | 单核且主要输出程序级性能，但显著抬高“快+准”的基线；FastSim 要靠多核共享状态、窗口/活动和无训练差异化 |
| **PerfVec，SC 2024** [R19] | 解耦程序与微架构表示，支持跨程序/配置性能预测 | “跨微架构无需重建程序表示”已有强 ML 竞争；FastSim 必须以参数语义、可解释事件和真实 DSE regret 证明价值 |
| **QFlex 3.0，2025/ISPASS 2026 keynote** [R20] | full-system ARM、统计采样、可复用 checkpoint 和误差界 | 只与完整 gem5 比速度不再充分；需要同算力预算下与 sampled detailed simulation 比较，并给统计置信 |
| **CounterPoint，ASPLOS 2026 Best Paper** [R21] | 用 HEC/PMU 约束反驳和细化微架构假设，定位不一致部件 | “PMU 语义审计/模型反例”已成为强竞争赛道；FastSim 需证明多核、动态时序、模拟器校准上的新增能力 |

补充检索到 **CAPSim**（arXiv:2510.10484）和 **NeuroScalar**
（arXiv:2509.22410）[R16]，都涉及学习型 CPU 性能预测。后者页面带 ASPLOS
模板和占位 DOI，**本轮未证实正式录用，不能标注为 ASPLOS 2026 已发表论文**。
它们需在实际投稿前更新正式版本与 artifact；不能因尚未确认 venue 就忽略
其对“功能特征可迁移”主张的竞争。

文献定位校正：SimNet 的完整论文是 POMACS 6(2), 2022，
DOI `10.1145/3530891`；另一个 DOI `10.1145/3489048.3530958` 是
SIGMETRICS/PERFORMANCE 两页摘要，不是 ISCA 完整论文。Tao 应优先引用完整
POMACS DOI `10.1145/3656012`，不是指定 Markdown 所写的 HPCA 2025。
NeuroScalar 的预印本带 ASPLOS 2026 模板，但本轮在官方 programme 搜索中未得到
可确认条目，继续按预印本处理。CounterPoint 的 DOI、ASPLOS 2026 正式论文和
Best Paper 已由 ACM/会议官方页面核实。已用 Crossref/正式页面交叉核对。

### 3.1 结合 2022--2026 前沿路线后，FastSim 还剩什么位置

本次补充调研进一步确认：2022--2026 年没有出现一个公认的、同时满足“开源软件、
详细乱序核心、共享内存多核、真实多线程/OS、接近 ZSim/Sniper 速度”的全面继任者。
但这不等于 FastSim 只要补齐功能表就有创新；近期工作已经把“快速评估”拆成三条
竞争路线：

| 路线 | 代表工作 | 对 FastSim 的挤压 | 仍留下的研究空间 |
|---|---|---|---|
| **减少需要详细模拟的工作量** | LoopPoint（HPCA 2022）[R22]、Viper（IEEE Access 2024）[R23]、loop-bounded checkpoints（TACO 2026）[R24]、Nugget（HPCA 2026）[R15] | 只与完整 gem5 比 replay 速度已经不够；代表区间、checkpoint 和 portable snippet 可直接加速 gem5/Sniper/RTL | FastSim 应与这些方法组合，并比较相同总预算下的精度、设计排序和 time-to-result，而不是把 sampling 当互斥竞品 |
| **把高保真推向 RTL/FPGA** | FireSim、FireAxe（ISCA 2024）[R25]、Assassyn（ISCA 2025）[R26]、TraceRTL（HPCA 2026）[R27] | “比逐周期软件模拟器快”不是终点；已有方法能保持 RTL 对齐或 cycle-exact，并支持 Linux/复杂 OoO 设计 | FastSim 的价值只能是普通 CPU 上更低部署成本、更快迭代和更宽 DSE；不能声称在最终保真度上胜出 |
| **用分析/学习模型筛大空间** | SimNet、Tao、Concorde（ISCA 2025）、CAPSim 预印本 | 单核程序级 CPI 的“快且准”门槛已很高，单纯报告平均 CPI 难以构成优势 | FastSim 必须证明显式多核共享状态、无再训练的参数语义、窗口活动/PMU、可解释失效和设计 regret，而不是与代理模型比绝对推理速度 |

Virtuoso（ASPLOS 2025）[R28] 还表明 Sniper 可以通过轻量 OS/MMU 模型扩展虚拟内存研究；
因此“支持 DTLB/page walk 或部分内核记录”也不能单独作为贡献。Phalanx（CAMS
2025）说明新的模块化 OoO 模拟器仍在出现，但其 workshop/单核原型状态不足以证明
通用多核替代已经完成。CAPSim 等预印本继续按预印本处理，不能用未确认会刊状态
夸大或缩小竞争。

这三条路线反而把 FastSim 的近期论文定位收紧为：

> **在普通 CPU 上，对给定功能路径提供显式共享状态、可审计 PMU 和参数敏感性的
> 中间保真度多核模拟，并以实际设计选择质量证明它比 sampled detailed simulation
> 和纯代理模型更有用。**

这个定位有真实空隙，但“中间保真度系统组合”本身仍不是足够强的算法创新。
若要冲 HPCA/MICRO，必须完成一种可隔离的新机制，例如有充分条件和原子回退的
certificate-guided sparse replay，并证明它在同一精度下显著改善成本；若机制收益
不成立，则应按 TACO/TC 路线强调可审计建模方法、DSE 决策质量和明确适用边界。

### 3.2 为什么不能直接拿原论文数字“证明更优”

Sniper 原论文报告硬件对照与特定 16 核目标；ZSim 原论文覆盖 Westmere 硬件、
千核目标及不同 core model。当前 FastSim 对照的是另一种 gem5 配置和工作负载，
输入也是本地适配的 FST。即使有相同的百分比数值，也不是同一实验问题。
本报告不把这些外部数字与 FastSim 的 CPI MAPE/MAE 做胜负表。

## 4. FastSim 能提出什么贡献

### 4.1 已有能力、候选创新、未完成目标要分层

| 候选贡献 | 当前可确认基础 | 创新性判断 | 必须补的关键证据 |
|---|---|---|---|
| 功能 trace + 机制型 OoO + 显式共享内存状态 | 配置、实现和既有回归存在 | 有工程价值；组合本身新颖性偏弱 | 超过 IW-centric / bound-weave 的具体机制与隔离消融 |
| **基于状态证书的私有路径优化/响应计算** | 两类机制均可达；40 案 private-preview 事件覆盖 87.0845%，response-activity UOP 覆盖仅 0.0000362%；两者分母不同；历史 preview 消融已有混合收益 | 是有实现和局部证据的候选，不是已证明有普遍提速的主贡献；response-activity 不支持大范围跳过主张；全局 certificate 未完成 | 给定规范模型的充分条件、入口/出口状态等价、跨区间条件、失败原子回退，以及解释负载间收益变化的机制；详见 4.5、4.6 |
| **面向 native-kernel trace 的可审计 CPU/共享状态近似** | FST、CPL/ASID、预热/共同终点、原生 PMU 来源与守恒 | 有方法学潜力；“修了验证脚本”还不是研究贡献 | 将错误来源推广为可复用的失效类别与检测方法，在多 producer/独立模型上证明作用 |
| **无再训练的参数敏感性与 DSE** | 参数可配；已有 136 案设计 | 是重要目标，不是已证优势 | 参数方向、幅度、配置排序、最优设计 regret，以及新负载/新组合留出 |
| gem5-FST / QEMU-FST 统一入口 | [双 producer 设计](dual-producer-fst-convergence-design-20260916.md) 有已完成部分，也列出 `.imap`、严格性和一致性门缺口 | 有实用潜力，不能只凭相同格式称可迁移 | 同一程序事实与 lowering、依赖、地址、分支、CPL 的差分验证及端到端新负载测试 |
| 为功耗 ML 提供语义稳定的活动与时序特征 | 外部窗口/API 和指标基础 | 应用贡献候选，不是已验证功耗模型 | 独立功耗 ground truth、训练/测试分离、时间和事件双误差传播、DVFS/能量决策质量 |
| 同步语义可重排的多线程 trace replay | FST 可保留 thread/syscall/maybe-blocking 元数据，资料给出 pthread semantic event 设计 | 当前没有 block/wakeup、迁移、时间片和重新决定锁赢家的 scheduler；只能算研究问题 | pthread/futex/barrier 语义 trace、控制流切片规则、调度状态机，以及与真机/FS 的竞争顺序和尾延迟验证 |
| PMU 约束驱动的自动校准 | 已有 event dictionary、原生来源审计、机制反例；CounterPoint 提供约束化先例 | 直接称“首个自动 PMU 校准”会与 CounterPoint 冲突；多核/uncore/动态窗口可能构成增量 | 形式化约束、噪声/多路复用处理、不可行证书、避免调参过拟合，以及独立硬件闭环 |

历史 response-activity 证书的 **92 案等价记录**只说明当时某个规范 FastSim
路径与优化路径一致；本轮没有重新运行这 92 案，也不能据此认证当前默认的
所有优化。原设计明确 shared timing certificate 未完成。不要把
[设计文档](conflict-certified-sparse-weave.md) 的最终愿景写成已实现贡献。

### 4.2 优先检验的论文主线，而非已经成立的贡献

工作标题方向（不是已查重的新系统名）：

> **Auditable Multicore Simulation from Functional Traces**
>
> 如何让低成本的显式状态近似具有可审计的适用边界，
> 并证明它保留微架构设计选择，而不只拟合一个基准配置？

暂不在标题突出 `Sparse`：第 4.5 节证明现有 response-inactive 快路的覆盖
不足以支撑这一总体叙事。只有 private-path/后续稀疏机制的独立收益和新颖性
通过检验，再把它升级为标题和摘要的核心贡献。

可组织为三个贡献，但每条都须由实验/论证兑现：

1. **机制：** 从 private-path 状态证书已有的混合收益消融出发，解释其有效
   条件并明确与 bound-weave 的差别；不是重做一个已存在的 on/off 实验。
   response-activity 先列为局部优化，不据此宣称大部分 UOP 被跳过。
   只主张确实覆盖的状态和操作。
2. **方法：** 定义功能输入、预热/共同终点、参考事件来源、参数身份和模型
   误差的分层证据链，揭示“数值接近但原因错误”的可复现反例。
3. **效用：** 在独立负载和新参数组合上，以更低总成本保留设计方向、排序
   和低 regret，同时给出 CPI/PMU 长尾，而非仅平均速度。

关键不变量应写清：优化路径 `F_opt(S, X)` 与规范路径 `F_ref(S, X)` 的
目标状态、事件序和输出等价，并不推出 `F_ref` 与 gem5/硬件等价。
证明应覆盖依赖、queue/calendar、跨 epoch 状态、可见填充、coherence 和
失败回退中的实际子集；没有覆盖的部分就明确排除。

如果现有局部证书的覆盖率和净收益不足，不建议立即投入又一次大规模
共享因果闭合重构。先利用已有实现做贡献隔离。若最终没有可迁移的新机制，
应把论文收窄为可信评估/模型研究，而不是硬包装“通用新仿真范式”。

### 4.3 结合指定资料后的三条论文路线

指定资料不是要求把所有愿景塞进一篇论文。按当前完成度，三条路线应分开：

| 路线 | 可投稿问题 | 当前成熟度 | 审稿门槛与建议 |
|---|---|---|---|
| **A. 可审计多核性能/DSE** | 功能 trace 下显式共享状态的精度—成本—设计决策质量 | 最高；已有系统、CPI/PMU 和机制审计，但泛化/DSE 未完成 | **近期主线**。先按 TACO/TC 完整化；机制强且结果突出再投 HPCA/MICRO |
| **B. 性能—功耗—DVFS 联合仿真** | 窗口级活动→功率/温度→控制决策的闭环误差传播 | 低；只有频率窗口 API 和候选 PMU，没有功耗/温度/电压模型与 ground truth | 先做应用/第二篇；完成后更适合 DAC/TCAD，若形成系统/控制创新再看 ASPLOS |
| **C. 同步语义与 trace 可迁移** | 在新 timing 下重建 lock/futex/barrier 因果，而非固化原执行结果 | 设计问题清楚，生产 scheduler 未实现 | 若能提出一般化语义 trace 与高效重放算法，可能是 HPCA/ASPLOS 强路线；当前不可作为贡献 |

路线 A 的贡献可以表述为“**可审计的条件性能预测**”，而不是“完整重执行所有
软件行为”。其必要边界包括：给定已退休功能路径、静态线程绑定和 producer
语义，预测目标微架构上的性能与活动；对会改变控制流、同步赢家、OS 调度或
物理地址的设计变化，必须 fail closed 或单独验证。

路线 B 的最小正确实验不是接一个 McPAT 文件，而是四路误差分解：真实 PMU→功耗、
FastSim PMU→同一功耗模型、只替换时间、同时替换时间和事件；再在 DVFS 窗口中
验证功率、能量、EDP 和策略 regret。指定资料所列 NoC/Directory/同步活动目前
没有完整 formal truth，因此先从已验证事件子集起步，缺失特征明确标 unavailable。

路线 C 揭示了当前 trace-driven 主张最重要的限制：低层 trace 记录的是原执行的
竞争**结果**，而 DSE 需要同步**语义**。仅增加 pthread marker 仍不够；还需要决定
如何从 trace 中删去/替换原 CAS-spin-futex 实现路径、维护锁状态和线程可运行集，
并验证新控制流。未完成前，不能称 FastSim 支持一般多线程调度探索。

### 4.4 不应独立列成贡献的项目

环形缓冲区、少分配、CPU 亲和性、NUMA、独立实验并行、完整 RAW 附件、
支持更多 op-class，以及把 reference 字段选对，通常是实现或实验质量要求。
可以服务于一个新算法，但单独堆成三点贡献很容易被审稿人判为增量工程。

同样，[两阶段天花板诊断](two-stage-accuracy-ceiling-20260916.md) 的失败案例
不能证明所有两阶段方法存在普适误差下界。[9 月 17 日路线图](optimization-roadmap-20260917.md)
已收紧这个结论。论文应写“当前实现及已研究路线的实证限制”，而不是“不可能定理”。

### 4.5 续审：当前默认的证书究竟覆盖什么

本节是**当前源码门控审查与既有 40 案实现计数盘点**，不新跑模拟器、不做
CPI/PMU 评分或吞吐测量。原始来源仍是第 2 节的归档 JSON；源码仅解释目前
的调用与计数含义，不代替历史二进制身份认证。
可复核产物为 `tmp/ccfa-research-20260917/mechanism-coverage.json`，包含逐案
原始文件 SHA256、当前源码 SHA256、开关值及计数；生成命令：

```bash
python3 tmp/ccfa-research-20260917/audit_mechanism_coverage.py
```

该脚本只汇总实现计数并检查分区，不选择 gem5 参考源，不替代维护验证器。

**配置与可达性：**

- `KeyValueConfig::load()` 先读 include，再以当前文件覆盖；维护入口依次沿
  v28.9 → v28.6 → v28.5 → v28.4 → v28.2 → v28.1 native → v28.1 time-epoch
  继承。当前 `response_activity_certificate`、`interval_private_preview`、
  `response_materialized_uop_fast_kernel` 均为 true；归档 40 案也全部记录为 true。
- `compute_core_timing_feedback_impl<false,true,false>` 仍调用
  `try_compute_response_inactive_segment()`，并非 fast kernel 把证书完全关掉。
  但调用针对**整个 accepted prefix**，要求至少 64 UOP、没有 memory event，
  并排除序列化/屏障及多种反馈/审计模式。
- 证书内部仍按 UOP 检查依赖、ROB/IQ、writeback、dispatch/commit 等条件并
  重建退出状态；成功是“用 reduced transition 代替完整响应计算”，不是
  O(1) 跳过整个指令区间。完整 fast-kernel 路径也仍有逐 UOP 循环。
- `prepare_measurement_phase()` 调用 `initialize_stats()` 清零实现计数；
  本节 accepted UOP 为测量阶段 **mixed user-plus-kernel UOP**，不是
  measured user UOP，更不能用作正式吞吐分子。

**归档完整集合的覆盖盘点：**

| 实现计数/比值 | 40 案合计 | 解释 |
|---|---:|---|
| `interval_accepted_uops` | 5,256,566,858 | 此处响应计算覆盖分母 |
| `response_activity_candidates` | 17 | 通过基本长度/无访存筛选后才计数 |
| certified / fallback segments | 15 / 2 | 88.2353% 的候选通过率，不是指令覆盖率 |
| `response_activity_certified_uops` | 1,903 | 仅 5/40 案非零，35/40 案为零 |
| activity UOP 覆盖 | 0.0000362023% | `1,903 / 5,256,566,858 × 100%` |
| `response_materialized_fast_kernel_uops` | 5,256,564,955 | 剩余 accepted UOP 均计入专用完整反馈路径 |
| `batch_memory_events` | 1,023,013,369 | private-preview 的事件分母，不是 UOP 分母 |
| `private_preview_events` | 890,886,481 | safe private-preview 事件；覆盖 87.0845% |
| private unsafe / bypass events | 2,002,802 / 130,124,086 | 不能把 bypass 当作 certificate 通过 |
| `timing_certificate_failures` | 0 | 不能解释成全局共享时序认证通过 |

逐案均验证：
`activity_certified_uops + materialized_fast_kernel_uops = interval_accepted_uops`，
`private_preview_events + unsafe_events + bypass_events = batch_memory_events`，
以及 `certified_segments + fallback_segments = activity_candidates`。
这些分区只核对计数，不证明状态等价，也不证明节省了多少宿主时间。

activity 非零仅 Stockfish C4 和 Neutron C4/C8/C16/C32。LBM、TeaLeaf、
Graph500 等在此集合的该快路覆盖为零。因此早期 92 案约 13.8% UOP 覆盖和
纯计算流提速记录不能外推为当前默认 40 案的实际收益；这是语料/路径范围
不同，不是本轮测出性能回退。

**private-path 与 shared timing 不能混淆：**

`plan_private_preview()` 按 core/private-set 组件和可能失效的次序截断安全
前缀；inclusive LLC、atomic 等情况会收紧或关闭预览。`run_interval_weave()`
还要求 time-epoch、最小 batch 等条件，并可能因低收益绕过预览。私有处理
之后仍将 write/atomic/eviction/共享访问等交给共享 replay；因此 87.0845%
不是同等比例的共享工作消除，更不是同等比例的加速。

归档 40 案 `interval_reweave_passes=1`、`interval_causal_timing=false`。
源码中 shared-conflict reweave 的主要认证分支要求 `interval_reweave_passes>1`；
其他 page-walk/修复分支也有各自前提。零 `timing_certificate_failures` 不能
证明执行了全局 timing certificate，更不能证明跨区间真实到达闭合。

**对论文计划的直接调整：**

1. 不把 response-activity 写成当前系统的主提速来源，不用开关为 true 或
   候选通过率高代替 workload coverage。
2. 优先复用 **private-preview on/off** 和 materialized kernel 的历史配对
   消融，见第 4.6 节；已有净收益证据，不再把它们列为从未完成的实验。
   当前 common-end/v28.9 的机制收益归因仍不能直接继承旧版本结果。
3. 若 private-path 的贡献主要是已有思想的实现优化，继续走 TACO/TC 所需的
   是更强模型/方法学洞见和独立 DSE 效用；不能只为保留“新稀疏算法”故事而
   擅自扩建共享修复系统。
4. 修复采集、完成泛化和 fair-baseline 仍是优先补证；本节不授权新实验。
   飞书参考正文仍未取得，整体研究结论保留待补资料状态。

### 4.6 续审：已有独立消融，不能重复列为“待首次验证”

继续沿 [历史吞吐记录](throughput-bottleneck-plan-2026-08-25.md) 查到了两组
完整配对输出。本次读取全部 **100 份原始模拟器 JSON**，不是只接受汇总中的
`complete` 或 `exact_pair_count`：

- preview 开关：`tmp/preview-bypass-v28_7-c16-c32-r1-20260827/summary.json`，
  C16/C32 各十负载，每案一对，共 20 对、40 份输出，AB/BA 各十对；
- materialized kernel：`tmp/materialized-fast-kernel-c32-paired-20260826/summary.json`，
  C32 十负载，每案三对，共 30 对、60 份输出，AB/BA 各十五对。

本轮逐对检查 `scope_metrics.throughput.user_uops_per_second` 与保存的速度
一致，重算 pair speedup，再按每案中位数计算等权几何平均。配置差异分别只有
`interval_private_preview` 和 `response_materialized_uop_fast_kernel`；
`totals/cores/threads/cha/instruction_cha` 及去掉 throughput 后的 scope
metrics 均逐对相等。该检查覆盖保存的这些输出，不证明未序列化的内部状态。

归档复核指纹及数值：
`tmp/ccfa-research-20260917/archived-ablation-audit.json`。其中 95% 区间转述
原 summary 的 workload bootstrap，未在本轮重新估计。

| 历史独立实验 | 吞吐比较方向 | 等权几何平均变化 | 原 summary 的 workload-bootstrap 95% 区间 | 可得结论 |
|---|---|---:|---|---|
| v28.7 preview bypass，20 案 | **关闭** preview 相对开启 | +0.7438% | [−0.4140%, +1.8147%] | 混合、近零的总体变化，不能证明普遍应开或应关 |
| v28.6 materialized kernel，10 案×3 对 | 开启专用内核相对 generic | +1.6966% | [+1.3042%, +2.0959%] | 当时存在小幅、一致的净收益，10/10 workload 中位数为正 |

preview-off 的关键单案方向：LBM C32 **+4.3267%**，Graph500 C32
**−4.4626%**，Neutron C32 **−5.1931%**。同一开关会省去证书扫描和 barrier，
也会失去并行私有 cache replay；高覆盖率不决定哪项成本占优。旧全局关闭
候选未通过默认门禁，维护入口保留 preview；这不是“证书没有作用”，也不是
“证书整体带来显著提速”。

另外，[9 月 7 日审计](architecture-evidence-audit-20260907.md) 已有三个负载、
每模式三次交错运行的复验，`bench-summary.json` 和
`bench-target-checks.json` 仍在。它记录 preview-off 在 LBM C4、Graph500 C8、
ASTCENC C4 的中位速度变化约 +0.51%、−0.53%、+0.04%。本轮读取其汇总与目标
检查记录，未逐一重审该组全部运行，因此仅作为附加历史线索，不提升为新认证。

**口径限制：** 上述数据为 2026-08-26/27 旧版本、旧采集窗口的保存计时，
不是当前 common-end/v28.9 的新性能测量；不与第 2 节的 CPI 主表拼成一个
联合 Pareto 点。旧记录采用固定 CPU/NUMA 绑定、串行配对，但 preview 每案
仅一对，且记录有宿主负载变化；其 bootstrap 区间不估计同一 case 的运行
波动。配置键与保存输出可核验，也不代替原始命令/二进制完整 provenance。

**对发表可行性的进一步收紧：**

1. 目前没有证据可把“证书稀疏化 + 专用内核”写成一个产生数量级提升的新算法。
   专用内核已有的约 1.7% 净收益是有价值的工程优化，但独立支撑顶会创新偏弱；
   阶段内的更大百分比改善也不能代替端到端收益。
2. 应把有限研究资源优先投入**独立泛化、参数排序/设计决策和公平对比**，
   而不是为了包装新贡献重复同类小幅性能优化。若坚持算法主线，需要新的
   可迁移机制洞见及其独立证据，而不是仅增加实现复杂度。
3. 如以后重新做当前版本的消融，目的是版本迁移验证或检验明确的新机制，
   不再称“首次证明 preview/fast kernel 是否有收益”；也不按 workload 名称
   给开关挑选最有利结果。

## 5. 站在审稿人角度

### 5.1 会认可的优点

- 问题重要：详细系统仿真成本高，快速设计探索确有需求；不是为了引入 ML
  或新硬件而增加部署门槛。
- 有可运行系统、机制归因、真实规模 trace 和反例，而非只有宏观回归曲线。
- 显式 cache/coherence/DRAM/branch 状态有解释力，几何参数变化不要求训练
  新权重；可帮助定位是事件、时序还是人口/测量的问题。
- 原生 PMU 来源、共同终点、失败关闭和负实验记录是较好的科研基础设施。
- 已经知道均值掩盖长尾、warmup 影响测量、总周期相近可能是误差抵消；
  这能形成诚实且有信息量的限制章节。
- 已有动态窗口 C++/Python API，适合把性能模型接入外部 governor/RL 环境；
  虽然尚无功耗模型，但比只有整程序 CPI 的模型更接近动态控制应用。
- 指定资料的需求分析覆盖 PMU、基准、功耗/热、同步和多核资源，能帮助形成
  完整问题定义；但论文必须把其中“愿景”与“当前系统”明确分栏。

### 5.2 很可能造成拒稿的意见

| 严重度 | 审稿意见 | 需要什么回应 |
|---|---|---|
| 致命 | “与 Sniper/ZSim/1998 FastSim 相比，新算法究竟在哪？” | 用伪代码、不变量和消融说明增量；不能只回答代码量和指标更好 |
| 致命 | “被反复调过的同一批负载，为什么能证明泛化？” | 冻结模型后用真正未参与调参的 workload/input/profile；公开 calibration 账本 |
| 致命 | “喂的是目标配置 gem5 执行后的 trace，还算快速预测未知设计吗？” | 区分给定功能路径的条件预测与部署 DSE；验证采集依赖、interleaving 敏感度及完整成本 |
| 高 | “你把别人的模型改成自己的 trace/时序抽象，再声称其很差？” | upstream/default 与 matched-fork 双轨；公开补丁、微测试、支持范围、计量协议及性能影响 |
| 高 | “5% 是对 gem5，不是硬件；PMU 字段也不等价。” | 分开报告 perf→gem5、gem5→FastSim；保留事件 dictionary、scope 与不可比项 |
| 高 | “trace 没有错误路径，也不重新调度 OS，某些设计根本无法预测。” | 限定 claim，加入 branch/memory-order/lock/syscall/调度压力测试；不能靠 label 补真值 |
| 致命（若声称功耗） | “系统没有 power/voltage/thermal 模型，为什么标题写性能+能耗仿真？” | 当前稿从标题/摘要撤下功耗精度；或补真实功耗标签、模型、窗口误差传播和闭环决策 |
| 致命（若声称多线程 DSE） | “trace 固化了原机器的锁竞争与 futex 路径，变化微架构后如何自洽？” | 限定为静态功能路径条件预测，或实现显式同步语义、可运行线程调度和控制流替换 |
| 高 | “正确性只是几个汇总输出一样。” | 对优化证明状态等价；对模型用逐核、阶段与请求链反例；两套证据不能互相代替 |
| 高 | “吞吐没算 trace、预热、I/O、训练或更多 host cores。” | 固定宿主预算，分别给 replay 和 end-to-end；报告 CPU-seconds、RSS 和重用次数 |
| 高 | “Concorde/PerfVec/QFlex 已分别覆盖更快、更可迁移或有误差界的方法，你的新意在哪里？” | 与强基线按相同任务/预算比较；强调并验证多核共享状态、窗口活动和设计 regret，而非功能列表 |
| 高 | “CounterPoint 已用 PMU 约束反驳微架构假设，你的审计方法为何不是工程复现？” | 给出多核/uncore/动态窗口的新约束体系、可复现反例、自动定位质量和独立硬件闭环 |
| 中高 | “用户凭什么用这个工具选设计，而不是用 sampled gem5？” | 以相同墙钟/算力预算比较实际设计选择和 regret |
| 中高 | “构建/采集补丁、输入及授权数据不可复现。” | 可公开小型 corpus、自制压力测试、自动重现脚本及合法数据获取说明 |

**模拟评审结论：** 如果今天仅以“一个更快、更准的多核性能+功耗模拟器”提交，
我会给 **Reject**：功耗与通用同步调度尚未实现，且强基线和独立泛化未覆盖。
若收窄为“给定功能路径的可审计多核性能/DSE”，当前更接近 **Weak Reject**：
系统和证据基础有价值，但新算法与设计效用仍不足。补齐参数/工作负载留出、
sampled detailed/当代 ML 公平比较和 DSE regret 后，TACO/TC 有合理目标空间；
HPCA/MICRO 需要更突出的机制收益或洞见；不保证任何 venue 录用。

## 6. 论文应该报告哪些指标

### 6.1 必选指标与口径

| 维度 | 必选指标 | 易踩的坑 |
|---|---|---|
| CPI | 逐案 gem5/FS CPI、signed/absolute relative error、绝对误差；聚合 MAE、MAPE、WAPE、bias、P50/P90/P99/max | macro 与 UOP 不混；active 与含 idle 不混；人口差异单列；每张聚合表保留 CPI MAE |
| PMU | 有效事件字典、raw count、MPKI 或明确定义的 rate、APE 分布、WAPE、bias、零参考处理与 coverage | hit rate 分母、upgrade/merge/remote supply 不混；缺少 native source 就不评分 |
| 功耗活动覆盖 | 功耗模型实际消费的每个特征：来源、单位、窗口、有效 case、真值映射、缺失策略 | 不把 `totals` 诊断字段自动升级为 formal PMU；ITLB/NoC/同步等待缺失时明确 unavailable |
| 参数趋势 | 相对 baseline 的 CPI 比值/增量误差；有可辨识参考变化的方向一致率 | 预先定义小变化/tie 区间，不按结果删点；增量同时报告绝对 CPI 单位 |
| 配置选择 | Kendall/Spearman、pairwise 排序、top-k 命中、regret；涉及面积/能耗时报告 Pareto 选择质量 | 相关性高仍可能选错最优点；缺少 area/power truth 时不能声称 PPA 优化 |
| 性能成本 | replay measured user-UOP/s；端到端墙钟；host CPU-seconds；峰值 RSS；bytes/UOP；并发扩展 | 目标核数与 host worker 数分开；独立 case 并发不是单次模拟的加速 |
| 机制 | 证书 eligible/通过/回退比例、被省计算量、开销分解、最坏失败成本、完整目标状态差分 | 通过率高不等于覆盖率高；减少事件数不等于端到端变快 |
| 泛化 | workload/input/core-count/profile/producer 的独立划分，分别报告 | 40 案来自 10 负载，不是 40 个独立工作负载 |
| 系统边界 | 多阶段/多 ROI、不同 kernel 占比、同步强度、错路径敏感度和 warmup 敏感度 | 短 common-end 窗口不直接代表全程序完成时间或所有服务 tail latency |
| 动态控制 | 窗口 CPI/PMU、功率/温度、频率 action、transition 开销、SLA/EDP regret | 异频 sum cycles 不是 makespan；频率 API 存在不等于 DVFS 模型准确 |
| 同步语义 | lock/barrier/futex 人口、赢家/等待边、blocked time、控制流替换、尾延迟 | 原 trace 的 CAS 成败和 spin 次数不是新微架构下的 ground truth |

推荐以 workload 为 cluster 做置信区间/重采样，避免把同负载多核数当独立样本
夸大统计量。P99 在几十案上的意义有限，应同时给最坏点和原始散点。
测速度采用 quiet-host、固定 affinity/频率/编译参数，至少多次重复并报告中位数
与离散程度；不要把历史串行一次运行当稳定速度证明。

对“越小越好”的设计目标，regret 可定义为：

`J_gem5(design_selected_by_FastSim) / min_design J_gem5(design) - 1`。

这里 `J` 必须事先声明是 macro CPI、完成时间、能量还是 EDP。共同终点 CPI
不是自动等价于完整任务延迟；研究 DSE 时还要保证不同设计的程序阶段可比。

### 6.2 端到端优势必须算清

对一份合法可重用的功能输入，`K` 次探索：

`T_FastSim = T_collect + T_lower + T_warmup_and_IO + sum(T_replay_i)`。

与同样覆盖、精度目标及宿主预算的详细/采样基线比较。若可近似每案成本恒定，
只有在 `T_reference > T_replay` 时，才有
`K > T_preprocess / (T_reference - T_replay)` 的摊销交叉点。
必须说明预热/I/O是否已包含在每次 replay，避免双计数。

正式**评估真值**仍须每个微架构重新运行 gem5 并通过身份门，不能复用基准
trace 冒充变体真值。但这不意味着**部署推理输入**可以免费依赖每个变体的
详细 gem5 执行：

- 目标配置自己产生的 gem5-FST，主要测“给定目标功能路径后的时序近似”。
- 独立 functional producer 的 trace 才能回答实际使用成本和迁移性。
- 多核交织、锁、OS/中断路径会随时序改变。若使用固定功能路径，应明确只支持
  哪些不依赖该变化的设计问题，并用每变体新 gem5 truth 检查偏差；不得将此
  限制实验混入正式同源 common-end 评分。

如果每个待探索设计都先付完整 gem5 仿真成本，FastSim 仍可能有调试/后处理
价值，但“替代 gem5 加速 DSE”的主张就尚未成立。

### 6.3 功耗与能量：可作为应用，但不要提前写成结果

平均功率、总能量和 PMU 数量分别验证。恒定频率、相同指令人口下：
`T = N_macro × CPI / f`，`E = P_avg × T`。
绝对 CPI 误差对应绝对运行时间误差；相对 CPI 误差影响相对时间预算。
事件率同时受事件数和时间误差影响，所以 CPI 准不保证功率准，功率误差与
时间误差也可能抵消造成能量看似准确。

应至少报告功率误差（W/相对）、能量误差（J/相对）、EDP/配置 regret；
DVFS 需按窗口积分而非单一频率公式。建议用同一个冻结功耗模型分别输入：
真实/参考活动、FastSim 活动、只替换时序、只替换事件、两者都替换，隔离误差
来源。McPAT 输出是另一模型预测，不等于真实测量；“接上模型”不构成功耗精度认证。

指定资料列出的目标活动包括 idle、branch penalty、ITLB/DTLB、cache、
upgrade/invalidate/writeback、directory、NoC、DRAM 和同步等待。当前维护输出中，
branch、L1D/private-L2/LLC tag miss 有受控评分，DTLB/DRAM 多为模型输出或诊断，
ITLB/NoC traffic/directory entries/同步等待缺少同等级 formal truth。功耗模型应
冻结一个**最小可验证特征集**起步，不得用更多未验证字段换取表面拟合精度。

## 7. 最小补证路线与停止条件

以下是建议，不授权启动昂贵实验、不承诺工期、不改变当前默认。
目标是先决定论文故事是否成立，再决定是否投入更大模型修改。

| 优先级 | 具体工作与产物 | 通过条件 / 停止条件 |
|---|---|---|
| P0：恢复证据链 | 修复已定位的空核共同关闭、Ruby `IM` functional-read 和 unknown-kernel 归属问题；新 binary/source 身份与新输出目录 | 不能伪造周期、关掉 native gate 或把 10K smoke 当 10M 正式集；全部 smoke 通过再正式采集 |
| P0：冻结主张 | 一页 claim-to-evidence；列清 implemented/proposed、校准历史、支持的 OS/trace 范围；选择新论文名 | 若与 bound-weave/IW/memoization 的差异只有配置/功能表，先收窄方向 |
| P1：完成已有 136 案计划 | 17 profiles × C4/C8 × 4 workloads；每变体独立 truth、FST、配置；维护入口评分 | 完整案例集合、身份/native/共同终点/人口门；不是只看 `complete` 文件；该集合本身不自动成为 workload-held-out |
| P1：增加真正留出 | 冻结模型后选未用于诊断的 workload/input；至少覆盖计算、分支、流式、随机、同步、kernel-heavy 类别；数量按目标覆盖制定 | 不以 FastSim 表现挑选；失败和不支持项保留；仅已有四负载不足证明工作负载泛化 |
| P1：贡献消融 | full/reference、单独 private certificate、单独 response certificate、组合；再单独考察 load-ready/store-wait，不捆绑 | 无损项要求目标状态等价；近似项给完整 10-workload 矩阵、CPI MAE/MAPE、LBM C1/C13 控制与速度 |
| P1：公平竞争 | gem5 O3、sampled gem5、Sniper interval/IW、ZSim；按 claim 加 Tao/学习型和 ChampSim 子域 | 分开公共支持 scope 与 FS 原生优势；公开 fork 修改；同宿主预算，宏/UOP与warmup对齐 |
| P1：更新强基线 | 增加 BZSim/当代 NoC 基线；性能/DSE 加 Concorde、PerfVec；端到端时间加 QFlex/LoopPoint sampled detailed | 不要求每个基线承担不支持的任务；对共同子域使用同一输入、目标和预算，分别列 unavailable |
| P2：部署闭环 | 新 QEMU-FST 输入、lowering 差分、物理地址 strictness、独立目标真值；采集到结果完整成本 | 不凭同为 FST 就继承精度；不依赖目标 timing/cache label；成本无优势则撤回 DSE 提速主张 |
| P2：设计决策 | 对盲测配置报告方向/幅度/排序/regret，给具体设计决策案例 | “CPI 平均接近但常选错设计”则不能走 DSE 主线；没 reference 激励的参数标覆盖不足 |
| P2：同步语义边界 | 先做 pthread mutex/barrier/futex 的两线程可重排微实验，明确替换哪些低层 trace 路径 | 若不能在新 timing 下改变锁赢家并保持功能/尾延迟自洽，就从论文范围排除 timing-sensitive synchronization |
| P2：功耗最小闭环 | 冻结少量有真值的 PMU 特征，外部功率计/RAPL 域标签；真实→功耗、模拟→功耗、校准层、时间误差四路消融 | 无窗口级功率/能量 ground truth 或 covariate-shift 失控时，不声称功耗精度 |
| P2：PMU 约束方法 | 以 CounterPoint 为强前作，定义多核/uncore/动态窗口约束和可定位反例 | 若只是手工 event dictionary + 阈值检查，不列为独立研究贡献 |
| P2：独立硬件或应用 | 至少对可映射的真实机器/设计做独立验证；若走能耗路线再完成功率闭环 | 不能把 gem5 当 hardware truth；没有功率真值就从摘要去掉 power accuracy |
| P3：复现包 | 模型/数据 manifest、源码补丁、合法小 corpus、反例、绘图表格、命令与失败策略 | 检查公司披露、SPEC/trace/镜像许可；受限数据给合法重建路径，不自动公开上传 |

不要为凑漂亮数字给所有项目设同一个百分比门槛。项目既有 guardrail 是底线；
论文的主标准是**相对强基线、相同预算下的显著收益及清楚的适用边界**。
工程 E1 有界化可以继续作为独立低风险优化，但不能代替以上创新/泛化工作。

建议主文组织六组图表：方法与适用边界；全矩阵 CPI/PMU 分布；精度—成本散点；
机制消融/证书覆盖；新参数和留出 DSE regret；独立 producer/系统应用与失败案例。
附录保留全部逐案绝对误差、事件定义、fork 差异和采集 provenance。

## 8. CCF-A 会刊选择与时间现实

分类依据是 **CCF 2026 第七版正式目录** [V1]，2026-03-31 发布，页面说明
4 月 9 日有勘误。已下载官方 PDF；第 2 页列 A 类期刊，第 5 页列本领域 A 类
会议。目录将正式 Full/Regular paper 纳入考虑，不把 workshop、demo、
short paper 等自动视为 A 类论文。具体认定仍以所在单位适用版本和规则为准。

### 8.1 按贡献匹配，而非按名气排序

| Venue | CCF 2026 | 对 FastSim 的适配 | 推荐定位 |
|---|---|---|---|
| **ACM TACO** | A，期刊 | 高：架构模型、性能分析、软硬件优化；能容纳边界、负结果和长评估 | **当前首选期刊**：路线 A 完整机制+DSE+独立评估；不能只写工具使用说明 |
| **IEEE TC** | A，期刊 | 高：计算机组织/体系结构、设计方法、性能及实验评估 | 第二期刊方向；适合强调模型方法、可复用性与硬件验证 |
| **HPCA** | A，会议 | 高：新仿真机制能服务架构设计，近期有 Nugget 等方法工作 | 主攻会议之一；需要清晰算法创新和架构洞见 |
| **MICRO** | A，会议 | 高：指令窗口/资源/响应建模与微架构机制 | 若重点是核心—存储时序机制和可信消融，可优先于广系统叙事 |
| **ISCA** | A，会议 | 方向相关，但当前突破性证据不足 | 完成强机制/广泛效用后冲刺，不建议直接以当前工程汇总投稿 |
| **DAC / IEEE TCAD** | 均 A | 当前性能稿中等；完成功耗/DVFS/DSE 后高 | 路线 B 的首选：以设计选择、PPA/EDP regret 和动态控制为中心，不是给 CPI 工具改名 |
| **ASPLOS** | A，会议 | 当前低；若同步语义/PMU约束形成系统交叉贡献则提升 | CounterPoint 已把 PMU 约束门槛抬高；路线 C 或强功耗控制系统成熟后再考虑 |
| **SC** | A，会议 | 当前中低：没有大规模 HPC 方法及效用闭环 | HPC 设计/应用和规模化仿真确为主贡献时再选 |
| **IEEE TPDS / HPDC** | 均 A | 当前低：需要新的并行/分布式方法，不是 jobs 并发 | 只有单次模拟或分布式执行算法取得强扩展证据才转向 |
| **ACM TOCS / EuroSys / ACM SIGOPS ATC** | 均 A | 当前低：需要更强系统贡献、真实部署和广泛工作负载 | 不作为规避架构创新门槛的替代 |
| **ACM TOS / FAST** | 均 A | 低：当前不是存储系统研究 | 不推荐 |
| **PPoPP** | A | 低：缺少并行编程/运行时核心创新 | 不推荐仅因实现多线程而投 |

范围参考 [V2–V6]；适配高低是本报告判断，不是官方录用偏好或概率。
TACO/TCAD/TC 都不等于“更容易中”；TACO 应核查当前 OA/APC 政策，不能假定
投稿和发表成本。与会议互转时遵守不得一稿多投及期刊扩展披露要求。

### 8.2 截至调研日的可用窗口

| 会议/期刊 | 已核实信息 | 对现在的行动含义 |
|---|---|---|
| HPCA 2027 | 正文截止 2026-07-31；摘要 07-24 [V2] | 已过，不再建议赶这一轮 |
| MICRO 2026 | 正文截止 2026-04-07 23:59 EDT [V3] | 已过；未核实 MICRO 2027 CFP，不编造日期 |
| ASPLOS 2027 | April cycle 04-15；September cycle 09-09，AoE [V4] | 两轮均过；官方还有首两页 rapid review，更要求清楚贡献 |
| ISCA 2026 | 正文截止 2025-11-17 [V5] | 已过；本轮未获得 ISCA 2027 正式 CFP，不拿往年日期当新截止 |
| DAC 2027 | 本轮未核实官方最终截止 | 不用第三方 tentative 日期做排期承诺 |
| TACO / TC / TCAD | 常规期刊投稿路线，不依赖上述年度会议窗口 | 先完成证据门，提交前复核 author instructions、费用及当期政策 |

实际建议：**先按 TACO 路线 A 准备可审查的长稿证据包，同时关注下一轮
HPCA/MICRO 正式 CFP；不要为追一个日期把尚未实现的功耗/同步愿景并入主稿。**
路线 B 先作为后续 DAC/TCAD 项目，路线 C 只有在调度/控制流语义落地后再独立判断。
不为追一个日历日期跳过公平比较、held-out 或采集完整性。

若最终贡献更像专门的仿真/评估工具，ISPASS/IISWC 等可以获取有价值的专业反馈；
它们不是本报告对 CCF-A 目标的替代承诺。也不要把 Tao 的发表路线简单当成 A 类：
本次官方目录的 SIGMETRICS 在该领域为 B 类，POMACS/会议对应及单位认定应独立核对。

## 9. 来源目录

### 9.1 相关工作

- **[R1]** Schnarr, Larus. *Fast Out-of-Order Processor Simulation Using Memoization*.
  ASPLOS 1998. DOI: <https://doi.org/10.1145/291006.291063>。
  [作者全文](https://ftp.cs.wisc.edu/wwt/asplos98_fastsim.pdf)；
  [原系统页面](https://pages.cs.wisc.edu/~wwt/fastsim/)。本轮读取全文首页。
- **[R2]** Carlson, Heirman, Eeckhout. *Sniper: Exploring the Level of Abstraction
  for Scalable and Accurate Parallel Multi-Core Simulation*. SC 2011.
  <https://doi.org/10.1145/2063384.2063454>；
  [作者全文](https://www.comp.nus.edu.sg/~tcarlson/pdfs/carlson2011etloafsaapms.pdf)。
  本轮读取摘要、引言。
- **[R3]** Carlson et al. *An Evaluation of High-Level Mechanistic Core Models*.
  TACO 11(3), Article 28, 2014. <https://doi.org/10.1145/2629677>。
  本轮核验出版元数据、作者页面和 IW-centric 摘要说明。
- **[R4]** Sanchez, Kozyrakis. *ZSim: Fast and Accurate Microarchitectural
  Simulation of Thousand-Core Systems*. ISCA 2013.
  <https://doi.org/10.1145/2485922.2485963>；
  [作者全文](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf)。
  本轮读取摘要、干涉分类、限制与硬件验证段。
- **[R5]** Miller et al. *Graphite: A Distributed Parallel Simulator for Multicores*.
  HPCA 2010.
  [作者稿](http://groups.csail.mit.edu/carbon/docs/graphite_hpca2010_preprint.pdf)。
  本轮核对公开摘要与规模化机制说明。
- **[R6]** Binkert et al. *The gem5 Simulator*. 2011；Lowe-Power et al.
  *The gem5 Simulator: Version 20.0+*. 2020.
  [官方出版列表](https://www.gem5.org/publications/)；
  <https://arxiv.org/abs/2007.03152>。
- **[R7]** Feliu et al. *Rebasing Microarchitectural Research with Industry Traces*.
  IISWC 2023. <https://doi.org/10.1109/IISWC59245.2023.00027>。
  [出版页面](https://www.computer.org/csdl/proceedings-article/iiswc/2023/031700a100/1RyGCYs211e)；
  [作者讲稿](https://personales.upv.es/jofepre/docs/IISWC_2023_slides.pdf)。
- **[R8]** Sherwood et al. *Automatically Characterizing Large Scale Program
  Behavior*. ASPLOS 2002；Perelman et al. *Using SimPoint for Accurate and
  Efficient Simulation*. 2003, <https://doi.org/10.1145/885651.781076>；
  Wunderlich et al. *SMARTS: Accelerating Microarchitecture Simulation via
  Rigorous Statistical Sampling*. ISCA 2003,
  <https://doi.org/10.1145/859618.859629>。
- **[R9]** *LiveSim: Going Live with Microarchitecture Simulation*. HPCA 2016.
  [作者全文与摘要](https://masc.soe.ucsc.edu/docs/hpca16.pdf)。
- **[R10]** Li et al. *SimNet: Accurate and High-Performance Computer Architecture
  Simulation using Deep Learning*. POMACS 6(2), 2022.
  <https://doi.org/10.1145/3530891>；
  [全文](https://ar5iv.labs.arxiv.org/html/2105.05821)。本轮读取摘要与引言。
- **[R11]** Pandey et al. *Scalable Deep Learning-Based Microarchitecture
  Simulation on GPUs*. SC 2022.
  <https://doi.org/10.1109/SC41404.2022.00084>；
  [作者稿](https://asherliu.github.io/docs/sc22.pdf)。
- **[R12]** Pandey, Yazdanbakhsh, Liu. *Tao: Re-Thinking DL-based Microarchitecture
  Simulation*. POMACS 8(2), 2024.
  <https://doi.org/10.1145/3656012>；
  [全文](https://arxiv.org/html/2404.10921v1)。本轮读取摘要、贡献与引言。
- **[R13]** Karandikar et al. *FireSim: FPGA-Accelerated, Cycle-Accurate Scale-Out
  System Simulation in the Public Cloud*. ISCA 2018.
  [官方论文目录](https://fires.im/publications/)；
  [官方系统说明](https://fires.im/2018/05/21/firesim-now-open-sourced.html)。
- **[R14]** Li et al. *McPAT: An Integrated Power, Area, and Timing Modeling
  Framework for Multicore and Manycore Architectures*. MICRO 2009；
  *CoMeT: An Integrated Interval Thermal Simulation Toolchain for 2D, 2.5D,
  and 3D Processor-Memory Systems*，
  [全文](https://arxiv.org/html/2109.12405v3)。本轮用于确认已有活动→功耗/热工具链，
  不引其外部误差数字。
- **[R15]** Qiu, Samani, Lowe-Power. *Nugget: Portable Program Snippets*.
  HPCA 2026.
  [官方会议论文页](https://2026.hpca-conf.org/details/hpca-2026-main-conference/111/Nugget-Portable-Program-Snippets)；
  [全文](https://arxiv.org/html/2509.02873)。
- **[R16]** *CAPSim: A Fast CPU Performance Simulator Using Attention-based
  Predictor*, <https://arxiv.org/html/2510.10484>；
  *NeuroScalar: A Deep Learning Framework for Fast, Accurate, and In-the-Wild
  Cycle-Level Performance Prediction*, <https://arxiv.org/html/2509.22410v1>。
  本轮只确认预印本，不确认正式会刊归属。
- **[R17]** Strikos, Ejaz, Sourdis. *BZSim: Fast, Large-Scale
  Microarchitectural Simulation with Detailed Interconnect Modeling*.
  ISPASS 2024.
  [官方 programme](https://ispass.org/ispass2024/program.php)。
- **[R18]** Nasr-Esfahany et al. *Concorde: Fast and Accurate CPU Performance
  Modeling with Compositional Analytical-ML Fusion*. ISCA 2025.
  <https://doi.org/10.1145/3695053.3731037>。
- **[R19]** Li, Flynn, Hoisie. *Learning Generalizable Program and Architecture
  Representations for Performance Modeling* (PerfVec). SC 2024.
  <https://doi.org/10.1109/SC41406.2024.00072>。
- **[R20]** QFlex 3.0.
  [项目页](https://qflex.epfl.ch/)；
  [SIGARCH 文章](https://www.sigarch.org/the-return-of-rigorous-full-system-timing-simulation/)；
  [ISPASS 2026 keynote](https://ispass.org/ispass2026/keynotes.php)。
  本轮将其作为当前 sampling/error-bound 方法基线，不把 keynote 当正式论文。
- **[R21]** Lindsay et al. *CounterPoint: Using Hardware Event Counters to
  Refute and Refine Microarchitectural Assumptions*. ASPLOS 2026.
  <https://doi.org/10.1145/3779212.3790145>；
  [官方奖项页](https://www.asplos-conference.org/asplos2026/awards/index.html)。
  本轮读取指定资料中的 extended-version 解读，并以 ACM/官方奖项页核验。
- **[R22]** Sabu et al. *LoopPoint: Checkpoint-driven Sampled Simulation for
  Multi-threaded Applications*. HPCA 2022.
  <https://doi.org/10.1109/HPCA53966.2022.00051>。
- **[R23]** Sabu, Liu, Carlson. *Viper: Utilizing Hierarchical Program Structure
  to Accelerate Multi-Core Simulation*. IEEE Access 12, 2024.
  [作者全文](https://alenks.github.io/pdf/viper_access2024.pdf)。
- **[R24]** Sabu et al. *Accelerating the Simulation of Parallel Workloads
  using Loop-Bounded Checkpoints*. ACM TACO, 2026.
  <https://doi.org/10.1145/3799430>。
- **[R25]** Whangbo et al. *FireAxe: Partitioned FPGA-Accelerated Simulation
  of Large-Scale RTL Designs*. ISCA 2024.
  <https://doi.org/10.1109/ISCA59077.2024.00044>。
- **[R26]** Weng et al. *Assassyn: A Unified Abstraction for Architectural
  Simulation and Implementation*. ISCA 2025.
  <https://doi.org/10.1145/3695053.3731004>。
- **[R27]** Zhang et al. *TraceRTL: Agile Performance Evaluation for
  Microarchitecture Exploration*. HPCA 2026.
  [官方会议论文页](https://2026.hpca-conf.org/details/hpca-2026-main-conference/27/TraceRTL-Agile-Performance-Evaluation-for-Microarchitecture-Exploration)。
- **[R28]** Kanellopoulos et al. *Virtuoso: Enabling Fast and Accurate Virtual
  Memory Research via an Imitation-based Operating System Simulation
  Methodology*. ASPLOS 2025.
  [项目页](https://cmu-safari.github.io/Virtuoso/)。

### 9.2 分类与投稿要求

- **[V1] CCF 官方正式发布与目录**：
  <https://www.ccf.org.cn/Academic_Evaluation/By_category/>；
  <https://www.ccf.org.cn/Academic_Evaluation/ARCH_DCP_SS/>；
  [第七版正式 PDF](https://www.ccf.org.cn/ccf/contentcore/resource/download?ID=112CF3BF7E1140ACEB271ADAED12A67ADFABB8FF099E40C2759502A85C8A281F)。
  本地原件：`tmp/ccfa-research-20260917/ccf-7-official.pdf`。
- **[V2] HPCA 2027**：
  [SIGARCH CFP](https://www.sigarch.org/call-contributions/hpca-2027/)；
  [会议官方日期表](https://conf.researchr.org/dates/hpca-2027)。
- **[V3] MICRO 2026**：
  <https://www.microarch.org/micro59/submit/papers.php>。
- **[V4] ASPLOS 2027**：
  <https://www.asplos-conference.org/asplos2027/cfp/>。本轮读取完整 scope、
  日期及 rapid-review 说明。
- **[V5] ISCA 2026**：
  <https://www.iscaconf.org/isca2026/submit/callforpapers.php>；
  <https://www.iscaconf.org/isca2026/submit/guidelines.html>。
- **[V6] 期刊范围**：
  [SIGARCH 对 TACO 的介绍](https://www.sigarch.org/acm-taco-a-high-quality-venue-targeting-computer-architecture-and-compiler-research/)；
  [IEEE TC 范围](https://xplorestaging.ieee.org/xpl/aboutJournal.jsp?punumber=12)；
  [IEEE TCAD 说明](https://technav.ieee.org/topic/ieee-transactions-on-computer-aided-design-of-integrated-circuits-and-systems/)。

### 9.3 用户指定资料及提取证据

- 飞书文件 `APiDb2gmjoMqrAxnEF5cTgBqnSg`：
  `仿真器调研报告v2.md`，下载 SHA256
  `31ea20fb4f766be65cacc2458ee92c498fbdc48cc3309c03144f9aad4d63ad80`。
- 飞书 Wiki `XFOnwGbVCiwNUkkdScwcyVMRnlf`：
  Docx `X63MdwLt0ochNGxp9wpcQQpMnKd` revision 6828；整篇读取快照：
  `tmp/ccfa-research-20260917/energy-co-design-wiki.json`。
- 嵌入《仿真器技术路线》：
  Docx `VV5IdTaiSogXpyx0Z3act2c9nff` revision 1699；
  `tmp/ccfa-research-20260917/simulator-technical-route.json`。
- 嵌入同步语义说明：
  Docx `JNeudx9nSol6HqxxcTGcbduznUb` revision 20；
  `tmp/ccfa-research-20260917/drmemtrace-sync-semantics.json`。
- 嵌入 CounterPoint 解读：
  Docx `JSPfdaiToo4M7UxkknNcAAuYnSe` revision 6；贡献、Agent 校准和局限
  分别保存在 `counterpoint-*.json`。
- 嵌入表格：功耗 PMU 目标 revision 9、Intel/RISC-V 通用事件 revision 38；
  本地为 `power-pmu-table.json`、`intel-common-pmu-table.json` 和
  `riscv-pmu-table.json`。

这些 `tmp/` 文件是本次只读调研证据，不是代码交付或公开 artifact；公司内部
资料的分发、论文引用和披露须另行审批。报告引用其观点时优先转引正式公开论文。

## 10. 完成审计与后续交接

### 10.1 用户目标转化为交付项

目标是：基于用户两份参考资料和 FastSim 当前实现/证据，调研相关工作与 CCF-A
会刊，判断创新、贡献、指标及相对优势，以审稿人视角说明优缺点和发表可行性，
并给出具体投稿与补证建议。**并不要求本轮把所有缺失实验都实现或跑完。**

| 用户要求 | 本文/实际证据 | 本轮状态 |
|---|---|---|
| 调研相关工作 | 第 3、9.1 节；原论文、作者页面、Crossref；含直接前作和 2026 方法工作 | 已完成针对性公开调研；未宣称系统性穷尽 |
| 调研会议/期刊与 CCF-A | 第 8、9.2 节；官方第七版 PDF 第 2、5 页及 CFP | 已核验；未公开的新截止不猜测 |
| 创新点和贡献 | 第 4 节；现有证书条件、当前配置与代码入口、原设计未完成项 | 已分清能力、候选创新、未来目标 |
| 指标及相对优势 | 第 2、6 节；原始已评分逐案汇总、120 行三方 CSV；完整 CPI 绝对误差/MAE | 已核验归档数字并限定结论；未声称新性能测试 |
| 审稿人视角优缺点 | 第 5 节，按严重度列拒稿风险与补证 | 已给出 |
| 能否支撑 CCF-A | 第 1、5、7 节，明确当前不充分及条件路线 | 已给出条件判断，不以测试通过代替创新证明 |
| 建议具体会刊 | TACO/TC 优先；HPCA/MICRO 机制路线；DAC/TCAD 条件路线 | 已给出，并说明已过截止 |
| 参考飞书文件 | 第 2.5、3、4.3、9.3 节；原始 Markdown 与 SHA256 | 已下载并综合；过时 FastSim 数字和 venue 已校正 |
| 参考飞书 Wiki | 第 2.5、4.3、6.3、7、9.3 节；Docx revision 6828，嵌入技术路线/表格/引用下钻 | 已读取并综合；未来功耗/调度设计未冒充实现 |
| 最终完成审计 | 本表、2.1 证据矩阵和 10.2 覆盖说明 | 用户要求的调研交付均有实际证据；缺失实验列为论文后续门禁而非本调研缺件 |

### 10.2 为什么不能用绿色状态宣称完成

- 本轮身份门确实为 40/40，但只覆盖目标配置身份。
- 原生验证器 40/40 只覆盖已有来源/人口/守恒等门；case JSON 明确
  `binary_config_provenance=not-certified-by-rescoring`。它不证明硬件精度、
  workload-held-out、方法创新或论文可录用。
- 136 案虽然有正式阶段 `collection-finished.json` 文件，其内容是
  **`state=incomplete`**；目录/文件存在不表示正式数据完成。
- 文档检查和算术核对不替代模型实验；本轮只新增/修订调研文档及项目 `tmp/`
  下的只读资料/身份验证产物，不修改默认、不提交、不推送。
- 已有私有 `.git` 临时元数据和工作区改动保持原样；本轮未做仓库恢复或覆盖。

本轮轻量验证：本报告本地 Markdown 链接均可解析；代码围栏和十节交付结构
检查通过；CPI 聚合及四个尾部例子的数值与维护验证器保存的字段一致；身份结果
与被评分的 40 案集合严格相等；smoke 的 34/102 和正式阶段 incomplete 已从实际
JSON 再核对；`git diff --no-index --check /dev/null` 检查本报告无空白错误。
引用审计用 Crossref 校对核心 DOI，纠正了 IISWC 行业 trace 论文的 DOI 尾号。
本轮只改文档，不以历史构建/测试结果宣称当前代码通过，也未额外启动构建。

续审增加第 4.5 节：核对配置 include 与活跃调用，汇总相同 40 案实现计数并
逐案检查三个分区恒等式；据极低 response-activity 覆盖收紧了标题/机制主张。
该临时盘点工具不是性能评分器，不替代 source/identity/native gates，不会把
实现计数或测试函数存在当作模型正确性证明。

第 4.6 节继续核验了两组历史配对消融的 100 份原始输出、预期配置差异、
保存的目标输出段和吞吐比值，并保留逐文件 SHA256。结果纠正了上一轮
“独立净收益消融尚未做”的过强表述；当前版本的收益仍有证据边界。

两份飞书资料已在 user OAuth 只读授权后获取；主文件、Wiki revision、嵌入
Docx 和表格均留有本地快照/指纹。对其新增主张又用正式页面核验 Concorde、
BZSim、PerfVec、QFlex、CounterPoint、TAO/SimNet venue；无法确认的
NeuroScalar venue 保持预印本标签。资料中的 FastSim C64、PMU WAPE <0.3%、
51.5M UOP/s 等历史混合口径未沿用为当前正式主张。

**完成边界：** 本报告已完成用户要求的调研、创新/贡献/指标/相对优势、模拟审稿
和会刊建议。它的结论是“当前 FastSim 尚不足以直接支撑一篇完成度高的 CCF-A
论文”，因此第 7 节列出的实验与实现缺口不属于本次调研交付缺失，也不能因报告
完成而标记为模型验收通过。实际投稿前仍应刷新 venue 政策、公开披露许可和所有
实验结果。
