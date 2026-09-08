"""C4 acceptance for the canonical QEMU-FST producer."""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
from pathlib import Path
from typing import Any

from .capture import FstAssets, collect_workload
from .lower import convert_qemu_fst_trace
from .workloads import (
    CANONICAL_CORES,
    PROJECT_ROOT,
    fastsim_binary,
    load_workloads,
    run_fastsim,
    selected_workloads,
)


RUN_ROOT = PROJECT_ROOT / "var/qemu_fst/runs"
CANONICAL_WORKLOADS = PROJECT_ROOT / "configs/qemu_fst/spec2026_c4.json"


def _output_dir(run_root: Path, workload: str) -> Path:
    return run_root / f"c{CANONICAL_CORES:02d}" / workload


def _publish(staging: Path, output: Path) -> None:
    previous = output.with_name(f".{output.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if output.exists():
        os.replace(output, previous)
    try:
        os.replace(staging, output)
    except Exception:
        if previous.exists() and not output.exists():
            os.replace(previous, output)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _accept_one(
    *,
    args: argparse.Namespace,
    workload: Any,
    assets: FstAssets,
    converter: Path,
    fastsim: Path,
    replay_config: Path,
) -> dict[str, Any]:
    output = _output_dir(args.output_root.resolve(), workload.name)
    staging = output.with_name(f".{output.name}.staging")
    if output.exists() and not args.force:
        raise FileExistsError(
            f"QEMU-FST acceptance output exists: {output}"
        )
    if staging.exists():
        if not args.force:
            raise FileExistsError(
                f"QEMU-FST staging output exists: {staging}"
            )
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    capture = collect_workload(
        workload=workload,
        memory=args.memory,
        timeout_seconds=args.timeout_seconds,
        force=args.force,
        raw_macro_envelope=args.capture_instruction_limit,
        assets=assets,
        output_dir=staging / "raw",
        qemu_library_dir=args.qemu_libdir,
    )
    fst_dir = staging / "fst"
    convert_qemu_fst_trace(
        trace_dir=capture.trace_dir,
        output_dir=fst_dir,
        num_cores=CANONICAL_CORES,
        converter=converter,
        measurement_user_record_target=args.user_fst_target,
    )
    manifest = fst_dir / "manifest.txt"

    replay_dir = staging / "replay"
    replay_dir.mkdir()
    stats_path = replay_dir / "stats.json"
    run_fastsim(
        fastsim=fastsim,
        config=replay_config,
        manifest=manifest,
        output=stats_path,
        log=replay_dir / "fastsim.log",
        measurement_scope="user",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish(staging, output)
    return {
        "workload": workload.name,
        "output": output,
        "raw": capture.trace_dir,
    }


def run(args: argparse.Namespace) -> int:
    workloads = selected_workloads(
        load_workloads(CANONICAL_WORKLOADS), tuple(args.workload)
    )
    converter = args.converter.resolve()
    if not converter.is_file():
        raise FileNotFoundError(f"gem5 converter missing: {converter}")
    fastsim = fastsim_binary(args.fastsim)
    replay_config = args.config.resolve()
    if not replay_config.is_file():
        raise FileNotFoundError(
            f"QEMU-FST replay config missing: {replay_config}"
        )
    assets = FstAssets(
        kernel=args.kernel.resolve(),
        initramfs=args.initramfs.resolve(),
        workload_disk=args.workload_disk.resolve(),
        qemu=args.qemu.resolve(),
        plugin=args.plugin.resolve(),
        launcher=args.launcher.resolve(),
    )
    accepted = []
    for workload in workloads:
        lock_path = (
            args.output_root.resolve() / f"c{CANONICAL_CORES:02d}"
            / f".{workload.name}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    f"QEMU-FST acceptance is already running: "
                    f"{workload.name}"
                ) from error
            accepted.append(
                _accept_one(
                    args=args,
                    workload=workload,
                    assets=assets,
                    converter=converter,
                    fastsim=fastsim,
                    replay_config=replay_config,
                )
            )

    for result in accepted:
        print(
            f"{result['workload']}: FastSim strict fs-user replay passed; "
            f"artifacts: {result['output']}"
        )
    return 0
