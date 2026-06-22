from ..observations import build_observation


def import_gem5_stats(path, mapping=None, **ci_kwargs):
    mapping = mapping or {}
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("-") or s.startswith("#"):
                continue
            if "#" in s:
                s = s.split("#", 1)[0].strip()
            parts = s.split()
            if len(parts) < 2:
                continue
            stat, raw = parts[0], parts[1]
            if mapping and stat not in mapping:
                continue
            name = mapping.get(stat, stat)
            try:
                values[name] = float(raw)
            except ValueError:
                continue
    return build_observation("gem5-stats", path, values, aggregation="snapshot", **ci_kwargs)

