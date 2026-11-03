# DRAM 同流控制器差分与投影流实验（2026-11-02）

## 结论

本轮实现独立 gem5 控制器 oracle，完成13类微型流的52次参考运行、两案候选流的
8次参考运行，以及两案投影流的4次参考运行。没有改动 gem5 全系统 runner、重采
FST、修改默认模型时序、拟合参数或引入全局迭代，没有 commit/push。

1. **当前整体误差不能直接归咎于 FastSim 控制器的基础读服务规则。** 固定同一条
   到达流、使用同一套整数时序且关闭刷新后，FastSim 与 gem5 的 arrival→data
   均值之差：Marian **+0.776**、G500 **−0.003 cycles**。Marian 匹配到的
   203,119条 store 触发读为 **+2.149 cycles**。这些是均值诊断，不是控制器完全
   等价或 CPI 因果份额的证明。
2. **时间轴选择有实测影响。** 固定 gem5 原生参数控制器，将 demand read 按修正
   issue 平移后重放，Marian 同成员均值 **134.541→108.867**，G500
   **122.814→134.187**。两案分别向原生工作负载的92.672、145.483靠近。
   因而到达流近似确实是误差候选，不能仅凭“吞吐量要求”认定这部分损失不可改善。
   但它仍是开环敏感性实验，不是 CPU/cache/MSHR 联立求解后的结果。
3. **刷新遗漏是另一条独立、已激活的机制差异。** 在候选流、整数参数下，开启
   gem5 刷新使均值增加 Marian **16.128**、G500 **31.451 cycles**。它会掩盖
   Marian 的部分偏慢，并加重 G500 的偏快。不能用一个固定补偿量代替刷新状态。
4. **取整不是几十周期正残差的主要来源。** 候选流上测得取整增量分别为
   **4.827、2.746 cycles**；其与刷新存在交互。参数全部向上取整也不保证请求
   延迟全部增加，写 drain 微型流提供了反例。
5. **确有局部语义缺口，但不能直接推广为本轮 CPI 修复。** 微型流复现 row-cap
   多一次 PRE，以及写 drain、写合并、写队列转发的差异。已有单次 PRE 候选曾使
   G500 CPI 绝对误差变差超过0.05，继续保持 opt-in。

本轮纠正一个重要口径：旧文的 FastSim arrival→data **137不是正确值，应为
124.017**。额外13cycles是 LLC fill return8 + NoC5，不能再算成 DRAM 总线等待。
因此 Marian 对原生92.672的控制器段差距为 **31.345**，不是44.3。

## 实施和来源

- `tools/gem5_controller_oracle.py`：复制捕获配置中的16个 MemCtrl/DRAMInterface，
  保留低3GiB/高4GiB以上两段范围及交织 mask/match；读取全部可复制标量参数，
  保存 source/effective 字段清单并核对运行后 config.json。
- 每个控制器直接接 PyTrafficGen 与 CommMonitor，避免另外引入 XBar 仲裁。
  TraceGen 按精确 tick 开环提出请求；分别记录实际接收、响应和 retry。
- `tools/run_controller_differential.py`、`tools/controller_oracle_common.py`：生成
  输入、运行四档参考、解码包迹、核验人口/配置/输入哈希，保留分布统计。
- `tools/analyze_controller_differential.py`：使用明确指定的同窗 native stats，输出
  分组、分布和差分，绑定全部消费的 oracle 产物；不选择 native cache PMU 参考，
  不替代维护版 validator，不根据控制器均值生成 CPI。
- `diagnostics.committed_dram_path`：默认关闭，导出最终被接受的 DRAM 事件。
  原 `projected_dram_path` 的部分时间戳早于最终反馈修复，不能直接当成实际最终流。
  本轮新增导出逐位守恒最终控制器的读数、arrival/command/data时间戳总和。
- 导出可用的 corrected issue 与 `projected_arrival`；不可用时保留原轴并显式标记。
  新字段随逐核反馈替换一起转移，避免 retry 后保留旧投影。

