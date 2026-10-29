# 退休 load cache 口径与八通道重采集

用户要求：FastSim 以已提交指令流为输入，主要 PMU 应选择匹配该输入的退休事件；
删除过时大数据，新采集使用八通道，并控制详细 warmup 时间。

## 指标合同

主输出为独立 `scope_metrics.retired_cache`，与原请求类 `perf_cache` 分离。

| 新字段 | SPR 真机事件 | raw 编码 |
|---|---|---|
| retired_loads | MEM_INST_RETIRED.ALL_LOADS | D0/81 |
| retired_stores | MEM_INST_RETIRED.ALL_STORES | D0/82 |
| retired_l1_load_hits | MEM_LOAD_RETIRED.L1_HIT | D1/01 |
| retired_l1_load_misses | MEM_LOAD_RETIRED.L1_MISS | D1/08 |
| retired_load_fb_hits | MEM_LOAD_RETIRED.FB_HIT | D1/40 |
| retired_l2_load_hits | MEM_LOAD_RETIRED.L2_HIT | D1/02 |
| retired_l2_load_misses | MEM_LOAD_RETIRED.L2_MISS | D1/10 |
| retired_l3_load_hits | MEM_LOAD_RETIRED.L3_HIT | D1/04 |
| retired_l3_load_misses | MEM_LOAD_RETIRED.L3_MISS | D1/20 |

单位是架构退休宏指令，不是 UOP、cache line 或请求事务。对应事件资格按成员 OR，
不同事件不强制互斥；混合分片可以同时满足多个事件。不能用访问数减命中数生成新 miss。
原 `L2_RQSTS.*`、`LONGEST_LAT_CACHE.*` 及 native tag miss 继续作为各自独立诊断。

gem5 在原生服务生命周期收集各层事实，归属实际退休的宏；没有重新查 cache 或使用
path_class 推导原生真值。FastSim 在两阶段最终接受前缀上归并自身模拟结果；宏成员
跨 chunk/warmup 保留，未退休尾部不计数。合并、远端供给、forwarding、缺失成员等
不能确证目标事件时保留逐事件 unknown；明确上层命中时，下层非事件与 unknown 区分。

所有预测仍是近似值，ISA/服务资格和硬件等价未认证；错误路径、预取对正确路径 cache
状态及争用的间接影响仍是模型限制。证据不完整时按用户授权报告暂定误差和缺项，
不将 unknown 当作实际零事件，不将新旧不同事件的误差称为修复前后改善。

## 新采集流程

- 四核，显式 `--mem-channels 8`；FastSim 从实际 config.ini 导入通道及 DRAM 时序。
  保留现有缓存几何、buffer TSO114，并独立审计采集/回放身份。
- LBM64³ 的初始化和初始统计在 KVM 串行完成；Graph500 scale18/edge16 在 KVM
  完成串行 setup。handoff 确认 main-only，在 O3 建立四线程团队（主线程加三个工作线程）。
- 详细 warmup 由 core0 的gem5指令停止计数200万控制（该计数排除NOP和指令预取，并非全部FST宏指令），调用进程级测量切换；
  不等待完整 timestep/BFS。其他核没有残留 warmup stop，避免误触发共同终点。
- stat 仍采用 first-core-target common end，首核500万 user UOP 后共同关闭。
  warmup/state 输入仍保留；不向 FastSim 传入 gem5 计数、命中结果或时钟。
- 详细 warmup 最长600秒，整案2400秒；超限源失败，不计为完成。
  该固定预热预算尚未通过收敛实验，不能声称缓存已稳态。
- Graph500宿主完整64轮验证通过，详细采集 `SKIP_VALIDATION=1`，测量窗口排除
  verify_bfs_tree。LBM标记版三步输出与先前同网格基线一致。不是标准SPEC成绩。

## 存储与验证

已删除旧双通道两案8个 FST，共46,333,294,848字节，保留 JSON/报告及小回归输入。
每个删除文件的路径、大小及SHA256见
[删除收据](../tmp/retired-cache-eight-channel-20261015/old-data-retirement.json)。
旧大案因 FST 已删除，只能视为存档结果，不能声称仍可从本地原始流复验。
全局FST上限100GB，单案预算20GB，保留20GB磁盘余量。

