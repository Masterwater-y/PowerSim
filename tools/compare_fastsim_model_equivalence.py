#!/usr/bin/env python3
"""Compare same-input FastSim predictions exactly; do not score native accuracy."""

import argparse
import hashlib
import json
import re
from pathlib import Path


HOST_EXCLUSIONS = (
    "throughput", "wall_time_seconds", "wall_time_breakdown",
    "input_identity_wall_seconds", "*_wall_ns", "frontier_waits",
    "max_resident_chunks",
)


def excluded(path):
    if re.search(
        r"(^|/)(throughput|wall_time_seconds|wall_time_breakdown|input_identity_wall_seconds)(/|$)",
        path,
    ):
        return True
    return path.endswith(("_wall_ns", "/frontier_waits", "/max_resident_chunks"))


def differences(before, after, path=""):
    if excluded(path):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key
            if key not in before or key not in after:
                result.append({"path": child, "reason": "missing field"})
            else:
                result.extend(differences(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [{"path": path, "reason": "length mismatch",
                     "before": len(before), "after": len(after)}]
        return [
            change for index, (a, b) in enumerate(zip(before, after))
            for change in differences(a, b, path + "/" + str(index))
        ]
    if before == after:
        return []
    return [{"path": path, "before": before, "after": after}]


def load_prediction(path):
    payload = path.read_bytes()
    data = json.loads(payload)
    for key in ("measurement_scope", "configuration", "functional_source_identity",
                "time_accounting", "scope_metrics"):
        if key not in data:
            raise ValueError(f"{path}: missing required {key}")
    identity = data["functional_source_identity"]
    if identity.get("status") != "complete" or not identity.get("participants"):
        raise ValueError(f"{path}: incomplete functional input identity")
    if not identity.get("source_identity") or not identity.get("sources"):
        raise ValueError(f"{path}: missing source population/window")
    accounting = data["time_accounting"]
    for key in ("active_cycles", "task_macro_instructions", "per_core"):
        if key not in accounting:
            raise ValueError(f"{path}: missing time-accounting {key}")
    if not accounting["per_core"] or accounting["task_macro_instructions"] <= 0:
        raise ValueError(f"{path}: empty CPI population")
    if (accounting.get("active_status") != "available" or
            accounting.get("active_scope") != "workload-task" or
            accounting.get("ownership_status") != "complete" or
            accounting.get("unknown_cycles") != 0 or
            not accounting.get("active_end_policy") or not accounting.get("task_ids")):
        raise ValueError(f"{path}: workload-task active CPI unavailable/incomplete")
    if accounting.get("scope") != data["measurement_scope"]:
        raise ValueError(f"{path}: time-accounting measurement scope mismatch")
    for key in ("task_pmu", "retired_cache"):
        if key not in data["scope_metrics"]:
            raise ValueError(f"{path}: missing model PMU {key}")
    return data, hashlib.sha256(payload).hexdigest()


def compare(before_path, after_path):
    before, before_sha = load_prediction(before_path)
    after, after_sha = load_prediction(after_path)
    changes = differences(before, after)
    cpi = []
    for data in (before, after):
        accounting = data["time_accounting"]
        cpi.append(accounting["active_cycles"] / accounting["task_macro_instructions"])
    return {
        "schema": "fastsim-model-equivalence-v1",
        "passed": not changes,
        "before": str(before_path),
        "after": str(after_path),
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "functional_source_identity_equal": (
            before["functional_source_identity"] == after["functional_source_identity"]),
        "effective_configuration_equal": before["configuration"] == after["configuration"],
        "time_accounting_equal": before["time_accounting"] == after["time_accounting"],
        "task_pmu_equal": (
            before["scope_metrics"]["task_pmu"] == after["scope_metrics"]["task_pmu"]),
        "retired_cache_equal": (
            before["scope_metrics"]["retired_cache"] == after["scope_metrics"]["retired_cache"]),
        "before_cpi": cpi[0],
        "after_cpi": cpi[1],
        "absolute_cpi_delta_cycles_per_macro": abs(cpi[1] - cpi[0]),
        "cpi_scope": "each prediction's workload-task active cycles / task macro instructions",
        "host_field_exclusions": HOST_EXCLUSIONS,
        "target_difference_count": len(changes),
        "target_differences": changes,
        "native_accuracy_certified": False,
        "hardware_equivalence_certified": False,
        "throughput_gate_certified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = compare(args.before, args.after)
    except (ValueError, KeyError, TypeError, OSError) as error:
        result = {"schema": "fastsim-model-equivalence-v1", "passed": False,
                  "error": str(error)}
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
