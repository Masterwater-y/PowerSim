# FastSim 第三轮等价吞吐优化（2026-11-13）

已在隔离版本实施并集成六个源码/测试文件。九个测试程序、八组定向差分、五负载完整回放均通过；
五负载与第二轮、冻结 SPR-M2 的 CPI、逐核人口/周期及全部模型 PMU 字段精确一致，目标差异为零。
普通候选、NUMA 和 PGO 分开进行同输入串行配对；本批结果不代替 30 组稳定性门禁。

普通构建相对第二轮：**zstd-throughput 1.532 → 1.435 Muser-UOP/s（配对中位数 -6.43%）；c04-872.marian_s 1.816 → 1.712 Muser-UOP/s（配对中位数 -4.84%）**。PGO 相对本轮普通构建：**zstd-throughput +14.17%；c04-872.marian_s +12.89%**。
普通候选存在配对收益中位数为负的负载，本轮不能认定组合优化提高了吞吐。PGO 在本批配对中收益为正，保留为独立候选；已覆盖部分未参与训练的负载，尚未完成长期稳定性验收。 默认二进制仍保留冻结 SPR-M2。
组合吞吐回退与 CPI/PMU 守恒是不同结论；本批没有逐项消融，尚不能将回退归因于某一处源码改动。

## 实施范围与依据

| 优化点 | 本轮实现 | 等价边界与评估 |
|---|---|---|
| 伴随数据零拷贝 | 为静态 operand 和扩展依赖增加只读 `TraceArrayView`；预加载源直接借用连续数组；slice/warmup 透传 | 保留 vector 接口兼容旧消费者；view 至下一次 `next()` 前有效，跨阶段持久依赖仍复制到 chunk |
| 静态查询复用 | 每条 UOP 的静态事实懒查询一次；64 槽缓存绑定源内精确 ASID map 指针和 PC，包含 negative 查询 | map 构建后只读；不同 ASID、缺项、哈希冲突、重复查询均有测试；旧窗口 static_instruction self 样本 0.95%，不是可扣除的墙钟预算 |
| SPR 第二阶段热路径 | checkpoint 入口缓存 partitioned IQ / branch recovery 选择；Sequencer 排序键每事件计算一次 | 原有分区 IQ、分支、容量与父服务路径保留；这是局部选择缓存，尚非完整模板专门化 |
| 冷热布局 | FR-FCFS 的 ordinal/tag/CHA queue 放入紧凑事实数组，完整 descriptor 保持原引用路径 | 局部分离已实现；resident UOP 仍为 200 B，完整 UOP sidecar 尚未迁移，避免额外间接访问与复制未经验证就扩大改动 |
| FR-FCFS 直接事件索引 | 三张 event hash map 改为逐核 dense first-occurrence 索引与 materialized 下标 | 保留 emplace 的首项/重复键语义、stable_sort 次序、缺项拒绝、资源准入及递归 scratch 隔离 |
| PGO / NUMA / 线程交接 | PGO 独立训练与重编译；NUMA node 1 显式内存绑定配对 | NATIVE OFF，无 fast-math；输入准备原已在 CPU 52–63 上 first touch；既有采样没有证实主要锁/交接热点，未改线程交接算法 |

上述组合通过差分后整体测量。未做逐项消融，不能把组合收益拆分为各优化的独立贡献。
本轮没有启用块转移、修改两阶段算法或更改目标模型配置。退休 prepare、unknown、fallback、事件人口和宏级退休归并保持原语义。

## 验证、输入和证据等级

- [九程序测试收据](tmp/host-throughput-phase3-20261113/test-receipts.json)涵盖主测试、validated cache、static UOP、动态 DTLB/hierarchy、SPR frontend、FR-FCFS、prepare 和 ifetch Sequencer。
- [八组定向差分](tmp/host-throughput-phase3-20261113/stress-equivalence.json)包含递归 FR-FCFS、重复 epoch、信用回退、活跃核减少、空核、零内存及 prepare 提案事务回退。各版使用各自匹配的头文件/静态库；新增 virtual 接口后全量重编译。
- 五负载集合固定为 Zstd、XZ、FLAC、Graph500、Marian，均四核，沿用第二轮的 manifest、source-bound config、warmup/reset 和 user-plus-kernel 执行范围。
- 每次输出由维护工具 `tools/compare_fastsim_model_equivalence.py` 对比第二轮；五负载另对冻结 M2。验证 source/config 身份、time accounting、逐核人口、task PMU、retired cache 与全部非主机目标字段。
- CPI 为 workload-task active cycles / task macro instructions，绝对误差为 `abs(FastSim CPI - gem5 CPI)`。复用第二轮已绑定 CPI 参考及其原证据等级，不重新选择原生参考。
- 本轮仅证明模型输出守恒；native retired-cache unknown、硬件等价和 PMU 来源认证没有因此改善，不发布新的 native PMU 误差均值。

