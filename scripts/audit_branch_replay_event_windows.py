#!/usr/bin/env python3
"""Audit seed1 branch replay at event and fixed-UOP-window granularity."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import os
import sys
import time
import traceback
from typing import Any, Mapping, Sequence


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (REPO_ROOT, SCRIPT_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from run_branch_replay_suite import (  # noqa: E402
    _csv_values,
    _int_values,
    _safe_name,
    select_traces,
)
from tcsim.branch_replay import (  # noqa: E402
    ReplayConfig,
    aggregate_audit_reports,
    audit_trace,
)
from tcsim.utils.io import dump_json, ensure_dir, load_json  # noqa: E402


SCHEMA_VERSION = "tcsim-branch-replay-event-window-audit-1"


def _run_trace(
    row: Mapping[str, Any], window_sizes: Sequence[int], cold_branches: int
) -> dict[str, Any]:
    started = time.perf_counter()
    cache_dir = os.path.abspath(str(row["cache_dir"]))
    trace_dir = os.path.abspath(str(row["trace_dir"]))
    meta_path = os.path.join(cache_dir, "meta.json")
    meta = load_json(meta_path)
    config = ReplayConfig.from_mapping(meta)
    audit = audit_trace(
        trace_dir,
        cache_dir,
        config,
        window_sizes=window_sizes,
        cold_branches=cold_branches,
    )
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
            "cache_dir": cache_dir,
        },
        "audit": audit,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _trace_summary(detail: Mapping[str, Any], detail_path: str) -> dict[str, Any]:
    trace = detail["trace"]
    audit = detail["audit"]
    return {
        **trace,
        "alignment_passed": bool(audit["alignment"]["passed"]),
        "event": dict(audit["event"]),
        "windows": {name: dict(value) for name, value in audit["windows"].items()},
        "elapsed_seconds": float(detail["elapsed_seconds"]),
        "detail_report": os.path.abspath(detail_path),
    }


def _aggregate_group(
    details: Sequence[Mapping[str, Any]], key: str
) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for detail in details:
        groups.setdefault(str(detail["trace"][key]), []).append(detail["audit"])
    return {
        name: aggregate_audit_reports(items)
        for name, items in sorted(groups.items(), key=lambda item: item[0])
    }


def _pct(value: Any, digits: int = 3) -> str:
    return f"{100.0 * float(value):.{digits}f}%"


def _float(value: Any, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}"


def _event_table(groups: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = [
        "| group | branches | TP | FP | FN | precision | recall | F1 | event mismatch | count error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, audit in groups.items():
        item = audit["event"]
        lines.append(
            f"| {name} | {item['events']:,} | {item['tp']:,} | {item['fp']:,} | "
            f"{item['fn']:,} | {_pct(item['precision'])} | {_pct(item['recall'])} | "
            f"{_pct(item['f1'])} | {_pct(item['event_mismatch_rate'])} | "
            f"{_pct(item['count_abs_relative_error'])} |"
        )
    return lines


def _window_table(
    groups: Mapping[str, Mapping[str, Any]], size: str
) -> list[str]:
    lines = [
        "| group | branch windows | count MAE/window | normalized L1 | exact | within ±1 | rate MAE | Pearson | signed bias/window |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, audit in groups.items():
        item = audit["windows"][size]
        lines.append(
            f"| {name} | {item['branch_windows']:,} | "
            f"{_float(item['count_mae_per_branch_window'])} | "
            f"{_pct(item['normalized_count_l1'])} | "
            f"{_pct(item['exact_match_fraction'])} | "
            f"{_pct(item['within_one_fraction'])} | "
            f"{_float(item['rate_mae_pp'])} pp | "
            f"{_float(item['count_pearson'])} | "
            f"{_float(item['signed_count_bias_per_branch_window'])} |"
        )
    return lines


def _write_markdown(path: str, report: Mapping[str, Any]) -> None:
    aggregate = report["aggregate"]
    overall = aggregate["all"]
    selection = report["selection"]
    primary_groups = {
        "all": overall,
        **{
            f"role:{name}": value
            for name, value in aggregate["by_workload_role"].items()
        },
    }
    neural_references = {
        "All": ("all", 30.56, 1.56),
        "Train/base": ("train_base", 11.30, 0.33),
        "Heldout": ("business_heldout", 74.56, 4.38),
    }
    comparator_rows = []
    for label, (role, neural_count, neural_rate) in neural_references.items():
        audit = (
            overall if role == "all"
            else aggregate["by_workload_role"].get(role)
        )
        if audit is None:
            continue
        event = audit["event"]
        replay_rate_error_pp = abs(
            event["predicted_misses"] / event["events"]
            - event["true_misses"] / event["events"]
        ) * 100.0
        comparator_rows.append(
            f"| {label} | {neural_count:.2f}% / {neural_rate:.2f} pp | "
            f"{_pct(event['count_abs_relative_error'])} / "
            f"{_float(replay_rate_error_pp)} pp |"
        )
    lines = [
        "# Branch replay event/window audit",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Manifest: `{selection['manifest']}`",
        f"- Splits: `{','.join(selection['splits'])}`",
        f"- Core counts: `{','.join(map(str, selection['core_counts']))}`",
        f"- Completed/selected/failed: `{len(report['traces'])}/{selection['n_selected']}/{len(report['failures'])}`",
        f"- CPU workers: `{selection['jobs']}`; GPU/checkpoint/gem5 runtime: not used",
        f"- Fixed UOP windows: `{','.join(map(str, selection['window_sizes_uops']))}`",
        "- Oracle isolation: `mispredicted` is read from the v29 cache only after replay `process()` returns.",
        "",
        "## Layer 1: alignment",
        "",
        f"- Passed: `{overall['alignment']['passed']}`",
        f"- Cores passed: `{overall['alignment']['passed_cores']}/{overall['alignment']['cores']}`",
        f"- Raw/cache branches: `{overall['alignment']['raw_branches']:,}/{overall['alignment']['cache_branches']:,}`",
        f"- PC/kind/taken/history mismatches: `{overall['alignment']['pc_mismatches']}/{overall['alignment']['kind_mismatches']}/{overall['alignment']['taken_mismatches']}/{overall['alignment']['functional_history_mismatches']}`",
        "",
        "## Layer 2: event accuracy",
        "",
        *_event_table(primary_groups),
        "",
        "### Event accuracy by core count",
        "",
        *_event_table(aggregate["by_core_count"]),
    ]
    for size in map(str, selection["window_sizes_uops"]):
        lines.extend([
            "",
            f"## Fixed {size}-UOP windows",
            "",
            *_window_table(primary_groups, size),
            "",
            f"### {size}-UOP windows by core count",
            "",
            *_window_table(aggregate["by_core_count"], size),
        ])
    lines.extend([
        "",
        "## Cold versus steady-state events",
        "",
        *_event_table({
            name: {"event": values}
            for name, values in overall["by_segment"].items()
        }),
        "",
        "## Aggregate-only historical neural comparator",
        "",
        "| set | neural count/rate error | full replay count/rate error |",
        "|---|---:|---:|",
        *comparator_rows,
        "",
        "Neural values above are only the existing aggregate comparator; the previous inference report did not retain per-event neural probabilities, so event F1 and fixed-window neural comparison are unavailable without rerunning model inference.",
        "",
        "## Worst traces by event mismatch",
        "",
        "| cores | role | workload | FP | FN | F1 | mismatch | detail |",
        "|---:|---|---|---:|---:|---:|---:|---|",
    ])
    worst = sorted(
        report["traces"],
        key=lambda row: float(row["event"]["event_mismatch_rate"]),
        reverse=True,
    )[:20]
    for row in worst:
        event = row["event"]
        lines.append(
            f"| {row['n_cores']} | {row['workload_role']} | {row['workload']} | "
            f"{event['fp']:,} | {event['fn']:,} | {_pct(event['f1'])} | "
            f"{_pct(event['event_mismatch_rate'])} | `{row['detail_report']}` |"
        )
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        for failure in report["failures"]:
            lines.append(
                f"- `{failure['trace_id']}`: `{failure['error_type']}: {failure['error']}`"
            )
    lines.extend([
        "",
        "## Metric semantics",
        "",
        "- Event F1 exposes FP/FN cancellation hidden by aggregate count error.",
        "- Window metrics use non-overlapping per-core functional UOP windows and only branch-containing windows for MAE/rate/exact fractions.",
        "- `normalized L1 = sum(abs(predicted_count-true_count))/sum(true_count)`.",
        "- These windows test whether replay provides correctly located inputs; they are not free-running scheduler windows, whose boundaries depend on model timing predictions.",
    ])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _arguments() -> argparse.Namespace:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=os.path.join(REPO_ROOT, "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument("--splits", default="deployment_inference")
    parser.add_argument("--core-counts", default="4,8,16,32")
    parser.add_argument("--jobs", type=int, default=min(64, os.cpu_count() or 1))
    parser.add_argument("--window-sizes", default="256,1024")
    parser.add_argument("--cold-branches", type=int, default=4096)
    parser.add_argument(
        "--out",
        default=os.path.join(
            REPO_ROOT, "logs", f"branch_replay_event_window_seed1_{timestamp}"
        ),
    )
    parser.add_argument("--workloads")
    parser.add_argument("--roles")
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.jobs <= 0 or args.cold_branches < 0:
        raise ValueError("jobs must be positive and cold-branches non-negative")
    window_sizes = tuple(_int_values(args.window_sizes))
    if not window_sizes or any(value <= 0 for value in window_sizes):
        raise ValueError("window sizes must be positive")
    manifest_path = os.path.abspath(args.manifest)
    out_dir = os.path.abspath(args.out)
    detail_dir = os.path.join(out_dir, "traces")
    ensure_dir(detail_dir)
    splits = _csv_values(args.splits)
    core_counts = set(_int_values(args.core_counts))
    rows = select_traces(
        load_json(manifest_path),
        splits=splits,
        core_counts=core_counts,
        workloads=set(_csv_values(args.workloads)) if args.workloads else None,
        roles=set(_csv_values(args.roles)) if args.roles else None,
        seeds={1},
    )
    if args.max_traces > 0:
        rows = rows[: args.max_traces]
    if not rows:
        raise RuntimeError("selection produced no seed1 traces")

    details: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    pending = []
    for row in rows:
        path = os.path.join(detail_dir, _safe_name(row))
        if args.resume and os.path.isfile(path):
            try:
                detail = load_json(path)
                if (
                    detail.get("schema_version") == SCHEMA_VERSION
                    and detail.get("trace", {}).get("trace_id") == row["trace_id"]
                    and tuple(detail.get("audit", {}).get("window_sizes_uops", [])) == window_sizes
                ):
                    details.append(detail)
                    summaries.append(_trace_summary(detail, path))
                    continue
            except Exception:
                pass
        pending.append(row)

    print(
        f"[branch-event-window-audit] selected={len(rows)} "
        f"resume_hits={len(details)} pending={len(pending)} jobs={args.jobs} "
        f"windows={window_sizes}",
        flush=True,
    )
    started = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(_run_trace, row, window_sizes, args.cold_branches): row
            for row in pending
        }
        completed = len(details)
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            path = os.path.join(detail_dir, _safe_name(row))
            try:
                detail = future.result()
                dump_json(path, detail)
                details.append(detail)
                summary = _trace_summary(detail, path)
                summaries.append(summary)
                completed += 1
                event = summary["event"]
                print(
                    f"[branch-event-window-audit] {completed}/{len(rows)} ok "
                    f"c{summary['n_cores']:02d} {summary['workload']} "
                    f"align={summary['alignment_passed']} "
                    f"f1={100.0 * event['f1']:.3f}% "
                    f"mismatch={100.0 * event['event_mismatch_rate']:.3f}%",
                    flush=True,
                )
            except Exception as exc:
                completed += 1
                failures.append({
                    "trace_id": str(row["trace_id"]),
                    "workload": str(row["workload"]),
                    "workload_role": str(row.get("workload_role", "unknown")),
                    "seed": int(row["seed"]),
                    "n_cores": int(row["n_cores"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                })
                print(
                    f"[branch-event-window-audit][ERROR] {completed}/{len(rows)} "
                    f"c{int(row['n_cores']):02d} {row['workload']}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    details.sort(
        key=lambda item: (
            int(item["trace"]["n_cores"]), str(item["trace"]["workload"])
        )
    )
    summaries.sort(key=lambda row: (int(row["n_cores"]), str(row["workload"])))
    aggregate = {
        "all": aggregate_audit_reports([detail["audit"] for detail in details]),
        "by_core_count": _aggregate_group(details, "n_cores"),
        "by_workload_role": _aggregate_group(details, "workload_role"),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "splits": splits,
            "core_counts": sorted(core_counts),
            "jobs": int(args.jobs),
            "window_sizes_uops": list(window_sizes),
            "cold_branches": int(args.cold_branches),
            "n_selected": len(rows),
        },
        "runtime_seconds": time.perf_counter() - started,
        "aggregate": aggregate,
        "traces": summaries,
        "failures": sorted(
            failures, key=lambda row: (row["n_cores"], row["workload"])
        ),
        "oracle_labels_consumed_as_input": False,
        "gpu_used": False,
    }
    json_path = os.path.join(out_dir, "report.json")
    markdown_path = os.path.join(out_dir, "summary.md")
    dump_json(json_path, report)
    _write_markdown(markdown_path, report)
    event = aggregate["all"]["event"]
    print(
        f"[branch-event-window-audit] complete={len(details)}/{len(rows)} "
        f"failures={len(failures)} align={aggregate['all']['alignment']['passed']} "
        f"event_f1={100.0 * event['f1']:.3f}% "
        f"event_mismatch={100.0 * event['event_mismatch_rate']:.3f}%",
        flush=True,
    )
    print(
        f"[branch-event-window-audit] json={json_path} summary={markdown_path}",
        flush=True,
    )
    return 0 if not failures and aggregate["all"]["alignment"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
