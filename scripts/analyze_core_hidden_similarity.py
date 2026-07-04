#!/usr/bin/env python3
"""Analyze whether per-core query hidden states collapse to similar vectors."""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
from pathlib import Path
from typing import Iterable

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.eval_quota_cycles import load_model_and_tokenizer  # noqa: E402
from model.regression_head import PMU_KEYS  # noqa: E402
from train.dataset import (  # noqa: E402
    _build_local_core_sequences,
    _denom_vecs,
    _pad_side_feats,
    _remap_label,
    make_collate,
)
from train.loss import invert_pred  # noqa: E402


CPI_IDX = PMU_KEYS.index("cpi_uop")


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


def quantile(xs: Iterable[float], q: float) -> float:
    vals = sorted(finite(xs))
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def mean(xs: Iterable[float]) -> float:
    vals = finite(xs)
    return st.mean(vals) if vals else float("nan")


def pstdev(xs: Iterable[float]) -> float:
    vals = finite(xs)
    return st.pstdev(vals) if vals else float("nan")


def cv(xs: Iterable[float]) -> float:
    vals = finite(xs)
    if not vals:
        return float("nan")
    m = st.mean(vals)
    return st.pstdev(vals) / abs(m) if abs(m) > 1.0e-12 else float("nan")


def range_rel(xs: Iterable[float]) -> float:
    vals = finite(xs)
    if not vals:
        return float("nan")
    m = st.mean(vals)
    return (max(vals) - min(vals)) / abs(m) if abs(m) > 1.0e-12 else float("nan")


def mean_abs_relerr(pred: Iterable[float], label: Iterable[float]) -> float:
    pp = finite(pred)
    yy = finite(label)
    if len(pp) != len(yy) or not pp:
        return float("nan")
    return st.mean(
        abs(p - y) / (abs(y) + 1.0e-6) for p, y in zip(pp, yy)
    )


def summarize(xs: Iterable[float]) -> dict:
    vals = finite(xs)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": mean(vals),
        "p50": quantile(vals, 0.50),
        "p90": quantile(vals, 0.90),
        "p95": quantile(vals, 0.95),
        "min": min(vals),
        "max": max(vals),
    }


def pearson(a: list[float], b: list[float]) -> float:
    aa = finite(a)
    bb = finite(b)
    if len(aa) != len(bb) or len(aa) < 2:
        return float("nan")
    ma = st.mean(aa)
    mb = st.mean(bb)
    da = [x - ma for x in aa]
    db = [x - mb for x in bb]
    va = sum(x * x for x in da)
    vb = sum(x * x for x in db)
    if va <= 1.0e-20 or vb <= 1.0e-20:
        return float("nan")
    return sum(x * y for x, y in zip(da, db)) / math.sqrt(va * vb)


