"""Compare FastSim responses to QEMU-FST and TaoTrace-FST inputs.

The two producers write the canonical FST v7 wire (see include/fastsim/trace.hpp
and src/trace.cpp) and FastSim replays them through the same
configs/gem5-v28_1-fs-user.cfg. This tool checks that the v7 wire headers are
structurally compatible and then measures whether QEMU-FST can replace the
TaoTrace-FST reference input without changing FastSim's functional populations
or modeled timing/cache response. It emits one table of absolute + relative
differences for every scope_metrics.pmu / memory_hierarchy_user / throughput
field defined by src/main.cpp's fastsim-stats-v5 emitter.
"""
from __future__ import annotations

import json
import math
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

FUNCTIONAL_SCOPE_FIELDS: tuple[str, ...] = (
    "user_trace_uops",
    "user_trace_instructions",
    "native_kernel_trace_uops",
    "native_kernel_trace_instructions",
    "perf_like_cpi_denominator_instructions",
)
FUNCTIONAL_PMU_FIELDS: tuple[str, ...] = (
    "retired_instructions",
    "retired_uops",
    "memory_uops",
    "line_requests",
    "branches",
)
FRONTEND_SCOPE_FIELDS: tuple[str, ...] = (
    "sum_core_cycles",
    "cycles_per_user_uop",
    "cpi",
    "perf_like_cpi",
)
FRONTEND_PMU_FIELDS: tuple[str, ...] = ("branch_misses",)
PRODUCER_SENSITIVE_PMU_FIELDS: tuple[str, ...] = tuple(
    field
    for field in PMU_FIELDS
    if field not in FUNCTIONAL_PMU_FIELDS + FRONTEND_PMU_FIELDS
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
    delta = a_value - b_value
    denom = b_value if b_value not in (0, 0.0) else None
    rel = (delta / denom) if denom else None
    return {
        "field": name,
        "qemu_fst": a_value,
        "taotrace": b_value,
        "delta": delta,
        "delta_relative": rel,
    }


def _require_same_topology(
    workload: str, qemu_stats: dict, tao_stats: dict,
) -> None:
    qemu_config = qemu_stats.get("configuration")
    tao_config = tao_stats.get("configuration")
    if not isinstance(qemu_config, dict) or not isinstance(tao_config, dict):
        raise ValueError(f"{workload}: replay configuration is missing")
    fields = set(qemu_config) | set(tao_config)
    mismatches = [
        field for field in sorted(fields)
        if qemu_config.get(field) != tao_config.get(field)
    ]
    if mismatches:
        raise ValueError(
            f"{workload}: QEMU/TaoTrace replay topology differs: "
            f"{', '.join(mismatches)}"
        )


def _static_span_coverage(document: dict) -> dict:
    cores = document.get("cores")
    if not isinstance(cores, list) or not cores:
        raise ValueError("replay stats lack per-core static-span coverage")
    rows = []
    for core in cores:
        if not isinstance(core, dict):
            raise ValueError("invalid per-core replay stats")
        core_id = core.get("core")
        lookups = core.get("fetch_supply_static_span_lookups")
        unavailable = core.get("fetch_supply_static_span_unavailable")
        if (
            not isinstance(core_id, int)
            or not isinstance(lookups, int)
            or not isinstance(unavailable, int)
            or lookups < 0
            or unavailable < 0
        ):
            raise ValueError("invalid per-core static-span coverage")
        state = (
            "available"
            if lookups > 0
            else "unavailable"
            if unavailable > 0
            else "unused"
        )
        rows.append({
            "core": core_id,
            "lookups": lookups,
            "unavailable": unavailable,
            "state": state,
        })
    rows.sort(key=lambda row: row["core"])
    if [row["core"] for row in rows] != list(range(len(rows))):
        raise ValueError("replay stats have non-dense core identifiers")
    return {
        "cores": rows,
        "whole_core_available": [
            row["core"] for row in rows if row["state"] == "available"
        ],
        "whole_core_unavailable": [
            row["core"] for row in rows if row["state"] == "unavailable"
        ],
        "unused": [
            row["core"] for row in rows if row["state"] == "unused"
        ],
    }


def _static_span_comparability(
    workload: str, qemu_stats: dict, tao_stats: dict,
) -> dict:
    qemu = _static_span_coverage(qemu_stats)
    taotrace = _static_span_coverage(tao_stats)
    if len(qemu["cores"]) != len(taotrace["cores"]):
        raise ValueError(
            f"{workload}: static-span core count differs: "
            f"{len(qemu['cores'])} vs {len(taotrace['cores'])}"
        )
    asymmetric = [
        left["core"]
        for left, right in zip(qemu["cores"], taotrace["cores"])
        if left["state"] != right["state"]
    ]
    return {
        "qemu_fst": qemu,
        "taotrace": taotrace,
        "asymmetric_cores": asymmetric,
        "comparable": not asymmetric,
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
    if wire_errors:
        raise ValueError(
            f"{workload}: incompatible QEMU/TaoTrace FST inputs: "
            + "; ".join(wire_errors)
        )
    qemu_document = _read_stats(qemu_root / "replay/stats.json")
    tao_document = _read_stats(tao_stats)
    _require_same_topology(workload, qemu_document, tao_document)
    static_span = _static_span_comparability(
        workload, qemu_document, tao_document,
    )
    qemu_scope = qemu_document["scope_metrics"]
    tao_scope = tao_document["scope_metrics"]

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
        "status": (
            "diagnostic_only"
            if static_span["comparable"]
            else "frontend_reference_incomplete"
        ),
        "wire_errors": [],
        "static_span": static_span,
        "records": {
            "qemu_fst": sum(h.records for h in qemu_headers),
            "taotrace": sum(h.records for h in tao_headers),
        },
        "sections": rows,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _aggregate_field(
    workloads: list[dict], section: str, field: str,
) -> dict:
    pairs: list[tuple[str, float, float]] = []
    missing_cases = 0
    for workload in workloads:
        row = next(
            item for item in workload["sections"][section]
            if item["field"] == field
        )
        candidate = row["qemu_fst"]
        reference = row["taotrace"]
        if candidate is None or reference is None:
            missing_cases += 1
            continue
        pairs.append(
            (workload["workload"], float(candidate), float(reference))
        )

    relative: list[tuple[str, float]] = []
    both_zero_cases = 0
    undefined_relative_cases = 0
    for workload, candidate, reference in pairs:
        if reference == 0.0:
            if candidate == 0.0:
                both_zero_cases += 1
                relative.append((workload, 0.0))
            else:
                undefined_relative_cases += 1
            continue
        relative.append(
            (workload, abs(candidate - reference) / abs(reference))
        )

    reference_total = sum(reference for _, _, reference in pairs)
    candidate_total = sum(candidate for _, candidate, _ in pairs)
    absolute_error_total = sum(
        abs(candidate - reference) for _, candidate, reference in pairs
    )
    reference_magnitude = sum(abs(reference) for _, _, reference in pairs)
    worst = max(relative, key=lambda item: item[1]) if relative else None
    errors = [value for _, value in relative]
    return {
        "field": field,
        "cases": len(pairs),
        "finite_relative_cases": len(relative),
        "missing_cases": missing_cases,
        "both_zero_cases": both_zero_cases,
        "undefined_relative_cases": undefined_relative_cases,
        "qemu_fst_total": candidate_total,
        "taotrace_total": reference_total,
        "mape": sum(errors) / len(errors) if errors else None,
        "p50_ape": _percentile(errors, 0.50),
        "p90_ape": _percentile(errors, 0.90),
        "p99_ape": _percentile(errors, 0.99),
        "wape": (
            absolute_error_total / reference_magnitude
            if reference_magnitude else None
        ),
        "signed_aggregate": (
            (candidate_total - reference_total) / reference_magnitude
            if reference_magnitude else None
        ),
        "worst_workload": worst[0] if worst else None,
        "worst_ape": worst[1] if worst else None,
    }


def _aggregate_domain(
    workloads: list[dict],
    fields: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[dict]:
    rows = []
    for section, names in fields:
        for field in names:
            row = _aggregate_field(workloads, section, field)
            row["source_section"] = section
            rows.append(row)
    return rows


def _aggregate_report(workloads: list[dict]) -> dict[str, dict]:
    frontend = [
        workload for workload in workloads
        if workload.get("static_span", {}).get("comparable", True)
    ]
    domains = {
        "functional_population": {
            "status": "diagnostic_only",
            "workloads": [workload["workload"] for workload in workloads],
            "fields": _aggregate_domain(
                workloads,
                (
                    ("scope", FUNCTIONAL_SCOPE_FIELDS),
                    ("pmu", FUNCTIONAL_PMU_FIELDS),
                ),
            ),
        },
        "frontend_timing": {
            "status": "requires_static_instruction_coverage",
            "workloads": [workload["workload"] for workload in frontend],
            "fields": _aggregate_domain(
                frontend,
                (
                    ("scope", FRONTEND_SCOPE_FIELDS),
                    ("pmu", FRONTEND_PMU_FIELDS),
                ),
            ),
        },
        "producer_sensitive_memory": {
            "status": "producer_sensitive_diagnostic",
            "workloads": [workload["workload"] for workload in workloads],
            "fields": _aggregate_domain(
                workloads,
                (
                    ("pmu", PRODUCER_SENSITIVE_PMU_FIELDS),
                    ("memory_hierarchy_user", MEMORY_HIERARCHY_FIELDS),
                ),
            ),
        },
        "host_throughput": {
            "status": "host_only",
            "workloads": [workload["workload"] for workload in workloads],
            "fields": _aggregate_domain(
                workloads, (("throughput", THROUGHPUT_FIELDS),)
            ),
        },
    }
    return domains


def _percent(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.2f}%"


def _render_markdown(report: dict) -> str:
    lines: list[str] = [
        "# QEMU-FST replacement consistency against TaoTrace-FST",
        "",
        "Status: diagnostic_only. TaoTrace-FST is the reference functional "
        "input and QEMU-FST is the candidate replacement. Both are replayed "
        "by the same FastSim configuration; this report does not compare "
        "against gem5 timing-oracle outputs.",
        "",
        "Delta convention: `QEMU-FST - TaoTrace-FST`; relative error uses "
        "TaoTrace-FST as the reference denominator.",
        "",
        "Coverage: requested "
        f"{len(report['coverage']['requested'])}, functional compared "
        f"{len(report['coverage']['functional_compared'])}, frontend compared "
        f"{len(report['coverage']['frontend_compared'])}, reference unavailable "
        f"{len(report['coverage']['reference_unavailable'])}, reference "
        "frontend incomplete "
        f"{len(report['coverage']['frontend_reference_incomplete'])}, "
        "missing "
        f"{len(report['coverage']['missing'])}.",
        "",
        "## Aggregate field gaps",
        "",
    ]
    for domain, aggregate in report["aggregate"].items():
        lines.extend(
            [
                f"### {domain}",
                "",
                f"status: {aggregate['status']}",
                "",
                "workloads: " + ", ".join(aggregate["workloads"]),
                "",
                "| field | finite/total | MAPE | P50 | P90 | P99 | WAPE | "
                "signed aggregate | worst workload | worst APE |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|",
            ]
        )
        for row in aggregate["fields"]:
            lines.append(
                f"| {row['source_section']}.{row['field']} | "
                f"{row['finite_relative_cases']}/"
                f"{row['cases']} | {_percent(row['mape'])} | "
                f"{_percent(row['p50_ape'])} | {_percent(row['p90_ape'])} | "
                f"{_percent(row['p99_ape'])} | {_percent(row['wape'])} | "
                f"{_percent(row['signed_aggregate'])} | "
                f"{row['worst_workload'] or ''} | "
                f"{_percent(row['worst_ape'])} |"
            )
        lines.extend(
            [
                "",
                "Both-zero cases contribute zero relative error. Cases with "
                "a zero TaoTrace reference and nonzero QEMU value are excluded "
                "from relative percentiles and counted in JSON as "
                "`undefined_relative_cases`.",
                "",
            ]
        )
    lines.append("## Per-workload details")
    lines.append("")
    for entry in report["workloads"]:
        lines.append(f"### {entry['workload']}")
        lines.append("")
        lines.append(f"status: {entry['status']}")
        lines.append("")
        if entry["status"] == "frontend_reference_incomplete":
            cores = ", ".join(
                str(core) for core in entry["static_span"]["asymmetric_cores"]
            )
            lines.append(
                "Excluded only from frontend/timing aggregates because "
                "whole-core static instruction-span availability differs on "
                f"core(s): {cores}."
            )
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
            lines.append(f"#### {section}")
            lines.append("")
            lines.append(
                "| field | qemu_fst | taotrace reference | QEMU - TaoTrace | "
                "relative to TaoTrace |"
            )
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
    report = {
        "schema": "fastsim-fst-replacement-consistency-v1",
        "status": "diagnostic_only",
        "reference": "taotrace-fst",
        "candidate": "qemu-fst",
        "delta_convention": "qemu_fst_minus_taotrace",
        "relative_denominator": "taotrace",
        "coverage": {
            "requested": list(workloads),
            "functional_compared": [],
            "frontend_compared": [],
            "reference_unavailable": [],
            "frontend_reference_incomplete": [],
            "missing": [],
        },
        "workloads": [],
    }
    missing: list[str] = []
    unavailable = set(getattr(args, "reference_unavailable", ()))
    for name in workloads:
        q = qemu_root / name
        if inference_mode:
            tao = tao_sources.get(name)
            if tao is None:
                if name in unavailable:
                    report["coverage"]["reference_unavailable"].append(name)
                else:
                    missing.append(f"taotrace: no case for {name}")
                continue
        else:
            tao = _symmetric_tao_source(tao_root, name)
        if not (q / "replay/stats.json").is_file():
            missing.append(f"qemu_fst: {q}")
            continue
        if not tao.stats.is_file():
            if name in unavailable:
                report["coverage"]["reference_unavailable"].append(name)
            else:
                missing.append(f"taotrace: {tao.stats}")
            continue
        comparison = _compare_run(name, q, tao.fst_dir, tao.stats)
        report["workloads"].append(comparison)
        report["coverage"]["functional_compared"].append(name)
        if comparison["status"] == "frontend_reference_incomplete":
            report["coverage"]["frontend_reference_incomplete"].append(name)
        else:
            report["coverage"]["frontend_compared"].append(name)
    report["aggregate"] = _aggregate_report(report["workloads"])
    report["coverage"]["missing"] = list(missing)
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
