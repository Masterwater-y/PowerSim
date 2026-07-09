#!/usr/bin/env python3
"""Merge v26/v27 dataset parts into one tensor cache without a giant JSONL."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model import tokenizer as tk
from train.dataset import (
    MANIFEST_NAME,
    TENSOR_CACHE_FORMAT,
    build_cache_meta,
    build_cache_samples_from_jsonl,
    build_tensor_cache_shard,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--label-keys", required=True)
    ap.add_argument("--jsonl", action="append", default=[])
    ap.add_argument("--cache", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--lines-per-shard", type=int, default=512)
    ap.add_argument("--uop-field-schema", default="v26_14")
    ap.add_argument("--uop-field-count", type=int, default=tk.V26_UOP_FIELD_COUNT)
    args = ap.parse_args()

    label_keys = [x.strip() for x in args.label_keys.split(",") if x.strip()]
    if not label_keys:
        raise SystemExit("--label-keys must not be empty")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stub = out_dir / "windows.jsonl"
    cache_dir = out_dir / f"windows.maxlen{int(args.max_len)}.tensor_cache"
    tmp_dir = cache_dir / "_tmp_parts"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with stub.open("w") as f:
        json.dump({
            "direct_tensor_cache": True,
            "merged_tensor_cache": True,
            "uop_field_schema": args.uop_field_schema,
            "uop_field_count": int(args.uop_field_count),
            "label_keys": label_keys,
        }, f, separators=(",", ":"))
        f.write("\n")

    shards: list[dict] = []
    total = 0

    for src_idx, jsonl in enumerate(args.jsonl):
        path = Path(jsonl)
        if not path.exists():
            raise SystemExit(f"missing jsonl part: {path}")
        parts = split_jsonl(path, tmp_dir / f"jsonl-{src_idx:02d}",
                            int(args.lines_per_shard))
        print(f"[merge] jsonl={path} parts={len(parts)}", flush=True)
        with ProcessPoolExecutor(max_workers=max(1, int(args.jobs))) as ex:
            futs = {
                ex.submit(
                    process_jsonl_part,
                    str(part),
                    str(cache_dir),
                    f"shard-jsonl{src_idx:02d}-{part_idx:05d}.pt",
                    int(args.max_len),
                    label_keys,
                ): (part_idx, part)
                for part_idx, part in enumerate(parts)
            }
            for done_idx, fut in enumerate(as_completed(futs), start=1):
                info = fut.result()
                shards.append(info)
                total += int(info["count"])
                if done_idx % 16 == 0 or done_idx == len(futs):
                    print(
                        f"[merge] jsonl_done src={src_idx} "
                        f"{done_idx}/{len(futs)} total={total}",
                        flush=True,
                    )
        shutil.rmtree(tmp_dir / f"jsonl-{src_idx:02d}", ignore_errors=True)

    for src_idx, cache in enumerate(args.cache):
        cache_path = Path(cache)
        manifest_path = cache_path / MANIFEST_NAME
        if not manifest_path.exists():
            raise SystemExit(f"missing tensor cache manifest: {manifest_path}")
        import torch

        manifest = torch.load(manifest_path, map_location="cpu")
        if manifest.get("format") != TENSOR_CACHE_FORMAT:
            raise SystemExit(f"bad tensor cache format: {manifest_path}")
        for shard_idx, shard in enumerate(manifest.get("shards") or []):
            src = cache_path / shard["file"]
            if not src.exists():
                raise SystemExit(f"missing tensor shard: {src}")
            dst_name = f"shard-cache{src_idx:02d}-{shard_idx:05d}.pt"
            shutil.copy2(src, cache_dir / dst_name)
            count = int(shard["count"])
            total += count
            shards.append({
                "file": dst_name,
                "count": count,
                "max_n_core": int(shard.get("max_n_core", 0) or 0),
            })
        print(
            f"[merge] cache={cache_path} shards={len(manifest.get('shards') or [])} "
            f"total={total}",
            flush=True,
        )

    shards.sort(key=lambda x: x["file"])
    meta = build_cache_meta(
        str(stub),
        int(args.max_len),
        max_cores=tk.MAX_CORES,
        label_keys=label_keys,
    )
    save_torch_atomic(cache_dir / MANIFEST_NAME, {
        "format": TENSOR_CACHE_FORMAT,
        "meta": meta,
        "total_samples": int(total),
        "shards": shards,
    })
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"[merge] ready samples={total} cache={cache_dir}", flush=True)
    print(f"[merge] stub={stub}", flush=True)


def split_jsonl(src: Path, tmp_dir: Path, lines_per_shard: int) -> list[Path]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    buf: list[str] = []
    shard_idx = 0
    with src.open() as f:
        for line in f:
            if not line.lstrip().startswith("{"):
                continue
            buf.append(line)
            if len(buf) >= lines_per_shard:
                part = tmp_dir / f"part-{shard_idx:05d}.jsonl"
                part.write_text("".join(buf))
                parts.append(part)
                shard_idx += 1
                buf = []
    if buf:
        part = tmp_dir / f"part-{shard_idx:05d}.jsonl"
        part.write_text("".join(buf))
        parts.append(part)
    return parts


def process_jsonl_part(part_path: str, cache_dir: str, shard_name: str,
                       max_len: int, label_keys: list[str]) -> dict:
    samples = build_cache_samples_from_jsonl(
        part_path,
        max_len=max_len,
        label_keys=label_keys,
    )
    blob = build_tensor_cache_shard(samples)
    save_torch_atomic(Path(cache_dir) / shard_name, blob)
    return {
        "file": shard_name,
        "count": len(samples),
        "max_n_core": int(blob.get("max_n_core", 0)),
    }


def save_torch_atomic(path: Path, obj: object) -> None:
    import torch

    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
