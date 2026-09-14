# SPEC CPU 2026 六负载单变量/多变量微架构排序实验计划

## 目标

在既有三负载、九个 OFAT profile 的 native C4/C8 排序验证上，扩展 workload
覆盖并加入参数交互点。实验继续以 gem5 为唯一微架构标签，以 FastSim 的目标配置
回放结果做预测；两端都使用 user-plus-kernel measurement scope，不使用 gem5 timing
或 PMU 作为 FastSim 在线输入。

## 负载

保留 `706.stockfish_r`、`731.astcenc_r`、`811.tealeaf_s`，新增三个已经通过历史
C4/C8 native 采集链路的负载：

| Workload | 主要覆盖 |
|---|---|
| `706.stockfish_r` | 分支密集搜索 |
| `731.astcenc_r` | 媒体压缩、线程池 |
| `811.tealeaf_s` | 稀疏迭代和内存访问 |
| `710.omnetpp_r` | 事件模拟、对象与控制流 |
| `816.nab_s` | 分子动力学、浮点计算 |
| `854.graph500_s` | 图遍历、共享内存与不规则访问 |

不选历史 smoke 开销较大的 `777.zstd_r`、`782.lbm_r`，避免在第一轮交互矩阵中让
采集成本压倒覆盖收益；也不恢复已经从当前探索集合删除的 750/867。

负载磁盘映射在采集前逐项检查二进制和关键输入。`816.nab_s` 使用历史验证过的
`spec2026-native-multicore-warmtrace.ext4`，其余五个负载保持各自已有映射；若
`debugfs stat` 发现任一路径缺失，整个矩阵在启动采集前直接失败。

## 参数设计

基线为 ROB192、L1D 32KiB/8-way、private L2 1MiB/8-way、总 LLC
64MiB/16-way（8 banks）。矩阵定义在
`configs/spec2026-uarch-exploration-v2.json`，共有 17 个 profile：

- 1 个 baseline；
- 8 个单变量锚点：ROB 96/256、L1D 16/64KiB、L2 512KiB/2MiB、
  LLC 32/128MiB；
- 6 个 ROB/cache 两变量组合：ROB96 配小端、ROB256 配大端，分别与 L1D、L2、
  LLC 组合；
- 2 个四变量角点：`small_all` 和 `large_all`。

这不是完整的 `3^4=81` 全因子矩阵。它优先验证当前已知的 response-driven ROB
生命周期与 cache/memory pressure 交互，并用全小/全大角点检查多个主效应是否可组合。

结合六个 workload 和 C4/C8，正式规模为：

```text
17 profiles × 6 workloads × 2 core counts = 204 cases
```

其中单变量/基线子集 108 cases，多变量子集 96 cases。每个 case 独立采集每核至少
10M user UOP 的 native FST，同时保存 measurement 窗口内的 kernel records。

宿主采集并发限制为 20。smoke/正式采样超时分别为 1 小时/8 小时，并设置覆盖检查点
创建和采样全过程的 75 分钟/8.25 小时硬超时；超时或收到停止信号时终止该任务的
整个进程组，避免遗留 gem5 子进程。

## 执行阶段

1. 静态矩阵、最终 gem5 config、FastSim effective config 和磁盘路径 fail-closed
   预检；
2. 全 204-case、每核 10K user UOP smoke；
3. smoke 全部通过后，断点续跑 204-case、每核 10M user UOP 正式采集；
4. materialize profile-specific labels/FST views；
5. 使用维护别名 `configs/gem5-fs-native-kernel.cfg` 做 FastSim 回放；
6. 生成原有 CPI/PMU/speedup/direction/ranking 报告；
7. 额外拆分 baseline+单变量、涉及多变量、单变量对多变量、多变量对多变量的
   pairwise 排序准确率并生成组会摘要。

采集器和回放器均以完成标记断点续跑。后台启动器对失败阶段最多自动重试六轮，
已完成 case 不重复采集；所有阶段均不因需要人工输入而暂停。

2026-09-07 恢复前审计确认，除 NAB 曾误用不含其 run 目录的通用镜像外，另外五个
负载的镜像、二进制和关键输入均有效。定向清理只删除 41 个失败 case 的不完整目录，
保留 163 个有效 smoke case 和 7 个完整检查点。

## 排序和验收

排序仍按相同 workload/core 内的 aggregate user-UOP CPI 两两比较。gem5 CPI 相对差异
低于 0.5% 的 pair 只进入 raw 诊断，不进入 material accuracy。主要 gate 保持：

- material pairwise ranking accuracy ≥ 90%；
- material baseline direction accuracy ≥ 90%；
- variant speedup error P90 ≤ 10%；
- variant CPI APE P99 ≤ 10%；
- strict PMU WAPE ≤ 2%；
- 最低 FastSim 吞吐 ≥ 5M user UOP/s；
- 每个 profile 至少有两个有效激励 case。

单变量和多变量排序同时报告；只有总体和涉及多变量的结果都稳定，才能把结论扩展到
联合微架构 DSE。

## 后台运行与产物

启动和查看状态：

```bash
bash scripts/launch_spec2026_uarch_exploration_v2.sh start
bash scripts/launch_spec2026_uarch_exploration_v2.sh status
```

启动器使用 `nohup + setsid`，标准输入断开，日志重定向，因此不依赖当前 SSH 会话。
默认产物位于：

```text
tmp/spec2026-uarch-exploration-v2-native-v28_6-20260907/
```

关键文件为 `pipeline.state`、`pipeline.log`、采集 `status.json`、
`evaluation-v28_6-maintained/generalization-report.{json,md}` 和最终
`result-summary/summary.{json,md}`。

## 磁盘预算与保护

启动前使用已完成的 54-case native v1 目录作为实测基准，按 case 数线性外推正式
204-case 数据；另为 smoke 保留正式数据的 12%（至少 64GiB），为 trace 转换、失败
现场和重试保留正式数据的 20%。估算写入 `disk-budget/disk-budget.{json,md}`；若预计
完成后的剩余空间不足 1TiB，pipeline 在采集前 fail closed。

独立 `nohup + setsid` 磁盘守护每 60 秒读取实际可用空间：低于 1.5TiB 时写预警，
低于或等于 1.2TiB 时终止 pipeline 的整个进程组。1.2TiB 停止线为 1TiB 硬保留线
提供约 0.2TiB 的并发写入刹车余量；状态和日志分别位于 `disk-guard.state` 与
`disk-guard.log`，并由 launcher 的 `status` 子命令一并展示。
