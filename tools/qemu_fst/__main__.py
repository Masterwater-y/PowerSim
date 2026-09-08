"""CLI entrypoint for the canonical QEMU-to-FST workflow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import accept, build, prepare
from ._assets import ASSET_ROOT, USER_ONLY_WORKLOAD_DISK_NAME
from .lower import DEFAULT_CONVERTER


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QEMU_ROOT = PROJECT_ROOT.parent / "qemu_tracer"
DEFAULT_QEMU_BUILD = PROJECT_ROOT.parent / "qemu_tracer_qemu"
DEFAULT_KERNEL = Path(
    "/data00/yinhaolang/gem5-fs/resources/"
    "x86-linux-kernel-6.8.0-52-generic-1.0.0"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.qemu_fst")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser(
        "prepare",
        help="materialize the canonical SPEC C4 workloads and guest assets",
    )
    p_prepare.set_defaults(func=prepare.run)

    p_build = sub.add_parser(
        "build",
        help="build the canonical FastSim, QEMU, dumper, and gem5 artifacts",
    )
    p_build.add_argument(
        "--component",
        action="append",
        choices=build.COMPONENTS,
        default=[],
        help="build only this component; may be repeated",
    )
    p_build.add_argument("--jobs", type=int, default=build.DEFAULT_JOBS)
    p_build.set_defaults(func=build.run)

    p_accept = sub.add_parser(
        "accept",
        help="run the canonical SPEC C4 capture, lowering, and replay flow",
    )
    p_accept.add_argument(
        "--output-root", type=Path, default=accept.RUN_ROOT,
    )
    p_accept.add_argument("--workload", action="append", default=[])
    p_accept.add_argument("--user-fst-target", type=int, default=10_000_000)
    p_accept.add_argument(
        "--capture-instruction-limit", type=int, default=12_000_000,
    )
    p_accept.add_argument(
        "--memory", default="3G",
        help="guest RAM; the producer uses i440fx to keep 3G below 4GiB",
    )
    p_accept.add_argument("--timeout-seconds", type=int, default=28_800)
    p_accept.add_argument(
        "--fastsim", type=Path, default=PROJECT_ROOT / "build/fastsim",
    )
    p_accept.add_argument("--converter", type=Path, default=DEFAULT_CONVERTER)
    p_accept.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "configs/gem5-v28_1-fs-user.cfg",
    )
    p_accept.add_argument(
        "--qemu", type=Path,
        default=DEFAULT_QEMU_BUILD / "build/qemu-system-x86_64",
    )
    p_accept.add_argument(
        "--plugin", type=Path,
        default=DEFAULT_QEMU_ROOT / "backend/dumper/build/libdumper.so",
    )
    p_accept.add_argument(
        "--launcher", type=Path,
        default=DEFAULT_QEMU_ROOT / "scripts/run_fst_x86.sh",
    )
    p_accept.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    p_accept.add_argument(
        "--initramfs", type=Path, default=ASSET_ROOT / "initramfs.cpio.gz",
    )
    p_accept.add_argument(
        "--workload-disk", type=Path,
        default=ASSET_ROOT / USER_ONLY_WORKLOAD_DISK_NAME,
    )
    p_accept.add_argument("--qemu-libdir", type=Path)
    p_accept.add_argument("--force", action="store_true")
    p_accept.set_defaults(func=accept.run)

    args = parser.parse_args(argv)
    if args.cmd == "build" and args.jobs <= 0:
        parser.error("--jobs must be positive")
    if args.cmd == "accept" and args.user_fst_target <= 0:
        parser.error("--user-fst-target must be positive")
    if args.cmd == "accept" and (
        args.capture_instruction_limit < args.user_fst_target
    ):
        parser.error(
            "--capture-instruction-limit must be at least --user-fst-target"
        )
    try:
        return args.func(args)
    except Exception as error:
        print(f"{args.cmd} failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
