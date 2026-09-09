"""Unified QEMU-first FST v7 workflow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools import fst_pmu_compare
from tools.qemu_fst import accept as qemu_accept
from tools.qemu_fst import build as qemu_build
from tools.qemu_fst import prepare as qemu_prepare
from tools.qemu_fst._assets import ASSET_ROOT, USER_ONLY_WORKLOAD_DISK_NAME
from tools.qemu_fst.lower import DEFAULT_CONVERTER

from .descriptor import DEFAULT_DESCRIPTOR, load_pipeline, select


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "var/qemu_fst/runs"
DEFAULT_REPLAY_CONFIG = PROJECT_ROOT / "configs/gem5-v28_1-fs-user.cfg"
CANONICAL_UBUNTU = ASSET_ROOT / qemu_prepare.CANONICAL_UBUNTU_NAME


def _run_root(run_id: str, producer: str) -> Path:
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
        argparse.Namespace(component=list(components), jobs=args.jobs)
    )


def _qemu_run(
    args: argparse.Namespace, pipeline, workloads
) -> int:
    output_root = _run_root(args.run_id, "qemu")
    target = args.user_fst_target or pipeline.user_fst_target
    kernel = Path(pipeline.environment["kernel"])
    qargs = argparse.Namespace(
        output_root=output_root,
        workload=[workload.name for workload in workloads],
        user_fst_target=target,
        capture_instruction_limit=(
            args.user_fst_target or pipeline.raw_macro_envelope
        ),
        jobs=args.jobs,
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
            / "qemu_tracer_qemu/build/qemu-system-x86_64"
        ),
        plugin=(
            WORKSPACE_ROOT
            / "qemu_tracer/backend/dumper/build/libdumper.so"
        ),
        launcher=(
            WORKSPACE_ROOT / "qemu_tracer/scripts/run_fst_x86.sh"
        ),
        kernel=kernel,
        rootfs=CANONICAL_UBUNTU,
        workload_disk=ASSET_ROOT / USER_ONLY_WORKLOAD_DISK_NAME,
        qemu_libdir=None,
        force=args.force,
    )
    return qemu_accept.run_with_workloads(qargs, workloads)


def _run(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload, pilots=args.pilot)
    return _qemu_run(args, pipeline, workloads)


def _compare(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.descriptor)
    workloads = select(pipeline, args.workload)
    qemu_root = _run_root(args.run_id, "qemu") / "c04"
    cargs = argparse.Namespace(
        qemu_root=qemu_root,
        output_root=DEFAULT_RUN_ROOT / args.run_id / "compare/c04",
        workload=[workload.name for workload in workloads],
        cores=pipeline.cores,
    )
    if args.taotrace_dataset is not None:
        cargs.taotrace_dataset = args.taotrace_dataset.resolve()
        cargs.taotrace_inference = args.taotrace_inference.resolve()
        cargs.taotrace_root = None
    else:
        cargs.taotrace_dataset = None
        cargs.taotrace_root = args.taotrace_root.resolve()
    return fst_pmu_compare.run(cargs)


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
    build.add_argument("--jobs", type=int, default=24)
    build.set_defaults(func=_build)

    run = sub.add_parser("run")
    _add_common(run)
    run.add_argument("--run-id", required=True)
    run.add_argument("--workload", action="append", default=[])
    run.add_argument("--pilot", action="store_true")
    run.add_argument("--user-fst-target", type=int)
    run.add_argument("--jobs", type=int, default=1)
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=_run)

    compare = sub.add_parser("compare")
    _add_common(compare)
    compare.add_argument("--run-id", required=True)
    tao = compare.add_mutually_exclusive_group(required=True)
    tao.add_argument("--taotrace-root", type=Path)
    tao.add_argument(
        "--taotrace-dataset",
        type=Path,
        help="origin/FastSim formal dataset root (index.json + cases/)",
    )
    compare.add_argument(
        "--taotrace-inference",
        type=Path,
        help="origin/FastSim run_fst_v7_formal_inference.py output root",
    )
    compare.add_argument("--workload", action="append", default=[])
    compare.set_defaults(func=_compare)

    args = parser.parse_args(argv)
    if getattr(args, "taotrace_dataset", None) is not None and (
        args.taotrace_inference is None
    ):
        parser.error("--taotrace-dataset requires --taotrace-inference")

    if hasattr(args, "jobs") and args.jobs <= 0:
        parser.error("--jobs must be positive")
    if (
        hasattr(args, "user_fst_target")
        and args.user_fst_target is not None
        and args.user_fst_target <= 0
    ):
        parser.error("--user-fst-target must be positive")
    try:
        return args.func(args)
    except Exception as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
