# QEMU-FST Workflow

唯一生产链是：

```text
QEMU full-system capture → gem5 x86 EXTRAS lowering → FST v7 → FastSim replay
```

所有操作通过一个入口完成：

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh
python -m tools.qemu_fst prepare
python -m tools.qemu_fst build
python -m tools.qemu_fst accept
```

## Prepare

`prepare` 固定读取 `configs/qemu_fst/spec2026_c4.json`，从只读历史来源物化十个
SPEC CPU 2026 C4 workload，并生成：

```text
var/qemu_fst/workloads/{build,run}
var/qemu_fst/assets/initramfs.cpio.gz
var/qemu_fst/assets/user-only-workloads.ext4
```

完整产物会被复用；部分存在或结构不完整时命令失败，不静默覆盖。

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

## Accept

`accept` 默认运行完整十 workload、C4、每核 10,000,000 user UOP 和 12,000,000
capture envelope。`--workload` 只允许选择正式矩阵中的 workload。

每例串行执行 `capture → lower → replay`，成功后原子发布到：

```text
var/qemu_fst/runs/c04/<workload>
```

输出包含 raw shards、`coreN.fst`、`.vmap`、`.asmap`、`boundaries.json`、
`manifest.txt` 和 `replay/stats.json`。`manifest.txt` 以每核精确 record 边界隔离
warmup 与 measurement。没有 raw-only、lower-only、reuse、dynamic-check 或独立
audit/validate 入口。详细数据合同见
[`docs/qemu_fst/user-only.md`](../../docs/qemu_fst/user-only.md)。
