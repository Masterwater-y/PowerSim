#!/usr/bin/env python3
"""Screen fixed-Q candidates using existing results; never launch simulations.

Perfect-subset P99 results are conditional upper bounds, not expected gains.
The optional dense-window replay is deliberately a frozen-service probe, not
a replacement simulator or a bound on a complete resource-repair model.
"""

import argparse
import collections
import hashlib
import json
import math
from pathlib import Path


def quantile(values, probability):
    values = sorted(values)
    position = (len(values) - 1) * probability
    lo, hi = int(math.floor(position)), int(math.ceil(position))
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def fingerprint(path):
    return {"path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def screen_matrix(inventory_path, runs):
    inventory = json.loads(inventory_path.read_text())
    rows = []
    for case, entry in sorted(inventory.items()):
        path = runs / case / "current" / "stats.json"
        stats = json.loads(path.read_text())
        config, frontier = stats["configuration"], stats["causal_frontier"]
        if config["interval_max_cycles"] != 1024:
            raise ValueError("Q must remain 1024: " + case)
        if config.get("response_pending_fill", False):
            raise ValueError("expected pending-fill OFF: " + case)
        metric = entry["metric"]
        if metric not in ("cycles_per_user_uop", "perf_like_cpi"):
            raise ValueError("unknown CPI denominator: " + metric)
        prediction = stats["scope_metrics"][metric]
        reference = entry["reference"]
        if not (math.isfinite(prediction) and prediction > 0 and
                math.isfinite(reference) and reference > 0):
            raise ValueError("invalid CPI: " + case)
        cycle_gap = stats["scope_metrics"]["sum_core_cycles"] * (
            reference / prediction - 1)
        branch_misses = stats["scope_metrics"]["pmu"]["branch_misses"]
        rows.append({
            "case": case, "workload": entry["workload"],
            "cores": entry["cores"], "metric": metric,
            "signed_error_pct": 100 * (prediction / reference - 1),
            "cycles_to_reference": cycle_gap,
            "branch_misses": branch_misses,
            "cycles_needed_per_branch_miss_to_explain_entire_gap": (
                cycle_gap / branch_misses if branch_misses else None),
            "frfcfs_candidate_epochs": frontier["dram_frfcfs_candidate_epochs"],
            "frfcfs_bypass_epochs": frontier["dram_frfcfs_bypass_epochs"],
            "frfcfs_requests": frontier["dram_frfcfs_requests"],
            "frfcfs_bypass_requests": frontier["dram_frfcfs_bypass_requests"],
            "stats": fingerprint(path),
        })
    matrices = {}
    for name, metric in (("dse", "cycles_per_user_uop"),
                         ("formal", "perf_like_cpi")):
        subset = [r for r in rows if r["metric"] == metric]
        if not subset:
            continue
        baseline = quantile([abs(r["signed_error_pct"]) for r in subset], .99)
        worst = max(subset, key=lambda r: abs(r["signed_error_pct"]))
        groups = [("worst_case_only", {worst["case"]})]
        for workload in sorted({r["workload"] for r in subset}):
            groups.append((workload, {r["case"] for r in subset
                                      if r["workload"] == workload}))
        groups.append(("tealeaf_and_graph500", {r["case"] for r in subset
                      if r["workload"] in ("811.tealeaf_s", "854.graph500_s")}))
        bounds = []
        for label, affected in groups:
            ideal = quantile([0 if r["case"] in affected else
                              abs(r["signed_error_pct"]) for r in subset], .99)
            bounds.append({"support": label, "affected_cases": len(affected),
                           "ideal_p99_pct": ideal,
                           "maximum_p99_reduction_pp": baseline - ideal})
        matrices[name] = {"cases": len(subset), "metric": metric,
                          "p99_absolute_error_pct": baseline,
                          "conditional_perfect_subset_bounds": bounds,
                          "tail": sorted(subset, key=lambda r:
                                         -abs(r["signed_error_pct"]))[:8]}
    activation = {}
    for cores in sorted({r["cores"] for r in rows}):
        subset = [r for r in rows if r["cores"] == cores]
        activation[str(cores)] = {
            "cases": len(subset),
            "cases_with_frfcfs_candidates": sum(
                r["frfcfs_candidate_epochs"] > 0 for r in subset),
            "candidate_epochs": sum(r["frfcfs_candidate_epochs"] for r in subset),
            "bypass_requests": sum(r["frfcfs_bypass_requests"] for r in subset),
        }
    return {"inventory": fingerprint(inventory_path), "matrices": matrices,
            "runtime_activation": activation, "cases": rows}


def frozen_resource_probe(samples, width, writeback, commit, retirement_edge,
                          resources):
    """Propagate only observed register/ROB edges, holding all other state fixed.

    Known in-window edges consume their original slack. External predecessors,
    memory service durations, branch state, FU classes and queue order are held
    fixed. The fifth, StoreSet edge has a separate completion semantic and is
    intentionally excluded. Program-order reservation permits earlier empty
    slots for younger independent UOPs; it is not gem5's dynamic issue policy.
    """
    old = {x["sequence"]: x for x in samples}
    new = {}
    issue_slots, wb_slots, commit_slots = (collections.Counter() for _ in range(3))
    issue_delta, retire_delta = [], []
    for x in samples:
        seq, issue = x["sequence"], x["actual_issue_cycle"]
        extra = 0
        for distance in x["producer_dists"][:4]:
            pred = seq - distance
            if distance and pred in new:
                before = old[pred]["actual_completion_cycle"]
                extra = max(extra, new[pred][1] - before - max(0, issue - before))
        pred = x["rob_capacity_predecessor_sequence"]
        if x["rob_capacity_predecessor_valid"] and pred in new:
            before = old[pred]["actual_retire_cycle"]
            extra = max(extra, new[pred][2] - before - max(0, issue - before))
        repaired_issue = issue + extra
        if resources:
            while issue_slots[repaired_issue] >= width:
                repaired_issue += 1
            issue_slots[repaired_issue] += 1
        completion = x["actual_completion_cycle"] + repaired_issue - issue
        if resources:
            while wb_slots[completion] >= writeback:
                completion += 1
            wb_slots[completion] += 1
        retire = max(x["actual_retire_cycle"], completion + retirement_edge,
                     new[seq - 1][2] if seq - 1 in new else 0)
        if resources:
            while commit_slots[retire] >= commit:
                retire += 1
            commit_slots[retire] += 1
        new[seq] = repaired_issue, completion, retire
        issue_delta.append(repaired_issue - issue)
        retire_delta.append(retire - x["actual_retire_cycle"])
    return {"final_retire_delta_cycles": retire_delta[-1],
            "retire_span_delta_cycles": retire_delta[-1] - retire_delta[0],
            "max_retire_delta_cycles": max(retire_delta),
            "max_issue_delta_cycles": max(issue_delta),
            "sum_issue_delta_cycles_diagnostic_only": sum(issue_delta)}


def screen_window(path, core):
    stats = json.loads(path.read_text())
    config = stats["configuration"]
    if core < 0 or core >= len(stats["cores"]):
        raise ValueError("core index outside dense audit")
    samples = stats["cores"][core]["response_frontier_audit"]
    if len(samples) < 2 or any(b["sequence"] != a["sequence"] + 1
                              for a, b in zip(samples, samples[1:])):
        raise ValueError("resource screening requires a contiguous dense window")
    widths = [("actual_dispatch_cycle", "dispatch_width"),
              ("actual_issue_cycle", "issue_width"),
              ("actual_completion_cycle", "writeback_width"),
              ("actual_retire_cycle", "commit_width"),
              ("base_issue_cycle", "issue_width"),
              ("base_completion_cycle", "writeback_width")]
    violations = {}
    for field, key in widths:
        counts = collections.Counter(x[field] for x in samples)
        violations[field] = {
            "width": config[key], "peak": max(counts.values()),
            "overfull_cycles": sum(n > config[key] for n in counts.values()),
            "excess_uops": sum(max(n - config[key], 0) for n in counts.values())}
    args = (samples, config["issue_width"], config["writeback_width"],
            config["commit_width"], config["execute_to_commit"])
    identity = frozen_resource_probe(*args, resources=False)
    if any(identity.values()):
        raise ValueError("frozen replay failed zero-change identity check")
    return {"input": fingerprint(path), "core": core, "samples": len(samples),
            "sequence_begin": samples[0]["sequence"],
            "sequence_end": samples[-1]["sequence"],
            "width_observations": violations, "identity_control": identity,
            "frozen_service_resource_probe": frozen_resource_probe(
                *args, resources=True),
            "limitation": "Not a CPI bound or complete FU/queue/controller replay; "
            "external state and service times fixed; no extrapolation to full ROI."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--dense-audit", type=Path)
    parser.add_argument("--core", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = screen_matrix(args.inventory, args.runs)
    result["contract"] = {
        "q": 1024, "reference": "gem5", "simulations_launched": 0,
        "p99_method": "linear interpolation at (N-1)*0.99, absolute case errors",
        "support_bound": "Set ONLY the named subset's errors to zero; all other "
        "cases fixed. This is not a model prediction or an unconditional bound."}
    if args.dense_audit:
        result["dense_window"] = screen_window(args.dense_audit, args.core)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("wrote", args.output)


if __name__ == "__main__":
    main()
