# Sniper / Zsim 对齐 FastSim 微架构后的 FS Trace-driven 对比

## 1. 结论摘要

Sniper 和 Zsim 已从原先的 Westmere/Pentium-M/DDR3 近似改为尽可能匹配
FastSim 的目标配置，并重新完成 6 个 gem5-FS workload 的串行隔离实验。

正式结果目录：

`tmp/sniper-zsim-trace-driven/fs-c8-comparison-matched-uarch-v2-serial/`

关键结果：

- 完整矩阵为 3 个模拟器 × 6 个 workload，共 18 行；
- 每个模拟器均消费 **1,065,419,853 UOP**；
- 6 个 workload 的总 UOP、逐核 UOP、逐核 branch miss 三者完全一致；
- Sniper median absolute CPI error 从 44.88% 降至 38.99%；
- Zsim median absolute CPI error 从 158.53% 降至 27.28%；
- 串行隔离的 median simulator throughput：
  FastSim 11.59 M UOP/s、Sniper 3.25 M UOP/s、Zsim 8.13 M UOP/s。

“参数匹配”不等于实现完全同构。Sniper/Zsim 仍不能完整复现 Ruby CHA、
NoC、DTLB 和 `open_adaptive` DRAM 调度；这些残余边界在第 5 节列出。

## 2. 公平输入合同

三者只读取同一份 canonical FST v6 committed functional trace：

- PC、next PC、branch target/outcome/type；
- load/store/atomic、物理地址和访问大小；
- gem5 functional `op_class`；
- 最多 4 条 producer distance；
- macro/micro-op 边界和 serialize/syscall marker。

gem5 timing、cache、coherence 和 PMU label 仅在模拟结束后用于评分，不进入
任何模拟器的在线输入。FS ROI 使用同一组 `fastsim-binary-slice` manifest，
不复制原始 FST。

计分口径：

- `label_scope_cycles = 8 × max(per-core modeled cycles)`；
- `UOP CPI = label_scope_cycles / total UOP`；
- Sniper 使用 `performance_model.nonidle_elapsed_time` 转换出的每核模型周期，
  不把 trace 结束时的跨核同步 idle 时间计入 CPI；
- 吞吐正式值来自 `--jobs 1` 串行隔离运行。

## 3. 对齐后的公共目标

目标配置来自 `configs/gem5-v28_1-time-epoch.cfg`：

| 组件 | 对齐参数 |
|---|---|
| Core | 8 cores，3 GHz |
| Width | fetch/decode/rename/dispatch/issue/writeback/commit = 8 |
| Window | fetch queue 32，ROB 192，IQ 64，LQ 32，SQ 32 |
| Pipeline | fetch→decode 1，decode→rename 1，rename→dispatch 2，dispatch→issue 1 |
| FU count | integer 6，integer-multiply 2，FP-simple 4，FP-complex 2，SIMD 4，predicate 1，memory 4，system 1 |
| FU latency | int 1，imul 3，idiv 1 non-pipelined，FP 2/4/5/3/12/24，SIMD/predicate/system 1 |
| Branch | tournament 2048 local、8192 global/choice、BTB 4096×1、RAS 16、indirect 256×2、penalty 2 |
| L1 | L1I/L1D 32 KiB、8-way、64 B line，L1D hit 2 |
| L2 | private 1 MiB、8-way、hit 6 |
| LLC | shared 64 MiB、16-way、hit 12 |
| DRAM | 8 channels、2 ranks/channel、16 banks/rank、8 KiB row、tCL/tRCD/tRP 43、burst 10、controller 30+30 |

## 4. 实现改造

### 4.1 Sniper

主要实现位于：

- `tmp/sniper-isa-audit/sift/sift_reader.{h,cc}`
- `tmp/sniper-isa-audit/common/trace_frontend/trace_thread.{h,cc}`
- `tmp/sniper-isa-audit/common/performance_model/branch_predictors/tournament_branch_predictor.*`
- `tmp/sniper-isa-audit/common/performance_model/performance_models/core_model/*fastsim*`
- `tmp/sniper-isa-audit/common/performance_model/performance_models/rob_performance_model/rob_contention_fastsim.*`
- `tmp/sniper-isa-audit/common/performance_model/performance_models/interval_performance_model/interval_contention_fastsim.*`
- `tmp/sniper-isa-audit/config/fastsim-fs-c8.cfg`

关键变化：

- 一条 FST record 生成一个 dynamic micro-op；
- producer distance 直接注入 ROB dependency；
- 新增 `fastsim` core model，直接读取原始 `op_class`，不再伪装成 Nehalem
  XED opcode；
