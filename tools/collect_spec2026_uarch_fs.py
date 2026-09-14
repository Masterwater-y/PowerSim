#!/usr/bin/env python3
"""Parallel, resumable FS FST collection for the SPEC2026 uarch sweep."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "configs/spec2026-uarch-exploration-v1.json"
DEFAULT_RUN_ROOT = ROOT / "tmp/spec2026-uarch-exploration-v1-native-v28_2"
TCSIM_ROOT = Path("/data00/yinhaolang/TCSim")
GEM5_ROOT = Path("/data00/yinhaolang/gem5-fs")
RUNNER = TCSIM_ROOT / "scripts/gem5_fs_roi.py"
IDENTITY_VALIDATOR = ROOT / "tools/validate_fs_oracle_identity.py"
PRIVILEGE_AUDITOR = ROOT / "tools/audit_fst_privilege.py"
EVENT_DICTIONARY = ROOT / "configs/pmu-event-dictionary-v1.json"
WORKLOAD_MANIFEST = TCSIM_ROOT / "configs/gem5/spec2026_test_workloads.json"
RESULT_RE = re.compile(r"^\[gem5-fs-roi\] result=(.+)$", re.MULTILINE)
ENTRY_RE = re.compile(r"^\[gem5-fs-roi\] entry=(.+)$", re.MULTILINE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def size_bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    for suffix, scale in (
        ("kib", 1 << 10), ("mib", 1 << 20), ("gib", 1 << 30),
        ("kb", 1000), ("mb", 1000**2), ("gb", 1000**3),
    ):
        if text.endswith(suffix):
            return int(text[: -len(suffix)]) * scale
    return int(text)


def effective_profile(matrix: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    effective = dict(matrix["common"])
    effective.update(profile.get("gem5", {}))
    return effective


def fastsim_expected(matrix: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    gem5 = effective_profile(matrix, profile)
    expected = {
        "core.rob_entries": int(gem5["rob_entries"]),
        "cache.l1d.size": size_bytes(gem5["l1d_size"]),
        "cache.l1d.associativity": int(gem5["l1d_assoc"]),
        "cache.l2.size": size_bytes(gem5["l2_size"]),
        "cache.l2.associativity": int(gem5["l2_assoc"]),
        "cache.llc.size": size_bytes(gem5["l3_size_per_bank"])
        * int(gem5["num_l3_banks"]),
        "cache.llc.associativity": int(gem5["l3_assoc"]),
        "uncore.cha_count": int(gem5["num_l3_banks"]),
        "dram.channels": int(gem5["mem_channels"]),
        "dram.size": size_bytes(gem5["mem_size"]),
    }
    for key, value in profile.get("fastsim", {}).items():
        normalized = (
            size_bytes(value)
            if key.endswith(".size")
            and (key.startswith("cache.") or key == "dram.size")
            else value
        )
        if expected.get(key) != normalized:
            raise ValueError(
                f"{profile['id']}: FastSim override {key}={value!r} does not "
                f"match gem5-derived value {expected.get(key)!r}"
            )
    return expected


def static_preflight(matrix: dict[str, Any]) -> dict[str, Any]:
    schema = matrix.get("schema")
    if schema not in {
        "fastsim-spec2026-fs-uarch-sweep-v1",
        "fastsim-spec2026-fs-uarch-sweep-v2",
    }:
        raise ValueError("unsupported matrix schema")
    names = [item["name"] for item in matrix["workloads"]]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate workload names: {names}")
    if schema == "fastsim-spec2026-fs-uarch-sweep-v1":
        if names != ["731.astcenc_r", "706.stockfish_r", "811.tealeaf_s"]:
            raise ValueError(f"unexpected v1 workload set/order: {names}")
    elif len(names) != 6:
        raise ValueError(f"v2 sweep requires exactly six workloads, got {names}")
    if {750, 867} & {int(name.split(".", 1)[0]) for name in names}:
        raise ValueError("deleted workloads 750/867 cannot enter the sweep")
    workload_manifest = load(WORKLOAD_MANIFEST).get("workloads", {})
    disk_image_checks = []
    for workload in matrix["workloads"]:
        aux_disk = Path(str(workload["aux_disk"]))
        if not aux_disk.is_file():
            raise ValueError(
                f"{workload['name']}: auxiliary disk does not exist: {aux_disk}"
            )
        manifest_row = workload_manifest.get(workload["name"])
        if not isinstance(manifest_row, dict) or not manifest_row.get("binary"):
            raise ValueError(
                f"{workload['name']}: workload/binary missing from {WORKLOAD_MANIFEST}"
            )
        required_paths = list(workload.get("required_disk_paths", []))
        if manifest_row["binary"] not in required_paths:
            required_paths.insert(0, str(manifest_row["binary"]))
        checked_paths = []
        for relative in required_paths:
            relative_path = Path(str(relative))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError(
                    f"{workload['name']}: unsafe required_disk_paths entry {relative!r}"
                )
            internal = (
                f"/spec2026/benchspec/CPU/{workload['name']}/run/"
                f"{relative_path.as_posix()}"
            )
            probe = subprocess.run(
                ["debugfs", "-R", f"stat {internal}", str(aux_disk)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if "Inode:" not in probe.stdout or "File not found" in probe.stdout:
                raise ValueError(
                    f"{workload['name']}: required path missing from {aux_disk}: "
                    f"{internal}\n{probe.stdout.strip()}"
                )
            checked_paths.append(internal)
        disk_image_checks.append({
            "workload": workload["name"],
            "aux_disk": str(aux_disk),
            "aux_disk_size_bytes": aux_disk.stat().st_size,
            "required_paths": checked_paths,
            "valid": True,
        })
    allowed = {"rob_entries", "l1d_size", "l2_size", "l3_size_per_bank"}
    seen = set()
    rows = []
    design_class_counts = {"baseline": 0, "single": 0, "multi": 0}
    for profile in matrix["profiles"]:
        profile_id = str(profile["id"])
        if profile_id in seen:
            raise ValueError(f"duplicate profile: {profile_id}")
        seen.add(profile_id)
        changed = set(profile.get("gem5", {}))
        if not changed <= allowed:
            raise ValueError(
                f"{profile_id}: unsupported sweep parameters {sorted(changed - allowed)}"
            )
        for key in changed:
            if profile["gem5"][key] == matrix["common"][key]:
                raise ValueError(
                    f"{profile_id}: {key} repeats the baseline value instead of changing it"
                )
        inferred_class = (
            "baseline" if not changed else "single" if len(changed) == 1 else "multi"
        )
        design_class = str(profile.get("design_class", inferred_class))
        if design_class != inferred_class:
            raise ValueError(
                f"{profile_id}: design_class={design_class!r} but changed "
                f"parameters imply {inferred_class!r}"
            )
        if schema == "fastsim-spec2026-fs-uarch-sweep-v1" and design_class == "multi":
            raise ValueError(f"{profile_id}: v1 sweep must be one-factor-at-a-time")
        design_class_counts[design_class] += 1
        rows.append({
            "profile": profile_id,
            "design_class": design_class,
            "changed_parameters": sorted(changed),
            "gem5": effective_profile(matrix, profile),
            "fastsim_expected": fastsim_expected(matrix, profile),
        })
    if design_class_counts["baseline"] != 1:
        raise ValueError(
            f"sweep requires exactly one baseline, got {design_class_counts['baseline']}"
        )
    if schema == "fastsim-spec2026-fs-uarch-sweep-v2" and (
        design_class_counts["single"] == 0 or design_class_counts["multi"] == 0
    ):
        raise ValueError(
            "v2 sweep requires both single-factor and multi-factor profiles"
        )
    runner_text = RUNNER.read_text(encoding="utf-8")
    main_text = (TCSIM_ROOT / "configs/gem5/x86_fs_kvm_boot_checkpoint.py").read_text(
        encoding="utf-8"
    )
    required = ("--rob-entries", "--l1d-size", "--l2-size", "--l3-size")
    missing = [item for item in required if item not in runner_text or item not in main_text]
    if missing:
        raise ValueError(f"TCSim uarch CLI plumbing is missing: {missing}")
    return {
        "schema": "fastsim-spec2026-uarch-static-alignment-v2",
        "valid": True,
        "workloads": names,
        "disk_image_checks": disk_image_checks,
        "design_class_counts": design_class_counts,
        "profiles": rows,
    }


@dataclass(frozen=True)
class Task:
    profile: dict[str, Any]
    workload: dict[str, Any]
    cores: int

    @property
    def key(self) -> str:
        return f"{self.profile['id']}/c{self.cores:02d}/{self.workload['name']}"


def validate_result(
    result: Path, task: Task, matrix: dict[str, Any], target: int, audit_path: Path
) -> dict[str, Any]:
    request = load(result / "request.json")
    trace = load(result / "tao_trace/trace.json")
    effective = load(result / "effective-target.json")
    expected = fastsim_expected(matrix, task.profile)
    errors: list[str] = []
    checks: dict[str, Any] = {}

    def check(name: str, actual: Any, wanted: Any) -> None:
        checks[name] = {"actual": actual, "expected": wanted, "match": actual == wanted}
        if actual != wanted:
            errors.append(f"{name}: effective={actual!r} expected={wanted!r}")

    check("core.count", effective["core"]["count"], task.cores)
    check("core.rob_entries", effective["core"]["pipeline"]["numROBEntries"], expected["core.rob_entries"])
    check("cache.l1d.size", effective["cache"]["l1d"]["size_b"], size_bytes(expected["cache.l1d.size"]))
    check("cache.l1d.associativity", effective["cache"]["l1d"]["assoc"], expected["cache.l1d.associativity"])
    check("cache.l2.size", effective["cache"]["l2"]["size_b"], size_bytes(expected["cache.l2.size"]))
    check("cache.l2.associativity", effective["cache"]["l2"]["assoc"], expected["cache.l2.associativity"])
    check("cache.llc.size", effective["cache"]["l3"]["size_b"], int(expected["cache.llc.size"]))
    check("cache.llc.associativity", effective["cache"]["l3"]["assoc"], expected["cache.llc.associativity"])
    check("uncore.cha_count", effective["cache"]["l3"]["num_banks"], expected["uncore.cha_count"])
    check("dram.channels", effective["dram"]["num_channels"], expected["dram.channels"])
    check("dram.size", effective["dram"]["size_b"], expected["dram.size"])
    sampling = request.get("sampling", {})
    check("sampling.roi_target_domain", sampling.get("roi_target_domain"), "user-fst")
    check("sampling.functional_user_only", sampling.get("functional_user_only"), False)
    check("sampling.functional_include_kernel", sampling.get("functional_include_kernel"), True)
    check("sampling.roi_insts", sampling.get("roi_insts"), target)
    check("trace.trace_scope", trace.get("trace_scope"), "user-plus-kernel")
    per_core = trace.get("per_core", {})
    check("trace.core_ids", sorted(int(core) for core in per_core), list(range(task.cores)))
    measurement_kernel_records = 0
    for core in range(task.cores):
        row = per_core.get(str(core), {})
        user_records = int(
            row.get("measurement_user_records", row.get("measurement_records", 0))
        )
        records = int(row.get("measurement_records", 0))
        if user_records < target:
            errors.append(
                f"core{core}.measurement_user_records={user_records} < {target}"
            )
        if records < user_records:
            errors.append(
                f"core{core}.measurement_records={records} < "
                f"measurement_user_records={user_records}"
            )
        measurement_kernel_records += records - user_records
    checks["trace.measurement_kernel_records"] = {
        "actual": measurement_kernel_records,
        "expected": "native-capable; zero is valid for a short quiet window",
        "match": True,
    }
    audit = {
        "schema": "fastsim-spec2026-uarch-case-alignment-v1",
        "case": task.key,
        "result": str(result),
        "valid": not errors,
        "checks": checks,
        "errors": errors,
    }
    atomic_json(audit_path, audit)
    if errors:
        raise ValueError("; ".join(errors))
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--checkpoint-root", type=Path)
    parser.add_argument("--target-records", type=int)
    parser.add_argument(
        "--jobs", type=int, default=max(1, min(54, os.cpu_count() or 1)),
        help="concurrent cases (default: all 54 matrix cases, capped by host CPUs)",
    )
    parser.add_argument("--sample-timeout-seconds", type=int)
    parser.add_argument(
        "--task-timeout-seconds",
        type=int,
        help="hard wall timeout for checkpoint creation plus sampling",
    )
    parser.add_argument(
        "--transient-retries", type=int, default=2,
        help="retries for the known Ruby functional-read restore race",
    )
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--cores", action="append", type=int, default=[])
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs <= 0:
        raise SystemExit("--jobs must be positive")
    if args.transient_retries < 0:
        raise SystemExit("--transient-retries must be non-negative")
    matrix_path = args.matrix.resolve()
    run_root = args.run_root.resolve()
    checkpoint_root = (
        args.checkpoint_root.resolve()
        if args.checkpoint_root
        else run_root / "checkpoints"
    )
    matrix = load(matrix_path)
    target = args.target_records or int(matrix["target_records_per_core"])
    timeout = args.sample_timeout_seconds or (900 if target <= 10_000 else 1_800)
    task_timeout = args.task_timeout_seconds or timeout + 900
    if target <= 0 or timeout <= 0 or task_timeout <= timeout:
        raise SystemExit(
            "target/sample timeout must be positive and task timeout must exceed sample timeout"
        )

    alignment = static_preflight(matrix)
    run_root.mkdir(parents=True, exist_ok=True)
    atomic_json(run_root / "static-alignment.json", alignment)
    process_tmp = run_root / "process-tmp"
    process_tmp.mkdir(parents=True, exist_ok=True)
    profiles = [p for p in matrix["profiles"] if not args.profile or p["id"] in args.profile]
    workloads = [w for w in matrix["workloads"] if not args.workload or w["name"] in args.workload]
    cores = args.cores or [int(value) for value in matrix["core_counts"]]
    tasks = [Task(p, w, c) for p in profiles for c in cores for w in workloads]
    if args.max_cases:
        tasks = tasks[: args.max_cases]
    if args.dry_run:
        print(json.dumps({"target": target, "tasks": [t.key for t in tasks]}, indent=2))
        return 0

    env = dict(os.environ)
    env["TMPDIR"] = str(process_tmp)
    env["FASTSIM_EFFECTIVE_TARGET_GENERATOR"] = str(ROOT / "tools/generate_fs_effective_target.py")
    env["FASTSIM_EFFECTIVE_TARGET_PYTHON"] = env.get("PYTHON_BIN", "/data00/yinhaolang/infer/.venv/bin/python")
    env["FASTSIM_EVENT_DICTIONARY"] = str(EVENT_DICTIONARY)
    env["FUNCTIONAL_TRACE_MODE"] = "native-kernel"
    python = env["FASTSIM_EFFECTIVE_TARGET_PYTHON"]
    lock = threading.Lock()
    active_lock = threading.Lock()
    active_processes: dict[int, subprocess.Popen[str]] = {}
    shutdown_requested = threading.Event()
    shutdown_signal = 0

    def stop_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def handle_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum
        shutdown_requested.set()
        with active_lock:
            processes = list(active_processes.values())
        for active in processes:
            stop_process_group(active)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def run_captured(command: list[str]) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with active_lock:
            active_processes[process.pid] = process
        timed_out = False
        try:
            try:
                output, _ = process.communicate(timeout=task_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process_group(process)
                try:
                    output, _ = process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    output, _ = process.communicate()
        finally:
            with active_lock:
                active_processes.pop(process.pid, None)
        returncode = 124 if timed_out else process.returncode
        if timed_out:
            output = (
                (output or "")
                + f"\n[collector] task wall timeout after {task_timeout}s; "
                "terminated runner process group\n"
            )
        return subprocess.CompletedProcess(command, returncode, output or "")
    state = {
        "schema": "fastsim-spec2026-uarch-collection-status-v1",
        "matrix": str(matrix_path),
        "target_records_per_core": target,
        "created_at_utc": now(),
        "updated_at_utc": now(),
        "tasks": {},
    }
    status_path = run_root / "status.json"
    if status_path.is_file():
        previous = load(status_path)
        if previous.get("target_records_per_core") == target:
            state["created_at_utc"] = previous.get("created_at_utc", state["created_at_utc"])
            state["tasks"] = previous.get("tasks", {})

    def save() -> None:
        state["updated_at_utc"] = now()
        atomic_json(status_path, state)

    def run(task: Task) -> tuple[str, str, str | None]:
        if shutdown_requested.is_set():
            return task.key, "cancelled", "collector shutdown requested"
        case_root = run_root / "cases" / task.profile["id"] / f"c{task.cores:02d}" / task.workload["name"]
        case_root.mkdir(parents=True, exist_ok=True)
        record_path = case_root / "case.json"
        audit_path = case_root / "alignment.json"
        if record_path.is_file() and not args.force:
            record = load(record_path)
            result_text = record.get("result_dir")
            if record.get("status") == "complete" and result_text:
                try:
                    validate_result(Path(result_text), task, matrix, target, audit_path)
                    return task.key, "skipped", None
                except Exception:
                    pass
        geometry = effective_profile(matrix, task.profile)
        # TCSim names selection metadata at second resolution.  Profiles for
        # the same workload/core are intentionally launched together, so a
        # shared tmp root can make two writers truncate the same JSON file.
        # Give every matrix task its own staging roots; result/checkpoint roots
        # remain shared and content-addressed.
        task_tmp = (
            run_root / "driver-tmp" / task.profile["id"] /
            f"c{task.cores:02d}" / task.workload["name"]
        )
        task_trace_tmp = (
            run_root / "trace-scratch" / task.profile["id"] /
            f"c{task.cores:02d}" / task.workload["name"]
        )
        # Tiny smoke targets need a fixed scheduling runway: at WORKBEGIN Linux may
        # still be spreading target threads across CPUs.  A pure 100x multiplier
        # gives a 1K smoke only 100K total instructions and can stop before every
        # core sees the target CR3.  Formal 10M collection retains the established
        # 100x (1B-instruction) safety limit.
        safety_multiplier = max(100, (10_000_000 + target - 1) // target)
        command = [
            python, str(RUNNER), "run", task.workload["name"], str(task.cores),
            "--warmup-mode", "source", "--roi-insts", str(target),
            "--roi-target-domain", "user-fst", "--roi-safety-multiplier", str(safety_multiplier),
            "--roi-stop-policy", "all-core", "--sample-timeout-seconds", str(timeout),
            "--cache-hierarchy", "mesi-three-level", "--mem-size", str(geometry["mem_size"]),
            "--rob-entries", str(geometry["rob_entries"]),
            "--l1i-size", str(geometry["l1i_size"]), "--l1i-assoc", str(geometry["l1i_assoc"]),
            "--l1d-size", str(geometry["l1d_size"]), "--l1d-assoc", str(geometry["l1d_assoc"]),
            "--l2-size", str(geometry["l2_size"]), "--l2-assoc", str(geometry["l2_assoc"]),
            "--l3-size", str(geometry["l3_size_per_bank"]), "--l3-assoc", str(geometry["l3_assoc"]),
            "--num-l3-banks", str(geometry["num_l3_banks"]),
            "--mem-channels", str(geometry["mem_channels"]),
            "--result-root", str(run_root / "source"),
            "--tmp-root", str(task_tmp),
            "--trace-tmp-root", str(task_trace_tmp),
            "--checkpoint-root", str(checkpoint_root),
            "--gem5-root", str(GEM5_ROOT), "--aux-disk", str(task.workload["aux_disk"]),
            "--emit-functional-trace", "--trace-format", "fst", "--measure-cpl",
            "--functional-include-kernel", "--native-anomaly-limit", "32",
            "--reuse-binary-mismatch", "--reuse-restore-config-mismatch",
        ]
        started = time.monotonic()
        attempts: list[str] = []
        process: subprocess.CompletedProcess[str] | None = None
        rebuilt_checkpoint = False
        # First use cheap restore retries.  If every retry fails at the same
        # Ruby functional-read boundary, quarantine that exact cache entry and
        # perform one fresh KVM checkpoint build.  The move is recoverable and
        # constrained below checkpoint_root.
        for attempt in range(1, args.transient_retries + 3):
            process = run_captured(command)
            attempts.append(f"\n===== attempt {attempt} =====\n{process.stdout}")
            if process.returncode == 0:
                break
            if "Ruby functional read failed" not in process.stdout:
                break
            if attempt <= args.transient_retries:
                continue
            if rebuilt_checkpoint:
                break
            entries = ENTRY_RE.findall(process.stdout)
            if not entries:
                break
            entry = Path(entries[-1]).resolve()
            try:
                entry.relative_to(checkpoint_root)
            except ValueError:
                attempts.append(
                    f"\nrefusing to quarantine checkpoint outside {checkpoint_root}: {entry}\n"
                )
                break
            if not entry.is_dir():
                break
            quarantine = (
                run_root / "quarantined-checkpoints" / task.profile["id"] /
                f"c{task.cores:02d}" / task.workload["name"] /
                f"{entry.name}-{int(time.time())}"
            )
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            entry.rename(quarantine)
            attempts.append(
                f"\nquarantined deterministic Ruby-fatal checkpoint "
                f"{entry} -> {quarantine}; rebuilding once\n"
            )
            rebuilt_checkpoint = True
        assert process is not None
        (case_root / "collector.log").write_text(
            "command=" + " ".join(command) + "\n" + "".join(attempts),
            encoding="utf-8",
        )
        if process.returncode:
            error = f"collector exit={process.returncode}"
            atomic_json(record_path, {"status": "failed", "error": error, "wall_time_seconds": time.monotonic() - started})
            return task.key, "failed", error
        matches = RESULT_RE.findall(process.stdout)
        if not matches:
            error = "collector did not print result path"
            atomic_json(record_path, {"status": "failed", "error": error})
            return task.key, "failed", error
        result = Path(matches[-1]).resolve()
        try:
            validate_result(result, task, matrix, target, audit_path)
            identity_out = case_root / "oracle-identity.json"
            identity = subprocess.run(
                [python, str(IDENTITY_VALIDATOR), "--result", str(result),
                 "--event-dictionary", str(EVENT_DICTIONARY), "--output", str(identity_out)],
                cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            (case_root / "oracle-identity.log").write_text(identity.stdout, encoding="utf-8")
            if identity.returncode:
                raise ValueError(f"oracle identity exit={identity.returncode}")
            privilege_out = case_root / "fst-privilege.json"
            privilege = subprocess.run(
                [python, str(PRIVILEGE_AUDITOR),
                 "--trace-dir", str(result / "tao_trace"),
                 "--require-user",
                 "--output", str(privilege_out)],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            (case_root / "fst-privilege.log").write_text(
                privilege.stdout, encoding="utf-8"
            )
            if privilege.returncode:
                raise ValueError(f"FST privilege audit exit={privilege.returncode}")
            privilege_report = load(privilege_out)
            if any(
                not bool(row.get("privilege_feature"))
                for row in privilege_report.get("files", [])
            ):
                raise ValueError(
                    "native FST set contains a file without the privilege feature"
                )
            record = {
                "status": "complete", "case": task.key, "result_dir": str(result),
                "target_records_per_core": target,
                "wall_time_seconds": time.monotonic() - started,
            }
            atomic_json(record_path, record)
            return task.key, "completed", None
        except Exception as exc:
            error = str(exc)
            atomic_json(record_path, {"status": "failed", "result_dir": str(result), "error": error})
            return task.key, "failed", error

    failed = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run, task): task for task in tasks}
        for future in as_completed(futures):
            key, status, error = future.result()
            print(f"[{status}] {key}" + (f": {error}" if error else ""), flush=True)
            with lock:
                state["tasks"][key] = {"status": status, "error": error, "updated_at_utc": now()}
                save()
            failed += int(status in {"failed", "cancelled"})
    completed = sum(1 for task in tasks if (run_root / "cases" / task.profile["id"] / f"c{task.cores:02d}" / task.workload["name"] / "case.json").is_file() and load(run_root / "cases" / task.profile["id"] / f"c{task.cores:02d}" / task.workload["name"] / "case.json").get("status") == "complete")
    state["summary"] = {"expected": len(tasks), "completed": completed, "failed": failed, "status": "complete" if completed == len(tasks) and failed == 0 else "failed"}
    save()
    print(f"collection expected={len(tasks)} completed={completed} failed={failed} root={run_root}")
    if shutdown_requested.is_set():
        return 128 + shutdown_signal
    return 0 if completed == len(tasks) and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
