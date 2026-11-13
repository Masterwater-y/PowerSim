# SPR-M1：FLAC、Graph500、Marian 的 CPI 缺口与 DTLB/STLB 对齐分析

日期：2026-11-11。对象是当前已恢复的 **SPR-M1 — Retired Prepare baseline**，冻结 R0/R1 `v2-wait`。二进制 SHA-256：

```text
dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905
```

结论：**DTLB/STLB 确实尚未完整对齐 gem5-spr，但现有证据不支持把三个负载的 CPI 缺口都归因于它。** FLAC 的逐核分布直接反对“DTLB 等待主导当前误差”；Graph500 应优先调查内存等待暴露、父服务时间和并行重叠；Marian 应优先调查 store/SQ、拷贝流及软件预取的服务生命周期。后三类是有证据支撑的调查方向，尚不是完成的互斥因果归因。

本次读取已有预测、维护 validator 绑定的原生账本、实际 `config.ini` 和源码；重新核验二进制及小型证据文件哈希并计算表格。没有新仿真、生产模型修改或吞吐测量。[机器可读证据](spr-m1-three-workload-cpi-gap-evidence-20261111.json)记录逐核数据、配置、输入资格、原生门禁状态与来源 SHA。

## 1. 当前误差：先锁定版本、输入和计量口径

口径为 `workload-task active cycles / retired macro instructions`，`common-start-v1` 起点，`retired-stream-completion-v1` 终点。gem5 分子来自独立 `BaseCPU` 活跃周期账本。所有三案已有逐核人口匹配和 `scored-active` CPI，不使用最终 `stats.txt` 的额外运行尾部或含 idle 的总周期。

| 负载 | gem5 CPI | SPR-M1 CPI | CPI绝对误差（cycles/macro） | 相对误差 | 活跃周期差（FastSim−gem5） |
|---|---:|---:|---:|---:|---:|
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% | +1,050,690 |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | −13.51% | −4,060,689 |
| Marian | 2.364997 | 3.068823 | 0.703826 | +29.76% | +17,146,431 |

| 聚合范围 | CPI MAE（cycles/macro） | CPI MAPE | CPI WAPE |
|---|---:|---:|---:|
| 本文三个负载 | 0.289400 | 18.39% | 23.06% |
| SPR-M1 全部五负载，含 Zstd/XZ | 0.191599 | 12.99% | 17.48% |

MAE/MAPE 按负载等权；WAPE 为同口径周期绝对差之和除以 gem5 活跃周期之和，等价于宏指令加权 CPI 绝对误差。三案不是完整五案的替代评价集。

功能内容身份为 `complete`，但 `roi_edges=false`、`macro_boundary_complete=false` 的采集能力缺口仍保留。Marian 的整案 native 验证因一个退休 cache 成员 unknown 而 `failed`；其独立 CPI 已评分，绑定的 native task 请求证据也有效，不能把该整案失败改写为“全部 PMU 已认证”。本文缓存请求值是原生诊断，非严格硬件 PMU。

## 2. DTLB/STLB：哪些机制确实没有对齐

这三批 gem5-spr `config.ini` 中，四个 switched O3 核均有 DTLB=96、ITLB=256、STLB=2048。每核 DTLB 和 ITLB 指向该核同一个 `mmu.stlb`，`spr_shared_domains=true`；**共享的是同核 I/D 翻译域，不是四核共享一张 STLB。**

| 项目 | 当前 SPR-M1 | 此次 gem5-spr 目标 | 判断 |
|---|---|---|---|
| L1 DTLB 容量 | 96 | 96 | 容量已对齐 |
| DTLB miss 的第二级查询 | `spr_dtlb_stlb_proxy=false` | 查询 2048 项同核 I/D STLB | 明确结构缺口 |
| STLB 命中返回 | 当前不走 STLB | D-side 6 cycles、I-side 7 cycles | 缺少第二级命中路径 |
| 真正 page walk | 一个 walker，默认服务 12 cycles；可能另排队 | 发起页表内存请求，经缓存/内存与 walker 状态机完成 | 服务和争用模型不同 |
| 页表访问对层级的作用 | `hierarchy_walk=false`；三案 hierarchy request 均为 0 | walker 发送 PTE 请求，受缓存、端口和响应影响 | 当前没有这类模型流量 |
| 映射与页大小 | 已有动态翻译伴随；支持 4K/2M/4M/1G | 多页型、PCID/global、控制状态影响 | 不能声称当前“只认 4K” |
| 失效与 pending 命中 | 动态映射版本有局部处理；完整控制证据不足 | STLB 延迟响应检查失效代数、CR3/CR4/EFER，必要时重试 | 未完成生命周期对齐 |
| 同页 walk 自动合并 | 当前 `coalesce_misses=false` | `Walker::start` 中仍有 coalescing TODO，请求进入 `currStates` 队列 | 不能以“gem5 默认会合并”作为修复依据 |

