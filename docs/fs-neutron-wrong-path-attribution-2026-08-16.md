# C8 Neutron 错路径归因（2026-08-16）

## 结论

本轮已经得到严格同窗口的 gem5/FastSim 对照，但还不能把错路径模型提升为生产默认：

- C8、每核 100k 用户 UOP、source warmup 的同一 measurement window 中，gem5 用户态
  CPI 为 1.004926，FastSim 为 0.575038，APE 为 42.778%；
- CPL-aware v2 在用户态共接受 6,478 次 branch-mispredict squash。每次错路径平均
  fetch 68.500 条、issue 30.332 条，P99 分别为 215 和 147 条；
- 用户错路径 fetch/issue 的满宽容量暴露分别只有 0.069413/0.030730 CPI；所有用户
  错路径活跃区间按 core 取并集后为 0.164830 CPI，只覆盖 0.429889 CPI 缺口的
  38.343%；
- v3 实测 13,642 条 squash 时已执行但未完成的用户错路径访存中，只有 1,510 条
  后续收到 DataAccessComplete（11.069%）。这些回调的完成延迟均值/P99 仅为
  0.003799/0.012012 cycle，不存在可解释 0.43 CPI 的长尾请求；
- 即使做一个故意悲观、并不合法的 `FastSim + active-window ceiling` 相加，CPI 也只有
  0.739868，APE 仍为 26.376%。因此，单纯增加固定 branch penalty 或匿名 ROB drain
  不可能独立关闭 Neutron 尾差。

这里的 0.164830 是“从最早观测到的错路径 stage 到 squash”的直接干扰窗口上界，
不是可加的 CPI 修正，也不覆盖 squash 后仍可能存在的 cache、TLB、MSHR 或内存系统
状态影响。反过来，窗口内还有 older-path 有效工作，因此它也不是实测损失周期。

## 数据身份

- gem5 结果：
  `tmp/wrong-path-oracle-c8-v2-pilots/results/sample/mesi-three-level-3GiB/8c/881.neutron_s/5775cbab41dd2369c226/20260816T094741Z`
- FST measurement records：800,005；functional warmup records：37,195,364；
- FastSim 重放使用该结果自己的 `tao_trace/manifest.txt`，而不是 10M 正式 trace；
- FastSim `measurement_scope=user`，syscall/page-fault/IRQ event model 均关闭；
- `wrong_path.jsonl` 为 schema v2，只覆盖 functional measurement window，元数据固定
  声明 `oracle_only=true`、`fst_input=false`、
  `cpl_attribution=decoded-x86-mode`；
- v1 曾把 measurement gate 内的 CPL0 squash 混入用户态归因。Graph500 的 100k
  窗口将问题放大为 2,930 个混合 branch episode 对 4 个用户态退休 miss，因此所有
  用户态结论已经由 v2 重采替代。v1 仅保留为审计证据。

正式 10M C8 Neutron 的用户态 APE 为 46.709%，而 100k 同窗口为 42.778%。二者窗口
长度不同，不能把一个窗口的 FastSim CPI 与另一个窗口的 gem5 CPI交叉相减。

## 守恒与事件结构

严格 validator 通过以下检查：schema 和 oracle/FST 隔离、episode 引用、动态序号唯一、
所有 instruction 均年轻于 squash cutoff、stage tick 单调，以及逐 episode 的 stage/count
完全守恒。v2 还逐 episode 验证 user/kernel/unknown CPL instruction records 之和等于
`instruction_records`。用户态表只保留 `cause_cpl=3` 的 exact branch/memory-order
episode；fallback 的 victim CPL 仍保留在 instruction 行，但其原因 CPL 固定为 unknown，
不会再被误当成用户分支。

| 原因 | episode | fetch | rename | dispatch | issue | ToCommit | memory | data complete |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| branch mispredict | 6,478 | 443,742 | 353,798 | 308,199 | 196,491 | 190,498 | 38,261 | 5,970 |
| memory order | 5 | 505 | 332 | 291 | 180 | 178 | 59 | 25 |

用户态接受的 branch squash 比退休态 user branch misses 5,711 多 767 次。这不是 PMU
重复计数：错路径上的分支可以先触发 redirect，随后自身又被更老的 redirect 清除；退休
PMU 看不到这种 nested wrong-path branch，而 Commit squash oracle 会看到。外层
`stats.txt` 还运行到所有 core 都完成，因此它的全局 stop window 也不能与 per-core
冻结的 sidecar 直接要求相等。

## Held-out 机制反例

四个 v2 pilot 都使用每核 100k 用户 UOP、各自正式 request 对应的 checkpoint/磁盘，并
用自己的 warmup-slice manifest 做同窗口 FastSim 用户态重放：

| workload | gem5/FastSim user CPI | signed error | user branch episode / retired miss | active ceiling | `FastSim + ceiling` APE |
|---|---:|---:|---:|---:|---:|
| Neutron | 1.004926 / 0.575038 | -42.778% | 6,478 / 5,711 | 0.164830 | 26.376% |
| Graph500 | 0.334625 / 1.029911 | +207.781% | 7 / 4 | 0.000179 | 207.834% |
| LBM | 4.173701 / 4.784224 | +14.628% | 859 / 694 | 0.073720 | 16.394% |
| NAMD | 0.371965 / 0.336532 | -9.526% | 806 / 716 | 0.016189 | 5.174% |

