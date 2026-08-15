from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .validation import (
    VALIDATION_MATRIX_PATH,
    _fastsim_binary,
    _load_matrix,
    _matrix_root,
    _selected_workloads,
    _validate_dr_address_provenance,
    _validate_strict_fst_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "configs/gem5/v28_1-c04.cfg"
DEFAULT_DR_CONFIG = PROJECT_ROOT / "configs/dynamoRIO/physical-v28_1-c04.cfg"
FUNCTIONAL_TOTAL_KEYS = (
    "records",
    "retired_uops",
    "retired_instructions",
    "memory_accesses",
    "mmio_escape_accesses",
    "unknown_addresses",
    "branches_without_outcome",
    "serializing_uops",
    "syscall_uops",
    "branches",
    "conditional_branches",
)
TOPOLOGY_DERIVED_TOTAL_KEYS = (
    "sum_core_cycles",
    "simulated_makespan_cycles",
    "aggregate_ipc",
    "branch_penalty_cycles",
    "exposed_memory_penalty_cycles",
    "l1d_accesses",
    "l1d_hits",
    "l1d_misses",
    "l2_accesses",
    "l2_hits",
    "l2_misses",
    "llc_accesses",
    "llc_hits",
    "llc_misses",
    "dtlb_accesses",
    "dtlb_hits",
    "dtlb_misses",
    "dtlb_untracked",
    "dram_frfcfs_requests",
    "dram_frfcfs_row_hits",
    "dram_frfcfs_row_misses",
)
REPLAY_CONTRACT = {
    "exact": "functional_totals",
    "diagnostic": "address_topology_derived_totals",
    "diagnostic_reason": (
        "DR FST addresses are backed by DR PA markers, but originate from a "
        "separate execution; cache, CHA, DRAM, and final timing counters may "
        "differ from gem5's local physical layout."
    ),
}


@dataclass(frozen=True)
class ReplayValidationOptions:
    fst_root: Path
    output_dir: Path
    matrix_path: Path = VALIDATION_MATRIX_PATH
    cores: int | None = None
    config_path: Path | None = None
    dr_config_path: Path | None = None
    fastsim_binary: Path | None = None
    workloads: tuple[str, ...] = ()
    resume: bool = False


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _run_fastsim(
    *,
    fastsim: Path,
    config: Path,
    manifest: Path,
    output: Path,
    log: Path,
) -> None:
    run_env = os.environ.copy()
    run_env["LD_LIBRARY_PATH"] = "/opt/gcc-11.5.0/lib64"
    result = subprocess.run(
        [
            str(fastsim),
            "simulate",
            "--config",
            str(config),
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"FastSim simulate failed rc={result.returncode}: {log}")
    if not output.is_file():
        raise RuntimeError(f"FastSim did not write replay: {output}")


def _value_at(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _numeric_delta(left: Any, right: Any) -> dict[str, Any]:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return {"gem5": left, "dr": right, "delta": None, "relative": None}
    delta = right - left
    relative = None if left == 0 else delta / left
    return {"gem5": left, "dr": right, "delta": delta, "relative": relative}


def _compare_replay_outputs(gem5_replay: Path, dr_replay: Path) -> dict[str, Any]:
    gem5 = json.loads(gem5_replay.read_text(encoding="utf-8"))
    dr = json.loads(dr_replay.read_text(encoding="utf-8"))
    exact_mismatches = []
    exact = {}
    for key in FUNCTIONAL_TOTAL_KEYS:
        path = f"totals.{key}"
        left = _value_at(gem5, path)
        right = _value_at(dr, path)
        exact[key] = {"gem5": left, "dr": right}
        if left != right:
            exact_mismatches.append({"field": path, "gem5": left, "dr": right})
    topology_derived = {
        key: _numeric_delta(
            _value_at(gem5, f"totals.{key}"),
            _value_at(dr, f"totals.{key}"),
        )
        for key in TOPOLOGY_DERIVED_TOTAL_KEYS
    }
    return {
        "status": "diagnostic_only" if not exact_mismatches else "fail",
        "contract": REPLAY_CONTRACT,
        "exact": exact,
        "exact_mismatches": exact_mismatches,
        "topology_derived": topology_derived,
    }


def _case_root(fst_root: Path, cores: int, workload: str) -> Path:
    return fst_root / f"c{cores:02d}" / workload


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _resolved_replay_configs(
    matrix: dict[str, Any], options: ReplayValidationOptions
) -> tuple[Path, Path]:
    del matrix
    gem5_config = (
        options.config_path.resolve()
        if options.config_path is not None
        else DEFAULT_CONFIG.resolve()
    )
    dr_config = (
        options.dr_config_path.resolve()
        if options.dr_config_path is not None
        else DEFAULT_DR_CONFIG.resolve()
    )
    return gem5_config, dr_config


def _has_supported_manifests(case_root: Path) -> bool:
    return (
        case_root.joinpath("gem5", "manifest.txt").is_file()
        and case_root.joinpath("dr", "manifest.txt").is_file()
    )


def simulate_replay_matrix(options: ReplayValidationOptions) -> dict[str, Any]:
    matrix = _load_matrix(options.matrix_path)
    fst_root = _matrix_root(options.fst_root, options.matrix_path).resolve()
    cores = int(options.cores or matrix["default_cores"])
    workloads = _selected_workloads(matrix, options.workloads)
    fastsim = _fastsim_binary(options.fastsim_binary)
    gem5_config, dr_config = _resolved_replay_configs(matrix, options)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-replay-simulation-v1",
        "status": "running",
        "fst_root": str(fst_root),
        "config": str(gem5_config),
        "gem5_config": str(gem5_config),
        "dr_config": str(dr_config),
        "cores": cores,
        "address_topology_contract": (
            "functional totals are exact; topology-derived counters are "
            "diagnostic when gem5 and DR configs or physical namespaces differ"
        ),
        "cases": [],
    }
    for workload in workloads:
        source_case = _case_root(fst_root, cores, workload.name)
        replay_dir = source_case / "replay"
        replay_dir.mkdir(parents=True, exist_ok=True)
        case_report: dict[str, Any] = {
            "workload": workload.name,
            "group": workload.group,
            "path": str(replay_dir),
        }
        try:
            if not _has_supported_manifests(source_case):
                case_report.update({"status": "skipped", "reason": "missing dr/gem5 FST manifests"})
            else:
                _validate_strict_fst_manifest(source_case / "dr" / "manifest.txt")
                _validate_dr_address_provenance(source_case / "dr", cores)
                gem5_replay = replay_dir / "gem5.json"
                dr_replay = replay_dir / "dr.json"
                if options.resume and gem5_replay.is_file() and dr_replay.is_file():
                    case_report.update({"status": "pass", "reused": True})
                else:
                    _run_fastsim(
                        fastsim=fastsim,
                        config=gem5_config,
                        manifest=source_case / "gem5" / "manifest.txt",
                        output=gem5_replay,
                        log=replay_dir / "gem5.log",
                    )
                    _run_fastsim(
                        fastsim=fastsim,
                        config=dr_config,
                        manifest=source_case / "dr" / "manifest.txt",
                        output=dr_replay,
                        log=replay_dir / "dr.log",
                    )
                    case_report.update({"status": "pass", "reused": False})
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(fst_root / "replay-report.json", report)
    statuses = [str(case["status"]) for case in report["cases"]]
    report["status"] = (
        "error" if "error" in statuses
        else "needs_work" if any(status in {"skipped", "limited"} for status in statuses)
        else "pass"
    )
    _write_json(fst_root / "replay-report.json", report)
    return report


def validate_replay_matrix(options: ReplayValidationOptions) -> dict[str, Any]:
    fst_root = _matrix_root(options.fst_root, options.matrix_path).resolve()
    output_dir = options.output_dir.resolve()
    if output_dir.exists() and not options.resume:
        raise FileExistsError(f"replay validation output exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    matrix = _load_matrix(options.matrix_path)
    cores = int(options.cores or matrix["default_cores"])
    workloads = _selected_workloads(matrix, options.workloads)
    gem5_config, dr_config = _resolved_replay_configs(matrix, options)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-replay-validation-v1",
        "status": "running",
        "fst_root": str(fst_root),
        "config": str(gem5_config),
        "gem5_config": str(gem5_config),
        "dr_config": str(dr_config),
        "cores": cores,
        "address_topology_contract": (
            "functional totals are exact; topology-derived counters are "
            "diagnostic when gem5 and DR configs or physical namespaces differ"
        ),
        "cases": [],
    }
    for workload in workloads:
        source_case = _case_root(fst_root, cores, workload.name)
        replay_dir = source_case / "replay"
        comparison_path = (
            output_dir / f"c{cores:02d}" / workload.name / "replay-comparison.json"
        )
        comparison_path.parent.mkdir(parents=True, exist_ok=True)
        case_report: dict[str, Any] = {
            "workload": workload.name,
            "group": workload.group,
            "source": str(source_case),
            "path": str(comparison_path.parent),
        }
        try:
            gem5_replay = replay_dir / "gem5.json"
            dr_replay = replay_dir / "dr.json"
            _validate_strict_fst_manifest(source_case / "dr" / "manifest.txt")
            _validate_dr_address_provenance(source_case / "dr", cores)
            if not gem5_replay.is_file() or not dr_replay.is_file():
                case_report.update({"status": "skipped", "reason": "missing canonical replay json"})
            else:
                comparison = _compare_replay_outputs(gem5_replay, dr_replay)
                comparison.update({
                    "workload": workload.name,
                    "group": workload.group,
                    "gem5_replay": str(gem5_replay),
                    "dr_replay": str(dr_replay),
                })
                _write_json(comparison_path, comparison)
                case_report.update(comparison)
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(output_dir / "report.json", report)
    statuses = [str(case["status"]) for case in report["cases"]]
    report["status"] = (
        "error" if "error" in statuses
        else "needs_work" if any(status in {"fail", "skipped"} for status in statuses)
        else "diagnostic_only"
    )
    report["counts"] = {
        status: statuses.count(status)
        for status in sorted(set(statuses))
    }
    _write_json(output_dir / "report.json", report)
    return report
