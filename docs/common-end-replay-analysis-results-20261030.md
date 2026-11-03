# 新共同终点来源的 FastSim 重放与机制分析（2026-10-30）

## 结论

1. 两份新FST的P1/P2共四次重放已完成，直接参数身份每次130项全部匹配；维护版native validator每个arm各2/2通过，failed/pending=0。这里的P1/P2是hierarchy_walk关闭/开启两个模型arm，不是实施阶段编号。
2. Marian仍偏慢，G500仍偏快。新来源上P2的两案CPI MAE高于P1；本轮不推广P2、不调整延迟常数。旧来源没有被替换，新旧CPI变化不能记为模型修复收益。
3. P2−P1不能当作纯PTE开销：Marian只增加13,290 active cycles，G500反而减少3,123,760。G500固定CPU范围复跑后，目标状态和逐核周期完全复现；变化可复现，但包含排队、并行、求解路径与makespan变化，不是隔离的PTE直接成本。
4. 新同窗物理统计显示，Marian模型控制器排队偏高，G500偏低；G500的FB分类人口差依旧很大。因此不能只用FB缺失解释G500，也不能由平均时间轴位移推断两案排队必然都偏高。
5. 本轮完成同源重放和聚合诊断，没有完成逐请求到达/回填/退休关键路径配对，尚未认证单一主导根因。

## 输入、配置与验收

新来源仅来自 `tmp/common-end-stats-20261030/dynamic-retry1/source/` 的 Marian C4和G500 C16，各自FST、thread-state.plan、oracle、stats-common-end.txt成套使用。没有使用旧stats或旧oracle给新预测评分。
使用此前已测试、修复Sequencer准入的冻结 `fastsim-diagnostics`，SHA256为 `97a98b253d0c8b67520bd79f5d3cffbcfb59dc5010f1c2ea6f418c96a413f525`。二进制和源码指纹与前轮一致。本轮没有修改/重编模型。
配置递归展开后冻结，去掉继承的旧thread-state.plan，绑定新plan。每案P1/P2有效配置差异仅 `dtlb.hierarchy_walk=false/true`。共同设置包含bounded_analytic、typed store callback、store_wait_relief=false、scalar timing kernel、既有FR-FCFS配置，不改变默认模型。
评分通过维护入口 `tools/run_native_kernel_fastsim_validation.py --source-root ... --stats-dir ... --retired-cache`，每个arm都发现且评分两案；cache真值来自native Ruby/SLICC。统计/参数身份通过不表示精度阈值、严格硬件PMU或吞吐通过。
首次重放的C16宿主CPU亲和性在运行中从单CPU调整到之前实验使用的16 CPU范围；另外固定16 CPU范围重复P1/P2，所有目标字段相同，仅宿主wall timers和producer队列frontier_waits不同。原过严比较的失败记录保留，修正的目标比较见 `repeat-target-comparison.json`，排除frontier_waits依据 `src/simulator.cpp:acquire_chunk()`，没有改预测。并发运行和亲和性调整使本轮宿主wall/throughput不适合用于性能准入。

## 同源CPI结果

单位为workload-task active cycles/宏指令。gem5/FastSim各自计周期，逐核宏/UOP人口及任务集合通过维护门禁。CPI来自维护版validation.json，本分析不重新选择参考。

| case | arm | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差（cycles/宏指令） |
|---|---|---:|---:|---:|---:|
| Marian C4 | P1 | 4.937551 | 6.015521 | +21.83% | 1.077971 |
| Marian C4 | P2 | 4.937551 | 6.016929 | +21.86% | 1.079378 |
| G500 C16 | P1 | 2.303451 | 2.153629 | -6.50% | 0.149822 |
| G500 C16 | P2 | 2.303451 | 2.114538 | -8.20% | 0.188913 |

| N=2、case-equal | CPI MAE | MAPE | WAPE | signed bias | P50 / P90 / P99 / max APE |
|---|---:|---:|---:|---:|---|
| P1 | 0.613896 | 14.17% | 9.60% | -0.78% | 14.17% / 20.30% / 21.68% / 21.83% |
| P2 | 0.634146 | 15.03% | 10.96% | -2.13% | 15.03% / 20.49% / 21.72% / 21.86% |

MAE/MAPE为case-equal；WAPE/bias按通过人口核验的task宏指令数加权。N=2的Type-7百分位只是两点插值，不是六项矩阵或P99泛化验收。相反方向会使signed bias发生抵消，不能用小bias代替低误差。

## PTE A/B与makespan分解

本轮两案均逐位满足 `active_cycles = K × makespan − inactive_cycles`。下表只比较同一新FST内P2−P1，不用不同来源估计同输入的统计噪声带。

| case | Δmakespan | Δactive cycles | Δ各核报告周期之和 | Δ官方active CPI | Seq请求增量 / PTE读 |
|---|---:|---:|---:|---:|---:|
| marian | +3,316 | +13,290 | -93,427 | +0.001407 | 1,870 / 1,870 |
| g500 | -195,235 | -3,123,760 | -423,236 | -0.039091 | 220,887 / 220,887 |

