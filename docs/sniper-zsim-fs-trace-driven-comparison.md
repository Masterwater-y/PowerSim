# Sniper / Zsim 对齐 FastSim 微架构后的 FS Trace-driven 对比

> **勘误（2026-09-16）——本文第 6 节 FastSim 精度列已过时，请勿引用。**
>
> 本文（2026-08-10）用的 FastSim 配置是 `configs/gem5-v28_1-fs-user.cfg`
> （**v28.1、`measurement.scope=user`、FST v6 slice**）。此后维护入口已推进到
> v28.9（materialized-UOP 专用内核 v28.6、monotone-IQ v28.5、modeled-ifetch
> v28.4、load-ready v28.9、store-wait-relief v2 默认），且默认口径改为
> `user-plus-kernel` 的 native-kernel-trace。因此：
>
> - 第 6 节表中 **LBM 782 的 FastSim `+94.82%`（CPI 5.012552）不代表当前
>   FastSim**。它来自旧的 v28.1 + user-scope 快照，该行自身记录 FastSim
>   label-scope cycles 1,037,747,824 vs gem5 532,669,040（旧配置下确有近 2×
>   过估）。
> - 用**当前二进制 + 维护入口 `configs/gem5-fs-native-kernel.cfg`** 在现存
>   common-end 轨迹上实测：**LBM C8 = +0.93%**（CPI 3.231963 vs gem5
>   3.202265，abs 0.0297；冻结 guardrail 记录为 +1.85% / abs 0.0592）；
>   **LBM C32 ≈ +6.5%（relief on）/ +7.5%（relief off）**。均远低于 +94.82%。
> - 本文所用的**原始 v6 slice 输入已被清理**
>   （`/data00/yinhaolang/TCSim/logs/gem5-fs-roi/.../900cf530.../tao_trace/` 快照目录现为空），
>   因此本文这份矩阵**无法按原样字节级复现**。当前 common-end 轨迹是 v7、
>   native-kernel 口径；Zsim 旧 reader 只认 v6（已在本地放开 v6/v7），且三家在
>   内核记录上无法 bit-exact（Sniper/Zsim 无特权模型），需要单独口径决策后才能
>   刷新三方对比。
> - 吞吐相对结论（FastSim > Zsim > Sniper）方向大概率仍成立，但绝对值与精度对比
>   都应以当前二进制重跑为准。见下方第 0 节的当前二进制 40 案重跑。

## 0. 当前二进制 40 案重跑（2026-09-16，权威结果）

本节用**当前 FastSim 二进制**（`build/fastsim`，2026-09-15 构建，维护入口
`configs/gem5-fs-native-kernel.cfg` = v28.9 + store-wait-relief v2）在现存的
`first-core-common-end-20260911` 轨迹上，对 **10 个 workload × 4 个核数
（C4/C8/C16/C32）= 40 案** 重跑三方对比。它取代第 1、6、8 节的过时结论。

结果产物：`tmp/throughput-analysis-20260916/cmp-full-40/c{4,8,16,32}/summary.{csv,md}`。

### 0.1 复现口径与合同

- **输入**：三家消费同一份 v7 committed functional trace 的冷 slice；每案逐核
  UOP 完全一致（40/40 案 UOP 守恒 bit-exact，见 0.5）。
- **口径**：`--measurement-scope user-plus-kernel`。这批 common-end 轨迹内嵌
  privilege-tagged 内核记录；FastSim 走 native-kernel-trace 路径退休它们，
  Sniper/Zsim 以无特权模型重放同一批记录。因此**逐核 branch-miss 守恒门放宽为
  警告**（内核分支三家不同源，仅出现在含内核记录的核上；UOP 守恒仍严格）。
- **CPI 口径**：`uop_cpi = cores × max(每核 cycles) / 总 UOP`，gem5 基线用同一
  公式从 oracle（`cores × max(incl_cycles) / 总 user+kernel UOP`）重算，已验证与
  guardrail 的冻结 signed error 一致（LBM C8 +1.9% 对齐）。
- **本地改造（为可复现记录）**：Zsim reader 放开 FST v6/v7 版本门并重编
  `libzsim.so`（主记录布局 v6/v7 相同，仅版本号检查）；Sniper 用已支持 v7 的
  匹配 binary；为 C4/C16/C32 按 gem5 固定 8-slice LLC / 8-channel DRAM 生成缩放
  配置。串行 `--jobs 1`。

### 0.2 精度：FastSim 全面且系统性领先

40 案 CPI 误差聚合（绝对误差单位 = cycles per UOP，遵循
[`accuracy-reporting-contract.md`](accuracy-reporting-contract.md)）：

| 指标 | **FastSim** | Sniper | Zsim |
|---|---:|---:|---:|
| **CPI MAE** | **0.0634** | 0.5244 | 0.4393 |
| CPI MAPE（平均\|signed\|） | **7.26%** | 45.19% | 35.91% |
| 中位 \|abs err\| | **0.0508** | 0.2810 | 0.2595 |
| P90 \|abs err\| | **0.1471** | 2.0745 | 1.4200 |
| max \|abs err\| | **0.3809** | 2.5697 | 2.0202 |

- **FastSim MAE 比 Sniper 低 8.3×、比 Zsim 低 6.9×**，且尾部（P90/max）差距更大：
  FastSim 最坏单点 0.38 cyc/UOP，Sniper/Zsim 最坏到 2.0–2.6。
- **FastSim 在 37/40 案里绝对误差严格最小。**
- 误差符号：FastSim 双向（+14.7% 到 −17.9%，围绕 gem5 波动）；Sniper/Zsim
  **系统性单向低估**（几乎全为负），说明其访存/排队暴露不足，而非随机噪声。

