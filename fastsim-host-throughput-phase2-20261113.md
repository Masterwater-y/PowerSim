# FastSim 第二轮主机吞吐优化（2026-11-13）

已在隔离版本实施 FR-FCFS 临时容器复用与反馈窄副本。五负载与上一轮优化版、冻结 SPR-M2 的 CPI、逐核人口、周期及全部模型 PMU 字段精确一致，差异为零。
本报告验证模型结果守恒；沿用已绑定的 CPI 参考，不新增 native PMU 或硬件等价认证。

## 实现与校验

- FR-FCFS 保留稳定排序、哈希键/重复键语义、资源准入和可见性回退；复用事件、请求、CHA 日历、映射桶、回填及延迟数组。递归响应重算使用独立 deque 槽位，RAII 恢复层数。
- 反馈提案复制全部活跃核状态和计数，仅省去入口先初始化的输出数组；97 个向量、4 个标量均有显式策略。账本形状变化沿用完整复制。
- 七个相关测试程序通过；八个差分场景对照上一轮库，覆盖递归、重复 epoch、信用回退、活跃核减少、空核、零内存事件及 prepare 提案事务回退。详情见实验目录中的测试收据。
- 保持退休功能输入、两阶段并行框架、prepare、父服务、DTLB 和 SQ 行为。

## CPI 与模型 PMU 守恒

CPI 使用 workload-task active cycles / task macro instructions；绝对误差为 |FastSim CPI − gem5 CPI|，相对误差以 gem5 CPI 为分母。

| 负载 | gem5 CPI | 冻结 M2 / 上轮 / 本轮 CPI | CPI 绝对误差（cycles/macro） | 相对误差 | 模型 PMU |
|---|---:|---:|---:|---:|---|
| Zstd | 1.206049 | 1.155342 | 0.050708 | -4.20% | 全字段不变 |
| XZ | 0.575297 | 0.604032 | 0.028735 | +4.99% | 全字段不变 |
| FLAC | 0.284584 | 0.293530 | 0.008946 | +3.14% | 全字段不变 |
| Graph500 | 0.966422 | 0.837365 | 0.129057 | -13.35% | 全字段不变 |
| Marian | 2.364997 | 3.044500 | 0.679503 | +28.73% | 全字段不变 |

五负载等权 CPI MAE **0.179390 cycles/macro**，MAPE **10.89%**，均与 M2 和上一轮相同。这些 CPI 参考与原报告的窗口/证据等级一致，结果守恒不提升其原认证等级。

## 同输入串行配对吞吐

主吞吐排除输入预解码、初始化、完整输入校验、前后完整性检查和功能 warmup；未扣除 CPU 样本时间。各版均启用 FASTSIM_PRELOAD_VALIDATED=1，使用相同输入、有效配置和编译选项。CPU 52–63 / socket、NUMA 1 / performance；工作频率来自完整进程 perf cycles/task-clock，不能当作测量窗口独立频率。
净吞吐分子为测量范围内的 user UOP 数，分母为测量阶段并行墙钟时间；吞吐均值取三次运行的算术平均，收益中位数取各同输入配对吞吐比值减一的中位数。

| 负载 | 上轮净吞吐均值 | 本轮净吞吐均值 | 配对收益中位数 | 完整进程均值（前→后） |
|---|---:|---:|---:|---:|
| zstd-throughput | 1.454 Muser-UOP/s | 1.552 Muser-UOP/s | +6.81% | 73.11 → 71.77 s |
| c04-872.marian_s | 1.753 Muser-UOP/s | 1.817 Muser-UOP/s | +3.42% | 70.07 → 69.21 s |

