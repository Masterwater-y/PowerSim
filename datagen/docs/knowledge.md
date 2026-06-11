我们的方案：https://bytedance.larkoffice.com/docx/IjnOd5nNxop3HYxq7KTcpfRKnIe

TAO论文：https://bytedance.larkoffice.com/docx/S6GldLqeOof38dxKsx8cWKULnR2

LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH} scons build/X86_MESI_Three_Level/gem5.opt PROTOCOL=MESI_Three_Level -j 96

---

## 物理机 vs gem5 仿真放大倍数（2026-06-01 实测）

### 物理机原生 ROI 时间（5 个 W11–W15 workload，nthreads=4）

> 测试机：4 物理核可用。perf stat r=5 测 W11，其余用 200 次循环求平均值（含 fork/exec，物理 ROI 实际更短）。
> 全量 args 与 `scripts/run_w11_w15_10m_experiment.sh` 完全一致；环境变量 `TAO_DISABLE_M5=1` 跳过 m5 ROI 钩子。

| Workload         | args（nthreads=4） | 物理机 wall (ROI) | gem5 + 下游 wall（status.tsv）| 放大倍数（wall/wall） |
|------------------|--------------------|-------------------|--------------------------------|------------------------|
| W11_stream_mix   | `4 47 256 1 11`    | ~8.7 ms (perf)    | 82 min                         | **~5.7 × 10⁵**         |
| W12_stencil2d    | `4 11 256 1 12`    | ~4 ms             | 64 min                         | **~9.6 × 10⁵**         |
| W13_graph_walk   | `4 1 640 1 13`     | ~14 ms            | 82 min                         | **~3.5 × 10⁵**         |
| W14_branch_state | `4 11 64 1 14`     | ~10 ms            | 82 min                         | **~4.9 × 10⁵**         |
| W15_indirect     | `4 11 256 1 15`    | ~9 ms             | 80 min                         | **~5.3 × 10⁵**         |

### gem5 内部口径（W11 stats.txt 末段累计）

| 指标             | 值                |
|------------------|-------------------|
| simSeconds       | 0.000800 s        |
| simInsts (4core) | 10,628,384        |
| hostSeconds      | 499.0 s ≈ 8.3 min |
| hostInstRate     | 21,300 inst/s     |
| simSec / hostSec | **~6.2 × 10⁵**    |

> 三种"慢化"口径互相校验：wall/wall ≈ 5.7×10⁵，simSec/hostSec ≈ 6.2×10⁵，
> instr 吞吐比 ≈ 5.7×10⁵ — 量级一致。
> gem5 自身仅占单 workload 80 min 中约 8 min；剩余 ~70 min 是 dump events
> + merge + mesi_ref_sim + sample/pack/dedup 等收尾阶段，与 simInsts 同阶。

### nthreads / num-cores 对仿真时间的影响

