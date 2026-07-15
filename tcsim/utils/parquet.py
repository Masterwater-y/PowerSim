"""Minimal parquet writer/reader using pyarrow when available, else json fallback.

MVP: we store `chunks` and `labels` as parquet if pyarrow is installed; otherwise
we fall back to JSONL (`<name>.parquet.jsonl`). Both keep the same schema — the
downstream code only sees a list of dicts.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional, Sequence

try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore
    _HAS_PARQUET = True
except Exception:
    pa = None
    pq = None
    _HAS_PARQUET = False


def write_table(path: str, rows: Sequence[dict], schema_cols: Optional[List[str]] = None) -> str:
    """Write `rows` to parquet at `path`; if pyarrow is unavailable, fall back to
    a JSONL sidecar and return the actual path used.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if _HAS_PARQUET and rows:
        cols = schema_cols or sorted({k for r in rows for k in r.keys()})
        arrays = []
        for c in cols:
            col_vals = [r.get(c) for r in rows]
            arrays.append(pa.array(col_vals))
        table = pa.Table.from_arrays(arrays, names=list(cols))
        pq.write_table(table, path)
        return path
    fallback = path + ".jsonl"
    with open(fallback, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r))
            fh.write("\n")
    return fallback


def read_table(path: str) -> List[dict]:
    if _HAS_PARQUET and os.path.exists(path):
        table = pq.read_table(path)
        return table.to_pylist()
    fallback = path if path.endswith(".jsonl") else path + ".jsonl"
    if not os.path.exists(fallback):
        raise FileNotFoundError(path)
    out: List[dict] = []
    with open(fallback, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def has_parquet() -> bool:
    return _HAS_PARQUET
