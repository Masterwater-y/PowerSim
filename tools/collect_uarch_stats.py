#!/usr/bin/env python3
"""Parallel, resumable gem5 CPI/PMU collection for FastSim uarch sweeps."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX = ROOT / "configs" / "uarch-first-batch.json"
DEFAULT_GEM5 = Path(
    "/data00/yinhaolang/gem5/build/X86_MESI_Three_Level/gem5.opt"
)
DEFAULT_GEM5_CONFIG = ROOT / "tools" / "gem5" / "run_uarch_stats_se.py"
DEFAULT_BIN_DIR = Path("/data00/yinhaolang/TSim/workloads/bin")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
PRINT_LOCK = threading.Lock()
ACTIVE_LOCK = threading.Lock()
ACTIVE_PROCESSES: set[subprocess.Popen] = set()


@dataclass(frozen=True)
class Task:
    profile_id: str
    profile_description: str
    workload: str
    domain: str
    scale: int
    cores: int
    seed: int
    gem5_config: dict[str, Any]
    fastsim_overrides: dict[str, Any]
    binary: Path
    final_dir: Path

    @property
    def label(self) -> str:
        return f"{self.profile_id}/c{self.cores:02d}/W_{self.workload}"


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a stats-only gem5 microarchitecture/workload matrix in "
            "parallel and emit validated CPI/PMU JSON plus summary.csv"
        )
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument(
        "--gem5-config", type=Path, default=DEFAULT_GEM5_CONFIG
    )
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "tmp" / "uarch-se-first-batch"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Concurrent gem5 processes; 0 selects a CPU/RAM-aware value",
    )
    parser.add_argument(
        "--timeout", type=int, default=7200, help="Per-case timeout in seconds"
    )
    parser.add_argument(
        "--cores",
        help="Comma-separated core counts overriding matrix core_counts",
    )
    parser.add_argument("--seed", type=int, help="Override matrix seed")
    parser.add_argument(
        "--uarch",
        action="append",
        default=[],
        help="Profile ID/glob to include; repeatable",
    )
    parser.add_argument(
        "--workload",
        action="append",
        default=[],
        help="Workload name/glob to include; repeatable",
    )
    parser.add_argument(
        "--expected-profiles-only",
        action="store_true",
        help=(
            "collect baseline plus each workload's expected_profiles instead "
            "of the full selected profile/workload product"
        ),
    )
    parser.add_argument(
        "--max-cases", type=int, default=0, help="Limit selected cases"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: top level must be an object")
    return value


def parse_core_counts(text: str | None, fallback: Iterable[int]) -> list[int]:
    values = list(fallback) if text is None else [int(x) for x in text.split(",")]
    if not values or any(value <= 0 for value in values):
        raise SystemExit("core counts must be positive")
    return sorted(set(values))


def selected(value: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(value, p) for p in patterns)


def build_tasks(args: argparse.Namespace, matrix: dict[str, Any]) -> list[Task]:
    common = matrix.get("common", {})
    profiles = matrix.get("profiles", [])
    workloads = matrix.get("workloads", [])
    core_counts = parse_core_counts(args.cores, matrix.get("core_counts", [4]))
    seed = int(matrix.get("seed", 0) if args.seed is None else args.seed)

    profile_ids: set[str] = set()
    tasks: list[Task] = []
    for profile in profiles:
        profile_id = str(profile["id"])
        if not SAFE_ID.fullmatch(profile_id):
            raise SystemExit(f"unsafe profile id: {profile_id}")
        if profile_id in profile_ids:
            raise SystemExit(f"duplicate profile id: {profile_id}")
        profile_ids.add(profile_id)
        if not selected(profile_id, args.uarch):
            continue
        effective = dict(common)
        effective.update(profile.get("gem5", {}))
        for workload in workloads:
            name = str(workload["name"])
            if not SAFE_ID.fullmatch(name):
                raise SystemExit(f"unsafe workload name: {name}")
            if not selected(name, args.workload):
                continue
            if args.expected_profiles_only:
                expected = {str(value) for value in workload.get("expected_profiles", [])}
                if profile_id != "baseline" and profile_id not in expected:
                    continue
            binary = (args.bin_dir / name).resolve()
            for cores in core_counts:
                final_dir = (
                    args.out.resolve()
                    / profile_id
                    / f"c{cores:02d}"
                    / f"W_{name}"
                )
                tasks.append(
                    Task(
                        profile_id=profile_id,
                        profile_description=str(profile.get("description", "")),
                        workload=name,
                        domain=str(workload.get("domain", "unknown")),
                        scale=int(workload.get("scale", 1)),
                        cores=cores,
                        seed=seed,
                        gem5_config=effective,
                        fastsim_overrides=dict(profile.get("fastsim", {})),
                        binary=binary,
                        final_dir=final_dir,
                    )
                )
    tasks.sort(key=lambda task: (task.profile_id, task.cores, task.workload))
    if args.max_cases:
        tasks = tasks[: args.max_cases]
    return tasks


def available_memory_bytes() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", text, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def automatic_jobs() -> int:
    cpu_limit = max(1, os.cpu_count() or 1)
    memory = available_memory_bytes()
    # Measured C4/C16 Ruby+O3 jobs use roughly 9--11 GiB. Reserve 12 GiB so
    # automatic concurrency does not turn page-cache pressure into swap/OOM.
    memory_limit = max(1, memory // (12 << 30)) if memory else cpu_limit
    return max(1, min(cpu_limit, memory_limit, 32))


def size_bytes(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = value.strip()
    units = {
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "B": 1,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(text)


def command_for(
    task: Task, args: argparse.Namespace, output_dir: Path
) -> list[str]:
    command = [
        str(args.gem5.resolve()),
        f"--outdir={output_dir}",
        str(args.gem5_config.resolve()),
        "--cmd",
        str(task.binary),
        "--workload-args",
        str(task.cores),
        str(task.scale),
        "1",
        str(task.seed),
        "--num-cores",
        str(task.cores),
    ]
    for key in sorted(task.gem5_config):
        value = task.gem5_config[key]
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                command.append(option)
        else:
            command.extend([option, str(value)])
    return command


def parse_ini(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return sections


def validate_effective_config(path: Path, task: Task) -> dict[str, Any]:
    sections = parse_ini(path)
    cfg = task.gem5_config
    errors: list[str] = []

    def check(section: str, key: str, expected: Any) -> None:
        actual = sections.get(section, {}).get(key)
        if actual is None:
            errors.append(f"missing {section}.{key}")
            return
        if isinstance(expected, int):
            try:
                matches = int(actual) == expected
            except ValueError:
                matches = False
        else:
            matches = actual == str(expected)
        if not matches:
            errors.append(
                f"{section}.{key}: requested={expected} effective={actual}"
            )

    cpu_keys = {
        "fetch_width": "fetchWidth",
        "decode_width": "decodeWidth",
        "rename_width": "renameWidth",
        "dispatch_width": "dispatchWidth",
        "issue_width": "issueWidth",
        "wb_width": "wbWidth",
        "commit_width": "commitWidth",
        "rob_entries": "numROBEntries",
        "lq_entries": "LQEntries",
        "sq_entries": "SQEntries",
    }
    for core in range(task.cores):
        cpu = f"board.processor.switch{core}.core"
        for source_key, ini_key in cpu_keys.items():
            check(cpu, ini_key, int(cfg[source_key]))
        check(f"{cpu}.instQueues", "numEntries", int(cfg["iq_entries"]))
        check(f"{cpu}.mmu.dtb", "size", int(cfg["dtlb_entries"]))

        l1 = f"board.cache_hierarchy.ruby_system.l1_controllers{core}"
        check(f"{l1}.Icache", "size", size_bytes(cfg["l1i_size"]))
        check(f"{l1}.Icache", "assoc", int(cfg["l1i_assoc"]))
        check(f"{l1}.Dcache", "size", size_bytes(cfg["l1d_size"]))
        check(f"{l1}.Dcache", "assoc", int(cfg["l1d_assoc"]))
        check(l1, "number_of_TBEs", int(cfg["l1_tbes"]))
        check(
            f"{l1}.sequencer",
            "max_outstanding_requests",
            int(cfg["sequencer_outstanding"]),
        )

        l2 = f"board.cache_hierarchy.ruby_system.l2_controllers{core}"
        check(f"{l2}.cache", "size", size_bytes(cfg["l2_size"]))
        check(f"{l2}.cache", "assoc", int(cfg["l2_assoc"]))
        check(l2, "number_of_TBEs", int(cfg["l2_tbes"]))

    l3_base = "board.cache_hierarchy.ruby_system.l3_controllers"
    l3_sections = sorted(
        name for name in sections if re.fullmatch(re.escape(l3_base) + r"\d+", name)
    )
    if len(l3_sections) != int(cfg["num_l3_banks"]):
        errors.append(
            "L3 bank count: requested="
            f"{cfg['num_l3_banks']} effective={len(l3_sections)}"
        )
    for controller in l3_sections:
        check(f"{controller}.L2cache", "size", size_bytes(cfg["l3_size"]))
        check(f"{controller}.L2cache", "assoc", int(cfg["l3_assoc"]))
        check(controller, "number_of_TBEs", int(cfg["l3_tbes"]))

    directory_base = "board.cache_hierarchy.ruby_system.directory_controllers"
    directory_sections = sorted(
        name
        for name in sections
        if re.fullmatch(re.escape(directory_base) + r"\d+", name)
    )
    for controller in directory_sections:
        check(controller, "number_of_TBEs", int(cfg["directory_tbes"]))

    mem_base = "board.memory.mem_ctrl"
    mem_sections = sorted(
        name for name in sections if re.fullmatch(re.escape(mem_base) + r"\d+", name)
    )
    if len(mem_sections) != int(cfg["mem_channels"]):
        errors.append(
            "memory channel count: requested="
            f"{cfg['mem_channels']} effective={len(mem_sections)}"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "checked_cores": task.cores,
        "effective_l3_banks": len(l3_sections),
        "effective_memory_channels": len(mem_sections),
    }


def indexed_int(text: str, pattern: str) -> dict[int, int]:
    return {
        int(match.group(1)): int(float(match.group(2)))
        for match in re.finditer(pattern, text)
    }


def indexed_float(text: str, pattern: str) -> dict[int, float]:
    result: dict[int, float] = {}
    for match in re.finditer(pattern, text):
        try:
            result[int(match.group(1))] = float(match.group(2))
        except ValueError:
            result[int(match.group(1))] = math.nan
    return result


def sum_dict(value: dict[int, int]) -> int:
    return sum(value.values())


def extract_metrics(stats_path: Path, task: Task, wall_seconds: float) -> dict[str, Any]:
    text = stats_path.read_text(encoding="utf-8", errors="replace")
    cycles = indexed_int(
        text, r"board\.processor\.switch(\d+)\.core\.numCycles\s+([0-9.eE+-]+)"
    )
    instructions = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.commitStats0\.numInsts\s+([0-9.eE+-]+)",
    )
    uops = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.commitStats0\.numOps\s+([0-9.eE+-]+)",
    )
    if len(cycles) != task.cores or len(uops) != task.cores:
        raise ValueError(
            f"expected {task.cores} switch-core stats, found "
            f"cycles={len(cycles)} uops={len(uops)}"
        )
    total_cycles = sum_dict(cycles)
    total_uops = sum_dict(uops)
    total_instructions = sum_dict(instructions)
    if total_uops == 0 or total_instructions == 0:
        raise ValueError("zero committed UOP/instruction count")

    def ruby(name: str, stat: str) -> dict[int, int]:
        return indexed_int(
            text,
            rf"ruby_system\.{name}(\d+).*?\.{stat}\s+([0-9.eE+-]+)",
        )

    branch_committed = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.branchPred\.committed_0::total\s+([0-9.eE+-]+)",
    )
    branch_misses = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.branchPred\.mispredicted_0::total\s+([0-9.eE+-]+)",
    )
    dtlb_rd_access = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.rdAccesses\s+([0-9.eE+-]+)",
    )
    dtlb_wr_access = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.wrAccesses\s+([0-9.eE+-]+)",
    )
    dtlb_rd_miss = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.rdMisses\s+([0-9.eE+-]+)",
    )
    dtlb_wr_miss = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.wrMisses\s+([0-9.eE+-]+)",
    )
    rob_full = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.rename\.ROBFullEvents\s+([0-9.eE+-]+)",
    )
    iq_full = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.rename\.IQFullEvents\s+([0-9.eE+-]+)",
    )
    lsq_full = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.iew\.lsqFullEvents\s+([0-9.eE+-]+)",
    )
    commit_branch_mispredicts = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.commit\.branchMispredicts\s+([0-9.eE+-]+)",
    )
    fetch_squash_cycles = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.fetch\.status::squashing\s+([0-9.eE+-]+)",
    )
    icache_stall_cycles = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.fetchStats0\.icacheStallCycles\s+([0-9.eE+-]+)",
    )
    squashed_insts_examined = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.squashedInstsExamined\s+([0-9.eE+-]+)",
    )
    commit_squashed_insts = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.commit\.commitSquashedInsts\s+([0-9.eE+-]+)",
    )
    rename_register_full = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.rename\.fullRegistersEvents\s+([0-9.eE+-]+)",
    )
    rename_blocked_cycles = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.rename\.status::Blocked\s+([0-9.eE+-]+)",
    )
    rename_unblocking_cycles = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.rename\.status::Unblocking\s+([0-9.eE+-]+)",
    )
    dispatch_blocked_cycles = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.iew\.dispatchStatus::blocked\s+([0-9.eE+-]+)",
    )
    mem_order_violations = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.iew\.memOrderViolationEvents\s+([0-9.eE+-]+)",
    )
    rescheduled_loads = indexed_int(
        text,
        r"board\.processor\.switch(\d+)\.core\.lsq0\.rescheduledLoads\s+([0-9.eE+-]+)",
    )

    l1_access = ruby("l1_controllers", r"Dcache\.m_demand_accesses")
    l1_miss = ruby("l1_controllers", r"Dcache\.m_demand_misses")
    l2_access = ruby("l2_controllers", r"cache\.m_demand_accesses")
    l2_miss = ruby("l2_controllers", r"cache\.m_demand_misses")
    l3_access = ruby("l3_controllers", r"L2cache\.m_demand_accesses")
    l3_miss = ruby("l3_controllers", r"L2cache\.m_demand_misses")

    dram_reads = indexed_int(
        text, r"board\.memory\.mem_ctrl(\d+)\.dram\.readBursts\s+([0-9.eE+-]+)"
    )
    dram_writes = indexed_int(
        text, r"board\.memory\.mem_ctrl(\d+)\.dram\.writeBursts\s+([0-9.eE+-]+)"
    )
    dram_latency_ticks = indexed_int(
        text, r"board\.memory\.mem_ctrl(\d+)\.dram\.totMemAccLat\s+([0-9.eE+-]+)"
    )
    dram_read_row_hit_rate = indexed_float(
        text,
        r"board\.memory\.mem_ctrl(\d+)\.dram\.readRowHitRate\s+([^\s]+)",
    )

    per_core = []
    for core in range(task.cores):
        core_uops = uops.get(core, 0)
        per_core.append(
            {
                "core": core,
                "cycles": cycles.get(core, 0),
                "uops": core_uops,
                "instructions": instructions.get(core, 0),
                "uop_cpi": cycles.get(core, 0) / core_uops if core_uops else None,
            }
        )

    return {
        "schema": "fastsim-gem5-uarch-metrics-v1",
        "uarch": task.profile_id,
        "uarch_description": task.profile_description,
        "workload": task.workload,
        "domain": task.domain,
        "cores": task.cores,
        "seed": task.seed,
        "scale": task.scale,
        "wall_time_seconds": wall_seconds,
        "aggregate_uop_cpi": total_cycles / total_uops,
        "aggregate_macro_cpi": total_cycles / total_instructions,
        "sum_core_cycles": total_cycles,
        "retired_uops": total_uops,
        "retired_instructions": total_instructions,
        "branch_committed": sum_dict(branch_committed),
        "branch_misses": sum_dict(branch_misses),
        "l1d_demand_accesses": sum_dict(l1_access),
        "l1d_demand_misses": sum_dict(l1_miss),
        "private_l2_demand_accesses": sum_dict(l2_access),
        "private_l2_demand_misses": sum_dict(l2_miss),
        "cha_llc_demand_accesses": sum_dict(l3_access),
        "ruby_llc_demand_misses": sum_dict(l3_miss),
        "dtlb_accesses": sum_dict(dtlb_rd_access) + sum_dict(dtlb_wr_access),
        "dtlb_misses": sum_dict(dtlb_rd_miss) + sum_dict(dtlb_wr_miss),
        "rob_full_events": sum_dict(rob_full),
        "iq_full_events": sum_dict(iq_full),
        "lsq_full_events": sum_dict(lsq_full),
        "commit_branch_mispredicts": sum_dict(commit_branch_mispredicts),
        "fetch_squash_cycles": sum_dict(fetch_squash_cycles),
        "icache_stall_cycles": sum_dict(icache_stall_cycles),
        "squashed_insts_examined": sum_dict(squashed_insts_examined),
        "commit_squashed_insts": sum_dict(commit_squashed_insts),
        "rename_register_full_events": sum_dict(rename_register_full),
        "rename_blocked_cycles": sum_dict(rename_blocked_cycles),
        "rename_unblocking_cycles": sum_dict(rename_unblocking_cycles),
        "dispatch_blocked_cycles": sum_dict(dispatch_blocked_cycles),
        "mem_order_violation_events": sum_dict(mem_order_violations),
        "rescheduled_loads": sum_dict(rescheduled_loads),
        "dram_read_bursts": sum_dict(dram_reads),
        "dram_write_bursts": sum_dict(dram_writes),
        "dram_total_access_latency_ticks": sum_dict(dram_latency_ticks),
        "dram_read_row_hit_rate_per_channel": {
            channel: value if math.isfinite(value) else None
            for channel, value in dram_read_row_hit_rate.items()
        },
        "per_core": per_core,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_task(task: Task, args: argparse.Namespace) -> tuple[str, str, str | None]:
    complete = task.final_dir / "complete.json"
    if complete.is_file():
        return task.label, "skipped", None
    if not task.binary.is_file() or not os.access(task.binary, os.X_OK):
        return task.label, "failed", f"missing executable: {task.binary}"

    task.final_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    temp_dir = task.final_dir.parent / f".{task.final_dir.name}.running-{token}"
    temp_dir.mkdir()
    command = command_for(task, args, temp_dir)
    meta = {
        "schema": "fastsim-gem5-uarch-task-v1",
        "label": task.label,
        "command": command,
        "binary": str(task.binary),
        "binary_sha256": file_sha256(task.binary),
        "gem5": str(args.gem5.resolve()),
        "gem5_config": str(args.gem5_config.resolve()),
        "requested_gem5": task.gem5_config,
        "fastsim_overrides": task.fastsim_overrides,
    }
    (temp_dir / "task.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    environment = os.environ.copy()
    required_libraries = [
        "/root/.local/share/uv/python/cpython-3.11.14-linux-x86_64-gnu/lib",
        "/data00/yinhaolang/LLMSim/data/_gem5libs",
        "/opt/gcc-11.5.0/lib64",
    ]
    existing = environment.get("LD_LIBRARY_PATH")
    if existing:
        required_libraries.append(existing)
    environment["LD_LIBRARY_PATH"] = ":".join(required_libraries)

    log(f"[start] {task.label}")
    started = time.monotonic()
    return_code = 1
    timed_out = False
    process: subprocess.Popen | None = None
    with (temp_dir / "gem5.log").open("wb") as output:
        try:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            with ACTIVE_LOCK:
                ACTIVE_PROCESSES.add(process)
            try:
                return_code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process(process)
                return_code = 124
        finally:
            if process is not None:
                with ACTIVE_LOCK:
                    ACTIVE_PROCESSES.discard(process)
    wall_seconds = time.monotonic() - started

    error: str | None = None
    if return_code != 0:
        error = f"gem5 exit={return_code}{' timeout' if timed_out else ''}"
    else:
        try:
            validation = validate_effective_config(temp_dir / "config.ini", task)
            (temp_dir / "config-validation.json").write_text(
                json.dumps(validation, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if not validation["ok"]:
                raise ValueError("; ".join(validation["errors"]))
            metrics = extract_metrics(temp_dir / "stats.txt", task, wall_seconds)
            (temp_dir / "metrics.json").write_text(
                json.dumps(metrics, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            completion = {
                "status": "complete",
                "return_code": return_code,
                "wall_time_seconds": wall_seconds,
                "config_ini_sha256": file_sha256(temp_dir / "config.ini"),
                "stats_txt_sha256": file_sha256(temp_dir / "stats.txt"),
            }
            (temp_dir / "complete.json").write_text(
                json.dumps(completion, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError, KeyError) as exc:
            error = f"post-validation failed: {exc}"

    if error is None:
        if task.final_dir.exists():
            error = f"final directory unexpectedly exists: {task.final_dir}"
        else:
            temp_dir.rename(task.final_dir)
            log(f"[done ] {task.label} {wall_seconds:.1f}s")
            return task.label, "completed", None

    failure = {
        "status": "failed",
        "return_code": return_code,
        "timed_out": timed_out,
        "wall_time_seconds": wall_seconds,
        "error": error,
    }
    (temp_dir / "failed.json").write_text(
        json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failed_dir = task.final_dir.parent / (
        f"{task.final_dir.name}.failed-{time.strftime('%Y%m%d-%H%M%S')}-{token}"
    )
    temp_dir.rename(failed_dir)
    log(f"[fail ] {task.label}: {error}")
    return task.label, "failed", error


SUMMARY_FIELDS = [
    "uarch",
    "workload",
    "domain",
    "cores",
    "seed",
    "scale",
    "aggregate_uop_cpi",
    "aggregate_macro_cpi",
    "gem5_speedup_vs_baseline",
    "sum_core_cycles",
    "retired_uops",
    "retired_instructions",
    "branch_committed",
    "branch_misses",
    "l1d_demand_accesses",
    "l1d_demand_misses",
    "private_l2_demand_accesses",
    "private_l2_demand_misses",
    "cha_llc_demand_accesses",
    "ruby_llc_demand_misses",
    "dtlb_accesses",
    "dtlb_misses",
    "rob_full_events",
    "iq_full_events",
    "lsq_full_events",
    "commit_branch_mispredicts",
    "fetch_squash_cycles",
    "icache_stall_cycles",
    "squashed_insts_examined",
    "commit_squashed_insts",
    "rename_register_full_events",
    "rename_blocked_cycles",
    "rename_unblocking_cycles",
    "dispatch_blocked_cycles",
    "mem_order_violation_events",
    "rescheduled_loads",
    "dram_read_bursts",
    "dram_write_bursts",
    "dram_total_access_latency_ticks",
    "wall_time_seconds",
]


def write_summary(output: Path) -> int:
    rows: list[dict[str, Any]] = []
    for path in output.glob("*/c*/W_*/metrics.json"):
        try:
            rows.append(load_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    baseline = {
        (row["workload"], int(row["cores"]), int(row["seed"])): float(
            row["aggregate_uop_cpi"]
        )
        for row in rows
        if row["uarch"] == "baseline"
    }
    for row in rows:
        key = (row["workload"], int(row["cores"]), int(row["seed"]))
        base_cpi = baseline.get(key)
        row["gem5_speedup_vs_baseline"] = (
            base_cpi / float(row["aggregate_uop_cpi"])
            if base_cpi is not None
            else None
        )
    rows.sort(key=lambda row: (row["uarch"], int(row["cores"]), row["workload"]))
    output.mkdir(parents=True, exist_ok=True)
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in SUMMARY_FIELDS})
    (output / "summary.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return len(rows)


def terminate_all() -> None:
    with ACTIVE_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        terminate_process(process)


def main() -> int:
    args = parse_args()
    matrix = load_json(args.matrix.resolve())
    tasks = build_tasks(args, matrix)
    if not tasks:
        raise SystemExit("selection produced no cases")

    if args.list:
        print("uarch profiles:")
        for profile in matrix["profiles"]:
            print(f"  {profile['id']}: {profile.get('description', '')}")
        print("workloads:")
        for workload in matrix["workloads"]:
            print(
                f"  {workload['name']}: scale={workload.get('scale', 1)} "
                f"domain={workload.get('domain', 'unknown')}"
            )
        return 0

    missing = [task.binary for task in tasks if not task.binary.is_file()]
    for required in (args.gem5, args.gem5_config):
        if not required.is_file():
            missing.append(required)
    if missing:
        raise SystemExit("missing required files:\n" + "\n".join(map(str, missing)))

    jobs = args.jobs or automatic_jobs()
    jobs = min(jobs, len(tasks))
    complete_before = sum((task.final_dir / "complete.json").is_file() for task in tasks)
    log(
        f"matrix={args.matrix.resolve()} cases={len(tasks)} "
        f"already_complete={complete_before} jobs={jobs} out={args.out.resolve()}"
    )
    if args.dry_run:
        for task in tasks:
            print(task.label)
            print("  " + " ".join(command_for(task, args, task.final_dir)))
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "fastsim-uarch-resolved-manifest-v1",
        "matrix": str(args.matrix.resolve()),
        "jobs": jobs,
        "timeout_seconds": args.timeout,
        "cases": [
            {
                "label": task.label,
                "gem5": task.gem5_config,
                "fastsim": task.fastsim_overrides,
            }
            for task in tasks
        ],
    }
    (args.out / "resolved-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    results: list[tuple[str, str, str | None]] = []
    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [executor.submit(run_task, task, args) for task in tasks]
        for future in as_completed(futures):
            results.append(future.result())
    except KeyboardInterrupt:
        log("interrupt received; terminating active gem5 processes")
        terminate_all()
        executor.shutdown(wait=False, cancel_futures=True)
        return 130
    finally:
        executor.shutdown(wait=True, cancel_futures=False)

    summary_cases = write_summary(args.out.resolve())
    failures = [
        {"label": label, "error": error}
        for label, status, error in results
        if status == "failed"
    ]
    status_counts = {
        status: sum(1 for _, item_status, _ in results if item_status == status)
        for status in ("completed", "skipped", "failed")
    }
    run_result = {
        "status_counts": status_counts,
        "summary_cases": summary_cases,
        "failures": failures,
    }
    (args.out / "last-run.json").write_text(
        json.dumps(run_result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log(
        f"finished completed={status_counts['completed']} "
        f"skipped={status_counts['skipped']} failed={status_counts['failed']} "
        f"summary_cases={summary_cases}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
