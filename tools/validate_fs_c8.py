#!/usr/bin/env python3
"""Validate FastSim against matched gem5 FS O3/Ruby traces and labels.

The current FS capture contains an O3 warmup prefix because its Tao wrapper
used ``require_roi=False``.  This tool locates the global WORKBEGIN boundary
from the recorded per-core committed-instruction baselines and emits a
read-only ``fastsim-binary-warmup-slice`` manifest. FastSim first replays each
functional prefix, pauses all producers at a common barrier, resets only the
measurement counters/time origin, and then resumes the bounded ROI while
retaining its reconstructed cache, predictor, TLB, OoO, and memory state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


WORKLOADS = (
    "706.stockfish_r",
    "710.omnetpp_r",
    "777.zstd_r",
    "782.lbm_r",
    "811.tealeaf_s",
    "854.graph500_s",
)
BASELINES_RE = re.compile(r"baselines=\[([^]]+)\]")
FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
PMU_FIELDS = (
    "l1d_accesses",
    "l1d_misses",
    "private_l2_accesses",
    "private_l2_misses",
    "cha_llc_lookups",
    "llc_tag_misses",
    "branch_direction_misses",
    "dtlb_accesses",
    "dtlb_misses",
    "dram_reads",
    "dram_writes",
)


def parse_args() -> argparse.Namespace:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cores",
        type=int,
        default=8,
        help="Matched gem5/FST core count (default: 8).",
    )
    parser.add_argument(
        "--fs-root",
        type=Path,
        default=None,
        help="FS result root; defaults to the matching <cores>c directory.",
    )
    parser.add_argument(
        "--tcsim-root", type=Path, default=Path("/data00/yinhaolang/TCSim")
    )
    parser.add_argument("--fastsim", type=Path, default=project / "build/fastsim")
    parser.add_argument(
        "--config",
        type=Path,
        default=project / "configs/gem5-v28_1-fs-user.cfg",
    )
    parser.add_argument(
        "--output", type=Path, default=None
    )
    parser.add_argument("--workloads", nargs="+", default=list(WORKLOADS))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        default=12,
        help="Accepted FastSim FS timing-walk service in cycles (default: 12).",
    )
    write_queue_group = parser.add_mutually_exclusive_group()
    write_queue_group.add_argument(
        "--dram-separate-write-queue",
        dest="dram_separate_write_queue",
        action="store_true",
        help=(
            "Override the config's per-channel dirty-writeback queue; "
            "the production profile enables it by default."
        ),
    )
    write_queue_group.add_argument(
        "--no-dram-separate-write-queue",
        dest="dram_separate_write_queue",
        action="store_false",
        help=(
            "Disable the dirty-writeback queue for legacy differential "
            "validation."
        ),
    )
    parser.set_defaults(dram_separate_write_queue=None)
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument(
        "--cold-slice",
        action="store_true",
        help=(
            "Skip the pre-WORKBEGIN prefix instead of replaying it through "
            "the two-phase functional warmup barrier."
        ),
    )
    parser.add_argument(
        "--skip-trace-hashes",
        action="store_true",
        help="Skip the expensive per-trace SHA-256 identity check.",
    )
    args = parser.parse_args()
    if args.cores <= 0:
        parser.error("--cores must be positive")
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    if args.dtlb_page_walk_latency <= 0:
        parser.error("--dtlb-page-walk-latency must be positive")
    if args.fs_root is None:
        args.fs_root = Path(
            "/data00/yinhaolang/TCSim/logs/gem5-fs-roi/sample/"
            f"mesi-three-level-3GiB/{args.cores}c"
        )
    if args.output is None:
        args.output = project / f"tmp/fs-c{args.cores}-validation-v1"
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_error(predicted: float, reference: float) -> float | None:
    if reference == 0:
        return None
    return (predicted - reference) / reference


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def load_fs_summarizer(tcsim_root: Path):
    sys.path.insert(0, str((tcsim_root / "scripts").resolve()))
    try:
        from summarize_gem5_fs_cpi import load_result
    except ImportError as error:
        raise SystemExit(f"cannot import FS label parser: {error}") from error
    return load_result


def locate_cases(args: argparse.Namespace) -> list[dict]:
    load_result = load_fs_summarizer(args.tcsim_root)
    cases = []
    for workload in args.workloads:
        candidates = sorted(
            args.fs_root.glob(f"{workload}/*/*/request.json"), reverse=True
        )
        selected = None
        for request_path in candidates:
            result_dir = request_path.parent
            label = load_result(result_dir)
            trace_dir = result_dir / "tao_trace"
            if (
                label
                and label.get("valid")
                and label.get("cores") == args.cores
                and label.get("warmup_mode") == "source"
                and label.get("roi_stop_policy") == "all-core"
                and label.get("roi_target_insts_per_core") == 10_000_000
                and (trace_dir / "trace.json").is_file()
                and (trace_dir / "manifest.txt").is_file()
            ):
                selected = (result_dir, label)
                break
        if selected is None:
            raise SystemExit(f"no complete matched FS result for {workload}")
        result_dir, label = selected
        request = json.loads((result_dir / "request.json").read_text())
        trace_meta = json.loads(
            (result_dir / "tao_trace/trace.json").read_text()
        )
        match = BASELINES_RE.search(
            (result_dir / "run.log").read_text(errors="replace")
        )
        if match is None:
            raise SystemExit(f"missing WORKBEGIN baselines in {result_dir}")
        baselines = [int(value) for value in match.group(1).split(",")]
        if (
            len(baselines) != args.cores
            or len(label["per_core"]) != args.cores
        ):
            raise SystemExit(f"invalid per-core metadata in {result_dir}")
        sampling = request.get("sampling", {})
        if not sampling.get("functional_trace"):
            raise SystemExit(f"request does not declare functional trace: {result_dir}")
        cases.append(
            {
                "workload": workload,
                "cores": args.cores,
                "result_dir": result_dir,
                "trace_dir": result_dir / "tao_trace",
                "request": request,
                "trace_meta": trace_meta,
                "label": label,
                "baselines": baselines,
            }
        )
    return cases


def validate_fst_case(case: dict, verify_hashes: bool) -> dict:
    trace_meta = case["trace_meta"]
    if trace_meta.get("schema") != "tcsim-gem5-fs-functional-trace-v1":
        raise SystemExit(f"unexpected trace schema for {case['workload']}")
    cores = case["cores"]
    if trace_meta.get("cores") != cores:
        raise SystemExit(f"trace core count mismatch for {case['workload']}")
    files = []
    for core in range(cores):
        path = case["trace_dir"] / f"core{core}.fst"
        raw = path.open("rb").read(FST_HEADER.size)
        if len(raw) != FST_HEADER.size:
            raise SystemExit(f"short FST header: {path}")
        (
            magic,
            version,
            header_size,
            record_size,
            source_core,
            records,
            features,
            metadata_offset,
            metadata_count,
            metadata_size,
            _syscall_abi,
        ) = FST_HEADER.unpack(raw)
        records_end = 72 + records * 64
        if version == 7 and (features & (1 << 3)):
            complete_size = (
                metadata_count > 0
                and metadata_offset == records_end
                and metadata_size == 128
                and path.stat().st_size
                == metadata_offset + metadata_count * metadata_size
            )
        elif version == 7:
            complete_size = (
                metadata_offset == metadata_count == metadata_size == 0
                and path.stat().st_size == records_end
            )
        else:
            complete_size = path.stat().st_size == records_end
        if (
            magic != b"FSTRC01\0"
            or version not in (5, 6, 7)
            or header_size != 72
            or record_size != 64
            or source_core != core
            or not complete_size
        ):
            raise SystemExit(f"invalid/incomplete FST: {path}")
        declared = trace_meta["per_core"][str(core)]
        if declared.get("records") != records:
            raise SystemExit(f"trace.json record mismatch: {path}")
        actual_hash = sha256_file(path) if verify_hashes else None
        if verify_hashes and actual_hash != declared.get("sha256"):
            raise SystemExit(f"trace.json SHA-256 mismatch: {path}")
        files.append(
            {
                "core": core,
                "path": str(path.resolve()),
                "version": version,
                "records": records,
                "feature_flags": features,
                "sha256": actual_hash or declared.get("sha256"),
                "hash_verified": verify_hashes,
            }
        )
    return {"workload": case["workload"], "files": files}


def write_slice_manifest(
    case: dict, case_output: Path, two_phase_warmup: bool
) -> Path:
    path = case_output / "roi-slice.manifest.txt"
    lines = []
    for core, label_core in enumerate(case["label"]["per_core"]):
        source = case["trace_dir"] / f"core{core}.fst"
        trace_format = (
            "fastsim-binary-warmup-slice"
            if two_phase_warmup
            else "fastsim-binary-slice"
        )
        lines.append(
            f"{core} {trace_format} {source.resolve()} {core} "
            f"{case['baselines'][core]} {label_core['instructions']}\n"
        )
    path.write_text("".join(lines))
    return path


def run_case(args: argparse.Namespace, case: dict) -> Path:
    case_output = args.output / "cases" / case["workload"]
    case_output.mkdir(parents=True, exist_ok=True)
    stats_path = case_output / "fastsim-stats.json"
    if args.reuse_existing and stats_path.is_file():
        return stats_path
    manifest = write_slice_manifest(case, case_output, not args.cold_slice)
    command = [
        str(args.fastsim.resolve()),
        "simulate",
        "--measurement-scope",
        "user",
        "--config",
        str(args.config.resolve()),
        "--manifest",
        str(manifest.resolve()),
        "--cores",
        str(case["cores"]),
        "--dtlb-miss-model",
        "timing_walk",
        "--dtlb-page-walk-latency",
        str(args.dtlb_page_walk_latency),
        "--allow-mmio-escape",
        "true",
        "--allow-cross-page-without-virtual-token",
        "true",
        "--dram-size",
        str(3 * 1024**3),
        "--output",
        str(stats_path.resolve()),
    ]
    if args.dram_separate_write_queue is not None:
        command.extend([
            "--dram-separate-write-queue",
            str(args.dram_separate_write_queue).lower(),
        ])
    (case_output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    with (case_output / "fastsim.log").open("w") as log:
        subprocess.run(
            command,
            cwd=Path(__file__).resolve().parent.parent,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    return stats_path


def pmu_values(label: dict, stats: dict) -> tuple[dict, dict]:
    totals = stats["totals"]
    predicted = {
        "l1d_accesses": totals["l1d_accesses"],
        "l1d_misses": totals["l1d_misses"],
        "private_l2_accesses": totals["l2_accesses"],
        "private_l2_misses": totals["l2_misses"],
        "cha_llc_lookups": sum(
            item["llc_hits"] + item["llc_misses"] for item in stats["cha"]
        ),
        "llc_tag_misses": totals["llc_misses"],
        "branch_direction_misses": totals["branch_direction_misses"],
        "dtlb_accesses": totals["dtlb_accesses"],
        "dtlb_misses": totals["dtlb_misses"],
        "dram_reads": sum(item["dram_reads"] for item in stats["cha"]),
        "dram_writes": sum(item["dram_writes"] for item in stats["cha"]),
    }
    reference = {
        "l1d_accesses": label["l1d_accesses_total"],
        "l1d_misses": label["l1d_misses_total"],
        "private_l2_accesses": label["l2_accesses_total"],
        "private_l2_misses": label["l2_misses_total"],
        "cha_llc_lookups": label["llc_accesses_total"],
        "llc_tag_misses": label["llc_misses_total"],
        "branch_direction_misses": label["cond_mispred_total"],
        "dtlb_accesses": (
            label["dtlb_rd_accesses_total"] + label["dtlb_wr_accesses_total"]
        ),
        "dtlb_misses": (
            label["dtlb_rd_misses_total"] + label["dtlb_wr_misses_total"]
        ),
        "dram_reads": label["dram_read_reqs"],
        "dram_writes": label["dram_write_reqs"],
    }
    return predicted, reference


def analyze_case(case: dict, stats_path: Path) -> tuple[dict, list[dict]]:
    stats = json.loads(stats_path.read_text())
    totals = stats["totals"]
    label = case["label"]
    # gem5's FS numCycles counter advances on every clocked core through the
    # common ROI end, including a core that has already become idle. Mirror
    # that label scope with C * FastSim's global makespan. The sum of local
    # trace completion times is useful, but it is a different active-stream
    # diagnostic and must not be compared as the primary FS CPI.
    fastsim_makespan = totals["simulated_makespan_cycles"]
    cores = case["cores"]
    fastsim_label_scope_cycles = cores * fastsim_makespan
    fastsim_macro_cpi = (
        fastsim_label_scope_cycles / totals["retired_instructions"]
    )
    fastsim_uop_cpi = fastsim_label_scope_cycles / totals["retired_uops"]
    fastsim_active_stream_uop_cpi = (
        totals["sum_core_cycles"] / totals["retired_uops"]
    )
    gem5_macro_cpi = label["sum_core_cycles"] / label["total_instructions"]
    gem5_uop_cpi = label["sum_core_cycles"] / label["total_micro_ops"]
    gem5_core_cycles = [item["cycles"] for item in label["per_core"]]
    gem5_core_cycle_spread = max(gem5_core_cycles) - min(gem5_core_cycles)
    predicted, reference = pmu_values(label, stats)
    frontier = stats["causal_frontier"]
    row = {
        "workload": case["workload"],
        "gem5_macro_cpi": gem5_macro_cpi,
        "fastsim_macro_cpi": fastsim_macro_cpi,
        "macro_cpi_signed_error": relative_error(fastsim_macro_cpi, gem5_macro_cpi),
        "gem5_uop_cpi": gem5_uop_cpi,
        "fastsim_uop_cpi": fastsim_uop_cpi,
        "uop_cpi_signed_error": relative_error(fastsim_uop_cpi, gem5_uop_cpi),
        "fastsim_active_stream_uop_cpi": fastsim_active_stream_uop_cpi,
        "active_stream_uop_cpi_signed_error": relative_error(
            fastsim_active_stream_uop_cpi, gem5_uop_cpi
        ),
        "fastsim_makespan_cycles": fastsim_makespan,
        "fastsim_label_scope_cycles": fastsim_label_scope_cycles,
        "gem5_label_scope_cycles": label["sum_core_cycles"],
        "gem5_per_core_cycle_spread": gem5_core_cycle_spread,
        "gem5_per_core_cycle_relative_spread": (
            gem5_core_cycle_spread / max(gem5_core_cycles)
        ),
        "fastsim_uops_per_second": stats["throughput"]["uops_per_second"],
        "fastsim_measurement_uops_per_second": stats["throughput"].get(
            "measurement_uops_per_second",
            stats["throughput"]["uops_per_second"],
        ),
        "fastsim_end_to_end_uops_per_second": stats["throughput"].get(
            "end_to_end_uops_per_second",
            stats["throughput"]["uops_per_second"],
        ),
        "fastsim_mips": stats["throughput"]["mips"],
        "gem5_instructions": label["total_instructions"],
        "fastsim_instructions": totals["retired_instructions"],
        "gem5_uops": label["total_micro_ops"],
        "fastsim_uops": totals["retired_uops"],
        "uop_count_signed_error": relative_error(
            totals["retired_uops"], label["total_micro_ops"]
        ),
        "uop_conservation_ok": (
            frontier["interval_accepted_uops"] == totals["retired_uops"]
        ),
        "memory_event_conservation_ok": (
            frontier["batch_memory_events"] == totals["memory_accesses"]
        ),
        "memory_partition_ok": (
            frontier["interval_private_memory_events"]
            + frontier["interval_escape_memory_events"]
            == frontier["batch_memory_events"]
        ),
        "response_critical_conserved": totals["response_critical_conserved"],
        "unknown_addresses": totals["unknown_addresses"],
        "mmio_escape_accesses": totals["mmio_escape_accesses"],
        "branches_without_outcome": totals["branches_without_outcome"],
        "dtlb_untracked": totals["dtlb_untracked"],
        "functional_warmup_enabled": totals["functional_warmup_enabled"],
        "functional_warmup_instructions": totals[
            "functional_warmup_instructions"
        ],
        "functional_warmup_uops": totals["functional_warmup_uops"],
        "functional_warmup_memory_events": totals[
            "functional_warmup_memory_events"
        ],
        "functional_warmup_barrier_cycles": totals[
            "functional_warmup_barrier_cycles"
        ],
        "functional_warmup_instruction_count_ok": (
            totals["functional_warmup_instructions"] == sum(case["baselines"])
            if totals["functional_warmup_enabled"]
            else totals["functional_warmup_instructions"] == 0
        ),
        "dram_write_queue_enqueues": frontier.get(
            "dram_write_queue_enqueues", 0
        ),
        "dram_write_queue_drained": frontier.get(
            "dram_write_queue_drained", 0
        ),
        "dram_write_queue_read_bypasses": frontier.get(
            "dram_write_queue_read_bypasses", 0
        ),
        "dram_write_queue_high_watermark_switches": frontier.get(
            "dram_write_queue_high_watermark_switches", 0
        ),
        "dram_write_queue_turnarounds": frontier.get(
            "dram_write_queue_turnarounds", 0
        ),
        "dram_write_queue_row_hits": frontier.get(
            "dram_write_queue_row_hits", 0
        ),
        "dram_write_queue_row_misses": frontier.get(
            "dram_write_queue_row_misses", 0
        ),
        "dram_write_queue_max_pending": frontier.get(
            "dram_write_queue_max_pending", 0
        ),
        "dram_write_queue_pending_initial": frontier.get(
            "dram_write_queue_pending_initial", 0
        ),
        "dram_write_queue_pending_final": frontier.get(
            "dram_write_queue_pending_final", 0
        ),
        "result_dir": str(case["result_dir"].resolve()),
        "fastsim_stats": str(stats_path.resolve()),
    }
    for field in PMU_FIELDS:
        row[f"{field}_fastsim"] = predicted[field]
        row[f"{field}_gem5"] = reference[field]
        row[f"{field}_signed_error"] = relative_error(
            predicted[field], reference[field]
        )
    per_core = []
    for core in range(cores):
        fast = stats["cores"][core]
        ref = label["per_core"][core]
        fast_uop_cpi = fastsim_makespan / fast["uops"]
        fast_active_uop_cpi = fast["cycles"] / fast["uops"]
        ref_uop_cpi = ref["cycles"] / ref["micro_ops"]
        per_core.append(
            {
                "workload": case["workload"],
                "core": core,
                "warmup_instructions_skipped": case["baselines"][core],
                "gem5_instructions": ref["instructions"],
                "fastsim_instructions": fast["instructions"],
                "gem5_uops": ref["micro_ops"],
                "fastsim_uops": fast["uops"],
                "gem5_cycles": ref["cycles"],
                "fastsim_cycles": fastsim_makespan,
                "fastsim_active_stream_cycles": fast["cycles"],
                "gem5_uop_cpi": ref_uop_cpi,
                "fastsim_uop_cpi": fast_uop_cpi,
                "fastsim_active_stream_uop_cpi": fast_active_uop_cpi,
                "uop_cpi_signed_error": relative_error(fast_uop_cpi, ref_uop_cpi),
                "active_stream_uop_cpi_signed_error": relative_error(
                    fast_active_uop_cpi, ref_uop_cpi
                ),
            }
        )
    return row, per_core


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict], per_core: list[dict], cores: int) -> dict:
    cpi_errors = [abs(row["uop_cpi_signed_error"]) for row in rows]
    uop_count_errors = [
        abs(row["uop_count_signed_error"]) for row in rows
    ]
    pmu = {}
    for field in PMU_FIELDS:
        pairs = [
            (row[f"{field}_fastsim"], row[f"{field}_gem5"]) for row in rows
        ]
        denominator = sum(reference for _, reference in pairs)
        pmu[field] = {
            "wape": (
                sum(abs(predicted - reference) for predicted, reference in pairs)
                / denominator
                if denominator
                else None
            ),
            "pooled_signed_error": (
                (sum(predicted for predicted, _ in pairs) - denominator)
                / denominator
                if denominator
                else None
            ),
        }
    two_phase_warmup = all(
        row["functional_warmup_enabled"] for row in rows
    )
    return {
        "schema": "fastsim-gem5-fs-validation-v1",
        "scope": {
            "cores": cores,
            "cases": len(rows),
            "workloads": [row["workload"] for row in rows],
            "roi": "source WORKBEGIN to all-core >=10M committed instructions",
            "trace_slice": (
                "two-phase prefix replay and common measurement barrier"
                if two_phase_warmup
                else "skip per-core O3 prefix at macro-instruction boundary"
            ),
            "initial_state": (
                "FastSim functional warm state retained at WORKBEGIN barrier"
                if two_phase_warmup
                else "cold FastSim state at ROI slice; FS warm state not replayed"
            ),
            "two_phase_functional_warmup": two_phase_warmup,
            "production_gate_eligible": False,
            "cpi_cycle_scope": "cores multiplied by common simulated makespan",
        },
        "cpi": {
            "mean_absolute_error": statistics.mean(cpi_errors),
            "median_absolute_error": statistics.median(cpi_errors),
            "p90_absolute_error": percentile(cpi_errors, 0.90),
            "p99_absolute_error": percentile(cpi_errors, 0.99),
            "maximum_absolute_error": max(cpi_errors),
            "mean_signed_error": statistics.mean(
                row["uop_cpi_signed_error"] for row in rows
            ),
            "per_core_mape": statistics.mean(
                abs(row["uop_cpi_signed_error"]) for row in per_core
            ),
        },
        "pmu": pmu,
        "throughput": {
            "minimum_uops_per_second": min(
                row["fastsim_uops_per_second"] for row in rows
            ),
            "median_uops_per_second": statistics.median(
                row["fastsim_uops_per_second"] for row in rows
            ),
            "minimum_measurement_uops_per_second": min(
                row["fastsim_measurement_uops_per_second"] for row in rows
            ),
            "median_measurement_uops_per_second": statistics.median(
                row["fastsim_measurement_uops_per_second"] for row in rows
            ),
            "minimum_end_to_end_uops_per_second": min(
                row["fastsim_end_to_end_uops_per_second"] for row in rows
            ),
            "median_end_to_end_uops_per_second": statistics.median(
                row["fastsim_end_to_end_uops_per_second"] for row in rows
            ),
        },
        "conservation": {
            "uop_failures": sum(not row["uop_conservation_ok"] for row in rows),
            "memory_event_failures": sum(
                not row["memory_event_conservation_ok"] for row in rows
            ),
            "memory_partition_failures": sum(
                not row["memory_partition_ok"] for row in rows
            ),
            "response_critical_failures": sum(
                not row["response_critical_conserved"] for row in rows
            ),
            "unknown_addresses": sum(row["unknown_addresses"] for row in rows),
            "mmio_escape_accesses": sum(
                row["mmio_escape_accesses"] for row in rows
            ),
            "dtlb_untracked": sum(row["dtlb_untracked"] for row in rows),
        },
        "input_alignment": {
            "instruction_count_mismatches": sum(
                row["fastsim_instructions"] != row["gem5_instructions"]
                for row in rows
            ),
            "absolute_uop_count_delta": sum(
                abs(row["fastsim_uops"] - row["gem5_uops"])
                for row in rows
            ),
            "maximum_absolute_uop_count_error": max(uop_count_errors),
            "maximum_gem5_per_core_cycle_spread": max(
                row["gem5_per_core_cycle_spread"] for row in rows
            ),
            "maximum_gem5_per_core_cycle_relative_spread": max(
                row["gem5_per_core_cycle_relative_spread"] for row in rows
            ),
            "functional_warmup_instruction_count_failures": sum(
                not row["functional_warmup_instruction_count_ok"]
                for row in rows
            ),
        },
        "diagnostic_gates": {
            "cpi_p99_le_10_percent": percentile(cpi_errors, 0.99) <= 0.10,
            "minimum_throughput_ge_5m_uops": min(
                row["fastsim_measurement_uops_per_second"] for row in rows
            )
            >= 5_000_000,
            "minimum_end_to_end_throughput_ge_5m_uops": min(
                row["fastsim_end_to_end_uops_per_second"] for row in rows
            )
            >= 5_000_000,
            "all_internal_conservation": all(
                row["uop_conservation_ok"]
                and row["memory_event_conservation_ok"]
                and row["memory_partition_ok"]
                and row["response_critical_conserved"]
                and row["unknown_addresses"] == 0
                for row in rows
            ),
            "gem5_core_cycle_scope_spread_le_10ppm": max(
                row["gem5_per_core_cycle_relative_spread"] for row in rows
            ) <= 1e-5,
            "all_functional_warmup_instruction_counts_match": all(
                row["functional_warmup_instruction_count_ok"] for row in rows
            ),
        },
        "cases": rows,
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def write_markdown(path: Path, report: dict) -> None:
    two_phase_warmup = report["scope"]["two_phase_functional_warmup"]
    cores = report["scope"]["cores"]
    trace_files = [
        item
        for case in report.get("identity", {}).get("trace_files", [])
        for item in case["files"]
    ]
    hashes_verified = bool(trace_files) and all(
        item["hash_verified"] for item in trace_files
    )
    lines = [
        f"# FastSim gem5 FS C{cores} diagnostic validation",
        "",
        (
            "> Two-phase functional warmup replays the complete pre-WORKBEGIN "
            "prefix, retains FastSim cache/predictor/TLB/OoO/memory state, and "
            "resets measurement counters only after the common core barrier."
            if two_phase_warmup
            else "> This is a cold-slice diagnostic. The functional ROI is "
                 "aligned, but pre-WORKBEGIN state is not replayed."
        ),
        f"> Primary CPI mirrors gem5 FS `numCycles`: {cores} clocked cores times "
        "the common FastSim makespan. Per-core active-stream completion time "
        "is retained only as a diagnostic.",
        "",
        "| Workload | gem5 UOP CPI | FastSim UOP CPI | CPI error | UOP count error | ROI / end-to-end M UOP/s |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["cases"]:
        lines.append(
            f"| {row['workload']} | {row['gem5_uop_cpi']:.6f} | "
            f"{row['fastsim_uop_cpi']:.6f} | "
            f"{percent(row['uop_cpi_signed_error'])} | "
            f"{percent(row['uop_count_signed_error'])} | "
            f"{row['fastsim_measurement_uops_per_second'] / 1e6:.3f} / "
            f"{row['fastsim_end_to_end_uops_per_second'] / 1e6:.3f} |"
        )
    if any(row["dram_write_queue_enqueues"] for row in report["cases"]):
        lines.extend(
            [
                "",
                "## DRAM write-controller audit",
                "",
                "| Workload | enqueued | drained | read bypasses | "
                "turnarounds | write row-hit | max/initial/final pending |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in report["cases"]:
            write_commands = (
                row["dram_write_queue_row_hits"]
                + row["dram_write_queue_row_misses"]
            )
            row_hit_rate = (
                row["dram_write_queue_row_hits"] / write_commands
                if write_commands
                else 0.0
            )
            lines.append(
                f"| {row['workload']} | "
                f"{row['dram_write_queue_enqueues']} | "
                f"{row['dram_write_queue_drained']} | "
                f"{row['dram_write_queue_read_bypasses']} | "
                f"{row['dram_write_queue_turnarounds']} | "
                f"{percent(row_hit_rate)} | "
                f"{row['dram_write_queue_max_pending']}/"
                f"{row['dram_write_queue_pending_initial']}/"
                f"{row['dram_write_queue_pending_final']} |"
            )
    cpi = report["cpi"]
    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- UOP CPI mean / P90 / P99 / max absolute error: "
            f"{percent(cpi['mean_absolute_error'])} / "
            f"{percent(cpi['p90_absolute_error'])} / "
            f"{percent(cpi['p99_absolute_error'])} / "
            f"{percent(cpi['maximum_absolute_error'])}",
            f"- Per-core UOP CPI MAPE: {percent(cpi['per_core_mape'])}",
            f"- Minimum ROI / end-to-end throughput: "
            f"{report['throughput']['minimum_measurement_uops_per_second'] / 1e6:.3f} / "
            f"{report['throughput']['minimum_end_to_end_uops_per_second'] / 1e6:.3f} M UOP/s",
            "- Legacy mixed-scope throughput (ROI UOPs divided by warmup+ROI "
            f"wall time): {report['throughput']['minimum_uops_per_second'] / 1e6:.3f} M UOP/s",
            "",
            "## Input alignment and explicit exceptions",
            "",
            f"- Exact macro-instruction count mismatches: "
            f"{report['input_alignment']['instruction_count_mismatches']}",
            f"- Absolute UOP count delta / maximum relative error: "
            f"{report['input_alignment']['absolute_uop_count_delta']} / "
            f"{percent(report['input_alignment']['maximum_absolute_uop_count_error'])}",
            f"- Explicit FS MMIO/pseudo-op escape accesses: "
            f"{report['conservation']['mmio_escape_accesses']}",
            f"- Cross-page records without representable DTLB token: "
            f"{report['conservation']['dtlb_untracked']}",
            f"- Functional warmup instruction-count failures: "
            f"{report['input_alignment']['functional_warmup_instruction_count_failures']}",
            f"- Maximum gem5 per-core cycle-scope spread: "
            f"{report['input_alignment']['maximum_gem5_per_core_cycle_spread']} "
            f"cycles ({percent(report['input_alignment']['maximum_gem5_per_core_cycle_relative_spread'])})",
            "",
            "## PMU WAPE",
            "",
            "| Metric | WAPE | pooled signed error |",
            "|---|---:|---:|",
        ]
    )
    for field in PMU_FIELDS:
        item = report["pmu"][field]
        lines.append(
            f"| {field} | {percent(item['wape'])} | "
            f"{percent(item['pooled_signed_error'])} |"
        )
    lines.extend(
        [
            "",
            "## Validity",
            "",
            f"- {cores * report['scope']['cases']}/"
            f"{cores * report['scope']['cases']} inputs are supported canonical "
            "FST v5/v6/v7 with matching header, source core, record count, and "
            + (
                "SHA-256."
                if hashes_verified
                else "file length; SHA-256 values were reused from trace.json."
            ),
            "- The capture's `uarch_profile.json` is not authoritative: its wrapper "
            "used fallback 4 GHz / 2 MiB / 1-bank defaults while `config.ini` and "
            "`request.json` describe the actual 3 GHz / 64 MiB / 8-bank target.",
            "- The one-cycle fixed timing walker is a declared starting proxy; page-table "
            "memory requests, ITLB/I-cache timing, interrupts, scheduling, and OS noise "
            "remain unmodeled.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    args.fs_root = args.fs_root.resolve()
    args.tcsim_root = args.tcsim_root.resolve()
    args.fastsim = args.fastsim.resolve()
    args.config = args.config.resolve()
    args.output = args.output.resolve()
    for required in (args.fs_root, args.fastsim, args.config):
        if not required.exists():
            raise SystemExit(f"missing required path: {required}")
    args.output.mkdir(parents=True, exist_ok=True)

    cases = locate_cases(args)
    trace_identity = []
    for case in cases:
        print(
            f"[fs-c{args.cores}] validate FST {case['workload']}",
            flush=True,
        )
        trace_identity.append(
            validate_fst_case(case, not args.skip_trace_hashes)
        )

    stats_paths: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(run_case, args, case): case for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            stats_paths[case["workload"]] = future.result()
            print(
                f"[fs-c{args.cores}] complete {case['workload']}",
                flush=True,
            )

    rows = []
    per_core = []
    for case in cases:
        row, core_rows = analyze_case(case, stats_paths[case["workload"]])
        rows.append(row)
        per_core.extend(core_rows)
    report = summarize(rows, per_core, args.cores)
    report["identity"] = {
        "fastsim_sha256": sha256_file(args.fastsim),
        "config_sha256": sha256_file(args.config),
        "validator_sha256": sha256_file(Path(__file__).resolve()),
        "trace_files": trace_identity,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": sys.argv,
        "dtlb_miss_model": "timing_walk",
        "dtlb_page_walk_latency": args.dtlb_page_walk_latency,
        "dram_size_bytes": 3 * 1024**3,
        "allow_mmio_escape": True,
        "allow_cross_page_without_virtual_token": True,
        "two_phase_functional_warmup": not args.cold_slice,
        "production_switches": {
            "dram_separate_write_queue": (
                args.dram_separate_write_queue
                if args.dram_separate_write_queue is not None
                else "config"
            ),
        },
        "experimental_switches": {
            "rename_free_list": False,
            "response_rename_feedback": False,
            "branch_shadow_rob": False,
            "dram_frfcfs_full_queue_page_policy": False,
            "dram_frfcfs_row_cap_single_precharge": False,
        },
    }
    (args.output / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    write_csv(args.output / "summary.csv", rows)
    write_csv(args.output / "per-core.csv", per_core)
    write_markdown(args.output / "summary.md", report)
    (args.output / "run-manifest.json").write_text(
        json.dumps(report["identity"], indent=2, sort_keys=True) + "\n"
    )
    print(
        f"[fs-c{args.cores}] report={args.output / 'summary.md'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
