"""Lightweight IO helpers used across the pipeline."""
from __future__ import annotations

import gzip
import json
import os
from typing import Any, Iterable, Iterator, Optional


def open_maybe_gzip(path: str, mode: str = "rt"):
    if path.endswith(".gz"):
        return gzip.open(path, mode, encoding="utf-8") if "t" in mode else gzip.open(path, mode)
    return open(path, mode, encoding="utf-8") if "t" in mode else open(path, mode)


def iter_jsonl(path: str) -> Iterator[dict]:
    with open_maybe_gzip(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def write_jsonl(path: str, rows: Iterable[dict]) -> int:
    ensure_dir(os.path.dirname(path))
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
            n += 1
    return n


def ensure_dir(path: Optional[str]) -> None:
    if not path:
        return
    os.makedirs(path, exist_ok=True)


def dump_json(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
