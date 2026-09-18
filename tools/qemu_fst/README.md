# QEMU-FST Workflow

唯一生产链是：

```text
QEMU full-system capture → gem5 x86 EXTRAS lowering → FST v7 → FastSim replay
```

每个 raw 事务保存完整 QEMU macro 前态、frame-local RAM sparse-memory 与有序
MMIO/PIO AccessFact。普通 RAM callback 与 helper 补充区域在事务提交前归一为
唯一 RAM 字节图；store 不保存值。lowerer 仅在一个宏需要官方 memory/control
微码时调用 gem5 micro-op 语义，以当前 EA 查询该事务输入。它不运行 gem5 在线
仿真：没有 tick、`m5.simulate()`、cache/TLB/DRAM/device 或跨宏 gem5 状态。
`helper_fxrstor` 通过私有 Observation 发布完整 512-byte legacy area；这只是
通用 frame RAM，不是 mnemonic 专用格式。

所有操作通过一个入口完成：

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh
python -m tools.fst_pipeline prepare
python -m tools.fst_pipeline build
python -m tools.fst_pipeline run \
  --run-id qemu-c4 --jobs 5 \
  --gem5-fst-root /readonly/gem5-fst
```

## Prepare

`prepare` 固定读取 `configs/fst_pipeline/spec2026_c4.json`，从只读历史来源物化十个
SPEC CPU 2026 C4 workload，并生成：

```text
var/qemu_fst/workloads/{build,run}
var/qemu_fst/assets/fst-pipeline-ubuntu-24.04.raw
var/qemu_fst/assets/workloads.ext4
```

QEMU 使用与参考 FS 环境相同的 Ubuntu 24.04 base、kernel、C4/3 GiB、基础 kernel
arguments、UTC、禁网、workload argv/显式 env 和 `x86-64` 编译基线；这些字段由唯一
descriptor 驱动，而不是另在 launcher 中复制。QEMU 仅追加
`panic=-1`、runner `init=` 和 workload 选择参数。该 rootfs 以内置静态 runner
作为 PID 1，避免启动与负载无关的 systemd 服务。

同步环境不等于同化 producer。负载原有的 OpenMP binding、fork worker affinity
与 ROI wave barrier 继续按参考 workload 执行；QEMU tracer 不按 TID/CR3 过滤，
也不增加 measurement 协调线程。lowerer 仅使用已有 CPL3
`ASID/FS_BASE/RSP` 事实隔离线程上下文。QEMU machine/TCG、NOP marker、raw trace
和地址命名空间保持 producer-local。

## Build

`build` 默认依次构建：

```text
build/fastsim
qemu/build/qemu-system-x86_64
qemu/build/qemu-img
qemu_tracer/backend/dumper/build/libdumper.so
gem5_fastsim/build/X86_QEMU_FST/gem5.fast
```

QEMU 与 native dumper 由 `qemu_tracer/scripts/build_native_fst.sh` 一次性构建；
它在同一个执行单元中配置并编译 QEMU、`qemu-img`、官方 plugin test 和
`libdumper.so`。Minesim 只调用该入口：`--component producer`；不再保留
`qemu` 或 `dumper` 的独立构建入口，也不依赖时间、哈希或额外 metadata 判断同步。

增量开发可重复使用 `--component fastsim|producer|gem5`。gem5 lowerer 通过
`integrations/gem5/qemu_fst` 的原生 `EXTRAS` 接口编译，不向 gem5 checkout
复制源码。`--component fastsim` 构建并运行 `fastsim_tests`；
`--component producer` 构建 QEMU system binary、`qemu-img`、官方通用 plugin
测试和 native collector。事件、cutoff 和 helper
语义只由真实 workload 的公开 capture → lower → replay 门禁验证，不维护
BUG 专用 guest。

开发态门禁固定 QEMU `v11.0.0@98b060da`、qemu_tracer
`origin/master@abd8dc2b` 与 gem5 `v25.1.0.1@c8222cc6` 官方基线；QEMU 和
qemu_tracer 只要求 HEAD 派生自对应基线，允许未提交工作树构建。旧 integration
commit 不再作为 ancestry gate；`patches/qemu_fst/` 已刷新到当前验收工作树，
但同样不阻塞后续本地开发验收。

## Run

CLI 不带 `--workload` 时会按 descriptor 顺序运行十个 C4 workload；这是未来
扩展能力，不是本轮自动授权。本轮三阶段验收必须显式传
`--workload 706.stockfish_r`。唯一测量合同为
`first-core-macro-common-end`：首个核心完成 `1,000,000` 个 measurement
CPL3 宏后触发共同结束。其他核心保留实际已退休人口，不填充、不等待其达到目标，
也不按 UOP 二次截断。kernel UOP 和 syscall/event marker 不消费宏目标。
raw Full/Delta 由 assembler 唯一合并为 QEMU architectural shadow；每个 frame
完整覆盖 gem5 的 architectural input，仅 gem5 micro temporaries、decoder/PC
microstate、availability 与 frame memory 逐 frame 重置。gem5 执行结果不作为
下一 frame 的 QEMU 输入。
每个 worker 用 canonical transaction assembler 扫描本核 raw 到 footer，并将实际
`measurement_user_macros`、唯一 `trigger_core` 和共同结束合同写入
`boundaries.json`。gem5 `WarnUnimplemented` 与
`FailUnimplemented` 完全遵循固定官方版本的执行结果。

600 秒只限制 QEMU process launch 到 producer 发布 `.measurement-started` 的宿主
墙钟等待时间；它不指定或截断 warmup record 数。到达 measurement 后，真实
prefix 完整进入同一 FST，并由 `fastsim-binary-warmup-slice` 在 replay 时建立状态。

`--jobs` 控制同时执行的 workload 数，默认 `5`；单负载诊断可用 `--jobs 1`。
每个 workload 仍独立执行完整
`capture → lower → replay`，其中 `lower` 固定启动
四个互相独立的 per-core gem5 worker 并行处理 C4 raw shard；全部 worker 成功后
才原子发布。一个 workload 失败不会取消已启动的其他 workload，批次等待全部任务
收尾后汇总失败。syscall 返回只在同一 shard 内唯一匹配，迁核、歧义或截止前未返回
时保持 unavailable：

lower 默认从当前 cpuset 选择同一 NUMA node 的四个不同物理核，并分别绑定 worker
CPU 与本地内存。复现实验时可显式指定：

```bash
python -m tools.fst_pipeline lower \
  --raw-run-id captured-c4 --run-id lowered-c4 \
  --workload 706.stockfish_r --lower-cpus 0,1,2,3
