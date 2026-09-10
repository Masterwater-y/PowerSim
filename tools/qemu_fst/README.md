# QEMU-FST Workflow

唯一生产链是：

```text
QEMU full-system capture → gem5 x86 EXTRAS lowering → FST v7 → FastSim replay
```

所有操作通过一个入口完成：

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh
python -m tools.fst_pipeline prepare
python -m tools.fst_pipeline build
python -m tools.fst_pipeline run --run-id qemu-c4
```

## Prepare

`prepare` 固定读取 `configs/fst_pipeline/spec2026_c4.json`，从只读历史来源物化十个
SPEC CPU 2026 C4 workload，并生成：

```text
var/qemu_fst/workloads/{build,run}
var/qemu_fst/assets/fst-pipeline-ubuntu-24.04.raw
var/qemu_fst/assets/user-only-workloads.ext4
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
qemu_tracer_qemu/build/qemu-system-x86_64
qemu_tracer/backend/dumper/build/libdumper.so
gem5_fastsim/build/X86_QEMU_FST/gem5.fast
```

增量开发可重复使用 `--component fastsim|qemu|dumper|gem5`。gem5 lowerer 通过
`integrations/gem5/qemu_fst` 的原生 `EXTRAS` 接口编译，不向 gem5 checkout
复制源码。

## Run

`run` 默认串行运行正式十 workload、C4、每核 10,000,000 user UOP。QEMU raw
envelope 同样为每核 10,000,000 个已提交 CPL3 宏指令；若 lowerer 无法生成每核
10,000,000 user UOP，则直接失败，不自动扩容。

600 秒只限制 producer 从真实 `start` marker 到真实 `measurement` marker 的墙钟
等待时间；它不指定或截断 warmup record 数。到达 measurement 后，真实 prefix
完整进入同一 FST，并由 `fastsim-binary-warmup-slice` 在 replay 时建立状态。

每例串行执行 `capture → lower → replay`，成功后原子发布到：

```text
var/qemu_fst/runs/<run-id>/qemu/c04/<workload>
```

正式验收完成后将唯一当前结果提升到：

```text
var/qemu_fst/runs/production/
  qemu/c04/<workload>/{raw,fst,replay}
  imap-audit.json
  syscall-audit.json

var/qemu_fst/diagnostics/
  taotrace-reference/c04/<workload>/{fst,replay}
  comparisons/production/c04/pmu.{json,md}
  validation/threaded-multi-asid/
  taotrace-checkpoints/
```

`runs/production` 只保存 QEMU 主线及其直接合同审计；`runs/` 下的其他目录只允许
作为待验收 QEMU run。TaoTrace reference、跨 producer 对比、机制验证和 checkpoint
统一位于 `diagnostics/`，不与正式 QEMU raw/FST/replay 混放。新 run 验收完成后显式
提升，不要为 smoke、round 或日期另建长期目录。

输出包含 raw shards、`coreN.fst`、`.vmap`、`.asmap` 和 `.imap`、
`boundaries.json`、`manifest.txt` 和 `replay/stats.json`。`manifest.txt` 以每核
精确 record 边界隔离 warmup 与 measurement。FastSim reader 在 replay 流内验证
`.vmap/.asmap` 的 token、首次 ordinal、ASID、PA 和全量消费；lowerer 只对可选
`.imap` 做轻量静态审计，不再额外全量扫描 FST。`.imap` 以
`(address_space_id, pc)` 为键，包含静态几何与 x86 architectural operand masks；
旧 PC-only companion 不再接受。descriptor 中的 guest memory
同时驱动 FastSim `--dram-size`，当前为 3 GiB。任一失败均保留 staging 并拒绝
发布。详细数据合同见
[`docs/qemu_fst/user-only.md`](../../docs/qemu_fst/user-only.md)。

正式 `run` 不提供覆盖选项；已有发布或 staging 目录一律拒绝继续执行，capture
单次失败后保留 raw/log。新实验必须使用新 `--run-id`。复用历史 raw 或已 lower
FST 时只能显式选择阶段。`run/lower/replay` 禁止将 `production` 作为写入目标；
TaoTrace collector 也必须显式指定非 production 输出，且不提供覆盖选项：

```bash
python -m tools.fst_pipeline lower \
  --raw-run-id captured-c4 --run-id lowered-c4 \
  --workload 777.zstd_r
python -m tools.fst_pipeline replay \
  --run-id lowered-c4 --workload 777.zstd_r
python -m tools.taotrace_fst.collect \
  --output-root var/qemu_fst/diagnostics/taotrace-candidates/taotrace-c4
```

真实多地址空间和同 ASID 多线程阻塞 syscall 验收使用独立测试负载，不进入正式
SPEC descriptor：

```bash
python test/qemu_fst/run_multi_asid.py \
  --output-root var/qemu_fst/diagnostics/validation/threaded-multi-asid \
  --user-fst-target 100000
```

TaoTrace 不属于 QEMU 生产链，但它是当前输入替代一致性的 reference FST。对比时
必须显式提供已有 TaoTrace FST。比较工具先要求 replay topology 一致，再按字段域
聚合：功能人口使用全部结构有效 reference；前端/CPI 只使用 static-span 完整项；
cache/TLB/coherence/DRAM 始终标记 producer-sensitive；宿主 throughput 不进入
模型准确性判断。前端不完整 workload 标记为
`frontend_reference_incomplete`，但不从功能或 memory 诊断中整体排除：

```bash
python -m tools.fst_pipeline compare \
  --run-id production \
  --taotrace-root var/qemu_fst/diagnostics/taotrace-reference/c04
```

比较报告默认写入
`var/qemu_fst/diagnostics/comparisons/<run-id>/c04/`，不回写 QEMU run。
该报告只描述两份独立 producer FST 经同一 FastSim 配置后的差异，不构成
QEMU raw→FST 转换门禁。

当前正式矩阵、九项 TaoTrace 可比覆盖和分析记录见
[`docs/qemu_fst/current-status.md`](../../docs/qemu_fst/current-status.md)。
