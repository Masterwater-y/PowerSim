# FastSim 微架构泛化：C4 functional trace 与 OoO CPI/PMU 采集方案

逐轮假设、被否决路径、复现命令和日志索引统一维护在
`docs/uarch-generalization-debug-log.md`；本文件保留数据合同、采集方案和阶段结果。

更新日期：2026-08-05

## 1. 目标和边界

本方案验证 FastSim 在未参与调参的微架构配置上的 CPI、PMU 和相对性能变化。
gem5 SE 是离线 reference；FastSim 运行时只读取 canonical functional trace，不读取
gem5 timing、cache/TLB 命中或 coherence/path oracle。

首批采用 trace/label 解耦：

- 每个 `(workload, C4, seed)` 用 baseline O3 只采一份 functional trace；
- 同一 functional trace 被 16 个 uarch 的 FastSim replay 复用；
- 每个 uarch 独立运行无 trace 的 OoO gem5，保存 CPI/PMU label；
- binary/FST hash 固定输入身份，uarch label 的 retired-uop count gate 拒绝错误关联。

当前 C4 seed0 首批共有 12 份 trace 和 `16 × 12 = 192` 份 OoO label。

本次正式采集已于 2026-08-05 完成（01:38:50--02:11:53，约 33 分钟）：

- functional trace 12/12、OoO label 192/192、失败 0；
- 192/192 个 trace-label 关联通过，所有 label 相对 trace 固定多 21 uops，误差仅
  1.10--4.07 ppm，低于每核 32 uops 的 ROI 边界容差；
- 每核 OoO 退休指令实测 1,007,639--1,511,355，达到约 1M/核目标；
- canonical trace 共 91,600,196 条 functional uops；每核 1,290,273--4,754,499
  条，较大的 `int_div_serial` 来自 x86 微码展开；
- syscall ROI 事件为 0，所有 trace 均确认未输出 timing label 或 cache oracle；
- 最终数据位于 `tmp/uarch-c4-first-batch/`，总计约 6.9 GiB（trace 5.5 GiB、
  label 1.5 GiB）。

当前第一批只改变从 committed functional stream 可以较可靠辨识的参数：O3
width、ROB/IQ、x86 DTLB 容量、L1D/L2/LLC 几何、L3 bank 和 DRAM channel。
FU pool、物理寄存器、StoreSet、SMT、复杂 Ruby transient 和完整 DRAM timing
留到后续批次。

## 2. 一键运行

一键采集 C4 functional trace 和全部 uarch CPI/PMU：

```bash
cd /data00/yinhaolang/FastSim
./scripts/collect_uarch_c4_trace_and_cpi.sh
```

trace JSONL 写入和 FST 转换是 IO-heavy，默认最多 12 个 trace writer；label 默认最多
32 个 gem5 进程。实测单进程约占 9--11 GiB，自动并发按 12 GiB/进程预算。
当前主机可显式使用 8 个 trace writer 和 64 个 label worker：

```bash
FASTSIM_TRACE_JOBS=8 FASTSIM_UARCH_JOBS=64 \
  ./scripts/collect_uarch_c4_trace_and_cpi.sh
```

只补 trace 或只补 label：

```bash
FASTSIM_SKIP_LABELS=1 ./scripts/collect_uarch_c4_trace_and_cpi.sh
FASTSIM_SKIP_TRACE=1  ./scripts/collect_uarch_c4_trace_and_cpi.sh
```

只做一个 smoke：

```bash
./scripts/collect_uarch_c4_trace_and_cpi.sh \
  --workload v28_int_alu_dense --max-cases 1
```

查看矩阵或 dry-run：

```bash
./scripts/collect_uarch_c4_trace_and_cpi.sh --list
./scripts/collect_uarch_c4_trace_and_cpi.sh --dry-run --max-cases 4
```

支持 glob 过滤，例如：

```bash
FASTSIM_SKIP_TRACE=1 ./scripts/collect_uarch_c4_trace_and_cpi.sh \
  --uarch 'rob*' --uarch 'iq*' \
  --workload 'v28_*alu*'
```

`--uarch` 只适用于 label collector，因此该用法需要设置 `FASTSIM_SKIP_TRACE=1`。

输出目录默认为 `tmp/uarch-c4-first-batch/`，可通过
`FASTSIM_UARCH_DATASET_OUT=/path/to/output` 修改。已存在 `complete.json` 的任务会
自动跳过；失败目录保留现场，支持断点续跑。

## 3. 第一批 uarch

矩阵定义在 `configs/uarch-first-batch.json`。除对应变量外，其余参数保持 baseline：

