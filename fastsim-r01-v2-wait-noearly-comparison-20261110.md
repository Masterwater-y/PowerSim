# R0/R1 v2-wait 同输入对照与 prepare 退休边界检查

五个相同 FST，native `sprEarlyStorePrepare=false`、硬件预取关闭；所有 FastSim 版本保留 `legacy_prepare_timing=true`。新增运行的是改善过 Graph500 的 R0/R1 冻结版，不是当前完整二进制的 v2-wait 模式。pre-parent 与 current-v2-publish 复用此前正确配置的完成回放，本次重新评分。

## CPI

workload-task active cycles / retired macro instructions，common-start-v1 / retired-stream-completion-v1。身份验证 5/5，三个版本逐核宏人口与功能内容身份一致。绝对误差 AE = abs(FastSim CPI - gem5 CPI)，单位 cycles/macro。

| 负载 | gem5 CPI | pre-parent CPI | RE | AE | R0/R1 v2-wait CPI | RE | AE | 当前 publish CPI | RE | AE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| c04-777.zstd_r | 1.206049 | 1.136342 | -5.78% | 0.069708 | 1.142316 | -5.28% | 0.063733 | 2.713221 | +124.97% | 1.507172 |
| c04-801.xz_s | 0.575297 | 0.598726 | +4.07% | 0.023429 | 0.601359 | +4.53% | 0.026062 | 1.566361 | +172.27% | 0.991064 |
| c04-817.flac_s | 0.284584 | 0.319077 | +12.12% | 0.034493 | 0.318433 | +11.89% | 0.033849 | 0.438374 | +54.04% | 0.153790 |
| c04-854.graph500_s | 0.966422 | 0.754675 | -21.91% | 0.211747 | 0.835896 | -13.51% | 0.130526 | 0.699389 | -27.63% | 0.267033 |
| c04-872.marian_s | 2.364997 | 3.040902 | +28.58% | 0.675905 | 3.068823 | +29.76% | 0.703826 | 73.953462 | +3027.00% | 71.588465 |

| 版本 | CPI MAE (cycles/macro) | MAPE | P50 APE | P90 APE | P99 APE | CPI WAPE | 最大 APE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 11月9日 pre-parent 基线 | 0.203056 | +14.49% | +12.12% | +25.91% | +28.31% | +18.87% | +28.58% |
| R0/R1 冻结版 v2-wait | 0.191599 | +12.99% | +11.89% | +23.26% | +29.11% | +17.48% | +29.76% |
| 当前完整 v2-publish | 14.901505 | +681.18% | +124.97% | +1885.11% | +2912.81% | +1300.03% | +3027.00% |

MAE、MAPE 按五个负载等权；CPI WAPE 按同 scope 宏指令数加权周期误差，百分位 Type 7。并行运行的单次 wall time 只作运行回执，不是安静宿主串行配对吞吐认证。

## PMU

native PMU 缓存参考仅来自守恒 Ruby/SLICC 摘要；branch/L1/L2 为 proxy，LLC 为 diagnostic，未认证严格硬件 perf 等价。退休 load miss 使用独立参考；XZ core2、Marian core1 各有一条U-BOUNDARY unknown，三个主事件保留 N/A，仅 3/5 完整参考参与聚合。

| 版本 | 原生诊断事件 | 评分例数 | MAPE | WAPE | 最大 APE |
|---|---|---:|---:|---:|---:|
| 11月9日 pre-parent 基线 | branch_misses | 5/5 | +4.64% | +1.46% | +17.53% |
| 11月9日 pre-parent 基线 | l1d_tag_misses | 5/5 | +15.80% | +11.11% | +40.48% |
| 11月9日 pre-parent 基线 | private_l2_tag_misses | 5/5 | +13.40% | +6.08% | +33.70% |
| 11月9日 pre-parent 基线 | llc_tag_misses | 5/5 | +16.35% | +27.61% | +50.47% |
| R0/R1 冻结版 v2-wait | branch_misses | 5/5 | +4.64% | +1.46% | +17.53% |
| R0/R1 冻结版 v2-wait | l1d_tag_misses | 5/5 | +16.23% | +11.30% | +41.58% |
| R0/R1 冻结版 v2-wait | private_l2_tag_misses | 5/5 | +13.85% | +6.29% | +35.40% |
| R0/R1 冻结版 v2-wait | llc_tag_misses | 5/5 | +16.35% | +27.61% | +50.47% |
| 当前完整 v2-publish | branch_misses | 5/5 | +4.64% | +1.46% | +17.53% |
| 当前完整 v2-publish | l1d_tag_misses | 5/5 | +35.29% | +23.17% | +94.49% |
| 当前完整 v2-publish | private_l2_tag_misses | 5/5 | +33.99% | +26.61% | +62.74% |
| 当前完整 v2-publish | llc_tag_misses | 5/5 | +15.79% | +26.08% | +47.64% |

