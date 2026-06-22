import csv

from ..observations import build_observation


def import_perf_stat(path, aliases=None, **ci_kwargs):
    aliases = aliases or {}
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            raw_value = row[0].strip().replace(",", "")
            event = row[2].strip()
            if not raw_value or raw_value.startswith("<") or not event:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            name = aliases.get(event, event)
            values[name] = values.get(name, 0.0) + value
    return build_observation("perf-stat", path, values, aggregation="snapshot", **ci_kwargs)