def encode_record(rec: dict, tok, max_len: int, max_cores: int,
                  input_mode: str = "global") -> dict | None:
    if int(rec.get("n_core", 0)) <= 0 or int(rec.get("n_core", 0)) > max_cores:
        return None
    tokens = rec.get("tokens")
    if not tokens:
        return None
    if len(tokens) > max_len:
        return None
    ids = tok.convert_tokens_to_ids(tokens)
    if any(i is None or i == tok.unk_token_id for i in ids):
        return None
    label = _remap_label(rec)
    if label is None:
        return None

    qpos = []
    lpos = []
    for ci in range(int(rec["n_core"])):
        qt = tok.convert_tokens_to_ids(f"<QUERY_C{ci}>")
        lt = tok.convert_tokens_to_ids(f"<LOCAL_C{ci}>")
        if qt not in ids:
            return None
        pos = len(ids) - 1 - ids[::-1].index(qt)
        qpos.append(pos)
        lpos.append(ids.index(lt) if lt in ids else pos)

    is_uop = rec.get("is_uop")
    if is_uop is None:
        is_uop = [1 if t == "<UOP>" else 0 for t in tokens]
    uop_fields = rec.get("uop_fields")
    if uop_fields is None:
        uop_fields = [[0, 0, 0, 0, 0, 0] for _ in ids]
    if len(is_uop) != len(ids) or len(uop_fields) != len(ids):
        return None

    item = {
        "ids": ids,
        "qpos": qpos,
        "local_pos": lpos,
        "label": label,
        "n_core": int(rec["n_core"]),
        "instr_retired": rec["instr_retired"],
        "uops": rec.get("uops_per_core", rec["instr_retired"]),
        "t_start_rel": rec.get("t_start_rel", [0.0] * int(rec["n_core"])),
        "is_uop": is_uop,
        "uop_fields": uop_fields,
        "side_feats": _pad_side_feats(rec.get("side_feats"), int(rec["n_core"])),
        "denoms": _denom_vecs(rec.get("denoms"), int(rec["n_core"])),
        "meta": {
            "id": rec.get("id", ""),
            "workload": rec.get("workload", ""),
        },
    }
    if input_mode == "local_core":
        local = _build_local_core_sequences({
            "tokens": tokens,
            "n_core": int(rec["n_core"]),
            "is_uop": is_uop,
            "uop_fields": uop_fields,
        }, ids, max_len)
        if local is None:
            return None
        item.update({
            "ids": ids[:1],
            "qpos": [0] * int(rec["n_core"]),
            "local_pos": [0] * int(rec["n_core"]),
            "is_uop": [0],
            "uop_fields": [[0, 0, 0, 0, 0, 0]],
        })
        item.update(local)
    return item


def iter_samples(args: argparse.Namespace, tok, input_mode: str):
    seen = 0
    kept = 0
    with open(args.data) as fh:
        for line in fh:
            s = line.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            if args.workload and rec.get("workload") != args.workload:
                continue
            if args.n_core and int(rec.get("n_core", 0)) != int(args.n_core):
                continue
            seen += 1
            if seen <= args.skip:
                continue
            sample = encode_record(
                rec, tok, args.max_len, args.max_cores,
                input_mode=input_mode)
            if sample is None:
                continue
            kept += 1
            yield sample
            if args.max_samples and kept >= args.max_samples:
                return


def gather_query_hidden(model, batch: dict, use_tstart: bool,
                        device: str) -> tuple[dict[str, torch.Tensor], torch.Tensor]:
    input_ids = batch["input_ids"].to(device)
    attention_mask = batch["attention_mask"].to(device)
    query_pos = batch["query_pos"].to(device)
    local_pos = batch["local_pos"].to(device)
    core_mask = batch["core_mask"].to(device)
    is_uop = batch["is_uop"].to(device)
    uop_fields = batch["uop_fields"].to(device)
    side_feats = batch["side_feats"].to(device)
    t_start = batch["t_start"].to(device) if use_tstart else None

    if getattr(model.cfg, "model_input_mode", "global") == "local_core":
        local_input_ids = batch["local_input_ids"].to(device)
        local_attention_mask = batch["local_attention_mask"].to(device)
        local_query_pos = batch["local_query_pos"].to(device)
        local_is_uop = batch["local_is_uop"].to(device)
        local_uop_fields = batch["local_uop_fields"].to(device)
        valid = core_mask.to(torch.bool)
        flat_ids = local_input_ids[valid]
        flat_attn = local_attention_mask[valid]
        flat_pos = local_query_pos[valid]
        flat_is_uop = local_is_uop[valid]
        flat_uop_fields = local_uop_fields[valid]

        tok_emb = model.backbone.get_input_embeddings()(flat_ids)
        safe_fields = flat_uop_fields.clamp(min=0)
        uop_emb = model.uop_encoder(safe_fields).to(tok_emb.dtype)
        inputs_embeds = tok_emb.clone()
        mask = flat_is_uop.to(torch.bool)
        inputs_embeds[mask] = uop_emb[mask]

        out = model.backbone(inputs_embeds=inputs_embeds,
                             attention_mask=flat_attn)
        hs = out.last_hidden_state
        idx = flat_pos.unsqueeze(-1).unsqueeze(-1).expand(-1, 1, hs.size(-1))
        flat_hidden = torch.gather(hs, 1, idx).squeeze(1)
        query_hidden = torch.zeros(
            (core_mask.size(0), core_mask.size(1), hs.size(-1)),
            device=hs.device, dtype=hs.dtype)
        query_hidden[valid] = flat_hidden
    else:
        tok_emb = model.backbone.get_input_embeddings()(input_ids)
        safe_fields = uop_fields.clamp(min=0)
        uop_emb = model.uop_encoder(safe_fields).to(tok_emb.dtype)
        inputs_embeds = tok_emb.clone()
        mask = is_uop.to(torch.bool)
        inputs_embeds[mask] = uop_emb[mask]

        out = model.backbone(inputs_embeds=inputs_embeds,
                             attention_mask=attention_mask)
        hs = out.last_hidden_state
        idx = query_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))
        query_hidden = torch.gather(hs, 1, idx)
    stages = {"query": query_hidden.float()}

    if getattr(model.cfg, "model_input_mode", "global") == "local_core":
        query_hidden = query_hidden + model.local_proj(query_hidden)
    else:
        lidx = local_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))
        local_hidden = torch.gather(hs, 1, lidx)
        query_hidden = query_hidden + model.local_proj(local_hidden)
    if t_start is not None:
        ts = torch.log1p(t_start.clamp(min=0).to(query_hidden.dtype))
        query_hidden = query_hidden + model.tstart_proj(ts.unsqueeze(-1))
    query_hidden = query_hidden + model.side_proj(side_feats.to(query_hidden.dtype))
    stages["pre_adapter"] = query_hidden.float()

    if model.core_adapter is not None:
        query_hidden = model.core_adapter(query_hidden, core_mask)
    stages["post_adapter"] = query_hidden.float()

    raw = model.head(query_hidden, core_mask=core_mask)
    pred = invert_pred(raw.float())
    return stages, pred.float()


