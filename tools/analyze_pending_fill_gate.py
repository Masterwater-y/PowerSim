#!/usr/bin/env python3
"""Compare frozen CPI matrices without mixing formal macro-CPI and DSE UOP-CPI."""

import argparse
import collections
import json
import math
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text())


def aggregate(rows, field):
    errors = sorted(abs(row[field]) for row in rows)
    if not errors:
        return {"count": 0}
    position = (len(errors) - 1) * 0.99
    lo, hi = math.floor(position), math.ceil(position)
    return {
        "count": len(errors),
        "mape_pct": statistics.mean(errors),
        "p99_abs_error_pct": errors[lo] + (position - lo) * (errors[hi] - errors[lo]),
        "max_abs_error_pct": errors[-1],
        "worst": [{"case": row["case"], "signed_error_pct": row[field]}
                  for row in sorted(rows, key=lambda row: abs(row[field]), reverse=True)[:8]],
    }


def dse_order(rows, inventory, cpi_field):
    groups = collections.defaultdict(list)
    for row in rows:
        case = inventory[row["case"]]
        groups[case["workload"], case["cores"]].append(row)
    directions, significant, pairs, errors = [], [], [], []
    for group in groups.values():
        baseline = next((row for row in group
                         if inventory[row["case"]]["profile"] == "baseline"), None)
        for index, a in enumerate(group):
            for b in group[index + 1:]:
                ref = inventory[a["case"]]["reference"] / inventory[b["case"]]["reference"]
                pred = a[cpi_field] / b[cpi_field]
                pairs.append((ref - 1) * (pred - 1) > 0)
        if baseline is None:
            continue
        for row in group:
            if row is baseline:
                continue
            ref = inventory[baseline["case"]]["reference"] / inventory[row["case"]]["reference"]
            pred = baseline[cpi_field] / row[cpi_field]
            correct = (ref - 1) * (pred - 1) > 0
            directions.append(correct)
            if abs(ref - 1) >= 0.01:
                significant.append(correct)
            errors.append(abs(pred / ref - 1) * 100)
    return {"direction": [sum(directions), len(directions)],
            "direction_reference_effect_ge_1pct": [sum(significant), len(significant)],
            "pairwise_order": [sum(pairs), len(pairs)],
            "speedup_mape_pct": statistics.mean(errors) if errors else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--baseline-variant", default="current")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = read(args.experiment / "case-inventory.json")
    rows, missing, failed = [], [], []
    target_keys = ("totals", "cores", "threads", "cha", "instruction_cha")
    for name, case in inventory.items():
        folder = args.experiment / "runs" / name / args.variant
        path = folder / "stats.json"
        summary_path = folder / "summary.json"
        if summary_path.exists():
            summary = read(summary_path)
            if summary["exit_code"] != 0:
                failed.append(summary)
                missing.append(name)
                continue
        if not path.exists():
            missing.append(name)
            continue
        current = read(path)
        baseline = read(args.baseline / "runs" / name / args.baseline_variant / "stats.json")
        assert current["configuration"]["interval_max_cycles"] == 1024
        assert baseline["configuration"]["interval_max_cycles"] == 1024
        scope, base_scope = current["scope_metrics"], baseline["scope_metrics"]
        cpi, base_cpi = scope[case["metric"]], base_scope[case["metric"]]
        mismatched = [key for key in target_keys if current[key] != baseline[key]]
        rows.append({
            "case": name, "metric": case["metric"], "reference_cpi": case["reference"],
            "cpi": cpi, "baseline_cpi": base_cpi,
            "error_pct": (cpi / case["reference"] - 1) * 100,
            "baseline_error_pct": (base_cpi / case["reference"] - 1) * 100,
            "cycles": scope["sum_core_cycles"], "baseline_cycles": base_scope["sum_core_cycles"],
            "trace_population_equal": all(scope[key] == base_scope[key] for key in
                                          ("user_trace_uops", "native_kernel_trace_uops")),
            "pmu_equal": scope["pmu"] == base_scope["pmu"],
            "mismatched_target_sections": mismatched,
            "pending_fill": current.get("pending_fill", {}),
            "functional_replay_passes": current["causal_frontier"]["sequencer_functional_replay_passes"],
            "stats": str(path.resolve()),
        })
    output = {"variant": args.variant, "expected": len(inventory), "count": len(rows),
              "missing": missing, "failed": failed,
              "complete": not missing and not failed,
              "p99_method": "linear interpolation at (N - 1) * 0.99",
              "target_state_equal": bool(rows) and all(not row["mismatched_target_sections"] for row in rows),
              "trace_populations_equal": bool(rows) and all(row["trace_population_equal"] for row in rows),
              "rows": rows}
    for prefix in ("formal-", "dse-"):
        selected = [row for row in rows if row["case"].startswith(prefix)]
        entry = {"baseline": aggregate(selected, "baseline_error_pct"),
                 "candidate": aggregate(selected, "error_pct")}
        if prefix == "dse-":
            entry["baseline_order"] = dse_order(selected, inventory, "baseline_cpi")
            entry["candidate_order"] = dse_order(selected, inventory, "cpi")
        output[prefix.rstrip("-")] = entry
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({key: value for key, value in output.items() if key != "rows"}, indent=2))


if __name__ == "__main__":
    main()