| ID | 变化 | 目的 |
|---|---|---|
| `baseline` | 8-wide, ROB192, IQ64, DTLB64, L1D32K, L2 1M, LLC64M, 8ch | reference |
| `core_width4` | 全部流水宽度 8 → 4 | 前后端带宽敏感性 |
| `rob96`, `rob256` | ROB 192 → 96/256 | OoO window 泛化 |
| `iq32`, `iq96` | IQ 64 → 32/96 | issue/response occupancy 泛化 |
| `dtlb32`, `dtlb128` | x86 fully-associative DTLB 64 → 32/128 | translation PMU 泛化 |
| `l1d16k4`, `l1d64k8` | L1D 32K/8-way → 16K/4-way、64K/8-way | private tag 泛化 |
| `l2_512k8`, `l2_2m8` | L2 1M → 512K/2M | private miss-path 泛化 |
| `llc32m`, `llc128m` | 总 LLC 64M → 32M/128M，保持 8 banks | shared capacity 泛化 |
| `llc4bank64m` | 总 LLC 保持 64M，8 → 4 banks | CHA/home-slice 泛化 |
| `dram4ch` | DDR4 channel 8 → 4 | memory bandwidth/contention 泛化 |

L3 的 gem5 `l3_size` 是每 bank 容量，而 FastSim `cache.llc.size` 是总容量；矩阵
已经显式换算。例如 `llc4bank64m` 使用 gem5 `4 × 16MiB`，FastSim 使用
`cache.llc.size=64MiB, uncore.cha_count=4`。

`dtlb-assoc` 不进入矩阵。gem5 x86 DTLB 只有 size 参数，替换策略是
fully-associative LRU；旧 profile 中的 assoc 只是 TaoTrace 辅助元数据。

## 4. 第一批 workload

第一批 12 个负载覆盖各主要机制，同时控制总采集量：

| 类别 | Workload |
|---|---|
| Core/FU | `int_alu_dense`, `int_div_serial`, `fp_alu_dense`, `simd_sse_dense` |
| Cache | `cache_L1_mixed`, `cache_L2_mixed` |
| Memory | `memory_seq_moderate`, `memory_random_mlp` |
| Coherence | `coh_readmostly_sparse` |
| Business | `gofeed_base`, `pytorch_base`, `mysql_base` |

scale 按现有 C4 O3 退休指令实测线性外推，使每核约 1M 指令。整数 scale 会使实际
范围落在约 1.0M--1.5M/核：

| Workload | scale | 预计 O3 指令/核 |
|---|---:|---:|
| `int_alu_dense` | 7 | 1.09M |
| `int_div_serial` | 41 | 1.01M |
| `fp_alu_dense` | 23 | 1.04M |
| `simd_sse_dense` | 23 | 1.04M |
| `cache_L1_mixed`, `cache_L2_mixed` | 2 | 1.35M |
| `memory_seq_moderate`, `memory_random_mlp` | 3 | 1.45M |
| `coh_readmostly_sparse` | 2 | 1.21M |
| `gofeed_base` | 2 | 1.30M |
| `pytorch_base` | 2 | 1.07M |
| `mysql_base` | 2 | 1.51M |

首批先跑 C4；通过配置实效性、ROI、指令数和 PMU 完整性 gate 后再扩展 C16。

## 5. 采集实现和有效性检查

采集链路分为两条：

```text
                         uarch-first-batch.json
                            │                 │
               baseline × 12│                 │16 uarch × 12
                            ▼                 ▼
       collect_functional_traces.py      collect_uarch_stats.py
                            │                 │
              Atomic init → baseline O3      │Atomic init → target O3
              functional records only        │TaoTrace disabled
                            │                 │
                   FST + syscall sidecar      │CPI/PMU metrics
```

两条路径都在首个 `WORKBEGIN` 切换 O3 后执行 `m5.stats.reset()`。这样 switched O3
CPU 计数和 Ruby/DRAM 全局 PMU 都严格从 ROI 边界开始，同时保留边界前建立的内存
和线程架构状态；Atomic non-caching fast-forward 不把初始化访问作为 ROI cache warmup。
trace path 设置：

- `emit_micro=true`；
- `emit_micro_labels=false`；
- `emit_macro=false`；
- `emit_mem_events=false`；
- `require_roi=true`。

raw JSONL 只作为 staging 输入，立即转成 FST v7 后删除；64-byte 热记录继续保留
每 UOP 的 Int/Float/Vec/CC destination class counts。v7 在同一文件尾部嵌入稀疏
syscall 元数据表，仍不保存 cache/commit timing labels 或 oracle 字段。

`tools/gem5/run_uarch_stats_se.py` 直接修改真实 gem5 SimObject：

- switched O3 core 的 width、ROB、IQ、LQ/SQ、DTLB size；
- MESI_Three_Level 的 cache geometry、Sequencer 和各 controller TBE；
- L3 bank 数与 DDR4 channel 数。

每个 case 完成后，collector 从 `config.ini` 回读并逐核检查：

- 7 个 O3 width、ROB/IQ/LQ/SQ、DTLB；
- L1I/L1D/L2/L3 size 和 associativity；
- L3 bank 数、memory controller 数；
- Sequencer outstanding 和 L1/L2/L3/directory TBE。

