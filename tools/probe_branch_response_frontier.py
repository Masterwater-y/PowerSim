#!/usr/bin/env python3
"""Bounded conditional-branch response/front-end consistency probe.

Uses synthetic functional records, never gem5 timing as inference input.
The output establishes a necessary-order counterexample, not gem5 CPI accuracy.
Each simulation is limited to 3 or 4 UOPs and a 15 second host timeout.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/gem5-fs-native-kernel.cfg"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    common = {"core_id": 0, "thread_id": 0, "cpl": 3}
    records = [
        dict(common, pc=4096, paddr=65536, vaddr=65536, size=8,
             is_load=True, n_src=0, op_class=84),
        dict(common, pc=4100, is_branch=True, is_branch_cond=True,
             branch_taken=True, branch_target=8192, branch_next_pc=8192,
             producer_dists=[1, 0, 0, 0], n_src=1, op_class=1),
        dict(common, pc=8192, op_class=1),
    ]
    # Explicitly isolate a single user-mode stream, retaining target timings
    # and Q from the supplied profile. Audit requires the generic feedback
    # path; the fourth run checks its target output against the fast kernel.
    config = ("config.include = " + str(args.config.resolve()) + "\n"
              "measurement.native_kernel_trace = false\n"
              "measurement.scope = user\nsim.cores = 1\n"
              "sim.interval_max_cycles = 1024\n"
              "sim.cpi_attribution = true\n"
              "core.response_frontier_audit_stride_uops = 1\n"
              "core.response_materialized_uop_fast_kernel = false\n")
    results, all_stats = [], {}
    base_cases = (
        "dependent_miss",
        "independent_miss",
        "correct_prediction",
        "fast_kernel",
        "load_after_branch",
    )
    cases = base_cases + tuple(name + "_recovery" for name in base_cases)
    for name in cases:
        recovery = name.endswith("_recovery")
        base_name = name[:-len("_recovery")] if recovery else name
        rows = [dict(row) for row in records]
        candidate_config = config
        if recovery:
            candidate_config += "core.response_branch_recovery = true\n"
        if base_name == "independent_miss":
            rows[1].update(producer_dists=[0, 0, 0, 0], n_src=0)
        if base_name == "correct_prediction":
            rows[1].update(branch_taken=False, branch_next_pc=4104)
            rows[2]["pc"] = 4104
        if base_name == "fast_kernel":
            candidate_config += (
                "core.response_materialized_uop_fast_kernel = true\n"
                "sim.cpi_attribution = false\n"
                "core.response_frontier_audit_stride_uops = 0\n")
        if base_name == "load_after_branch":
            rows[2].update(is_load=True, paddr=131136, vaddr=131136,
                           size=8, op_class=84)
            rows.append(dict(common, pc=8196, op_class=1,
                             producer_dists=[1, 0, 0, 0], n_src=1))
        directory = args.output_dir / name
        directory.mkdir(exist_ok=True)
        trace = directory / "trace.jsonl"
        trace.write_text("".join(json.dumps(row) + "\n" for row in rows))
        manifest, cfg = directory / "manifest.txt", directory / "probe.cfg"
        manifest.write_text("0 gem5-jsonl " + str(trace.resolve()) + "\n")
        cfg.write_text(candidate_config)
        output = directory / "stats.json"
        command = [str(args.fastsim.resolve()), "simulate", "--config", str(cfg),
                   "--manifest", str(manifest), "--output", str(output)]
        run = subprocess.run(command, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True,
                             timeout=15)
        (directory / "run.log").write_text(run.stdout)
        if run.returncode:
            raise RuntimeError(name + ": " + run.stdout)
        stats = json.loads(output.read_text())
        all_stats[name] = stats
        stages = [{key: sample[key] for key in (
            "sequence", "pc", "branch", "branch_miss",
            "actual_fetch_cycle", "actual_dispatch_cycle",
            "actual_issue_cycle", "actual_completion_cycle", "actual_retire_cycle")}
            for sample in stats["cores"][0].get("response_frontier_audit", [])]
        result = {"name": name, "command": command,
                  "branch_misses": stats["scope_metrics"]["pmu"]["branch_misses"],
                  "cycles": stats["scope_metrics"]["sum_core_cycles"],
                  "fast_kernel_uops": stats["causal_frontier"][
                      "response_materialized_fast_kernel_uops"],
                  "branch_recovery": {
                      key: stats["totals"][key]
                      for key in (
                          "branch_recovery_mispredictions",
                          "branch_recovery_response_delayed_mispredictions",
                          "branch_recovery_frontier_updates",
                          "branch_recovery_fetch_gated_uops",
                          "branch_recovery_fetch_gated_cycles",
                          "branch_recovery_maximum_fetch_gate_cycles",
                          "response_critical_branch_recovery_cycles",
                      )
                  }, "stages": stages}
        if stages and result["branch_misses"] == 1:
            result["correct_path_fetch_before_branch_completion"] = max(
                0, stages[1]["actual_completion_cycle"] -
                stages[2]["actual_fetch_cycle"])
        results.append(result)
    baseline = all_stats["dependent_miss"]["scope_metrics"]
    fast = all_stats["fast_kernel"]["scope_metrics"]
    equal = {key: baseline[key] == fast[key] for key in (
        "sum_core_cycles", "pmu", "perf_like_cpi", "cycles_per_user_uop")}
    if not all(equal.values()):
        raise RuntimeError("audit and production fast kernel target outputs differ")
    recovered = all_stats["dependent_miss_recovery"]["scope_metrics"]
    recovered_fast = all_stats["fast_kernel_recovery"]["scope_metrics"]
    recovered_equal = {key: recovered[key] == recovered_fast[key] for key in (
        "sum_core_cycles", "pmu", "perf_like_cpi", "cycles_per_user_uop")}
    if not all(recovered_equal.values()):
        raise RuntimeError(
            "recovery audit and production fast kernel target outputs differ")

    def stages(name):
        return next(row["stages"] for row in results if row["name"] == name)

    recovered_dependent = stages("dependent_miss_recovery")
    checks = {
        "dependent_correct_path_waits_for_resolution": (
            recovered_dependent[2]["actual_fetch_cycle"] >=
            recovered_dependent[1]["actual_completion_cycle"]
        ),
        "independent_control_unchanged": (
            stages("independent_miss_recovery") == stages("independent_miss")
        ),
        "correct_prediction_control_unchanged": (
            stages("correct_prediction_recovery") ==
            stages("correct_prediction")
        ),
        "load_chain_exposes_retirement_span": (
            all_stats["load_after_branch_recovery"]["scope_metrics"]
                ["sum_core_cycles"] >
            all_stats["load_after_branch"]["scope_metrics"]
                ["sum_core_cycles"]
        ),
    }
    for base_name in base_cases:
        baseline_scope = all_stats[base_name]["scope_metrics"]
        candidate_scope = all_stats[base_name + "_recovery"]["scope_metrics"]
        checks[base_name + "_functional_pmu_unchanged"] = (
            baseline_scope["pmu"] == candidate_scope["pmu"]
        )
    if not all(checks.values()):
        raise RuntimeError("branch recovery probe failed: " + repr(checks))
    write_json(args.output_dir / "summary.json", {
        "binary_sha256": hashlib.sha256(args.fastsim.read_bytes()).hexdigest(),
        "fast_kernel_target_equal": equal,
        "recovery_fast_kernel_target_equal": recovered_equal,
        "checks": checks, "results": results,
        "limitation": "Synthetic necessary-order probe, not collected gem5 timing "
        "or a measured P99 gain. No throughput conclusions from these tiny runs."})
    print("wrote", args.output_dir / "summary.json")


if __name__ == "__main__":
    main()