def hidden_metrics(h: torch.Tensor) -> dict:
    h = h.float()
    c, _ = h.shape
    if c < 2:
        return {}
    hn = torch.nn.functional.normalize(h, dim=-1)
    cos = hn @ hn.t()
    off = cos[~torch.eye(c, dtype=torch.bool, device=cos.device)]
    center = h.mean(dim=0, keepdim=True)
    diff = h - center
    norm = h.norm(dim=-1).mean().clamp(min=1.0e-12)
    center_rel = diff.norm(dim=-1).mean() / norm
    rms_rel = torch.sqrt((diff * diff).mean()) / torch.sqrt((h * h).mean()).clamp(min=1.0e-12)
    svals = torch.linalg.svdvals(diff)
    power = svals * svals
    if torch.sum(power) > 0:
        eff_rank = (torch.sum(power) ** 2 / torch.sum(power * power)).item()
        top1_frac = (power.max() / power.sum()).item()
    else:
        eff_rank = 0.0
        top1_frac = 0.0
    return {
        "pair_cos_mean": float(off.mean().item()),
        "pair_cos_p95": float(torch.quantile(off, 0.95).item()),
        "pair_cos_min": float(off.min().item()),
        "center_rel_norm": float(center_rel.item()),
        "rms_rel": float(rms_rel.item()),
        "effective_rank": float(eff_rank),
        "pca_top1_frac": float(top1_frac),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--workload", default="W_ads_ranking_proxy")
    ap.add_argument("--n-core", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--max-cores", type=int, default=32)
    ap.add_argument("--max-samples", type=int, default=64)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--device", default=None)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    model_args = argparse.Namespace(ckpt=args.ckpt, max_len=args.max_len)
    model, tok, use_tstart = load_model_and_tokenizer(model_args, device)
    input_mode = getattr(model.cfg, "model_input_mode", "global")
    collate = make_collate(tok.pad_token_id)

    rows = []
    batch_samples = []
    n_samples = 0
    with torch.no_grad():
        for sample in iter_samples(args, tok, input_mode):
            batch_samples.append(sample)
            if len(batch_samples) < args.batch_size:
                continue
            batch = collate(batch_samples)
            stages, pred = gather_query_hidden(model, batch, use_tstart, device)
            labels = batch["label"].float()
            core_mask = batch["core_mask"].bool()
            for bi, sample_b in enumerate(batch_samples):
                active = core_mask[bi]
                y = labels[bi, active, CPI_IDX].tolist()
                p = pred[bi, active, CPI_IDX].cpu().tolist()
                row = {
                    "id": sample_b.get("meta", {}).get("id", ""),
                    "workload": sample_b.get("meta", {}).get("workload", ""),
                    "n_core": int(active.sum().item()),
                    "label_cpi_cv": cv(y),
                    "pred_cpi_cv": cv(p),
                    "label_cpi_range_rel": range_rel(y),
                    "pred_cpi_range_rel": range_rel(p),
                    "cpi_abs_relerr_mean": mean_abs_relerr(p, y),
                    "pred_label_cpi_corr": pearson(p, y),
                    "label_slowest": int(torch.tensor(y).argmax().item()),
                    "pred_slowest": int(torch.tensor(p).argmax().item()),
                    "label_fastest": int(torch.tensor(y).argmin().item()),
                    "pred_fastest": int(torch.tensor(p).argmin().item()),
                }
                for stage_name, h in stages.items():
                    met = hidden_metrics(h[bi, active].cpu())
                    for k, v in met.items():
                        row[f"{stage_name}_{k}"] = v
                rows.append(row)
                n_samples += 1
            batch_samples = []
        if batch_samples:
            batch = collate(batch_samples)
            stages, pred = gather_query_hidden(model, batch, use_tstart, device)
            labels = batch["label"].float()
            core_mask = batch["core_mask"].bool()
            for bi, sample_b in enumerate(batch_samples):
                active = core_mask[bi]
                y = labels[bi, active, CPI_IDX].tolist()
                p = pred[bi, active, CPI_IDX].cpu().tolist()
                row = {
                    "id": sample_b.get("meta", {}).get("id", ""),
                    "workload": sample_b.get("meta", {}).get("workload", ""),
                    "n_core": int(active.sum().item()),
                    "label_cpi_cv": cv(y),
                    "pred_cpi_cv": cv(p),
                    "label_cpi_range_rel": range_rel(y),
                    "pred_cpi_range_rel": range_rel(p),
                    "cpi_abs_relerr_mean": mean_abs_relerr(p, y),
                    "pred_label_cpi_corr": pearson(p, y),
                    "label_slowest": int(torch.tensor(y).argmax().item()),
                    "pred_slowest": int(torch.tensor(p).argmax().item()),
                    "label_fastest": int(torch.tensor(y).argmin().item()),
                    "pred_fastest": int(torch.tensor(p).argmin().item()),
                }
                for stage_name, h in stages.items():
                    met = hidden_metrics(h[bi, active].cpu())
                    for k, v in met.items():
                        row[f"{stage_name}_{k}"] = v
                rows.append(row)
                n_samples += 1

    if not rows:
        raise SystemExit("[err] no matching samples")

    summary = {
        "ckpt": args.ckpt,
        "data": args.data,
        "workload": args.workload,
        "n_core": args.n_core,
        "samples": n_samples,
        "use_tstart": bool(use_tstart),
        "model_input_mode": input_mode,
        "metrics": {},
    }
    metric_keys = sorted(k for k in rows[0] if k not in {
        "id", "workload", "n_core", "label_slowest", "pred_slowest",
        "label_fastest", "pred_fastest",
    })
    for k in metric_keys:
        summary["metrics"][k] = summarize(row.get(k) for row in rows)
    summary["slowest_hit_rate"] = mean(
        1.0 if r["label_slowest"] == r["pred_slowest"] else 0.0 for r in rows
    )
    summary["fastest_hit_rate"] = mean(
        1.0 if r["label_fastest"] == r["pred_fastest"] else 0.0 for r in rows
    )

    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
        print(f"[wrote] {out}", flush=True)


if __name__ == "__main__":
    main()
