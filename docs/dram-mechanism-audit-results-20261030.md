# DRAM 机制对齐审计与微型流结果（2026-10-30）

## 结论

本轮完成配置、地址解码、参数生效路径和微型流敏感性审计，随后用原冻结二进制
完成“单次预充电”两案同输入A/B。没有改动默认生产时序、拟合参数、启动全局迭代
或重采gem5；新增两案候选CPI结果，没有新的吞吐结论。

1. 当前两案没有发现基本 DRAM 拓扑、地址映射或已表示的时序参数配错。
   每案扩展检查 **561/561** 匹配，生产解码器的 **8,364** 个地址样本全部匹配
   gem5 的 AddrRange/DRAMInterface 解码规则。这是当前共同拓扑的样本验证，
   不代表所有配置或所有控制器行为等价。
2. “参数相等”仍不等于“执行路径覆盖”。当前紧凑 `DramModel` 的写服务复用
   读服务日历，没有使用已配置的七个写方向时序字段。微型流实际 drain 了96次写，
   逐一加倍这七个字段，所有读响应仍完全相同。源码检查确认该路径未读取这些字段；
   另一个 mixed 控制器有写方向时序实现，但不能据此认为当前路径已覆盖。
   当前 Marian/G500 模型窗口的写 drain 都为0，但native G500实际有1,238次写服务。
   因此配置字段未生效与写流量/drain策略必须分开检查，尚不能归因主要残差。
3. 复现了已有、默认关闭的“行访问上限只预充电一次”机制修正的适用反例。
   行上限已触发 PRE 后，旧路径仍执行 adaptive PRE，额外推进一次 tRP。
   当前参数下两个64请求反例最后响应均相差 **43 cycles**。这不是新发现的修复代码，
   局部43cycles不等于完整负载暴露的成本；后续A/B测量的是整套模型的响应变化。
4. 当前 selection window 为完整物理读队列容量；本轮微型流启用 full-queue
   page-policy 后没有变化。不能因为这个开关默认关闭就把它列为当前主因。
5. 请求到达密度、同 bank 行交替确实可以改变服务时间，但敏感性依赖请求流。
   不能从候选→修正平均位移直接推出排队偏差，当前仍没有证据要求全局迭代收敛。
6. 单次PRE候选使Marian CPI减少0.002251，G500减少0.072372；两案CPI MAE从
   0.613896升至0.648957。G500本来偏快，局部规则纠正后误差变大，说明有误差补偿。
   规则有源码依据，但不能作为当前完整精度修复推广；默认配置保持不变。

## 实施内容与来源

- `tools/audit_dram_mechanisms.py`：逐控制器检查拓扑、读写时序、队列参数、
  页策略族与显式刷新缺口；绑定配置、预测、脚本、probe二进制和参考源码哈希。
- `tools/probe_dram_mechanisms.cpp`：调用生产 `DramModel` 解码器与既有测试接口，
  不另写一个模型冒充生产路径；输出完整有效 DRAM 配置，审计要求与被评分配置一致。
- `include/fastsim/simulator.hpp`、`src/simulator.cpp`：增加仅测试构建可见的只读
  地址解码接口。原生产解码及服务逻辑未变。
- `tests/test_dram_mechanism_audit.py`：8项检查覆盖第二控制器不一致、缺项、
  刷新缺口、未知映射、bank/rank/row、XOR交织、probe配置漂移及写接收/服务区分。
- `tests/test_dram_address.cpp`：验证实际生产解码器的高地址区 channel/bank/rank/row。

继续使用 `tmp/common-end-stats-20261030/dynamic-retry1/source/` 的 Marian C4、
G500 C16，配置和预测来自 `tmp/common-end-replay-20261030/` 的 P1。
本工具补充维护版 `audit_fs_profile_identity.py`，不选择 native PMU 参考，
不替代 native validator，也没有对原始 miss 计数另行评分。

最终收据：

- `tmp/memory-causality-20261030/results-marian/summary.json`
- `tmp/memory-causality-20261030/results-g500/summary.json`
- `tmp/memory-causality-20261030/implementation.json`

早期 `audit-*` / `final-*` / `verified-*` / `complete-*` 为开发阶段收据，
最终审计结果以 `results-*` 为准。A/B结果见 `single-precharge/analysis.json`。

## 参数与刷新边界

两案16个控制器均为 RoRaBaCoCh、open_adaptive，2 rank/channel、16 bank/rank、
4 bank group/rank、8 KiB行缓冲；RAM分为低3 GiB和高地址区，不能按16个通道
在两个区域之间统一交织。解码审计直接读取每个AddrRange的选择掩码与match，
独立计算去交织地址，再解码bank/rank/row，与生产解码结果比较。