requested/effective 任一不一致，case 标记失败且不进入正式目录。这消除了旧
`run_mt_mvp.py` 中某些 CLI 只写 `uarch_profile.json`、没有改变 gem5 对象的风险。

## 6. 每个 case 的产物

目录结构示例：

```text
tmp/uarch-c4-first-batch/
  traces/seed0/c04/W_v28_pytorch_base/
    core0.fst ... core3.fst
    core0.syscalls.jsonl ... core3.syscalls.jsonl
    manifest.txt
    syscalls.jsonl
    roi.jsonl
    trace.json
    config.ini
    complete.json
  labels/
    summary.csv
    summary.json
    rob96/c04/W_v28_pytorch_base/
      task.json
      requested_uarch.json
      config.ini
      config-validation.json
      stats.txt
      gem5.log
      metrics.json
      complete.json
  dataset-index.json
  dataset-index.csv
```

`dataset-index.*` 将每份 functional trace 关联到所有 uarch label，并记录
`stream_exact`、`stream_delta_uops` 和 `stream_delta_ppm`。gem5 的 ROI 控制伪指令与
Atomic→O3 切换边界可能不进入 functional trace；默认仅允许每核最多 32 uops 的
固定边界差，超限即拒绝关联。C4 冒烟实测差 21/5,161,092 uops（4.07 ppm）。

FST v7 的每条 syscall 在热记录中以 `op_class=-1` 保存，`address` 保存 sysnum；
文件尾部的 128-byte 稀疏行再以 `(record_ordinal, syscall_ordinal)` 对齐，保存 DR
通用能力能提供的 `thread_id`、最多 6 个 raw ABI 参数、raw 返回值、failure/errno、
前后微秒时间戳/CPU ID 和 maybe-blocking hint。每个可选字段都有 validity bit，未采到
不会填成 0。`coreN.syscalls.jsonl` 与合并后的 `syscalls.jsonl` 是同一次转换生成的
`fastsim-functional-syscall-v2` 审计镜像；回放只读 FST 内嵌表，不依赖 loose sidecar。
时间戳差是插桩下 wall time，不是 active CPL0 cycles 或 latency oracle。当前 12 个
v28 workload 的线程创建、barrier 和 syscall 都设计在 ROI 外，因此首批表通常为空；
该字段主要为后续 syscall 负载预留。

`metrics.json` 和 `summary.csv` 包含：

- aggregate UOP CPI：`sum(switch core cycles) / sum(committed UOPs)`；
- macro CPI、每核 cycles/UOP/instruction；
- branch committed/miss；
- Ruby L1D、private L2、shared L3 demand access/miss；
- DTLB access/miss；
- ROB/IQ/LSQ full events；
- DRAM read/write burst、总访问延迟、每 channel row-hit rate；
- 相对 baseline 的 gem5 speedup；
- wall time、命令、binary SHA-256、config/stats SHA-256。

Ruby LLC demand miss 和 FastSim LLC tag miss 不具有完全相同语义；严格 PMU 主项是
L1D miss、private-L2 miss、shared L3 demand access/CHA lookup 和 branch miss。

## 7. 后续与 FastSim 合并

矩阵中每个 profile 都带有 `fastsim` override。后续 replay 必须：

1. 复用对应 `(workload, cores, seed)` 的 canonical FST；
2. 从 baseline 配置生成 uarch-specific FastSim cfg；
3. 检查 gem5 `retired_uops/retired_instructions` 与 trace/FastSim 是否一致；仅 ROI
   marker/切核造成的每核不超过 32 uops 边界差可接受，并在 index 中显式记录；
4. 超出边界容差时将 case 标记为动态流变化，禁止计算 CPI 误差；
5. 同时报告绝对 CPI 误差和相对 baseline 的 speedup error：

```text
speedup_gem5    = CPI_gem5_baseline / CPI_gem5_variant
speedup_fastsim = CPI_fastsim_baseline / CPI_fastsim_variant
speedup_error   = speedup_fastsim / speedup_gem5 - 1
```

第一阶段建议 gate：每个 core count 的绝对 CPI P99 ≤ 10%，uarch speedup error
P90 ≤ 10%，改善方向准确率 ≥ 90%，高流量 branch/L1D/L2/CHA PMU WAPE ≤ 2%，
且所有 FastSim case ≥ 5M UOP/s。DTLB access 因 committed trace 缺少 wrong-path
translation，继续单列诊断。

## 8. 推荐执行顺序

1. 单 case trace smoke：`baseline × int_alu_dense × C4`，验证 4 个 FST。
2. 单 case label smoke：baseline 和 width4，验证 ROI reset、CPI/PMU 与配置回读。
3. 正式第一批：12 份 C4 trace + `16 × 12` 份 C4 OoO label。
4. 对代表性 workload 做 1M/2M 收敛检查：CPI ≤ 0.5%，高频 PMU rate ≤ 2%。
5. 通过后增加 C16 label 和独立 C16 trace；不能把 C4 trace 复制成 C16 输入。

