# SPEC CPU 2026 微架构探索验证集 v1

更新日期：2026-09-02

2026-09-03 的 54-case native v28.2 正式结果见
[`spec2026-uarch-native-v28_2-results-2026-09-03.md`](spec2026-uarch-native-v28_2-results-2026-09-03.md)。

## 验证集

固定使用三个负载：

| workload | 业务特征 | 选择理由 |
|---|---|---|
| `731.astcenc_r` | 图像/纹理编码 | 媒体压缩，生产 pthread 池，已有可靠 source warmup 和 ROI marker |
| `706.stockfish_r` | 分支密集搜索 | 对应检索/策略搜索类前端压力；原正式 10 负载 C4/C8 10M 已通过 |
| `811.tealeaf_s` | 稀疏迭代与内存访问 | 对应图计算/稀疏数值类内存行为；原正式 10 负载 C4/C8 10M 已通过 |

`750.sealcrypto_r` 和 `867.nest_s` 已从 manifest overlay、镜像构建、默认 smoke
和采集集合移除。750 的生产线程在每线程 SEAL 内存池/页面首次触达时产生长尾，
source marker 之前虽然有 warmup，但没有覆盖每个线程后续实际使用的同构私有页面，
导致慢核追赶和 OoO source warmup 超时；867 的 C32 10M 已按要求停止并删除。

最初 smoke 曾试用 777.zstd_r 和 782.lbm_r，但它们不满足“从原 10 负载中选两个
简单负载”的开销约束。同一次历史 10M 正式采集中，从 gem5 `23:40:38` 启动到
`run.log` 最后写入的近似 C4/C8 wall time 分别为：706 `3:40/13:24`、811
`4:11/9:45`、777 `8:03/23:54`、782 `12:28/28:35`。此外 777 的多个 C8
微架构变体在 900 秒 smoke 窗口仍未结束 source warmup。因此验证集最终冻结为
731/706/811；777/782 的旧 smoke 结果不得混入正式矩阵。

## 参数矩阵

矩阵定义在 `configs/spec2026-uarch-exploration-v1.json`。L1 在本验证中明确指
数据 cache：gem5 MESI 的 L0 Dcache 对应 FastSim L1D；L1I 保持 32KiB 不变。
采用 baseline 加 8 个单因素端点，共 9 个 profile：

| profile | ROB | L1D | private L2 | total L3/LLC |
|---|---:|---:|---:|---:|
| `baseline` | 192 | 32KiB | 1MiB | 64MiB |
| `rob96`, `rob256` | 96 / 256 | 32KiB | 1MiB | 64MiB |
| `l1d16k8`, `l1d64k8` | 192 | 16 / 64KiB | 1MiB | 64MiB |
| `l2_512k8`, `l2_2m8` | 192 | 32KiB | 512KiB / 2MiB | 64MiB |
| `llc32m`, `llc128m` | 192 | 32KiB | 1MiB | 32 / 128MiB |

L3 固定 8 个 bank。gem5 CLI 的 `l3_size` 是每 bank 容量，分别使用 4、8、16MiB；
FastSim `cache.llc.size` 是总容量，分别使用 32、64、128MiB。结合 C4/C8 和三个负载，
正式矩阵为 `9 × 2 × 3 = 54` 个 case；每个 case 独立采集每核 10M user UOP
作为停止目标的 native FST，FST 同时保留该窗口内的 CPL0 records，
不把 baseline FST 复制给其他 profile。

## 配置对齐门禁

运行有三层门禁：

1. 静态门禁从同一矩阵推导 gem5 和 FastSim 参数，拒绝非 OFAT profile、L3
   per-bank/total 换算错误和已删除负载。
2. 每个 gem5 case 从最终 `config.ini` 生成 `effective-target.json`，再检查核心数、
   `numROBEntries`、L1D/L2/L3 size/assoc、L3 bank 和 DRAM channel；任何不一致的
   case 不会标记 complete。
