# SPEC2026 微架构探索 native v28.2 结果（2026-09-03）

## 数据与口径

本批次包含 54 个独立采集的 FST：3 个 workload × C4/C8 × baseline 和 8 个
ROB/L1D/L2/LLC 单因素 profile。每个 case 以每核至少 10M user UOP 为停止目标，
同时记录同一 measurement 窗口的原生 CPL0 指令。54/54 个 case 均通过 gem5 最终
`config.ini`、oracle identity、ROI、FST privilege 和微架构参数对齐门禁。

- workload：`706.stockfish_r`、`731.astcenc_r`、`811.tealeaf_s`；
- trace scope：54/54 为 `user-plus-kernel`；
- user records：每核 10,000,000--10,000,745；全批次 3,240,001,246；
- user+kernel measurement records：3,256,300,437，其中 kernel records
  16,299,191；
- gem5/FastSim DRAM capacity：均为 3GiB；
- FastSim profile：`configs/gem5-v28_2-fs-native-kernel.cfg`，实效回读为
  `measurement.scope=user-plus-kernel`、`measurement.native_kernel_trace=true`；
- FastSim CPI：active user+kernel cycles / user UOP；idle cycles 不进入分子；
- PMU：user+active-kernel scope；cache 参考计数来自 TaoTrace native Ruby
  controller actions。

正式采集 54 路并发。单 case wall time 为 315--1,047 秒，中位数 711 秒；所有
采集均成功。正式数据约 221GiB。FastSim 54 路回放全部成功，单 case
12.5--28.1 秒。

## 汇总

| 指标 | 结果 | Gate | 状态 |
|---|---:|---:|---|
| 54-case CPI mean APE | 12.100% | — | diagnostic |
| 54-case CPI median / P90 / P99 | 13.067% / 16.053% / 18.001% | P99 ≤ 10% | FAIL |
| 48 variant CPI mean APE | 12.118% | — | diagnostic |
| Variant CPI P90 / P99 / max | 16.302% / 18.025% / 18.212% | P99 ≤ 10% | FAIL |
| Variant speedup error mean / P90 | 2.890% / 6.183% | P90 ≤ 10% | PASS |
| Material direction accuracy | 85.366% (35/41) | ≥ 90% | FAIL |
| Material CPI ranking accuracy | 86.387% (165/191) | ≥ 90% | FAIL |
| 参数激励覆盖 | 8/8 profile 达标 | 每 profile ≥ 2 cases | PASS |
| 最低回放吞吐 | 2.680M user UOP/s | ≥ 5M | FAIL |

总体 gate 为 **FAIL**。主要问题是绝对 CPI 偏差、ROB 变化方向、CPI pairwise 排序、
L1D miss 和 CHA lookup 口径/模型误差；不是 FST 缺失、采集失败或配置错位。

## 逐 workload CPI 误差

| workload | cases | mean APE | max APE |
|---|---:|---:|---:|
| `706.stockfish_r` | 18 | 9.512% | 13.305% |
| `731.astcenc_r` | 18 | 14.709% | 16.702% |
| `811.tealeaf_s` | 18 | 12.080% | 18.212% |

C4 的 27-case mean APE 为 11.847%，C8 为 12.353%。baseline 明细如下：

| workload | cores | gem5 CPI | FastSim CPI | APE |
|---|---:|---:|---:|---:|
| `706.stockfish_r` | 4 | 0.33531 | 0.35517 | 5.923% |
| `706.stockfish_r` | 8 | 0.31943 | 0.35917 | 12.443% |
| `731.astcenc_r` | 4 | 0.37886 | 0.31998 | 15.541% |
| `731.astcenc_r` | 8 | 0.36793 | 0.31594 | 14.129% |
| `811.tealeaf_s` | 4 | 0.66966 | 0.58706 | 12.335% |
| `811.tealeaf_s` | 8 | 0.42378 | 0.37556 | 11.378% |

## 逐参数 CPI 与 speedup 误差

| profile | CPI mean APE | CPI max APE | speedup mean error | speedup P90 |
|---|---:|---:|---:|---:|
| `baseline` | 11.958% | 15.541% | — | — |
| `rob96` | 12.447% | 16.702% | 1.997% | 3.741% |
| `rob256` | 13.336% | 17.814% | 3.127% | 6.409% |
| `l1d16k8` | 11.728% | 16.450% | 1.077% | 2.473% |
| `l1d64k8` | 12.120% | 18.212% | 3.670% | 6.125% |
| `l2_512k8` | 12.549% | 16.239% | 2.794% | 5.139% |
| `l2_2m8` | 10.485% | 15.481% | 3.407% | 8.714% |
| `llc32m` | 11.143% | 14.943% | 4.176% | 11.365% |
| `llc128m` | 13.136% | 17.228% | 2.873% | 5.569% |

Cache profile 的有效事件变化方向为 29/29 正确。ROB profile 的有效 CPI 变化方向
仅为 `rob96` 3/5、`rob256` 3/5，是总体方向 gate 未通过的主要来源。

## PMU 精度

| PMU | variant WAPE | pooled signed error | Gate/用途 |
|---|---:|---:|---|
| Branch misses | 1.196% | -0.463% | PASS（≤2%） |
| Private-L2 misses | 1.010% | -0.474% | PASS（≤2%） |
| L1D misses | 3.279% | +1.850% | FAIL（≤2%） |
| CHA/LLC lookups | 28.011% | +27.981% | FAIL（≤2%） |
| LLC tag misses vs Ruby | 0.397% | +0.397% | diagnostic |
| DRAM reads | 4.051% | -4.051% | diagnostic |
| Branch committed | <0.000001% | <0.000001% | diagnostic |
| DTLB accesses | 0% | 0% | diagnostic |
| DTLB misses | 44.184% | -44.184% | diagnostic |

CHA lookup 的大误差延续了旧批次中 FastSim protocol request 与 gem5 Ruby demand/tag
reference 的口径差异；LLC tag miss 对 Ruby miss 只有 0.397% WAPE，说明 LLC 数据
本身并非整体失真。后续应先统一 CHA lookup 事件语义，再决定它是否继续作为 strict
gate。

## CPI 排序

| workload/core | 正确 / material pairs | 准确率 |
|---|---:|---:|
| `706.stockfish_r` C4 | 27/34 | 79.412% |
| `706.stockfish_r` C8 | 31/34 | 91.176% |
| `731.astcenc_r` C4 | 25/29 | 86.207% |
| `731.astcenc_r` C8 | 28/28 | 100.000% |
| `811.tealeaf_s` C4 | 28/34 | 82.353% |
| `811.tealeaf_s` C8 | 26/32 | 81.250% |

全体 raw pairwise 排序为 177/216（81.944%）；过滤 gem5 CPI 差异小于 0.5% 的
pair 后为 165/191（86.387%）。

## 产物

- 原始 FST、oracle、labels：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/`；
- FastSim replay：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/fastsim-v28_2-native/`；
- 完整 JSON/CSV/Markdown 报告：
  `tmp/spec2026-uarch-exploration-v1-native-v28_2/evaluation-v28_2-native/`；
- 被删除的旧 user-only 批次摘要：
  `docs/spec2026-uarch-user-only-v28_1-v28_2-record-2026-09-03.md`。
