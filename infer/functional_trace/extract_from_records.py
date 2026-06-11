#!/usr/bin/env python3
"""Project records/labels traces to deploy-side functional/label files."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from schema import (  # noqa: E402
    FUNCTIONAL_TRACE_SCHEMA, LABEL_SCHEMA,
    project_label_row, project_record_row,
)


def infer_core_id(path: str) -> int:
    base = os.path.basename(path)
    marker = "cores"
    if marker in base:
        s = base.split(marker, 1)[1]
        return int(s.split(".", 1)[0])
    return -1


def write_rows(rows, out_path: str, fmt: str, schema: pa.Schema) -> int:
    n = 0
    if fmt == "jsonl":
        with open(out_path, "w") as fout:
            for row in rows:
                fout.write(json.dumps(row, separators=(",", ":")))
                fout.write("\n")
                n += 1
        return n

    writer = None
    batch = []
    try:
        for row in rows:
            batch.append(row)
            n += 1
            if len(batch) >= 100_000:
                table = pa.Table.from_pylist(batch, schema=schema)
                if writer is None:
                    writer = pq.ParquetWriter(out_path, schema=schema, compression="zstd")
                writer.write_table(table)
                batch.clear()
        if batch:
            table = pa.Table.from_pylist(batch, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(out_path, schema=schema, compression="zstd")
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    return n


def iter_json_rows(in_path: str, projector):
    with open(in_path) as fin:
        for line in fin:
            if not line.startswith("{"):
                continue
            yield projector(json.loads(line))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace-dir", required=True,
                    help="Directory containing *records.micro.jsonl files")
    ap.add_argument("--out-dir", required=True,
                    help="Output directory for functional.core<N>.(parquet|jsonl)")
    ap.add_argument("--format", choices=("parquet", "jsonl"), default="parquet")
    ap.add_argument("--labels-trace-dir",
                    help="Directory containing *labels.micro.jsonl files")
    ap.add_argument("--labels-out-dir",
                    help="Output directory for labels.core<N>.(parquet|jsonl)")
    args = ap.parse_args()

    trace_dir = os.path.abspath(args.trace_dir)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    labels_dir = os.path.abspath(args.labels_out_dir) if args.labels_out_dir else None
    if args.labels_trace_dir and labels_dir:
        os.makedirs(labels_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(trace_dir, "*records.micro.jsonl")))
    if not files:
        raise SystemExit(f"no records.micro files found under {trace_dir}")

    total = 0
    manifest = []
    for path in files:
        cid = infer_core_id(path)
        if cid < 0:
            raise SystemExit(f"cannot infer core id from {path}")
        ext = "parquet" if args.format == "parquet" else "jsonl"
        out = os.path.join(out_dir, f"functional.core{cid}.{ext}")
        n = write_rows(
            iter_json_rows(path, project_record_row),
            out, args.format, FUNCTIONAL_TRACE_SCHEMA,
        )
        rec = {"core_id": cid, "path": out, "rows": n}
        if args.labels_trace_dir and labels_dir:
            label_in = os.path.join(os.path.abspath(args.labels_trace_dir),
                                    os.path.basename(path).replace("records.micro", "labels.micro"))
            if not os.path.isfile(label_in):
                raise SystemExit(f"missing label file for core {cid}: {label_in}")
            label_out = os.path.join(labels_dir, f"labels.core{cid}.{ext}")
            ln = write_rows(
                iter_json_rows(label_in, project_label_row),
                label_out, args.format, LABEL_SCHEMA,
            )
            rec["labels_path"] = label_out
            rec["labels_rows"] = ln
        manifest.append(rec)
        total += n
        print(f"[functional] core={cid} rows={n} -> {out}", file=sys.stderr)

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump({"total_rows": total, "files": manifest}, f, indent=2)
    print(f"[functional] total_rows={total}", file=sys.stderr)


if __name__ == "__main__":
    main()