| 负载/组 | 净耗时前→后（s） | 收益 | RSS 前→后（GiB） | 工作频率前→后（GHz） |
|---|---:|---:|---:|---:|
| zstd-throughput/1 | 21.224 → 19.871 | +6.81% | 15.88 → 15.93 | 3.090 → 3.090 |
| zstd-throughput/2 | 21.584 → 20.119 | +7.28% | 15.93 → 15.91 | 3.090 → 3.090 |
| zstd-throughput/3 | 21.036 → 19.827 | +6.10% | 15.93 → 15.94 | 3.090 → 3.090 |
| c04-872.marian_s/1 | 19.197 → 18.561 | +3.42% | 15.86 → 15.87 | 3.090 → 3.090 |
| c04-872.marian_s/2 | 19.236 → 18.687 | +2.94% | 15.89 → 15.84 | 3.090 → 3.089 |
| c04-872.marian_s/3 | 19.104 → 18.281 | +4.50% | 15.82 → 15.87 | 3.089 → 3.090 |

每个负载仅三组 AB/BA/AB 串行配对，尚未完成 30 组稳定性门禁。JSON 收据保存起止核竞争快照；快照不能排除区间内短暂竞争，这些数据只支持本批次观察收益。准备、校验、warmup、完整进程耗时均独立保存。

观察到核竞争的边界：

- `zstd-throughput/2 baseline 结束: 181573  62 16.1 node`
- `zstd-throughput/3 baseline 开始: 181573  62 15.6 node`

| 负载/版本 | 输入准备均值（s） | 初始化均值（s） | 完整性检查均值（s） | 功能 warmup 均值（s） |
|---|---:|---:|---:|---:|
| zstd-throughput/baseline | 16.265 | 0.013 | 31.995 | 1.533 |
| zstd-throughput/candidate | 16.374 | 0.013 | 31.934 | 1.465 |
| c04-872.marian_s/baseline | 15.867 | 0.013 | 31.219 | 1.721 |
| c04-872.marian_s/candidate | 15.781 | 0.013 | 31.163 | 1.660 |

## 测量窗口专属采样

perf 99 Hz、DWARF 8192、monotonic 时钟；FIFO 开关采样的确认在计时器外完成，报告再次按精确起止纳秒过滤。采样运行的模型输出与基线精确一致，其吞吐包含采样开销，不进入配对结果。首次失败因 perf 的 ack 含尾随 NUL；原失败日志保留，探针确认后修正协议。
采样窗：3114057746574671–3114079910907688 ns。
[窗口采样报告（已解码函数名）](tmp/host-throughput-phase2-20261113/profile-c04-777.zstd_r.perf-report-demangled.txt)

| 热点 | self 采样占比 |
|---|---:|
| 输入块生产 produce_thread_chunk | 12.33% |
| 第一阶段 IntervalCoreModel::schedule | 10.86% |
| 第二阶段反馈内的主要 lambda | 8.47% |
| 第二阶段反馈函数本体 | 5.55% |
| 第二阶段反馈内的另一个 lambda | 4.08% |

这些是同一精确窗口内各线程的 self 样本比例，不是串行墙钟预算。主要热点仍是逐 UOP 调度与反馈。本批 Zstd 未启用 hierarchy_walk 或sequencer_load_admission，因此反馈窄副本的运行效果由合成场景验证；不能把它当作该负载吞吐变化的来源。

## 二进制与源码身份

- 上轮基线：`f797a80a0c9ec7bbb73cd1b6be6f450f657d28083546a072b4cd1c8f85277786`。
- 本轮候选：`a35959255a2ec1dca53c40b5a67304303770f5a08d2da4e522cb6ab314d479ea`。
- 冻结默认 M2：`0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`。
- 本轮 simulator.cpp：`c8b40ec88b02fd6cd22576920f6b8dcdecc166762eef0123751ff3f51efa7cb2`。
- Release -O3 -DNDEBUG、IPO ON、LTO 16、NATIVE OFF，与上一轮一致。
- 源码已合入主工作区。
- 使用候选：`LD_LIBRARY_PATH=/opt/gcc-11/lib64 FASTSIM_PRELOAD_VALIDATED=1 python3 scripts/run_high_frequency.py -- tmp/host-throughput-phase2-20261113/candidate-build/fastsim simulate ...`；冻结 build/fastsim 保留。

[完整可追溯数据](fastsim-host-throughput-phase2-20261113.json)；[上一轮报告](fastsim-host-throughput-optimization-20261112.md)。
