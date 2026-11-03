# Marian C8 / 32GiB / 10M FastSim 同源回放误差（2026-10-27）

2026-10-28 存储状态：本文误差数值保留为历史结果；用户已授权回收对应早期Marian
C8来源的FST及专属旁表。配置、统计和报告仍在，但来源已标记
`retired-not-replayable`，无法再原样回放。当前四负载矩阵中的Marian C8是另一次采集，
不能直接套用本文误差。删除凭据见[回收收据](../tmp/fst-retirement-20261028/receipt.json)。

已完成这组FST的完整warmup＋共同测量窗口回放。主口径为 **workload-task active CPI**：gem5 **2.622350**，FastSim **3.034149**，相对误差 **+15.7034%**，绝对误差 **0.411799 cycles/宏指令**。本案CPI MAE同为 **0.411799**。FastSim偏高；这不是精度验收通过或真机性能结论。

## 来源、配置与门禁

- 来源：`tmp/spec-marian-c8-32g-10m-20261027/source/c08-872.marian_s`；官方第一条refspeed Marian命令，单应用、8个计算worker，保留辅助线程。最快核10M measured user UOP触发全核共同终点；八核实际测量user UOP合计72,960,546。
- 主CPI的宏指令分母为 **39,127,055 workload-task macros**，两侧相同；核0的4,204条other-task macros不混入。另逐核核对宏指令、UOP、memory UOP和branch人口均一致。
- 8核、3.003003003GHz有效时钟（333 ticks/core cycle）、ROB192、LQ32、SQ/TSO buffer114；L1D32KiB、私有L2 1MiB、LLC64MiB，LRU。
- 实际RAM32GiB：低段[0,3GiB)、高段[4GiB,33GiB)，每段8个独立DDR4通道，总16个控制器；使用显式`dram.memory_layout=x86-low3g-high4g-v1`。所有DRAM时序按实际config.ini向上取整转换，无负载拟合。
- 使用维护的`gem5-fs-native-kernel.cfg`冻结include链；依据采集设置buffer114并关闭与buffer不兼容的`store_wait_relief`。未开启bounded analytic、prepare-lifecycle或branch-recovery实验。
- FastSim SHA256：`911ac2738d3b58fd8e48c9a594ab45701d959ec41d26b7a37cdc4f62d4cdb973`。生产gem5 SHA256与采集配置身份见[来源记录](marian-c8-32g-10m-collection-20261027.md)。
- 独立`validate_fs_oracle_identity.py`有效；正式FastSim输出经`audit_fs_profile_identity.py`核对 **130/130直接参数匹配**。维护native验证 **预期1案、发现1案、通过1案、失败0案**；含原生drain、守恒和人口门禁，不代表CPI/PMU阈值验收。

## CPI与测量结束时间

| 口径 | gem5 | FastSim | 相对误差 | 绝对误差 |
|---|---:|---:|---:|---:|
| task active CPI | 2.622349880 | 3.034149261 | +15.7034% | 0.411799381 cycles/宏指令 |
| common-window elapsed | 4.271380010 ms | 4.941900819 ms | +15.6980% | 0.670520809 ms |

单案case-equal MAPE=15.7034%，CPI MAE=0.411799381 cycles/宏指令；宏指令权重WAPE=15.7034%，signed bias=+15.7034%；APE P50/P90/P99/max均为15.7034%。这里只有1案，不构成多负载误差分布。主CPI是各核active cycles求和除以task macros总和，不是各核CPI算术均值。

| 核 | task macros | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差（cycles/宏指令） |
|---:|---:|---:|---:|---:|---:|
| 0 | 4,838,059 | 2.649008 | 3.066007 | +15.7417% | 0.416999 |
| 1 | 4,766,334 | 2.691160 | 3.113618 | +15.6980% | 0.422458 |
| 2 | 4,837,814 | 2.651397 | 3.067613 | +15.6980% | 0.416216 |
| 3 | 4,827,338 | 2.657151 | 3.074271 | +15.6980% | 0.417119 |
| 4 | 4,812,507 | 2.665340 | 3.083745 | +15.6980% | 0.418405 |
| 5 | 4,802,511 | 2.670888 | 3.090163 | +15.6980% | 0.419276 |
| 6 | 4,957,609 | 2.587329 | 2.993488 | +15.6980% | 0.406159 |
| 7 | 5,284,883 | 2.427105 | 2.808112 | +15.6980% | 0.381007 |

## 原生请求诊断PMU

