# 当前负载与 CPI / PMU 复验（2026-10-20）

后续 SQLite 已按用户要求使用当前 gem5 重建 checkpoint 并重采，最新结果见 [SQLite NO_ACCESS 重采集与验证](sqlite-noaccess-recapture-results-20261020.md)。本报告的 SQLite 数字保留为重采前旧源结果，不代表新窗口。

本轮使用当前生产 FastSim 的冻结副本重新运行维护 validator，没有修改模型、配置、trace 或参考计数。当前可直接进入完整验证的标准 SPEC rate 来源为 SQLite 与 Zstd；两案均为四核、四个单线程 rate 副本、八通道、buffer TSO 114。

## 1. 当前有哪些负载

主工作负载目录有 Stockfish、OMNeT++、Zstd、LBM、SPH-EXA、TeaLeaf、NAB、Graph500、NAMD、Neutron 10 项，另有已准备的 SQLite。另存的完整 SPEC 安装共有 54 个 CPU 子目录，包含 rate/speed 及 specrand；目录存在不代表已编译、采集或验证。

| 来源 | 当前 FST | 本轮状态 |
|---|---:|---|
| SQLite rate | 4 条 / 3.037 GB | 本轮重新回放，来源/人口/native 守恒检查通过 |
| Zstd rate | 4 条 / 3.100 GB | 本轮重新回放，来源/人口/native 守恒检查通过 |
| 历史 LBM C4 八通道 | 0 条 / 0.000 GB | FST 已清理，保留历史报告；不能报告当前模型重放精度 |
| 历史 Graph500 C4 八通道 | 0 条 / 0.000 GB | FST 已清理，保留历史报告；不能报告当前模型重放精度 |
| 旧 TeaLeaf 正式窗口 | 0 条 / 0.000 GB | FST 已清理，保留历史报告；不能报告当前模型重放精度 |
| 另存 TeaLeaf 小窗口 | 4 条 / 0.128 GB | 缺 request/manifest/trace 元数据、有效目标或回放配置等材料，未纳入精度集 |
| ALS process 诊断窗口 | 4 条 / 1.225 GB | 缺 request/manifest/trace 元数据、有效目标或回放配置等材料，未纳入精度集 |

小探针、恢复 smoke、旧 Zstd 窗口及同负载的多次诊断不是新的独立负载。本轮不以历史误差替代当前结果，不重采已清理负载，不将不完整小窗口强行补成合格来源。

## 2. CPI 与 elapsed

统计范围为 recorded-owner workload-task active cycles / task macro instructions；双方指令数量匹配。相对误差带符号；绝对误差与 MAE 单位为 cycles/宏指令。

| 负载 | task 宏指令数 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|
| sqlite C4 | 23,635,778 | 1.000286 | 0.772986 | -22.72% | 0.227301 |
| zstd C4 | 21,067,990 | 1.472610 | 1.342789 | -8.82% | 0.129822 |

| 汇总 | 案例数 | CPI MAPE | CPI MAE cycles/宏指令 |
|---|---:|---:|---:|
| 当前两案，配置等权 | 2 | 15.77% | 0.178561 |

| 负载 | gem5 elapsed ms | FastSim elapsed ms | elapsed 相对误差 |
|---|---:|---:|---:|
| sqlite C4 | 1.968546 | 1.521277 | -22.72% |
| zstd C4 | 2.584096 | 2.356146 | -8.82% |

Elapsed 为目标系统经过时间，不求四核之和，也不等于宿主回放或独立审计耗时。两案规模不大，不能由两案均值推广到完整 SPEC 集。

## 3. 退休 load cache PMU 主指标

**Incomplete-evidence errors：以下误差比较 native 已观察子集与模型估计，尚无硬件 perf 等价认证。** unknown 不当作零，也不一律补成 miss；各事件的缺口分别保留。