不应在第一批中改变 `sim.interval_max_cycles`、chunk/lookahead、response exposure、
FR-FCFS proxy window 等 FastSim 算法超参数；它们不是目标微架构参数。

## 9. FastSim C4 微架构泛化实测

完整 replay 和评估命令：

```bash
FASTSIM_REPLAY_JOBS=32 ./scripts/run_uarch_c4_fastsim_validation.sh
```

脚本先并行完成 192 个 replay，再以单 job 重放一次，使每 case 的吞吐量不受批量
资源竞争影响。设置 `FASTSIM_SKIP_ISOLATED_THROUGHPUT=1` 可跳过第二步，但此时不能
用结果判定独立的 5M UOP/s gate。每个 case 都回读 FastSim 输出检查 override，并
要求 FST record count 与 FastSim retired UOP 完全相等。

2026-08-05 的 C4 结果为：FastSim 192/192 成功、配置错误 0、UOP 守恒错误 0。

| 指标 | 实测 | Gate | 结论 |
|---|---:|---:|---|
| Variant absolute CPI mean / P90 | 2.504% / 5.871% | — | typical case 较好 |
| Variant absolute CPI P99 | 15.672% | ≤ 10% | **Fail** |
| Uarch speedup error P90 | 0.887% | ≤ 10% | Pass |
| Uarch speedup error P99 / max | 19.636% / 24.500% | diagnostic | tail 明显 |
| 有效变化方向准确率 | 23/25 = 92.0% | ≥ 90% | Pass |
| 最低独立吞吐量 | 8.142M UOP/s | ≥ 5M | Pass |

方向只在 gem5 speedup 变化至少 0.5% 时计分；该阈值对应前述 1M/2M 收敛检查目标。
不加阈值时，75 个非完全相等的 case 中原始方向准确率为 61.3%，但多数变化远低于
0.5%，把舍入/边界级差异判作改善或退化没有稳定意义。完整报告同时保留两种口径。

超过 10% absolute CPI error 的 4 个 case：

| Uarch / workload | gem5 CPI | FastSim CPI | signed CPI error | speedup error |
|---|---:|---:|---:|---:|
| `iq96 / memory_seq_moderate` | 2.092761 | 1.746868 | -16.528% | 18.381% |
| `rob256 / pytorch_base` | 1.012708 | 0.853225 | -15.748% | 24.500% |
| `iq32 / pytorch_base` | 1.526097 | 1.287241 | -15.651% | 24.357% |
| `llc4bank64m / memory_seq_moderate` | 2.136841 | 1.909233 | -10.652% | 10.595% |

这些 case 的 effective IQ/ROB/CHA 参数均已回读确认，trace/replay UOP 也完全相等。
因此当前主要缺口是 response-driven IQ/ROB capacity 和 CHA bank timing 的敏感性
模型，而不是采集错误。

Variant count-weighted PMU WAPE：

| PMU | WAPE | Gate | 结论 |
|---|---:|---:|---|
| L1D demand miss | 0.014% | ≤ 2% | Pass |
| private-L2 demand miss | 1.347% | ≤ 2% | Pass |
| CHA/shared-L3 lookup | 1.347% | ≤ 2% | Pass |
| branch miss | 0.042% | ≤ 2% | Pass |
| DTLB access | 11.801% | diagnostic | committed trace 缺 wrong-path translation |
| IQ-full | 70.268% | diagnostic | occupancy/event 语义尚未对齐 |
| ROB-full | 3645.135% | diagnostic | 当前近似不能当 gem5 PMU 使用 |
| LLC tag miss vs Ruby demand miss | 4.871% | diagnostic | 统计口径不同 |

这批数据能够支持“典型 CPI、相对 speedup P90 和高流量 functional PMU 具有一定
跨 uarch 泛化性”，但不能宣称整体通过：CPI P99 明确失败，IQ/ROB timing PMU 也
未对齐。另外 `dtlb32/128`、`llc32m/128m` 在 12 个负载上没有产生 ≥0.5% 的 gem5
变化，因此属于激励不足，而不是已经验证容量泛化。下一批应增加 TLB/LLC working-set
跨容量阈值负载，并优先修复 IQ/ROB response occupancy sensitivity。

结果文件位于：

- `tmp/uarch-c4-first-batch/evaluation/generalization-report.md`；
- `tmp/uarch-c4-first-batch/evaluation/generalization-report.json`；
- `tmp/uarch-c4-first-batch/evaluation/cases.csv`；
- `tmp/uarch-c4-first-batch/fastsim/`（逐 case config、stats、log 和校验结果）。

## 10. 第二批：容量阈值激励与 ROB pilot（2026-08-05）

首批 12 个负载并未充分覆盖所有参数。以 gem5 speedup 相对 baseline 变化至少 0.5%
作为“有效激励”，首批欠激励 profile 为：`dtlb32/128`、`l1d16k4/64k8`、
`l2_512k8`、`llc32m/128m/4bank64m` 和 `rob256`。因此评估器现在要求每个非 baseline
uarch 至少有 2 个有效 case；不满足时即使 CPI/speedup 数值较好也不能报告整体 PASS。

