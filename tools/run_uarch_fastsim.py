#!/usr/bin/env python3
"""Parallel, resumable FastSim replay for the uarch-generalization dataset."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CompatibleBooleanOptionalAction(argparse.Action):
    """Python 3.8-compatible form of argparse.BooleanOptionalAction."""

    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        default: bool | None = None,
        **kwargs: Any,
    ) -> None:
        expanded: list[str] = []
        for option in option_strings:
            expanded.append(option)
            if option.startswith("--"):
                expanded.append(f"--no-{option[2:]}")
        super().__init__(
            option_strings=expanded,
            dest=dest,
            nargs=0,
            default=default,
            **kwargs,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser, values
        setattr(
            namespace,
            self.dest,
            option_string is not None and not option_string.startswith("--no-"),
        )


BOOLEAN_OPTIONAL_ACTION = getattr(
    argparse, "BooleanOptionalAction", CompatibleBooleanOptionalAction
)


@dataclass(frozen=True)
class Task:
    uarch: str
    description: str
    workload: str
    domain: str
    cores: int
    seed: int
    overrides: dict[str, Any]
    trace_manifest: Path
    trace_meta: Path
    label_metrics: Path
    final_dir: Path
    experiment_id: str | None = None

    @property
    def name(self) -> str:
        case = f"{self.uarch}/c{self.cores:02d}/W_{self.workload}"
        return f"{self.experiment_id}/{case}" if self.experiment_id else case


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_override(value: str) -> tuple[str, Any]:
    key, separator, raw = value.partition("=")
    key = key.strip()
    raw = raw.strip()
    if not separator or not key or not raw:
        raise argparse.ArgumentTypeError(
            "config override must be KEY=VALUE"
        )
    if raw.lower() == "true":
        parsed: Any = True
    elif raw.lower() == "false":
        parsed = False
    else:
        try:
            parsed = int(raw, 0)
        except ValueError:
            try:
                parsed = float(raw)
            except ValueError:
                parsed = raw
    return key, parsed


def selected(name: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def size_bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    suffixes = {
        "kib": 1 << 10,
        "mib": 1 << 20,
        "gib": 1 << 30,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
    }
    for suffix, multiplier in suffixes.items():
        if text.endswith(suffix):
            return int(text[: -len(suffix)]) * multiplier
    return int(text)


def nested(source: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = source
    for key in path:
        current = current[key]
    return current


EFFECTIVE_PATHS: dict[str, tuple[str, ...]] = {
    "sim.interval_max_cycles": ("interval_max_cycles",),
    "sim.functional_warmup_interval_max_cycles": (
        "functional_warmup_interval_max_cycles",
    ),
    "sim.cpi_attribution": ("cpi_attribution",),
    "sim.interval_causal_timing": ("interval_causal_timing",),
    "core.fetch_width": ("fetch_width",),
    "core.decode_width": ("decode_width",),
    "core.rename_width": ("rename_width",),
    "core.dispatch_width": ("dispatch_width",),
    "core.issue_width": ("issue_width",),
    "core.writeback_width": ("writeback_width",),
    "core.commit_width": ("commit_width",),
    "core.rob_entries": ("rob_entries",),
    "core.iq_entries": ("iq_entries",),
    "core.lq_entries": ("lq_entries",),
    "core.sq_entries": ("sq_entries",),
    "core.committed_pipeline_audit": ("committed_pipeline_audit",),
    "core.fu_gap_aware_schedule": ("fu_gap_aware_schedule",),
    "core.response_materialized_uop_fast_kernel": (
        "response_materialized_uop_fast_kernel",
    ),
    "core.response_sparse_resource_repair": (
        "response_sparse_resource_repair",
    ),
    "core.response_frontier_audit_stride_uops": (
        "response_frontier_audit_stride_uops",
    ),
    "core.response_frontier_audit_begin_sequence": (
        "response_frontier_audit_begin_sequence",
    ),
    "core.response_frontier_audit_end_sequence": (
        "response_frontier_audit_end_sequence",
    ),
    "core.response_frontier_audit_core": (
        "response_frontier_audit_core",
    ),
    "core.response_paired_frontier": ("response_paired_frontier",),
    "core.store_post_commit_request": ("store_post_commit_request",),
    "cache.l1i.speculative_path_state": (
        "l1i_speculative_path_state",
    ),
    "dtlb.speculative_path_state": ("dtlb", "speculative_path_state"),
    "dtlb.entries": ("dtlb", "entries"),
    "cache.l1d.size": ("l1d", "size_bytes"),
    "cache.l1d.associativity": ("l1d", "associativity"),
    "cache.l2.size": ("l2", "size_bytes"),
    "cache.l2.associativity": ("l2", "associativity"),
    "cache.llc.size": ("llc", "size_bytes"),
    "cache.llc.associativity": ("llc", "associativity"),
    "uncore.cha_count": ("cha_count",),
    "dram.channels": ("dram", "channels"),
    "dram.size": ("dram", "size_bytes"),
    "dram.t_ras": ("dram", "t_ras"),
    "dram.t_rtp": ("dram", "t_rtp"),
    "dram.t_rrd": ("dram", "t_rrd"),
    "dram.t_rrd_l": ("dram", "t_rrd_l"),
    "dram.t_xaw": ("dram", "t_xaw"),
    "dram.activation_limit": ("dram", "activation_limit"),
    "dram.t_ccd_l": ("dram", "t_ccd_l"),
    "dram.t_cs": ("dram", "t_cs"),
}
SIZE_KEYS = {"cache.l1d.size", "cache.l2.size", "cache.llc.size", "dram.size"}


def validate_effective(task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    configuration = stats["configuration"]
    errors: list[str] = []
    checked: dict[str, dict[str, Any]] = {}
    for key, requested in task.overrides.items():
        if key not in EFFECTIVE_PATHS:
            errors.append(f"no effective-output mapping for {key}")
            continue
        expected = size_bytes(requested) if key in SIZE_KEYS else requested
        actual = nested(configuration, EFFECTIVE_PATHS[key])
        checked[key] = {"requested": requested, "expected": expected, "actual": actual}
        if actual != expected:
            errors.append(f"{key}: requested={expected!r} effective={actual!r}")
    if int(configuration["cores"]) != task.cores:
        errors.append(
            f"cores: requested={task.cores} effective={configuration['cores']}"
        )
    trace = load(task.trace_meta)
    trace_uops = sum(int(value) for value in trace["records_per_core"].values())
    trace_user_uops = sum(
        int(value) for value in trace.get("user_records_per_core", {}).values()
    )
    replay_uops = int(stats["totals"]["retired_uops"])
    if replay_uops != trace_uops:
        errors.append(f"retired_uops: trace={trace_uops} replay={replay_uops}")
    scope = stats.get("scope_metrics", {})
    replay_user_uops = int(scope.get("user_trace_uops", -1))
    if replay_user_uops != trace_user_uops:
        errors.append(
            f"user_trace_uops: trace={trace_user_uops} replay={replay_user_uops}"
        )
    trace_scope = trace.get("trace_scope")
    measurement_scope = stats.get(
        "measurement_scope", configuration.get("measurement_scope")
    )
    if measurement_scope != trace_scope:
        errors.append(
            f"measurement_scope: trace={trace_scope!r} replay={measurement_scope!r}"
        )
    native = bool(configuration.get("native_kernel_trace"))
    if trace_scope == "user-plus-kernel" and not native:
        errors.append("native user-plus-kernel trace replay disabled native_kernel_trace")
    feature_flags = trace.get("fst_feature_flags", {})
    if trace_scope == "user-plus-kernel" and (
        set(feature_flags) != {str(core) for core in range(task.cores)}
        or any((int(value) & (1 << 4)) == 0 for value in feature_flags.values())
    ):
        errors.append("native trace is missing per-core FST privilege feature bits")
    return {
        "ok": not errors,
        "checked": checked,
        "trace_uops": trace_uops,
        "trace_user_uops": trace_user_uops,
        "replay_uops": replay_uops,
        "replay_user_uops": replay_user_uops,
        "trace_scope": trace_scope,
        "measurement_scope": measurement_scope,
        "native_kernel_trace": native,
        "errors": errors,
    }


def build_tasks(args: argparse.Namespace) -> list[Task]:
    matrix = load(args.matrix)
    root = args.root.resolve()
    out = args.out.resolve()
    profiles = {profile["id"]: profile for profile in matrix["profiles"]}
    domains = {workload["name"]: workload["domain"] for workload in matrix["workloads"]}
    tasks: list[Task] = []
    for metrics_path in sorted(root.glob("labels/*/c*/W_*/metrics.json")):
        metrics = load(metrics_path)
        uarch = str(metrics["uarch"])
        workload = str(metrics["workload"])
        cores = int(metrics["cores"])
        seed = int(metrics["seed"])
        if not selected(uarch, args.uarch) or not selected(workload, args.workload):
            continue
        profile = profiles[uarch]
        overrides = dict(profile.get("fastsim", {}))
        overrides.update(args.config_overrides)
        trace_dir = root / "traces" / f"seed{seed}" / f"c{cores:02d}" / f"W_{workload}"
        trace_manifest = Path(metrics.get("trace_manifest", trace_dir / "manifest.txt"))
        trace_meta = Path(metrics.get("trace_metadata", trace_dir / "trace.json"))
        tasks.append(
            Task(
                uarch=uarch,
                description=str(profile["description"]),
                workload=workload,
                domain=str(metrics.get("domain", domains[workload])),
                cores=cores,
                seed=seed,
                overrides=overrides,
                trace_manifest=trace_manifest,
                trace_meta=trace_meta,
                label_metrics=metrics_path,
                final_dir=out / uarch / f"c{cores:02d}" / f"W_{workload}",
                experiment_id=args.experiment_id,
            )
        )
    if args.max_cases:
        tasks = tasks[: args.max_cases]
    return tasks


def validate_trace_contract(task: Task, require_destination_classes: bool) -> None:
    metadata = load(task.trace_meta)
    if not require_destination_classes:
        return
    versions = metadata.get("fst_versions", {})
    feature_flags = metadata.get("fst_feature_flags", {})
    destination_classes = bool(metadata.get("destination_class_counts", False))
    expected_cores = {str(core) for core in range(task.cores)}
    if (
        not destination_classes
        or set(versions) != expected_cores
        or set(feature_flags) != expected_cores
        or any(int(versions[core]) < 6 for core in expected_cores)
        or any((int(feature_flags[core]) & (1 << 2)) == 0 for core in expected_cores)
    ):
        raise ValueError(
            f"{task.name}: --rename-free-list requires an FST trace set "
            "with destination_class_counts"
        )


def make_config(base: Path, overrides: dict[str, Any], output: Path) -> None:
    source_lines = base.read_text(encoding="utf-8").rstrip().splitlines()
    # The effective config lives in a per-case staging directory.  Preserve
    # overlay configs by resolving their include relative to the source config,
    # not relative to that transient staging directory.
    materialized: list[str] = []
    for line in source_lines:
        if line.lstrip().startswith("config.include") and "=" in line:
            prefix, _, value = line.partition("=")
            include = Path(value.strip())
            if not include.is_absolute():
                include = (base.parent / include).resolve()
            line = f"{prefix}= {include}"
        materialized.append(line)
    lines = [*materialized, "", "# uarch-generalization overrides"]
    for key, value in sorted(overrides.items()):
        lines.append(f"{key} = {scalar(value)}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_task(task: Task, args: argparse.Namespace) -> tuple[str, str, str | None]:
    if (task.final_dir / "complete.json").is_file() and not args.force:
        return task.name, "skipped", None
    task.final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = task.final_dir.parent / f".{task.final_dir.name}.running-{uuid.uuid4().hex[:10]}"
    staging.mkdir()
    config = staging / "effective.cfg"
    stats_path = staging / "fastsim-stats.json"
    make_config(args.config.resolve(), task.overrides, config)
    command = [
        str(args.fastsim.resolve()),
        "simulate",
        "--config",
        str(config),
        "--manifest",
        str(task.trace_manifest.resolve()),
        "--cores",
        str(task.cores),
        "--output",
        str(stats_path),
    ]
    # The selected profile owns the measurement scope by default.  In
    # particular, do not silently turn a native profile back into user-only.
    if args.measurement_scope is not None:
        command.extend(["--measurement-scope", args.measurement_scope])
    if args.native_kernel_trace is not None:
        command.extend(
            [
                "--native-kernel-trace",
                "true" if args.native_kernel_trace else "false",
            ]
        )
    if args.interval_corrected_suffix_carry is not None:
        command.extend(
            [
                "--interval-corrected-suffix-carry",
                "true" if args.interval_corrected_suffix_carry else "false",
            ]
        )
    if args.interval_causal_timing is not None:
        command.extend(
            [
                "--interval-causal-timing",
                "true" if args.interval_causal_timing else "false",
            ]
        )
    if args.interval_response_retime is not None:
        command.extend(
            [
                "--interval-response-retime",
                "true" if args.interval_response_retime else "false",
            ]
        )
    if args.interval_rob_head_suffix_replay is not None:
        command.extend(
            [
                "--interval-rob-head-suffix-replay",
                "true" if args.interval_rob_head_suffix_replay else "false",
            ]
        )
    if args.llc_fill_response_latency is not None:
        command.extend(
            [
                "--llc-fill-response-latency",
                str(args.llc_fill_response_latency),
            ]
        )
    if args.committed_pipeline_audit is not None:
        command.extend(
            [
                "--committed-pipeline-audit",
                "true" if args.committed_pipeline_audit else "false",
            ]
        )
    if args.fu_gap_aware_schedule is not None:
        command.extend(
            [
                "--fu-gap-aware-schedule",
                "true" if args.fu_gap_aware_schedule else "false",
            ]
        )
    if args.rename_free_list is not None:
        command.extend(
            [
                "--rename-free-list",
                "true" if args.rename_free_list else "false",
            ]
        )
    if args.response_rename_feedback is not None:
        command.extend(
            [
                "--response-rename-feedback",
                "true" if args.response_rename_feedback else "false",
            ]
        )
    if args.branch_shadow_rob is not None:
        command.extend(
            [
                "--branch-shadow-rob",
                "true" if args.branch_shadow_rob else "false",
            ]
        )
    started = time.monotonic()
    error: str | None = None
    print(f"[start] {task.name}", flush=True)
    with (staging / "fastsim.log").open("wb") as log:
        try:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
            if result.returncode != 0:
                error = f"FastSim exit={result.returncode}"
        except subprocess.TimeoutExpired:
            error = "FastSim timeout"
    wall = time.monotonic() - started
    try:
        if error:
            raise ValueError(error)
        stats = load(stats_path)
        validation = validate_effective(task, stats)
        (staging / "config-validation.json").write_text(
            json.dumps(validation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        effective_configuration = staging / "effective-configuration.json"
        effective_configuration.write_text(
            json.dumps(stats["configuration"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        metadata = {
            "schema": "fastsim-uarch-replay-v1",
            "uarch": task.uarch,
            "uarch_description": task.description,
            "workload": task.workload,
            "domain": task.domain,
            "cores": task.cores,
            "seed": task.seed,
            "experiment_id": task.experiment_id,
            "overrides": task.overrides,
            "trace_manifest": str(task.trace_manifest.resolve()),
            "label_metrics": str(task.label_metrics.resolve()),
            "base_config": str(args.config.resolve()),
            "hashes": {
                "fastsim_sha256": sha256(args.fastsim.resolve()),
                "base_config_sha256": sha256(args.config.resolve()),
                "effective_config_sha256": sha256(config),
                "effective_configuration_sha256": sha256(
                    effective_configuration
                ),
                "trace_manifest_sha256": sha256(task.trace_manifest.resolve()),
                "trace_metadata_sha256": sha256(task.trace_meta.resolve()),
                "label_metrics_sha256": sha256(task.label_metrics.resolve()),
            },
            "command": command,
            "wall_time_seconds": wall,
        }
        (staging / "run.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / "complete.json").write_text(
            json.dumps({"status": "complete", "wall_time_seconds": wall}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        if task.final_dir.exists():
            shutil.rmtree(task.final_dir)
        staging.rename(task.final_dir)
        print(f"[done ] {task.name} {wall:.2f}s", flush=True)
        return task.name, "completed", None
    except Exception as failure:  # noqa: BLE001 - preserve failed staging
        message = str(failure)
        (staging / "failed.json").write_text(
            json.dumps({"status": "failed", "error": message}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[fail ] {task.name}: {message}", flush=True)
        return task.name, "failed", message


def write_summary(tasks: list[Task], out: Path) -> None:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        directory = task.final_dir
        if not (directory / "complete.json").is_file():
            continue
        stats = load(directory / "fastsim-stats.json")
        total = stats["totals"]
        scope = stats["scope_metrics"]
        cycles = sum(int(core["cycles"]) for core in stats["cores"])
        uops = int(scope["user_trace_uops"])
        throughput = scope.get("throughput", {})
        rows.append(
            {
                "uarch": task.uarch,
                "workload": task.workload,
                "domain": task.domain,
                "cores": task.cores,
                "uop_cpi": float(scope["cycles_per_user_uop"]),
                "sum_core_cycles": cycles,
                "retired_uops": uops,
                "trace_retired_uops": int(total["retired_uops"]),
                "uops_per_second": float(
                    throughput.get(
                        "user_uops_per_second",
                        stats["throughput"]["uops_per_second"],
                    )
                ),
                "wall_time_seconds": load(directory / "run.json")["wall_time_seconds"],
                "stats": str((directory / "fastsim-stats.json").resolve()),
            }
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = list(rows[0]) if rows else ["uarch", "workload", "cores"]
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("tmp/uarch-c4-first-batch"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--matrix", type=Path, default=Path("configs/uarch-first-batch.json"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/gem5-v28_2-fs-native-kernel.cfg"),
    )
    parser.add_argument(
        "--measurement-scope",
        choices=("user", "user-plus-kernel"),
        default=None,
        help="override the profile measurement scope (default: use the profile)",
    )
    parser.add_argument(
        "--native-kernel-trace",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help="override whether the input contains native kernel records",
    )
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--uarch", action="append", default=[])
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument(
        "--config-override",
        action="append",
        type=config_override,
        default=[],
        metavar="KEY=VALUE",
        help=(
            "append a validated config override to every selected replay; "
            "repeat for multiple keys"
        ),
    )
    parser.add_argument(
        "--experiment-id",
        help="optional provenance label recorded in every run.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="rerun completed cases")
    parser.add_argument(
        "--interval-corrected-suffix-carry",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help="override the experimental corrected epoch-suffix carry",
    )
    parser.add_argument(
        "--interval-causal-timing",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help="override the legacy timing-only corrected-arrival repair",
    )
    parser.add_argument(
        "--interval-response-retime",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help="override the one-pass response/shared-queue retime",
    )
    parser.add_argument(
        "--interval-rob-head-suffix-replay",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help="override ROB-head-local response suffix timing replay",
    )
    parser.add_argument(
        "--llc-fill-response-latency",
        type=int,
        default=None,
        help="override DRAM-completion to Ruby LLC-fill visibility cycles",
    )
    parser.add_argument(
        "--committed-pipeline-audit",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help=(
            "enable audit-only committed rename/free-list and mutually "
            "exclusive lower-bound dispatch-gate ledgers"
        ),
    )
    parser.add_argument(
        "--fu-gap-aware-schedule",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help=(
            "enable the experimental producer-side FU capacity calendar "
            "that can fill gaps before future reservations"
        ),
    )
    parser.add_argument(
        "--rename-free-list",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help=(
            "enable the experimental committed-path per-class physical "
            "register free list (requires FST destination class counts)"
        ),
    )
    parser.add_argument(
        "--response-rename-feedback",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help=(
            "release physical-register mappings only at response-corrected "
            "ordered retirement (alternative to --rename-free-list)"
        ),
    )
    parser.add_argument(
        "--branch-shadow-rob",
        action=BOOLEAN_OPTIONAL_ACTION,
        default=None,
        help=(
            "enable anonymous wrong-path ROB occupancy derived only from "
            "functional branch misses and target pipeline geometry"
        ),
    )
    args = parser.parse_args()
    args.config_overrides = {}
    for key, value in args.config_override:
        if key in args.config_overrides:
            parser.error(f"duplicate --config-override key: {key}")
        args.config_overrides[key] = value
    args.root = args.root.resolve()
    args.out = (args.out or (args.root / "fastsim")).resolve()
    for required in (args.matrix, args.config, args.fastsim, args.root / "labels", args.root / "traces"):
        if not required.exists():
            parser.error(f"missing required path: {required}")
    tasks = build_tasks(args)
    require_destination_classes = args.rename_free_list is True
    require_destination_classes = (
        require_destination_classes or
        args.response_rename_feedback is True
    )
    if not require_destination_classes:
        require_destination_classes = any(
            task.overrides.get("core.rename_free_list") is True or
            task.overrides.get("core.response_rename_feedback") is True
            for task in tasks
        )
    for task in tasks:
        validate_trace_contract(task, require_destination_classes)
    cpu_count = os.cpu_count() or 1
    jobs = args.jobs or max(1, min(32, cpu_count // 8))
    print(f"FastSim replays={len(tasks)} jobs={jobs} out={args.out}")
    if args.dry_run:
        for task in tasks:
            print(f"{task.name} overrides={json.dumps(task.overrides, sort_keys=True)}")
        return 0
    statuses = {"completed": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(run_task, task, args): task for task in tasks}
        for future in as_completed(futures):
            name, status, error = future.result()
            statuses[status] += 1
            if error:
                failures.append(f"{name}: {error}")
    write_summary(tasks, args.out)
    print(
        "finished " + " ".join(f"{key}={value}" for key, value in statuses.items())
    )
    if failures:
        for failure in failures[:20]:
            print(f"[error] {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