源码见 [FastSim 翻译与 walker 日历](../src/interval_core.cpp#L1400-L1610)、[伴随加载](../src/trace.cpp#L2080-L2120)、[gem5 STLB 延迟响应](../gem5-spr/src/arch/x86/tlb.cc#L560-L630)、[gem5 walker 队列及 TODO](../gem5-spr/src/arch/x86/pagetable_walker.cc#L111-L139)。

FastSim 的非 STLB-hit walk 基本关系是：

$$
t_{\mathrm{start}}=\max(t_{\mathrm{lookup}},t_{\mathrm{walker\ ready}}),
\qquad
t_{\mathrm{ready}}=t_{\mathrm{start}}+12.
$$

因此 `page_walk_latency=12` 不代表每次 miss 只延迟 12 周期：共享单 walker 的排队还会增加等待。在当前模型中，功能 LRU 会立即填入，而 timing LRU 等 completion 后才填入；两套 miss 数不应相等，也不应混作 native DTLB PMU。

**输入能力也是限制，不能只改开关。** 三案均有四个 `.xlat.gz`，wire version 为 2；源准备报告使用 `fst-dynamic-translations-v3` 审计 schema，但不能因此声称 wire 已升级到 v3。功能映射字节覆盖完整、unknown mapping records=0，仍然有 `origin_context_binding=unavailable`、`logical_mmu_controls_complete=false`、I-side retained macro 翻译 `not-declared`、core static-map ordering `unavailable`。映射完整不等于 TLB 状态、发起上下文和失效过程完整。

已有 D-only STLB 代理只覆盖部分 PCID0、页型及控制资格，miss 时采用退休顺序即时插入；它不是完整同核 I/D STLB。pending 组件或独立单元测试通过，也不等于负载已经接入并合格。详见 [STLB 增量与限制](spr-shared-stlb-increment-20261105.md)。

## 3. 当前 DTLB 数量级及其解释边界

| 负载 | 模型功能 miss | 模型 timing miss | 模型事件累计等待（cycles） | 同任务同窗口 native DTLB miss | DTLB 对当前 CPI 差的因果份额 |
|---|---:|---:|---:|---|---|
| FLAC | 409 | 2,781 | 423,615 | N/A | 尚未测得 |
| Graph500 | 4,628 | 13,732 | 313,320 | N/A | 尚未测得 |
| Marian | 1,457 | 6,401 | 530,823 | N/A | 尚未测得 |

三案 timing merged/untracked 均为 0，STLB 命中/查询诊断均为 0。`missing_controls=0` 在代理关闭时不能证明 MMU 控制完整。没有独立、同窗口、同 task 的 native DTLB 账本，不能用最终物理核 `rdMisses+wrMisses` 直接计算上述 miss 的误差。

事件等待可能重叠，并改变后续请求到达和争用。因此：

$$
\Delta CPI_{\mathrm{DTLB}}
\ne
\frac{\sum_j \mathrm{walk\ wait}_j}{N_{\mathrm{macro}}}.
$$

右侧仅为诊断归一化值：FLAC 0.013647、Graph500 0.010071、Marian 0.021789 cycles/macro。它既不是可删去的周期，也不是 DTLB 修复收益或因果上界。尤其 Graph500 当前是低估，缺少真实 PTE 流量可能低估等待，缺少 STLB 又可能高估部分翻译延迟，两类偏差可以反向；聚合等待不能决定净效应。

## 4. FLAC：现有反证最强，优先看执行与分配语义

| 核 | gem5 CPI | SPR-M1 CPI | CPI绝对误差（cycles/macro） | 相对误差 | 活跃周期差 | timing DTLB miss | DTLB累计等待 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0.486513 | 0.485456 | 0.001057 | −0.22% | −4,798 | 2,439 | 391,523 |
| 1 | 0.243246 | 0.283722 | 0.040476 | +16.64% | +367,486 | 15 | 253 |
| 2 | 0.261958 | 0.307139 | 0.045181 | +17.25% | +380,901 | 313 | 31,597 |
| 3 | 0.245600 | 0.279752 | 0.034153 | +13.91% | +307,101 | 14 | 242 |

core0 占 DTLB 等待的 **92.42%**，却已基本对齐；核心正误差在 core1–3。core1 与 core3 的等待仅 253/242 cycles，仍分别多算 36.75/30.71 万活跃周期。这不排除翻译对跨核到达序列的间接作用，但强烈反对“当前 11.89% 主要是逐核 DTLB 多收等待”的解释。

历史 FLAC 大回归已定位到共享 FU 的未来预约阻挡：保留目标延迟、启用 gap-aware 后，在旧同源控制中 CPI 从 0.500305493 降至 0.320469114，gem5 0.282346215；AE 从 0.217959277 降至 0.038122899，覆盖该次旧差的 82.51%。**当前 `spr_shared_fu_gap_aware=true`，这部分收益已经在 SPR-M1 内，不能再算一遍。**

另一份旧 FLAC 同源 D-only STLB OFF/ON 实验，CPI 0.3210412161→0.3197865156，gem5 0.2818719356，AE 0.0391692805→0.0379145800；变化约 −0.0012547004 CPI，仅覆盖那次旧差的约 3.20%。该二进制和输入不同于当前，不能预测当前收益，但足以否定“已有 STLB 实验已经证明可以消除全部 FLAC 差距”。

当前值得优先验证的是静态 MOV/zero-idiom、共享 allocation slot、整数执行族和依赖别名。gem5 消除 MOV 时不再占普通 IQ/FU，但下游仍依赖源 producer；FastSim 当前相关路线未完整实现这些语义。旧源曾显示 core1–3 的 MOV 消除人口很高，与当前残差核的位置一致，这是优先级线索，不能把旧计数乘一拍当当前误差。当前源没有声明完整 `.core` 功能事实，`spr_integer_family_enabled=false`；即便 `.uops.gz` 已存在，也不能直接补出 MOV/fusion/pin 的全部资格。

当前 FLAC 请求 L1/L2 miss 偏高也是候选，见下一节；其 LLC miss 反而偏低，不能只按“所有 miss 太多”解释。需要分别验证执行路径与私有侧缓存结果，而不是关闭 shared FU 或改 SIMD 延迟来拟合。旧 `shared_fu=false` 控制同时改变端口和真实目标延迟，不是合格修复。

## 5. Graph500：miss 数对齐，等待与重叠仍不足

四核 CPI AE 分别为 0.133819、0.131078、0.128155、0.129058 cycles/macro，相对误差 −13.83%、−13.56%、−13.27%、−13.36%；各核分别少算 1,039,714、1,019,191、997,549、1,004,235 cycles。是分布稳定的四核缺口，不是单核结束、人口丢失或一个异常翻译热点。

以下缓存请求真值均来自维护 validator 绑定的 conserved Ruby/SLICC native task evidence；不是 `kernel-events*.json` path-class 字段。

| 负载 | L1D tag miss：FastSim / native / RE | private L2 tag miss：FastSim / native / RE | LLC tag miss：FastSim / native / RE |
|---|---|---|---|
| FLAC | 26,163 / 18,479 / +41.58% | 4,605 / 3,401 / +35.40% | 1,076 / 1,491 / −27.83% |
| Graph500 | 556,590 / 556,493 / +0.0174% | 277,741 / 277,711 / +0.0108% | 277,645 / 277,636 / +0.0032% |
| Marian | 537,483 / 593,102 / −9.38% | 423,475 / 459,543 / −7.85% | 223,214 / 450,620 / −50.47% |

L1D/private-L2 为 proxy、LLC 为 diagnostic；这些是请求 tag miss，不能改名为退休 load miss。同样，数量接近不证明逐请求的等待、服务来源、顺序或关键路径相等。

同一批输入的 pre-parent→冻结 M1 对比，Graph500 CPI 0.754675→0.835896，gem5 0.966422；AE 0.211747→0.130526，改善约 0.081221 cycles/macro，约为旧差的 38.36%。该版本对比支持父服务等待值得调查，但不是单变量 DTLB 消融，也不能把全部改善认作某一个 completion edge 的贡献。

当前 M1 有 **1,288,590 次 follower wait**、累计 176,612,360 wait cycles、4,954,470 次 source fallback；ordinary reads=13,325,391、certified services=93,648。这些计数支持检查普通 load 与同线服务时序，但各类别角色可重叠，不能用一个比值宣称“未覆盖 X% CPI”。

优先配对新冷 owner 的准入、follower 的剩余等待、数据可见/权限/controller/释放边、局部回填与负载就绪、LQ/ROB/端口的等待暴露，以及跨核/DRAM 请求到达。miss 个数匹配仍可能由于过乐观重叠或服务提前可见而低估 CPI。

Graph500 的退休 L1 miss 估计另有 +27.921% 偏差，主要落在回填观察器的成员扩展，而退休 L2/L3 接近。该观察器是 **不反馈时序的 PMU 估计层**，所以多报 L1 不能直接解释 CPI 少算；观察器与 timing 可能共享有问题的时钟，需分别证明。见 [退休 miss 分析](../fastsim-r01-retired-cache-miss-attribution-20261111.md)。

后续漂移版 `wait/merge/publish` 曾产生更大的双向 Graph500 回归，也有纯 load 冷 owner 时间锚见证；它们是后续变更的证据，不证明冻结 M1 已有完全相同的错误，不能把这些 CPI 或见证偷换成当前结果。

## 6. Marian：服务生命周期优先，旧 C8 DTLB 诊断不能搬来

| 核 | gem5 CPI | SPR-M1 CPI | CPI绝对误差（cycles/macro） | 相对误差 | 活跃周期差 | DTLB累计等待 |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 2.599513 | 3.419285 | 0.819772 | +31.54% | +4,537,235 | 141,081 |
| 1 | 2.599752 | 3.397340 | 0.797588 | +30.68% | +4,420,671 | 154,399 |
| 2 | 2.513678 | 3.354565 | 0.840887 | +33.45% | +4,820,247 | 127,284 |
| 3 | 1.907983 | 2.353988 | 0.446005 | +23.38% | +3,368,278 | 108,059 |

所有四核均偏慢，core2 多算最多；该核 DTLB 等待并非最多。当前总 walk 累计仅 530,823 cycles，对应诊断归一化 0.021789 cycles/macro，而 AE 为 0.703826。这不能构成因果上界，但已经不支持简单的“多收 page-walk 等待解释 1715 万周期”。

当前 native 服务路径中有 523,998 个含 software-prefetch 的退休 load 宏成员、1,048 个含 split/retry 的成员；这些路径人口可重叠，**不是软件预取指令数**。store-forwarding 宏成员模型/native 为 3,369,575/3,398,424；接近的宏成员计数不证明双方转发存活边界相同。当前普通 load 父服务等待只有 18,389 次，special fallback=8,338,256，ordinary reads=3,154,369。Marian 的普通 follower-wait 路径覆盖与 Graph500 明显不同，不能期待只修普通 load 合并即可闭合。

Marian 的 LLC request miss 少报约一半，退休 L3 已知成员诊断却仅约 +2.10%（一个 native unknown，严格主误差 N/A）。这两件事可同时成立：退休 load 事件不覆盖全部 store/prepare/软件预取请求。**整体少 miss、CPI 却偏高，指向“哪些请求被计入、什么时候发出、谁等谁、何时释放”的检查，不能直接宣布纯 DRAM 延迟过大或 LLC 延迟常数错了。**

重点是退休 prepare→store takeover→最终写 callback→SQ 释放、四行发送窗口、数据/权限完成分离，以及软件预取 CPU ack 与后台 fill/资源释放的分离。当前保留 `legacy_prepare_timing=true`，但 `buffer_tso_prepare_lifecycle=false`、store-window feedback 和 SQ join-credit proxy 关闭；native 每核 store line window=4。功能预取仍在，`software_prefetch_early_ack=false`。这证明覆盖仍是近似，尚未证明每项贡献多少周期。

两个历史结论必须校正到当前版本：

1. **旧 Marian C8 的 FR-FCFS 选择窗口退化到 1，当前配置已取消这一缩窗。** M1 为 `frfcfs_topology_scaled_window=false`、selection window=0 使用 read buffer=64、causal selection=true。旧 C8 的 38.46% DRAM 干预敏感性不能计入当前缺口。当前队列、命令和到达近似仍可有残差，取消缩窗也不等于完整 FR-FCFS 对齐。
2. **旧 C8 关闭 DTLB 后反而更慢。** 同一旧 FST 中，gem5 2.622349880，baseline 3.034149261（AE 0.411799381，+15.70%），DTLB-off 3.141320910（AE 0.518971030，+19.79%）；活跃周期增加 4,193,311。它说明翻译 pacing 与争用会交互，否定“减去 29M 累计等待就是收益”。该实验使用 C8、不同二进制及旧 common-end 计量；其 miss 202,122 和最终 native 7,263 不是当前 C4 同窗口误差，不能移植。

同输入 pre-parent→M1 的 Marian CPI 3.040902→3.068823，gem5 2.364997，AE 0.675905→0.703826。当前绝大部分缺口在父服务扩展前就存在。后来 publish 的 store 排空重复收费及 73.95 CPI 雪崩属于后续版本；当前 M1 是 3.068823，不能用已经恢复掉的雪崩根因直接解释当前全部残差。详见 [旧 C8 诊断](marian-c8-32g-10m-cpi-diagnosis-20261027.md)、[同输入版本对比](../fastsim-r01-v2-wait-noearly-comparison-20261110.md)、[后续回归分层](../fastsim-v2-publish-residual-gap-analysis-20261111.md)。

## 7. 哪些结论能落地，怎样进一步量化

| 证据层级 | 当前能够确认 | 当前不能确认 |
|---|---|---|
| 直接配置/源码/输出 | M1 没有完整 I/D STLB 和 PTE 服务；MMU/I-side 输入资格不足；三案确切 CPI、逐核周期、诊断数量 | 机制存在差异不等于它是主因 |
| 当前同输入版本对比 | Graph500 父服务扩展方向改善；Marian 多数残差是既有缺口 | 版本对比不是各组件互斥归因 |
| 历史同源消融 | FLAC gap-aware 修过大回归；旧 D-only STLB 收益小；旧 Marian DTLB-off 会恶化 | 不同二进制/输入/计量的收益不能填当前误差预算 |
| 待验证假设 | FLAC 执行与分配、Graph500 等待暴露、Marian store/拷贝流生命周期 | 各因素的当前 CPI 份额、修复收益、吞吐成本 |

下一轮应从冻结 M1 派生隔离候选，先保持行为不变建立诊断，再每次只改变一个有目标依据的机制：

1. FLAC 先在 core1–3 配对静态执行/分配资格、producer alias 与私有侧路径；不再次关闭 shared FU 或缩短正确 SIMD latency。
2. Graph500 优先做纯 load、新冷 owner、晚到 follower 见证，并检查实际准入到数据可见/释放边的有界剩余等待；保持请求人口守恒后再评估总 CPI。
3. Marian 优先配对 prepare、接管、write callback、SQ release 及软件预取 ack/background fill；之后再区分实际已到达 DRAM 队列的残差。
4. DTLB/STLB 独立建立输入与状态合同：使用 Atomic/QEMU 可取得的有效页型、翻译发起上下文、退休 MMU 控制及 I-side 功能映射；未知显式回退。采用固定容量同核 I/D STLB、pending completion 与 generation 检查，有界 walk 服务近似；不回放 OoO 的实际命中或时间，不按负载拟合延迟，不额外生成 speculative/wrong-path 流量。
5. 同输入对比 CPI/原生诊断/退休事件，并保留逐核 AE。组合干预需另测交互，不能相加单项收益；吞吐独立按同输入串行配对验收。

这些工作应保持现有并行两阶段，使用固定容量/固定处理次数的近似，不改为每周期流水线或整窗口反复收敛。保留退休 prepare 和功能软件预取；完整硬件预取关闭的采集事实不授权删去退休成员。

## 8. 复核入口

- [当前三案证据 JSON](spr-m1-three-workload-cpi-gap-evidence-20261111.json)：来源 SHA、实际 target TLB/配置、逐核误差及未评分字段。
- [抽取脚本](../tmp/restore-r01-main-20261111/analyze_m1_cpi_gap.py)：只读取维护 validator 已绑定的参考；不另选 native cache truth。
- [恢复验证汇总](../tmp/restore-r01-main-20261111/validation-summary.json)：当前五案与冻结值完全一致；该文件记录的是恢复时状态，不代表后来没有 commit/push。
- 原生报告：[FLAC](../tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-817.flac_s/validation.json)、[Graph500](../tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-854.graph500_s/validation.json)、[Marian](../tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-872.marian_s/validation.json)。
- 输入资格与实际 target：[FLAC 源准备](../tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-817.flac_s/spr-capture-preparation.json)、[Graph500 源准备](../tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-854.graph500_s/spr-capture-preparation.json)、[Marian 源准备](../tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-872.marian_s/spr-capture-preparation.json)。各同级 `config.ini` 为本文直接参数证据。
- [FLAC 历史诊断](spr-flac-c4-cpi-diagnosis-20261105.md)、[STLB 历史控制](spr-shared-stlb-increment-20261105.md)、[整数执行族边界](spr-integer-family-arbitration-20261105.md)、[报告口径合同](accuracy-reporting-contract.md)。