新增 `workloads/uarch_excitation/`，第一组固定功能流覆盖：

| 机制 | workload 档位 | 跨越的目标容量 |
|---|---|---|
| ROB head-miss overlap | 80 / 160 / 240 independent ALU UOP block | ROB 96 / 192 / 256 |
| IQ miss-dependent chains | 24 / 56 / 72 / 88 waiting ALU UOP block | IQ 32 / 64 / 96 |
| DTLB | 48 / 80 / 96 个 4KiB page | DTLB 32 / 64 / 128 |
| L1D | 24 / 40 / 48KiB 私有随机工作集 | L1D 16 / 32 / 64KiB |
| private L2 | 768 / 1280 / 1536KiB 私有随机工作集 | L2 512KiB / 1MiB / 2MiB |

所有分配、first-touch、线程创建、affinity 和 barrier 都在 ROI 外；每线程使用私有
数据，避免把核心容量测试变成 coherence 测试。ROB/IQ 数据使用每核 32MiB、每 line
一个元素的 full-cycle pointer chain。scale=1 的 `uarch_rob160` 实测每核
2,275,056 functional UOP、约 1.18M committed macro instruction，四核完全一致。

该 pilot 的 gem5 O3 结果表明负载确实激活 ROB 阈值，而不是只改变配置文件：

| ROB | UOP CPI | vs baseline | ROBFullEvents |
|---:|---:|---:|---:|
| 96 | 0.635510 | -6.786% speedup | 174,852 |
| 192 | 0.592383 | baseline | 49,996 |
| 256 | 0.583974 | +1.440% speedup | 0 |

同一 functional trace 的 FastSim replay 对 ROB96/256 方向均正确，speedup error 分别
为 2.515% 和 0.780%，absolute CPI error 分别为 4.684% 和 3.043%。但单 workload
仍不足以证明泛化，所以新 coverage gate 将该三点 pilot 正确标成整体 FAIL。

完整一键命令：

```bash
FASTSIM_TRACE_JOBS=12 FASTSIM_UARCH_JOBS=24 FASTSIM_REPLAY_JOBS=24 \
  ./scripts/run_uarch_excitation_c4.sh
```

脚本执行 build → 16 份 C4 functional trace → 48 个匹配机制的 gem5 label → FastSim
replay → 评估报告。矩阵位于 `configs/uarch-excitation-first-batch.json`；不会生成
不相关 workload/profile 的全笛卡尔积。采集和 replay 均可断点续跑。

模型源码核对同时修正了一个设计假设：gem5 O3 的非访存 UOP 在 issue 后释放 IQ，
但 memory UOP 明确在 `wakeDependents()` 的完成路径才 `clearInIQ()`。FastSim 当前
memory-IQ completion/response lifetime 与该方向一致，不能改成“所有访存发射即释放”。
后续核心修复保持在 response-corrected epoch suffix：拆分 decode/issue/retire cursor，
对越过 horizon 的未退休 ROB/LSQ/dependency 状态跨 epoch 携带，避免错误提交整个
lower-bound accepted prefix；在该闭环完成前不通过 workload-specific latency scale
拟合尾差。

最终补齐上侧阈值后，正式目录包含 16 份 trace、48 个 gem5 label 和 48 个 FastSim
replay，采集/配置/UOP 关联错误均为 0，占用约 19GiB。结果如下：

16 个 workload 的 functional trace 为每核 2.18M--6.82M UOP（中位数 5.58M），
ROB160 的 gem5 committed macro instruction 为每核约 1.18M；因此这批不是用几十万
指令制造的短暂启动效应，同时仍保持一分钟级 gem5 label 和秒级 FastSim replay。

| 指标 | 结果 | Gate |
|---|---:|---:|
| Variant CPI mean / P90 / P99 | 1.918% / 4.303% / 6.388% | P99 ≤ 10% |
| uarch speedup error P90 / max | 2.630% / 4.009% | P90 ≤ 10% |
| 有效 CPI 方向准确率 | 90.0% | ≥ 90% |
| 微架构 CPI 排序准确率 | 93.75%（15/16） | pairwise gem5 CPI delta ≥ 0.5% |
| 欠激励 uarch | 0 | 每个 uarch ≥ 2 cases |
| 最低独立 FastSim 吞吐 | 11.067M UOP/s | ≥ 5M |
| L1D / private-L2 / CHA WAPE | 0.006% / 0.123% / 0.123% | ≤ 2% |
| 总体 | PASS | — |

覆盖按参数族判定：ROB/IQ 使用 CPI speedup，DTLB/L1D/private-L2 分别使用对应 miss
count 相对 baseline 的变化，PMU materiality 阈值为 5%。这是必要区分：gem5 SE 的
DTLB 容量变化可让 miss count 从数百变为数十万，但不会产生 timing page-walk CPI；
L1/L2 容量 miss 也可能被该负载的 MLP 隐藏。低于 10,000 个 reference event 的 branch
miss 只报告诊断值，不把启动期几百次事件纳入 2% WAPE hard gate。