读 [pthread_harness.c](file://MTAO/taogen/workloads/common/pthread_harness.c)：

- 总指令数 = `iters × total_items`（与 `nthreads` 无关），nthreads 只影响 work 切分。
- 物理机：nthreads ≤ 物理核数时近似线性加速；超过物理核后由于 oversubscription，wall 反升。
- **gem5 内部 host 是单线程仿真器**（每个 simulated CPU 串行交错驱动）：
  - num-cores ↑ → simInsts 守恒，但仿真目标 cycles 放大 → **hostSeconds 大致与 num-cores 线性增长**。
  - nthreads > num-cores 时 OS 调度开销额外引入 simInsts ↑，等价于 hostSeconds 多增。
- 提高 `nthreads + num-cores`（同步抬高）：simInsts 守恒，但**coherence/false-sharing/migratory line/inv-fanout 多样性显著提升**（对训练 _MemCoh / _ISide 友好），代价是 wall 几乎线性放大。
- 推荐位：保持 4×4 是当前最稳；若需丰富 coherence 样本，建议 6×6 或 8×8 同步抬高，并把 dedup target 按比例下调。

---

## TAO 论文 (Pandey et al., POMACS 2024) 数据集规模阅读笔记（2026-06-01）

来源：[3656012.txt](file://MTAO/taogen/docs/3656012.txt) §5 EVALUATION（Page 14–15）。

### Benchmarks 与 ISA

- 用 SPEC CPU2017 整套作为 benchmark suite；ISA 是 **ARM**（不是 x86）。
- 处理器模型：gem5 **O3CPU**（detailed）+ **AtomicSimpleCPU**（functional）。
- 训练集 4 个：`531.deepsjeng_r / 654.roms_s / 544.nab_r / 641.leela_s`。
- 测试集 4 个：`605.mcf_s / 523.xalancbmk_r / 621.wrf_s / 507.cactuBSSN_r`。
- 训练 / 测试集均按论文 [53] 的"代表性 benchmark"分组（按 µArch 性能差异聚类），不是随机划分。

### 训练集采集协议（关键）

> 原文 §5 EVALUATION：
> _"To construct the training dataset, we first generate detailed and functional traces with **100 million instructions** from each training benchmark with default test workloads using the gem5 O3CPU and AtomicSimpleCPU model, respectively. Of note, **we skip the first 100 million instructions** as adopted by earlier projects to avoid the common program initialization phase."_

- **每个训练 benchmark 仅采 100M instructions**，并 fast-forward 跳过前 100M（避开程序初始化）。
- 4 个训练 benchmark × 100M = 4 亿原始 instructions；去重后**得到约 180M instructions** 作为最终训练集（去重前 detailed trace 还包含 squashed/nop，inflation 比 functional 多 ~5%，见原文 Table 1）。
- 测试阶段：每个 test benchmark 同样 100M instructions（仅 functional trace），用于 throughput / accuracy 评测。
- **没有用 SimPoint** 选 representative slice，只是简单 "skip 100M + take 100M"。

### 是否完整仿真整个 SPEC2017 benchmark？

**没有**。论文明确说 SPEC2017 是用 "skip 100M + take 100M" 的 fast-forward + 固定窗口策略截取的，远不是跑到 benchmark 退出：

| 维度                                                     | 数值                                  |
|----------------------------------------------------------|---------------------------------------|
| 单 benchmark 采集量（已用）                              | 100M (skip) + 100M (collect) = 200M   |
| 单 SPEC2017 完整 reference run 完整动态指令数（典型量级）| **数千亿至数万亿**（10¹¹–10¹³）       |
| TAO 采集占比                                             | **~0.001%–0.1%** 的完整 trace         |

> SPEC CPU2017 各 benchmark 在 reference input 下完整动态指令数（业界经验 / SPEC 官方报告量级）：
>
> - 偏轻量者：500.perlbench_r / 519.lbm_r / 538.imagick_r ≈ 几千亿（×10¹¹）
> - 中量者：520.omnetpp_r / 525.x264_r / 526.blender_r ≈ 1–5 万亿（×10¹²）
> - 偏重者：549.fotonik3d_r / 627.cam4_s / 654.roms_s ≈ 5–30 万亿（×10¹³）
> - 全套 reference 总动态指令数 ≈ **数十万亿至 10¹⁴ 级**
>
> 用 gem5 O3CPU 完整跑，按 hostInstRate ≈ 2×10⁴ inst/s 估算：
>
> - 5×10¹² inst / 2×10⁴ = **2.5×10⁸ s ≈ 8 年单 benchmark 串行 wall**
>
> 结论：**TAO 完全没有完整仿真 SPEC2017**，他们只采 100M / benchmark = 4 亿原始 instructions（去重后 180M）作为训练集。该规模与我们当前 W11–W15 单 workload 10M dedup × 5 = **50M dedup** 在数量级上同阶（差 ~3.6×），完全足以训练 TAO 同尺寸的 self-attention 模型。

### 与我们方案的对照

| 维度                    | TAO 论文                                  | 我们当前（W11–W15 50M）             |
|-------------------------|--------------------------------------------|--------------------------------------|
| ISA                     | ARM                                        | x86_64                               |
| Benchmark 来源          | SPEC CPU2017 reference (skip100M+take100M) | 自研 W11–W15 多线程 µbench          |
| 处理器模型              | O3CPU + AtomicSimpleCPU                    | O3CPU + 自研 MESI ref_sim           |
| 采集策略                | skip 100M + take 100M / benchmark          | ROI 内全采，dedup 至 ~10M / workload |
| 原始 instruction 数     | 4 × 100M = 400M                            | ~50M dedup（pack 前 ~52M）           |
| 去重后训练 instruction  | ~180M                                      | 50M dedup                            |
| Context length (N)      | 128（与 ROB 同）                           | 128                                  |
| 多线程 / 多核           | 论文不强调（单核 superscalar）             | 4 thread × 4 core，coherence 友好   |
| 任务覆盖                | fetch_lat / exec_lat / mispred / dlevel    | 同四项 + mem_coh / i_side（V10）    |

> 我们 50M 与 TAO 180M 是同一量级（差 ~3.6×），且我们的 multi-thread MESI 流量在 coherence 维度比 TAO 单核 SPEC trace 更丰富，对 `_MemCoh / _ISide` 训练更具信号密度。
