"""C4 acceptance for the canonical QEMU-FST producer."""
from __future__ import annotations

import argparse
import concurrent.futures
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
    run_fastsim,
)


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
        if not (output / "fst/manifest.txt").is_file() or not (
            output / "replay/stats.json"
        ).is_file():
            raise FileExistsError(
                f"incomplete QEMU-FST acceptance output exists: {output}"
            )
        return {
            "workload": workload.name,
            "output": output,
            "raw": output / "raw",
            "reused": True,
        }
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
        warmup_timeout_seconds=args.warmup_timeout_seconds,
        kernel_args=args.kernel_args,
        network=args.network,
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
        measurement_scope=args.measurement_scope,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish(staging, output)
    return {
        "workload": workload.name,
        "output": output,
        "raw": capture.trace_dir,
        "reused": False,
    }


def run_with_workloads(
    args: argparse.Namespace, workloads: list[Any],
) -> int:
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
        rootfs=args.rootfs.resolve(),
        workload_disk=args.workload_disk.resolve(),
        qemu=args.qemu.resolve(),
        plugin=args.plugin.resolve(),
        launcher=args.launcher.resolve(),
    )
    def accept_locked(workload: Any) -> dict[str, Any]:
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
            return _accept_one(
                args=args,
                workload=workload,
                assets=assets,
                converter=converter,
                fastsim=fastsim,
                replay_config=replay_config,
            )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.jobs, len(workloads))
    ) as executor:
        futures = [
            executor.submit(accept_locked, workload)
            for workload in workloads
        ]
        accepted = [future.result() for future in futures]

    for result in accepted:
        if result["reused"]:
            print(
                f"{result['workload']}: reusing published QEMU-FST artifacts: "
                f"{result['output']}"
            )
        else:
            print(
                f"{result['workload']}: FastSim strict fs-user replay passed; "
                f"artifacts: {result['output']}"
            )
    return 0
