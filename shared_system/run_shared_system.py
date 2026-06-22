#!/usr/bin/env python3
"""Run the LLMSim shared-system simulator on global memory-event JSONL files.

The C++ simulator maintains cache/coherence/TLB/MSHR state across the complete
input stream. Insert {"event_type":"snapshot"} or {"event_type":"window_end"}
records into the input to force an immediate cumulative PMU snapshot.

Two run modes:

1. Single file (legacy):
   --events <file.jsonl> --out <pmu.jsonl>

2. Batch directory (offline LLMSim eval workflow):
   --events-dir <dir>   reads every <name>.mem_events.jsonl in dir
   --out-dir <dir>      writes <name>.shared_pmu.jsonl per workload
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "config" / "uarch_profile_arch_A.json"
DEFAULT_BIN = (
    ROOT / "shared_system" / "mesi_ref_sim" / "build" / "llmsim_shared_system"
)
EVENT_SUFFIX = ".mem_events.jsonl"
OUT_SUFFIX = ".shared_pmu.jsonl"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="",
                    help="Input JSONL global memory sequence (single-file mode).")
    ap.add_argument("--out", default="",
                    help="Output JSONL PMU snapshots (single-file mode).")
    ap.add_argument("--events-dir", default="",
                    help="Directory containing <workload>.mem_events.jsonl files.")
    ap.add_argument("--out-dir", default="",
                    help="Directory to write <workload>.shared_pmu.jsonl files. "
                         "Defaults to --events-dir.")
    ap.add_argument("--workload", action="append", default=[],
                    help="Restrict batch run to specific workload names "
                         "(can be passed multiple times).")
    ap.add_argument("--uarch-profile", default=str(DEFAULT_PROFILE),
                    help="MTAO uarch_profile.json compatible config.")
    ap.add_argument("--binary", default=str(DEFAULT_BIN),
                    help="Built llmsim_shared_system executable.")
    ap.add_argument("--snapshot-interval", type=int, default=0,
                    help="Emit a cumulative snapshot every N memory events "
                         "(in addition to per-window snapshots).")
    ap.add_argument("--build", action="store_true",
                    help="Build the C++ simulator before running.")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="In batch mode, do not abort on a single failure.")
    return ap.parse_args()


def build_binary() -> None:
    src = ROOT / "shared_system" / "mesi_ref_sim"
    build = src / "build"
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmake", "-S", str(src), "-B", str(build)], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j"], check=True)


def runtime_env() -> dict:
    env = os.environ.copy()
    try:
        lib = subprocess.check_output(
            ["g++", "-print-file-name=libstdc++.so.6"],
            text=True,
        ).strip()
    except Exception:
        lib = ""
    if lib and lib != "libstdc++.so.6":
        libdir = str(Path(lib).resolve().parent)
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = libdir if not old else f"{libdir}:{old}"
    return env


def discover_jobs(events_dir: Path, out_dir: Path,
                  filter_names: List[str]) -> List[Tuple[str, Path, Path]]:
    jobs: List[Tuple[str, Path, Path]] = []
    if not events_dir.is_dir():
        sys.stderr.write(f"[shared_system] events-dir not found: {events_dir}\n")
        return jobs
    name_set = set(filter_names) if filter_names else None
    for entry in sorted(os.listdir(events_dir)):
        if not entry.endswith(EVENT_SUFFIX):
            continue
        name = entry[: -len(EVENT_SUFFIX)]
        if name_set is not None and name not in name_set:
            continue
        ev = events_dir / entry
        out = out_dir / f"{name}{OUT_SUFFIX}"
        jobs.append((name, ev, out))
    return jobs


def run_one(binary: Path, profile: str, events: Path, out: Path,
            snapshot_interval: int, env: dict) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(binary),
        profile,
        str(events),
        str(out),
    ]
    if snapshot_interval:
        cmd.append(f"--snapshot-interval={snapshot_interval}")
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def main() -> int:
    args = parse_args()
    if args.build:
        build_binary()

    binary = Path(args.binary)
    if not binary.exists():
        sys.stderr.write(
            f"[shared_system] binary not found: {binary}\n"
            "Run with --build first.\n"
        )
        return 2

    env = runtime_env()
    profile = args.uarch_profile

    if args.events_dir:
        events_dir = Path(args.events_dir)
        out_dir = Path(args.out_dir or args.events_dir)
        jobs = discover_jobs(events_dir, out_dir, args.workload)
        if not jobs:
            sys.stderr.write(
                f"[shared_system] no *{EVENT_SUFFIX} files matched in {events_dir}\n"
            )
            return 2
        rc_total = 0
        for name, ev, out in jobs:
            print(f"[shared_system] {name}: {ev} -> {out}", flush=True)
            rc = run_one(binary, profile, ev, out, args.snapshot_interval, env)
            if rc != 0:
                rc_total = rc
                sys.stderr.write(
                    f"[shared_system] {name} failed with exit code {rc}\n"
                )
                if not args.continue_on_error:
                    return rc
        return rc_total

    if not args.events or not args.out:
        sys.stderr.write(
            "[shared_system] either --events-dir or both --events and --out "
            "must be provided\n"
        )
        return 2

    return run_one(
        binary, profile,
        Path(args.events), Path(args.out),
        args.snapshot_interval, env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
