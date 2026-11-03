# FR-FCFS native默认与其他C8负载回放（2026-10-27）

后续用户要求暂不修复FLAC尾部计数差异，并忽略该极小偏差报告结果。包含FLAC的三负载对照另见[暂定结果报告](frfcfs-c8-provisional-results-20261027.md)；本文保留原严格检查状态和两案汇总。

**用户已明确选择FR-FCFS候选为维护native-FS默认。** 本轮将同一策略用于Zstd、Graph500、FLAC三组既有C8 FST，并用同一冻结二进制重跑旧模式对照。6次模型运行全部完成；参考数据一致性检查为 **2/3来源合格**，FLAC两模式均因既有原生指令计数不一致失败，不混入合格CPI/PMU汇总。Graph500改善，Zstd回退；默认变更是用户指定的近似档位选择，不宣称全负载精度验收通过。

## 默认、作用域与回退

维护入口 `configs/gem5-fs-native-kernel.cfg` 现在追加：

```ini
dram.scheduler = frfcfs
dram.frfcfs_topology_scaled_window = false
dram.frfcfs_selection_window = 0
dram.frfcfs_causal_selection = true
```

`0`使用实际`read_buffer_size`，本批三个来源均为64；不是无限队列或允许未来请求。旧版拓扑缩窗、nominal window8、causal=false可由`configs/gem5-fs-native-kernel-legacy-dram.cfg`恢复。旧实验/来源的冻结include链不改写。所有版本化配置与通用C++ DramConfig默认保持原样；维护入口的其他时延、store、branch开关不变。

既有`gem5-exp-shared-service-constraints.cfg`仅支持canonical旧DRAM路径，因此显式改为继承legacy-dram；未放松原C++支持范围。该兼容问题先用真实CLI复现失败，再修复并通过回归。

完整MemCtrl语义尚未认证：批次/队列准入、refresh及不支持路径回退等限制继续存在。`causal_selection`约束重建选择时刻可见的到达集合，并不单独证明所有端到端时序正确。

## 来源与执行身份

- 3个source均有完整8核FST和source-ready；使用原共同终点与完整warmup前史，没有重新采集、裁剪、补齐或改写源字节。Zstd为rate×8；Graph500/FLAC为各自原speed任务。SQLite/XZ完整来源门禁未合格，未以短源替代。
- 8核、3GiB DDR4单区间、8控制器、实际333 ticks/core cycle、buffer TSO114、L1D32KiB/L2 1MiB/LLC64MiB。各case硬件和thread-state plan按原final config.ini对齐。
- 所有新回放均使用本次维护默认的其他机制：store engine、prepare lifecycle、certificate、branch recovery均关闭。只改变DRAM策略。不能与历史legacy-prepare或bounded模型数字直接相减归因。
- 冻结FastSim SHA256：`911ac2738d3b58fd8e48c9a594ab45701d959ec41d26b7a37cdc4f62d4cdb973`。每次保存实际命令、output SHA、FST/companion/plan内容身份；同案旧/新身份相等，逐核宏指令、微操作、访存微操作和分支数量相同。
- 三案独立硬件身份检查有效；6份preflight及6份实际输出均由维护`audit_fs_profile_identity.py`核对130项直接参数匹配。直接参数匹配不替代动态调度或硬件等价资格。
- case worker并发3，每个case顺序跑旧/新，CPU组48–56、64–72、80–88，同NUMA node1。随后分别用冻结维护native入口`--source-root --stats-dir --expected-cases 1`重评；rescoring不独立认证原二进制，本轮另以实际命令和输出哈希/冻结文件补齐执行链。

| 来源 | 测量user UOP | workload-task macros | 旧/新原生门禁 |
|---|---:|---:|---|
| c08-777.zstd_r | 78,550,027 | 41,704,170 | passed / passed |
| c08-854.graph500_s | 79,593,563 | 39,858,393 | passed / passed |
| c08-817.flac_s | 77,126,512 | 35,803,108 | failed / failed，原生线程指令计数不一致 |

## CPI：合格结果

口径为workload-task active cycles / 同窗task宏指令。两个模式的gem5参考及分母相同；不是全核CPI算术均值。

| 来源 | gem5 CPI | 旧CPI | 旧相对误差 | 旧CPI绝对误差 | 新CPI | 新相对误差 | 新CPI绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| zstd | 1.525718171 | 1.428348700 | -6.3819% | 0.097369471 | 1.414117557 | -7.3146% | 0.111600614 |
| graph500 | 2.056529273 | 1.863511557 | -9.3856% | 0.193017716 | 1.944102664 | -5.4668% | 0.112426609 |
| flac | 不可作合格评分 | — | — | — | — | — | — |

CPI绝对误差单位均为cycles/宏指令；单案MAE等于其绝对误差。FLAC的原生owner1621（core0）记录4,439,946 macros、7,881,399 UOP、293,560 branches，独立FST扫描为4,439,947 / 7,881,405 / 293,561。两个模式同样复现；不修改oracle、不缩小task集合或窗口。其模型运行数值保存于原始JSON，但不发布为合格精度。

### 两个合格来源的汇总（覆盖2/3，不代表完整集合通过）

