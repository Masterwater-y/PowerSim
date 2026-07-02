#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import statistics as st
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.regression_head import PMU_KEYS
from model.tokenizer import SIDE_FEATURE_KEYS


META_INDEX_FORMAT = "window_meta_index_v1"


def finite(xs: Iterable[float]) -> list[float]:
    out = []
    for x in xs:
        try:
            v = float(x)
        except Exception:
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def q(xs: Iterable[float], quantile: float) -> float:
    vals = sorted(finite(xs))
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return vals[0]
    pos = quantile * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def mean(xs: Iterable[float]) -> float:
    vals = finite(xs)
    return st.mean(vals) if vals else float("nan")


def cv(xs: Iterable[float]) -> float:
    vals = finite(xs)
    if not vals:
        return float("nan")
    m = st.mean(vals)
    if abs(m) <= 1e-12:
        return float("nan")
    return st.pstdev(vals) / abs(m)


def summary(name: str, xs: Iterable[float]) -> str:
    vals = finite(xs)
    if not vals:
        return f"{name}: n=0"
    return (
        f"{name}: n={len(vals)} mean={mean(vals):.6g} "
        f"p50={q(vals, 0.50):.6g} p90={q(vals, 0.90):.6g} "
        f"p95={q(vals, 0.95):.6g} min={min(vals):.6g} max={max(vals):.6g}"
    )