| 版本 | 退休 load miss | 完整参考 | MAPE | WAPE | 最大 APE |
|---|---|---:|---:|---:|---:|
| 11月9日 pre-parent 基线 | retired_l1_load_misses | 3/5 | +26.73% | +13.28% | +52.08% |
| 11月9日 pre-parent 基线 | retired_l2_load_misses | 3/5 | +39.70% | +10.88% | +109.07% |
| 11月9日 pre-parent 基线 | retired_l3_load_misses | 3/5 | +49.16% | +10.66% | +122.62% |
| R0/R1 冻结版 v2-wait | retired_l1_load_misses | 3/5 | +32.77% | +30.55% | +50.19% |
| R0/R1 冻结版 v2-wait | retired_l2_load_misses | 3/5 | +39.64% | +10.88% | +109.09% |
| R0/R1 冻结版 v2-wait | retired_l3_load_misses | 3/5 | +49.16% | +10.66% | +122.62% |
| 当前完整 v2-publish | retired_l1_load_misses | 3/5 | +54.67% | +21.29% | +98.31% |
| 当前完整 v2-publish | retired_l2_load_misses | 3/5 | +944.11% | +105.91% | +1843.03% |
| 当前完整 v2-publish | retired_l3_load_misses | 3/5 | +4271.78% | +107.35% | +11667.88% |

逐负载三版本 PMU 原始计数、相对误差、unknown 和来源见 CSV/JSON。重验所有既有原生审计绑定的源哈希，调用维护的比较 API 重新评分；未使用错误配置模型的预测作为 reference。

## prepare 检查

CPU 最终时序侧，普通 store 的 prepare admission = max(actual_retire + store_commit_to_iew_delay + 1, local_scan_ready)。本次 delay=1，至少在退休后 2 周期。LegacyPrepareTiming 的实际准入/容量延后从该时刻出发，mandatory store_send 同样不早于该门禁。

但共享/私有缓存的功能回放会更早执行 permission-only access，并按 canonical issue 建立父服务、占资源、推进 coherence/Tag 状态。当前 merge/publish 的服务记录 admission 来自 canonical selected_issue；CPU store_receipt 重算返回边并不会把这份共享记录、它的同线 load follower 或状态修改一起迁移到最终退休后的时间。

因此：未发现独立执行 native sprEarlyStorePrepare 的退休前扫描机制；已证明 CPU prepare/send 采用退休后门禁；仍不能宣称整个模型消除了退休前 prepare 的效果。退休输入只能证明指令最终退休，不能证明模型事务在退休后生效。

定向见证使用当前 build/libfastsim_lib.a、32 条退休功能记录：旧 load 阻塞退休，两个不同 cache set 的 store，后续 load 读第二个 store 同行的不同字节，避免 forwarding。默认 cache 时钟同域，原始 0 表示 audit 未选中共享事件，不能解释成 issue cycle=0。

| 模式 | 指令序号 | canonical issue | canonical response | actual retire | post-retire 下限 | store send | prepare permission | role |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2-wait | 2 | 0 | 0 | 251 | 253 | 253 | 397 | 1 |
| v2-wait | 4 | 0 | 0 | 252 | 254 | 501 | 398 | 3 |
| v2-wait | 6 | 6 | 8 | 252 | 254 | 0 | 0 | 0 |
| v2-merge | 2 | 0 | 0 | 215 | 217 | 217 | 461 | 1 |
| v2-merge | 4 | 0 | 0 | 216 | 218 | 463 | 462 | 3 |
| v2-merge | 6 | 6 | 268 | 268 | 270 | 0 | 0 | 0 |
| v2-publish | 2 | 0 | 0 | 215 | 217 | 217 | 461 | 1 |
| v2-publish | 4 | 0 | 0 | 216 | 218 | 463 | 462 | 3 |
| v2-publish | 6 | 6 | 268 | 268 | 270 | 0 | 0 | 0 |

