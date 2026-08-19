#!/usr/bin/env python3
"""Audit whether a functional warmup covers measurement cache lines.

The audit is deliberately timing-free.  It reads only canonical FST hot
records and their record-exact warmup boundaries.  Physical cache lines used
by measurement are classified as:

* seen by the same core during user-functional warmup;
* seen only by another core during user-functional warmup; or
* absent from every user-functional warmup stream.

Optional FastSim/gem5 accuracy reports are joined only after the trace-derived
coverage has been computed.  Oracle counters never affect the classification.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np


HEADER = struct.Struct("<8sIIIIQQ4Q")
HEADER_BYTES = 72
RECORD_BYTES = 64
MAGIC = b"FSTRC01\0"
MEMORY_FLAGS = (1 << 1) | (1 << 2) | (1 << 3)
WRITE_FLAGS = (1 << 2) | (1 << 3)
PHYSICAL_FLAG = 1 << 12
RECORD_DTYPE = np.dtype(
    {
        "names": ["address", "size", "flags"],
        "formats": ["<u8", "<u2", "<u2"],
        "offsets": [8, 48, 50],
        "itemsize": RECORD_BYTES,
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit", type=Path, required=True,
        help="audit_functional_warmup_matrix.py JSON output",
    )
    parser.add_argument(
        "--accuracy-root", type=Path,
        help="Optional run_kernel_event_accuracy_pipeline.py output root",
    )
    parser.add_argument(
        "--include-cores", type=int, nargs="+",
        help="Only audit cases with these core counts",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--line-bytes", type=int, default=64)
    parser.add_argument(
        "--page-bytes", type=int, default=4096,
        help="Base-page size used only for first-touch page diagnostics",
    )
    parser.add_argument("--chunk-records", type=int, default=1_000_000)
    parser.add_argument(
        "--tail-records", type=int, action="append", default=[],
        help="Warmup tail size to audit; repeatable (default: 10K,100K,1M)",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_header(path: Path) -> dict[str, int]:
    with path.open("rb") as source:
        raw = source.read(HEADER_BYTES)
    if len(raw) != HEADER_BYTES:
        raise ValueError(f"short FST header: {path}")
    magic, version, header_size, record_size, core_id, records, features, *_ = (
        HEADER.unpack(raw)
    )
    if (
        magic != MAGIC or version not in range(3, 8)
        or header_size != HEADER_BYTES or record_size != RECORD_BYTES
    ):
        raise ValueError(f"unsupported canonical FST header: {path}")
    minimum_size = HEADER_BYTES + records * RECORD_BYTES
    if path.stat().st_size < minimum_size:
        raise ValueError(f"truncated FST hot stream: {path}")
    return {
        "version": version, "core_id": core_id,
        "records": records, "features": features,
    }


def empty_u64() -> np.ndarray:
    return np.empty(0, dtype=np.uint64)


def sorted_unique(parts: list[np.ndarray]) -> np.ndarray:
    nonempty = [part for part in parts if part.size]
    if not nonempty:
        return empty_u64()
    return np.unique(np.concatenate(nonempty))


def contains(sorted_haystack: np.ndarray, needles: np.ndarray) -> np.ndarray:
    if needles.size == 0:
        return np.zeros(0, dtype=bool)
    if sorted_haystack.size == 0:
        return np.zeros(needles.size, dtype=bool)
    indices = np.searchsorted(sorted_haystack, needles)
    valid = indices < sorted_haystack.size
    result = np.zeros(needles.size, dtype=bool)
    result[valid] = sorted_haystack[indices[valid]] == needles[valid]
    return result


def extract_line_touches(
    path: Path,
    begin: int,
    count: int,
    line_bytes: int,
    chunk_records: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Return physical line touches, write bits, and exclusion counters."""
    header = read_header(path)
    end = begin + count
    if begin < 0 or count < 0 or end > header["records"]:
        raise ValueError(
            f"slice [{begin},{end}) exceeds {header['records']} records: {path}"
        )
    records = np.memmap(
        path, dtype=RECORD_DTYPE, mode="r", offset=HEADER_BYTES,
        shape=(header["records"],),
    )
    line_parts: list[np.ndarray] = []
    write_parts: list[np.ndarray] = []
    stats: Counter[str] = Counter()
    for chunk_begin in range(begin, end, chunk_records):
        chunk_end = min(end, chunk_begin + chunk_records)
        chunk = records[chunk_begin:chunk_end]
        flags = np.asarray(chunk["flags"])
        memory = (flags & MEMORY_FLAGS) != 0
        stats["records"] += chunk_end - chunk_begin
        stats["memory_records"] += int(np.count_nonzero(memory))
        physical = memory & ((flags & PHYSICAL_FLAG) != 0)
        sizes = np.asarray(chunk["size"])
        zero_size = physical & (sizes == 0)
        selected = physical & (sizes != 0)
        stats["nonphysical_memory_records"] += int(
            np.count_nonzero(memory & ~physical)
        )
        stats["zero_size_memory_records"] += int(np.count_nonzero(zero_size))
        if not np.any(selected):
            continue
        addresses = np.asarray(chunk["address"])[selected].astype(
            np.uint64, copy=False
        )
        selected_sizes = sizes[selected].astype(np.uint64, copy=False)
        if np.any(addresses > np.iinfo(np.uint64).max - (selected_sizes - 1)):
            raise ValueError(f"memory access wraps uint64 address space: {path}")
        starts = addresses // line_bytes
        ends = (addresses + selected_sizes - 1) // line_bytes
        writes = (flags[selected] & WRITE_FLAGS) != 0
        line_parts.append(starts)
        write_parts.append(writes)
        cross = np.flatnonzero(ends > starts)
        stats["cross_line_memory_records"] += int(cross.size)
        for index in cross:
            extras = np.arange(
                int(starts[index]) + 1, int(ends[index]) + 1,
                dtype=np.uint64,
            )
            line_parts.append(extras)
            write_parts.append(np.full(extras.size, writes[index], dtype=bool))
    del records
    lines = np.concatenate(line_parts) if line_parts else empty_u64()
    writes = (
        np.concatenate(write_parts)
        if write_parts else np.empty(0, dtype=bool)
    )
    stats["physical_memory_records"] = (
        stats["memory_records"] - stats["nonphysical_memory_records"]
        - stats["zero_size_memory_records"]
    )
    stats["line_touches"] = int(lines.size)
    stats["write_line_touches"] = int(np.count_nonzero(writes))
    stats["read_line_touches"] = int(lines.size - np.count_nonzero(writes))
    return lines, writes, dict(sorted(stats.items()))


