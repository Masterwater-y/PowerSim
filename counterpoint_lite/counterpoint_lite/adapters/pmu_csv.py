import csv

from ..observations import build_observation


def import_pmu_csv(path, events=None, aliases=None, aggregation="sum", **ci_kwargs):
    aliases = aliases or {}
    selected = [e.strip() for e in events.split(",")] if isinstance(events, str) and events else None
    totals = {}
    samples = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty CSV")
        fields = selected or [x for x in reader.fieldnames if x not in ("ts_ns", "timestamp", "timestamp_ms", "time_name")]
        for row in reader:
            samples += 1
            for field in fields:
                if field not in row or row[field] in ("", None):
                    continue
                name = aliases.get(field, field)
                try:
                    value = float(row[field])
                except ValueError:
                    continue
                if aggregation == "mean":
                    totals[name] = totals.get(name, 0.0) + value
                else:
                    totals[name] = totals.get(name, 0.0) + value
    if aggregation == "mean" and samples:
        totals = {k: v / samples for k, v in totals.items()}
    obs = build_observation("pmu-csv", path, totals, aggregation=aggregation, **ci_kwargs)
    for c in obs["counters"]:
        c["samples"] = samples
    return obs