## CPI 与模型 PMU 守恒

| 负载 | gem5 CPI | 冻结 M2 / 第二轮 / 本轮 CPI | CPI 绝对误差（cycles/macro） | 相对误差 | 目标字段差异 |
|---|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.155342 | 0.050708 | -4.20% | 0 |
| XZ | 0.575297 | 0.604032 | 0.028735 | +4.99% | 0 |
| FLAC | 0.284584 | 0.293530 | 0.008946 | +3.14% | 0 |
| Graph500 | 0.966422 | 0.837365 | 0.129057 | -13.35% | 0 |
| Marian | 2.364997 | 3.044500 | 0.679503 | +28.73% | 0 |

五负载等权 CPI MAE **0.179390 cycles/macro**，MAPE **10.89%**，与第二轮及冻结 M2 完全不变。
完整 task PMU、逐核计数、retired-cache estimate / unknown / fallback 字段及 native CPI 来源 SHA 保存于 JSON。

## 普通候选：相对第二轮串行配对

主吞吐分子为所有参与核实际测量 user UOP 数，分母为测量阶段并行墙钟。两版均使用 `FASTSIM_PRELOAD_VALIDATED=1`；
输入解码/全量校验先于计时，初始化、前后完整性检查和功能 warmup 分列。未从墙钟扣除 CPU 样本或累计 worker CPU 时间。
执行范围为 user-plus-kernel，吞吐分子为 user UOP；未新做独立 user-only scope 配对。
各实验一次只运行一个仿真进程，AB/BA/AB；吞吐均值为三次算术平均，配对收益中位数为各组比值减一的中位数。

| 负载 | 前版净吞吐均值 | 后版净吞吐均值 | 配对收益中位数 | 完整进程均值（前→后） |
|---|---:|---:|---:|---:|
| zstd-throughput | 1.532 Muser-UOP/s | 1.435 Muser-UOP/s | -6.43% | 72.03 → 73.51 s |
| c04-872.marian_s | 1.816 Muser-UOP/s | 1.712 Muser-UOP/s | -4.84% | 69.40 → 70.72 s |

| 负载/组 | 净耗时前→后（s） | 收益 | RSS 前→后（GiB） | 工作频率前→后（GHz） |
|---|---:|---:|---:|---:|
| zstd-throughput/1 | 20.262 → 21.920 | -7.56% | 16.01 → 15.93 | 3.090 → 3.090 |
| zstd-throughput/2 | 20.488 → 21.553 | -4.94% | 15.91 → 15.92 | 3.089 → 3.090 |
| zstd-throughput/3 | 19.847 → 21.212 | -6.43% | 16.02 → 15.91 | 3.090 → 3.090 |
| c04-872.marian_s/1 | 18.348 → 19.963 | -8.09% | 15.84 → 15.83 | 3.090 → 3.089 |
| c04-872.marian_s/2 | 18.712 → 19.539 | -4.23% | 15.86 → 15.85 | 3.089 → 3.089 |
| c04-872.marian_s/3 | 18.478 → 19.418 | -4.84% | 15.86 → 15.89 | 3.089 → 3.089 |

| 负载/版本 | 输入准备均值（s） | 初始化均值（s） | 完整性检查均值（s） | 功能 warmup 均值（s） |
|---|---:|---:|---:|---:|
| zstd-throughput/baseline | 16.270 | 0.013 | 32.136 | 1.471 |
| zstd-throughput/candidate | 16.329 | 0.013 | 32.041 | 1.599 |
| c04-872.marian_s/baseline | 15.905 | 0.013 | 31.251 | 1.640 |
| c04-872.marian_s/candidate | 15.844 | 0.013 | 31.379 | 1.739 |

| 负载/版本 | n | mean | P50 | P90 | P99 | minimum（Muser-UOP/s） |
|---|---:|---:|---:|---:|---:|---:|
| zstd-throughput/baseline | 3 | 1.532 | 1.527 | 1.552 | 1.558 | 1.510 |
| zstd-throughput/candidate | 3 | 1.435 | 1.435 | 1.454 | 1.458 | 1.411 |
| c04-872.marian_s/baseline | 3 | 1.816 | 1.820 | 1.830 | 1.832 | 1.797 |
| c04-872.marian_s/candidate | 3 | 1.712 | 1.721 | 1.729 | 1.731 | 1.684 |

## NUMA：普通候选 vs 显式 node 1 内存绑定

