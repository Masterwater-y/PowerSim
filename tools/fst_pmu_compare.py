"""Compare PMU-like fields between TaoTrace and QEMU-FST FastSim replays.

The two producers write the canonical FST v7 wire (see include/fastsim/trace.hpp
and src/trace.cpp) and FastSim replays them through the same
configs/gem5-v28_1-fs-user.cfg. This tool checks that the v7 wire headers are
structurally compatible and then emits one table of absolute + relative
differences for every scope_metrics.pmu / memory_hierarchy_user / throughput
field defined by src/main.cpp's fastsim-stats-v5 emitter.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tools import fst_wire


# Fields owned by write_user_functional_pmu (src/main.cpp L348-393).
PMU_FIELDS: tuple[str, ...] = (
    "retired_instructions",
    "retired_uops",
    "memory_uops",
    "line_requests",
    "branches",
    "branch_misses",
    "l1d_accesses", "l1d_hits", "l1d_misses",
    "l1d_tag_accesses", "l1d_tag_hits", "l1d_tag_misses",
    "l2_accesses", "l2_hits", "l2_misses",
    "private_l2_tag_accesses", "private_l2_tag_hits", "private_l2_tag_misses",
    "llc_accesses", "llc_hits", "llc_misses",
    "llc_tag_accesses", "llc_tag_hits", "llc_tag_misses",
    "permission_upgrades", "remote_supplies",
    "llc_merged_misses", "llc_unique_fills",
    "dram_reads", "dram_writes",
    "dtlb_accesses", "dtlb_hits", "dtlb_misses",
)

# Fields owned by write_user_memory_hierarchy (src/main.cpp L477-503),
# excluding the coverage_scope tag and the boolean llc_outcomes_conserved.
MEMORY_HIERARCHY_FIELDS: tuple[str, ...] = (
    "shared_requests",
    "permission_upgrades", "remote_supplies",
    "llc_tag_accesses", "llc_tag_hits", "llc_tag_misses",
    "llc_merged_misses", "llc_unique_fills",
    "dram_reads", "dram_writes",
)

# Numeric fields under scope_metrics.throughput (src/main.cpp L915-925).
THROUGHPUT_FIELDS: tuple[str, ...] = (
    "user_uops_per_second",
    "end_to_end_user_uops_per_second",
)

# Numeric fields directly under scope_metrics (src/main.cpp L847-895).
SCOPE_FIELDS: tuple[str, ...] = (
    "user_trace_uops",
    "user_trace_instructions",
    "native_kernel_trace_uops",
    "native_kernel_trace_instructions",
    "sum_core_cycles",
    "cycles_per_user_uop",
    "cpi",
    "perf_like_cpi",
    "perf_like_cpi_denominator_instructions",
    "synthetic_kernel_active_cycles",
    "blocked_wall_cycles",
)


@dataclass(frozen=True)
class HeaderSummary:
    core_id: int
    records: int
    features: int
    syscall_metadata_count: int


def _read_stats(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as source:
        document = json.load(source)
    if document.get("schema") != "fastsim-stats-v5":
        raise ValueError(f"unexpected schema in {path}: {document.get('schema')!r}")
    if document.get("measurement_scope") != "user":
        raise ValueError(
            f"expected measurement_scope=user in {path}, got "
            f"{document.get('measurement_scope')!r}"
        )
    totals = document.get("totals")
    if not isinstance(totals, dict) or (
        totals.get("functional_warmup_enabled") is not True
    ):
        raise ValueError(f"functional warmup is not enabled in {path}")
    scope = document.get("scope_metrics")
    if not isinstance(scope, dict):
        raise ValueError(f"scope_metrics missing in {path}")
    return document


def _extract(scope: dict, section: str, keys: Iterable[str]) -> dict[str, float | int | None]:
    if section == "":
        source = scope
    else:
        source = scope.get(section)
        if not isinstance(source, dict):
            source = {}
    result: dict[str, float | int | None] = {}
    for key in keys:
        value = source.get(key)
        if value is None or isinstance(value, bool):
            result[key] = None
            continue
        if isinstance(value, (int, float)):
            result[key] = value
        else:
            result[key] = None
    return result


def _summarize_fst_dir(fst_dir: Path) -> list[HeaderSummary]:
    if not fst_dir.is_dir():
        raise FileNotFoundError(f"FST directory missing: {fst_dir}")
    summaries: list[HeaderSummary] = []
    for path in sorted(fst_dir.glob("core*.fst")):
        info = fst_wire.fst_info(path)
        summaries.append(
            HeaderSummary(
                core_id=info.core_id,
                records=info.records,
                features=info.features,
                syscall_metadata_count=info.syscall_metadata_count,
            )
        )
    if not summaries:
        raise FileNotFoundError(f"no coreN.fst files in {fst_dir}")
    return summaries


def _summarize_fst(run_root: Path) -> list[HeaderSummary]:
    return _summarize_fst_dir(run_root / "fst")


def _wire_compatible(a: list[HeaderSummary], b: list[HeaderSummary]) -> list[str]:
    errors: list[str] = []
    if len(a) != len(b):
        errors.append(f"core count differs: {len(a)} vs {len(b)}")
        return errors
    for left, right in zip(a, b):
        if left.core_id != right.core_id:
            errors.append(
                f"core_id mismatch: {left.core_id} vs {right.core_id}"
            )
        common = left.features & right.features
        required = fst_wire.FEATURE_DESTINATION_CLASSES
        if (common & required) != required:
            errors.append(
                f"core {left.core_id} lacks destination-class feature on both sides"
            )
    return errors


def _delta_row(name: str, a_value, b_value) -> dict:
    if a_value is None or b_value is None:
        return {
            "field": name,
            "qemu_fst": a_value,
            "taotrace": b_value,
            "delta": None,
            "delta_relative": None,
        }
    delta = b_value - a_value
    denom = a_value if a_value not in (0, 0.0) else None
    rel = (delta / denom) if denom else None
    return {
        "field": name,
        "qemu_fst": a_value,
        "taotrace": b_value,
        "delta": delta,
        "delta_relative": rel,
    }


def _compare_run(
    workload: str,
    qemu_root: Path,
    tao_fst_dir: Path,
    tao_stats: Path,
) -> dict:
    qemu_headers = _summarize_fst(qemu_root)
    tao_headers = _summarize_fst_dir(tao_fst_dir)
    wire_errors = _wire_compatible(qemu_headers, tao_headers)
    qemu_scope = _read_stats(qemu_root / "replay/stats.json")["scope_metrics"]
    tao_scope = _read_stats(tao_stats)["scope_metrics"]

    sections = {
        "scope": ("", SCOPE_FIELDS),
        "pmu": ("pmu", PMU_FIELDS),
        "memory_hierarchy_user": (
            "memory_hierarchy_user",
            MEMORY_HIERARCHY_FIELDS,
        ),
        "throughput": ("throughput", THROUGHPUT_FIELDS),
    }
    rows: dict[str, list[dict]] = {}
    for label, (key, fields) in sections.items():
        qemu_values = _extract(qemu_scope, key, fields)
        tao_values = _extract(tao_scope, key, fields)
        rows[label] = [
            _delta_row(name, qemu_values[name], tao_values[name])
            for name in fields
        ]
    return {
        "workload": workload,
        "status": "diagnostic_only",
        "wire_errors": wire_errors,
        "records": {
            "qemu_fst": sum(h.records for h in qemu_headers),
            "taotrace": sum(h.records for h in tao_headers),
        },
        "sections": rows,
    }


def _render_markdown(report: dict) -> str:
    lines: list[str] = [
        "# TaoTrace vs QEMU-FST PMU-like comparison",
        "",
        "Status: diagnostic_only. Differences are observations, not an "
        "acceptance gate or a QEMU correction target.",
        "",
    ]
    for entry in report["workloads"]:
        lines.append(f"## {entry['workload']}")
        lines.append("")
        if entry["wire_errors"]:
            lines.append("Wire compatibility errors:")
            for detail in entry["wire_errors"]:
                lines.append(f"- {detail}")
            lines.append("")
        r = entry["records"]
        lines.append(
            f"records: qemu_fst={r['qemu_fst']} taotrace={r['taotrace']}"
        )
        lines.append("")
        for section, rows in entry["sections"].items():
            lines.append(f"### {section}")
            lines.append("")
            lines.append("| field | qemu_fst | taotrace | Δ | Δ% |")
            lines.append("|---|---:|---:|---:|---:|")
            for row in rows:
                qv = row["qemu_fst"]
                tv = row["taotrace"]
                delta = row["delta"]
                rel = row["delta_relative"]
                rel_str = "" if rel is None else f"{rel * 100:.2f}%"
                delta_str = "" if delta is None else f"{delta}"
                lines.append(
                    f"| {row['field']} | {qv} | {tv} | {delta_str} | {rel_str} |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class TaoSource:
    """A resolved TaoTrace side for one workload: FST dir + FastSim report."""
    fst_dir: Path
    stats: Path


# origin/FastSim tools/run_fst_v7_formal_inference.py owns the P0 PMU contract.
PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"


def _inference_tao_sources(
    dataset: Path, inference: Path, cores: int
) -> dict[str, TaoSource]:
    """Map workload -> origin TaoTrace FST dir and FastSim report.

    ``dataset`` is the formal dataset root that build_fst_v7_formal_dataset.py
    writes (index.json + cases/<id>/tao_trace/coreN.fst). ``inference`` is the
    run_fst_v7_formal_inference.py output root (cases/<id>/user.json). Both use
    the case_id ``{cores:02d}c-{workload}``.
    """
    index = json.loads((dataset / "index.json").read_text(encoding="utf-8"))
    contract = index.get("oracle_validity", {}).get("pmu_contract_id")
    if contract != PMU_CONTRACT_ID:
        raise ValueError(
            f"dataset index lacks the P0 PMU contract identity: {dataset}"
        )
    sources: dict[str, TaoSource] = {}
    for case in index.get("cases", []):
        if int(case["cores"]) != cores:
            continue
        workload = str(case["workload"])
        case_id = f"{cores:02d}c-{workload}"
        sources[workload] = TaoSource(
            fst_dir=(dataset / "cases" / case_id / "tao_trace").resolve(),
            stats=(inference / "cases" / case_id / "user.json").resolve(),
        )
    return sources


def _symmetric_tao_source(tao_root: Path, workload: str) -> TaoSource:
    base = (tao_root / workload).resolve()
    return TaoSource(fst_dir=base / "fst", stats=base / "replay/stats.json")


def run(args) -> int:
    qemu_root = args.qemu_root.resolve()
    inference_mode = getattr(args, "taotrace_dataset", None) is not None
    if inference_mode:
        tao_sources = _inference_tao_sources(
            args.taotrace_dataset.resolve(),
            args.taotrace_inference.resolve(),
            getattr(args, "cores", 4),
        )
    else:
        tao_sources = None
        tao_root = args.taotrace_root.resolve()
    if args.workload:
        workloads = list(args.workload)
    elif inference_mode:
        workloads = sorted(tao_sources)
    else:
        workloads = sorted(
            path.name for path in qemu_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    if not workloads:
        print(f"no workloads found under {qemu_root}", file=sys.stderr)
        return 2
    report = {"workloads": []}
    missing: list[str] = []
    for name in workloads:
        q = qemu_root / name
        if inference_mode:
            tao = tao_sources.get(name)
            if tao is None:
                missing.append(f"taotrace: no case for {name}")
                continue
        else:
            tao = _symmetric_tao_source(tao_root, name)
        if not (q / "replay/stats.json").is_file():
            missing.append(f"qemu_fst: {q}")
            continue
        if not tao.stats.is_file():
            missing.append(f"taotrace: {tao.stats}")
            continue
        report["workloads"].append(
            _compare_run(name, q, tao.fst_dir, tao.stats)
        )
    if missing:
        for detail in missing:
            print(f"skipped: missing stats.json under {detail}", file=sys.stderr)
    args.output_root.mkdir(parents=True, exist_ok=True)
    (args.output_root / "pmu.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    (args.output_root / "pmu.md").write_text(
        _render_markdown(report), encoding="utf-8"
    )
    print(f"wrote {args.output_root / 'pmu.json'}")
    print(f"wrote {args.output_root / 'pmu.md'}")
    return 0 if not missing else 1
