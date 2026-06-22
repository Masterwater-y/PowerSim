import re

from ..observations import build_observation


def import_key_value_text(path, mapping=None, **ci_kwargs):
    mapping = mapping or {}
    values = {}
    section = ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            section_match = _parse_section(s)
            if section_match:
                section = section_match
                continue
            if s.startswith("-"):
                continue
            if ":" in s:
                key, raw = s.split(":", 1)
            else:
                parts = s.split()
                if len(parts) < 2:
                    continue
                key, raw = " ".join(parts[:-1]), parts[-1]
            key = re.sub(r"\s+", " ", key.strip())
            full_key = f"{section}.{key}" if section else key
            mapped = _lookup_mapping(mapping, full_key, key)
            if mapping and mapped is None:
                continue
            try:
                value_match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", raw)
                if not value_match:
                    continue
                name = mapped or full_key
                values[name] = float(value_match.group(0))
            except ValueError:
                continue
    return build_observation("minesim-stats", path, values, aggregation="snapshot", **ci_kwargs)


def _parse_section(line):
    if line.startswith("===") and line.endswith("==="):
        return _clean_section(line.strip("= "))
    if line.startswith("---") and line.endswith("---"):
        body = line.strip("- ")
        if not body or set(body) == {"-"}:
            return None
        return _clean_section(body)
    return None


def _clean_section(section):
    section = section.replace(":", "")
    section = re.sub(r"\s+", " ", section.strip())
    return section


def _lookup_mapping(mapping, full_key, key):
    if full_key in mapping:
        return mapping[full_key]
    if key in mapping:
        return mapping[key]
    return None
