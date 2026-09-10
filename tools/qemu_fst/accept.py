"""C4 acceptance for the canonical QEMU-FST producer."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import os
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
    if output.exists():
        raise FileExistsError(
            f"QEMU-FST output became occupied before publication: {output}"
        )
    os.replace(staging, output)


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
    if output.exists():
        raise FileExistsError(
            f"QEMU-FST acceptance output is immutable: {output}; "
            "use a new --run-id"
        )
    if staging.exists():
        raise FileExistsError(f"QEMU-FST staging output exists: {staging}")
    staging.mkdir(parents=True)

    capture = collect_workload(
        workload=workload,
        memory=args.memory,
        timeout_seconds=args.timeout_seconds,
        warmup_timeout_seconds=args.warmup_timeout_seconds,
        kernel_args=args.kernel_args,
        network=args.network,
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
        dram_size=args.dram_size,
        measurement_scope=args.measurement_scope,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish(staging, output)
    return {
        "workload": workload.name,
        "output": output,
        "raw": capture.trace_dir,
    }


def _lower_one(
    *,
    args: argparse.Namespace,
    workload: Any,
    converter: Path,
) -> dict[str, Any]:
    output = _output_dir(args.output_root.resolve(), workload.name)
    source = _output_dir(args.raw_root.resolve(), workload.name) / "raw"
    if not source.is_dir():
        raise FileNotFoundError(
            f"QEMU-FST raw source is missing for {workload.name}: {source}"
        )
    if output.exists():
        raise FileExistsError(
            f"QEMU-FST lower output already exists: {output}; "
            "use a new --run-id"
        )
    staging = output.with_name(f".{output.name}.staging")
    if staging.exists():
        raise FileExistsError(f"QEMU-FST staging output exists: {staging}")
    staging.mkdir(parents=True)
    convert_qemu_fst_trace(
        trace_dir=source,
        output_dir=staging / "fst",
        num_cores=CANONICAL_CORES,
        converter=converter,
        measurement_user_record_target=args.user_fst_target,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish(staging, output)
    return {"workload": workload.name, "output": output}


def _replay_one(
    *,
    args: argparse.Namespace,
    workload: Any,
    fastsim: Path,
    replay_config: Path,
) -> dict[str, Any]:
    output = _output_dir(args.output_root.resolve(), workload.name)
    manifest = output / "fst/manifest.txt"
    if not manifest.is_file():
        raise FileNotFoundError(
            f"QEMU-FST manifest is missing for {workload.name}: {manifest}"
        )
    replay = output / "replay"
    if replay.exists():
        raise FileExistsError(
            f"QEMU-FST replay output already exists: {replay}; "
            "use a new --run-id"
        )
    staging = output / ".replay.staging"
    if staging.exists():
        raise FileExistsError(f"QEMU-FST replay staging exists: {staging}")
    staging.mkdir()
    run_fastsim(
        fastsim=fastsim,
        config=replay_config,
        manifest=manifest,
        output=staging / "stats.json",
        log=staging / "fastsim.log",
        dram_size=args.dram_size,
        measurement_scope=args.measurement_scope,
    )
    os.replace(staging, replay)
    return {"workload": workload.name, "output": output}


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

    if args.jobs == 1:
        accepted = [accept_locked(workload) for workload in workloads]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.jobs, len(workloads))
        ) as executor:
            futures = [
                executor.submit(accept_locked, workload)
                for workload in workloads
            ]
            accepted = [future.result() for future in futures]

    for result in accepted:
        print(
            f"{result['workload']}: FastSim strict fs-user replay passed; "
            f"artifacts: {result['output']}"
        )
    return 0


def lower_with_workloads(
    args: argparse.Namespace, workloads: list[Any],
) -> int:
    converter = args.converter.resolve()
    if not converter.is_file():
        raise FileNotFoundError(f"gem5 converter missing: {converter}")
    for workload in workloads:
        result = _lower_one(
            args=args, workload=workload, converter=converter,
        )
        print(
            f"{result['workload']}: lowered existing raw trace; "
            f"artifacts: {result['output']}"
        )
    return 0


def replay_with_workloads(
    args: argparse.Namespace, workloads: list[Any],
) -> int:
    fastsim = fastsim_binary(args.fastsim)
    replay_config = args.config.resolve()
    if not replay_config.is_file():
        raise FileNotFoundError(
            f"QEMU-FST replay config missing: {replay_config}"
        )
    for workload in workloads:
        result = _replay_one(
            args=args,
            workload=workload,
            fastsim=fastsim,
            replay_config=replay_config,
        )
        print(
            f"{result['workload']}: replayed existing FST; "
            f"artifacts: {result['output']}"
        )
    return 0