这张表不是新的正式 CPI 报告。特别是 Graph500 的 100k 窗口是高度规则的短 phase，
user+kernel CPI 为 1.937028，不能替代其 10M 正式窗口。但它是有效的机制反例：该用户
窗口只有 7 次 branch squash，v1 中其余 3,774 个 CPL0 cause 和 17,756 个 unknown
fallback 已被 v2 排除。LBM 已经高估，任何无条件增加的错路径周期都会继续恶化；NAMD
的上界也不是缺口的通用比例。因此不能从 Neutron 单点拟合固定 penalty、depth scale 或
workload coefficient。

## 错路径访存足迹与同窗 PMU

v2 可严格区分 squash 前已完成和 squash 时仍未完成的用户态错误路径访存，但不能观察
后者是否在 squash 后返回。地址足迹按 64B physical cache line、4KiB page 去重：

| workload | wrong-path memory | squash 前 data complete | squash 时 executed/incomplete | 完成 unique line/page | gem5/FastSim L1D miss | L2 miss | LLC miss |
|---|---:|---:|---:|---:|---:|---:|---:|
| Neutron | 38,320 | 5,995 | 13,642 | 717 / 140 | 16,199 / 15,151 | 10,725 / 9,533 | 890 / 904 |
| Graph500 | 50 | 5 | 6 | 3 / 3 | 5 / 8,340 | 0 / 8,320 | 0 / 3,518 |
| LBM | 7,340 | 1,167 | 1,767 | 171 / 44 | 3,199 / 19,966 | 3,151 / 17,447 | 3,146 / 17,441 |
| NAMD | 6,231 | 1,176 | 1,424 | 252 / 61 | 7,407 / 8,587 | 846 / 1,178 | 419 / 751 |

Neutron 的 squash 前完成率为 15.645%，与 LBM 15.899%、NAMD 18.873% 同量级；它的
突出点是错路径访存总暴露量，而不是一种 Neutron 独有的完成概率。Neutron 的 L1D/L2
缺口与 717 条完成 cache line 同量级，但 LLC miss 已接近且方向相反；仅靠把这些地址
无条件写入 cache state 不能解释 0.429889 CPI 缺口。Graph500/LBM/NAMD 的 cache PMU
还提供了强反例：FastSim 已经高估 miss，通用污染模型会继续恶化。

为消除 v2 的观测盲区，采集器已升级为 schema v3：它只对“已执行、squash 时未完成”
的请求保留 measurement-window-scoped tombstone，并在后续 DataAccessComplete 到达时
输出独立 `late_data_complete` 行。该行仍是 oracle-only，不进入 FST。

Neutron v3 严格 validator 共看到 1,566 条 late callback；按 `cause_cpl=3` 和 instruction
`cpl=3` 过滤后为 1,510 条，全部来自 branch mispredict。它们覆盖 764 条 physical line
和 142 页；其中 259 条 line/139 页已被 squash 前完成集覆盖，新增覆盖为 505 条 line、
仅 3 页。把 squash 前和 squash 后完成集做并集得到 1,222 条 physical line，与当前
L2 miss 少算的 1,192 次同量级，因此 exact cache-tag ablation 仍值得作为 PMU 诊断；
但回调没有 target-cycle 长尾，且该地址集合不可用于生产推理，不能把它解释为 CPI 修复。

v3 与 v2 的八份 FST 和八份 vmap 逐文件 SHA-256 相同，`kernel_events.json` 也逐字节
相同。用同一 target-aligned FastSim 配置重放后，scope cycles/PMU 与 v2 基线完全一致；
用户 CPI 仍为 0.575038，APE 仍为 42.778%。主机吞吐重新计时，不参与该一致性判断。

进一步按 core 比较时，gem5-FastSim CPI 缺口范围为 0.370120--0.476895，缺口与
wrong-path active ceiling 的 Pearson 相关系数为 -0.563。样本只有同一 workload 的八个
core，不能据此做总体统计推断，但方向上再次否定“按错路径暴露统一加周期”。

## 对生产优化的约束

1. oracle sidecar 不进入 FST、FastSim CLI 或正式精度输入；现有默认模型因此完全不变。
2. 固定 penalty 和匿名 branch shadow 已被 source 语义与独立 192-case gate 否决，不再
   重启同类调参。
3. 下一候选必须区分两层：trace 可见的 branch-resolution/queue pressure，以及只能用
   oracle 归因的错上下文 memory/translation 持久状态。
4. 所有可提升候选只能使用 portable FST 字段、已提交控制流历史和目标微架构参数，禁止
   workload ID、gem5 动态序号、错路径 PC/地址和 stage tick。
5. 候选先做 workload-held-out C8 验证，再重跑冻结的 SE 92-case。旧 SE 基线的 CPI P99
   为 C4 6.256%、C8 5.461%、C16 8.917%、C32 11.057%；任何核数回退到 12% 以上都
   不能提升为默认模型。

## 下一实施门

四个 v2 定向 pilot 和 Neutron v3 late-completion pilot 已完成。下一步不再实现全局
branch/cache 加法项。cache-tag oracle 只用于解释 L1D/L2 PMU；CPI 主线转向 committed
path 的 dependency、memory-response、ROB/IQ/LSQ backpressure 守恒审计。当前 Neutron
FastSim 的 179,473 个 response-critical cycles 中，145,746 来自 dependency、33,727
来自 memory response，二者守恒；还需找出 gem5 用户周期中未映射到这套 ledger 的部分。
只有能由 portable FST 和目标配置表达、并通过 workload-held-out 的候选才进入生产实现。
随后依次执行 C8 held-out gate、FS C4/C8 全集和 SE 92-case no-regression gate。正式精度
仍只报告未读取 oracle 的生产重放。

可复核产物为 `wrong-path-validation.{json,md}`、
`wrong-path-attribution.{json,md}` 和 `fastsim-baseline.json`。