这些cache字段来自守恒的Ruby/SLICC native controller summaries，经维护validator选择；未使用kernel-events中的path-class cache字段作真值。它们是请求/tag诊断，不能替代退休load-miss硬件事件。branch/L1D/L2映射仍为proxy，LLC为diagnostic。

| 诊断事件 | gem5 | FastSim | 相对误差 | 绝对计数差 |
|---|---:|---:|---:|---:|
| branch_misses | 36,880 | 36,880 | +0.0000% | 0 |
| l1d_tag_misses | 843,009 | 670,055 | -20.5163% | 172,954 |
| private_l2_tag_misses | 317,723 | 553,513 | +74.2124% | 235,790 |
| llc_tag_misses | 309,219 | 326,150 | +5.4754% | 16,931 |

Branch-miss全案计数相等不代表逐核/逐事件预测相等，也不证明恢复时序正确。私有L2请求诊断误差较大，需先做同源事件/路径诊断；本轮未据此调整模型。

## 退休缓存PMU资格

**完整资格的L1/L2/L3退休load-miss主误差不可用。** 参考与模型各有2个未知归属，位于kernel并计入workload-task。维护`compare_retired_cache(..., misses_only=True)`已实测拒绝：`native retired-cache unknown membership: core=3 scope=physical event=retired_l1_load_misses unknown=1`。不删除内核、不缩小参与人口、不将unknown置零。

以下仅为维护工具输出的 **incomplete-evidence计数差**，不计入完整资格主误差或准确率验收：

| 退休事件 | gem5已分类计数 | FastSim已分类计数 | 条件计数差% | gem5/model unknown |
|---|---:|---:|---:|---:|
| retired_l1_load_misses | 98,146 | 99,086 | +0.9578% | 2/2 |
| retired_l2_load_misses | 25,175 | 25,583 | +1.6207% | 2/2 |
| retired_l3_load_misses | 20,676 | 20,797 | +0.5852% | 2/2 |

退休load/store人口分别11,720,798和5,593,312，两侧相同；这不消除缓存事件的未知归属或硬件语义资格缺口。

## 墙钟与限制

- FastSim内部计时：功能warmup **6.893s**，测量 **25.933s**，合计 **32.825s**。维护入口包含来源扫描、输入绑定、回放及评分的墙钟 **497.566s**，两者口径不同；没有单独记录FastSim子进程完整端到端墙钟。
- 内部测量阶段约 **2.813 M measured user-UOP/s**、**1.505 M measured user-macro/s**。仅为本次观测，没有quiet-host、重复试验或吞吐验收。
- 原100条warmup syscall的arguments仍unknown，stat内syscall为0；初始PTE snapshot应用root资格仍unknown。回放采用native内核、关闭合成syscall/page-fault/IRQ服务，不将这些字段宣称为已补齐。
- 合成I-side映射、未记录的wrong-path/prefetch影响、紧凑Ruby网络/目录和DRAM模型近似仍存在。`hardware_equivalence=unverified`，不报告SPEC完整分数或SPR真机认证。
- 新DRAM映射选项默认contiguous；边界/空洞、两控制器实现、区域独立性、合成地址及默认回归通过。有效目标9项、profile2项及TSO7项Python测试通过；完整C++测试通过。
- 首次冻结目录缺少流哈希helper，验证在仿真前失败，保留`*.setup-failed`；补齐并SHA绑定后完成正式运行。没有改动FST或再次采集。

## 复现与原始证据

实验目录：`tmp/marian-c8-32g-10m-replay-20261027/`。执行`run_replay.py`调用冻结的维护入口；完整参数在`command.json`，配置在`fastsim.cfg`与`frozen-configs/`，实际参数来源在`parameter-provenance.json`。正式结果在`validation/summary.{json,md,csv}`及`validation/cases/c08-872.marian_s/{fastsim,validation}.json`。`result.json`为该维护结果的可读汇总；`profile-actual/summary.json`、`identity-live.json`、`strict-retired-cache-gate.json`和`freeze.json`分别保存独立参数/来源/资格/内容身份证据。

大内存回放选项见[runbook第13节](gem5-x86-fs-large-memory-runbook-20261026.md#13-fastsim-回放双段-ram2026-10-27)。本轮不提交或推送。

后续已完成[本组FST的CPI误差来源诊断](marian-c8-32g-10m-cpi-diagnosis-20261027.md)：同源控制显示DRAM选择窗口退化及store生命周期近似有明确影响；候选组合仍有+7.8634%差值、CPI绝对误差/单案MAE0.206206 cycles/宏指令。诊断未更改本报告的原baseline或默认模型。
