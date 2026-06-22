import csv
import json
import math
from pathlib import Path

from .dsl import dump_json


def build_observation(source_kind, source_path, values, target=None, ci_scale=3.0,
                      min_relative_ci=0.01, min_absolute_ci=1.0, aggregation="sum"):
    counters = []
    for name, raw in values.items():
        value = float(raw)
        poisson = ci_scale * math.sqrt(max(abs(value), 1.0))
        relative = abs(value) * min_relative_ci
        radius = max(poisson, relative, min_absolute_ci)
        counters.append({
            "name": name,
            "value": value,
            "ci_low": max(0.0, value - radius),
            "ci_high": value + radius,
            "stddev": None,
            "samples": None,
            "unit": "count",
        })
    return {
        "schema_version": "0.1",
        "source": {"kind": source_kind, "path": str(source_path), "aggregation": aggregation},
        "target": target or {},
        "counters": counters,
        "normalization": {"mode": "none"},
    }


def load_observation(path):
    with open(path, "r", encoding="utf-8") as f:
        obs = json.load(f)
    if "counters" not in obs:
        raise ValueError(f"{path}: not an observation JSON")
    return obs


def save_observation(obs, path):
    dump_json(obs, path)


def observation_vectors(obs, counters):
    by_name = {c["name"]: c for c in obs.get("counters", [])}
    y, lo, hi, missing = [], [], [], []
    for name in counters:
        item = by_name.get(name)
        if item is None:
            missing.append(name)
            y.append(0.0)
            lo.append(0.0)
            hi.append(float("inf"))
        else:
            value = float(item["value"])
            y.append(value)
            lo.append(float(item.get("ci_low", value)))
            hi.append(float(item.get("ci_high", value)))
    return y, lo, hi, missing


def write_violations_csv(report, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = ["counter", "observed", "ci_low", "ci_high", "predicted", "violation", "normalized_violation", "status", "component_hint"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in report.get("counters", []):
            out = {k: row.get(k, "") for k in fields}
            out["counter"] = row.get("counter", row.get("name", ""))
            writer.writerow(out)
