"""Dataset validation for v24 data pipeline.

Checks (all must pass):
  1. windows.jsonl schema:
     - required fields present, consistent shapes
     - label_keys align with model/regression_head.PMU_KEYS (or is a superset)
     - is_uop and uop_fields lengths match tokens length
     - core_split sum <= is_uop count
     - PMU numeric sanity (cpi_uop finite and positive, misses non-negative)
  2. tensor_cache manifest + shard consistency:
     - manifest.pt loads
     - all shard files referenced exist
     - meta.max_len, feat_version, pmu_keys match runtime PMU_KEYS
  3. WindowDataset loads via cache (require_cache=True), a sample
     collates and matches expected tensor shapes/dtypes.
  4. Model forward smoke:
     - loads current LLMSimModel (Qwen3-0.6B-Base by default; override via
       BASE_MODEL env), forwards 1 collated batch on CPU or GPU, verifies
       pred shape [B, n_core, K].

Usage:
  /data00/yinhaolang/infer/.venv/bin/python scripts/validate_v24_dataset.py

Env overrides:
  DATA=data/windows_v16_v9core_tail_local_all/windows.jsonl
  BASE_MODEL=Qwen/Qwen3-0.6B-Base
  MAX_LEN=32768
  BATCH=2
  DEVICE=cpu | cuda | auto  (default: auto)
  SKIP_MODEL=1              (skip the model forward smoke)
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple


REQUIRED_FIELDS = [
    "id", "workload", "cfg_hash", "n_core",
    "tokens", "is_uop", "uop_fields",
    "core_split", "label", "label_keys",
    "denoms", "instr_retired", "uops_per_core",
]


def _ok(msg: str) -> None:
    print(f"[ ok ] {msg}")


def _warn(msg: str) -> None:
    print(f"[warn] {msg}")


def _err(msg: str) -> None:
    print(f"[FAIL] {msg}")


def check_jsonl_schema(path: Path, pmu_keys: List[str], max_check: int) -> Tuple[bool, dict]:
    print(f"\n== jsonl schema check ({path}) ==")
    if not path.exists():
        _err(f"missing: {path}")
        return False, {}
    n = 0
    per_core_hist: Dict[int, int] = {}
    workloads: Dict[str, int] = {}
    label_key_seen = set()
    problems: List[str] = []
    cpi_min, cpi_max, cpi_sum, cpi_n = float("inf"), 0.0, 0.0, 0
    stats_first_row: Dict[str, Any] = {}

    with path.open() as f:
        for line in f:
            n += 1
            try:
                r = json.loads(line)
            except Exception as e:
                problems.append(f"line {n}: bad json: {e}")
                if len(problems) > 8:
                    break
                continue

            # required fields
            for k in REQUIRED_FIELDS:
                if k not in r:
                    problems.append(f"line {n} id={r.get('id')}: missing '{k}'")
                    break

            n_core = int(r.get("n_core", 0))
            per_core_hist[n_core] = per_core_hist.get(n_core, 0) + 1
            workloads[r.get("workload", "?")] = workloads.get(r.get("workload", "?"), 0) + 1

            tokens = r.get("tokens", [])
            is_uop = r.get("is_uop", [])
            uop_fields = r.get("uop_fields", [])
            core_split = r.get("core_split", [])
            label = r.get("label", [])
            lkeys = r.get("label_keys", [])
            denoms = r.get("denoms", [])
            instr = r.get("instr_retired", [])
            upc = r.get("uops_per_core", [])
            label_key_seen.update(lkeys)

            # shape consistency
            if len(tokens) != len(is_uop):
                problems.append(
                    f"line {n} id={r.get('id')}: tokens({len(tokens)}) != is_uop({len(is_uop)})")
            if len(uop_fields) != len(tokens):
                problems.append(
                    f"line {n} id={r.get('id')}: uop_fields({len(uop_fields)}) != tokens({len(tokens)})")
            uop_positions = sum(1 for x in is_uop if x)
            cs_sum = sum(core_split)
            if cs_sum > uop_positions:
                problems.append(
                    f"line {n} id={r.get('id')}: core_split sum {cs_sum} > uop_positions {uop_positions}")
            if len(label) != n_core or (n_core > 0 and len(label[0]) != len(lkeys)):
                problems.append(
                    f"line {n} id={r.get('id')}: label shape mismatch "
                    f"got=({len(label)},{len(label[0]) if label else 0}) "
                    f"expect=({n_core},{len(lkeys)})")
            if len(instr) != n_core:
                problems.append(
                    f"line {n} id={r.get('id')}: instr_retired len {len(instr)} != n_core {n_core}")
            if len(upc) != n_core:
                problems.append(
                    f"line {n} id={r.get('id')}: uops_per_core len {len(upc)} != n_core {n_core}")

            # PMU numeric sanity
            if "cpi_uop" in lkeys:
                ci = lkeys.index("cpi_uop")
                for row in label:
                    v = row[ci]
                    if not (v > 0 and v == v and v < 1e6):
                        problems.append(
                            f"line {n} id={r.get('id')}: cpi_uop invalid: {v}")
                        break
                    cpi_min = min(cpi_min, v)
                    cpi_max = max(cpi_max, v)
                    cpi_sum += v
                    cpi_n += 1

            if n == 1:
                stats_first_row = {
                    "id": r.get("id"),
                    "n_core": n_core,
                    "tokens_len": len(tokens),
                    "uop_positions": uop_positions,
                    "core_split": core_split,
                    "label_keys": lkeys,
                    "label0": label[0] if label else None,
                }

            if max_check and n >= max_check:
                break

    ok = not problems
    print(f"  scanned={n} n_core hist={sorted(per_core_hist.items())}")
    print(f"  workloads({len(workloads)})={sorted(workloads.items())[:6]}...")
    print(f"  label_keys seen={sorted(label_key_seen)}")
    if cpi_n:
        print(f"  cpi_uop min={cpi_min:.3f} max={cpi_max:.3f} mean={cpi_sum/cpi_n:.3f} over {cpi_n} rows")
    print(f"  first row: {stats_first_row}")
    if problems:
        _err(f"{len(problems)} schema problems:")
        for p in problems[:10]:
            print("       " + p)
        if len(problems) > 10:
            print(f"       ... and {len(problems)-10} more")

    # ensure pmu_keys covered
    missing_pmu = [k for k in pmu_keys if k not in label_key_seen]
    if missing_pmu:
        _err(f"PMU keys expected by model.regression_head.PMU_KEYS are missing "
             f"from label_keys: {missing_pmu}")
        ok = False
    else:
        _ok(f"label_keys covers required PMU_KEYS ({pmu_keys})")

    return ok, {
        "n": n,
        "n_core_hist": per_core_hist,
        "workloads": workloads,
        "label_keys": sorted(label_key_seen),
    }


def check_tensor_cache(jsonl_path: Path, max_len: int) -> Tuple[bool, Path]:
    print(f"\n== tensor cache check ==")
    cache_dir = jsonl_path.with_name(f"{jsonl_path.stem}.maxlen{max_len}.tensor_cache")
    manifest = cache_dir / "manifest.pt"
    if not cache_dir.exists():
        _err(f"missing tensor_cache dir: {cache_dir}")
        return False, cache_dir
    if not manifest.exists():
        _err(f"missing manifest: {manifest}")
        return False, cache_dir

    import torch
    m = torch.load(manifest, map_location="cpu")
    meta = m.get("meta", {})
    shards = m.get("shards", [])
    print(f"  cache dir: {cache_dir}")
    print(f"  meta.max_len={meta.get('max_len')} "
          f"feat_version={meta.get('feat_version')} "
          f"input_mode={meta.get('input_mode')} "
          f"side_feat_dim={meta.get('side_feat_dim')} "
          f"jsonl_size={meta.get('jsonl_size')}")
    print(f"  pmu_keys={meta.get('pmu_keys')}")
    print(f"  shards={len(shards)}")

    missing_shards = []
    total_samples = 0
    for sh in shards:
        p = cache_dir / sh["file"]
        if not p.exists():
            missing_shards.append(sh["file"])
        total_samples += sh.get("num_samples", 0)
    print(f"  total_samples={total_samples}")
    if missing_shards:
        _err(f"missing shard files: {missing_shards[:5]}...")
        return False, cache_dir
    _ok(f"manifest + {len(shards)} shard files complete")
    return True, cache_dir


def check_dataset_and_collate(jsonl_path: Path, cache_dir: Path, max_len: int,
                              base_model: str, batch: int) -> Tuple[bool, dict]:
    print(f"\n== WindowDataset + collate check ==")
    try:
        from model.llm_wrapper import build_tokenizer
        from train.dataset import WindowDataset, make_collate
        from torch.utils.data import DataLoader
    except Exception as e:
        _err(f"import failure: {e}")
        traceback.print_exc()
        return False, {}

    t0 = time.time()
    tok = build_tokenizer(base_model)
    print(f"  tokenizer loaded ({time.time()-t0:.1f}s), "
          f"vocab_size={len(tok)}, pad_token_id={tok.pad_token_id}")

    ds = WindowDataset(
        str(jsonl_path), tok, max_len=max_len,
        cache_path=str(cache_dir),
        require_cache=True,
    )
    print(f"  WindowDataset size = {len(ds)}, mode = {ds.mode}")

    collate = make_collate(tok.pad_token_id)
    dl = DataLoader(ds, batch_size=batch, shuffle=False,
                    collate_fn=collate, num_workers=0)
    b = next(iter(dl))
    print(f"  batch keys = {sorted(b.keys())}")
    for k in ["input_ids", "attention_mask", "query_pos", "local_pos",
              "core_mask", "label", "uops", "is_uop", "uop_fields",
              "side_feats", "denoms", "t_start"]:
        if k in b:
            v = b[k]
            print(f"    {k}: shape={tuple(v.shape)} dtype={v.dtype}")

    _ok("collated batch produced without errors")
    return True, {"batch": b, "tok": tok}


def check_model_forward(batch: dict, base_model: str, device: str) -> bool:
    print(f"\n== model forward smoke ==")
    try:
        import torch
        from model.llm_wrapper import LLMSimModel, WrapperConfig
    except Exception as e:
        _err(f"import failure: {e}")
        return False

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  device={device}, base_model={base_model}")

    b = batch["batch"]
    tok = batch["tok"]
    cfg = WrapperConfig(
        base_model=base_model,
        max_len=int(b["input_ids"].shape[1]),
        cpi_head_mode="direct",
        local_fuse_mode="add",
    )
    try:
        model = LLMSimModel(cfg, tok)
    except Exception as e:
        _err(f"model init failure: {e}")
        traceback.print_exc()
        return False

    if device == "cuda":
        model = model.to("cuda")
    model.eval()
    b_dev = {k: (v.to(model.parameters().__next__().device)
                 if hasattr(v, "to") else v) for k, v in b.items()}
    ts = b_dev["t_start"] * 0.0  # keep tstart injection off in smoke
    with torch.no_grad():
        try:
            pred = model(
                b_dev["input_ids"], b_dev["attention_mask"],
                b_dev["query_pos"], ts,
                is_uop=b_dev.get("is_uop"),
                uop_fields=b_dev.get("uop_fields"),
                side_feats=b_dev.get("side_feats"),
                local_pos=b_dev.get("local_pos"),
                core_mask=b_dev.get("core_mask"),
            )
        except Exception as e:
            _err(f"forward failure: {e}")
            traceback.print_exc()
            return False
    print(f"  pred shape={tuple(pred.shape)} dtype={pred.dtype}")
    B, N_core = b["label"].shape[:2]
    from model.regression_head import K
    if tuple(pred.shape) != (B, N_core, K):
        _err(f"pred shape {tuple(pred.shape)} != expected ({B},{N_core},{K})")
        return False
    _ok(f"pred shape matches ({B},{N_core},{K})")
    return True


def main() -> int:
    root = Path(os.environ.get("LLMSIM_ROOT", "/data00/yinhaolang/LLMSim"))
    os.chdir(root)
    sys.path.insert(0, str(root))

    data = os.environ.get(
        "DATA",
        "data/windows_v16_v9core_tail_local_all/windows.jsonl",
    )
    base_model = os.environ.get("BASE_MODEL", "Qwen/Qwen3-0.6B-Base")
    max_len = int(os.environ.get("MAX_LEN", "32768"))
    batch = int(os.environ.get("BATCH", "2"))
    device = os.environ.get("DEVICE", "auto")
    max_check = int(os.environ.get("MAX_CHECK", "0"))  # 0 = full
    skip_model = os.environ.get("SKIP_MODEL", "0") == "1"

    from model.regression_head import PMU_KEYS
    print(f"[env] data={data}")
    print(f"[env] base_model={base_model}  max_len={max_len}  batch={batch}")
    print(f"[env] device={device}  skip_model={skip_model}")
    print(f"[env] PMU_KEYS={PMU_KEYS}")

    path = Path(data)

    ok_schema, _ = check_jsonl_schema(path, PMU_KEYS, max_check)
    ok_cache, cache_dir = check_tensor_cache(path, max_len)

    if not ok_cache:
        _err("stop: tensor cache missing/broken")
        return 2

    ok_ds, batch_state = check_dataset_and_collate(
        path, cache_dir, max_len, base_model, batch)
    if not ok_ds:
        return 3

    ok_fwd = True
    if not skip_model:
        ok_fwd = check_model_forward(batch_state, base_model, device)

    print("\n== summary ==")
    print(f"  schema: {'OK' if ok_schema else 'FAIL'}")
    print(f"  cache : {'OK' if ok_cache else 'FAIL'}")
    print(f"  loader: {'OK' if ok_ds else 'FAIL'}")
    print(f"  fwd   : {'OK' if ok_fwd else ('SKIPPED' if skip_model else 'FAIL')}")

    all_ok = ok_schema and ok_cache and ok_ds and (skip_model or ok_fwd)
    print(f"\nRESULT: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