CPU 均为 52–63，socket/NUMA node 1，使用现有 performance governor。
此处仅新增 `numactl --membind=1`；默认 first touch 和显式绑定结果分开报告，未改全局设置。

| 负载 | 前版净吞吐均值 | 后版净吞吐均值 | 配对收益中位数 | 完整进程均值（前→后） |
|---|---:|---:|---:|---:|
| zstd-throughput | 1.452 Muser-UOP/s | 1.465 Muser-UOP/s | +0.75% | 73.24 → 72.92 s |

| 负载/组 | 净耗时前→后（s） | 收益 | RSS 前→后（GiB） | 工作频率前→后（GHz） |
|---|---:|---:|---:|---:|
| zstd-throughput/1 | 21.111 → 20.954 | +0.75% | 15.91 → 15.91 | 3.090 → 3.090 |
| zstd-throughput/2 | 21.550 → 21.114 | +2.06% | 15.89 → 15.91 | 3.090 → 3.090 |
| zstd-throughput/3 | 21.279 → 21.280 | -0.01% | 15.97 → 15.93 | 3.090 → 3.090 |

| 负载/版本 | 输入准备均值（s） | 初始化均值（s） | 完整性检查均值（s） | 功能 warmup 均值（s） |
|---|---:|---:|---:|---:|
| zstd-throughput/candidate | 16.258 | 0.013 | 32.181 | 1.535 |
| zstd-throughput/candidate-numa | 16.204 | 0.013 | 32.057 | 1.584 |

| 负载/版本 | n | mean | P50 | P90 | P99 | minimum（Muser-UOP/s） |
|---|---:|---:|---:|---:|---:|---:|
| zstd-throughput/candidate | 3 | 1.452 | 1.454 | 1.463 | 1.465 | 1.436 |
| zstd-throughput/candidate-numa | 3 | 1.465 | 1.465 | 1.474 | 1.476 | 1.454 |

## PGO：普通候选 vs 独立 profile-use 构建

先用 profile-generate + atomic update 构建，运行九程序及一个完整 Zstd 输入收集训练数据，再在同一 object 路径用
`-fprofile-use -fprofile-correction` 重编译。profile-use 的九程序、八组定向差分与五负载再次通过精确等价验证。
未启用 fast-math；Zstd 参与训练，XZ、FLAC、Graph500、Marian 未参与 workload profile 收集；
未参与训练的四负载仍进行完整等价验证，其中 Marian 另外完成吞吐配对。结果不证明所有负载上的普适收益。PGO 保留为独立候选。
完整 Zstd 训练仿真进程耗时 **875.62 s**，作为一次性准备成本单列；
不计入 profile-use 吞吐分母。该合计不含编译、九程序测试、中断尝试和驱动层事后对比耗时，未测得完整 PGO 准备墙钟。
中断的 XZ 训练不计入成功训练收据或模型验收；profile 文件时间戳均不晚于已完成 Zstd 进程的结束时间，
确认中断尝试没有更新保留的 profile。[训练计划与中断证据](tmp/host-throughput-phase3-20261113/pgo-training-plan.json)保存实际训练集合与文件 SHA。

| 负载 | 前版净吞吐均值 | 后版净吞吐均值 | 配对收益中位数 | 完整进程均值（前→后） |
|---|---:|---:|---:|---:|
| zstd-throughput | 1.456 Muser-UOP/s | 1.663 Muser-UOP/s | +14.17% | 73.01 → 69.89 s |
| c04-872.marian_s | 1.733 Muser-UOP/s | 1.949 Muser-UOP/s | +12.89% | 70.37 → 67.48 s |

| 负载/组 | 净耗时前→后（s） | 收益 | RSS 前→后（GiB） | 工作频率前→后（GHz） |
|---|---:|---:|---:|---:|
| zstd-throughput/1 | 21.365 → 18.662 | +14.48% | 15.97 → 15.91 | 3.090 → 3.089 |
| zstd-throughput/2 | 21.079 → 18.463 | +14.17% | 16.02 → 15.97 | 3.090 → 3.088 |
| zstd-throughput/3 | 21.307 → 18.704 | +13.92% | 15.93 → 15.91 | 3.090 → 3.089 |
| c04-872.marian_s/1 | 19.510 → 17.210 | +13.36% | 15.89 → 15.87 | 3.089 → 3.088 |
| c04-872.marian_s/2 | 19.401 → 17.457 | +11.13% | 15.86 → 15.80 | 3.089 → 3.089 |
| c04-872.marian_s/3 | 19.306 → 17.101 | +12.89% | 15.86 → 15.88 | 3.089 → 3.089 |

