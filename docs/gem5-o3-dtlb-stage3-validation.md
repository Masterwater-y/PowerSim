# gem5 参数化 O3/DTLB Stage 3：C4–C32 验证报告

日期：2026-08-02  
配置：`configs/gem5-v28_1-time-epoch.cfg`  
最终结果：`results/gem5-v4-o3-dtlb-stage3-final-tbe256/`  
页表遍历消融：`results/gem5-v4-o3-dtlb-stage3-final-lat8/`、
`results/gem5-v4-o3-dtlb-stage3-final-lat32/`

> **复审说明（2026-08-02）：** 本文是 Stage 3 的历史结果记录，不再代表
> “组件语义已与 gem5 对齐”。后续源码审查确认了 memory-IQ lifetime、
> SQ/TSO drain、Ruby Sequencer 16 与 controller TBE 256、x86 单 walker
> 无 coalescing 等差距。另外 timing_certificate_failures=0 不能视为时序
> 正确性证明，因为 checker 尚未实际启用；四组仍有 29,666--245,077 个
> corrected-horizon violations。最新结论和实施顺序见
> [gem5 源码对齐与 P99 方案](gem5-source-aligned-p99-plan.md)。

## 结论

FastSim 现在可以用配置文件调整 gem5 基线中能够由退休态 functional trace
驱动的主要微架构资源：流水级宽度和延迟、ROB/IQ/LQ/SQ、FU 数量/延迟/流水化、
load/store 端口、分支预测器、DTLB/page walker、三级缓存容量/相联度/替换策略/
MSHR、CHA/NoC 以及 DRAM 组织和时序。每个已支持参数都连接到实际资源或状态机，
并非只增加同名配置字段。完整逐项映射见
[`gem5-parameter-coverage.md`](gem5-parameter-coverage.md)。

这轮同时把 trace 升级为 v4 双地址契约：物理地址只驱动 cache/coherence/CHA/
DRAM，虚拟页 token 只驱动 DTLB。v2/v3 仍可读取，但不能在严格模式下伪装成
具有虚拟页信息的输入。

精度结论必须保持克制。新增模型提高了参数可解释性和 PMU 覆盖面，但完整 92-case
CPI MAPE 为 12.26%--14.64%，仍未达到 6% gate，也略差于 Stage 2。DTLB miss、
cache 和 branch 的高流量 PMU 很准，但退休态 trace 缺少 wrong-path load、页表
访存地址、物理寄存器 lifetime 和 memory-order replay；仅靠增大一个固定 page-walk
latency 会过校准，不能解决 CPI 尾部误差。

吞吐量目标已经通过：92 个 C4/C8/C16/C32 case 全部高于 5M UOP/s，最慢的 C32
random-memory 为 8.11M UOP/s。

## 已实施的 gem5 参数组

| 参数组 | FastSim 中的有效模型 | 当前 gem5 v28.1 基线值 |
|---|---|---|
| 前端/后端宽度 | fetch、decode、rename、dispatch、issue、writeback、commit 独立带宽 | 全部 8-wide |
| 队列 | fetch queue、ROB、IQ、LQ、SQ 的独立容量；部分释放生命周期仍是近似 | 32/192/64/32/32 |
| 流水延迟 | fetch→decode、decode→rename、rename→dispatch、dispatch→issue，以及可选 execute/commit 延迟 | 1/1/2/1 cycles |
| FU pool | 整数、乘除、FP、SIMD、predicate、memory、system 的数量、op latency 和 pipelining | 取自捕获的 `config.ini`/FU profile |
| 数据端口 | 每周期 cache load/store port 配额 | 200/200 |
| 分支预测 | Tournament/gshare、BTB、RAS、indirect predictor 和恢复 penalty | 捕获的 Tournament 配置 |
| DTLB | 64-entry fully-associative LRU；当前 walker/merge 是近似且未与源码对齐 | 64 entries；实际 gem5 为单 active walker、无 coalescing |
| Cache | L1D/L2/LLC 容量、相联度、line、latency、LRU/TreePLRU、inclusive、近似 miss lanes | 32KiB/1MiB/64MiB；实际 Sequencer 16 与 controller TBE 256 是独立资源 |
| Uncore | MESI directory 近似、CHA 数/hash、NoC 单程延迟、LLC service | 8 CHA、12-cycle NoC |
| DRAM | 容量、channel/bank/row、open-row queue 和 tCL/tRCD/tRP/burst | DDR4-2400 时序换算为 3GHz core cycles |