Marian满足 `4×3316−(−26)=13290`，但各核报告周期和减少93,427；G500满足 `16×(−195235)=−3123760`，而核和仅减少423,236。G500最慢核仍是core15，改善makespan的大部分收益表现为其他核相对它的尾部差距缩小。
G500 P2直接walk额外等待计数为2,152,769cycles，仍可同时出现官方CPI下降。该计数和active/makespan有不同暴露与重叠关系，不能相减建立加性归因。Seq增量恰等于PTE读数，Sequencer普通store错误准入修复保持成立。
各核终点之和除task宏指令数仅为辅助诊断量，分子包含完整报告的物理核周期，不能替代正式active CPI或直接与gem5重新评分。G500 P1的K×T−ΣCk为13,748,416cycles，P2为11,047,892；这不是已证明的“FastSim独有等待”。窗口截在退休前缀，尚未取得gem5每核最后退休时刻，不能排除原生尾部及边界未退休工作。

## DRAM：现在同窗，但还不是同请求cohort

下面native严格读取新来源stats-common-end.txt，FastSim读取同一新FST输出的dram_controller_reads。双方均为控制器读burst阶段，均包含各自实际模型中的物理流量；native还可能包含页表、投机等流量，FS包含数据读、store写分配及取指。因此总数相近不是逐请求身份相同。
本轮核查了endpoint：gem5在DRAM burst服务路径累计cmd_at-entryTime，data-ready后才加入frontend/backend返回延迟；FS观察command_at并从completion剥除frontend/backend。不能把约31cycles的固定frontend差直接当作排队差的解释。

| case / arm | 读burst | arrival→service均值，cycles | service→data-ready均值，cycles |
|---|---:|---:|---:|
| marian native | 403,451 | 40.1431 | 52.5285 |
| marian P1 | 403,404 | 70.0166 | 54.0000 |
| marian P2 | 403,412 | 69.6054 | 54.0000 |
| g500 native | 715,328 | 92.9544 | 52.5285 |
| g500 P1 | 712,663 | 40.1066 | 54.0000 |
| g500 P2 | 713,973 | 39.1700 | 54.0000 |

Marian：FS排队约70、native约40，偏差方向与FS偏慢相容。G500：FS约40、native约93，偏差方向与FS偏快相容。这是同源物理诊断，不是同请求队列误差认证；禁止用均值差乘某一方请求数宣称解释了多少CPI。
FS的service→data-ready固定54cycles，native为52.5285cycles，保留按源参数取整/紧凑模型近似边界，不调常数拟合。

### 候选发射轴线索

| P1模型生命周期观测 | Marian | G500 |
|---|---:|---:|
| 平均candidate→corrected issue位移，cycles | 10396.4 | 1883.1 |
| 未投影controller arrival早于corrected issue | 98.20% | 93.84% |

两案都在候选轴求共享服务，再投影相对延迟，这是直接的模型内事实。平均位移本身不能推出相邻请求间隔压缩，需要逐请求Δe和跨核顺序；G500同样大量“早到”却排队偏低，否定了“早到比例高就必然排队偏高”的普遍推断。Marian的时间轴压缩假说仍值得验证，尚未由本轮聚合数据完成因果认证。
G500另有行局部性线索：P1稳定FR-FCFS样本行命中率约73.43%，native全部物理读为46.86%；模型207个epoch fallback，稳定样本并未覆盖所有请求，因此这不是同人口行命中率精度结论。它与过低排队相容，提示下一步应配对到达顺序、行状态和批边界，而非只加FB等待。
仓库已有diagnostics.projected_dram_path，但它记录canonical arrival，不能当作corrected-axis到达流。被否决的projected_feedback和暂停的pending-fill补偿均未开启；本轮没有冒充完成corrected-axis影子FR-FCFS反事实。

## 计数门禁：并非所有cache计数都已对齐

以下为维护validator的workload-task native tag-miss诊断，单位为cache-line demand事件，不能改名为退休load miss或SPR硬件事件。

| P1事件 | Marian FS / native | 相对差 | G500 FS / native | 相对差 |
|---|---:|---:|---:|---:|
| l1d_tag_misses | 469,682 / 455,239 | +3.17% | 1,432,450 / 1,431,766 | +0.05% |
| private_l2_tag_misses | 423,455 / 199,785 | +111.96% | 713,191 / 729,774 | -2.27% |
| llc_tag_misses | 199,923 / 189,389 | +5.56% | 712,663 / 713,930 | -0.18% |

G500的L1/LLC计数接近，但不代表服务时序已对齐。Marian private-L2仍有约112%差异，必须保留为独立分类/路径问题；不能把writeback数减掉，也不能把“计数已全面对齐”当作前提。

## FB：G500人口闭合线索仍强，关键路径还缺证据