参考继续使用：

```
tmp/common-end-stats-20261030/dynamic-retry1/source/c04-872.marian_s/
tmp/common-end-stats-20261030/dynamic-retry1/source/c16-854.graph500_s/
tmp/common-end-replay-20261030/frozen/{marian,g500}-p1.cfg
```

实验根目录：`tmp/controller-differential-20261102/`。正式结果为 `micro-final/`、
`marian-final/`、`g500-final/`、`marian-projected/`、`g500-projected/`；`micro/`、
早期 projected stream、watchdog失败及首次缺投影覆盖的导出保留为开发记录。
`binding-validation.json`核验64次成功参考的来源/请求哈希和逐字段配置回读。
早期驱动版本保存在实验目录；更新后的后处理与原始模拟命令、配置清单分别记录。

控制器单独冷启动后注入已有 FastSim warmup，随后在原 FS warmup barrier reset统计。
它并不继承原全系统 checkpoint 的控制器状态、刷新相位或所有上下文流量。因此
“同流 oracle 与原 native 的差”必须称为 **到达流/状态/人口残项**。

## E1：微型流

对照使用精确 native tick 参数、关闭 gem5 刷新。下表为读的接收→响应平均差；
写行只比较其中读响应，不把入队 write acknowledgment 当成 DRAM 写完成。

| 流 | FS−gem5（cycles/读） | 观察 |
|---|---:|---|
| row hit、row conflict、late row hit | 0 | 本组有限流一致 |
| bank parallel、rank switch、channel parallel | 0 | 本组激活的并行/命令约束一致 |
| adaptive conflict | 0 | 本组自适应判断一致，不覆盖所有队列组合 |
| row-cap：16次访问后冲突 | +2.501 | 17条读总差14,160ticks，恰为一次 tRP |
| read queue full | +4.983 | 在 gem5 实际接收流上比较；总差等于15次 tRP，逐命令归因尚未完成 |
| refresh boundary（关闭刷新） | 0 | 开启刷新后显著改变结果，确认刷新机制激活 |
| write drain | +407.459 | 写日历/drain近似差异 |
| repeated-address writes | +904.198 | 写合并与服务近似差异 |
| write→same-address read | +125.081 | gem5 从写队列转发，无物理 DRAM 读；不能比较 avgMemAccLat |

read queue full 的原开环流发生64次 read retry、64条延迟准入，故额外导出 gem5
实际接收流再送入 FastSim，避免把准入等待与控制器内部等待混在一起。真实两案的
12次候选/投影参考运行均为 **零延迟准入、零 read retry**。

写 drain 中 G1−G0 的 arrival→data 约 **−13.62 cycles**，接收→响应约
**−11.68 cycles**。静态延迟取整保持正方向，但调度/切换交互可以使总延迟下降。
因此原方案的“固定段+burst次数”不能当成通用逐请求量化误差上界。

E1尚不构成穷尽认证：未独立完成所有 FR-FCFS 窗口内外组合与可控的跨 epoch 分段
不变性试验；真实流对照包含生产 epoch 的后果，但不能替代该性质测试。

## E2：同一候选到达流，分离控制器差异

全部为 arrival→data，单位reference cycles；不含frontend/backend、LLC和NoC。
四档参考与 FS 的读人口分别均为403,404、712,663。请求/响应和物理服务的统计窗口
定义分别保留，计数相等本身不冒充同成员证明。

| 控制器/流 | Marian C4 | G500 C16 |
|---|---:|---:|
| 原 native 工作负载，同窗 | 92.671623 | 145.482939 |
| FS 最终候选流、生产控制器 | 124.016552 | 94.106607 |
| G0：同流 gem5、原参数、有刷新 | 134.541052 | 122.814492 |
| G1：同流 gem5、整数参数、有刷新 | 139.368300 | 125.560856 |
| G2：同流 gem5、整数参数、无刷新 | 123.240679 | 94.109393 |
| G3：同流 gem5、原参数、无刷新 | 118.803682 | 91.371903 |

