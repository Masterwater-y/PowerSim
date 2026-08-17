#!/usr/bin/env python3
"""Freeze dependency-resource scales on calibration cases and score held-out cases."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


RESOURCES = {
    "renamed": (
        "reference_renamed_scale_required",
        "modeled_to_reference_renamed_ratio",
    ),
    "memory": (
        "reference_memory_scale_required",
        "modeled_to_reference_memory_ratio",
    ),
    "destination_operands": (
        "reference_destination_scale_required",
        "modeled_to_reference_destination_ratio",
    ),
}


def load_summary(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("schema") != "fastsim-speculative-dependency-pilots-v2":
        raise ValueError(f"{path}: unexpected dependency summary schema")
    rows = summary.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: no dependency rows")
    for row in rows:
        if not row.get("resource_scope_comparable"):
            raise ValueError(
                f"{path}: {row.get('label')} is not CPL-safe for resources"
            )
        if not row.get("wrong_path_attribution"):
            raise ValueError(
                f"{path}: {row.get('label')} lacks exact wrong-path attribution"
            )
    return summary


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def evaluate(
    calibration_path: Path,
    held_out_path: Path,
    resources: list[str],
) -> dict[str, Any]:
    calibration = load_summary(calibration_path)
    held_out = load_summary(held_out_path)
    calibration_labels = {str(row["label"]) for row in calibration["rows"]}
    held_out_labels = {str(row["label"]) for row in held_out["rows"]}
    overlap = sorted(calibration_labels & held_out_labels)
    if overlap:
        raise ValueError(f"calibration/held-out labels overlap: {overlap}")

    frozen_scales = {}
    rows = []
    for resource in resources:
        scale_field, ratio_field = RESOURCES[resource]
        calibration_scales = [
            float(row[scale_field]) for row in calibration["rows"]
        ]
        scale = statistics.median(calibration_scales)
        frozen_scales[resource] = {
            "value": scale,
            "calibration_values": calibration_scales,
            "calibration_max_to_min": (
                max(calibration_scales) / min(calibration_scales)
                if min(calibration_scales) > 0 else None
            ),
        }
        for row in held_out["rows"]:
            raw_ratio = float(row[ratio_field])
            scaled_ratio = scale * raw_ratio
            signed_error_percent = 100.0 * (scaled_ratio - 1.0)
            rows.append({
                "label": str(row["label"]),
                "resource": resource,
                "frozen_scale": scale,
                "raw_modeled_to_reference_ratio": raw_ratio,
                "scaled_modeled_to_reference_ratio": scaled_ratio,
                "signed_error_percent": signed_error_percent,
                "ape_percent": abs(signed_error_percent),
            })

    distributions = {}
    for resource in resources:
        apes = [
            float(row["ape_percent"]) for row in rows
            if row["resource"] == resource
        ]
        distributions[resource] = {
            "mean_ape_percent": sum(apes) / len(apes),
            "p50_ape_percent": quantile(apes, 0.50),
            "p90_ape_percent": quantile(apes, 0.90),
            "max_ape_percent": max(apes),
        }
    return {
        "schema": "fastsim-speculative-dependency-holdout-v1",
        "timing_effect": False,
        "scales_frozen_from_calibration_only": True,
        "calibration_summary": str(calibration_path.resolve()),
        "held_out_summary": str(held_out_path.resolve()),
        "calibration_labels": sorted(calibration_labels),
        "held_out_labels": sorted(held_out_labels),
        "resources": resources,
        "frozen_scales": frozen_scales,
        "held_out_error_distributions": distributions,
        "rows": rows,
    }


def write_outputs(result: dict[str, Any], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with prefix.with_suffix(".csv").open(
        "w", encoding="utf-8", newline=""
    ) as output:
        writer = csv.DictWriter(output, fieldnames=list(result["rows"][0]))
        writer.writeheader()
        writer.writerows(result["rows"])
    lines = [
        "# Speculative dependency held-out audit",
        "",
        "Scales below are frozen from the calibration summary only. All "
        "quantities remain audit-only and have no CPI or PMU timing effect.",
        "",
        "| Held-out case | Resource | Frozen scale | Raw modeled/reference | "
        "Scaled modeled/reference | Signed error | APE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in result["rows"]:
        lines.append(
            f"| {row['label']} | {row['resource']} | "
            f"{row['frozen_scale']:.6f} | "
            f"{row['raw_modeled_to_reference_ratio']:.3f}x | "
            f"{row['scaled_modeled_to_reference_ratio']:.3f}x | "
            f"{row['signed_error_percent']:+.2f}% | "
            f"{row['ape_percent']:.2f}% |"
        )
    prefix.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration-summary", type=Path, required=True)
    parser.add_argument("--held-out-summary", type=Path, required=True)
    parser.add_argument(
        "--resource", action="append", choices=tuple(RESOURCES),
        help="resource to freeze/evaluate; defaults to renamed and memory",
    )
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()
    resources = args.resource or ["renamed", "memory"]
    result = evaluate(
        args.calibration_summary, args.held_out_summary, resources
    )
    write_outputs(result, args.output_prefix.resolve())


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error