- FU pool、lane 数、延迟和非流水化占用与 FastSim 使用同一映射；
- 新增 tournament + BTB + RAS + indirect predictor；
- load 使用 `max(minimum_load_latency, cache response)`，避免重复收费；
- ROB 192、RS/IQ 64、LQ/SQ 32、dispatch/issue/commit 8；
- 详细 DDR 使用 8 channel、2 rank、16 bank、4 bank group、8 KiB row 和
  43/43/43-cycle 主时序；
- EOF 排空 ROB，physical address 直接进入 cache hierarchy。

由于原 `lib/sniper` 在托管会话中不可覆盖，本次链接出的匹配 binary 保存在：

`tmp/sniper-zsim-trace-driven/sniper-matched-build/sniper`

### 4.2 Zsim

主要实现位于：

- `/data00/yinhaolang/Zsim/zsim/src/fastsim_trace_driver.{h,cpp}`
- `/data00/yinhaolang/Zsim/zsim/src/fastsim_branch_predictor.{h,cpp}`
- `/data00/yinhaolang/Zsim/zsim/src/ooo_core.{h,cpp}`
- `/data00/yinhaolang/Zsim/zsim/src/ddr_mem.{h,cpp}`
- `/data00/yinhaolang/Zsim/zsim/src/contention_sim.{h,cpp}`
- `/data00/yinhaolang/Zsim/zsim/tests/fastsim-fs-c8.template.cfg`

关键变化：

- 保留原 OOO/cache/coherence/DRAM bound-weave 路径，不使用 cache-only replay；
- ROB 128→192，issue window 36→64，fetch queue 28→32；
- issue/retire/LQ/SQ width 4→8，source slots 2→4；
- 端口掩码扩展到 32 bit，显式表示 FastSim 的 24 个 FU lane；
- FST frontend 每 cycle 处理最多 8 UOP，taken branch 结束 fetch group；
- 新增同参数 tournament + BTB + RAS + indirect predictor；
- 普通 store 核心完成为 1 cycle，load/atomic 使用 minimum 4-cycle；
- 新增 `FastSim-3GHz` DRAM timing，直接以核心周期解释 43/43/43 和 burst 10；
- 8 channel、2 rank/channel、16 bank/rank、8 KiB row；
- 离线 FST 模式使用单线程 inline contention weave，避免等待未启动的 Pin
  internal worker。

## 5. 残余不可同构项

Sniper：

- ROB abstraction 不分别暴露 FastSim 的 fetch queue、rename、writeback 等全部
  stage calendar；
- shared LLC controller + parametric MSI + mesh，不是 8 个 Ruby CHA 和固定
  5-cycle one-way NoC；
- DDR 主拓扑和时序已对齐，但不实现 FastSim 的 `open_adaptive` FR-FCFS
  selection policy；
- 不对 FST virtual-page token 执行 timing DTLB walk。

Zsim：

- 没有独立 writeback-width calendar；
- coherence/cache path 不是 gem5 Ruby MESI_Three_Level；
- DDR 主拓扑和时序已对齐，但没有 FastSim 的 4 bank groups/rank calendar；
- 不对 FST virtual-page token 执行 timing DTLB walk。

两者均只消费 committed functional trace，不制造错误路径 UOP。FastSim 当前
目标配置的 `branch.shadow_rob=false`，因此本次也没有为对照模拟器虚构
wrong-path 地址或占用。

## 6. 串行完整结果

| Workload | gem5 UOP CPI | FastSim | Sniper | Zsim |
|---|---:|---:|---:|---:|
| 706.stockfish_r | 0.228176 | 0.228126 (-0.02%) | 0.210246 (-7.86%) | 0.229901 (+0.76%) |
| 710.omnetpp_r | 0.378947 | 0.345567 (-8.81%) | 0.240514 (-36.53%) | 0.306882 (-19.02%) |
| 777.zstd_r | 0.530796 | 0.538688 (+1.49%) | 0.279348 (-47.37%) | 0.325334 (-38.71%) |
| 782.lbm_r | 2.572910 | 5.012552 (+94.82%) | 0.379735 (-85.24%) | 0.569558 (-77.86%) |
| 811.tealeaf_s | 0.379649 | 0.327203 (-13.81%) | 0.222318 (-41.44%) | 0.270447 (-28.76%) |
| 854.graph500_s | 1.022196 | 0.991820 (-2.97%) | 0.727377 (-28.84%) | 0.758582 (-25.79%) |

聚合 absolute CPI error：

| Simulator | Mean | Median | P90 | Max |
|---|---:|---:|---:|---:|
| FastSim | 20.32% | **5.89%** | 54.32% | 94.82% |
| Sniper | 41.21% | **38.99%** | 66.31% | 85.24% |
| Zsim | 31.82% | **27.28%** | 58.29% | 77.86% |