def coverage(count: int, total: int) -> dict[str, int | float | None]:
    return {
        "count": int(count),
        "fraction": (float(count) / total if total else None),
    }


def classify(
    lines: np.ndarray,
    own_warm: np.ndarray,
    global_warm: np.ndarray,
) -> dict[str, Any]:
    own = contains(own_warm, lines)
    anywhere = contains(global_warm, lines)
    same_count = int(np.count_nonzero(own))
    other_count = int(np.count_nonzero(~own & anywhere))
    unseen_count = int(np.count_nonzero(~anywhere))
    total = int(lines.size)
    return {
        "total": total,
        "same_core_warmup": coverage(same_count, total),
        "other_core_only_warmup": coverage(other_count, total),
        "absent_from_all_user_warmup": coverage(unseen_count, total),
    }


def accuracy_for_case(
    accuracy_root: Path | None, cores: int, workload: str
) -> dict[str, Any] | None:
    if accuracy_root is None:
        return None
    path = accuracy_root / "cases" / f"{cores:02d}c-{workload}" / "accuracy.json"
    if not path.is_file():
        return None
    document = read_json(path)
    user_pmu = document["pmu"]["user"]

    def pmu(name: str) -> dict[str, int]:
        row = user_pmu[name]
        return {
            "predicted": int(row["predicted"]),
            "reference": int(row["reference"]),
        }

    return {
        "path": str(path.resolve()),
        "cpi": {
            "predicted": float(
                document["cycles_per_user_uop"]["user"]["predicted"]
            ),
            "reference": float(
                document["cycles_per_user_uop"]["user"]["reference"]
            ),
            "absolute_percentage_error": float(
                document["cycles_per_user_uop"]["user"][
                    "absolute_percentage_error"
                ]
            ),
        },
        "pmu": {
            name: pmu(name)
            for name in ("l1d_misses", "l2_misses", "llc_misses")
        },
    }


def oracle_kernel_state(result_dir: Path) -> dict[str, Any] | None:
    path = result_dir / "oracle" / "kernel_events.json"
    if not path.is_file():
        return None
    aggregate = read_json(path).get("aggregate", {})
    event_counts = aggregate.get("event_counts", {})
    by_class = aggregate.get("pmu_kernel_by_class", {})
    page_fault_pmu = by_class.get("page_fault", {})
    return {
        "path": str(path.resolve()),
        "pmu_source": aggregate.get("pmu_source"),
        "page_fault_events": int(event_counts.get("page_fault", 0)),
        "page_fault_kernel_cycles": int(
            aggregate.get("page_fault_kernel_cycles", 0)
        ),
        "page_fault_kernel_pmu": {
            name: int(page_fault_pmu.get(name, 0))
            for name in ("l1d_misses", "l2_misses", "llc_misses")
        },
    }


