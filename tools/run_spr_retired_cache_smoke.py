#!/usr/bin/env python3
"""Collect a small real FS observer case with the maintained storage guard."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def command(args):
    capture = [
        str(args.gem5), "--outdir=" + str(args.output),
        str(ROOT / "gem5-spr/configs/fastsim/fs_capture.py"),
        "--fastsim-root", str(ROOT), "--workload-binary", str(args.binary),
        "--workload-args", str(args.mode), "--participant-cores", "4",
        "--functional-user-target", str(args.target),
        "--warmup-insts", str(args.warmup), "--rate-copies", "1",
        "--native-workers", "4", "--rate-launcher", str(args.launcher),
        "--rate-private-cwd", "--native-warmup-after-identities",
        "--thread-state", "--task-clock", "--scheduler-profile", str(args.scheduler_profile),
        "--mem-size", "32GiB", "--x86-large-memory", "--housekeeping-cpu", "0",
    ]
    if args.checkpoint:
        capture += ["--restore-staged-checkpoint", str(args.checkpoint)]
    if args.save_checkpoint:
        capture += ["--save-staged-checkpoint", str(args.save_checkpoint), "--checkpoint-only"]
    if args.kernel:
        capture += ["--kernel", str(args.kernel.resolve())]
    if args.disk:
        capture += ["--disk", str(args.disk.resolve()),
                    "--disk-root-partition", args.disk_root_partition]
    return [sys.executable, str(ROOT / "tools/trace_capture_storage.py"),
            "--root", str(ROOT), "--output", str(args.output),
            "--case-cap-bytes", "1000000000", "--wall-seconds", "7200",
            "--warmup-wall-seconds", "0", "--", *capture]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--gem5", type=Path, default=ROOT / "gem5-spr/build/X86/gem5.opt")
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--kernel", type=Path, help="reuse a verified local guest kernel")
    p.add_argument("--disk", type=Path, help="reuse a verified local guest disk")
    p.add_argument("--disk-root-partition", default="1")
    p.add_argument("--mode", type=int, choices=(0, 1, 2), required=True)
    p.add_argument("--target", type=int, default=50_000)
    p.add_argument("--warmup", type=int, default=500_000)
    p.add_argument("--launcher", type=Path,
                   default=ROOT / "tmp/spec-speed-32g-c4-c8-c16-20261027-final/frozen/native-launcher")
    p.add_argument("--scheduler-profile", type=Path,
                   default=ROOT / "tmp/spr-asid-task-clock-20261104/kernel-scheduler-profile.json")
    checkpoints = p.add_mutually_exclusive_group()
    checkpoints.add_argument("--checkpoint", type=Path)
    checkpoints.add_argument("--save-checkpoint", type=Path)
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    args.output = args.output.resolve()
    args.output.relative_to(ROOT)
    if args.target <= 0 or args.warmup <= 0:
        raise ValueError("smoke requires positive target and warmup")
    argv = command(args)
    print(json.dumps(dict(argv=argv, special_path_coverage="requires actual observer evidence"),
                     indent=2), flush=True)
    if not args.run:
        return 0
    if args.output.exists():
        raise ValueError("refuse to overwrite an existing smoke source")
    env = dict(os.environ, M5_OVERRIDE_PY_SOURCE="true",
               LD_LIBRARY_PATH="/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64:"
                               "/home/data00/xuhaoen/toolchains/protobuf/lib")
    return subprocess.run(argv, cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
