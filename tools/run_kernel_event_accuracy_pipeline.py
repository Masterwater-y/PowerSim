#!/usr/bin/env python3
"""Run the reproducible dual-scope kernel-event accuracy pipeline.

Each input is a complete gem5-FS result directory.  The pipeline validates the
classified oracle, runs FastSim once with kernel models disabled, optionally
calibrates a deployable kernel-event config, runs FastSim with that config, and
emits per-case plus aggregate user/user+kernel accuracy reports.

For a held-out split, pass the frozen config produced by a calibration run via
``--kernel-config``.  Held-out oracles are then used only for comparison.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from validate_kernel_events_oracle import validate_document
from validate_fs_oracle_identity import validate_result_identity


PAGE_FAULT_ALLOCATION_SYSCALLS = (9, 12, 25, 28)


@dataclass(frozen=True)
class Case:
    result_dir: Path
    workload: str
    cores: int
    oracle: Path
    manifest: Path
    output_dir: Path
    warmup_records: int
    warmup_instructions: int
    measurement_records: int
    measurement_instructions: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result",
        action="append",
        default=[],
        type=Path,
        help="Complete gem5-FS result directory; repeat for multiple cases.",
    )
    parser.add_argument(
        "--matrix",
        action="append",
        default=[],
        type=Path,
        help=(
            "Completed matrix directory or status.json; all completed "
            "result_dir entries are added. Repeat for multiple matrices."
        ),
    )
    parser.add_argument(
        "--allow-partial-matrix",
        action="store_true",
        help=(
            "Add only successful completed tasks from each --matrix. This is "
            "intended for merging a partial matrix with explicit retry "
            "matrices; duplicate core/workload cases are still rejected."
        ),
    )
    parser.add_argument(
        "--split", choices=("calibration", "held-out"), required=True
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--base-config",
        type=Path,
        default=Path("configs/gem5-v28_1-time-epoch.cfg"),
    )
    parser.add_argument(
        "--kernel-config",
        type=Path,
        help=(
            "Frozen model-on config from a previous calibration. If omitted, "
            "the input cases are calibrated and evaluated as a calibration "
            "split."
        ),
    )
    parser.add_argument(
        "--fastsim", type=Path, default=Path("build/fastsim")
    )
    parser.add_argument(
        "--include-cores",
        nargs="+",
        type=int,
        help="Keep only these core counts after loading matrix inputs.",
    )
    parser.add_argument(
        "--exclude-cores",
        nargs="+",
        type=int,
        help="Exclude these core counts after loading matrix inputs.",
    )
    parser.add_argument(
        "--allow-cold-slice",
        action="store_true",
        help=(
            "Allow diagnostic inputs without a two-phase functional warmup. "
            "Formal reports should leave this disabled."
        ),
    )
    parser.add_argument(
        "--page-fault-cache-state-model",
        action="store_true",
        help=(
            "After fitting/loading the frozen page-fault classifier, rerun "
            "both scopes with selected first-touch page fills applied to "
            "cache state. User scope still excludes all kernel cycles/PMU."
        ),
    )
    parser.add_argument(
        "--page-fault-syscall-semantic-model",
        action="store_true",
        help=(
            "Use successful mmap/munmap metadata plus FST virtual-page maps "
            "instead of fitted first-touch probabilities. Requires the "
            "paired cache-state rerun."
        ),
    )
    parser.add_argument(
        "--page-fault-roi-entry-page-state-model",
        dest="page_fault_roi_entry_page_state_model",
        action="store_true",
        default=None,
        help=(
            "Use exact initial/ROI-entry guest page state from FST virtual-"
            "page maps before falling back to the syscall selector. If "
            "omitted, retain the selected profile's default. Requires the "
            "paired cache-state rerun and page-state-enriched traces."
        ),
    )
    parser.add_argument(
        "--no-page-fault-roi-entry-page-state-model",
        dest="page_fault_roi_entry_page_state_model",
        action="store_false",
        default=None,
        help=(
            "Disable ROI-entry page-state replay in both scopes for a "
            "controlled baseline."
        ),
    )
    parser.add_argument(
        "--page-fault-initial-pte-state-model",
        dest="page_fault_roi_entry_page_state_model",
        action="store_const",
        const=True,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dtlb-miss-model",
        choices=("se_atomic", "timing_walk"),
        default="timing_walk",
        help=(
            "Translation timing policy. Full-system validation defaults to "
            "the delayed gem5-style walker; se_atomic is retained for "
            "explicit SE/backward-compatibility checks."
        ),
    )
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        default=12,
        help=(
            "Fixed total service cycles for a timing_walk request. The "
            "default is calibrated on the formal C4 split and frozen for C8."
        ),
    )
    parser.add_argument(
        "--fetch-buffer-refill-latency",
        type=int,
        default=1,
        help="Target L0-I resident-hit empty-cycle delay.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def matrix_results(path: Path, allow_partial: bool = False) -> list[Path]:
    status_path = path / "status.json" if path.is_dir() else path
    status = read_json(status_path)
    summary = status.get("summary", {})
    if not allow_partial and (
        summary.get("status") != "complete"
        or int(summary.get("return_code", 1)) != 0
    ):
        raise ValueError(f"matrix is not complete: {status_path}")
    results = []
    for task_name, task in sorted(status.get("tasks", {}).items()):
        sample = task.get("sample", {})
        completed = sample.get("status") == "completed" and int(
            sample.get("return_code", 1)
        ) == 0
        reused_success = (
            sample.get("status") == "skipped"
            and sample.get("reason") == "current successful result exists"
            and bool(sample.get("result_dir"))
        )
        successful = completed or reused_success
        if allow_partial and not successful:
            continue
        if not successful:
            raise ValueError(
                f"matrix task is not complete: {status_path}:{task_name}"
            )
        result_dir = sample.get("result_dir")
        if not result_dir:
            raise ValueError(
                f"matrix task lacks result_dir: {status_path}:{task_name}"
            )
        results.append(Path(result_dir))
    if not results:
        raise ValueError(f"matrix has no completed results: {status_path}")
    return results


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")


def load_case(
    result_dir: Path, output_root: Path, allow_cold_slice: bool = False
) -> Case:
    result_dir = result_dir.resolve()
    identity = validate_result_identity(result_dir)
    if not identity["valid"]:
        details = ", ".join(
            f"{item['field']}="
            f"{item.get('manifest', item.get('profile'))!r} "
            f"(target {item['target']!r})"
            for item in identity["mismatches"]
        )
        raise ValueError(
            f"{result_dir}: TaoTrace PMU profile does not describe the "
            f"restored gem5 target: {details}"
        )
    request_path = result_dir / "request.json"
    request = read_json(request_path)
    selection = request.get("workload_selection", {})
    profile = request.get("profile", {})
    workload = str(selection.get("workload", ""))
    cores = int(profile.get("cores", selection.get("cores", 0)))
    if not workload or cores <= 0:
        raise ValueError(
            f"{request_path}: missing workload or positive core count"
        )
    oracle = result_dir / "oracle" / "kernel_events.json"
    manifest = result_dir / "tao_trace" / "manifest.txt"
    if not oracle.is_file():
        raise ValueError(f"missing oracle: {oracle}")
    if not manifest.is_file():
        raise ValueError(f"missing functional trace manifest: {manifest}")
    trace_metadata_path = result_dir / "tao_trace" / "trace.json"
    trace_metadata = read_json(trace_metadata_path)
    per_core = trace_metadata.get("per_core", {})
    if len(per_core) != cores:
        raise ValueError(
            f"{trace_metadata_path}: expected {cores} per-core entries, "
            f"found {len(per_core)}"
        )
    manifest_rows = [
        line.split()
        for line in manifest.read_text().splitlines()
        if line.strip()
    ]
    warmup_manifest = False
    if (
        len(manifest_rows) == cores
        and all(
            len(row) == 8 and row[1] == "fastsim-binary-warmup-slice"
            for row in manifest_rows
        )
        and sorted(int(row[0]) for row in manifest_rows) == list(range(cores))
    ):
        rows_by_core = {int(row[0]): row for row in manifest_rows}
        warmup_manifest = all(
            int(rows_by_core[core][4])
            == int(per_core[str(core)]["warmup_instructions"])
            and int(rows_by_core[core][5])
            == int(per_core[str(core)]["measurement_instructions"])
            and int(rows_by_core[core][6])
            == int(per_core[str(core)]["warmup_records"])
            and int(rows_by_core[core][7])
            == int(per_core[str(core)]["measurement_records"])
            for core in range(cores)
        )
    warmup_records = sum(
        int(row.get("warmup_records", 0)) for row in per_core.values()
    )
    warmup_instructions = sum(
        int(row.get("warmup_instructions", 0)) for row in per_core.values()
    )
    measurement_records = sum(
        int(row.get("measurement_records", 0)) for row in per_core.values()
    )
    measurement_instructions = sum(
        int(row.get("measurement_instructions", 0))
        for row in per_core.values()
    )
    warmup_valid = (
        trace_metadata.get("functional_warmup_enabled") is True
        and warmup_manifest
        and warmup_records > 0
        and warmup_instructions > 0
        and measurement_records > 0
        and measurement_instructions > 0
    )
    oracle_document = read_json(oracle)
    oracle_counts = {
        int(row["core_id"]): int(row["n_user"])
        for row in oracle_document.get("per_core", [])
    }
    trace_counts = {
        int(core): int(row.get("measurement_records", 0))
        for core, row in per_core.items()
    }
    if oracle_counts != trace_counts:
        raise ValueError(
            f"{result_dir}: oracle/measurement record mismatch: "
            f"oracle={oracle_counts}, trace={trace_counts}"
        )
    if not allow_cold_slice and not warmup_valid:
        raise ValueError(
            f"{result_dir}: formal input lacks a valid two-phase functional "
            f"warmup (manifest={warmup_manifest}, records={warmup_records}, "
            f"instructions={warmup_instructions})"
        )
    name = f"{cores:02d}c-{safe_name(workload)}"
    return Case(
        result_dir=result_dir,
        workload=workload,
        cores=cores,
        oracle=oracle,
        manifest=manifest,
        output_dir=output_root / "cases" / name,
        warmup_records=warmup_records,
        warmup_instructions=warmup_instructions,
        measurement_records=measurement_records,
        measurement_instructions=measurement_instructions,
    )


def run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()
    if not args.result and not args.matrix:
        raise SystemExit("at least one --result or --matrix is required")
    if args.kernel_config is None and args.split != "calibration":
        raise SystemExit(
            "held-out evaluation requires --kernel-config from calibration"
        )
    if (
        (
            args.page_fault_syscall_semantic_model
            or args.page_fault_roi_entry_page_state_model is True
        )
        and not args.page_fault_cache_state_model
    ):
        raise SystemExit(
            "page-fault semantic/ROI-entry-page-state models require "
            "--page-fault-cache-state-model"
        )
    if args.dtlb_page_walk_latency <= 0:
        raise SystemExit("--dtlb-page-walk-latency must be positive")

    result_dirs = list(args.result)
    for matrix in args.matrix:
        result_dirs.extend(
            matrix_results(matrix, allow_partial=args.allow_partial_matrix)
        )
    unique_results = sorted({path.resolve() for path in result_dirs})

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    cases = [
        load_case(path, output_root, args.allow_cold_slice)
        for path in unique_results
    ]
    if args.include_cores is not None:
        included = set(args.include_cores)
        cases = [case for case in cases if case.cores in included]
    if args.exclude_cores is not None:
        excluded = set(args.exclude_cores)
        cases = [case for case in cases if case.cores not in excluded]
    if not cases:
        raise SystemExit("core-count filters removed every input case")
    cases.sort(key=lambda case: (case.cores, case.workload))
    case_keys = [(case.cores, case.workload) for case in cases]
    if len(case_keys) != len(set(case_keys)):
        raise SystemExit(
            "duplicate core/workload cases would overwrite output directories"
        )

    fastsim = args.fastsim.resolve()
    base_config = args.base_config.resolve()
    if not fastsim.is_file():
        raise SystemExit(f"missing FastSim binary: {fastsim}")
    if not base_config.is_file():
        raise SystemExit(f"missing base config: {base_config}")
    dtlb_args = [
        "--fetch-buffer-refill-latency",
        str(args.fetch_buffer_refill_latency),
        "--dtlb-miss-model",
        args.dtlb_miss_model,
    ]
    if args.dtlb_miss_model == "timing_walk":
        dtlb_args.extend(
            ["--dtlb-page-walk-latency", str(args.dtlb_page_walk_latency)]
        )
    effective_runtime_config = (
        "\n# Effective target/runtime overrides selected by this pipeline.\n"
        f"core.fetch_buffer_refill_latency = "
        f"{args.fetch_buffer_refill_latency}\n"
        f"dtlb.miss_model = {args.dtlb_miss_model}\n"
        "trace.allow_cross_page_without_virtual_token = true\n"
        "trace.allow_mmio_escape = true\n"
        f"dram.size = {3 * 1024**3}\n"
    )
    if args.dtlb_miss_model == "timing_walk":
        effective_runtime_config += (
            f"dtlb.page_walk_latency = {args.dtlb_page_walk_latency}\n"
        )
    user_config = output_root / "user.cfg"
    semantic_probe_line = (
        "page_fault.syscall_semantic_model = true\n"
        if args.page_fault_syscall_semantic_model
        else "page_fault.syscall_semantic_model = false\n"
    )
    user_config.write_text(
        base_config.read_text().rstrip()
        + "\n\n# Strict user-only measurement pipeline.\n"
        + "measurement.scope = user\n"
        + "syscall.service_latency = 0\n"
        + "syscall.cost_model = false\n"
        + "syscall.event_model = false\n"
        + "page_fault.event_model = false\n"
        + "page_fault.cache_state_model = false\n"
        + semantic_probe_line
        + "page_fault.syscall_semantic_fallback_write_probability_ppm = 0\n"
        + "irq.event_model = false\n"
        + "\n# Trace-visible page-fault candidate classification.\n"
        + "page_fault.allocation_syscalls = "
        + ",".join(str(value) for value in PAGE_FAULT_ALLOCATION_SYSCALLS)
        + "\n"
        + effective_runtime_config
    )

    for case in cases:
        case.output_dir.mkdir(parents=True, exist_ok=True)
        oracle = read_json(case.oracle)
        try:
            validation = validate_document(oracle, 0.0)
        except ValueError as exc:
            raise SystemExit(f"invalid oracle {case.oracle}: {exc}") from exc
        if not validation.get("formal_pmu_eligible", False):
            raise SystemExit(
                f"{case.oracle}: formal pipeline requires the P0 v3 "
                "accounting contract"
            )
        (case.output_dir / "oracle-validation.json").write_text(
            json.dumps(
                validation, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        )
        print(
            f"validated {case.cores}c/{case.workload}: "
            f"{validation['measured_cycles']} classified cycles",
            flush=True,
        )

    for case in cases:
        run(
            [
                str(fastsim),
                "simulate",
                "--config",
                str(user_config),
                "--manifest",
                str(case.manifest),
                "--measurement-scope",
                "user",
                "--cores",
                str(case.cores),
                "--allow-cross-page-without-virtual-token",
                "true",
                *dtlb_args,
                "--allow-mmio-escape",
                "true",
                "--dram-size",
                str(3 * 1024**3),
                "--output",
                str(case.output_dir / "user.json"),
            ]
        )

    tools_dir = Path(__file__).resolve().parent
    calibration_path = output_root / "calibration.json"
    generated_config = output_root / "kernel-events.cfg"
    if args.kernel_config is None:
        command = [
            sys.executable,
            str(tools_dir / "calibrate_kernel_event_profiles.py"),
        ]
        for case in cases:
            command.extend(
                [
                    "--case",
                    str(case.oracle),
                    str(case.output_dir / "user.json"),
                ]
            )
        command.extend(
            [
                "--output",
                str(calibration_path),
                "--config-output",
                str(generated_config),
                "--base-config",
                str(base_config),
            ]
        )
        run(command)
        kernel_config = generated_config
    else:
        kernel_config = args.kernel_config.resolve()
        if not kernel_config.is_file():
            raise SystemExit(f"missing frozen kernel config: {kernel_config}")

    effective_user_config = user_config
    effective_kernel_config = output_root / "kernel-events-effective.cfg"
    effective_kernel_config.write_text(
        kernel_config.read_text().rstrip() + "\n" + effective_runtime_config
    )
    if args.page_fault_cache_state_model:
        # The accepted default is scope-specific: exact ROI-entry fault state
        # improves combined timing, while the current whole-page cache-fill
        # approximation fails the user-only gate. An explicit boolean applies
        # to both scopes for controlled ablation.
        roi_entry_user_enabled = (
            args.page_fault_roi_entry_page_state_model is True
        )
        roi_entry_kernel_enabled = (
            args.page_fault_roi_entry_page_state_model is not False
        )
        semantic_line = (
            "page_fault.syscall_semantic_model = true\n"
            if args.page_fault_syscall_semantic_model
            else "page_fault.syscall_semantic_model = false\n"
        )
        roi_entry_user_line = (
            "page_fault.roi_entry_page_state_model = "
            + ("true\n" if roi_entry_user_enabled else "false\n")
        )
        roi_entry_kernel_line = (
            "page_fault.roi_entry_page_state_model = "
            + ("true\n" if roi_entry_kernel_enabled else "false\n")
        )
        effective_user_config = output_root / "user-cache-state.cfg"
        effective_user_config.write_text(
            kernel_config.read_text().rstrip()
            + "\n\n# User-only timing with selected kernel cache state.\n"
            + "measurement.scope = user\n"
            + "syscall.service_latency = 0\n"
            + "syscall.cost_model = false\n"
            + "syscall.event_model = false\n"
            + "page_fault.event_model = false\n"
            + "page_fault.cache_state_model = true\n"
            + semantic_line
            + roi_entry_user_line
            + "irq.event_model = false\n"
            + effective_runtime_config
        )
        effective_kernel_config = output_root / "kernel-events-cache-state.cfg"
        effective_kernel_config.write_text(
            kernel_config.read_text().rstrip()
            + "\n\n# Selected page-fault functional cache state.\n"
            + "page_fault.cache_state_model = true\n"
            + semantic_line
            + roi_entry_kernel_line
            + effective_runtime_config
        )
        # The first user pass supplied trace-only candidates to calibration.
        # Replace it with the frozen classifier's state-aware user result so
        # both reported scopes start from the same modeled cache state.
        for case in cases:
            run(
                [
                    str(fastsim),
                    "simulate",
                    "--config",
                    str(effective_user_config),
                    "--manifest",
                    str(case.manifest),
                    "--measurement-scope",
                    "user",
                    "--cores",
                    str(case.cores),
                    "--allow-cross-page-without-virtual-token",
                    "true",
                    *dtlb_args,
                    "--allow-mmio-escape",
                    "true",
                    "--dram-size",
                    str(3 * 1024**3),
                    "--output",
                    str(case.output_dir / "user.json"),
                ]
            )

    accuracy_reports = []
    for case in cases:
        combined_report = case.output_dir / "user-plus-kernel.json"
        accuracy_report = case.output_dir / "accuracy.json"
        run(
            [
                str(fastsim),
                "simulate",
                "--config",
                str(effective_kernel_config),
                "--manifest",
                str(case.manifest),
                "--measurement-scope",
                "user-plus-kernel",
                "--cores",
                str(case.cores),
                "--allow-cross-page-without-virtual-token",
                "true",
                *dtlb_args,
                "--allow-mmio-escape",
                "true",
                "--dram-size",
                str(3 * 1024**3),
                "--output",
                str(combined_report),
            ]
        )
        run(
            [
                sys.executable,
                str(tools_dir / "compare_kernel_event_accuracy.py"),
                str(case.oracle),
                str(case.output_dir / "user.json"),
                str(combined_report),
                "--output",
                str(accuracy_report),
            ]
        )
        accuracy_reports.append(accuracy_report)

    summary_json = output_root / "summary.json"
    summary_csv = output_root / "summary.csv"
    summary_markdown = output_root / "summary.md"
    run(
        [
            sys.executable,
            str(tools_dir / "summarize_kernel_event_accuracy.py"),
            *[str(path) for path in accuracy_reports],
            "--split",
            args.split,
            "--output",
            str(summary_json),
            "--csv-output",
            str(summary_csv),
            "--markdown-output",
            str(summary_markdown),
        ]
    )

    provenance = {
        "schema": "fastsim-kernel-event-pipeline-v2",
        "split": args.split,
        "base_config": str(base_config),
        "user_config": str(effective_user_config),
        "kernel_config": str(kernel_config),
        "effective_kernel_config": str(effective_kernel_config),
        "page_fault_cache_state_model": (
            args.page_fault_cache_state_model
        ),
        "page_fault_syscall_semantic_model": (
            args.page_fault_syscall_semantic_model
        ),
        "page_fault_roi_entry_page_state_model": {
            "requested": args.page_fault_roi_entry_page_state_model,
            "user": bool(
                args.page_fault_cache_state_model
                and args.page_fault_roi_entry_page_state_model is True
            ),
            "user_plus_kernel": bool(
                args.page_fault_cache_state_model
                and args.page_fault_roi_entry_page_state_model is not False
            ),
        },
        "page_fault_syscall_semantic_fallback": (
            "calibrated-shared-preexisting-first-write"
            if args.page_fault_syscall_semantic_model
            else "disabled"
        ),
        "measurement_scopes": ["user", "user-plus-kernel"],
        "calibrated_from_inputs": args.kernel_config is None,
        "input_matrices": [str(path.resolve()) for path in args.matrix],
        "allow_partial_matrix": args.allow_partial_matrix,
        "include_cores": args.include_cores,
        "exclude_cores": args.exclude_cores,
        "allow_cold_slice": args.allow_cold_slice,
        "functional_warmup_required": not args.allow_cold_slice,
        "dtlb_miss_model": args.dtlb_miss_model,
        "dtlb_page_walk_latency": (
            args.dtlb_page_walk_latency
            if args.dtlb_miss_model == "timing_walk"
            else None
        ),
        "dtlb_counter_domains": {
            "architectural": "retired committed-stream PMU",
            "timing": "delayed walker/follower timing only",
        },
        "fetch_buffer_refill_latency":
            args.fetch_buffer_refill_latency,
        "effective_configs_embed_runtime_overrides": True,
        "cases": [
            {
                "workload": case.workload,
                "cores": case.cores,
                "result_dir": str(case.result_dir),
                "oracle": str(case.oracle),
                "manifest": str(case.manifest),
                "accuracy": str(case.output_dir / "accuracy.json"),
                "warmup_records": case.warmup_records,
                "warmup_instructions": case.warmup_instructions,
                "measurement_records": case.measurement_records,
                "measurement_instructions": case.measurement_instructions,
            }
            for case in cases
        ],
        "summary": str(summary_json),
    }
    (output_root / "pipeline.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {summary_markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
