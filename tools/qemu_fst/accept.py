"""C4 acceptance for the canonical QEMU-FST producer."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tools.fst_producer_report import producer_report

from .capture import FstAssets, collect_workload
from .lower import convert_qemu_fst_trace, discover_trace_files
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


def _write_acceptance(
    output: Path, status: str, *,
    stage: str, error: str | None = None,
    summary: dict[str, int] | None = None,
    convergence: dict[str, Any] | None = None,
) -> None:
    payload = {"status": status, "stage": stage}
    if error is not None:
        payload["error"] = error
    if summary is not None:
        payload["summary"] = summary
    if convergence is not None:
        payload["convergence"] = convergence
    temporary = output / ".acceptance.json.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output / "acceptance.json")


def _prepare_formal_output(output: Path) -> None:
    if not output.exists():
        output.mkdir(parents=True)
        return
    state_path = output / "acceptance.json"
    if not state_path.is_file():
        raise FileExistsError(
            f"QEMU-FST formal output lacks acceptance state: {output}"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "failed":
        raise FileExistsError(
            f"QEMU-FST formal output is not replaceable: {output}; "
            f"status={state.get('status')!r}"
        )
    shutil.rmtree(output)
    output.mkdir(parents=True)


def _prepare_formal_retry(output: Path) -> tuple[Path, bool]:
    state_path = output / "acceptance.json"
    if not state_path.is_file():
        raise FileNotFoundError(
            f"QEMU-FST formal output lacks acceptance state: {output}"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "failed":
        raise FileExistsError(
            f"QEMU-FST formal output is not retryable: {output}; "
            f"status={state.get('status')!r}"
        )
    raw = output / "raw"
    for marker in (
        ".measurement-started", ".common-end-triggered", ".capture-complete",
    ):
        if not (raw / marker).is_file():
            raise RuntimeError(
                f"QEMU-FST failed output lacks reusable raw marker: "
                f"{raw / marker}"
            )
    trace_files = [
        path for path in discover_trace_files(raw)
        if path.stat().st_size > 0
    ]
    if len(trace_files) != CANONICAL_CORES:
        raise RuntimeError(
            "QEMU-FST failed output does not contain four reusable raw "
            f"shards: {output}"
        )
    reuse_fst = (
        state.get("stage") in {"replay", "validate"} and
        (output / "fst/manifest.txt").is_file()
    )
    children = ["replay", ".fst.staging", ".replay.staging"]
    if not reuse_fst:
        children.append("fst")
    for child in children:
        path = output / child
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    return raw, reuse_fst


def validate_acceptance(
    workload_root: Path, user_macro_target: int,
) -> dict[str, int]:
    raw = workload_root / "raw"
    for marker in (
        ".measurement-started", ".common-end-triggered", ".capture-complete",
    ):
        if not (raw / marker).is_file():
            raise RuntimeError(
                f"QEMU-FST raw gate failed: missing {raw / marker}"
            )
    trace_files = [
        path for path in discover_trace_files(raw)
        if path.stat().st_size > 0
    ]
    if len(trace_files) != CANONICAL_CORES:
        raise RuntimeError(
            "QEMU-FST raw gate failed: "
            f"expected {CANONICAL_CORES} non-empty shards, "
            f"found {len(trace_files)}"
        )
    return validate_fst_replay(
        workload_root / "fst",
        workload_root / "replay/stats.json",
        user_macro_target,
    )


def validate_fst_replay(
    fst_dir: Path, stats_path: Path, user_macro_target: int,
) -> dict[str, int]:
    boundaries = json.loads(
        (fst_dir / "boundaries.json").read_text(encoding="utf-8")
    )["cores"]
    expected_cores = {str(core) for core in range(CANONICAL_CORES)}
    if set(boundaries) != expected_cores:
        raise RuntimeError(
            f"QEMU-FST transfer gate failed: cores={sorted(boundaries)}"
        )

    measurement_records = 0
    measurement_user_records = 0
    trigger_cores = set()
    for core in range(CANONICAL_CORES):
        row = boundaries[str(core)]
        if (
            row["trace_scope"] != "user-plus-kernel"
            or int(row["target_user_macros"]) != user_macro_target
            or row["measurement_policy"] != "first-core-macro-common-end"
            or row["stop_reason"] != "first-core-user-macro-target"
            or int(row["measurement_user_macros"]) > user_macro_target
        ):
            raise RuntimeError(
                f"QEMU-FST transfer gate failed for core {core}: {row}"
            )
        trigger_cores.add(int(row["trigger_core"]))
        if bool(row["target_reached"]) != (
            core == int(row["trigger_core"])
        ):
            raise RuntimeError(
                f"QEMU-FST trigger identity mismatch for core {core}: {row}"
            )
        measurement_records += int(row["measurement_records"])
        measurement_user_records += int(row["measurement_user_records"])
    if len(trigger_cores) != 1:
        raise RuntimeError(
            f"QEMU-FST transfer lacks one trigger core: {trigger_cores}"
        )
    trigger_core = next(iter(trigger_cores))
    trigger = boundaries[str(trigger_core)]
    if int(trigger["measurement_user_macros"]) != user_macro_target:
        raise RuntimeError(
            "QEMU-FST trigger core did not reach the macro target: "
            f"{trigger}"
        )

    stats = json.loads(
        stats_path.read_text(encoding="utf-8")
    )
    totals = stats["totals"]
    scope = stats["scope_metrics"]
    threads = {
        int(row["thread"]): row for row in stats["threads"]
    }
    cores = {
        int(row["core"]): row for row in stats["cores"]
    }
    configuration = stats["configuration"]
    failures = []
    if stats["measurement_scope"] != "user-plus-kernel":
        failures.append("measurement_scope")
    if configuration["native_kernel_trace"] is not True:
        failures.append("native-kernel replay disabled")
    if configuration["strict_physical_address"] is not True:
        failures.append("strict physical replay disabled")
    if configuration["require_virtual_page_token"] is not True:
        failures.append("virtual-page token requirement disabled")
    if configuration["allow_mmio_escape"] is not True:
        failures.append("MMIO escape disabled")
    if measurement_user_records != int(scope["user_trace_uops"]):
        failures.append("user UOP conservation")
    if measurement_records != int(totals["retired_uops"]):
        failures.append("boundary retired UOP conservation")
    if measurement_records != (
        int(scope["user_trace_uops"]) +
        int(scope["native_kernel_trace_uops"])
    ):
        failures.append("privilege population conservation")
    if int(totals["records"]) != int(totals["retired_uops"]):
        failures.append("record retirement conservation")
    for field in (
        "unknown_addresses",
        "branches_without_outcome",
        "page_fault_untracked_accesses",
    ):
        if int(totals[field]) != 0:
            failures.append(field)
    if int(scope["native_kernel_trace_uops"]) <= 0:
        failures.append("native kernel population")
    if totals["dtlb_conserved"] is not True:
        failures.append("functional DTLB conservation")
    if totals["dtlb_timing_conserved"] is not True:
        failures.append("timing DTLB conservation")
    if set(threads) != set(range(CANONICAL_CORES)):
        failures.append("replay thread topology")
    if set(cores) != set(range(CANONICAL_CORES)):
        failures.append("replay core topology")
    for core in range(CANONICAL_CORES):
        boundary = boundaries[str(core)]
        thread = threads.get(core)
        if thread is not None and (
            int(thread["records"]) != int(boundary["measurement_records"])
            or int(thread["uops"]) != int(boundary["measurement_records"])
            or int(thread["native_kernel_uops"]) !=
                int(boundary["measurement_records"]) -
                    int(boundary["measurement_user_records"])
        ):
            failures.append(f"core {core} record/privilege conservation")
    if failures:
        raise RuntimeError(
            "QEMU-FST consumer gate failed: " + ", ".join(failures)
        )
    return {
        "measurement_records": measurement_records,
        "measurement_user_records": measurement_user_records,
    }


def _run_convergence(
    *, args: argparse.Namespace, workload: str, fst_dir: Path,
) -> dict[str, Any] | None:
    if args.gem5_fst_root is None:
        return None
    reference = (
        args.gem5_fst_root.resolve() /
        f"c{CANONICAL_CORES:02d}" / workload
    )
    try:
        if not reference.is_dir():
            raise FileNotFoundError(
                f"gem5-FST reference is missing for {workload}: {reference}"
            )
        return {
            "report": producer_report(
                workload=workload,
                gem5_fst_dir=reference,
                qemu_fst_dir=fst_dir,
                cores=CANONICAL_CORES,
            ),
        }
    except Exception as error:
        return {
            "report_error": "".join(traceback.format_exception_only(
                type(error), error,
            )).strip(),
        }


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
    _prepare_formal_output(output)
    stage = "capture"
    convergence = None
    _write_acceptance(output, "running", stage=stage)
    try:
        capture = collect_workload(
            workload=workload,
            memory=args.memory,
            timeout_seconds=args.timeout_seconds,
            warmup_timeout_seconds=args.warmup_timeout_seconds,
            kernel_args=args.kernel_args,
            network=args.network,
            user_macro_target=args.user_macro_target,
            assets=assets,
            output_dir=output / "raw",
        )
        stage = "lower"
        _write_acceptance(output, "running", stage=stage)
        fst_dir = output / "fst"
        convert_qemu_fst_trace(
            trace_dir=capture.trace_dir,
            output_dir=fst_dir,
            num_cores=CANONICAL_CORES,
            converter=converter,
            user_macro_target=args.user_macro_target,
            lower_cpus=getattr(args, "lower_cpus", None),
        )
        manifest = fst_dir / "manifest.txt"
        convergence = _run_convergence(
            args=args, workload=workload.name, fst_dir=fst_dir
        )
        stage = "replay"
        _write_acceptance(
            output, "running", stage=stage, convergence=convergence
        )
        replay_dir = output / "replay"
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
        stage = "validate"
        _write_acceptance(
            output, "running", stage=stage, convergence=convergence
        )
        summary = validate_acceptance(
            output, args.user_macro_target
        )
        _write_acceptance(
            output, "passed", stage="complete", summary=summary,
            convergence=convergence,
        )
    except Exception as error:
        _write_acceptance(
            output, "failed", stage=stage,
            error="".join(traceback.format_exception_only(
                type(error), error)).strip(),
            convergence=convergence,
        )
        raise
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
        user_macro_target=args.user_macro_target,
        lower_cpus=getattr(args, "lower_cpus", None),
    )
    convergence = _run_convergence(
        args=args, workload=workload.name, fst_dir=staging / "fst"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish(staging, output)
    _write_acceptance(
        output, "running", stage="lower-complete",
        convergence=convergence,
    )
    return {
        "workload": workload.name,
        "output": output,
        "convergence": convergence,
    }


def _retry_one(
    *,
    args: argparse.Namespace,
    workload: Any,
    converter: Path,
    fastsim: Path,
    replay_config: Path,
) -> dict[str, Any]:
    output = _output_dir(args.output_root.resolve(), workload.name)
    raw, reuse_fst = _prepare_formal_retry(output)
    stage = "lower" if not reuse_fst else "replay"
    convergence = None
    _write_acceptance(output, "running", stage=stage)
    try:
        fst_dir = output / "fst"
        if not reuse_fst:
            convert_qemu_fst_trace(
                trace_dir=raw,
                output_dir=fst_dir,
                num_cores=CANONICAL_CORES,
                converter=converter,
                user_macro_target=args.user_macro_target,
                lower_cpus=getattr(args, "lower_cpus", None),
            )
        convergence = _run_convergence(
            args=args, workload=workload.name, fst_dir=fst_dir
        )
        stage = "replay"
        _write_acceptance(
            output, "running", stage=stage, convergence=convergence
        )
        replay_dir = output / "replay"
        replay_dir.mkdir()
        stats_path = replay_dir / "stats.json"
        run_fastsim(
            fastsim=fastsim,
            config=replay_config,
            manifest=fst_dir / "manifest.txt",
            output=stats_path,
            log=replay_dir / "fastsim.log",
            dram_size=args.dram_size,
            measurement_scope=args.measurement_scope,
        )
        stage = "validate"
        _write_acceptance(
            output, "running", stage=stage, convergence=convergence
        )
        summary = validate_acceptance(
            output, args.user_macro_target
        )
        _write_acceptance(
            output, "passed", stage="complete", summary=summary,
            convergence=convergence,
        )
    except Exception as error:
        _write_acceptance(
            output, "failed", stage=stage,
            error="".join(traceback.format_exception_only(
                type(error), error)).strip(),
            convergence=convergence,
        )
        raise
    return {
        "workload": workload.name,
        "output": output,
        "raw": raw,
        "reused_fst": reuse_fst,
    }


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
    convergence = _run_convergence(
        args=args, workload=workload.name, fst_dir=output / "fst"
    )
    _write_acceptance(
        output, "running", stage="replay", convergence=convergence
    )
    staging.mkdir()
    try:
        run_fastsim(
            fastsim=fastsim,
            config=replay_config,
            manifest=manifest,
            output=staging / "stats.json",
            log=staging / "fastsim.log",
            dram_size=args.dram_size,
            measurement_scope=args.measurement_scope,
        )
        summary = validate_fst_replay(
            output / "fst", staging / "stats.json",
            args.user_macro_target,
        )
        os.replace(staging, replay)
        _write_acceptance(
            output, "passed", stage="complete", summary=summary,
            convergence=convergence,
        )
    except Exception as error:
        _write_acceptance(
            output, "failed", stage="replay",
            error="".join(traceback.format_exception_only(
                type(error), error)).strip(),
            convergence=convergence,
        )
        raise
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
    )
    output_root = args.output_root.resolve()

    def accept(workload: Any) -> dict[str, Any]:
        return _accept_one(
            args=args,
            workload=workload,
            assets=assets,
            converter=converter,
            fastsim=fastsim,
            replay_config=replay_config,
        )

    lock_path = output_root.parent / f".{output_root.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"QEMU-FST acceptance is already running: {output_root}"
            ) from error
        jobs = min(args.jobs, len(workloads))
        accepted = []
        failures = []
        if jobs == 1:
            for workload in workloads:
                try:
                    accepted.append(accept(workload))
                except Exception as error:
                    failures.append((workload.name, error))
                    break
        else:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = {
                    executor.submit(accept, workload): workload
                    for workload in workloads
                }
                for future in as_completed(futures):
                    workload = futures[future]
                    try:
                        accepted.append(future.result())
                    except Exception as error:
                        failures.append((workload.name, error))

    for result in sorted(accepted, key=lambda row: row["workload"]):
        output = _output_dir(output_root, result["workload"])
        print(
            f"{result['workload']}: raw, transfer, FST, and FastSim "
            f"native-kernel gates passed; artifacts: {output}"
        )
    if failures:
        details = "; ".join(
            f"{workload}: {error}" for workload, error in failures
        )
        raise RuntimeError(f"QEMU-FST workload failures: {details}")
    return 0


def retry_with_workloads(
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
    output_root = args.output_root.resolve()

    def retry(workload: Any) -> dict[str, Any]:
        return _retry_one(
            args=args,
            workload=workload,
            converter=converter,
            fastsim=fastsim,
            replay_config=replay_config,
        )

    lock_path = output_root.parent / f".{output_root.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"QEMU-FST acceptance is already running: {output_root}"
            ) from error
        jobs = min(args.jobs, len(workloads))
        accepted = []
        failures = []
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(retry, workload): workload
                for workload in workloads
            }
            for future in as_completed(futures):
                workload = futures[future]
                try:
                    accepted.append(future.result())
                except Exception as error:
                    failures.append((workload.name, error))

    for result in sorted(accepted, key=lambda row: row["workload"]):
        action = (
            "reused canonical raw and FST, rebuilt replay"
            if result["reused_fst"]
            else "reused canonical raw, rebuilt FST and replay"
        )
        print(
            f"{result['workload']}: {action}, and passed FastSim "
            f"native-kernel gates; artifacts: "
            f"{result['output']}"
        )
    if failures:
        details = "; ".join(
            f"{workload}: {error}" for workload, error in failures
        )
        raise RuntimeError(f"QEMU-FST workload retry failures: {details}")
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