使用现有 FASTSIM_TSO_TRACE 观察器独立读取 canonical permission-only 请求，补足上表 store 的共享事件 audit=0。共六条 TSOT，严格对应固定模式顺序与指令序号：

| 当前库模式 | store 序号 | canonical permission issue | canonical permission response | actual retire | retire后准入下限 | CPU最终permission |
|---|---:|---:|---:|---:|---:|---:|
| v2-wait | 2 | 5 | 261 | 251 | 253 | 397 |
| v2-wait | 4 | 6 | 265 | 252 | 254 | 398 |
| v2-merge | 2 | 5 | 261 | 215 | 217 | 461 |
| v2-merge | 4 | 6 | 265 | 216 | 218 | 462 |
| v2-publish | 2 | 5 | 261 | 215 | 217 | 461 |
| v2-publish | 4 | 6 | 265 | 216 | 218 | 462 |

当前 merge/publish 的 seq4：功能时钟 6 发起 permission-only 服务，但最终退休 216、退休后准入下限 218、CPU permission 462。同线 seq6 load 使用 canonical 服务并在 268 退休，早于 CPU permission 462。这构成提前服务可见性的具体见证；并非启用了 native 错误路径/未退休指令输入。

这只证明具体可见性/时钟缺口，不构成 Marian 巨大 CPI 回归的完整非重叠归因。当前二进制 Marian 的先前分层定位：off CPI 3.040902（AE 0.675905，RE +28.58%）；v2-wait 3.055769（AE 0.690772，RE +29.21%）；v2-merge 52.363298（AE 49.998301，RE +2114.10%）；v2-publish 73.953462（AE 71.588465，RE +3027.00%）。巨大回归在合并/所有权层进入，publication 进一步放大。

其他路径检查：buffer lifecycle prepare 的 own-canWB 下限同样为 retire+delay+1；reference StoreRequestEngine 要求 address-ready 与 canWB 后才可 enqueue/start prepare；bounded 非绑定估计以 retire+delay+1 为 eligible；该普通入口对非原子、单行、无 load 成员、无锁定/屏障/序列化 store 才判 eligible；page-walk/instruction-fetch 与 NO_ACCESS 有各自路径。software-prefetch 不属于普通退休 store prepare。当前 eligible 谓词没有直接检查全部 .sreq secure/uncacheable/HTM 特殊属性，本检查不能将这些类别一并认证为 native 等价。配置 legacy_prepare_timing=false 在旧 buffer 路径不代表关闭全部 permission-only 分类，不能用它表示关闭 native early prepare。

## 可复核产物

- [完整 JSON](fastsim-r01-v2-wait-noearly-comparison-20261110.json)
- [CPI CSV](fastsim-r01-v2-wait-noearly-comparison-20261110-cpi.csv)
- [native PMU CSV](fastsim-r01-v2-wait-noearly-comparison-20261110-native-pmu.csv)
- [退休 miss CSV](fastsim-r01-v2-wait-noearly-comparison-20261110-retired-load-misses.csv)
- [机制计数 CSV](fastsim-r01-v2-wait-noearly-comparison-20261110-mechanisms.csv)
- [冻结与运行回执](tmp/retired-prepare-on-previous-version-comparison-20261110/r01-wait-retired-prepare-on-gcc11/completion.json)
- [定向运行原始证据](tmp/prepare_retirement_witness_distinct_sets_20261110.json)
- [canonical prepare 原始 TSOT](tmp/prepare_retirement_witness_distinct_sets_20261110.log)
- [定向运行调用代码](tmp/prepare_retirement_witness_20261110.cpp)
- [CPU prepare 入口](src/simulator.cpp#L17880)
- [共享服务记录的 canonical admission](src/simulator.cpp#L27810)
- [准入边重算与 fallback](include/fastsim/legacy_prepare_timing.hpp#L133)

源码未作模型修改；二进制 SHA、配置等价检查、源审计哈希与逐核人口门禁见 JSON。