Ruby 的 `number_of_TBEs=256` 是每个 controller 的容量；CPU 侧 Sequencer
另有 `max_outstanding_requests=16`。Stage 3 只为 private controller 和每个
CHA slice 分配了 256 个 TBE-like miss lane，没有建模独立的 16-request gate，
因此这里只能称为容量近似。lane 选择使用最小堆，将分配从线性扫描降为
`O(log TBE)`，但不代表请求生命周期与 Ruby 相同。

## 暂不支持的参数

以下参数没有被做成无效的配置语法：

- integer/FP/vector/predicate physical-register 数量；
- StoreSet SSIT/LFST、memory-order violation 和 replay policy；
- SMT backend sharing policy；
- wrong-path fetch/decode、squash bandwidth、I-cache 和 ITLB；
- Ruby 完整 transient/message-buffer/vnet/topology 状态机和全部 DDR4 约束。

原因不是 gem5 不支持，而是当前输入只有每核一条退休流。缺少 speculative
instruction/load、rename lifetime、page-table address 和 replay 原因时，这些参数
无法从 trace 驱动或用 gem5 PMU 校准。后续需要扩展 functional trace，而不是先
增加不会改变结果的配置项。

## C4–C32 CPI 精度

指标为 23 个 workload 等权的 UOP-CPI absolute relative error；signed bias
为 FastSim 相对 gem5 的有符号误差。

| Cores | Mean | Median | P90 | Max | Signed bias | Median throughput |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 12.263% | 7.221% | 22.597% | 67.598% | -4.086% | 19.981M UOP/s |
| 8 | 12.637% | 6.616% | 34.279% | 63.507% | -4.407% | 18.748M UOP/s |
| 16 | 13.436% | 8.540% | 26.659% | 51.286% | -0.914% | 17.083M UOP/s |
| 32 | 14.639% | 12.564% | 36.044% | 43.537% | +5.191% | 14.485M UOP/s |

相对 Stage 2，mean absolute error 分别变化 `+0.356pp`、`+0.519pp`、
`+0.940pp`、`+1.332pp`。因此本轮应解释为“gem5 参数覆盖和可诊断性完成”，
不能解释为 CPI 精度已经改善。

## 逐 workload signed CPI error

正数表示 FastSim CPI 高于 gem5，负数表示低于 gem5。机器可读的完整数值和
每核误差在最终结果目录的 `summary.csv`/`summary.json` 中。

| Workload | C4 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| bvc_encoder_base | +2.66% | +2.87% | +4.75% | +9.65% |
| bvc_encoder_heldout | +2.90% | +2.81% | +5.70% | +11.23% |
| cache_L1_mixed | +2.29% | +6.83% | +7.98% | +12.56% |
| cache_L2_mixed | +7.84% | +5.27% | +19.37% | +16.46% |
| coh_readmostly_sparse | -7.22% | -7.25% | -7.22% | -7.14% |
| flink_base | +7.02% | +6.83% | +8.54% | +13.81% |
| flink_heldout | +7.72% | +6.62% | +9.29% | +15.58% |
| fp_alu_dense | -0.34% | -0.36% | -0.38% | -0.42% |
| gofeed_base | +10.75% | +11.41% | +14.60% | +23.12% |
| gofeed_heldout | +8.44% | +7.38% | +8.48% | +14.34% |
| int_alu_dense | -0.11% | -0.12% | -0.12% | -0.14% |
| int_div_serial | +22.60% | +22.58% | +22.54% | +22.55% |
| marine_base | -2.23% | -2.12% | +1.28% | +8.48% |
| marine_heldout | +5.88% | +5.72% | +8.96% | +15.04% |
| memory_random_mlp | -2.82% | +4.37% | +19.90% | +43.54% |
| memory_seq_moderate | -19.87% | -34.28% | -26.66% | -0.92% |
| mysql_base | +7.97% | +6.19% | +6.66% | +11.36% |
| mysql_heldout | +7.97% | +5.76% | +5.94% | +10.02% |
| pytorch_base | -67.60% | -63.51% | -51.29% | -41.25% |
| pytorch_heldout | -59.33% | -56.54% | -48.49% | -36.04% |
| redis_base | -6.05% | -6.61% | -4.94% | +0.29% |
| redis_heldout | -5.66% | -8.44% | -9.13% | -5.93% |
| simd_sse_dense | -16.78% | -16.79% | -16.80% | -16.82% |

## DTLB 校准消融

functional trace 没有页表访存地址，因此 `dtlb.page_walk_latency` 是显式的固定
服务时间近似。对同一完整 92-case 集测试 1、8、32 cycles：

