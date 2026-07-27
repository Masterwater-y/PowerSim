#!/usr/bin/env python3
"""Convert per-core raw jsonl traces into aligned parquet.

Each output row merges one records.micro row and one labels.micro row on
(thread_id, micro_seq), preserving source program order. This avoids repeated
json parsing and join work in downstream dataset builders.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq


CORE_RE = re.compile(r"(?:cores|switch)(\d*)\.core")
REC_SUFFIX = ".records.micro.jsonl"
LAB_SUFFIX = ".labels.micro.jsonl"
OUT_SUFFIX = ".aligned.parquet"

ROW_GROUP_SIZE = 65536


def merged_schema() -> pa.Schema:
    return pa.schema([
        pa.field("core_id", pa.int32()),
        pa.field("thread_id", pa.int32()),
        pa.field("micro_seq", pa.int64()),
        pa.field("seq_num", pa.int64()),
        pa.field("macro_pc", pa.uint64()),
        pa.field("micro_pc", pa.uint64()),
        pa.field("vaddr", pa.uint64()),
        pa.field("paddr", pa.uint64()),
        pa.field("cacheline_addr", pa.uint64()),
        pa.field("cacheline_paddr", pa.uint64()),
        pa.field("size", pa.uint16()),
        pa.field("is_load", pa.uint8()),
        pa.field("is_store", pa.uint8()),
        pa.field("is_atomic", pa.uint8()),
        pa.field("is_branch", pa.uint8()),
        pa.field("is_branch_cond", pa.uint8()),
        pa.field("is_branch_indirect", pa.uint8()),
        pa.field("is_call", pa.uint8()),
        pa.field("is_return", pa.uint8()),
        pa.field("branch_taken", pa.uint8()),
        pa.field("branch_target", pa.uint64()),
        pa.field("branch_next_pc", pa.uint64()),
        pa.field("branch_history", pa.uint16()),
        pa.field("is_int", pa.uint8()),
        pa.field("is_fp", pa.uint8()),
        pa.field("is_simd", pa.uint8()),
        pa.field("is_serialize", pa.uint8()),
        pa.field("is_microop", pa.uint8()),
        pa.field("is_last_microop", pa.uint8()),
        pa.field("op_class", pa.int16()),
        pa.field("n_src", pa.uint8()),
        pa.field("n_dst", pa.uint8()),
        pa.field("producer_dists", pa.list_(pa.uint32(), 4)),
        pa.field("producer_classes", pa.list_(pa.uint8(), 4)),
        pa.field("path_class", pa.int16()),
        pa.field("coh_oracle", pa.int16()),
        pa.field("i_path_class", pa.int16()),
        pa.field("d_mshr_depth", pa.int16()),
        pa.field("dtlb_hit", pa.int16()),
        pa.field("itlb_hit", pa.int16()),
        pa.field("fetch_tick", pa.int64()),
        pa.field("issue_tick", pa.int64()),
        pa.field("complete_tick", pa.int64()),
        pa.field("commit_tick", pa.int64()),
        pa.field("ready_tick", pa.int64()),
        pa.field("ready_source", pa.int32()),
        pa.field("mispredicted", pa.int32()),
    ])


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", required=True,
                    help="Root containing workload directories")
    ap.add_argument("--out-root", default=None,
                    help="Optional output root; default writes beside source")
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--row-group-size", type=int, default=ROW_GROUP_SIZE)
    return ap.parse_args()


def iter_jsonl(path: Path, bad_counter: Optional[dict] = None) -> Iterator[dict]:
    bad = 0
    with path.open() as f:
        for lno, ln in enumerate(f, start=1):
            s = ln.strip()
            if not s.startswith("{"):
                continue
            try:
                yield json.loads(s)
            except json.JSONDecodeError as e:
                bad += 1
                if bad <= 5:
                    print(
                        f"[warn] skip malformed json path={path} line={lno} "
                        f"err={e}",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
    if bad_counter is not None:
        bad_counter[str(path)] = bad


def key_of(row: dict) -> Tuple[int, int]:
    return int(row["thread_id"]), int(row["micro_seq"])


def fixed_list(vals: object, n: int) -> list[int]:
    if not isinstance(vals, list):
        vals = []
    out = [int(v) for v in vals[:n]]
    if len(out) < n:
        out.extend([0] * (n - len(out)))
    return out


def zero_for_field(field: pa.Field):
    if pa.types.is_list(field.type):
        return [0] * field.type.list_size
    return 0


def empty_columns(schema: pa.Schema) -> Dict[str, list]:
    return {field.name: [] for field in schema}


def flush_chunk(writer: pq.ParquetWriter, schema: pa.Schema,
                cols: Dict[str, list]) -> int:
    n_rows = len(cols[schema.names[0]]) if schema.names else 0
    if n_rows == 0:
        return 0
    table = pa.Table.from_pydict(cols, schema=schema)
    writer.write_table(table)
    for name in cols:
        cols[name].clear()
    return n_rows


def build_merged_row(core_id: int, rec: dict, lab: dict) -> dict:
    row = {
        "core_id": int(rec.get("core_id", core_id)),
        "thread_id": int(rec["thread_id"]),
        "micro_seq": int(rec["micro_seq"]),
        "seq_num": int(rec.get("seq_num", 0)),
        "macro_pc": int(rec.get("macro_pc", 0)),
        "micro_pc": int(rec.get("micro_pc", 0)),
        "vaddr": int(rec.get("vaddr", 0)),
        "paddr": int(rec.get("paddr", 0)),
        "cacheline_addr": int(rec.get("cacheline_addr", 0)),
        "cacheline_paddr": int(rec.get("cacheline_paddr", 0)),
        "size": int(rec.get("size", 0)),
        "is_load": int(rec.get("is_load", 0)),
        "is_store": int(rec.get("is_store", 0)),
        "is_atomic": int(rec.get("is_atomic", 0)),
        "is_branch": int(rec.get("is_branch", 0)),
        "is_branch_cond": int(rec.get("is_branch_cond", 0)),
        "is_branch_indirect": int(rec.get("is_branch_indirect", 0)),
        "is_call": int(rec.get("is_call", 0)),
        "is_return": int(rec.get("is_return", 0)),
        "branch_taken": int(rec.get("branch_taken", 0)),
        "branch_target": int(rec.get("branch_target", 0)),
        "branch_next_pc": int(rec.get("branch_next_pc", 0)),
        "branch_history": int(rec.get("branch_history", 0)),
        "is_int": int(rec.get("is_int", 0)),
        "is_fp": int(rec.get("is_fp", 0)),
        "is_simd": int(rec.get("is_simd", 0)),
        "is_serialize": int(rec.get("is_serialize", 0)),
        "is_microop": int(rec.get("is_microop", 0)),
        "is_last_microop": int(rec.get("is_last_microop", 0)),
        "op_class": int(rec.get("op_class", 0)),
        "n_src": int(rec.get("n_src", 0)),
        "n_dst": int(rec.get("n_dst", 0)),
        "producer_dists": fixed_list(rec.get("producer_dists"), 4),
        "producer_classes": fixed_list(rec.get("producer_classes"), 4),
        "path_class": int(rec.get("path_class", 0)),
        "coh_oracle": int(rec.get("coh_oracle", 0)),
        "i_path_class": int(rec.get("i_path_class", 0)),
        "d_mshr_depth": int(rec.get("d_mshr_depth", 0)),
        "dtlb_hit": int(rec.get("dtlb_hit", 1)),
        "itlb_hit": int(rec.get("itlb_hit", 1)),
        "fetch_tick": int(lab.get("fetch_tick", 0)),
        "issue_tick": int(lab.get("issue_tick", 0)),
        "complete_tick": int(lab.get("complete_tick", 0)),
        "commit_tick": int(lab.get("commit_tick", 0)),
        "ready_tick": int(lab.get("ready_tick", 0)),
        "ready_source": int(lab.get("ready_source", 0)),
        "mispredicted": int(lab.get("mispredicted", 0)),
    }
    return row


def check_monotonic(prev_key: Optional[Tuple[int, int]],
                    cur_key: Tuple[int, int], path: Path) -> None:
    if prev_key is not None and cur_key < prev_key:
        raise ValueError(f"{path} is not monotonic at key {cur_key} < {prev_key}")


def next_row(it: Iterator[dict]) -> Optional[dict]:
    return next(it, None)


def source_files(trace_dir: Path) -> Dict[int, dict]:
    out: Dict[int, dict] = defaultdict(dict)
    for p in trace_dir.glob(f"*{REC_SUFFIX}"):
        m = CORE_RE.search(p.name)
        if m:
            out[int(m.group(1) or 0)]["rec"] = p
    for p in trace_dir.glob(f"*{LAB_SUFFIX}"):
        m = CORE_RE.search(p.name)
        if m:
            out[int(m.group(1) or 0)]["lab"] = p
    return {c: v for c, v in out.items() if "rec" in v and "lab" in v}


def output_path(rec_path: Path, raw_root: Path, out_root: Optional[Path]) -> Path:
    if out_root is None:
        return rec_path.with_name(rec_path.name.replace(REC_SUFFIX, OUT_SUFFIX))
    rel = rec_path.relative_to(raw_root)
    out_rel = str(rel).replace(REC_SUFFIX, OUT_SUFFIX)
    return out_root / out_rel


def convert_pair(core_id: int, rec_path: Path, lab_path: Path, out_path: Path,
                 row_group_size: int) -> dict:
    schema = merged_schema()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = empty_columns(schema)
    t0 = time.time()
    n_out = 0
    dropped_rec = 0
    dropped_lab = 0
    prev_rec_key: Optional[Tuple[int, int]] = None
    prev_lab_key: Optional[Tuple[int, int]] = None

    bad_json = {}
    rec_it = iter_jsonl(rec_path, bad_json)
    lab_it = iter_jsonl(lab_path, bad_json)
    rec = next_row(rec_it)
    lab = next_row(lab_it)

    writer = pq.ParquetWriter(
        out_path, schema=schema, compression="zstd",
        use_dictionary=True, write_batch_size=row_group_size,
    )
    try:
        # outer-join 风格：rec 为主线，每条 rec µop 都产出一行；
        # lab 缺失（末尾不齐 / 中段单边丢）时用空 dict 兜 0，下游 commit_tick=0
        # 的行用于 macro 计数（分母与 gem5.numInsts 对齐），cycles 端点差则
        # 跳过 commit_tick=0 的行（仍按真实 lab 端点）。
        while rec is not None:
            rec_key = key_of(rec)
            check_monotonic(prev_rec_key, rec_key, rec_path)
            prev_rec_key = rec_key

            if lab is None:
                # lab 已 EOF：rec 末尾不齐部分全部用空 lab 写出
                merged = build_merged_row(core_id, rec, {})
                for field in schema:
                    cols[field.name].append(
                        merged.get(field.name, zero_for_field(field)))
                if len(cols["thread_id"]) >= row_group_size:
                    n_out += flush_chunk(writer, schema, cols)
                dropped_lab += 1
                rec = next_row(rec_it)
                continue

            lab_key = key_of(lab)
            check_monotonic(prev_lab_key, lab_key, lab_path)
            prev_lab_key = lab_key

            if rec_key == lab_key:
                merged = build_merged_row(core_id, rec, lab)
                rec = next_row(rec_it)
                lab = next_row(lab_it)
            elif rec_key < lab_key:
                # rec 这条没有匹配的 lab → 用空 lab
                merged = build_merged_row(core_id, rec, {})
                dropped_lab += 1
                rec = next_row(rec_it)
            else:
                # lab 多出一条 rec 不存在的 µop → rec 是 µop 主体载体，
                # 没 rec 没法构造行，跳过 lab
                dropped_rec += 1
                lab = next_row(lab_it)
                continue

            for field in schema:
                cols[field.name].append(
                    merged.get(field.name, zero_for_field(field)))
            if len(cols["thread_id"]) >= row_group_size:
                n_out += flush_chunk(writer, schema, cols)

        n_out += flush_chunk(writer, schema, cols)
    finally:
        writer.close()

    return {
        "core_id": core_id,
        "rows": n_out,
        "dropped_rec": dropped_rec,
        "dropped_lab": dropped_lab,
        "bad_json_rec": bad_json.get(str(rec_path), 0),
        "bad_json_lab": bad_json.get(str(lab_path), 0),
        "seconds": time.time() - t0,
        "out": str(out_path),
    }


def workloads_under(raw_root: Path, wanted: Optional[list[str]]) -> list[str]:
    wdirs = sorted([
        d.name for d in raw_root.iterdir()
        if d.is_dir() and d.name.startswith("W")
    ])
    if wanted:
        wanted_set = set(wanted)
        wdirs = [w for w in wdirs if w in wanted_set]
    return wdirs


def main() -> None:
    args = parse_args()
    raw_root = Path(args.raw_root)
    out_root = Path(args.out_root) if args.out_root else None
    workloads = workloads_under(raw_root, args.workloads)
    if not workloads:
        raise SystemExit("[err] no workloads found")

    for wd in workloads:
        trace_dir = raw_root / wd / "tao_trace"
        if not trace_dir.is_dir():
            print(f"[skip] {wd}: missing {trace_dir}", file=sys.stderr)
            continue
        files = source_files(trace_dir)
        if not files:
            print(f"[skip] {wd}: no jsonl trace pairs", file=sys.stderr)
            continue
        print(f"[start] {wd}: {len(files)} cores", flush=True)
        for core_id, fps in sorted(files.items()):
            out_path = output_path(fps["rec"], raw_root, out_root)
            if out_path.exists() and not args.overwrite:
                print(f"[skip] {wd}: core{core_id} exists {out_path}", flush=True)
                continue
            info = convert_pair(core_id, fps["rec"], fps["lab"], out_path,
                                args.row_group_size)
            print(
                f"[done] {wd}: core{core_id} rows={info['rows']} "
                f"dropped_rec={info['dropped_rec']} dropped_lab={info['dropped_lab']} "
                f"bad_json_rec={info['bad_json_rec']} bad_json_lab={info['bad_json_lab']} "
                f"time={info['seconds']:.1f}s out={info['out']}",
                flush=True,
            )


if __name__ == "__main__":
    main()
