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

`prepare` 固定读取 `configs/fst_pipeline/spec2026_c4.json`，从只读历史来源物化九个
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
与 ROI wave barrier 继续按参考 workload 执行；QEMU tracer 不读取这些信息作为
worker identity，不按 TID/CR3 过滤，也不增加 measurement 协调线程。QEMU
machine/TCG、NOP marker、raw trace 和地址命名空间保持 producer-local。

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

`run` 默认串行运行正式九 workload、C4、每核 10,000,000 user UOP。QEMU raw
envelope 同样为每核 10,000,000 个已提交 CPL3 宏指令；若 lowerer 无法生成每核
10,000,000 user UOP，则直接失败，不自动扩容。

600 秒只限制 producer 从真实 `start` marker 到真实 `measurement` marker 的墙钟
等待时间；它不指定或截断 warmup record 数。到达 measurement 后，真实 prefix
完整进入同一 FST，并由 `fastsim-binary-warmup-slice` 在 replay 时建立状态。

每例串行执行 `capture → lower → replay`，成功后原子发布到：

```text
var/qemu_fst/runs/<run-id>/qemu/c04/<workload>
```

输出包含 raw shards、`coreN.fst`、`.vmap`、`.asmap`、`boundaries.json`、
`manifest.txt` 和 `replay/stats.json`。`manifest.txt` 以每核精确 record 边界隔离
warmup 与 measurement。没有 raw-only、lower-only、reuse、dynamic-check 或独立
audit/validate 入口。详细数据合同见
[`docs/qemu_fst/user-only.md`](../../docs/qemu_fst/user-only.md)。

TaoTrace 不属于该生产链。只读对照必须显式提供已有 FST：

```bash
python -m tools.fst_pipeline compare \
  --run-id qemu-c4 \
  --taotrace-root /path/to/taotrace/c04
```
