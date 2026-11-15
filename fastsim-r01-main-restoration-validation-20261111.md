# R0/R1 冻结 v2-wait 主工作区恢复验证（2026-11-11）

已把冻结实现恢复到 `/home/data00/yinhaolang/FastSim`（与 `/data00/yinhaolang/FastSim` 为同一目录），当前分支为 `fastsim-spr`。主工作区重新构建后，五个同输入回放的完整模型输出均与冻结版严格一致。

- 恢复 14 个源码/构建文件，移出 5 个后续实验文件；完整源码 661 项与冻结源码一致（不包含 Python 字节码缓存）。
- 冻结快照 1067 项、其余工作区 2633 项校验未变化。
- 原工作区源码及完整 build 备份：[恢复前备份](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/backup)。
- Release 构建通过（GCC 11，`-j16`，`-flto=16`）；6/7 组测试通过。失败项均在原冻结二进制中复现同一退出码与完整日志，详见测试记录。
- 当前工作区改动未提交、未推送。

唯一失败项为 `fastsim_spr_l2_shared_prefetch_runtime_tests`：额外启用 L2 共享预取的定向场景报 `L2 shared prefetch population is not conserved`。原冻结测试与主工作区测试均退出 −6，完整日志一致；该已有问题不涉及本轮五负载的预取关闭配置。

## 版本身份

- 冻结二进制 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
- 主工作区新构建 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
- 本轮主程序与冻结二进制字节完全一致；另以相同输入的模型输出核验。

## 同输入 CPI

四核 workload-task active CPI，以每个模拟器的总 active cycles / 总宏指令数计算，共同起点与 `retired-stream-completion-v1` 终点一致。gem5 参考沿用维护原生验证器已评分的冻结报告；本轮先证明完整输出严格等价，再继承其来源、范围与人口核验，没有另选参考或重新执行原生审计。相对误差以 gem5 为分母；绝对误差单位为 cycles/macro。

| 负载 | gem5 CPI | 冻结 CPI | 主工作区 CPI | 主工作区−冻结 | CPI 绝对误差 | 相对误差 |
|---|---:|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 1.142316 | 0.000000 | 0.063733 | -5.28% |
| XZ | 0.575297 | 0.601359 | 0.601359 | 0.000000 | 0.026062 | +4.53% |
| FLAC | 0.284584 | 0.318433 | 0.318433 | 0.000000 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.835896 | 0.000000 | 0.130526 | -13.51% |
| Marian | 2.364997 | 3.068823 | 3.068823 | 0.000000 | 0.703826 | +29.76% |

五负载等权 CPI MAE = **0.191599 cycles/macro**；MAPE = **12.99%**。

## 一致性核验与范围

逐项比较 CPI 时间账本、逐核宏指令/UOP 人口、缓存与 CHA 计数、功能输入身份、调度与线程状态、prepare/父服务/预取诊断，以及其余全部模型字段，五负载差异均为零。仅排除宿主 wall time、吞吐、wall-time breakdown、`causal_frontier.*_wall_ns` 与宿主条件变量等待计数 `frontier_waits`；后者由 `acquire_chunk()` 等待生产者队列时记录，不是模型停顿周期。本轮不形成吞吐性能结论。退休指令 prepare 保持开启；实验输入仍关闭 native prefetch 和退休前 early prepare。

独立硬件配置身份检查 5/5 已通过，相关 config.ini 与事件字典哈希本轮复核一致。CPI 总量与逐核宏人口检查 5/5 一致。既有 PMU 证据缺口仍存在：XZ 和 Marian 各有一条原生退休 L1 load-miss unknown，完整 PMU 精度仍不能评分；本次恢复不改变这些参考缺口。

## 可追溯证据

- [验证汇总及原生报告哈希](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/validation-summary.json)
- [恢复文件清单](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/restoration.json)
- [冻结与其余文件保护校验](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/protection-after.json)
- [构建日志](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/build.log)；[测试及冻结对照记录](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/tests.json)
- [五次回放命令与二进制、配置、输出哈希](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/runs.json)
- [既有独立硬件身份验证](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/identity.json)

同输入命令与原样配置均保存在本次回放目录，直接使用主工作区 `build/fastsim`。