```

每次 lower 输出各 worker 的 wall time。需要拆分 assembler、官方 gem5 lowering
和 companion close 时间时设置 `QEMU_FST_PROFILE=1`；该变量只增加 stderr
诊断，不改变 raw、FST 或 acceptance artifact。

```text
var/qemu_fst/runs/<run-id>/qemu/c04/<workload>
```

正式验收只写入：

```text
var/qemu_fst/runs/production/
  qemu/c04/<workload>/{acceptance.json,raw,fst,replay}
```

`--gem5-fst-root` 是可选的只读参考根，其布局为
`<root>/c04/<workload>/{core0.fst,...}`。提供时，production acceptance 在完成
QEMU capture、lower 和 replay 后附加 OpClass、宏 UOP、operand、memory、branch、
serialize、syscall 与同 ASID 用户依赖分布报告。reference 缺失、损坏或 Dual-ELF
分布差异均不影响 QEMU production pass/fail；本地 `.io` 不参与共有 FST v7 特征
对比。

`acceptance.json` 是状态权威：`failed` 允许下一次完整 `run` 原位重建；
`running`、损坏状态与 `passed` 均拒绝覆盖。

输出包含 raw shards、`coreN.fst`、`.deps`、`.vmap`、`.asmap`、`.io`，
单 ASID 时的完整 operand `.imap`，
`boundaries.json`、`manifest.txt` 和 `replay/stats.json`。`manifest.txt` 以每核
精确 record 边界隔离 warmup 与 measurement。FastSim reader 在 replay 流内验证
`.vmap/.asmap` 的 token、首次 ordinal、ASID、PA 和全量消费；lowerer 只对可选
`.imap` 做轻量静态审计，不再额外全量扫描 FST。`.imap` 以 PC 为键，包含静态
几何、barrier/locked-RMW 与 x86 architectural operand masks；多 ASID stream
省略 `.imap`，由动态 FST、`.asmap` 和 `.vmap` 提供功能真值。descriptor 中的
guest memory 同时驱动 FastSim `--dram-size`，当前为 3 GiB。FST v7 bit 7 与
稀疏 `.io` 显式区分 MMIO/PIO；`allow_mmio_escape` 保留 core/LSQ 生命周期并
绕过 RAM hierarchy，不模拟设备服务。当前集成状态与阶段门禁见
[`docs/qemu_fst/mainline.md`](../../docs/qemu_fst/mainline.md)，上一版历史证据见
[`docs/qemu_fst/history.md`](../../docs/qemu_fst/history.md)。

完整 `run` 可写 `production`；`lower/replay` 禁止增量修改 `production`。
若 production workload 已失败但保留完整 raw，可在修复 converter 或 replay 后
原位恢复：

```bash
python -m tools.fst_pipeline retry \
  --run-id production --jobs 1 --workload 857.namd_s
