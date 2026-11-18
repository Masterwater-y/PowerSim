# SPR-M5：保留私有 preview 时间字段修复，Estimated 关闭

2026-11-17，用户要求整理当前结果并设为默认。当前 SPR 默认入口 `configs/spr-evaluation-default.cfg` 选择不可变的 `configs/spr-m5-private-preview-clocks.cfg`；维护入口 `configs/gem5-spr-retired.cfg` 指向同一 alias。`simulate` 仍需显式传入 `--config`，没有修改通用库或不带配置的benchmark默认行为。

本次安装的是12例测量使用的同一已审核 Release / LTO16 ELF，未修改模型源码、未重新编译模型。`build/fastsim` SHA-256：`b869c0041b7ca6cf625e8aebdd6392e7c4a52be95ad1466052dc3d39e7d5f1b8`。冻结副本：`tmp/spr-m5-default-20261117/frozen-m5/fastsim`。131个模型源码/共享头与review-source逐文件一致；来源、测试和配置身份分别记录，不把Git HEAD当成ELF身份。

## 默认机制

| 设置 | 默认状态 |
|---|---|
| 私有preview选定服务的issue/completion时间字段 | 保留修复；由ELF实现，不是固定延迟调参 |
| Estimated时间路径 | **关闭**，`core.unified_parent_estimated=false`；代码和测试保留 |
| 在途服务 | `core.unified_parent_service=v2-wait`，已有certified服务及follower继续使用 |
| 退休store prepare | `core.legacy_prepare_timing=true` |
| functional STLB / page walk | 继承M3，开启 |
| UART/APIC服务 | 继承M4，开启 |
| MOV / zero-idiom消除 | 继承M2，开启 |
| 宿主执行 | validated preload；materialized UOP fast kernel开启，CPI attribution关闭 |
| 硬件预取 / native early prepare | 来源必须独立验证关闭；保留退休软件预取指令 |

M5与12例原工作profile的全部显式配置等价，仅include路径与版本记录不同；327项基础设置和12例来源覆盖配置使用实际C++ loader逐项核对。每个新来源仍需绑定其geometry/timings、IQ、thread-state plan和目标平台设备参数；不把某个负载的绝对路径放入全局默认。

## C4/C8/C16 当前结果

四个共同负载各三种核数，共12例，无Marian。CPI为user-plus-kernel task active范围；PMU为 `workload_task` 的 `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS`。RE=`(FastSim−gem5)/gem5 ×100%`，CPI AE=`abs(FastSim−gem5)`，单位cycles/macro-instruction。

| 负载 | 核数 | gem5 CPI | FastSim CPI | CPI RE (%) | CPI AE | 主吞吐 (M user UOP/s) |
|---|---:|---:|---:|---:|---:|---:|
| ZSTD | 4 | 1.206049 | 1.185146 | -1.733 | 0.020903 | 1.342 |
| XZ | 4 | 0.575297 | 0.602991 | +4.814 | 0.027694 | 1.744 |
| FLAC | 4 | 0.284584 | 0.294976 | +3.651 | 0.010392 | 2.198 |
| Graph500 | 4 | 0.966422 | 0.839796 | -13.103 | 0.126626 | 1.569 |
| ZSTD | 8 | 1.052763 | 1.086689 | +3.223 | 0.033927 | 1.813 |
| XZ | 8 | 0.529423 | 0.553152 | +4.482 | 0.023728 | 2.252 |
| FLAC | 8 | 0.269360 | 0.274787 | +2.015 | 0.005426 | 2.922 |
| Graph500 | 8 | 0.969791 | 0.854475 | -11.891 | 0.115317 | 2.425 |
| ZSTD | 16 | 1.065956 | 1.137154 | +6.679 | 0.071198 | 1.810 |
| XZ | 16 | 0.511531 | 0.532531 | +4.105 | 0.021000 | 2.155 |
| FLAC | 16 | 0.258793 | 0.264746 | +2.300 | 0.005953 | 2.490 |
| Graph500 | 16 | 0.980453 | 0.944556 | -3.661 | 0.035897 | 2.443 |

| 负载 | 核数 | L1 MISS RE (%) | L2 MISS RE (%) | L3 MISS RE (%) | 原生unknown/事件 |
|---|---:|---:|---:|---:|---:|
| ZSTD | 4 | +67.258 | +109.086 | +122.623 | 0 |
| XZ | 4 | -54.925 | +3.428 | +3.228 | 1 |
| FLAC | 4 | +7.888 | +10.305 | +24.848 | 0 |
| Graph500 | 4 | +9.888 | +0.008 | +0.003 | 0 |
| ZSTD | 8 | +67.457 | +135.905 | +157.330 | 2 |
| XZ | 8 | -22.328 | +5.109 | +6.276 | 0 |
| FLAC | 8 | +1.606 | +7.964 | +12.102 | 0 |
| Graph500 | 8 | +9.952 | +0.006 | +0.002 | 0 |
| ZSTD | 16 | +67.760 | +87.598 | +92.999 | 0 |
| XZ | 16 | -7.474 | +5.493 | +6.898 | 1 |
| FLAC | 16 | -4.340 | +6.141 | +4.895 | 0 |
| Graph500 | 16 | +9.569 | +0.008 | +0.002 | 0 |

