#!/usr/bin/env python3
"""Capacity/split-aware semantic gate for macro-v29 training runs."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import re
import sys
from typing import Any, Callable, Dict, Mapping


DEFAULT_VARIANTS = (
    "real", "pseudo", "mnemonic_shuffle", "random_init", "side_only",
    "llm_only", "register_rename",
)
REQUIRED_CONTROLS = (
    "pseudo", "mnemonic_shuffle", "random_init", "side_only",
)
REQUIRED_DIAGNOSTICS = ("llm_only", "register_rename")
FAIRNESS_KEYS = (
    "base_model", "tiny_backbone", "tiny_width", "d_model", "d_field",
    "n_heads", "lora_r", "lora_alpha", "lora_dropout", "freeze_backbone",
    "max_steps", "batch_size", "sequence_length", "sequence_stride",
    "max_tokens", "train_split", "validation_split", "train_sources",
    "validation_sources", "train_sequences", "validation_sequences",
    "tokenizer_size", "train_workload_roles", "validation_workload_roles",
    "tokenizer_fingerprint", "base_model_commit",
    "backbone_config_fingerprint", "allow_download",
    "seed", "world_size", "dtype", "lr_head", "lr_lora", "weight_decay",
    "warmup_fraction", "gradient_clip", "eval_batches", "cores",
    "trainable_parameters", "train_order_policy", "validation_order_policy",
    "trainable_init_policy", "semantic_run_contract",
    "init_trainable", "initialized_from",
)
REQUIRED_PROTOCOL = {
    "semantic_run_contract": "macro-v29-semantic-run-v4",
    "init_trainable": "",
    "initialized_from": None,
    "train_order_policy": "seeded-random-v1",
    "trainable_init_policy": "isolated-seed-domains-v1",
    "validation_order_policy": "seeded-within-trace-round-robin-v2",
}
REQUIRED_SHA256_PROVENANCE = (
    "tokenizer_fingerprint", "backbone_config_fingerprint",
)


def load_run(root: Path, variant: str) -> Dict[str, Any] | None:
    directory = root / variant
    final_path = directory / "final_report.json"
    run_path = directory / "run.json"
    if not final_path.is_file() or not run_path.is_file():
        return None
    return {
        "directory": str(directory),
        "final": json.loads(final_path.read_text()),
        "run": json.loads(run_path.read_text()),
    }


def validation_wape(record: Mapping[str, Any] | None) -> float | None:
    if record is None:
        return None
    validation = record["final"].get("validation")
    if not isinstance(validation, Mapping):
        return None
    value = validation.get("commit_wape")
    if not isinstance(value, (float, int)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0.0 else None


def validation_per_trace(
    record: Mapping[str, Any] | None,
) -> tuple[Dict[str, Dict[str, float]], list[str]]:
    """Return validated additive validation totals keyed by trace ID."""
    if record is None:
        return {}, ["run is missing"]
    validation = record["final"].get("validation")
    if not isinstance(validation, Mapping):
        return {}, ["validation report is missing"]
    raw = validation.get("per_trace")
    if not isinstance(raw, Mapping) or not raw:
        return {}, ["validation.per_trace is missing or empty"]
    result: Dict[str, Dict[str, float]] = {}
    issues: list[str] = []
    for trace_id, entry in raw.items():
        name = str(trace_id)
        if not name or not isinstance(entry, Mapping):
            issues.append(f"invalid per-trace entry {name!r}")
            continue
        parsed: Dict[str, float] = {}
        for key in ("absolute_error", "target_magnitude", "valid_macros"):
            value = entry.get(key)
            if not isinstance(value, (float, int)):
                issues.append(f"{name}: {key} is not numeric")
                break
            parsed[key] = float(value)
        else:
            if not all(math.isfinite(value) for value in parsed.values()):
                issues.append(f"{name}: per-trace totals are not finite")
            elif parsed["absolute_error"] < 0.0:
                issues.append(f"{name}: absolute_error is negative")
            elif parsed["target_magnitude"] <= 0.0:
                issues.append(f"{name}: target_magnitude is not positive")
            elif parsed["valid_macros"] <= 0.0:
                issues.append(f"{name}: valid_macros is not positive")
            else:
                result[name] = parsed
    if not issues:
        absolute_error = sum(entry["absolute_error"] for entry in result.values())
        target_magnitude = sum(
            entry["target_magnitude"] for entry in result.values()
        )
        valid_macros = sum(entry["valid_macros"] for entry in result.values())
        aggregate_wape = absolute_error / max(target_magnitude, 1e-12)
        reported_wape = validation.get("commit_wape")
        if (
            not isinstance(reported_wape, (float, int))
            or not math.isclose(
                aggregate_wape,
                float(reported_wape),
                rel_tol=1e-8,
                abs_tol=1e-10,
            )
        ):
            issues.append(
                "per-trace totals do not reproduce validation.commit_wape"
            )
        reported_valid = validation.get("valid_macros")
        if (
            reported_valid is not None
            and (
                not isinstance(reported_valid, (float, int))
                or float(reported_valid) != valid_macros
            )
        ):
            issues.append(
                "per-trace totals do not reproduce validation.valid_macros"
            )
    return result, issues


def workload_from_trace_id(trace_id: str) -> str | None:
    """Extract the workload cluster from the canonical trace ID."""
    components = [part for part in trace_id.split("/") if part.startswith("W_")]
    if components:
        return components[-1]
    # Some legacy manifests stored the run ID instead of the path.  In those
    # IDs the workload is the final `_W_v<version>_...` suffix.
    match = re.search(r"(?:^|_)(W_v[0-9]+(?:_[A-Za-z0-9.-]+)+)$", trace_id)
    return match.group(1) if match else None


def aggregate_by_workload(
    per_trace: Mapping[str, Mapping[str, float]],
) -> tuple[Dict[str, Dict[str, float]], list[str]]:
    totals: Dict[str, Dict[str, float]] = {}
    issues: list[str] = []
    for trace_id, entry in per_trace.items():
        workload = workload_from_trace_id(trace_id)
        if workload is None:
            issues.append(f"cannot extract workload from trace ID {trace_id!r}")
            continue
        aggregate = totals.setdefault(
            workload,
            {"absolute_error": 0.0, "target_magnitude": 0.0,
             "valid_macros": 0.0, "traces": 0.0},
        )
        for key in ("absolute_error", "target_magnitude", "valid_macros"):
            aggregate[key] += float(entry[key])
        aggregate["traces"] += 1.0
    return totals, issues


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(float(probability), 0.0), 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_cluster_bootstrap(
    real_workloads: Mapping[str, Mapping[str, float]],
    comparison_workloads: Mapping[str, Mapping[str, float]],
    *,
    statistic: Callable[[float, float], float],
    samples: int,
    seed: int,
    confidence: float,
) -> Dict[str, Any]:
    """Bootstrap paired workload clusters using their additive totals."""
    workloads = sorted(real_workloads)
    if not workloads or set(workloads) != set(comparison_workloads):
        raise ValueError("paired bootstrap requires identical workload sets")

    def evaluate(selected: list[str]) -> float:
        real_error = sum(
            float(real_workloads[name]["absolute_error"]) for name in selected
        )
        real_target = sum(
            float(real_workloads[name]["target_magnitude"]) for name in selected
        )
        comparison_error = sum(
            float(comparison_workloads[name]["absolute_error"])
            for name in selected
        )
        comparison_target = sum(
            float(comparison_workloads[name]["target_magnitude"])
            for name in selected
        )
        real_wape = real_error / max(real_target, 1e-12)
        comparison_wape = comparison_error / max(comparison_target, 1e-12)
        return float(statistic(real_wape, comparison_wape))

    rng = random.Random(int(seed))
    estimates = [
        evaluate([rng.choice(workloads) for _ in workloads])
        for _ in range(int(samples))
    ]
    tail = (1.0 - float(confidence)) / 2.0
    return {
        "point_estimate": evaluate(workloads),
        "ci_lower": percentile(estimates, tail),
        "ci_median": percentile(estimates, 0.5),
        "ci_upper": percentile(estimates, 1.0 - tail),
        "confidence": float(confidence),
        "bootstrap_samples": int(samples),
        "workload_clusters": len(workloads),
        "workloads": workloads,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", required=True)
    parser.add_argument(
        "--variants", default=",".join(DEFAULT_VARIANTS),
    )
    parser.add_argument("--min-semantic-improvement", type=float, default=0.05)
    parser.add_argument("--min-side-improvement", type=float, default=0.03)
    parser.add_argument("--max-rename-regression", type=float, default=0.02)
    parser.add_argument("--min-training-steps", type=int, default=200)
    parser.add_argument("--min-validation-batches", type=int, default=32)
    parser.add_argument("--min-workload-clusters", type=int, default=7)
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260717)
    parser.add_argument("--bootstrap-confidence", type=float, default=0.95)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if int(args.bootstrap_samples) <= 0:
        parser.error("--bootstrap-samples must be positive")
    if int(args.min_workload_clusters) <= 1:
        parser.error("--min-workload-clusters must be greater than one")
    if not 0.0 < float(args.bootstrap_confidence) < 1.0:
        parser.error("--bootstrap-confidence must be between zero and one")

    requested = tuple(
        value.strip() for value in args.variants.split(",") if value.strip()
    )
    root = Path(args.runs_root)
    records = {variant: load_run(root, variant) for variant in requested}
    failures: list[str] = []
    unknown: list[str] = []
    fairness: Dict[str, Dict[str, Any]] = {}
    real = records.get("real")
    if real is None:
        unknown.append("missing real run")
    else:
        real_run = real["run"]
        if real["final"].get("kind") != "macro-native-qwen-training":
            unknown.append(
                "real run is not a Qwen training run; tiny smoke cannot pass "
                "the semantic gate"
            )
        heldout_scope = (
            real_run.get("validation_split") == "development_heldout"
            or (
                real_run.get("validation_split") == "seed0_inference"
                and set(str(real_run.get(
                    "validation_workload_roles", "",
                )).split(",")) == {"business_heldout"}
            )
        )
        if not heldout_scope:
            failures.append(
                "semantic gate validation must contain only development "
                "business-heldout workloads"
            )
        for variant, record in records.items():
            if record is None or variant == "real":
                continue
            mismatch = {
                key: {
                    "real": real_run.get(key),
                    "control": record["run"].get(key),
                }
                for key in FAIRNESS_KEYS
                if record["run"].get(key) != real_run.get(key)
            }
            fairness[variant] = mismatch
            if mismatch:
                failures.append(
                    f"{variant} is not capacity/split matched: "
                    f"{sorted(mismatch)}"
                )
            if record["final"].get("kind") != "macro-native-qwen-training":
                unknown.append(f"{variant} is not a Qwen training run")

    for variant in REQUIRED_CONTROLS:
        if records.get(variant) is None:
            unknown.append(f"missing required control {variant}")
    for variant in REQUIRED_DIAGNOSTICS:
        if records.get(variant) is None:
            unknown.append(f"missing required diagnostic {variant}")
    required_runs = ("real",) + REQUIRED_CONTROLS + REQUIRED_DIAGNOSTICS
    evidence_complete = True
    for variant in required_runs:
        record = records.get(variant)
        if record is None:
            evidence_complete = False
            continue
        final = record["final"]
        run = record["run"]
        validation = final.get("validation")
        if final.get("status") != "PASS":
            unknown.append(f"{variant} training status is not PASS")
            evidence_complete = False
        if int(final.get("steps", 0)) < int(args.min_training_steps):
            unknown.append(
                f"{variant} has fewer than {int(args.min_training_steps)} steps"
            )
            evidence_complete = False
        if (
            not isinstance(validation, Mapping)
            or int(validation.get("batches", 0))
            < int(args.min_validation_batches)
        ):
            unknown.append(
                f"{variant} has fewer than {int(args.min_validation_batches)} "
                "validation batches"
            )
            evidence_complete = False
        for key, expected in REQUIRED_PROTOCOL.items():
            if run.get(key) != expected:
                unknown.append(
                    f"{variant} lacks required {key}={expected!r} provenance"
                )
                evidence_complete = False
        for key in REQUIRED_SHA256_PROVENANCE:
            value = run.get(key)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                unknown.append(f"{variant} lacks a valid {key}")
                evidence_complete = False
        if (
            final.get("kind") == "macro-native-qwen-training"
            and run.get("base_model_commit") in {
                None, "", "missing", "local-unversioned",
            }
        ):
            unknown.append(f"{variant} lacks a versioned base-model commit")
            evidence_complete = False
    per_trace: Dict[str, Dict[str, Dict[str, float]]] = {}
    workload_totals: Dict[str, Dict[str, Dict[str, float]]] = {}
    paired_evidence_issues: Dict[str, list[str]] = {}
    for variant in required_runs:
        trace_totals, trace_issues = validation_per_trace(records.get(variant))
        workload, workload_issues = aggregate_by_workload(trace_totals)
        per_trace[variant] = trace_totals
        workload_totals[variant] = workload
        issues = trace_issues + workload_issues
        record = records.get(variant)
        if record is not None:
            expected_traces = record["run"].get("validation_sources")
            if (
                isinstance(expected_traces, int)
                and expected_traces > 0
                and len(trace_totals) != expected_traces
            ):
                issues.append(
                    f"validation covers {len(trace_totals)} traces but run "
                    f"declares {expected_traces} validation sources"
                )
            if len(workload) < int(args.min_workload_clusters):
                issues.append(
                    f"validation covers {len(workload)} workload clusters; "
                    f"at least {int(args.min_workload_clusters)} are required"
                )
        if issues:
            paired_evidence_issues[variant] = issues
            unknown.extend(f"{variant}: {issue}" for issue in issues)
            evidence_complete = False

    real_trace_ids = set(per_trace.get("real", {}))
    if real_trace_ids:
        for variant in REQUIRED_CONTROLS + REQUIRED_DIAGNOSTICS:
            comparison_trace_ids = set(per_trace.get(variant, {}))
            if comparison_trace_ids != real_trace_ids:
                missing = sorted(real_trace_ids - comparison_trace_ids)
                extra = sorted(comparison_trace_ids - real_trace_ids)
                failures.append(
                    f"{variant} validation trace set differs from real: "
                    f"missing={missing}, extra={extra}"
                )
                evidence_complete = False
                continue
            label_mismatches = []
            for trace_id in sorted(real_trace_ids):
                real_entry = per_trace["real"][trace_id]
                comparison_entry = per_trace[variant][trace_id]
                targets_match = math.isclose(
                    real_entry["target_magnitude"],
                    comparison_entry["target_magnitude"],
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                )
                counts_match = math.isclose(
                    real_entry["valid_macros"],
                    comparison_entry["valid_macros"],
                    rel_tol=0.0,
                    abs_tol=0.0,
                )
                if not targets_match or not counts_match:
                    label_mismatches.append(trace_id)
            if label_mismatches:
                failures.append(
                    f"{variant} validation labels/counts differ from real for "
                    f"traces={label_mismatches}"
                )
                evidence_complete = False

    qwen_evidence_ready = (
        evidence_complete
        and real is not None
        and real["final"].get("kind") == "macro-native-qwen-training"
        and all(
            records.get(variant) is not None
            and records[variant]["final"].get("kind")
            == "macro-native-qwen-training"
            for variant in REQUIRED_CONTROLS + REQUIRED_DIAGNOSTICS
        )
    )

    wape = {
        variant: validation_wape(record)
        for variant, record in records.items()
    }
    improvements: Dict[str, float | None] = {}
    paired_bootstrap: Dict[str, Dict[str, Any]] = {}
    real_wape = wape.get("real")
    for variant_index, variant in enumerate(REQUIRED_CONTROLS):
        control_wape = wape.get(variant)
        value = None
        if real_wape is not None and control_wape is not None:
            value = (control_wape - real_wape) / max(abs(control_wape), 1e-12)
            threshold = (
                float(args.min_side_improvement)
                if variant == "side_only"
                else float(args.min_semantic_improvement)
            )
            if qwen_evidence_ready and value < threshold:
                failures.append(
                    f"real improvement vs {variant} is {value:.6f} < {threshold:.6f}"
                )
        else:
            unknown.append(f"missing validation WAPE for real/{variant}")
        improvements[f"real_vs_{variant}"] = value
        if (
            workload_totals.get("real")
            and set(workload_totals["real"]) == set(workload_totals.get(variant, {}))
        ):
            bootstrap = paired_cluster_bootstrap(
                workload_totals["real"],
                workload_totals[variant],
                statistic=lambda real_value, control_value: (
                    control_value - real_value
                ) / max(abs(control_value), 1e-12),
                samples=int(args.bootstrap_samples),
                seed=int(args.bootstrap_seed) + variant_index,
                confidence=float(args.bootstrap_confidence),
            )
            paired_bootstrap[f"real_vs_{variant}"] = bootstrap
            if qwen_evidence_ready and float(bootstrap["ci_lower"]) <= 0.0:
                failures.append(
                    f"real improvement vs {variant} is not positive at "
                    f"{float(args.bootstrap_confidence):.1%} paired workload "
                    f"confidence: lower={float(bootstrap['ci_lower']):.6f}"
                )

    rename_delta = None
    rename_wape = wape.get("register_rename")
    if real_wape is not None and rename_wape is not None:
        rename_delta = (rename_wape - real_wape) / max(abs(real_wape), 1e-12)
        if (
            qwen_evidence_ready
            and rename_delta > float(args.max_rename_regression)
        ):
            failures.append(
                f"register rename regression {rename_delta:.6f} exceeds "
                f"{float(args.max_rename_regression):.6f}"
            )
    if (
        workload_totals.get("real")
        and set(workload_totals["real"])
        == set(workload_totals.get("register_rename", {}))
    ):
        rename_bootstrap = paired_cluster_bootstrap(
            workload_totals["real"],
            workload_totals["register_rename"],
            statistic=lambda real_value, renamed_value: (
                renamed_value - real_value
            ) / max(abs(real_value), 1e-12),
            samples=int(args.bootstrap_samples),
            seed=int(args.bootstrap_seed) + len(REQUIRED_CONTROLS),
            confidence=float(args.bootstrap_confidence),
        )
        paired_bootstrap["register_rename_relative_delta"] = rename_bootstrap
        if (
            qwen_evidence_ready
            and float(rename_bootstrap["ci_upper"])
            > float(args.max_rename_regression)
        ):
            failures.append(
                f"register rename upper confidence bound "
                f"{float(rename_bootstrap['ci_upper']):.6f} exceeds "
                f"{float(args.max_rename_regression):.6f}"
            )

    if failures:
        status, return_code = "FAIL", 2
    elif unknown:
        status, return_code = "UNKNOWN", 3
    else:
        status, return_code = "PASS", 0
    report = {
        "status": status,
        "contract": "macro-v29-semantic-gate-v2",
        "runs_root": str(root),
        "requested_variants": list(requested),
        "required_controls": list(REQUIRED_CONTROLS),
        "required_diagnostics": list(REQUIRED_DIAGNOSTICS),
        "wape": wape,
        "improvements": improvements,
        "paired_bootstrap": paired_bootstrap,
        "paired_evidence_issues": paired_evidence_issues,
        "register_rename_relative_delta": rename_delta,
        "fairness_mismatches": fairness,
        "qwen_evidence_ready": qwen_evidence_ready,
        "evidence_complete": evidence_complete,
        "minimum_training_steps": int(args.min_training_steps),
        "minimum_validation_batches": int(args.min_validation_batches),
        "minimum_workload_clusters": int(args.min_workload_clusters),
        "bootstrap_samples": int(args.bootstrap_samples),
        "bootstrap_seed": int(args.bootstrap_seed),
        "bootstrap_confidence": float(args.bootstrap_confidence),
        "failures": failures,
        "unknown": unknown,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