| 负载/版本 | 输入准备均值（s） | 初始化均值（s） | 完整性检查均值（s） | 功能 warmup 均值（s） |
|---|---:|---:|---:|---:|
| zstd-throughput/candidate | 16.265 | 0.013 | 31.987 | 1.535 |
| zstd-throughput/pgo | 15.266 | 0.013 | 32.691 | 1.383 |
| c04-872.marian_s/candidate | 15.866 | 0.013 | 31.263 | 1.727 |
| c04-872.marian_s/pgo | 14.801 | 0.013 | 31.846 | 1.525 |

| 负载/版本 | n | mean | P50 | P90 | P99 | minimum（Muser-UOP/s） |
|---|---:|---:|---:|---:|---:|---:|
| zstd-throughput/candidate | 3 | 1.456 | 1.452 | 1.465 | 1.467 | 1.448 |
| zstd-throughput/pgo | 3 | 1.663 | 1.658 | 1.672 | 1.675 | 1.654 |
| c04-872.marian_s/candidate | 3 | 1.733 | 1.733 | 1.740 | 1.741 | 1.723 |
| c04-872.marian_s/pgo | 3 | 1.949 | 1.954 | 1.964 | 1.966 | 1.926 |

[PGO profile 清单](tmp/host-throughput-phase3-20261113/pgo-training-profiles.json)；
[profile-use 测试收据](tmp/host-throughput-phase3-20261113/pgo-use-test-receipts.json)；
[PGO 定向差分](tmp/host-throughput-phase3-20261113/pgo-stress-equivalence.json)。

## 主机条件与统计限制

工作频率由完整进程 `perf cycles/task-clock` 计算，表中数值不代表独立测量窗口频率。
配对起止快照观察到的竞争进程如下；完整的 case/组/起止边界及 CPU stat 存于 JSON。
快照中的 `ps %CPU` 是进程累计口径，不能据此定量扣除测试区间的抢占损失。

- `collector_x_spr（PID 145933，快照所在 CPU 52）`
- `collector_x_spr（PID 176998，快照所在 CPU 52）`
- `collector_x_spr（PID 184456，快照所在 CPU 52）`
- `collector_x_spr（PID 33900，快照所在 CPU 52）`
- `collector_x_spr（PID 68113，快照所在 CPU 52）`
- `gem5.fast（PID 57291，快照所在 CPU 60）`
- `gem5.fast（PID 57291，快照所在 CPU 61）`
- `gem5.fast（PID 63579，快照所在 CPU 60）`
- `sniper（PID 171282，快照所在 CPU 53）`

起止快照不能排除区间内短暂竞争。每项每负载仅三组，P90/P99 是 Type-7 描述统计，不是尾部稳定性认证；
尚未完成 30 组安静主机验收。普通/NUMA/PGO 测量不混合，跨批次均值不得相乘得到累计收益。

## 集成与可追溯身份

- 第二轮基线：`a35959255a2ec1dca53c40b5a67304303770f5a08d2da4e522cb6ab314d479ea`。
- 本轮普通候选：`8b71a599ec60f1c979c1e7fedbec8da385dbd06c840ce7b2e541329a244ab188`。
- 本轮 PGO 候选：`2ed2a8f921e6a9a1d14d6638f79111570623c65fbe9bb2afd53f8f3ab4cc4b52`。
- 冻结默认 M2：`0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`。
- Release `-O3 -DNDEBUG`、IPO ON、LTO 16、GCC 11.5、NATIVE OFF；普通候选与第二轮采用相同编译设置，PGO 单独变更 profile 选项。
- 经校验后用 guarded patch 集成六个文件；769 个源码输入 SHA 与隔离验证树逐一一致。[集成收据](tmp/host-throughput-phase3-20261113/production-integration.json)确认默认二进制仍为冻结 M2。
- 未创建 commit 或 push；运行实验文件均在项目 `tmp/` 下。

运行普通候选：

```bash
source scripts/project_tmp.sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 FASTSIM_PRELOAD_VALIDATED=1 \
  python3 scripts/run_high_frequency.py -- \
  tmp/host-throughput-phase3-20261113/candidate-build/fastsim simulate ...
```

运行独立 PGO 候选，将上述二进制路径换为 `tmp/host-throughput-phase3-20261113/pgo-build/fastsim`；
每个 case 的完整参数、config/manifest/output SHA、耗时及主机快照见对应 `.run.json`。
源码新增 view 的 virtual 接口，外部自编译 TraceSource 消费者须使用匹配头文件和库重新构建。

[完整机器可读结果](fastsim-host-throughput-phase3-20261113.json)；
[上一轮报告](fastsim-host-throughput-phase2-20261113.md)；
[隔离实验目录](tmp/host-throughput-phase3-20261113/)。