| P1退休cache诊断 | Marian | G500 |
|---|---:|---:|
| FS L1 hit | 2,097,455 | 32,803,853 |
| native L1 hit | 1,682,589 | 24,367,240 |
| FS FB hit | 0 | 0 |
| native FB hit | 422,565 | 8,436,935 |
| FS(L1+FB)−native(L1+FB) | -7,699 | -322 |

G500的L1+FB总数仅差322，而native报告8,436,935次FB、模型为0，支持分类/在途回填可见性存在缺口的判断。如果该机制单独闭合P1官方CPI残差，每个native FB平均需暴露约1.42cycles；这是反推所需量，不是实测FB等待，也不是收益预测。

模型pending-fill和Sequencer行合并计数为0不等于每次native FB都给模型漏掉了暴露等待：较早miss的按序退休可能自然吸收后续load等待；同一宏指令的多成员分类也不能仅靠两个总数建立逐条对应。native FB还存在store→load forwarding语义边界，G500事件即使unknown=0也没有获得硬件等价认证。
这一补偿若只增加load等待，方向上会进一步恶化偏慢的Marian。本轮没有开启暂停的补偿，也没有把FB计数差直接当成CPI周期差。

## 严格退休miss资格

严格CLI重复扫描保留为cancelled，不记作passed。资格检查复用了每个arm已完成的维护版native validator来源审计和完整current_retired_cache_evidence；对全部534项fingerprint输入、完整功能artifact、manifest/plan、oracle/来源文件集合、预测及工具/字典重新哈希，全部相同，再直接调用维护版compare_retired_cache(misses_only=True)。没有重写broad fingerprint或复制参考选择逻辑。收据见strict-reuse-binding.json、strict-contract-p1.json、strict-contract-p2.json及strict-cli-cancellation.json。

- P1：passed=1，failed=1，pending=0。
  - c04-872.marian_s: failed; ['ValueError: native retired-cache unknown membership: core=3 scope=kernel event=retired_l1_load_misses unknown=2']
  - c16-854.graph500_s: passed; []
- P2：passed=1，failed=1，pending=0。
  - c04-872.marian_s: failed; ['ValueError: native retired-cache unknown membership: core=3 scope=kernel event=retired_l1_load_misses unknown=2']
  - c16-854.graph500_s: passed; []

广义retired-cache比较保留unknown和incomplete-evidence标签，不能替代三项主miss的全量native零unknown门禁。Marian被拒绝的2条unknown属于core3；严格CLI先遇到physical scope、复用JSON后严格函数先遇到kernel scope，均是同一源数据中未完成分类的成员，错误原文分别保留。严格资格失败不会被当成0误差，也不通过改分母、删unknown或回退path-class来放行。

G500三项主事件的完整native资格门禁通过；以下是模型全量点估计与该参考的计数对照，不是硬件SPR事件认证。Marian不进入此表或该主事件聚合。

| G500 retired load miss | P1点估计 | P2点估计 | native | P1计数相对差 | P2计数相对差 |
|---|---:|---:|---:|---:|---:|
| retired_l1_load_misses | 1,431,133 | 1,431,259 | 1,430,874 | +0.0181% | +0.0269% |
| retired_l2_load_misses | 713,145 | 713,176 | 729,668 | -2.2645% | -2.2602% |
| retired_l3_load_misses | 712,663 | 712,663 | 714,167 | -0.2106% | -0.2106% |

## 本轮边界与下一步

本轮完成四组新同源重放、来源/人口评分、全部直接参数匹配、同窗物理诊断和G500确定性复核。没有模型时序修复、默认推广、gem5重采、全六项/全矩阵、S2/S3吞吐认证、commit或push。

要进入机制修复，需要先取得同请求的candidate/corrected arrival、controller服务以及load fill/issue/retire配对；验证Δe是否压缩间隔、G500行局部性差来自哪类重排，并测量FB剩余等待在退休前沿的暴露。现有总量不足以决定一个通用修正量；先完成这些只读反事实/关键路径观察，再预登记两案预期方向。

## 逐核P2−P1周期差（诊断）

| core | Marian C4 | G500 C16 |
|---|---:|---:|
| 0 | -79,621 | -30,926 |
| 1 | +3,316 | -539,122 |
| 2 | -16,316 | -122,204 |
| 3 | -806 | +252,118 |
| 4 | — | -350,072 |
| 5 | — | +228,176 |
| 6 | — | -63,606 |
| 7 | — | +161,633 |
| 8 | — | +24,411 |
| 9 | — | -113,078 |
| 10 | — | -54,930 |
| 11 | — | -44,916 |
| 12 | — | +83,429 |
| 13 | — | +4,525 |
| 14 | — | +336,561 |
| 15 | — | -195,235 |

主要证据位于 `tmp/common-end-replay-20261030/`：freeze.json、effective-config-difference.json、runs/、validation-native-*、validation-strict-*、analysis.json、repeat-target-comparison.json。原始失败/过严比较日志均保留。独立复核确认聚合及来源边界，比较器已改为明确列出宿主字段、比对全部其余顶层/嵌套字段；对target blocked_wall_cycles和measurement_scope的变异检查证明它们不会被误排除。
