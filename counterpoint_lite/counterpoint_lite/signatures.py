import csv
import itertools
from pathlib import Path

from .dsl import counter_names, dump_json


def enumerate_signatures(model):
    counters = counter_names(model)
    signatures = []
    for rule in model.get("rules", []):
        expanded = _expand_rule(rule, counters)
        for suffix, vector, bounds in expanded:
            sig_id = "sig." + rule["name"] + suffix
            signatures.append({
                "id": sig_id,
                "rule": rule["name"],
                "component": rule.get("component", "unknown"),
                "dimensions": rule.get("when", {}),
                "vector": vector,
                "bounds": bounds,
            })
    return {
        "schema_version": "0.1",
        "model": model.get("name"),
        "target": model.get("target", {}),
        "counters": counters,
        "signatures": _dedupe_signatures(signatures, counters),
    }


def _expand_rule(rule, counters):
    choices = []
    bounds = {}
    for c in counters:
        val = rule.get("signature", {}).get(c, 0.0)
        if isinstance(val, dict):
            lo = float(val["min"])
            hi = float(val["max"])
            mid = (lo + hi) / 2.0
            choices.append((c, [("min", lo), ("mid", mid), ("max", hi)]))
            bounds[c] = [lo, hi]
        else:
            choices.append((c, [("", float(val))]))

    interval_counters = [c for c, opts in choices if len(opts) > 1]
    products = itertools.product(*[opts for _, opts in choices])
    out = []
    for prod in products:
        vector = {c: value for (c, _), (_, value) in zip(choices, prod)}
        if interval_counters:
            parts = []
            for c, (tag, _) in zip([c for c, _ in choices], prod):
                if tag:
                    parts.append(f".{c.split('.')[-1]}_{tag}")
            suffix = "".join(parts)
        else:
            suffix = ""
        out.append((suffix, vector, dict(bounds)))
    return out


def _dedupe_signatures(signatures, counters):
    seen = set()
    out = []
    for sig in signatures:
        key = (sig.get("component"), tuple(round(float(sig["vector"].get(c, 0.0)), 12) for c in counters))
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def save_signatures_json(signatures, path):
    dump_json(signatures, path)


def save_signatures_csv(signatures, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    counters = signatures["counters"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["signature_id", "component", "rule"] + counters)
        for sig in signatures["signatures"]:
            writer.writerow([sig["id"], sig.get("component", "unknown"), sig.get("rule", "")] + [sig["vector"].get(c, 0.0) for c in counters])


def load_signatures(path):
    import json
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if "counters" not in obj or "signatures" not in obj:
        raise ValueError(f"{path}: not a signature JSON")
    return obj