阈值套件 PASS 不覆盖首批业务集的 CPI P99 FAIL。尤其 `iq96` 的两个有效 case 只有
1/2 方向正确：`iq72` 中 gem5 speedup 为 +1.433%，FastSim 为 -0.089%；`iq88` 中
gem5 为 +2.762%，FastSim 仅 +0.046%。开启 CPI attribution 后，`iq72` baseline/iq96
分别有 43,178/40,626 个 response-corrected memory issue 越过 epoch horizon；当前实现
仍提交整个 lower-bound prefix，并使 iq96 的 response dependency cycles 反而上升。
保持 trace/uarch/latency 不变，只把 `interval_max_cycles` 从 256、512、1024、2048、
4096、8192 改到 16384 时，FastSim IQ96 speedup 依次为 +0.516%、+0.147%、-0.089%、
-0.524%、+0.201%、+1.721%、+3.230%，而 gem5 固定为 +1.433%。这种随宿主 epoch
划分改变方向的现象直接违反算法参数不应改变 target timing 的合同，进一步确认不能
通过选一个 Q 值修补。
这为下一提交的 suffix carry 修复提供了直接回归点，不能把阈值套件 PASS 误解为
IQ/ROB PMU 已对齐；当前 IQ-full/ROB-full WAPE 仍为 67.6%/9203.9%，继续只作诊断。

## 11. Q=1024 固定后的 response boundary 优化（2026-08-05）

从本阶段开始 `sim.interval_max_cycles` 固定为 1024，不再做 Q 拟合。首先验证了已有
`interval_causal_timing`：它能把 IQ72/IQ88 的 IQ96 speedup 分别修到 +0.169% 和
+2.619%，但完整 48-case 中对 small-IQ/ROB 严重过修，variant CPI P99 达 99.825%、
有效方向准确率降到 70%。因此生产配置继续关闭该旧 fixed-point 路径；这个反例也证明
不能以两个目标点代替完整机制矩阵。

新增实验开关 `sim.interval_corrected_suffix_carry`。其语义是：若 response-corrected
memory issue 超过当前 `[T,T+1024]` horizon，就从该 UOP 起延后该核后缀；shared
cache/directory、DRAM、private cache 和 per-core PMU counter 都恢复到同一个
epoch-entry transaction，再只重放可提交前缀。最多 8 次稀疏收缩，仍不稳定则恢复整个
epoch、推进 horizon 后重试，禁止提交未认证事件。实现另外加入连续 cache undo buffer，
避免每个 cache set 单独分配 way snapshot；单元测试覆盖后缀触发、事务收敛以及
UOP/memory/cache counter 守恒。

固定 Q=1024 的 48-case 结果位于：

- `tmp/uarch-c4-excitation-first-batch/fastsim-suffix-q1024/`；
- `tmp/uarch-c4-excitation-first-batch/evaluation-suffix-q1024/`。

| 指标 | 原生产路径 | suffix carry | Gate |
|---|---:|---:|---:|
| Variant CPI P99 | 6.388% | 7.212% | ≤ 10% |
| speedup error P90 | 2.630% | 2.988% | ≤ 10% |
| 有效方向准确率 | 90.0% | 100.0% | ≥ 90% |
| 微架构 CPI 排序准确率 | 93.75%（15/16） | 100.0%（16/16） | ≥ 90% |
| 最低独立吞吐 | 11.067M | 3.746M UOP/s | ≥ 5M |

精度、speedup 和方向 gate 均通过，但最慢 `rob96/uarch_rob80` 的隔离吞吐仍低于 5M，
所以该开关暂不写入生产 profile。当前性能瓶颈不是 trace/gem5 采集，也不是 Q；而是几乎
每个 response-active epoch 都先执行完整 speculative feedback，再回滚并计算一次前缀，
`rob80` 中累计被延后的候选 UOP 约为 trace UOP 的四倍。下一优化应给 per-core response
state 增加可回滚的 prefix checkpoint/undo log，使 suffix 只撤销 crossing UOP 之后的
queue/ROB/LSQ/dependency 增量，避免第二次扫描已认证前缀。达到 ≥5M 后再启用生产开关，
随后回放原 192-case 业务集；不需要重采 functional trace 或 gem5 label。

微架构 CPI 排序准确率按同 workload、同核数内的 uarch 两两比较计算；只有两点 gem5
UOP CPI 相差至少 0.5% 才进入正式分母，FastSim 给出相同的 CPI 大小关系即为正确。
不设 materiality 的 raw 排序准确率仅作诊断：阈值套件当前生产/suffix 均为 79.41%
（27/34）。原 192-case 业务集的有效排序准确率为 90.72%（303/334），raw 为 65.65%
（560/853）。评估器已将有效排序准确率 ≥90% 加入总体 gate，并在 JSON 中输出逐 workload
的 pair count、correct count 和 accuracy。