FastSim 的 mean/P90/max 仍被 LBM 的 +94.82% 硬失败拉高，不能只报告
median。参数对齐后 Zsim 的整体误差显著下降，但 LBM 仍严重低估 memory
tail；Sniper 同样在 LBM 上缺少足够的共享内存排队暴露。

## 7. 与旧非匹配矩阵的变化

| Simulator | 旧 Median | 匹配后 Median | 变化 |
|---|---:|---:|---:|
| Sniper | 44.88% | 38.99% | -5.90 percentage points |
| Zsim | 158.53% | 27.28% | -131.26 percentage points |

这不是所有 workload 都单调改善：

- Sniper 在 Stockfish、OMNeT++ 显著改善，但 Zstd、LBM、TeaLeaf、Graph500
  退化；旧配置在这些 workload 上存在误差抵消；
- Zsim 在 5 个 workload 上显著改善，但 LBM absolute error 从 34.62%
  增至 77.86%，说明剩余问题主要位于 coherence/NoC/DRAM scheduling，而
  不是 core width 或 branch predictor。

因此不能用单一 workload 的接近程度证明微架构等价。

## 8. 串行吞吐

Simulator-only throughput：

| Simulator | Minimum | Median |
|---|---:|---:|
| FastSim | 6.00 M UOP/s | **11.59 M UOP/s** |
| Sniper | 1.17 M UOP/s | **3.25 M UOP/s** |
| Zsim | 6.76 M UOP/s | **8.13 M UOP/s** |

FastSim median throughput 是 Sniper 的 **3.57×**、Zsim 的 **1.43×**；
Zsim median 是 Sniper 的 **2.50×**。

吞吐不含 gem5 trace 采集，但包括 FST slice 前缀扫描、模拟器启动和输出。
`fs-c8-comparison-matched-uarch-v1/` 是 `--jobs 6` 并行守恒审计矩阵，
准确性接近但吞吐受资源争用影响；正式吞吐必须使用本节的串行 v2 结果。

## 9. 守恒与复现

正式命令：

```bash
python3 tools/compare_fs_trace_driven.py \
  --jobs 1 \
  --output tmp/sniper-zsim-trace-driven/fs-c8-comparison-matched-uarch-v2-serial
```

定向 smoke：

```bash
python3 tools/compare_fs_trace_driven.py \
  --simulators fastsim sniper zsim \
  --workloads 706.stockfish_r \
  --limit-instructions-per-core 10000 \
  --jobs 1 \
  --output tmp/sniper-zsim-trace-driven/fs-c8-matched-uarch-triplet-smoke-v3
```

机器可读证据：

- `summary.csv`：18 行逐案结果；
- `summary.json`：聚合指标、逐核 UOP、逐核 branch miss；
- `run-manifest.json`：binary/config/tool hash 和 resolved case config hash；
- `cases/<workload>/manifest.txt`：实际 FST slice；
- 每个模拟器的 command、stdout/stderr、stats 和 wall time。

完整矩阵门禁：

- 总 UOP：三者均为 **1,065,419,853**；
- 总 branch miss：三者均为 **1,521,148**；
- 6/6 workload 的逐核 UOP 完全一致；
- 6/6 workload 的逐核 branch miss 完全一致；
- 没有 panic、assert、segmentation fault 或 fatal；
- Sniper stderr 只有预期的缺失 VMA warning；本实验禁用 translation，并直接
  使用 FST physical address；
- 根 `/tmp` 无本任务产物，所有任务文件在项目 `tmp/` 下。

## 10. 验证状态

- FastSim：`cmake --build build -- -j16` 通过；
- FastSim：`./build/fastsim_tests` 输出 `all FastSim tests passed`；
- Sniper：FST reader、common objects 和 standalone matched binary 构建通过；
- Zsim：`scons -j16` 在 `-Werror` 下通过；
- triplet smoke：per-core UOP 和 branch miss 双门禁通过；
- 并行完整矩阵：6/6 case 通过；
- 串行正式矩阵：6/6 case 通过；
- Graph500 Sniper 重复跑：CPI 差约 0.033%，UOP/branch 完全一致。

## 11. 最终判断

Sniper 和 Zsim 已不再使用旧的 Westmere/Pentium-M/DDR3 配置进行比较；
core width/window、FU、branch predictor、cache 和 DRAM 主拓扑/时序均已换成
FastSim 目标参数。

匹配后 Zsim 精度提升非常明显，证明旧结果的大部分误差来自核心和 DRAM
配置失配；但 LBM 仍同时击穿 FastSim、Sniper 和 Zsim，且三者误差方向不同，
说明共享内存服务顺序与长期排队仍是决定性边界。当前结果支持“同一 functional
input、主要微架构参数匹配后的实现对比”，不支持“Sniper/Zsim 与 FastSim
内部状态机完全同构”的更强声明。