按同一顺序相减：

| 分项 | Marian | G500 |
|---|---:|---:|
| FS−G2：紧凑控制器/epoch语义项 | +0.775872 | −0.002785 |
| G2−G1：遗漏刷新的有符号影响 | −16.127620 | −31.451464 |
| G1−G0：取整项 | +4.827248 | +2.746364 |
| G0−原native：到达流/初态/人口残项 | +41.869429 | −22.668447 |
| 合计：FS−原native | +31.344928 | −51.376331 |

四项相加是恒等账，最后一项不是纯到达流因果份额。反向消融的量化×刷新交互
分别为 **0.390251、0.008875 cycles**，不能把不同顺序的贡献直接相加，亦不能
把两种顺序的差称为所有交互的严格上界。

### Marian store 子集与250尖峰

用(core, source_sequence, ordinal, line)连接现有 `stores.bin`，匹配203,119条
测量段 store 触发的 DRAM 读。此为生命周期首 fragment 可匹配子集，不是所有
store miss；其 FS 整条 store 服务均值247.074，因此不能与所有 L2-miss OD 的
238.018混用。Marian全部输入没有重复(controller,address,cmd)，成员响应可唯一绑定。

| 同一store-read子集 arrival→data | cycles |
|---|---:|
| FS | 136.717658 |
| G0 | 141.036053 |
| G1 | 145.659747 |
| G2 | 134.568179 |
| G3 | 130.449276 |

FS−G2为+2.149479；四核分别+2.248、+1.621、+2.270、+2.514。最慢核core1
也没有出现控制器均值多50cycles的现象。但均值接近不能排除退休关键请求差异。

保留每条 FS store 的非控制器段，只替换同流 G2 控制器接收→响应后，得到一个
**冻结路径混合诊断**：250cycles占比40.953%→41.013%，207cycles占比约0.292%。
这支持尖峰随输入/行冲突结构出现，不能称作 gem5 原生 ST 服务分布或 CPI 预测。
global row-hit率高也不代表 store 子集的行命中率同样高。

### G500 的写人口和 FB 边界

| 测量段 | FS生产路径 | 同候选流G0 | 原native |
|---|---:|---:|---:|
| 写请求入队 | 485 | 485 | 1,727 |
| DRAM写实际服务 | 0 | 48 | 1,238 |
| 写转读次数 | 0 | 3 | 76 |

原native列为原工作负载，不能视为同成员差分。FS与同流oracle写服务也不同，但
G2读均值与FS相差仅0.003，故本轮不能以写微型流的大差直接解释G500总体读残差。
FB仍是“perf计数11,846,715、退休分类0、native约8.44M”的路径语义不一致；
本轮未改FB模型，不能重新写成“FS没有任何在途回填概念”。

## E3：固定 gem5，比较候选轴与投影轴

投影定义：

```
projected_arrival = corrected_issue + (final_arrival - shared_stage_issue)
```

其中store的corrected issue包含最终SQ send约束。这保留候选轴上已求出的共享路径
和MSHR相对等待，不重新求解cache/MSHR/CPU依赖；写回保持原轴。它用于有界敏感性
分析，不是实际运行时修复，也不声称存在唯一“真实顺序”。

Marian有403,042/403,404条测量读可投影，362条ifetch保留原轴；G500测量读
712,663/712,663条可投影。投影位移总和分别4,190,188,645和1,341,996,305，
与最终 `dram_request_lifecycle` 的有符号位移总和逐位一致。

| 固定原参数、有刷新gem5 | Marian | G500 |
|---|---:|---:|
| 候选流，同原始测量成员 arrival→data | 134.541052 | 122.814492 |
| 投影流，同原始测量成员 response减静态段 | 108.867024 | 134.187403 |
| 改变量 | −25.674028 | +11.372911 |
| 原native同窗 arrival→data | 92.671623 | 145.482939 |
| 行命中率：候选→投影→原native | 66.20%→58.77%→61.60% | 71.36%→51.45%→46.86% |
| 到达时平均读队列深度：候选→投影→原native | 2.731→0.332→0.476 | 1.244→1.079→1.844 |

