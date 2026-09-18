"""Unified QEMU-first FST v7 workflow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.qemu_fst import accept as qemu_accept
from tools.qemu_fst import build as qemu_build
from tools.qemu_fst import prepare as qemu_prepare
from tools.qemu_fst._assets import ASSET_ROOT, WORKLOAD_DISK_NAME
from tools.qemu_fst.lower import DEFAULT_CONVERTER
from tools.qemu_fst.workloads import parse_memory_bytes
from tools.taotrace_fst import collect as taotrace_collect

from .descriptor import DEFAULT_DESCRIPTOR, load_pipeline, select


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "var/qemu_fst/runs"
DEFAULT_REPLAY_CONFIG = PROJECT_ROOT / "configs/gem5-fs-native-kernel.cfg"
CANONICAL_UBUNTU = ASSET_ROOT / qemu_prepare.CANONICAL_UBUNTU_NAME


def _parse_lower_cpus(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    try:
        cpus = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise ValueError(
            "--lower-cpus must be a comma-separated CPU list"
        ) from error
    if not cpus or any(cpu < 0 for cpu in cpus):
        raise ValueError("--lower-cpus must contain non-negative CPU IDs")
    return cpus


def _require_run_id(run_id: str) -> None:
    if (
        not run_id
        or Path(run_id).name != run_id
        or run_id in {".", ".."}
    ):
        raise ValueError("run-id must be one directory name")


def _require_writable_run_id(
    run_id: str, *, allow_production: bool = False,
) -> None:
    _require_run_id(run_id)
    if run_id == "production" and not allow_production:
        raise ValueError(
            "run-id=production is a read-only promoted result; "
            "only a complete run may update its failed workload"
        )


def _run_root(run_id: str, producer: str) -> Path:
    _require_run_id(run_id)
    return DEFAULT_RUN_ROOT / run_id / producer


def _prepare(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.descriptor)
    if args.assets_only:
        ubuntu = qemu_prepare._build_canonical_ubuntu(
            Path(pipeline.environment["rootfs_base"]).resolve(),
            CANONICAL_UBUNTU,
            timezone=pipeline.timezone,
            force=args.force,
        )
        print(f"ubuntu: {ubuntu}")
        return 0
    return qemu_prepare.prepare_pipeline(
        pipeline.workloads,
        descriptor=pipeline.path,
        ubuntu_base=Path(pipeline.environment["rootfs_base"]),
        timezone=pipeline.timezone,
        isa_baseline=pipeline.isa_baseline,
        force=args.force,
    )


def _build(args: argparse.Namespace) -> int:
    components = tuple(dict.fromkeys(args.component))
    return qemu_build.run(
        argparse.Namespace(
            component=list(components), jobs=qemu_build.DEFAULT_JOBS
        )
    )


def _qemu_run(
    args: argparse.Namespace, pipeline, workloads
) -> int:
    output_root = _run_root(args.run_id, "qemu")
    target = args.user_macro_target or pipeline.user_macro_target
    kernel = Path(pipeline.environment["kernel"])
    qargs = argparse.Namespace(
        output_root=output_root,
        gem5_fst_root=args.gem5_fst_root,
        workload=[workload.name for workload in workloads],
        jobs=args.jobs,
        user_macro_target=target,
        memory=pipeline.memory,
        timeout_seconds=pipeline.total_timeout_seconds,
        warmup_timeout_seconds=pipeline.warmup_timeout_seconds,
        kernel_args=pipeline.kernel_args,
        network=pipeline.network,
        measurement_scope=pipeline.measurement_scope,
        fastsim=PROJECT_ROOT / "build/fastsim",
        converter=DEFAULT_CONVERTER,
        config=DEFAULT_REPLAY_CONFIG,
        qemu=(
            WORKSPACE_ROOT
            / "qemu/build/qemu-system-x86_64"
        ),
        plugin=(
            WORKSPACE_ROOT
            / "qemu_tracer/backend/dumper/build/libdumper.so"
        ),
        kernel=kernel,
        rootfs=CANONICAL_UBUNTU,
        workload_disk=ASSET_ROOT / WORKLOAD_DISK_NAME,
        dram_size=parse_memory_bytes(pipeline.memory),
        lower_cpus=_parse_lower_cpus(args.lower_cpus),
    )
    return qemu_accept.run_with_workloads(qargs, workloads)


def _run(args: argparse.Namespace) -> int:
    _require_writable_run_id(args.run_id, allow_production=True)
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload)
    return _qemu_run(args, pipeline, workloads)


def _taotrace(args: argparse.Namespace) -> int:
    _require_writable_run_id(args.run_id, allow_production=True)
    return taotrace_collect.collect(
        argparse.Namespace(
            descriptor=args.descriptor,
            output_root=_run_root(args.run_id, "gem5"),
            asset_root=taotrace_collect.DEFAULT_ASSET_ROOT,
            workload=args.workload,
            user_macro_target=args.user_macro_target,
            gem5=args.gem5,
            config=args.config,
            checkpoint=args.checkpoint,
            resource_dir=args.resource_dir,
            aux_disk=args.aux_disk,
            stockfish_elf=args.stockfish_elf,
            tcsim_root=args.tcsim_root,
            dry_run=args.dry_run,
        )
    )


def _retry(args: argparse.Namespace) -> int:
    if args.run_id != "production":
        raise ValueError("retry requires --run-id production")
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload)
    target = args.user_macro_target or pipeline.user_macro_target
    qargs = argparse.Namespace(
        output_root=_run_root(args.run_id, "qemu"),
        gem5_fst_root=args.gem5_fst_root,
        jobs=args.jobs,
        user_macro_target=target,
        measurement_scope=pipeline.measurement_scope,
        dram_size=parse_memory_bytes(pipeline.memory),
        fastsim=PROJECT_ROOT / "build/fastsim",
        converter=DEFAULT_CONVERTER,
        config=DEFAULT_REPLAY_CONFIG,
        lower_cpus=_parse_lower_cpus(args.lower_cpus),
    )
    return qemu_accept.retry_with_workloads(qargs, workloads)


def _lower(args: argparse.Namespace) -> int:
    _require_writable_run_id(args.run_id)
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload)
    qargs = argparse.Namespace(
        output_root=_run_root(args.run_id, "qemu"),
        raw_root=_run_root(args.raw_run_id, "qemu"),
        gem5_fst_root=args.gem5_fst_root,
        user_macro_target=(
            args.user_macro_target
            or pipeline.user_macro_target
        ),
        converter=DEFAULT_CONVERTER,
        lower_cpus=_parse_lower_cpus(args.lower_cpus),
    )
    return qemu_accept.lower_with_workloads(qargs, workloads)


def _replay(args: argparse.Namespace) -> int:
    _require_writable_run_id(args.run_id)
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload)
    qargs = argparse.Namespace(
        output_root=_run_root(args.run_id, "qemu"),
        gem5_fst_root=args.gem5_fst_root,
        user_macro_target=(
            args.user_macro_target or pipeline.user_macro_target
        ),
        measurement_scope=pipeline.measurement_scope,
        dram_size=parse_memory_bytes(pipeline.memory),
        fastsim=PROJECT_ROOT / "build/fastsim",
        config=DEFAULT_REPLAY_CONFIG,
    )
    return qemu_accept.replay_with_workloads(qargs, workloads)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--descriptor", type=Path, default=DEFAULT_DESCRIPTOR,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.fst_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    _add_common(prepare)
    prepare.add_argument("--assets-only", action="store_true")
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=_prepare)

    build = sub.add_parser("build")
    build.add_argument("--component", action="append", default=[])
    build.set_defaults(func=_build)

    run = sub.add_parser("run")
    _add_common(run)
    run.add_argument("--run-id", required=True)
    run.add_argument("--workload", action="append", default=[])
    run.add_argument("--jobs", type=int, default=5)
    run.add_argument("--user-macro-target", type=int)
    run.add_argument("--gem5-fst-root", type=Path)
    run.add_argument("--lower-cpus")
    run.set_defaults(func=_run)

    taotrace = sub.add_parser("taotrace")
    _add_common(taotrace)
    taotrace.add_argument("--run-id", required=True)
    taotrace.add_argument("--workload", action="append", default=[])
    taotrace.add_argument("--user-macro-target", type=int)
    taotrace.add_argument(
        "--gem5", type=Path, default=taotrace_collect.DEFAULT_GEM5,
    )
    taotrace.add_argument(
        "--config", type=Path, default=taotrace_collect.DEFAULT_CONFIG,
    )
    taotrace.add_argument(
        "--checkpoint",
        type=Path,
        default=taotrace_collect.DEFAULT_STOCKFISH_CHECKPOINT,
    )
    taotrace.add_argument(
        "--resource-dir",
        type=Path,
        default=taotrace_collect.DEFAULT_RESOURCE_DIR,
    )
    taotrace.add_argument(
        "--aux-disk", type=Path, default=taotrace_collect.DEFAULT_AUX_DISK,
    )
    taotrace.add_argument(
        "--stockfish-elf",
        type=Path,
        default=taotrace_collect.DEFAULT_STOCKFISH_ELF,
    )
    taotrace.add_argument(
        "--tcsim-root",
        type=Path,
        default=taotrace_collect.DEFAULT_TCSIM_ROOT,
    )
    taotrace.add_argument("--dry-run", action="store_true")
    taotrace.set_defaults(func=_taotrace)

    retry = sub.add_parser("retry")
    _add_common(retry)
    retry.add_argument("--run-id", required=True)
    retry.add_argument("--workload", action="append", required=True)
    retry.add_argument("--jobs", type=int, default=5)
    retry.add_argument("--user-macro-target", type=int)
    retry.add_argument("--gem5-fst-root", type=Path)
    retry.add_argument("--lower-cpus")
    retry.set_defaults(func=_retry)

    lower = sub.add_parser("lower")
    _add_common(lower)
    lower.add_argument("--run-id", required=True)
    lower.add_argument("--raw-run-id", required=True)
    lower.add_argument("--workload", action="append", default=[])
    lower.add_argument("--user-macro-target", type=int)
    lower.add_argument("--gem5-fst-root", type=Path)
    lower.add_argument("--lower-cpus")
    lower.set_defaults(func=_lower)

    replay = sub.add_parser("replay")
    _add_common(replay)
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--workload", action="append", default=[])
    replay.add_argument("--user-macro-target", type=int)
    replay.add_argument("--gem5-fst-root", type=Path)
    replay.set_defaults(func=_replay)

    args = parser.parse_args(argv)
    if (
        hasattr(args, "user_macro_target")
        and args.user_macro_target is not None
        and args.user_macro_target <= 0
    ):
        parser.error("--user-macro-target must be positive")
    if hasattr(args, "jobs") and args.jobs <= 0:
        parser.error("--jobs must be positive")
    try:
        return args.func(args)
    except Exception as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