维护验证入口新增 `--retired-cache`：复用现有来源/功能人口审计，核验逐核宏/UOP
人口、输入内容身份、task集合、新事件集合与聚合守恒；缺失新来源不回退旧请求字段。
输出 `retired-cache-summary.md`，保留raw计数、per-1K-macro、unknown、绝对差及暂定误差。

真机维护采集器 `tools/run_perf_cache_probe.py --retired-cache` 使用同一事件字典，
新增分组6–9，原请求事件分组保持原编码。本机 SPR 在CPU0完成3种访存探针、
每种3次重复，共36条读数；全部 `time_enabled == time_running > 0`，未发生计数器复用。
见[真机探针收据](../tmp/retired-cache-eight-channel-20261015/hardware-retired-groups/manifest.json)。
这些是user-only单线程采集路径验证；负载、缓存配置及ROI与四核仿真不同，宿主也非独占，
不用于计算三方精度或认证硬件等价。

独立审查发现混合直接服务/Sequencer合并片段可能漏标L3 unknown，已用生产C++
回归复现并修复；overlay一致，审核无剩余Critical/Important项。

## 实采结果

两案均已重新采集并完成 FastSim 回放。独立身份检查均为valid、零不匹配；实际配置
均为8个MemCtrl，buffer TSO114。维护验证发现2案、通过2案、失败0、待执行0。
这里的通过表示来源、人口及守恒检查通过，不表示精度达标或硬件事件已认证。
详见[维护汇总](../tmp/retired-cache-eight-channel-20261015/validation/summary.md)和
[本轮实验](../tmp/retired-cache-eight-channel-20261015/)。

### Active CPI 与 elapsed

CPI使用workload-task active cycles及各自退休宏指令数，人口匹配。
相对误差为 `(FastSim-gem5)/gem5`，CPI绝对误差单位为cycles/宏指令。

| 负载 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 |
|---|---:|---:|---:|---:|
| 782.lbm_r | 0.811328 | 1.620331 | +99.7134% | 0.809003 |
| 854.graph500_s | 2.269707 | 1.481147 | −34.7428% | 0.788560 |

两案CPI MAE为 **0.798782 cycles/宏指令**，MAPE为67.2281%。
active口径可评分，真机perf等价仍未认证。

| 负载 | gem5 elapsed(ms) | FastSim elapsed(ms) | 相对误差 | 绝对误差(ms) |
|---|---:|---:|---:|---:|
| 782.lbm_r | 0.892484 | 1.782410 | +99.7134% | 0.889927 |
| 854.graph500_s | 2.724959 | 1.630476 | −40.1651% | 1.094483 |

elapsed是共同测量窗口的目标运行时间，不是宿主采集耗时，不跨核心相加。
两案elapsed MAE为0.992205ms。Graph500 active和elapsed误差不同，两个时间域分别报告。

### 重点退休 miss：原始计数与暂定误差

范围为`workload_task`；unknown逐事件独立统计，不能相加形成总缺失访问数。
**以下误差只比较已有正资格计数；两侧未知集合不同，不代表全部退休load的miss精度。**

| 负载 | 退休事件 | gem5 | FastSim | 相对误差 | 计数绝对差 | gem5 unknown | FastSim unknown |
|---|---|---:|---:|---:|---:|---:|---:|
| LBM | L1_MISS | 70,708 | 71,639 | +1.3167% | 931 | 453,430 | 1,551,218 |
| LBM | L2_MISS | 70,683 | 71,618 | +1.3228% | 935 | 812,485 | 1,551,218 |
| LBM | L3_MISS | 70,294 | 71,223 | +1.3216% | 929 | 812,489 | 1,551,223 |
| Graph500 | L1_MISS | 297,061 | 422,827 | +42.3368% | 125,766 | 123,996 | 697,242 |
| Graph500 | L2_MISS | 198,662 | 317,529 | +59.8338% | 118,867 | 336,724 | 697,751 |
| Graph500 | L3_MISS | 51,013 | 78,793 | +54.4567% | 27,780 | 348,417 | 708,052 |

| 退休事件 | 两案暂定MAPE | 暂定WAPE | max APE |
|---|---:|---:|---:|
| L1_MISS | 21.8267% | 34.4502% | 42.3368% |
| L2_MISS | 30.5783% | 44.4790% | 59.8338% |
| L3_MISS | 27.8891% | 23.6664% | 54.4567% |

