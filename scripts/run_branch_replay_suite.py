#!/usr/bin/env python3
"""Run the functional-only branch predictor replay over a manifest in parallel."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import traceback
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tcsim.branch_replay import (  # noqa: E402
    ReplayConfig,
    attach_v29_meta_evaluation,
    discover_aligned_files,
    iter_aligned_events,
    replay_core_streams,
)
from tcsim.utils.io import dump_json, ensure_dir, load_json  # noqa: E402


SCHEMA_VERSION = "tcsim-branch-replay-suite-1"
DEFAULT_SPLITS = "deployment_inference"
DEFAULT_CORE_COUNTS = "4,8,16,32"


def _csv_values(raw: str) -> list[str]:
    return [value.strip() for value in str(raw).split(",") if value.strip()]


def _int_values(raw: str) -> list[int]:
    return [int(value) for value in _csv_values(raw)]


def _safe_name(row: Mapping[str, Any]) -> str:
    workload = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row["workload"]))
    return f"seed{int(row['seed'])}_c{int(row['n_cores']):02d}_{workload}.json"


def select_traces(
    manifest: Mapping[str, Any],
    *,
    splits: Sequence[str],
    core_counts: set[int],
    workloads: set[str] | None = None,
    roles: set[str] | None = None,
    seeds: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Select and de-duplicate manifest cells without pooling split aliases."""
    available = manifest.get("splits", {})
    missing = [split for split in splits if split not in available]
    if missing:
        raise ValueError(f"manifest does not contain splits: {missing}")
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for source in available[split]:
            row = dict(source)
            if int(row.get("n_cores", -1)) not in core_counts:
                continue
            if workloads is not None and str(row.get("workload")) not in workloads:
                continue
            if roles is not None and str(row.get("workload_role")) not in roles:
                continue
            if seeds is not None and int(row.get("seed", -1)) not in seeds:
                continue
            trace_id = str(row.get("trace_id", ""))
            if not trace_id:
                raise ValueError(f"manifest row lacks trace_id: {row}")
            if trace_id in selected:
                previous = selected[trace_id]
                for key in ("trace_dir", "cache_dir", "workload", "seed", "n_cores"):
                    if previous.get(key) != row.get(key):
                        raise ValueError(
                            f"conflicting duplicate trace_id={trace_id!r} field={key}"
                        )
                previous["selected_splits"].append(split)
                continue
            row["selected_splits"] = [split]
            selected[trace_id] = row
    return sorted(
        selected.values(),
        key=lambda row: (
            int(row["seed"]), int(row["n_cores"]), str(row["workload"])
        ),
    )