### 0.3 逐核数 CPI MAE（cyc/UOP）

| cores | **FastSim** | Sniper | Zsim | Sniper/FS | Zsim/FS |
|---:|---:|---:|---:|---:|---:|
| C4 | **0.0412** | 0.4621 | 0.4067 | 11.2× | 9.9× |
| C8 | **0.0462** | 0.4700 | 0.4073 | 10.2× | 8.8× |
| C16 | **0.0724** | 0.5365 | 0.4600 | 7.4× | 6.4× |
| C32 | **0.0937** | 0.6288 | 0.4831 | 6.7× | 5.2× |

FastSim 误差随核数上升而略增（共享访存尾闭合是它的已知天花板），但**每一档都
比另两家低 5×–11×**。

### 0.4 内存密集尾负载：三家分野最明显

LBM（访存密集）是关键判据，也是旧文档 +94.82% 的来源。当前二进制：

| 负载 | 核数 | gem5 CPI | **FastSim** | Sniper | Zsim |
|---|---:|---:|---:|---:|---:|
| lbm | C4 | 2.5081 | **−0.1%** | −83.7% | −80.5% |
| lbm | C8 | 2.4735 | **+2.7%** | −83.9% | −79.2% |
| lbm | C16 | 2.7215 | **+8.6%** | −82.5% | −72.9% |
| lbm | C32 | 3.0070 | **+12.7%** | −85.5% | −47.2% |
| sph_exa | C16 | 3.1563 | **−4.7%** | −39.3% | −39.1% |
| zstd | C32 | 0.7106 | **+10.0%** | −52.6% | −48.5% |

**旧的 LBM `+94.82%` 被彻底证伪**：当前 FastSim 的 LBM 误差在 −0.1%～+12.7%
区间，而 Sniper/Zsim 仍严重低估内存尾（−47%～−86%）。这正印证旧文档"共享内存
服务顺序是决定性边界"的结论——只是 FastSim 那一列早已过时，当前 FastSim 恰恰
是三家里唯一把这条尾建准的。

### 0.5 守恒与吞吐

**UOP 守恒**（40/40 案逐核 bit-exact，三家完全相等）：

| cores | 总 UOP（三家相同） |
|---:|---:|
| C4 | 375,704,597 |
| C8 | 715,296,972 |
| C16 | 1,392,466,429 |
| C32 | 2,773,111,520 |

**吞吐**（模拟器 UOP/s 中位数，serial `jobs=1`）：

| cores | FastSim | Sniper | Zsim |
|---:|---:|---:|---:|
| C4 | 6.35M | 1.68M | 9.42M |
| C8 | 7.83M | 2.71M | 8.89M |
| C16 | 7.69M | 5.57M | 9.50M |
| C32 | 7.55M | 8.92M | 9.77M |

**吞吐结论随口径改变，须如实说明**：本次 `user-plus-kernel` 口径下，
**Zsim 中位吞吐反而略高于 FastSim**（各档 8.9–9.8M vs 6.3–7.8M），Sniper 仍最慢
（低核数 1.7M，到 C32 才追平）。这与旧文档第 8 节"FastSim 吞吐最高"不同，原因：
① FastSim 在 user-plus-kernel 下要额外处理内核记录；② 当前 v28.9 native-kernel
比旧 v28.1 做更多工作。因此当前定位是：**FastSim 以相当（低核数更高、高核数与
Zsim 同量级）的吞吐取得数倍于两者的精度**——优越性在"精度/吞吐"联合点，而非
单看吞吐。

### 0.6 微架构对齐核对（本轮复核）

四方（gem5 / FastSim / Sniper / Zsim）已逐项核对并对齐：核频率 3GHz、
fetch/decode/rename/dispatch/issue/commit 宽度 8、ROB 192、LQ/SQ 32/32、
L1I/L1D 32KB·8way、L2 1MB·8way·hit6、LLC 64MB·16way·hit12·8-bank、
DRAM 8ch·2rank·16bank·tCL/tRCD/tRP=43·burst10、tournament+BTB+RAS+indirect
分支预测器。**FU 延迟表本轮逐 op-class 复核一致**（int-mul 3、int-div 1 非流水、
fp-add 2、fp-mul 4、fp-mac 5、fp-div 12 非流水、fp-misc 3、fp-sqrt 24 非流水、
SIMD 1、system 1、load/atomic 4、store 1；FU 单元数 6/2/4/2/4/1/4/1），
三家完全相同。

**仍不同构**（实现范式差异，非参数未设对；见第 5 节）：IQ 抽象（gem5/FastSim
独立 64 项 vs Sniper 用 window=ROB）、Ruby CHA/NoC 片上网络、DRAM `open_adaptive`
FR-FCFS 调度与 bank-group calendar、DTLB timing walk、以及特权/内核模型。
Sniper/Zsim 在访存密集负载上的系统性低估，恰好落在这几项它们未建模的边界上。

因此本节支持"同一 functional 输入、主要 uarch 参数（含 FU 延迟表）匹配下的
实现对比"，结论为：**当前 FastSim 在精度上系统性优于 Sniper/Zsim（MAE 低
5×–11×、37/40 案最优、且是唯一建准内存尾的），并保持与两者相当量级的吞吐。**

---

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
| 782.lbm_r | 2.572910 | 5.012552 (+94.82%) ⚠️旧值,见顶部勘误 | 0.379735 (-85.24%) | 0.569558 (-77.86%) |
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