各控制器 tCL/tRCD/tRP=14,160 ticks；参考周期333 ticks，保守取整为43 cycles。
包括 tCWL、tRCD_WR、tCCD_L_WR、tRTW、tWTR、tWTR_L、tWR 的配置值均匹配。
所有控制器的 frontend/backend、读写容量和阈值也纳入扩展检查。

这里的匹配指“按既定规则换算后相等”，不是精确tick时序等价：tCL约42.5225cycles
取43，tBURST=3,332ticks约10.0060cycles取11，静态frontend/backend各约30.0300
取31。尤其burst长度的取整可能影响带宽约束；这仍是需要量化的近似边界，不能把
130项或561项匹配表述为完全消除参数量化误差，也没有据此改用拟合后的常数。

gem5 tREFI=7,800,000 ticks、tRFC=350,000 ticks；紧凑模型无完整rank刷新状态。
350,000/7,800,000≈4.49%只是标称时长比例，不是测得的排队或CPI损失，不能作为
统一延迟罚时。刷新还涉及rank可用性、PRE与行状态，需单遍状态约束而非固定补偿。

## 固定微型请求流

每流64次读；dense间隔1cycle，sparse间隔为当前 tRP+tRCD+tCL+tBURST=140cycles。
这些间隔只用于敏感性试验，不写入生产参数。grouped/alternating两流地址多重集
完全相同，只改变这些地址与到达时刻的对应。

| 请求流 | dense 平均arrival→command | sparse 平均arrival→command |
|---|---:|---:|
| 两行分组 | 676.50 | 2.69 |
| 同bank两行交替 | 782.78 | 85.33 |
| 分散到不同bank | 433.47 | 10.75 |
| 分散到不同channel | 71.00 | 5.38 |

单位cycles。arrival→command包含PRE/ACT等服务前约束，不能全称为纯队列等待。
这是冷启动控制器的有限微型流，不是稳态容量标定，也不是Marian/G500的请求重放。
其结果证明敏感性可以存在，**不证明真实负载的候选顺序有同样大的误差**。

行上限单次PRE变体相对基线：dense alternating流command-wait总和减少645cycles，
dense grouped流减少1,376cycles，二者最后响应均提前43cycles；sparse流无此差异。
源规则是 gem5 `DRAMInterface::doBurstAccess` 在 `auto_precharge` 已成立时跳过
adaptive分支；FastSim已有 `dram.frfcfs_row_cap_single_precharge` 实现并有回归测试，
本轮保持其默认和既有基线配置不变，另建显式启用该规则的候选配置做A/B。

## 运行时激活与修复优先级

现有P1模型统计中，Marian write enqueue/drain=0/0；G500为485/0，尾部485项待写。
补充检查同窗native物理计数：

| case | MemCtrl接收写burst | DRAM实际服务写burst | 写转读次数 | FS enqueue/drain |
|---|---:|---:|---:|---:|
| Marian | 0 | 0 | 0 | 0 / 0 |
| G500 | 1,727 | 1,238 | 76 | 485 / 0 |

接收数取MemCtrl `writeBursts`，服务数取DRAMInterface `dram.writeBursts`，不能混用。
模型内无drain只能说明修改写时序字段不会直接生效，不能据此推断native没有写竞争。
G500还需要区分流量人口差、后台drain条件与写方向日历；双方物理流量人口并非逐请求
配对，尚未测出这部分暴露在退休上的成本。
Marian/G500有7,006/19,149次row-cap PRE；该数不等于重复PRE次数，尚不能乘43
当作可修复周期。FR-FCFS stable/fallback epoch分别为4,368/0与9,933/207。

后续顺序应为：

1. 已完成single-PRE两案同输入A/B（见下节），目前不推广默认。候选保留，
   后续与刷新、写队列及回填机制联合评估，不据此宣称精度修复完成。
2. 对刷新补齐rank生命周期和行关闭约束的独立微型差分，先验证局部机制；
   不做全局核心—内存迭代，不统一追加tRFC。
3. 先对齐G500的写流量人口和后台drain条件，再验证写方向日历。不能因发现缺项就
   认定它解释当前误差，也不能仅凭模型drain为0排除原生写竞争。
4. 只有完成这些明确机制检查后，再测当前负载对到达排列的敏感性；必要时采用
   有界队列或解析近似，不以恢复唯一真实请求顺序为目标。

## 验证

