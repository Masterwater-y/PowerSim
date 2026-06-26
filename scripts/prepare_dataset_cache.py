#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.llm_wrapper import build_tokenizer
from model import tokenizer as tk
from train.dataset import (
    MANIFEST_NAME,
    WindowDataset,
    build_cache_meta,
    build_cache_samples_from_jsonl,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="windows.jsonl path")
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--cache-out", default=None)
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    ap.add_argument("--lines-per-shard", type=int, default=512)
    args = ap.parse_args()

    data = Path(args.data)
    if not data.exists():
        raise SystemExit(f"[cache] missing dataset: {data}")

    tok = build_tokenizer()
    cache_path = args.cache_out or WindowDataset.default_cache_path(
        str(data), args.max_len
    )
    cache_dir = Path(cache_path)
    tmp_dir = cache_dir / "_tmp_parts"
    print(f"[cache] data={data}", flush=True)
    print(f"[cache] max_len={args.max_len}", flush=True)
    print(f"[cache] out={cache_path}", flush=True)
    print(f"[cache] jobs={args.jobs} lines_per_shard={args.lines_per_shard}",
          flush=True)

    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    meta = build_cache_meta(str(data), tok, args.max_len, max_cores=tk.MAX_CORES)
    part_files = split_jsonl(data, tmp_dir, args.lines_per_shard)
    print(f"[cache] split parts={len(part_files)}", flush=True)

    shards = []
    total_samples = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futs = {
            ex.submit(process_part, i, str(part), str(cache_dir), args.max_len): i
            for i, part in enumerate(part_files)
        }
        for done_idx, fut in enumerate(as_completed(futs), start=1):
            info = fut.result()
            shards.append(info)
            total_samples += info["count"]
            print(
                f"[cache] shard_done {done_idx}/{len(part_files)} "
                f"file={info['file']} samples={info['count']}",
                flush=True,
            )
    shards.sort(key=lambda x: x["file"])

    manifest = {
        "meta": meta,
        "total_samples": total_samples,
        "shards": shards,
    }
    save_torch_atomic(cache_dir / MANIFEST_NAME, manifest)

    cleanup_parts(part_files, tmp_dir)
    print(f"[cache] ready total_samples={total_samples}", flush=True)
    print(f"[cache] meta={json.dumps(meta, ensure_ascii=True)}", flush=True)


def split_jsonl(src: Path, tmp_dir: Path, lines_per_shard: int) -> list[Path]:
    parts: list[Path] = []
    buf: list[str] = []
    shard_idx = 0
    total = 0
    with src.open() as f:
        for line in f:
            if not line.lstrip().startswith("{"):
                continue
            buf.append(line)
            total += 1
            if len(buf) >= lines_per_shard:
                part = tmp_dir / f"part-{shard_idx:05d}.jsonl"
                part.write_text("".join(buf))
                parts.append(part)
                shard_idx += 1
                buf = []
                if shard_idx % 8 == 0:
                    print(f"[cache] split_progress lines={total} parts={shard_idx}",
                          flush=True)
    if buf:
        part = tmp_dir / f"part-{shard_idx:05d}.jsonl"
        part.write_text("".join(buf))
        parts.append(part)
    return parts


def process_part(part_idx: int, part_path: str, cache_dir: str,
                 max_len: int) -> dict:
    tok = build_tokenizer()
    samples = build_cache_samples_from_jsonl(part_path, tok, max_len=max_len)
    shard_name = f"shard-{part_idx:05d}.pt"
    save_torch_atomic(Path(cache_dir) / shard_name, {"samples": samples})
    return {"file": shard_name, "count": len(samples)}


def save_torch_atomic(path: Path, obj: object) -> None:
    import torch

    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    os.replace(tmp, path)


def cleanup_parts(parts: list[Path], tmp_dir: Path) -> None:
    for p in parts:
        if p.exists():
            p.unlink()
    if tmp_dir.exists():
        tmp_dir.rmdir()


if __name__ == "__main__":
    main()