G500时延与行局部性更接近，但队列深度没有更接近。因此不能声称投影轴在所有
维度都优于候选轴；queue latency还包含bank/行/命令约束，不等于队列条目数。

关闭刷新、保留原参数的两条投影流均值分别为88.453992、104.292616。
Marian store-read子集的G0候选→投影为141.036053→119.062328。

投影可让少量warmup读跨过原统计reset边界。Marian物理统计为403,414条，但
原始phase=1成员为403,404条；上表均值按原始成员身份重新选取，未把10条跨界
warmup读混入。分布来自原reset后的控制器统计，单独保留这一窗口差异。
G500原始phase=1成员与物理统计均为712,663条。native逐请求到达流没有现成产物，
本轮第三条native流仍只有同窗聚合分布，未额外采集。

## 当前修复决策

当前证据支持以下顺序，尚不支持宣布完整误差来源已闭合80%或推广新默认配置：

1. 优先设计**有界的一遍共享服务重定时**，把SQ demand的发出边界与共享控制器
   使用的边界对齐，并明确cache/MSHR/跨epoch状态的处理。到达流的顺序可以近似，
   但不能无条件把一条时间轴上的排队时长搬到另一条时间轴。不得绕过现有
   `bounded_analytic` 与 `interval_response_retime` 不兼容检查来“直接开B3”。
2. 单独设计按rank维护刷新可用时间与行关闭的机制，允许解析跳过空闲周期；
   使用配置的tREFI/tRFC，保留跨epoch状态，不给每个请求加一个拟合罚时。
   本轮观测到的16/31cycles不能作为运行时参数。量化若改进，使用通用子周期表示。
3. 保留已被微型oracle确认的单次PRE候选；写队列合并/转发和命令日历继续按明确
   所有权与实际服务拆分。它们有机制依据，但分别做A/B，不能靠误差抵消推广。
4. Marian仍缺同成员store完整服务的native边界与尾部关键性闭合；G500还需LQ
   占用/释放账及FB分类一致性。控制器均值不能替代这些CPI桥梁。

E4生产时序修复、两案CPI A/B、六项和吞吐验收尚未启动。吞吐≤3%约束不因离线
结果而自动满足。该决定是机制闭合门槛的结果，不是等待再次授权E0。

## 验证与结果边界

最终C++观测器与默认构建通过；`./build/fastsim_tests`完整通过，Python oracle
检查7项通过。候选与投影导出两案的全部现有目标字段均等于冻结P1；只排除明确
列出的宿主墙钟、吞吐和host调度计数，收据保存排除逻辑和二进制身份。

本轮没有新增CPI或吞吐结果。冻结P1如下，不能把独立控制器重放均值当作新CPI：

| case | gem5 CPI | FS CPI | 相对误差 | CPI绝对误差（cycles/宏指令） |
|---|---:|---:|---:|---:|
| Marian C4 | 4.937551 | 6.015521 | +21.83% | 1.077971 |
| G500 C16 | 2.303451 | 2.153629 | −6.50% | 0.149822 |

N=2：CPI MAE0.613896、MAPE14.17%、WAPE9.60%、signed bias−0.78%。
无新的makespan/各核周期和改变量；观测中立性要求两者均保持冻结值。

核心机器产物：`{marian,g500}-analysis.json`、
`{marian,g500}-projected-analysis.json`、`micro-analysis.json`、
`binding-validation.json`、`committed-projection-v2/*/receipt.json`。
原始QLat、MemAccLat、row hits、bytesPerActivate、rdQLenPdf、perBankRdBursts、
retry均保存在各次oracle的stats.txt及分析JSON；无样本分布显式缺项，不补零。