3. FastSim replay 为每个 profile 写完整 override，并从输出 JSON 回读同一组参数；
   `tools/run_uarch_fastsim.py` 同时要求 replay retired UOP 与该 profile 自己的 FST
   measurement record 数完全一致。

FastSim 的默认功能基线固定为已通过 C4/C8 gate 的
`configs/gem5-v28_2-fs-native-kernel.cfg`。runner 不再硬编码
`--measurement-scope user`，measurement scope 默认由选中的 profile 决定。采集工具
将 overlay 的相对 `config.include` 按源配置目录解析为绝对路径后再写入
case staging，避免临时目录改变 include 语义。

新采集门禁要求 `trace_scope=user-plus-kernel`、每个 FST 都带 privilege feature，
并且 measurement 区间中确实存在 kernel records。FastSim 回放必须回读为
`measurement.scope=user-plus-kernel` 和 `measurement.native_kernel_trace=true`；
旧 user-only FST 已归档后删除，不能混入新结果。gem5 和 FastSim 的 DRAM capacity
也都固定为 3GiB，不再保留旧批次的 3GiB/4GiB 偏差。

这保证“采集时参数”和“推理时参数”来自同一矩阵且经过两端实效回读，而不是只比较
命令行文本。

## 执行

所有临时文件、checkpoint、trace scratch 和报告都在项目 `tmp/` 下：

```bash
./scripts/prepare_spec2026_heldout.sh --skip-rebuild
./scripts/launch_spec2026_uarch_exploration.sh smoke
./scripts/launch_spec2026_uarch_exploration.sh formal
./scripts/launch_spec2026_uarch_exploration.sh replay
./scripts/launch_spec2026_uarch_exploration.sh report
```

smoke 使用每核 10K records，并覆盖全部 54 个参数/核心数/负载组合。正式采集只有在
smoke 的配置、ROI、FST 和 oracle identity 门禁通过后启动。KVM 负责启动到 source
checkpoint；恢复后使用 O3 执行 source warmup 和 ROI。

主机有 192 个硬件线程且内存充足，因此采集默认 54 路（整个矩阵一波发出），FastSim
回放默认 192 路。小规模 smoke 仍为每核用户记录目标保留至少 10M 条总指令的调度
窗口；否则 1K/10K 目标的纯 100 倍 safety limit 可能在 Linux 尚未把目标线程铺到
每个核之前提前退出。正式 10M 目标继续使用既有 100 倍、即 1B 条指令上限。
每个 sample 的 wall-clock 上限为 smoke 900 秒、正式 1800 秒；后者覆盖约 10 分钟
OoO source warmup 加 10M ROI，不再沿用过度宽松的 4 小时默认值。

满并发恢复时偶发的 `Ruby functional read failed` 发生在 TaoTrace 初始页表快照，
地址随运行变化，且相同参数重跑可以通过；它不是某个 ROB/cache 参数不支持。采集器
只针对该明确错误原地重试至多两次，仍失败则保留日志并让 case fail closed。这样既
不掩盖配置/ROI/oracle 错误，也避免一次 restore 竞态浪费整个并行批次。

正式产物位于 `tmp/spec2026-uarch-exploration-v1-native-v28_2/`：

- `cases/`：每个 case 的结果位置、日志和对齐审计；
- `source/`：gem5 原始 10M FST、oracle、stats、config.ini；
- `labels/` 和 `traces/`：profile-specific replay 数据视图；
- `fastsim/`：FastSim 回放结果和参数回读；
- `evaluation/generalization-report.{json,md}`：CPI error、PMU WAPE、speedup/direction
  与 pairwise CPI 排序准确率。

PMU 正式比较项为 user+active-kernel scope 的 branch miss、native Ruby L1D miss、private-L2 miss
和 CHA/LLC lookup；LLC tag miss 对 Ruby 协议 miss、DTLB、IQ/ROB full 保留为诊断项。