`cmake --build build -- -j16` 成功；最终probe目标重新编译成功；
`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 输出
`all FastSim tests passed`。新增Python审计测试8/8、既有DRAM参数身份测试2/2通过。
两份最终审计均返回0，且完整probe配置与被评分报告配置相同。

默认测试命令第一次因宿主旧libstdc++无法加载，使用该工作区已有GCC11运行库后运行；
地址probe回归第一次暴露测试夹具默认只有1 rank，随后显式设为2 rank并通过。
这些开发日志保留在证据目录。没有将微型流结果表述为新的模型精度或吞吐收益。

独立只读审阅发现写统计检查原来只验证控制器数量；现已按配置中的数值ID集合
完整核验，兼容mem_ctrl00/mem_ctrl0命名，错误ID替换的回归通过。补充审阅确认
该修复、最终收据和文档边界，无剩余可操作问题。

## 单次PRE候选：两案完整A/B

二进制继续使用原冻结 `fastsim-diagnostics`（SHA256
`97a98b253d0c8b67520bd79f5d3cffbcfb59dc5010f1c2ea6f418c96a413f525`），
源FST、oracle、thread-state.plan及其他配置不变。P1基线关闭PTE hierarchy walk；
候选唯一有效配置差异为 `dram.frfcfs_row_cap_single_precharge=true`。
两次候选重放完成，直接参数身份各130项匹配；维护版入口以 `--source-root`、
`--stats-dir`、`--retired-cache` 评分两案，来源/人口核对均完成。
本表是模拟器间workload-task active CPI，不是硬件PMU或吞吐认证。

| case | arm | gem5 CPI | FS CPI | 相对误差 | CPI绝对误差（cycles/宏指令） |
|---|---|---:|---:|---:|---:|
| Marian C4 | P1基线 | 4.937551 | 6.015521 | +21.83% | 1.077971 |
| Marian C4 | 单次PRE候选 | 4.937551 | 6.013270 | +21.79% | 1.075719 |
| G500 C16 | P1基线 | 2.303451 | 2.153629 | −6.50% | 0.149822 |
| G500 C16 | 单次PRE候选 | 2.303451 | 2.081257 | −9.65% | 0.222194 |

| N=2 | CPI MAE | MAPE | WAPE | signed bias | P50/P90/P99/max APE |
|---|---:|---:|---:|---:|---|
| P1基线 | 0.613896 | 14.17% | 9.60% | −0.78% | 14.17% / 20.30% / 21.68% / 21.83% |
| 单次PRE候选 | 0.648957 | 15.72% | 12.10% | −3.29% | 15.72% / 20.57% / 21.67% / 21.79% |

MAE/MAPE为case-equal；WAPE/bias按已核对的task宏指令数加权；N=2百分位只是
Type-7两点插值，不是六项或尾部泛化验收。

| Δ候选−基线 | Marian | G500 |
|---|---:|---:|
| active cycles | −21,256 | −5,783,232 |
| makespan | −5,314 | −361,452 |
| 各核报告周期之和 | −19,112 | −1,539,041 |
| FS控制器读burst总数 | 403,404→403,404 | 712,663→712,663 |
| FS平均arrival→service | 70.0166→70.0529 | 40.1066→37.9564 |
| 报告周期最慢核 | core1→core1 | core15→core4 |

两案均精确满足active变化的makespan账。G500的16核放大及最慢核切换说明，不能将
这次active变化当作“重复PRE次数×43”的加性成本。读burst总数不变也不证明逐请求
到达与服务身份不变。Marian控制器均值略升而CPI略降，进一步否定以控制器均值直接
推断CPI收益。

G500绝对CPI误差增加0.072372，超过此前0.05的暂停界限；本轮停止推广该候选，
不进入六项或吞吐扩展。局部规则正确性与整套近似模型的精度验收分开保留。
这次实验不支持“主要误差来自DRAM基本参数配错”，也没有证明“必须恢复真实顺序”。
下一步应先补齐刷新及写流量/drain证据，再与回填关键路径诊断联合设计通用修复。

候选输入冻结、逐次命令和退出码、有效配置差异、逐核变化及维护版评分均保存在
`tmp/memory-causality-20261030/single-precharge/`。并发/亲和性与原运行条件不同，
宿主wall时间不用于吞吐比较。本轮无commit或push。

## 复现配置与解码审计

开启测试构建时提供 `fastsim_probe_dram_mechanisms` 目标；它只用于离线审计。
以下命令读取现有来源和预测，输出目录必须尚不存在：

```bash
LD_LIBRARY_PATH=/opt/gcc-11/lib64 python3 tools/audit_dram_mechanisms.py \
  --gem5-config tmp/common-end-stats-20261030/dynamic-retry1/source/c04-872.marian_s/config.ini \
  --fastsim-report tmp/common-end-replay-20261030/runs/marian-p1/c04-872.marian_s.json \
  --fastsim-config tmp/common-end-replay-20261030/frozen/marian-p1.cfg \
  --probe build/fastsim_probe_dram_mechanisms \
  --common-end-stats tmp/common-end-stats-20261030/dynamic-retry1/source/c04-872.marian_s/stats-common-end.txt \
  --output tmp/dram-mechanism-recheck-marian
```