def _run_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    trace_dir = os.path.abspath(str(row["trace_dir"]))
    meta_path = os.path.join(os.path.abspath(str(row["cache_dir"])), "meta.json")
    meta = load_json(meta_path)
    config = ReplayConfig.from_mapping(meta)
    streams = [
        (core_id, iter_aligned_events(path))
        for core_id, path in discover_aligned_files(trace_dir)
    ]
    report = replay_core_streams(streams, config)
    attach_v29_meta_evaluation(report, meta)
    return {
        "schema_version": SCHEMA_VERSION,
        "trace": {
            "trace_id": str(row["trace_id"]),
            "workload": str(row["workload"]),
            "workload_role": str(row.get("workload_role", "unknown")),
            "seed": int(row["seed"]),
            "n_cores": int(row["n_cores"]),
            "selected_splits": list(row.get("selected_splits", [])),
            "trace_dir": trace_dir,
            "evaluation_meta": meta_path,
        },
        "replay": report,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _summary_row(detail: Mapping[str, Any], detail_path: str) -> dict[str, Any]:
    trace = detail["trace"]
    replay = detail["replay"]
    predicted = int(replay["predicted_misses"])
    true = int(replay["true_misses"])
    predicted_rate = float(replay["predicted_rate"])
    true_rate = float(replay["true_rate"])
    return {
        "trace_id": str(trace["trace_id"]),
        "workload": str(trace["workload"]),
        "workload_role": str(trace["workload_role"]),
        "seed": int(trace["seed"]),
        "n_cores": int(trace["n_cores"]),
        "branches": int(replay["branches"]),
        "replay_misses": predicted,
        "gem5_misses": true,
        "miss_count_signed_error": predicted - true,
        "miss_count_abs_error": abs(predicted - true),
        "miss_count_abs_relative_error": float(
            replay["miss_count_abs_relative_error"]
        ),
        "replay_miss_rate": predicted_rate,
        "gem5_miss_rate": true_rate,
        "miss_rate_signed_error_pp": (predicted_rate - true_rate) * 100.0,
        "miss_rate_abs_error_pp": float(replay["miss_rate_abs_error_pp"]),
        "conditional_direction_misses": int(
            replay["conditional_direction_misses"]
        ),
        "target_side_misses": int(replay["target_side_misses"]),
        "target_unavailable_misses": int(replay["target_unavailable_misses"]),
        "functional_history_mismatches": int(
            replay["functional_history_mismatches"]
        ),
        "elapsed_seconds": float(detail["elapsed_seconds"]),
        "detail_report": os.path.abspath(detail_path),
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    branches = sum(int(row["branches"]) for row in rows)
    predicted = sum(int(row["replay_misses"]) for row in rows)
    true = sum(int(row["gem5_misses"]) for row in rows)
    count_errors = [float(row["miss_count_abs_relative_error"]) for row in rows]
    rate_errors = [float(row["miss_rate_abs_error_pp"]) for row in rows]
    replay_rate = predicted / branches if branches else float("nan")
    true_rate = true / branches if branches else float("nan")
    return {
        "n_traces": len(rows),
        "branches": branches,
        "replay_misses": predicted,
        "gem5_misses": true,
        "trace_equal_count_mape": sum(count_errors) / len(rows) if rows else float("nan"),
        "trace_equal_count_p50": _percentile(count_errors, 50),
        "trace_equal_count_p90": _percentile(count_errors, 90),
        "trace_equal_rate_mae_pp": sum(rate_errors) / len(rows) if rows else float("nan"),
        "pooled_count_abs_relative_error": abs(predicted - true) / max(1, true),
        "pooled_count_signed_bias": (predicted - true) / max(1, true),
        "pooled_replay_miss_rate": replay_rate,
        "pooled_gem5_miss_rate": true_rate,
        "pooled_rate_abs_error_pp": abs(replay_rate - true_rate) * 100.0,
        "functional_history_mismatches": sum(
            int(row["functional_history_mismatches"]) for row in rows
        ),
    }


def _grouped(
    rows: Sequence[Mapping[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return {
        name: aggregate_rows(items)
        for name, items in sorted(groups.items(), key=lambda item: item[0])
    }


def _write_tsv(path: str, rows: Sequence[Mapping[str, Any]]) -> None:
    columns = (
        "seed", "n_cores", "workload_role", "workload", "branches",
        "replay_misses", "gem5_misses", "miss_count_signed_error",
        "miss_count_abs_relative_error", "replay_miss_rate", "gem5_miss_rate",
        "miss_rate_signed_error_pp", "miss_rate_abs_error_pp",
        "conditional_direction_misses", "target_side_misses",
        "target_unavailable_misses", "functional_history_mismatches",
        "elapsed_seconds", "trace_id", "detail_report",
    )
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def _pct(value: float) -> str:
    return f"{100.0 * float(value):.3f}%"


def _summary_table(groups: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = [
        "| group | traces | branches | replay/gem5 miss | trace-equal count MAPE | trace-equal rate MAE | pooled count error | pooled rate error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in groups.items():
        lines.append(
            f"| {name} | {item['n_traces']} | {item['branches']:,} | "
            f"{item['replay_misses']:,}/{item['gem5_misses']:,} | "
            f"{_pct(item['trace_equal_count_mape'])} | "
            f"{item['trace_equal_rate_mae_pp']:.4f} pp | "
            f"{_pct(item['pooled_count_abs_relative_error'])} | "
            f"{item['pooled_rate_abs_error_pp']:.4f} pp |"
        )
    return lines


def _write_markdown(
    path: str,
    report: Mapping[str, Any],
) -> None:
    selection = report["selection"]
    lines = [
        "# Branch predictor replay c04-c32 CPU validation",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Manifest: `{selection['manifest']}`",
        f"- Splits: `{','.join(selection['splits'])}`",
        f"- Core counts: `{','.join(map(str, selection['core_counts']))}`",
        f"- Jobs: `{selection['jobs']}`",
        f"- Completed/selected/failed: `{len(report['traces'])}/{selection['n_selected']}/{len(report['failures'])}`",
        "- GPU/checkpoint/gem5 runtime: not used",
        "",
        "## Overall",
        "",
        *_summary_table({"all": report["aggregate"]["all"]}),
        "",
        "## By core count",
        "",
        *_summary_table(report["aggregate"]["by_core_count"]),
        "",
        "## By workload role",
        "",
        *_summary_table(report["aggregate"]["by_workload_role"]),
        "",
        "## By seed",
        "",
        *_summary_table(report["aggregate"]["by_seed"]),
        "",
        "## Worst traces by count relative error",
        "",
        "| seed | cores | role | workload | replay/gem5 miss | count error | rate error |",
        "|---:|---:|---|---|---:|---:|---:|",
    ]
    worst = sorted(
        report["traces"],
        key=lambda row: float(row["miss_count_abs_relative_error"]),
        reverse=True,
    )[:20]
    for row in worst:
        lines.append(
            f"| {row['seed']} | {row['n_cores']} | {row['workload_role']} | "
            f"{row['workload']} | {row['replay_misses']:,}/{row['gem5_misses']:,} | "
            f"{_pct(row['miss_count_abs_relative_error'])} | "
            f"{row['miss_rate_abs_error_pp']:.4f} pp |"
        )
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        for failure in report["failures"]:
            lines.append(
                f"- `{failure['trace_id']}`: `{failure['error_type']}: "
                f"{failure['error']}`"
            )
    lines.extend([
        "",
        "## Metric semantics",
        "",
        "- `trace-equal count MAPE`: each trace has equal weight; per-trace denominator is `max(1, gem5_misses)`.",
        "- `trace-equal rate MAE`: mean absolute replay-vs-gem5 miss-rate difference in percentage points.",
        "- `pooled`: sums counts and branch opportunities first; inspect it together with trace-equal metrics because positive and negative workload errors can cancel.",
        "- gem5 labels are attached only after replay and never participate in predictor state transitions.",
    ])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _arguments() -> argparse.Namespace:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=os.path.join(REPO_ROOT, "data/v29_global_time_dataset/manifest.json")
    )
    parser.add_argument("--splits", default=DEFAULT_SPLITS)
    parser.add_argument("--core-counts", default=DEFAULT_CORE_COUNTS)
    parser.add_argument("--jobs", type=int, default=min(64, os.cpu_count() or 1))
    parser.add_argument(
        "--out",
        default=os.path.join(REPO_ROOT, "logs", f"branch_replay_c04_c32_{timestamp}"),
    )
    parser.add_argument("--workloads", help="optional comma-separated exact workload names")
    parser.add_argument("--roles", help="optional comma-separated workload roles")
    parser.add_argument("--seeds", help="optional comma-separated seeds")
    parser.add_argument("--max-traces", type=int, default=0, help="smoke-test limit")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.jobs <= 0:
        raise ValueError("--jobs must be positive")
    manifest_path = os.path.abspath(args.manifest)
    out_dir = os.path.abspath(args.out)
    detail_dir = os.path.join(out_dir, "traces")
    ensure_dir(detail_dir)
    manifest = load_json(manifest_path)
    splits = _csv_values(args.splits)
    core_counts = set(_int_values(args.core_counts))
    rows = select_traces(
        manifest,
        splits=splits,
        core_counts=core_counts,
        workloads=set(_csv_values(args.workloads)) if args.workloads else None,
        roles=set(_csv_values(args.roles)) if args.roles else None,
        seeds=set(_int_values(args.seeds)) if args.seeds else None,
    )
    if args.max_traces > 0:
        rows = rows[: args.max_traces]
    if not rows:
        raise RuntimeError("selection produced no traces")

    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in rows:
        detail_path = os.path.join(detail_dir, _safe_name(row))
        if args.resume and os.path.isfile(detail_path):
            try:
                detail = load_json(detail_path)
                if (
                    detail.get("schema_version") == SCHEMA_VERSION
                    and detail.get("trace", {}).get("trace_id") == row["trace_id"]
                ):
                    summaries.append(_summary_row(detail, detail_path))
                    continue
            except Exception:
                pass
        pending.append(row)

    print(
        f"[branch-replay-suite] selected={len(rows)} resume_hits={len(summaries)} "
        f"pending={len(pending)} jobs={args.jobs} cores={sorted(core_counts)}",
        flush=True,
    )
    started = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        future_rows = {pool.submit(_run_trace, row): row for row in pending}
        completed = len(summaries)
        for future in concurrent.futures.as_completed(future_rows):
            row = future_rows[future]
            detail_path = os.path.join(detail_dir, _safe_name(row))
            try:
                detail = future.result()
                dump_json(detail_path, detail)
                summary = _summary_row(detail, detail_path)
                summaries.append(summary)
                completed += 1
                print(
                    f"[branch-replay-suite] {completed}/{len(rows)} ok "
                    f"seed={summary['seed']} c{summary['n_cores']:02d} "
                    f"workload={summary['workload']} "
                    f"count_err={100.0 * summary['miss_count_abs_relative_error']:.3f}% "
                    f"rate_err={summary['miss_rate_abs_error_pp']:.4f}pp",
                    flush=True,
                )
            except Exception as exc:
                completed += 1
                failure = {
                    "trace_id": str(row["trace_id"]),
                    "workload": str(row["workload"]),
                    "seed": int(row["seed"]),
                    "n_cores": int(row["n_cores"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                }
                failures.append(failure)
                print(
                    f"[branch-replay-suite][ERROR] {completed}/{len(rows)} "
                    f"seed={row['seed']} c{int(row['n_cores']):02d} "
                    f"workload={row['workload']}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    summaries.sort(
        key=lambda row: (int(row["seed"]), int(row["n_cores"]), str(row["workload"]))
    )
    aggregate = {
        "all": aggregate_rows(summaries),
        "by_core_count": _grouped(summaries, "n_cores"),
        "by_workload_role": _grouped(summaries, "workload_role"),
        "by_seed": _grouped(summaries, "seed"),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "splits": splits,
            "core_counts": sorted(core_counts),
            "jobs": int(args.jobs),
            "n_selected": len(rows),
            "workloads": _csv_values(args.workloads) if args.workloads else None,
            "roles": _csv_values(args.roles) if args.roles else None,
            "seeds": _int_values(args.seeds) if args.seeds else None,
        },
        "runtime_seconds": time.perf_counter() - started,
        "aggregate": aggregate,
        "traces": summaries,
        "failures": sorted(
            failures,
            key=lambda row: (row["seed"], row["n_cores"], row["workload"]),
        ),
        "oracle_labels_consumed_as_input": False,
        "gpu_used": False,
    }
    json_path = os.path.join(out_dir, "report.json")
    tsv_path = os.path.join(out_dir, "traces.tsv")
    markdown_path = os.path.join(out_dir, "summary.md")
    dump_json(json_path, report)
    _write_tsv(tsv_path, summaries)
    _write_markdown(markdown_path, report)
    overall = aggregate["all"]
    print(
        f"[branch-replay-suite] complete={len(summaries)}/{len(rows)} "
        f"failures={len(failures)} "
        f"count_mape={100.0 * overall['trace_equal_count_mape']:.3f}% "
        f"rate_mae={overall['trace_equal_rate_mae_pp']:.4f}pp",
        flush=True,
    )
    print(
        f"[branch-replay-suite] json={json_path} tsv={tsv_path} summary={markdown_path}",
        flush=True,
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
