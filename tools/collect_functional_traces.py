#!/usr/bin/env python3
"""Collect one reusable C4 functional trace per workload/seed.

The gem5 run uses the baseline O3 configuration. TaoTrace emits only committed
functional micro records; timing labels and cache/coherence oracle streams are
disabled. Raw JSONL is converted immediately to canonical FST v7 and discarded.
Portable syscall metadata is embedded in each FST and mirrored to an auditable
JSONL sidecar by the same conversion pass.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collect_uarch_stats import (
    DEFAULT_BIN_DIR,
    DEFAULT_GEM5,
    DEFAULT_GEM5_CONFIG,
    DEFAULT_MATRIX,
    Task as UarchTask,
    command_for,
    file_sha256,
    load_json,
    validate_effective_config,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FASTSIM = ROOT / "build" / "fastsim"
CORE_RE = re.compile(r"(?:switch|cores)(\d*)\.core")
FST_HEADER_BYTES = 72
FST_RECORD_BYTES = 64
FST_VIRTUAL_PAGE_TOKENS = 1 << 0
FST_DESTINATION_CLASS_COUNTS = 1 << 2
FST_SYSCALL_METADATA = 1 << 3
FST_SYSCALL_METADATA_BYTES = 128
MINIMUM_FST_VERSION = 7
ASMAP_HEADER = struct.Struct("<8sIIIIQQQ")
ASMAP_ENTRY = struct.Struct("<QQ")
ASMAP_MAGIC = b"FSTASM1\0"


@dataclass(frozen=True)
class TraceTask:
    workload: str
    domain: str
    scale: int
    cores: int
    seed: int
    binary: Path
    gem5_config: dict[str, Any]
    final_dir: Path

    @property
    def label(self) -> str:
        return f"trace/c{self.cores:02d}/W_{self.workload}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect reusable baseline functional traces without timing labels"
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument("--gem5-config", type=Path, default=DEFAULT_GEM5_CONFIG)
    parser.add_argument("--fastsim", type=Path, default=DEFAULT_FASTSIM)
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "tmp" / "uarch-c4-first-batch" / "traces"
    )
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument(
        "--resume-orphans",
        action="store_true",
        help="resume post-processing from a complete .running-* gem5 staging directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    return parser.parse_args()


def automatic_jobs() -> int:
    cpu = max(1, os.cpu_count() or 1)
    memory = None
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", text, re.MULTILINE)
        if match:
            memory = int(match.group(1)) * 1024
    except OSError:
        pass
    memory_jobs = max(1, memory // (12 << 30)) if memory else cpu
    # JSONL conversion is IO-heavy; more than 12 concurrent writers usually
    # increases wall time and temporary disk pressure.
    return max(1, min(cpu, memory_jobs, 12))


def build_tasks(args: argparse.Namespace, matrix: dict[str, Any]) -> list[TraceTask]:
    baseline = next(
        (profile for profile in matrix.get("profiles", []) if profile.get("id") == "baseline"),
        None,
    )
    if baseline is None:
        raise SystemExit("matrix has no baseline profile")
    effective = dict(matrix.get("common", {}))
    effective.update(baseline.get("gem5", {}))
    seed = int(matrix.get("seed", 0) if args.seed is None else args.seed)
    tasks: list[TraceTask] = []
    for workload in matrix.get("workloads", []):
        name = str(workload["name"])
        if args.workload and not any(
            fnmatch.fnmatchcase(name, pattern) for pattern in args.workload
        ):
            continue
        tasks.append(
            TraceTask(
                workload=name,
                domain=str(workload.get("domain", "unknown")),
                scale=int(workload.get("scale", 1)),
                cores=args.cores,
                seed=seed,
                binary=(args.bin_dir / name).resolve(),
                gem5_config=effective,
                final_dir=(
                    args.out.resolve()
                    / f"seed{seed}"
                    / f"c{args.cores:02d}"
                    / f"W_{name}"
                ),
            )
        )
    if args.max_cases:
        tasks = tasks[: args.max_cases]
    return tasks


def as_uarch_task(task: TraceTask) -> UarchTask:
    return UarchTask(
        profile_id="baseline",
        profile_description="functional trace source",
        workload=task.workload,
        domain=task.domain,
        scale=task.scale,
        cores=task.cores,
        seed=task.seed,
        gem5_config=task.gem5_config,
        fastsim_overrides={},
        binary=task.binary,
        final_dir=task.final_dir,
    )


def trace_command(task: TraceTask, args: argparse.Namespace, out: Path) -> list[str]:
    command = command_for(as_uarch_task(task), args, out)
    command.extend(["--functional-trace-subdir", "tao_trace"])
    return command


def trace_core(path: Path) -> int:
    match = CORE_RE.search(path.name)
    if not match:
        raise ValueError(f"cannot identify core from {path.name}")
    return int(match.group(1) or 0)


def fst_header(path: Path) -> dict[str, int]:
    with path.open("rb") as source:
        header = source.read(FST_HEADER_BYTES)
    if len(header) != FST_HEADER_BYTES or header[:8] != b"FSTRC01\0":
        raise ValueError(f"invalid FST header: {path}")
    return {
        "version": int.from_bytes(header[8:12], byteorder="little", signed=False),
        "record_size": int.from_bytes(
            header[16:20], byteorder="little", signed=False
        ),
        "core_id": int.from_bytes(
            header[20:24], byteorder="little", signed=False
        ),
        "record_count": int.from_bytes(
            header[24:32], byteorder="little", signed=False
        ),
        "feature_flags": int.from_bytes(
            header[32:40], byteorder="little", signed=False
        ),
        "syscall_metadata_offset": int.from_bytes(
            header[40:48], byteorder="little", signed=False
        ),
        "syscall_metadata_count": int.from_bytes(
            header[48:56], byteorder="little", signed=False
        ),
        "syscall_metadata_size": int.from_bytes(
            header[56:64], byteorder="little", signed=False
        ),
        "syscall_abi": int.from_bytes(
            header[64:72], byteorder="little", signed=False
        ),
    }


def fst_record_count(path: Path) -> int:
    return fst_header(path)["record_count"]


def complete_address_space_map(
    path: Path, core_id: int, record_count: int
) -> bool:
    map_path = Path(str(path) + ".asmap")
    if not map_path.is_file():
        return True
    try:
        with map_path.open("rb") as source:
            raw = source.read(ASMAP_HEADER.size)
            if len(raw) != ASMAP_HEADER.size:
                return False
            (
                magic,
                version,
                header_size,
                entry_size,
                map_core,
                source_records,
                entry_count,
                reserved,
            ) = ASMAP_HEADER.unpack(raw)
            if (
                magic != ASMAP_MAGIC
                or version != 1
                or header_size != ASMAP_HEADER.size
                or entry_size != ASMAP_ENTRY.size
                or map_core != core_id
                or source_records != record_count
                or entry_count == 0
                or reserved != 0
                or record_count == 0
                or map_path.stat().st_size
                != ASMAP_HEADER.size + entry_count * ASMAP_ENTRY.size
            ):
                return False
            prior_ordinal = -1
            prior_address_space = 0
            for index in range(entry_count):
                raw = source.read(ASMAP_ENTRY.size)
                if len(raw) != ASMAP_ENTRY.size:
                    return False
                ordinal, address_space = ASMAP_ENTRY.unpack(raw)
                if (
                    address_space == 0
                    or ordinal >= record_count
                    or (index == 0 and ordinal != 0)
                    or ordinal <= prior_ordinal
                    or address_space == prior_address_space
                ):
                    return False
                prior_ordinal = ordinal
                prior_address_space = address_space
        return True
    except OSError:
        return False


def complete_fst(path: Path) -> bool:
    """Return true only for a finalized FST v7, including its sparse table."""
    try:
        header = fst_header(path)
        records = header["record_count"]
        records_end = FST_HEADER_BYTES + records * FST_RECORD_BYTES
        has_syscalls = (
            header["feature_flags"] & FST_SYSCALL_METADATA
        ) != 0
        if has_syscalls:
            metadata_valid = (
                header["syscall_metadata_count"] > 0
                and header["syscall_metadata_offset"] == records_end
                and header["syscall_metadata_size"]
                == FST_SYSCALL_METADATA_BYTES
                and path.stat().st_size
                == records_end
                + header["syscall_metadata_count"]
                * FST_SYSCALL_METADATA_BYTES
            )
        else:
            metadata_valid = (
                header["syscall_metadata_offset"] == 0
                and header["syscall_metadata_count"] == 0
                and header["syscall_metadata_size"] == 0
                and path.stat().st_size == records_end
            )
        virtual_map_valid = (
            not header["feature_flags"] & FST_VIRTUAL_PAGE_TOKENS
            or Path(str(path) + ".vmap").is_file()
        )
        address_space_map_valid = complete_address_space_map(
            path, header["core_id"], records
        )
        return (
            records > 0
            and header["version"] == MINIMUM_FST_VERSION
            and header["record_size"] == FST_RECORD_BYTES
            and header["syscall_abi"] == 1
            and (
                header["feature_flags"] & FST_DESTINATION_CLASS_COUNTS
            )
            != 0
            and metadata_valid
            and virtual_map_valid
            and address_space_map_valid
        )
    except (OSError, ValueError):
        return False


def complete_trace_set(path: Path, cores: int) -> bool:
    complete = path / "complete.json"
    trace_json = path / "trace.json"
    syscall_sidecar = path / "syscalls.jsonl"
    if (
        not complete.is_file()
        or not trace_json.is_file()
        or not syscall_sidecar.is_file()
    ):
        return False
    try:
        metadata = json.loads(trace_json.read_text(encoding="utf-8"))
        fst_paths = [path / f"core{core}.fst" for core in range(cores)]
        if not all(complete_fst(fst) for fst in fst_paths):
            return False
        embedded_count = sum(
            fst_header(fst)["syscall_metadata_count"] for fst in fst_paths
        )
        address_space_map_hashes = {
            str(core): file_sha256(Path(str(fst) + ".asmap"))
            for core, fst in enumerate(fst_paths)
            if Path(str(fst) + ".asmap").is_file()
        }
        return (
            metadata.get("schema") == "fastsim-functional-trace-set-v3"
            and metadata.get("syscall_metadata_embedded") is True
            and metadata.get("syscall_sidecar_schema")
            == "fastsim-functional-syscall-v2"
            and int(metadata.get("syscall_events", -1)) == embedded_count
            and metadata.get("syscall_sidecar_sha256")
            == file_sha256(syscall_sidecar)
            and metadata.get("address_space_map_sha256", {})
            == address_space_map_hashes
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def raw_has_destination_class_counts(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.startswith("{"):
                    continue
                return "destination_class_counts" in json.loads(line)
    except (OSError, json.JSONDecodeError):
        return False
    return False


def resumable_staging(task: TraceTask) -> Path | None:
    candidates = sorted(
        task.final_dir.parent.glob(f".{task.final_dir.name}.running-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for staging in candidates:
        trace_dir = staging / "tao_trace"
        raw_paths = {trace_core(path): path for path in trace_dir.glob("*.records.micro.jsonl")}
        log_path = staging / "gem5.log"
        stats_path = staging / "stats.txt"
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            stats_ready = stats_path.stat().st_size > 0
        except OSError:
            continue
        if (
            sorted(raw_paths) == list(range(task.cores))
            and all(path.stat().st_size > 0 for path in raw_paths.values())
            and all(
                raw_has_destination_class_counts(path)
                for path in raw_paths.values()
            )
            and stats_ready
            and f"WORKEND {task.cores}/{task.cores} finish" in log_text
        ):
            return staging
    return None


def convert_roi_boundaries(source: Path, output: Path) -> int:
    if not source.is_file():
        return 0
    count = 0
    with source.open(encoding="utf-8") as rows, output.open(
        "w", encoding="utf-8"
    ) as target:
        for line in rows:
            if not line.startswith("{"):
                continue
            raw = json.loads(line)
            event = {
                "schema": "fastsim-functional-roi-v1",
                "event": raw.get("event"),
                "core_id": int(raw.get("core_id", 0)),
                "thread_id": int(raw.get("threadid", 0)),
                "work_id": int(raw.get("workid", 0)),
            }
            target.write(json.dumps(event, sort_keys=True) + "\n")
            count += 1
    return count


def merge_syscall_sidecars(
    inputs: dict[int, Path], headers: dict[int, dict[str, int]], output: Path
) -> tuple[int, dict[int, int]]:
    """Validate v2 rows against embedded table cardinality, then merge them."""
    total = 0
    counts: dict[int, int] = {}
    with output.open("w", encoding="utf-8") as target:
        for core in sorted(inputs):
            count = 0
            with inputs[core].open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (
                        row.get("schema") != "fastsim-functional-syscall-v2"
                        or row.get("event") != "syscall"
                        or int(row.get("core_id", -1)) != core
                        or int(row.get("syscall_ordinal", -1)) != count
                        or int(row.get("record_ordinal", -1)) < 0
                        or int(row.get("record_ordinal", -1))
                        >= headers[core]["record_count"]
                    ):
                        raise ValueError(
                            f"invalid syscall v2 sidecar row core={core} ordinal={count}"
                        )
                    target.write(line if line.endswith("\n") else line + "\n")
                    count += 1
            if count != headers[core]["syscall_metadata_count"]:
                raise ValueError(
                    f"syscall metadata count mismatch core={core}: "
                    f"FST={headers[core]['syscall_metadata_count']} JSONL={count}"
                )
            counts[core] = count
            total += count
    return total, counts


def run_task(task: TraceTask, args: argparse.Namespace) -> tuple[str, str, str | None]:
    if complete_trace_set(task.final_dir, task.cores):
        return task.label, "skipped", None
    task.final_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    staging = resumable_staging(task) if args.resume_orphans else None
    resumed = staging is not None
    if staging is None:
        staging = task.final_dir.parent / f".{task.final_dir.name}.running-{token}"
        staging.mkdir()
    command = trace_command(task, args, staging)
    started = time.monotonic()
    error: str | None = None
    environment = os.environ.copy()
    libraries = [
        "/root/.local/share/uv/python/cpython-3.11.14-linux-x86_64-gnu/lib",
        "/data00/yinhaolang/LLMSim/data/_gem5libs",
        "/opt/gcc-11.5.0/lib64",
    ]
    if environment.get("LD_LIBRARY_PATH"):
        libraries.append(environment["LD_LIBRARY_PATH"])
    environment["LD_LIBRARY_PATH"] = ":".join(libraries)

    if resumed:
        print(f"[trace:resume] {task.label} staging={staging.name}", flush=True)
    else:
        print(f"[trace:start] {task.label}", flush=True)
        with (staging / "gem5.log").open("wb") as log:
            try:
                result = subprocess.run(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    timeout=args.timeout,
                    check=False,
                    start_new_session=True,
                )
                if result.returncode != 0:
                    error = f"gem5 exit={result.returncode}"
            except subprocess.TimeoutExpired:
                error = "gem5 timeout"

    wall = time.monotonic() - started
    try:
        if error:
            raise ValueError(error)
        validation = validate_effective_config(staging / "config.ini", as_uarch_task(task))
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        trace_dir = staging / "tao_trace"
        raw_paths = {trace_core(path): path for path in trace_dir.glob("*.records.micro.jsonl")}
        if sorted(raw_paths) != list(range(task.cores)):
            raise ValueError(
                f"expected trace cores 0..{task.cores - 1}, found {sorted(raw_paths)}"
            )

        functional_dir = staging / "functional"
        functional_dir.mkdir(exist_ok=True)
        roi_count = convert_roi_boundaries(
            trace_dir / "roi_boundaries.jsonl", functional_dir / "roi.jsonl"
        )
        manifest_lines = []
        fst_hashes: dict[int, str] = {}
        counts: dict[int, int] = {}
        fst_versions: dict[int, int] = {}
        fst_feature_flags: dict[int, int] = {}
        address_space_map_hashes: dict[int, str] = {}
        fst_headers: dict[int, dict[str, int]] = {}
        syscall_sidecars: dict[int, Path] = {}
        for core, raw in sorted(raw_paths.items()):
            fst = functional_dir / f"core{core}.fst"
            syscall_sidecar = functional_dir / f"core{core}.syscalls.jsonl"
            if not complete_fst(fst) or not syscall_sidecar.is_file():
                recovering = functional_dir / f"core{core}.fst.recovering"
                syscall_recovering = (
                    functional_dir / f"core{core}.syscalls.jsonl.recovering"
                )
                converted = subprocess.run(
                    [
                        str(args.fastsim.resolve()),
                        "convert-gem5",
                        "--input",
                        str(raw),
                        "--output",
                        str(recovering),
                        "--core",
                        str(core),
                        "--syscall-output",
                        str(syscall_recovering),
                        "--syscall-abi",
                        "linux-x86_64",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
                if converted.returncode != 0:
                    raise ValueError(
                        f"FST conversion failed core={core}: {converted.stdout.strip()}"
                    )
                recovering.replace(fst)
                for suffix in (".vmap", ".imap", ".asmap"):
                    recovering_sidecar = Path(str(recovering) + suffix)
                    if recovering_sidecar.is_file():
                        recovering_sidecar.replace(Path(str(fst) + suffix))
                syscall_recovering.replace(syscall_sidecar)
            if not complete_fst(fst):
                raise ValueError(
                    f"core {core} did not produce an FST v7 stream with "
                    "destination_class_counts and valid syscall metadata"
                )
            if not syscall_sidecar.is_file():
                raise ValueError(f"core {core} has no syscall v2 sidecar")
            counts[core] = fst_record_count(fst)
            header = fst_header(fst)
            fst_headers[core] = header
            fst_versions[core] = header["version"]
            fst_feature_flags[core] = header["feature_flags"]
            fst_hashes[core] = file_sha256(fst)
            address_space_map = Path(str(fst) + ".asmap")
            if address_space_map.is_file():
                address_space_map_hashes[core] = file_sha256(
                    address_space_map
                )
            syscall_sidecars[core] = syscall_sidecar
            final_fst = task.final_dir / fst.name
            manifest_lines.append(f"{core} fastsim-binary {final_fst}\n")
        syscall_count, syscall_counts = merge_syscall_sidecars(
            syscall_sidecars, fst_headers, functional_dir / "syscalls.jsonl"
        )
        (functional_dir / "manifest.txt").write_text(
            "".join(manifest_lines), encoding="utf-8"
        )

        metadata = {
            "schema": "fastsim-functional-trace-set-v3",
            "functional_only": True,
            "timing_labels_emitted": False,
            "cache_oracle_stream_emitted": False,
            "uarch_source": "baseline",
            "workload": task.workload,
            "domain": task.domain,
            "cores": task.cores,
            "seed": task.seed,
            "scale": task.scale,
            "binary": str(task.binary),
            "binary_sha256": file_sha256(task.binary),
            "records_per_core": counts,
            "fst_versions": fst_versions,
            "fst_feature_flags": fst_feature_flags,
            "destination_class_counts": True,
            "syscall_abi": "linux-x86_64",
            "syscall_metadata_embedded": True,
            "syscall_metadata_entry_bytes": FST_SYSCALL_METADATA_BYTES,
            "syscall_sidecar_schema": "fastsim-functional-syscall-v2",
            "syscall_events": syscall_count,
            "syscall_events_per_core": syscall_counts,
            "syscall_sidecar_sha256": file_sha256(
                functional_dir / "syscalls.jsonl"
            ),
            "roi_events": roi_count,
            "fst_sha256": fst_hashes,
            "address_space_map_sha256": address_space_map_hashes,
            "wall_time_seconds": wall,
            "resumed_postprocessing": resumed,
        }
        (functional_dir / "trace.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        shutil.copy2(staging / "config.ini", functional_dir / "config.ini")
        shutil.copy2(staging / "requested_uarch.json", functional_dir / "requested_uarch.json")
        shutil.copy2(staging / "gem5.log", functional_dir / "gem5.log")
        (functional_dir / "complete.json").write_text(
            json.dumps(
                {"status": "complete", "wall_time_seconds": wall},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        # The staging JSONL contains implementation-specific oracle columns.
        # Keep only canonical FST plus sparse functional sidecars in the final set.
        shutil.rmtree(trace_dir)
        for path in list(staging.iterdir()):
            if path != functional_dir:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        obsolete_dir = None
        if task.final_dir.exists():
            obsolete_dir = task.final_dir.parent / (
                f".{task.final_dir.name}.obsolete-pre-fst-v7-{token}"
            )
            task.final_dir.rename(obsolete_dir)
        try:
            functional_dir.rename(task.final_dir)
        except OSError:
            if obsolete_dir is not None and obsolete_dir.exists():
                obsolete_dir.rename(task.final_dir)
            raise
        if obsolete_dir is not None:
            shutil.rmtree(obsolete_dir)
        staging.rmdir()
        print(f"[trace:done ] {task.label} {wall:.1f}s", flush=True)
        return task.label, "completed", None
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        error = str(exc)
        # A previously orphaned converter can finish and atomically publish
        # the same task while a recovery worker is still post-processing it.
        # The published complete trace is authoritative; do not turn that
        # benign race into a failed run or try to write into vanished staging.
        if (task.final_dir / "complete.json").is_file():
            print(f"[trace:raced] {task.label} already published", flush=True)
            return task.label, "skipped", None
        (staging / "failed.json").write_text(
            json.dumps({"status": "failed", "error": error}, indent=2) + "\n",
            encoding="utf-8",
        )
        failed = task.final_dir.parent / (
            f"{task.final_dir.name}.failed-{time.strftime('%Y%m%d-%H%M%S')}-{token}"
        )
        staging.rename(failed)
        print(f"[trace:fail ] {task.label}: {error}", flush=True)
        return task.label, "failed", error


def main() -> int:
    args = parse_args()
    matrix = load_json(args.matrix.resolve())
    tasks = build_tasks(args, matrix)
    if not tasks:
        raise SystemExit("selection produced no trace tasks")
    if args.list:
        for task in tasks:
            print(f"{task.workload}: scale={task.scale} domain={task.domain}")
        return 0
    missing = [
        path
        for path in (args.gem5, args.gem5_config, args.fastsim)
        if not path.is_file()
    ]
    missing.extend(task.binary for task in tasks if not task.binary.is_file())
    if missing:
        raise SystemExit("missing required files:\n" + "\n".join(map(str, missing)))

    jobs = min(args.jobs or automatic_jobs(), len(tasks))
    print(
        f"functional traces={len(tasks)} cores={args.cores} jobs={jobs} "
        f"out={args.out.resolve()}",
        flush=True,
    )
    if args.dry_run:
        for task in tasks:
            print(task.label)
            print("  " + " ".join(trace_command(task, args, task.final_dir)))
        return 0

    failures = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [executor.submit(run_task, task, args) for task in tasks]
        for future in as_completed(futures):
            label, status, error = future.result()
            if status == "failed":
                failures.append({"label": label, "error": error})
    args.out.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": "fastsim-functional-trace-summary-v1",
        "tasks": len(tasks),
        "failures": failures,
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