九项完整计数、per-event unknown与汇总见
[退休PMU维护报告](../tmp/retired-cache-eight-channel-20261015/validation/retired-cache-summary.md)，
五种scope及逐核人口见各案validation.json。LBM退休load/store分别为2,870,923/1,424,751，
Graph500为3,432,222/837,182；FastSim计数相同。native ISA资格仍有少量unknown，
人口一致不能认证所有ISA成员均与硬件计数相同。

LBM模型unknown约占退休load的54.0%，native L2/L3 unknown约28.3%，远大于约7万
已资格miss。因此不能把约1.3%的数值称为完整miss精度。Graph500模型unknown约20%–21%，
native约3.6%–10.2%，已资格计数也有显著偏差。FB_HIT的模型正资格输出为0，
两案差异均为−100%，仍有大量unknown，不能解释为实际不存在FB命中。

本次没有修复或校准CPI时序，CPI误差仍大；改变观察事件不会补回错误路径、预取、
共享争用及服务完成时序的影响。现有数据未作单变量消融，不能据此认定这些因素中
任何一个是误差主因，也不能将新退休口径与旧请求口径作“修复前后精度”比较。

### 采集成本与存储

| 负载 | warmup宿主耗时(s) | core0实际warmup宏指令 | 整次采集宿主耗时(s) | 新FST字节 |
|---|---:|---:|---:|---:|
| LBM | 272.380 | 5,827,323 | 556.611 | 3,682,328,288 |
| Graph500 | 243.935 | 2,700,809 | 707.397 | 3,167,738,528 |

两案新FST合计6,850,066,816字节，均未触发空间或时间守护。Graph500由core2首先达到
500万user UOP，LBM由core0触发；其余核保留同一共同终点，不独立截断。
采集总耗时包含KVM、warmup、stat及终点后的退出/落盘。一次只读栈采样确认终点后
gem5还可能执行O3：`exitSimLoop`的全局退出事件延后`simQuantum`，此时FST与测量窗口
已冻结。该宿主开销不纳入目标elapsed；未改变生产者以掩盖收尾时间。

结束时全工作区FST总量28,767,155,752字节（28.77GB），低于100GB上限；磁盘空闲
563,280,703,488字节（563.28GB）。这是清点时快照，详见
[最终存储清点](../tmp/retired-cache-eight-channel-20261015/final-storage-inventory.json)。

可配置DRAM约束按实际config.ini导入，包括tRAS=97核心周期、8通道、3GiB；逐字段
换算见各案`parameter-provenance.json`。整周期向上取整、refresh、队列到达次序及
紧凑Ruby/network封装仍为近似，不能声称整个微架构逐周期等价。缓存几何和封装延迟
沿用已冻结的来源映射配置，没有以本轮结果拟合。

### 验证记录与复现边界

- gem5使用可用192核的80%向下取整，即`-j153`构建成功；FastSim默认构建、
  `fastsim_tests`及退休服务C++专项通过，采集/overlay四个文件逐字节一致。
- Python共447项：439通过、5跳过、3项环境错误。错误为缺少`fastsim_py`模块、
  两项checkpoint测试缺少外部TCSim脚本；完整日志为
  [最终Python日志](../tmp/retired-cache-python-final.log)。没有将整套测试称为全绿。
- 独立代码审查覆盖混合分片、unknown、维护来源人口复用及真机分组；复现并修复
  coalesced分片丢失L3 unknown的问题。详细记录见
  [review.md](../tmp/retired-cache-eight-channel-20261015/review.md)。
- 运行中的临时编排脚本在最终聚合时用了旧版函数签名；两案回放报告已通过且保留。
  使用当前维护`write_summary`入口恢复汇总，无重新采集、预测修改或自建评分。
- 初始采集/二进制指纹与最终实现源码指纹分别保留。采集后仅纠正warmup帮助文本和
  manifest策略名称中的单位，不改生产者行为。LBM还用当前维护入口复核保存输出，
  CPI与退休PMU比较逐项相同；这不是新的吞吐测量。
- 本轮未进行独占宿主、重复配对的吞吐对照，不发布新正式吞吐或开销结论。

本轮完成的是退休事件输出链、来源审计、八通道重采集及两案暂定误差报告。
后续精度工作应先分解并补齐模型合并/forwarding等未知服务资格，之后固定同一FST、
配置及ROI做时序与共享服务消融；当前不能承诺CPI或PMU已经恢复到10%内。