def gem5_prefetch_state(result_dir: Path) -> dict[str, Any]:
    path = result_dir / "config.ini"
    if not path.is_file():
        return {"config": None, "enabled": None, "setting_count": 0}
    settings = re.findall(
        r"^\s*enable_prefetch\s*=\s*(true|false)\s*$",
        path.read_text(encoding="utf-8", errors="replace"),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return {
        "config": str(path.resolve()),
        "enabled": (
            any(value.lower() == "true" for value in settings)
            if settings else None
        ),
        "setting_count": len(settings),
    }


def diagnose(case: dict[str, Any]) -> dict[str, Any] | None:
    accuracy = case.get("accuracy")
    if not accuracy:
        return None
    unseen = int(
        case["measurement_unique_lines"]
        ["absent_from_all_user_warmup"]["count"]
    )
    predicted = int(accuracy["pmu"]["llc_misses"]["predicted"])
    reference = int(accuracy["pmu"]["llc_misses"]["reference"])
    oracle_state = case.get("oracle_kernel_state") or {}
    page_fault_events = int(oracle_state.get("page_fault_events", 0))
    page_fault_llc = int(
        oracle_state.get("page_fault_kernel_pmu", {}).get("llc_misses", 0)
    )
    unseen_pages = int(case["measurement_pages"]["globally_unseen_lines_pages"])
    prefetch_enabled = case["gem5_prefetch"].get("enabled")
    ratio = unseen / predicted if predicted else None
    avoided = max(0, unseen - reference)
    cold_line_signature = (
        predicted > 0
        and 0.8 <= unseen / predicted <= 1.2
        and reference < unseen / 2
    )
    if (
        cold_line_signature and prefetch_enabled is False
        and page_fault_llc >= unseen * 0.75
    ):
        classification = "page_fault_kernel_cache_state_dominant"
    elif cold_line_signature and prefetch_enabled is False and page_fault_llc:
        classification = "kernel_cache_state_mixed"
    elif cold_line_signature:
        classification = "unmodeled_kernel_or_prefetch_state_dominant"
    elif predicted > max(1, unseen) * 1.25:
        classification = "replacement_reuse_or_coherence_dominant"
    elif unseen > max(1, predicted) * 1.25:
        classification = "measurement_sharing_or_prefetch_needed"
    else:
        classification = "mixed_or_low_signal"
    return {
        "classification": classification,
        "globally_unseen_unique_lines": unseen,
        "fastsim_llc_misses": predicted,
        "gem5_llc_misses": reference,
        "unseen_per_fastsim_llc_miss": ratio,
        "unseen_lines_not_accounted_as_gem5_llc_misses": avoided,
        "globally_unseen_lines_unique_pages": unseen_pages,
        "page_fault_events": page_fault_events,
        "page_fault_events_per_unseen_page": (
            page_fault_events / unseen_pages if unseen_pages else None
        ),
        "page_fault_kernel_llc_misses": page_fault_llc,
        "page_fault_llc_misses_per_unseen_line": (
            page_fault_llc / unseen if unseen else None
        ),
        "gem5_prefetch_enabled": prefetch_enabled,
        "note": (
            "Count alignment is diagnostic rather than address-level proof: "
            "the current oracle has kernel PMU counts but no kernel memory "
            "addresses. Cross-core sharing can also serve a demand line."
        ),
    }


def audit_case(
    source: dict[str, Any],
    accuracy_root: Path | None,
    line_bytes: int,
    page_bytes: int,
    chunk_records: int,
    tails: list[int],
) -> dict[str, Any]:
    result_dir = Path(source["result_dir"]).resolve()
    trace_dir = result_dir / "tao_trace"
    trace = read_json(trace_dir / "trace.json")
    cores = int(source["cores"])
    workload = str(source["workload"])
    warm_unique: dict[int, np.ndarray] = {}
    warm_tail_unique: dict[int, dict[int, np.ndarray]] = {}
    measurement_lines: dict[int, np.ndarray] = {}
    measurement_writes: dict[int, np.ndarray] = {}
    per_core: dict[str, Any] = {}

    for core in range(cores):
        boundary = trace["functional_boundaries"][str(core)]
        warm_records = int(boundary["warmup_records"])
        measurement_records = int(boundary["measurement_records"])
        path = Path(trace["per_core"][str(core)]["fst"]).resolve()
        header = read_header(path)
        if header["core_id"] != core:
            raise ValueError(f"FST core mismatch: expected {core}: {path}")
        if warm_records + measurement_records != header["records"]:
            raise ValueError(f"boundary does not conserve FST records: {path}")
        warm_lines, _warm_writes, warm_stats = extract_line_touches(
            path, 0, warm_records, line_bytes, chunk_records
        )
        measured_lines, measured_writes, measured_stats = extract_line_touches(
            path, warm_records, measurement_records, line_bytes, chunk_records
        )
        warm_unique[core] = np.unique(warm_lines)
        measurement_lines[core] = measured_lines
        measurement_writes[core] = measured_writes
        warm_tail_unique[core] = {}
        for tail in tails:
            tail_begin = max(0, warm_records - tail)
            tail_lines, _tail_writes, _tail_stats = extract_line_touches(
                path, tail_begin, warm_records - tail_begin,
                line_bytes, chunk_records,
            )
            warm_tail_unique[core][tail] = np.unique(tail_lines)
        per_core[str(core)] = {
            "fst": str(path),
            "warmup_records": warm_records,
            "measurement_records": measurement_records,
            "warmup": {
                **warm_stats,
                "unique_physical_lines": int(warm_unique[core].size),
            },
            "measurement": {
                **measured_stats,
                "unique_physical_lines": int(np.unique(measured_lines).size),
            },
        }

    global_warm = sorted_unique(list(warm_unique.values()))
    global_measurement = sorted_unique(
        [np.unique(lines) for lines in measurement_lines.values()]
    )
    global_tails = {
        tail: sorted_unique(
            [warm_tail_unique[core][tail] for core in range(cores)]
        )
        for tail in tails
    }
    global_unique_class = classify(
        global_measurement, global_warm, global_warm
    )
    # The global set has no meaningful "other core only" category.
    global_unique_class.pop("other_core_only_warmup")
    unseen_lines = global_measurement[
        ~contains(global_warm, global_measurement)
    ]
    lines_per_page = page_bytes // line_bytes
    measurement_pages = np.unique(global_measurement // lines_per_page)
    unseen_line_pages = np.unique(unseen_lines // lines_per_page)

    for core in range(cores):
        lines = measurement_lines[core]
        unique_lines = np.unique(lines)
        per_core[str(core)]["measurement_line_touches"] = classify(
            lines, warm_unique[core], global_warm
        )
        per_core[str(core)]["measurement_unique_lines"] = classify(
            unique_lines, warm_unique[core], global_warm
        )
        writes = measurement_writes[core]
        per_core[str(core)]["measurement_read_line_touches"] = classify(
            lines[~writes], warm_unique[core], global_warm
        )
        per_core[str(core)]["measurement_write_line_touches"] = classify(
            lines[writes], warm_unique[core], global_warm
        )

    tail_coverage = {}
    for tail, tail_lines in global_tails.items():
        seen = int(np.count_nonzero(contains(tail_lines, global_measurement)))
        tail_coverage[str(tail)] = {
            "warmup_tail_unique_lines": int(tail_lines.size),
            "measurement_unique_lines_seen": coverage(
                seen, int(global_measurement.size)
            ),
        }
    result: dict[str, Any] = {
        "workload": workload,
        "cores": cores,
        "result_dir": str(result_dir),
        "line_bytes": line_bytes,
        "page_bytes": page_bytes,
        "warmup_records": sum(
            int(row["warmup_records"]) for row in per_core.values()
        ),
        "measurement_records": sum(
            int(row["measurement_records"]) for row in per_core.values()
        ),
        "warmup_unique_lines": int(global_warm.size),
        "measurement_unique_lines": global_unique_class,
        "measurement_pages": {
            "unique_pages": int(measurement_pages.size),
            "globally_unseen_lines_pages": int(unseen_line_pages.size),
            "globally_unseen_lines_per_page": (
                float(unseen_lines.size) / unseen_line_pages.size
                if unseen_line_pages.size else None
            ),
        },
        "warmup_tail_coverage": tail_coverage,
        "per_core": per_core,
        "accuracy": accuracy_for_case(accuracy_root, cores, workload),
        "oracle_kernel_state": oracle_kernel_state(result_dir),
        "gem5_prefetch": gem5_prefetch_state(result_dir),
    }
    result["diagnosis"] = diagnose(result)
    return result


def percentage(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.2f}%"


def markdown(document: dict[str, Any]) -> str:
    lines = [
        "# FST user-functional warmup cacheline sufficiency audit",
        "",
        "All cacheline classifications are derived only from physical FST "
        "addresses. Oracle/FastSim counters are joined after classification.",
        "",
        "| Workload | Warmup records | Warm unique lines | Measurement unique "
        "lines | Seen in any user warmup | Globally unseen | FastSim LLC miss | "
        "Unseen pages | PF events | PF kernel LLC miss | gem5 LLC miss | "
        "User CPI APE | Prefetch | Diagnosis |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for case in document["cases"]:
        unique = case["measurement_unique_lines"]
        accuracy = case.get("accuracy")
        diagnosis = case.get("diagnosis")
        if accuracy:
            predicted = accuracy["pmu"]["llc_misses"]["predicted"]
            reference = accuracy["pmu"]["llc_misses"]["reference"]
            cpi_ape = f"{accuracy['cpi']['absolute_percentage_error']:.2f}%"
        else:
            predicted = reference = "n/a"
            cpi_ape = "n/a"
        oracle_state = case.get("oracle_kernel_state") or {}
        page_fault_events = oracle_state.get("page_fault_events", "n/a")
        page_fault_llc = oracle_state.get(
            "page_fault_kernel_pmu", {}
        ).get("llc_misses", "n/a")
        prefetch = case["gem5_prefetch"].get("enabled")
        prefetch_text = "unknown" if prefetch is None else str(prefetch).lower()
        lines.append(
            f"| {case['cores']}c/{case['workload']} | "
            f"{case['warmup_records']:,} | {case['warmup_unique_lines']:,} | "
            f"{unique['total']:,} | "
            f"{percentage(unique['same_core_warmup']['fraction'])} | "
            f"{unique['absent_from_all_user_warmup']['count']:,} | "
            f"{predicted} | "
            f"{case['measurement_pages']['globally_unseen_lines_pages']:,} | "
            f"{page_fault_events} | {page_fault_llc} | {reference} | "
            f"{cpi_ape} | {prefetch_text} | "
            f"{diagnosis['classification'] if diagnosis else 'n/a'} |"
        )
    lines.extend(["", "## Warmup-tail coverage", ""])
    tails = document["tail_records"]
    header = "| Workload | " + " | ".join(f"last {tail:,}" for tail in tails) + " | full |"
    lines.extend([header, "|---|" + "---:|" * (len(tails) + 1)])
    for case in document["cases"]:
        values = [
            percentage(
                case["warmup_tail_coverage"][str(tail)]
                ["measurement_unique_lines_seen"]["fraction"]
            )
            for tail in tails
        ]
        values.append(
            percentage(
                case["measurement_unique_lines"]["same_core_warmup"]["fraction"]
            )
        )
        lines.append(
            f"| {case['cores']}c/{case['workload']} | "
            + " | ".join(values) + " |"
        )
    lines.extend(
        [
            "",
            "`full` is coverage by the union of all per-core user-functional "
            "warmup lines. Tail columns use the union of the last N warmup "
            "records on every core. High full coverage with poor PMU implies "
            "replacement/reuse modeling; low full coverage with substantially "
            "fewer gem5 misses implies state or prefetch activity absent from "
            "the user-functional trace.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.line_bytes <= 0 or args.line_bytes & (args.line_bytes - 1):
        raise SystemExit("--line-bytes must be a positive power of two")
    if args.page_bytes <= 0 or args.page_bytes % args.line_bytes:
        raise SystemExit("--page-bytes must be positive and divisible by line size")
    if args.chunk_records <= 0:
        raise SystemExit("--chunk-records must be positive")
    tails = sorted(set(args.tail_records or [10_000, 100_000, 1_000_000]))
    if any(value <= 0 for value in tails):
        raise SystemExit("--tail-records must be positive")
    audit = read_json(args.audit.resolve())
    source_cases = audit["cases"]
    if args.include_cores:
        included = set(args.include_cores)
        source_cases = [
            row for row in source_cases if int(row["cores"]) in included
        ]
        if not source_cases:
            raise SystemExit("--include-cores selected no audit cases")
    cases = [
        audit_case(
            row,
            args.accuracy_root.resolve() if args.accuracy_root else None,
            args.line_bytes,
            args.page_bytes,
            args.chunk_records,
            tails,
        )
        for row in sorted(
            source_cases, key=lambda item: (item["cores"], item["workload"])
        )
    ]
    document = {
        "schema": "fastsim-fst-warmup-cacheline-audit-v1",
        "source_audit": str(args.audit.resolve()),
        "accuracy_root": (
            str(args.accuracy_root.resolve()) if args.accuracy_root else None
        ),
        "line_bytes": args.line_bytes,
        "page_bytes": args.page_bytes,
        "include_cores": sorted(set(args.include_cores or [])),
        "tail_records": tails,
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    report = markdown(document)
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