正式 PMU 表只报告 L1D miss、private-L2 miss、LLC miss、branch miss 和 CHA LLC
lookup。LLC 项明确保留 FastSim LLC tag miss 与 gem5 Ruby LLC demand miss 的口径差异；
IQ/ROB/LSQ、DTLB access 等计数仍可留在 JSON/CSV 供内部定位，但不再放入正式报告。

## 12. 业务负载微架构激励集（2026-08-05）

原 12 个业务 case 虽然覆盖全部 16 个 uarch，但部分参数在 gem5 中没有产生足够大的
响应，不能仅靠增加 ROI 长度解决。新增 `workloads/business_excitation/`，通过合法业务
输入形状改变工作集、并发 fanout 和共享访问分布；不在 ROI 尾部拼接 synthetic kernel，
也不为不同 uarch 生成不同功能流。所有 allocation、first-touch、线程创建和 barrier
仍在 ROI 外，functional trace 每个 workload 只采一份。

第一阶段是 12 trace、48 个定向 OoO label，而不是 12×16 全笛卡尔积：

| 业务形状 | 主要激励 | 目标 profile（另含 baseline） |
|---|---|---|
| GoFeed 8 路候选 fanout、PyTorch dense batch | width / ROB / IQ | width4、ROB96/256、IQ32/96 |
| GoFeed 80-page graph、MySQL 1280KiB index | DTLB | DTLB32/128 |
| MySQL 24/48KiB hot index | L1D 容量 | L1D16K/64K |
| PyTorch 768/1536KiB embedding、MySQL 1280KiB index | private L2 容量 | L2 512K/2M |
| 48MiB graph/embedding、96MiB table | LLC / DRAM | LLC32M/128M、DRAM4ch |
| 同 bank-line 类热点、96MiB shared table | CHA/home-slice | LLC4bank64M |

矩阵位于 `configs/business-excitation-c4.json`。gate 要求每核至少 5M retired UOP；
在生成大 trace 前先以 scale=8 采 12 个 baseline OoO label 审计实际长度，实测范围为
6.816M--27.749M UOP/core、101.7--736.5 秒/case。随后按线性退休 UOP 关系冻结正式
scale：fanout=2，80-page=8，24/48KiB L1 index=2，其余 workload=3；预测正式范围
5.1M--7.6M UOP/core。偏短 workload 必须提高 scale 并重采，不能用重复 trace 或 ROI
外指令补齐。`gofeed_graph_pages_80` 的 DTLB baseline 为 1,048,780 accesses /
210,088 misses，已同时满足长度和激励前置条件。

一键执行 build → functional trace → targeted gem5 OoO CPI/PMU → trace-label link →
固定 Q=1024 的 FastSim replay → 排序/误差评估：

```bash
FASTSIM_TRACE_JOBS=4 FASTSIM_UARCH_JOBS=24 FASTSIM_REPLAY_JOBS=24 \
  ./scripts/collect_business_excitation_c4.sh
```

默认输出到 `tmp/business-excitation-c4/`，支持断点续跑。可通过
`FASTSIM_SKIP_TRACE=1`、`FASTSIM_SKIP_LABELS=1`、`FASTSIM_SKIP_REPLAY=1` 分阶段运行；
并行 trace 建议限制为 4 以控制 raw JSONL 峰值，label 可按可用内存提高并发。正式
5.1M--7.7M UOP/core 实测 raw JSONL 为每核约 5.4--7.1GiB，4 条 trace 同时转换时应
至少预留 120GiB 临时空间；raw 在转换后删除。最终 FST/label/replay 预计新增约
16--22GiB。正式 PMU 报告仍只展示 L1D/private-L2/LLC miss、branch miss 和 CHA LLC
lookup，其他计数仅用于判断负载是否真正激励对应参数。

首轮 48 个 gem5 OoO label 已全部完成且失败为 0。有效激励包括：ROB96 在两个业务
case 中分别造成 -11.0%/-26.4% speedup，ROB256 在 dense batch 中为 +5.6%；IQ32/96
在 dense batch 中为 +4.8%/-1.8%；DTLB32/64/128 在 80-page graph 中分别产生
628,897/210,088/340 misses；L1D16K 对 24KiB index 的 miss 增加 63 倍，L1D64K 对
48KiB index 的 miss 减少 96.9%；private-L2 512K/2M 也跨越预期 miss 阈值；DRAM4ch
在三个 shared workload 中造成约 3.0%--4.1% slowdown。

该 pilot 同时将 `core_width4`、`llc128m` 和 `llc4bank64m` 标为仍欠激励：当前 shared
random 在一个 ROI 内没有完成足够的 64--128MiB 工作集复访，单 bank hotspot 也不能
检验 8→4 个 home slice 的并行度。下一小批只补三类，不重采已经有效的参数族：小热
模型上的多累加器 compute batch（width）、64--128MiB 的确定性两遍容量扫描（LLC128）
以及跨 8 bank 分散的高 MLP fanout（CHA）。