def rel_pos(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def extract_str(line: str, key: str, default: str = "") -> str:
    marker = f'"{key}":"'
    start = line.find(marker)
    if start < 0:
        return default
    start += len(marker)
    end = line.find('"', start)
    if end < 0:
        return default
    return line[start:end]


def extract_num(line: str, key: str, default: float = 0.0) -> float:
    marker = f'"{key}":'
    start = line.find(marker)
    if start < 0:
        return default
    start += len(marker)
    end = start
    while end < len(line) and line[end] not in ",}]":
        end += 1
    raw = line[start:end].strip()
    if not raw or raw == "null":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def metadata_index_path(cache_dir: Path) -> Path:
    return cache_dir / "metadata_index.pt"


def build_or_load_metadata_index(jsonl_path: Path, cache_dir: Path,
                                 force: bool = False) -> dict:
    out = metadata_index_path(cache_dir)
    st_jsonl = jsonl_path.stat()
    expected_meta = {
        "jsonl_path": str(jsonl_path.resolve()),
        "jsonl_size": int(st_jsonl.st_size),
        "jsonl_mtime_ns": int(st_jsonl.st_mtime_ns),
    }
    if out.exists() and not force:
        try:
            obj = torch.load(out, map_location="cpu")
            if (
                isinstance(obj, dict)
                and obj.get("format") == META_INDEX_FORMAT
                and obj.get("meta") == expected_meta
            ):
                return obj
        except Exception:
            pass

    workload: list[str] = []
    sample_id: list[str] = []
    cfg_hash: list[str] = []
    mode: list[str] = []
    n_core = []
    w_ops = []
    target_fill = []
    fill_ratio = []
    t_start_tick = []
    t_end_tick = []
    tq_span_tick = []
    stride_tick = []
    end_skew_cycle = []
    legacy_token_len = []

    print(f"[meta] build index from {rel_pos(jsonl_path)}", flush=True)
    with jsonl_path.open() as fh:
        for idx, line in enumerate(fh):
            if not line.lstrip().startswith("{"):
                continue
            workload.append(extract_str(line, "workload"))
            sample_id.append(extract_str(line, "id"))
            cfg_hash.append(extract_str(line, "cfg_hash"))
            mode.append(extract_str(line, "mode"))
            n_core.append(int(extract_num(line, "n_core", 0)))
            w_ops.append(int(extract_num(line, "w_ops", 0)))
            target_fill.append(float(extract_num(line, "target_fill", 0.0)))
            fill_ratio.append(float(extract_num(line, "fill_ratio", 0.0)))
            t_start_tick.append(int(extract_num(line, "t_start_tick", 0)))
            t_end_tick.append(int(extract_num(line, "t_end_tick", 0)))
            tq_span_tick.append(int(extract_num(line, "tq_span_tick", 0)))
            stride_tick.append(int(extract_num(line, "stride_tick", 0)))
            end_skew_cycle.append(float(extract_num(line, "end_skew_cycle", 0.0)))
            legacy_token_len.append(int(extract_num(line, "legacy_token_len", 0)))
            if (idx + 1) % 5000 == 0:
                print(f"[meta] indexed lines={idx + 1}", flush=True)

    obj = {
        "format": META_INDEX_FORMAT,
        "meta": expected_meta,
        "workload": workload,
        "sample_id": sample_id,
        "cfg_hash": cfg_hash,
        "mode": mode,
        "n_core": torch.tensor(n_core, dtype=torch.int16),
        "w_ops": torch.tensor(w_ops, dtype=torch.int32),
        "target_fill": torch.tensor(target_fill, dtype=torch.float32),
        "fill_ratio": torch.tensor(fill_ratio, dtype=torch.float32),
        "t_start_tick": torch.tensor(t_start_tick, dtype=torch.int64),
        "t_end_tick": torch.tensor(t_end_tick, dtype=torch.int64),
        "tq_span_tick": torch.tensor(tq_span_tick, dtype=torch.int64),
        "stride_tick": torch.tensor(stride_tick, dtype=torch.int64),
        "end_skew_cycle": torch.tensor(end_skew_cycle, dtype=torch.float32),
        "legacy_token_len": torch.tensor(legacy_token_len, dtype=torch.int32),
    }
    tmp = out.with_suffix(out.suffix + ".tmp")
    torch.save(obj, tmp)
    tmp.replace(out)
    print(f"[meta] saved {rel_pos(out)} samples={len(workload)}", flush=True)
    return obj


def selected_indices(meta: dict, workload: str | None, n_core: int | None) -> list[int]:
    out = []
    n_core_tensor = meta["n_core"]
    workloads = meta["workload"]
    for i, wl in enumerate(workloads):
        if workload is not None and wl != workload:
            continue
        if n_core is not None and int(n_core_tensor[i]) != n_core:
            continue
        out.append(i)
    return out


def shard_plan(manifest: dict, indices: list[int]) -> dict[int, list[int]]:
    cum = []
    total = 0
    for shard in manifest["shards"]:
        total += int(shard["count"])
        cum.append(total)
    plan: dict[int, list[int]] = defaultdict(list)
    for idx in indices:
        shard_idx = bisect_right(cum, idx)
        shard_start = 0 if shard_idx == 0 else cum[shard_idx - 1]
        plan[shard_idx].append(idx - shard_start)
    return plan


def group_stats(cache_dir: Path, manifest: dict, meta: dict,
                indices: list[int], name: str) -> dict:
    cpi_idx = PMU_KEYS.index("cpi_uop")
    llc_idx = PMU_KEYS.index("llc_miss")
    dtlb_idx = PMU_KEYS.index("dtlb_miss")
    plan = shard_plan(manifest, indices)

    win_cpi = []
    win_cpi_unweighted = []
    core_cpi = []
    core_cv = []
    core_spread = []
    core_max = []
    core_min = []
    win_uops = []
    core_uops = []
    fill_ratio = []
    end_skew = []
    legacy_token_len = []
    llc_sum = []
    dtlb_sum = []
    side_values: dict[str, list[float]] = {
        k: [] for k in SIDE_FEATURE_KEYS
    }

    for shard_idx, local_indices in sorted(plan.items()):
        shard_info = manifest["shards"][shard_idx]
        shard = torch.load(cache_dir / shard_info["file"], map_location="cpu")
        for li in local_indices:
            nc = int(shard["n_core"][li])
            cpi = shard["label"][li, :nc, cpi_idx].float()
            uops = shard["uops"][li, :nc].float().clamp_min(0.0)
            labels = cpi.tolist()
            uops_list = uops.tolist()
            denom = float(uops.sum())
            if denom > 0:
                win_cpi.append(float((cpi * uops).sum() / denom))
            win_cpi_unweighted.append(mean(labels))
            core_cpi.extend(labels)
            core_cv.append(cv(labels))
            core_spread.append(max(labels) - min(labels))
            core_max.append(max(labels))
            core_min.append(min(labels))
            win_uops.append(sum(uops_list))
            core_uops.extend(uops_list)
            llc_sum.append(float(shard["label"][li, :nc, llc_idx].sum()))
            dtlb_sum.append(float(shard["label"][li, :nc, dtlb_idx].sum()))
            side = shard["side_feats"][li, :nc].float()
            for fi, key in enumerate(SIDE_FEATURE_KEYS):
                side_values[key].extend(side[:, fi].tolist())

    for idx in indices:
        fill_ratio.append(float(meta["fill_ratio"][idx]))
        end_skew.append(float(meta["end_skew_cycle"][idx]))
        legacy_token_len.append(float(meta["legacy_token_len"][idx]))

    phase_bins = {
        "lt1": sum(1 for x in win_cpi if x < 1.0),
        "1to3": sum(1 for x in win_cpi if 1.0 <= x < 3.0),
        "3to6": sum(1 for x in win_cpi if 3.0 <= x < 6.0),
        "ge6": sum(1 for x in win_cpi if x >= 6.0),
    }
    side_focus = [
        "log1p_uops_core",
        "core_fill_ratio",
        "log1p_load_count",
        "log1p_mem_ops",
        "log1p_global_distinct_data_lines",
        "log1p_global_distinct_data_pages",
        "aggregate_load_density",
        "aggregate_mem_density",
        "global_large_stride_rate",
        "random_access_pressure",
        "lines_per_kuop_global",
        "pages_per_kuop_global",
        "core_random_load_density",
    ]
    return {
        "name": name,
        "windows": len(indices),
        "win_cpi": win_cpi,
        "win_cpi_unweighted": win_cpi_unweighted,
        "core_cpi": core_cpi,
        "core_cv": core_cv,
        "core_spread": core_spread,
        "core_max": core_max,
        "core_min": core_min,
        "win_uops": win_uops,
        "core_uops": core_uops,
        "fill_ratio": fill_ratio,
        "end_skew": end_skew,
        "legacy_token_len": legacy_token_len,
        "llc_sum": llc_sum,
        "dtlb_sum": dtlb_sum,
        "phase_bins": phase_bins,
        "side_focus": {
            key: side_values[key] for key in side_focus if key in side_values
        },
    }


def eval_dump_stats(path: Path, name: str, skip_first: int) -> dict:
    import json

    win_label = []
    win_pred = []
    label_cv = []
    pred_cv = []
    label_spread = []
    pred_spread = []
    win_uops = []
    rows = 0
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if int(r.get("window", 0)) < skip_first:
                continue
            cores = r.get("cores") or []
            if not cores:
                continue
            labels = [float(c["label"]["cpi_uop"]) for c in cores]
            preds = [float(c["pred"]["cpi_uop"]) for c in cores]
            uops = [float(c.get("uops", 0.0)) for c in cores]
            if any(not math.isfinite(x) for x in labels + preds + uops):
                continue
            denom = sum(uops)
            if denom <= 0:
                continue
            rows += 1
            win_label.append(sum(y * u for y, u in zip(labels, uops)) / denom)
            win_pred.append(sum(y * u for y, u in zip(preds, uops)) / denom)
            label_cv.append(cv(labels))
            pred_cv.append(cv(preds))
            label_spread.append(max(labels) - min(labels))
            pred_spread.append(max(preds) - min(preds))
            win_uops.append(denom)
    return {
        "name": name,
        "windows": rows,
        "win_cpi": win_label,
        "pred_win_cpi": win_pred,
        "core_cv": label_cv,
        "pred_core_cv": pred_cv,
        "core_spread": label_spread,
        "pred_core_spread": pred_spread,
        "win_uops": win_uops,
    }


def print_train_stats(s: dict) -> None:
    print(f"\n## {s['name']}")
    print(f"windows={s['windows']}")
    for key, label in [
        ("win_cpi", "window_cpi_weighted"),
        ("win_cpi_unweighted", "window_cpi_unweighted"),
        ("core_cpi", "core_cpi"),
        ("core_cv", "core_cpi_cv"),
        ("core_spread", "core_cpi_spread"),
        ("core_max", "core_cpi_max"),
        ("win_uops", "window_uops"),
        ("core_uops", "core_uops"),
        ("fill_ratio", "fill_ratio"),
        ("end_skew", "end_skew_cycle"),
        ("legacy_token_len", "legacy_token_len"),
        ("llc_sum", "llc_miss_sum"),
        ("dtlb_sum", "dtlb_miss_sum"),
    ]:
        print(summary(label, s[key]))
    print("phase_bins_by_weighted_window_cpi=" + repr(s["phase_bins"]))
    print("side_feature_focus_mean_p50_p95:")
    for key, vals in s["side_focus"].items():
        print(
            f"  {key}: mean={mean(vals):.6g} "
            f"p50={q(vals, 0.50):.6g} p95={q(vals, 0.95):.6g}"
        )


def print_eval_stats(s: dict) -> None:
    print(f"\n## {s['name']}")
    print(f"windows={s['windows']}")
    print(summary("eval_label_window_cpi", s["win_cpi"]))
    print(summary("eval_pred_window_cpi", s["pred_win_cpi"]))
    print(summary("eval_label_core_cv", s["core_cv"]))
    print(summary("eval_pred_core_cv", s["pred_core_cv"]))
    print(summary("eval_label_core_spread", s["core_spread"]))
    print(summary("eval_pred_core_spread", s["pred_core_spread"]))
    print(summary("eval_window_uops", s["win_uops"]))
    label_mean = mean(s["win_cpi"])
    pred_mean = mean(s["pred_win_cpi"])
    rel = abs(pred_mean - label_mean) / abs(label_mean) if abs(label_mean) > 1e-12 else float("nan")
    print(f"mean_pred_vs_label_relerr={rel:.6g}")


def parse_named_path(spec: str) -> tuple[str, Path]:
    if "=" in spec:
        name, path = spec.split("=", 1)
        return name, Path(path)
    path = Path(spec)
    return path.stem, path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="windows.jsonl")
    ap.add_argument("--cache", required=True, help="tensor_cache dir")
    ap.add_argument("--workload", default="W_phased_mix")
    ap.add_argument("--n-core", type=int, default=16)
    ap.add_argument("--eval-dump", action="append", default=[],
                    help="name=path to *.windows.jsonl; can repeat")
    ap.add_argument("--skip-first", type=int, default=1)
    ap.add_argument("--force-meta-index", action="store_true")
    args = ap.parse_args()

    data = Path(args.data)
    cache_dir = Path(args.cache)
    manifest = torch.load(cache_dir / "manifest.pt", map_location="cpu")
    meta = build_or_load_metadata_index(data, cache_dir, args.force_meta_index)
    total = int(manifest["total_samples"])
    if total != len(meta["workload"]):
        raise SystemExit(
            f"metadata/cache sample count mismatch: cache={total} "
            f"meta={len(meta['workload'])}"
        )

    target = selected_indices(meta, args.workload, args.n_core)
    same_core = selected_indices(meta, None, args.n_core)
    same_workload_all_core = selected_indices(meta, args.workload, None)

    print(f"data={rel_pos(data)}")
    print(f"cache={rel_pos(cache_dir)}")
    print(f"workload={args.workload} n_core={args.n_core}")
    print(f"total_samples={total}")
    print(f"target_samples={len(target)}")
    print(f"same_core_samples={len(same_core)}")
    print(f"same_workload_all_core_samples={len(same_workload_all_core)}")

    print_train_stats(group_stats(
        cache_dir, manifest, meta, target,
        f"train {args.workload} c{args.n_core:02d}",
    ))
    print_train_stats(group_stats(
        cache_dir, manifest, meta, same_core,
        f"train all workloads c{args.n_core:02d}",
    ))
    print_train_stats(group_stats(
        cache_dir, manifest, meta, same_workload_all_core,
        f"train {args.workload} all cores",
    ))

    for spec in args.eval_dump:
        name, path = parse_named_path(spec)
        print_eval_stats(eval_dump_stats(path, name, args.skip_first))


if __name__ == "__main__":
    main()