| 负载 | 退休事件 | gem5 已观察 | FastSim 估计 | 暂定 APE | gem5 unknown | FastSim unknown |
|---|---|---:|---:|---:|---:|---:|
| sqlite | L1 load hit | 6,545,430 | 4,564,473 | 30.26% | 581,748 | 2,567,529 |
| sqlite | L1 load miss | 71,979 | 74,513 | 3.52% | 581,748 | 2,567,543 |
| sqlite | L2 load hit | 646 | 706 | 9.29% | 589,108 | 2,567,543 |
| sqlite | L2 load miss | 71,330 | 73,808 | 3.47% | 589,108 | 2,567,543 |
| sqlite | L3 load hit | 176 | 184 | 4.55% | 589,122 | 2,567,564 |
| sqlite | L3 load miss | 71,140 | 73,603 | 3.46% | 589,122 | 2,567,564 |
| sqlite | FB hit | 7,357 | 0 | 100.00% | 581,748 | 2,567,543 |
| sqlite | loads | 7,206,439 | 7,206,439 | 0.00% | 8 | 0 |
| sqlite | stores | 3,106,645 | 3,106,645 | 0.00% | 8 | 0 |
| zstd | L1 load hit | 5,434,614 | 5,098,733 | 6.18% | 237,924 | 549,691 |
| zstd | L1 load miss | 585,581 | 714,689 | 22.05% | 237,928 | 549,805 |
| zstd | L2 load hit | 432,668 | 538,533 | 24.47% | 349,388 | 549,805 |
| zstd | L2 load miss | 154,351 | 177,596 | 15.06% | 362,223 | 549,817 |
| zstd | L3 load hit | 124,620 | 132,161 | 6.05% | 368,408 | 550,024 |
| zstd | L3 load miss | 29,424 | 45,234 | 53.73% | 362,565 | 550,024 |
| zstd | FB hit | 123,891 | 0 | 100.00% | 237,928 | 549,817 |
| zstd | loads | 6,322,869 | 6,322,869 | 0.00% | 170,982 | 0 |
| zstd | stores | 3,537,857 | 3,537,857 | 0.00% | 93 | 0 |

主要缺口包括 native 退休/服务成员分类、ISA/event qualification 未完全验证，以及错误路径/预取对 cache 状态的间接影响未完整建模。计数相同也不能取消 unknown 或升级成严格真机事件认证。

两案退休事件的暂定 MAPE、WAPE 与 max APE 由维护工具汇总，见 [retired-cache-summary.md](../tmp/workload-accuracy-20261020/aggregate/retired-cache-summary.md)。MAPE 对配置等权，WAPE 以原始参考计数加权，不混用。

## 4. 独立 native tag-miss / branch 诊断

Cache 参考仅使用维护 validator 审计的 conserved Ruby/SLICC native summaries，不使用 path-class cache 字段。这些事件与上节退休 load 事件口径不同。

| 负载 | 诊断 | gem5 | FastSim | APE |
|---|---|---:|---:|---:|
| sqlite | branch_misses | 136,980 | 137,008 | 0.02% |
| sqlite | l1d_tag_misses | 73,095 | 74,973 | 2.57% |
| sqlite | llc_tag_misses | 71,702 | 73,603 | 2.65% |
| sqlite | private_l2_tag_misses | 71,905 | 74,197 | 3.19% |
| zstd | branch_misses | 185,279 | 184,867 | 0.22% |
| zstd | l1d_tag_misses | 791,558 | 779,015 | 1.58% |
| zstd | llc_tag_misses | 33,775 | 46,408 | 37.40% |
| zstd | private_l2_tag_misses | 170,779 | 199,812 | 17.00% |

| 诊断 | 有限 APE 案例 | MAPE | WAPE | max APE |
|---|---:|---:|---:|---:|
| branch_misses | 2/2 | 0.12% | 0.14% | 0.22% |
| l1d_tag_misses | 2/2 | 2.08% | 1.67% | 2.57% |
| private_l2_tag_misses | 2/2 | 10.09% | 12.91% | 17.00% |
| llc_tag_misses | 2/2 | 20.03% | 13.78% | 37.40% |

## 5. 结果边界与复现

- Zstd 使用 10 月 20 日新源，四核均有 `.mreq`，保留 NO_ACCESS 功能语义。canWB/扫描仍为响应侧估计，严格 cache 准入扫描及 buffer/PTE 组合未完整实现。
- SQLite 是 10 月 16 日旧源，没有 `.mreq`，请求角色保持未知。本轮结果验证当前模型对该旧输入的预测，不能认证 NO_ACCESS 修复在 SQLite 上的效果。
- 两案硬件身份、指令人口与 native 守恒均通过；`passed` 不等于精度阈值达标，也不等于严格硬件 PMU 认证。
- 本轮没有新的吞吐结论，未修改模型或延迟参数。
- FST 总量 21,592,453,664 bytes（21.59 GB），剩余磁盘 430.62 GB；未新增或删除 FST。

冻结生产二进制 SHA256：`3478db94c82d581df4034f7a5c2da1aad0737c93f11cc988226dc3a5c05d125d`。

证据：[运行配置与二进制身份](../tmp/workload-accuracy-20261020/run-manifest.json)、[命令](../tmp/workload-accuracy-20261020/commands.json)、[硬件身份](../tmp/workload-accuracy-20261020/identity.json)、[盘点](../tmp/workload-accuracy-20261020/inventory.json)、[两案维护汇总](../tmp/workload-accuracy-20261020/aggregate/summary.json)、[SQLite 验证](../tmp/workload-accuracy-20261020/validation-sqlite/cases/c04-708.sqlite_r/validation.json)、[Zstd 验证](../tmp/workload-accuracy-20261020/validation-zstd/cases/c04-777.zstd_r/validation.json)。