| 核数 | CPI MAE | CPI MAPE (%) | L1 MAPE (%) | L2 MAPE (%) | L3 MAPE (%) | 平均主吞吐 (M user UOP/s) |
|---|---:|---:|---:|---:|---:|---:|
| 4 | 0.046404 | 5.825 | 34.990 | 30.707 | 37.675 | 1.713 |
| 8 | 0.044599 | 5.402 | 25.336 | 37.246 | 43.927 | 2.353 |
| 16 | 0.033512 | 4.187 | 22.286 | 24.810 | 26.198 | 2.224 |
| 全12例 | 0.041505 | 5.138 | 27.537 | 30.921 | 35.934 | 2.097 |

全12例CPI WAPE为5.447%，L1/L2/L3 MISS WAPE分别为17.757% /12.249% /12.082%。MAE/MAPE按case等权；CPI WAPE按共同宏指令人口加权周期，PMU WAPE为总绝对计数误差/总原生正例计数，不能互换。完整分位数、原始计数、unknown/estimated成员及单独成本见[12例详细报告](spr-private-preview-clock-retained-c4-c8-c16-validation-20261117.md)。

用户已明确允许忽略极少量原生unknown：XZ C4为1条、ZSTD C8为2条、XZ C16为1条（每个miss事件）。本表用原生已知正例作为点参考，实际unknown不清零；若全部未知均为miss，相对误差最大变化仅0.010243个百分点。模型完整 `estimated_count` 与模型unknown分别保留；原生参考即使unknown为零也仍有service-to-event估计。

CPI主要残差仍为Graph500 C4/C8低估0.126626/0.115317 cycles/macro；ZSTD多级miss高估仍明显，不能以CPI接近推断PMU机制已对齐。

## 证据与宿主口径

这些数值来自此前同输入12例完整回放，默认安装没有重新测量精度/吞吐。CPI使用维护的quick评分；PMU复用来源绑定的原生审计参考。全部输入身份、逐核宏/UOP/eligible-load人口及target identity分别核对；仍不是本次fresh strict原生PMU审计或硬件等价性认证。

主吞吐分子为measured user UOP；分母排除输入身份、准备/验证、初始化和功能warmup。完整输入中的kernel工作仍执行，主吞吐分子不与CPI宏指令分母互换。每例只有一次样本。CPU52–63位于socket/NUMA1，既有performance策略不变；C4/C8并行分别用52–55/56–63，C16独占52–63（**16个目标核，仅12个宿主CPU**）。启动频率快照3.8GHz，不代表持续频率遥测。不能据此宣布稳定吞吐验收或16宿主核扩展性。

## 默认使用与回退

实际来源配置应以默认alias为base，再覆盖该来源的mapping与thread-state plan。已有12例的等价来源配置保存在 `tmp/spr-m5-default-20261117/source-configs/`，可复查include切换后的全部设置。当前主机沿用GCC11运行库，命令局部设置 `LD_LIBRARY_PATH=/opt/gcc-11/lib64`；不修改全局shell或系统运行库。

```bash
source scripts/project_tmp.sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 python3 scripts/run_high_frequency.py -- \
  ./build/fastsim simulate \
  --config tmp/spr-m5-default-20261117/source-configs/c04-854.graph500_s.cfg \
  --manifest tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-854.graph500_s/tao_trace/manifest.txt \
  --cores 4 --measurement-scope user-plus-kernel --native-kernel-trace true \
  --allow-mmio-escape true --allow-cross-page-without-virtual-token true \
  --preload-validated-input true --output tmp/spr-m5-replay.json
```

真正恢复M4对照时，使用 `tmp/spr-m5-default-20261117/frozen-m4/fastsim`（SHA-256 `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`），配合以 `configs/spr-m4-device-io.cfg` 为base的原来源配置。仅将Estimated设为false不能撤销时间字段修复。若要全局回退，应同时恢复M4 ELF与alias；旧alias和入口备份在 `tmp/spr-m5-default-20261117/before/configs/`。M1-M4配置保持逐字节不变。

## 安装核验记录

本轮使用已测同一ELF，并重新运行对应的 `fastsim_tests`、parent-service与Estimated测试；实际C++ loader核对默认alias、维护入口及12例来源配置。安装后使用默认alias绑定的短Graph500完整输入检查运行和Estimated关闭状态；该短输入仅作默认入口烟测，不混入上述12例精度与吞吐聚合。

机器可读身份：[SPR-M5](../configs/spr-m5-private-preview-clocks.json)。备份、命令、测试、配置比较和安装后状态收据均位于 `tmp/spr-m5-default-20261117/`；最终结果见 `activation.json`。安装只推进默认版本，不升级未完成的精度、硬件或吞吐验收。

2026-11-18：按目标核数分配同数量物理核心的 M5 重测见 [C4/C8/C16 重测记录](spr-m5-c4-c8-c16-physical-host-20261118.md)。