| Cores | 1 cycle MAPE | 8 cycles | 32 cycles |
|---:|---:|---:|---:|
| 4 | **12.263%** | 12.746% | 15.551% |
| 8 | **12.637%** | 13.029% | 15.509% |
| 16 | **13.436%** | 13.898% | 16.312% |
| 32 | **14.639%** | 14.860% | 17.105% |

32 cycles 能改善 PyTorch 和 C16/C32 random-memory，却显著恶化其他 workload；
8 cycles 也在四个核数上统一恶化 mean。最终基线采用 1 cycle，保留 walker
容量、LRU、miss merge 和 PMU 状态，但不把一个无法泛化的固定 penalty 当作
精度修复。下一步应采集页表访问的 functional 地址，或者实现经过独立训练集/
测试集验证的分层 walk 模型。

## PMU 精度

下表为 count-weighted absolute error（WAPE）：

| Cores | L1D miss | Private L2 miss | CHA lookup | Branch miss | DTLB access | DTLB miss |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% | 0.239% | 0.165% | 8.770% | 0.051% |
| 8 | 0.048% | 0.266% | 0.265% | 0.170% | 8.799% | 0.055% |
| 16 | 0.052% | 0.283% | 0.283% | 0.160% | 8.805% | 0.056% |
| 32 | 0.059% | 0.290% | 0.290% | 0.153% | 8.843% | 0.061% |

DTLB access 是有意保留的诊断例外：gem5 会统计 wrong-path load 的 translation，
退休 trace 不含这些地址。例如 C4 `int_div_serial` core 0 的 gem5 dispatched
load 为 33,106、squashed load 为 16,712，而退休 trace 只有 16,388 个 load。
FastSim 不使用一个经验倍数伪造访问。相比之下，new-walk DTLB miss 主要由
实际退休地址集合决定，WAPE 小于 0.061%。

## 吞吐量、守恒和测试

| Cores | Min | Median | Max |
|---:|---:|---:|---:|
| 4 | 11.673M | 19.981M | 37.002M UOP/s |
| 8 | 10.545M | 18.748M | 36.465M UOP/s |
| 16 | 9.546M | 17.083M | 30.836M UOP/s |
| 32 | 8.108M | 14.485M | 23.564M UOP/s |

92 cases 中低于 5M UOP/s 的数量为 0。每个 case 的 UOP、memory-event 和
private/escape partition 守恒 mismatch 均为 0；state/timing certificate
failure 均为 0。

Release 单测、ASan/UBSan 单测和 Python converter/validator 语法检查通过。新增
测试覆盖：v3 兼容读取、v4 virtual-page token、严格双地址契约、DTLB cold/
merged/warm 行为、frontend/writeback bandwidth sensitivity。

| Gate | Stage 3 结果 |
|---|---|
| C4--C32 mean UOP-CPI ≤ 6% | **Fail**：12.26%--14.64% |
| P90 ≤ 10% | **Fail**：22.60%--36.04% |
| Cache/branch/new-walk DTLB-miss WAPE ≤ 1% | Pass |
| DTLB access WAPE ≤ 1% | Not identifiable from retired trace；诊断值约 8.8% |
| UOP/memory/partition conservation | Pass：92 cases 均为 0 mismatch |
| Simulator throughput ≥ 5M UOP/s | Pass：minimum 8.108M UOP/s |

完整验证命令：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/validate_tcsim_c4_c8.py \
  --config configs/gem5-v28_1-time-epoch.cfg \
  --cores 4 --cores 8 --cores 16 --cores 32 \
  --out-dir results/gem5-v4-o3-dtlb-stage3-final
```

## 当前核心误差来源和下一步

1. **退休流缺少 wrong-path/front-end 工作。** 这同时影响 branch recovery、
   fetch queue、I-cache/ITLB 和 DTLB access，无法通过已有退休 UOP 恢复。
2. **页表访问不可见。** 固定 walk latency 在不同 workload/核数上方向相反，
   需要页表物理地址或独立的 walk-level functional stream。
3. **load response 仍以 interval tail feedback 表达。** 独立 miss overlap、
   ROB-head blocking 和 event-level response slack 仍过粗，形成 sequential、
   random 和 PyTorch 的相反误差。
4. **rename/memory-dependence 状态不完整。** physical-register pressure、StoreSet
   误依赖和 violation replay 是 int-div/SIMD 及部分业务负载固定偏差的候选来源。
5. **Ruby 只做容量/目录近似。** 每 slice TBE 容量已正确，但 transient、merge、
   message-buffer backpressure 和 network contention 仍未复现。

优先级应是扩展 trace 的 wrong-path/page-table functional 事实和 event-level
load-response slack，然后才继续新增 gem5 参数；否则参数数量增加不会等价于精度。