| 模式 | CPI MAPE | CPI MAE（cycles/宏指令） | CPI WAPE | signed bias | APE P50 | P90 | P99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy | 7.8837% | 0.145193594 | 8.0729% | -8.0729% | 7.8837% | 9.0852% | 9.3556% | 9.3856% |
| frfcfs | 6.3907% | 0.112013612 | 6.2743% | -6.2743% | 6.3907% | 7.1298% | 7.2962% | 7.3146% |

MAPE/MAE/APE百分位按case等权；WAPE和signed bias按有效task宏指令加权；百分位为Type-7。两案都低估CPI：Graph500的新CPI向参考靠近，Zstd更低。不能宣称每个负载都改善，也不按负载误差方向选择性开关默认。

## 实际激活与请求诊断

| 来源 | 旧/新有效选择窗口 | 新candidate epochs | 新重排请求 | 新fallback epochs |
|---|---:|---:|---:|---:|
| zstd | 1/64 | 5,592 | 955 | 0 |
| graph500 | 1/64 | 8,553 | 707 | 0 |
| flac | 1/64 | 178 | 0 | 0 |

因此新默认确实进入选择器；FLAC本窗口没有改变请求排序，模型周期也相同，但其来源仍未合格。

请求诊断仅取维护native validator所选Ruby/SLICC summaries和原生分支来源，禁止使用kernel-events path-class cache字段。它们是proxy/diagnostic，不替代SPR退休load事件。

| 来源 | 事件 | gem5 | 旧模型 | 旧相对误差 | 新模型 | 新相对误差 |
|---|---|---:|---:|---:|---:|---:|
| zstd | branch_misses | 343,171 | 342,023 | -0.3345% | 342,023 | -0.3345% |
| zstd | l1d_tag_misses | 1,506,244 | 1,340,015 | -11.0360% | 1,340,012 | -11.0362% |
| zstd | private_l2_tag_misses | 335,247 | 393,612 | +17.4096% | 392,781 | +17.1617% |
| zstd | llc_tag_misses | 71,314 | 95,237 | +33.5460% | 95,237 | +33.5460% |
| graph500 | branch_misses | 1,571 | 1,327 | -15.5315% | 1,327 | -15.5315% |
| graph500 | l1d_tag_misses | 714,202 | 714,172 | -0.0042% | 714,185 | -0.0024% |
| graph500 | private_l2_tag_misses | 356,365 | 356,323 | -0.0118% | 356,314 | -0.0143% |
| graph500 | llc_tag_misses | 355,360 | 355,447 | +0.0245% | 355,447 | +0.0245% |

退休L1/L2/L3 load-miss主精度本轮不评分：原来源存在非零native unknown（下表仅为来源资格诊断，未将其作为完整参考评分）。FLAC另有指令范围与数量检查失败。

| 来源 | native L1 miss unknown | native L2 miss unknown | native L3 miss unknown |
|---|---:|---:|---:|
| zstd | 145,605 | 387,747 | 387,905 |
| graph500 | 2,004 | 4,179,613 | 4,179,631 |
| flac | 141,479 | 225,038 | 225,055 |

## 并行运行耗时与验证

**以下为本次并行观测，不能据此判断吞吐提升或下降。** 原Marian固定NUMA串行三次测试才是该单案吞吐证据，见[吞吐对照](marian-c8-frfcfs-throughput-20261027.md)。

| 来源 | 模式 | warmup秒 | 测量秒 | 进程总秒 |
|---|---|---:|---:|---:|
| zstd | legacy | 7.979 | 24.068 | 90.105 |
| zstd | frfcfs | 8.374 | 24.451 | 90.700 |
| graph500 | legacy | 7.932 | 22.736 | 88.680 |
| graph500 | frfcfs | 8.357 | 23.896 | 90.285 |
| flac | legacy | 19.031 | 13.731 | 131.152 |
| flac | frfcfs | 18.784 | 13.806 | 130.682 |

- 3项真实CLI配置测试通过：维护默认选择、旧策略回退、canonical-only shared-service实验可运行；前两项有RED/GREEN记录，兼容项另有独立失败复现。
- 旧策略回退与改前冻结native配置的有效configuration、task/scope metrics（排除host throughput）、time accounting、逐核/线程cycles及CHA计数一致。
- `cmake --build build -- -j16`及`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`通过。一轮fresh只读审查无剩余Important/Critical问题。
- 版本化历史profiles、C++通用默认、采集端和源FST未改。未提交或推送；硬件等价仍unverified。

原始证据：`tmp/c8-frfcfs-default-20261027/`中的`freeze.json`、`cases.json`、`campaign.json`、`completed.json`及`summary.json`；逐case/mode的命令、receipt、实际参数audit与维护validation.json均保留。计划与取舍见`docs/superpowers/plans/2026-10-27-frfcfs-default-c8-replay.md`。

后续已将FLAC计数差异定位到共同终点截断syscall；同一旧10M样本在10月23日就失败。10月26日另有合格2M样本，本轮复用了旧窗口，不能混淆两份数据。见[具体边界与来源更正](flac-c8-count-boundary-diagnosis-20261027.md)。