首轮完整 pipeline 已于同日完成：12/12 functional trace、48/48 gem5 OoO label、
48/48 固定 Q=1024 的 FastSim replay，trace-label 关联错误为 0。正式结果位于
`tmp/business-excitation-c4/evaluation/`：

| 指标 | 结果 | Gate |
|---|---:|---:|
| Variant CPI mean / P90 / P99 | 16.730% / 48.558% / 51.988% | P99 ≤ 10%，FAIL |
| speedup error P90 / max | 3.705% / 9.883% | P90 ≤ 10%，PASS |
| 有效方向准确率 | 77.778% | ≥ 90%，FAIL |
| 微架构 CPI 排序准确率 | 87.097%（27/31） | ≥ 90%，FAIL |
| 最低独立吞吐 | 10.160M UOP/s | ≥ 5M，PASS |
| L1D / private-L2 / branch / CHA WAPE | 0.206% / 0.092% / 0.358% / 0.092% | 均 PASS |
| LLC miss diagnostic WAPE | 31.343% | diagnostic |
| 欠激励 uarch | 8 | FAIL |

absolute CPI 主要失败集中在高 MLP `gofeed_fanout_wide`：gem5 baseline CPI=2.130，
FastSim=1.080，约低估 49.3%；ROB96/256 的 absolute error 为 52.3%/51.4%。这不是
通过继续延长 ROI 能修复的启动误差；speedup P90 仍通过说明相对变化已有部分泛化能力，
但 shared-miss queue/response lifetime 对 absolute stall 的建模不足。四个 material
ranking 错误全部来自 `pytorch_dense_batch`。下一阶段固定 Q=1024，分别处理：

1. 用 compute batch、两遍 LLC 容量扫描和跨-bank MLP 补足 width/LLC128/CHA 激励；
2. 对 fanout 做 gem5-vs-FastSim 的 L2/LLC/DRAM response lifetime 与 outstanding MLP
   attribution，修复 absolute CPI，不做 workload-specific latency scale；
3. 对 dense batch 检查 ROB/IQ release、dependency ready 和 SIMD throughput，修复四个
   material ranking 反向点；
4. 在原 48 cases 和补充 case 上共同回归，不能只优化新负载。

采集器新增 `--resume-orphans`：若 `.running-*` 已完成全部 WORKEND 和 raw JSONL，可复用
已完成 FST core 并仅续转残余 core；若旧转换器同时完成并发布 final trace，则以 final
`complete.json` 为准，不再误报失败。一键脚本默认开启该恢复模式。

首轮 shared transient-fill 修复及完整回归已记录在
`docs/uarch-generalization-debug-log.md` 第6节。业务48-case的variant CPI
mean/P90/P99改善为15.718%/43.442%/47.204%，阈值48-case仍PASS；该修复有效但不足以
解决fanout/dense，下一阶段转向branch/frontend与response→ordered-retire/ROB residual
的逐阶段守恒诊断。Q继续固定为1024。

2026-08-05 的下一阶段结果记录在调试日志第7节。新增逐阶段 response residual 守恒账本、
单次 shared-queue retime 事务和 Ruby fill-response service stage。retime 因大多数请求越过
Q=1024 boundary，对12-case的排序/CPI无实质收益，默认关闭；resource-calendar 消融同样
无效。根据 captured gem5 `config.ini` 补齐统一的 8-cycle memory-response→LLC-fill 路径后，
fanout/dense CPI 从1.18573/1.08168提高到1.20465/1.11135，12-case variant CPI mean 从
33.136%降到31.610%，strict PMU继续PASS，独立吞吐11.747M/11.043M UOP/s。material
ranking仍为78.947%，所以当前主任务转为 response-extended ROB-head 的局部 suffix closure，
不能继续放大 memory latency 或重启全 epoch causal replay。

8-cycle production 候选已继续通过完整回归：业务48-case variant CPI mean/P90/P99=
14.370%/42.454%/46.364%，较 transient-v2 全部改善，speedup P90=3.242%，strict PMU
全部PASS；阈值48-case总体PASS，CPI P99=8.439%，有效方向=90%，排序=93.750%，最低
8路并发吞吐9.641M UOP/s。正式输出为
`tmp/business-excitation-c4/evaluation-fill8-full/` 和
`tmp/uarch-c4-excitation-first-batch/evaluation-fill8/`。

2026-08-05 后续 ROB-head-local suffix 实验已实现并完成12-case定向验证，配置项为
`sim.interval_rob_head_suffix_replay`。该路径保持 Q=1024 和 canonical functional PMU，
但 variant CPI mean 仅从31.610%降到30.722%，ranking仍为78.947%，8路并发最低吞吐
降到4.629M UOP/s；未进入完整48-case，生产配置继续关闭。实现、`vector<bool>` 并行
确定性排错和结果见 `docs/uarch-generalization-debug-log.md` 第8节。
