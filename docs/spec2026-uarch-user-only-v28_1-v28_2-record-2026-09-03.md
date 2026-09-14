# SPEC2026 uarch user-only 旧批次结果归档（2026-09-03）

## 结论与口径

本文归档删除 bulk FST 前的 54-case 结果。矩阵为 3 workload × C4/C8 ×
9 uarch profile，每核 measurement ROI 为 10M user UOP records。三个 workload
为 `706.stockfish_r`、`731.astcenc_r` 和 `811.tealeaf_s`；uarch profile 为
baseline 以及 ROB/L1D/L2/LLC 的 8 个 OFAT 端点。

这批 FST 的 `trace_scope=user`，不含 privilege-tagged CPL0 records。因此：

- v28.1 结果是 scope-aligned user-only 结果；
- v28.2 结果只是在同一 user trace 上启用 v28.2 fetch/StoreSet timing
  修复的 counterfactual，运行时显式设置 `measurement.scope=user` 和
  `measurement.native_kernel_trace=false`；
- 直接用原生 `gem5-v28_2-fs-native-kernel.cfg` 运行时 54/54 都被
  FastSim fail-closed，错误为 `measurement.native_kernel_trace=true but the measured
  FST region contains no privilege-tagged kernel records`。这证明旧 FST 不能冒充
  native 数据。

## 汇总结果

| 指标 | v28.1 user-only | v28.2 timing on user trace | 变化 |
|---|---:|---:|---:|
| 54-case CPI mean APE | 14.208% | 12.337% | -1.871 pp |
| 54-case CPI P99 APE | 21.357% | 17.325% | -4.032 pp |
| 48 variant CPI mean APE | 14.207% | 12.346% | -1.860 pp |
| 48 variant CPI P99 APE | 21.377% | 17.350% | -4.027 pp |
| speedup error P90 | 7.159% | 6.321% | -0.838 pp |
| material direction accuracy | 87.50% | 87.50% | 0 |
| material CPI ranking accuracy | 80.33% | 79.23% | -1.09 pp |
| minimum throughput | 1.742M UOP/s | 2.092M UOP/s | +0.350M UOP/s |

v28.2 timing 对 Stockfish 有效，但没有将这个高误差子集恢复到历史全
10-workload 集合约 6% 的均值：

| workload | v28.1 variant mean APE | v28.2 counterfactual | 变化 |
|---|---:|---:|---:|
| `706.stockfish_r` | 15.740% | 10.876% | -4.864 pp |
| `731.astcenc_r` | 15.205% | 14.414% | -0.792 pp |
| `811.tealeaf_s` | 11.674% | 11.749% | +0.075 pp |

baseline 的 Stockfish C4 从 19.445% 改善到 8.188%，而 C8 从 12.259%
回退到 13.692%。v28.2 的 StoreSet 和 fetch-response ledger 在实效配置中均已
开启；Stockfish C4 记录到 3,121,224 条 reconstructed same-PC StoreSet edges
和 3,932,159 ready-extension cycles。

## PMU 和排序

| strict PMU WAPE | v28.1 | v28.2 counterfactual |
|---|---:|---:|
| L1D misses | 3.330% | 3.330% |
| private-L2 misses | 1.363% | 1.351% |
| CHA/LLC lookups | 27.975% | 27.983% |
| branch misses | 1.458% | 1.458% |

v28.2 几乎不改变 functional PMU population，说明 CPI 改善来自 timing 状态，
不是调整 cache/branch 事件数。但 material ranking 从 147/183 降到
145/183，主要是 Stockfish C4 从 25/32 降到 22/32；Stockfish C8 则从
31/34 升到 33/34。

## 旧数据的已知合同问题

1. 54 个 case 中有 30 个存在 `fetch_supply_static_span_unavailable`。原因是
   一个 core stream 中观测到多个 CR3/address space 时，PC-only `.imap` 会被
   生产端正确地抑制。baseline 的 Stockfish C4/C8、Astcenc C8 和 TeaLeaf C4
   不可用 lookup 比例分别为 39.1%/17.5%/14.2%/47.1%。
2. 旧 alignment gate 校验了 core count、ROB、cache size/assoc、CHA 和 DRAM
   channel，但没有校验 measurement/native scope、profile identity、`.imap` 可用性和
   DRAM capacity。gem5 当时为 3GiB，FastSim 实效值为 4GiB。
3. 这批数据使用了三个历史上就偏难的 workload，不能把它的均值与
   含大量 2%--7% 低误差项的旧 10-workload 均值直接比较。

## 归档与删除

删除前 bulk 目录占用约 217GiB，smoke 占用约 24GiB，checkpoint 占用约
8.8GiB。上述 bulk FST、FastSim JSON 和临时 report 按用户要求删除，本文为该
user-only 批次的持久摘要。后续正式数据必须使用 privilege-tagged
`user-plus-kernel` FST，v28.2 native profile 不允许再被命令行静默覆盖为
user-only。