```

`retry` 只接受 `production` 和显式 workload。`failed/lower` 复用原 raw 并重建
`fst/replay`；`failed/replay` 或 `failed/validate` 保留原 raw/FST，只重建 replay。
它不创建其他 run-id、raw 副本或诊断目录，并拒绝覆盖 `passed`。

其他 run-id 只用于显式阶段诊断：

```bash
python -m tools.fst_pipeline lower \
  --raw-run-id captured-c4 --run-id lowered-c4 \
  --workload 777.zstd_r
python -m tools.fst_pipeline replay \
  --run-id lowered-c4 --workload 777.zstd_r
```

TaoTrace 不属于 QEMU 生产链。标准 TaoTrace、训练与模型入口继续使用
`origin/FastSim` 的既有实现；`taotrace` 子命令仅在同一 descriptor、C4 与
`first-core-macro-common-end` 合同下发布只读 reference。QEMU `run`、`retry`、
`lower`、`replay` 不要求 reference；需要附加对比时才显式传入
`--gem5-fst-root`。也可独立运行：

```bash
python -m tools.fst_producer_report \
  --workload 706.stockfish_r \
  --gem5-fst-dir /readonly/gem5-fst/c04/706.stockfish_r \
  --qemu-fst-dir var/qemu_fst/runs/qemu-c4/qemu/c04/706.stockfish_r/fst
```

命令只输出 agent 可读 JSON：两侧各自 measurement 区间的 per-core 与 aggregate
FST 功能特征，以及 `qemu - gem5` 差值。它不截断为伪共同窗口、不输出 pass/fail，
也不比较 ELF、PC、地址、时间、ASID/CPL 序列、物理地址或 replay 性能。

当前分支已完成 frame RAM、PIO 与 FST bit 7 实现。现行 production 证据只以
`acceptance.json` 与同目录的 raw/FST/replay 产物为准；历史 per-core UOP
cutoff 矩阵不作为当前窗口合同或对比基线。

## 当前验证结果

当前 smoke 是 `runs/production/qemu/c04/706.stockfish_r/`，状态为
`passed/complete`。它使用 `first-core-macro-common-end`：core 1 精确退休
`1,000,000` 个 measurement CPL3 宏后共同结束；四核实际人口为
`3,876,704` CPL3 宏、`7,987,914` CPL3 UOP 和 `9,417,721` kernel UOP。
replay 的 `unknown_addresses`、`branches_without_outcome` 与
`page_fault_untracked_accesses` 均为零，功能与 timing DTLB 守恒通过。

该结果只证明 QEMU-FST 的功能转换与 FastSim 消费闭环；不用于声明
CPI/cache/DRAM timing 与其他 producer 等价。历史 UOP cutoff 矩阵和旧性能数字
由 `docs/qemu_fst/history.md` 保存。

lowerer 由 gem5 官方 micro-PC、Fault ROM 和 `isLastMicroop()` 自然终止，不设置
固定 UOP 上限。
