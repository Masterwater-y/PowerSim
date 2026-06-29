#!/usr/bin/env python3
"""Small v9 schema smoke test without collecting full data.

The script builds a synthetic two-core window, exercises PMU aggregation,
composite-uop serialization, global tokens, side features, and, when torch is
available, the dataset/collate/UopEncoder/PMULoss path.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from model import tokenizer as tk
from data.build_windows import (
    PMU_KEYS,
    aggregate_pmu,
    annotate_functional_proxies,
    annotate_rd_stride,
    encode_multicore_sample,
)


def _rec(core: int, seq: int, *, load=0, store=0, atomic=0,
         branch=0, cond=0, indir=0, pc=0, vaddr=0x1000, tick=100) -> dict:
    return {
        "core_id": core,
        "thread_id": core,
        "micro_seq": seq,
        "macro_pc": 0x400000 + seq * 4,
        "micro_pc": 0,
        "vaddr": vaddr,
        "cacheline_addr": vaddr & ~63,
        "size": 8,
        "is_load": load,
        "is_store": store,
        "is_atomic": atomic,
        "is_branch": branch,
        "is_branch_cond": cond,
        "is_branch_indirect": indir,
        "is_call": 0,
        "is_return": 0,
        "is_int": 1,
        "is_fp": 0,
        "is_simd": 0,
        "is_serialize": 0,
        "is_microop": 1,
        "is_last_microop": 1,
        "op_class": 56 if load else (57 if (store or atomic) else 1),
        "n_src": 1,
        "n_dst": 1,
        "producer_dists": [0, 0, 0, 0],
        "producer_classes": [0, 255, 255, 255],
        "path_class": pc,
        "i_path_class": 0,
        "dtlb_hit": 1,
        "itlb_hit": 1,
        "coh_oracle": 0,
        "d_mshr_depth": 0,
        "_commit_tick": tick,
        "_mispredicted": 1 if branch else 0,
    }


class _FakeTokenizer:
    def __init__(self):
        toks = tk.all_special_tokens()
        self.vocab = {tok: i + 2 for i, tok in enumerate(toks)}
        self.pad_token_id = 0
        self.unk_token_id = 1

    def convert_tokens_to_ids(self, value):
        if isinstance(value, list):
            return [self.vocab.get(tok, self.unk_token_id) for tok in value]
        return self.vocab.get(value, self.unk_token_id)

    def __len__(self):
        return len(self.vocab) + 2


def build_sample() -> tuple[dict, str]:
    wins = {
        0: [
            _rec(0, 1, store=1, pc=2, vaddr=0x2000, tick=100),
            _rec(0, 2, load=1, pc=4, vaddr=0x2008, tick=150),
            _rec(0, 3, branch=1, cond=1, tick=200),
        ],
        1: [
            _rec(1, 1, store=1, pc=2, vaddr=0x2010, tick=105),
            _rec(1, 2, load=1, pc=0, vaddr=0x3000, tick=160),
            _rec(1, 3, branch=1, indir=1, tick=220),
        ],
    }
    for seq in wins.values():
        annotate_rd_stride(seq)
        annotate_functional_proxies(seq)

    per_core = {}
    labels = []
    for c in [0, 1]:
        pmu = aggregate_pmu(wins[c], 10)
        assert pmu is not None
        assert "l2_ld_miss" in pmu and "l2_st_miss" in pmu
        per_core[c] = (wins[c], pmu)
        labels.append([pmu[k] for k in PMU_KEYS])

    sample = encode_multicore_sample(
        [],
        labels,
        per_core,
        [0, 1],
        {"cfg_tokens": {}, "tick_per_cycle": 10},
        {
            "id": "smoke",
            "workload": "W_smoke",
            "cfg_hash": "smoke",
            "n_core": 2,
            "t_start_rel": [0.0, 0.5],
        },
    )

    root = tempfile.mkdtemp(prefix="llmsim_v9_smoke_")
    path = os.path.join(root, "windows.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")
    return sample, path


def main() -> None:
    sample, path = build_sample()
    assert sample["label_keys"] == PMU_KEYS
    assert "<UOP>" in sample["tokens"]
    assert any(tok.startswith("<G_NCORE_") for tok in sample["tokens"])
    assert len(sample["tokens"]) == len(sample["is_uop"])
    assert len(sample["tokens"]) == len(sample["uop_fields"])
    assert len(sample["tokens"]) == len(sample["is_attn_feat"])
    assert len(sample["tokens"]) == len(sample["attn_feat_ids"])
    assert len(sample["tokens"]) == len(sample["attn_feat_values"])
    assert sum(sample["is_uop"]) == 6
    assert sum(sample["is_attn_feat"]) == (
        len(tk.GLOBAL_ATTN_FEATURE_KEYS)
        + 2 * len(tk.CORE_ATTN_FEATURE_KEYS)
    )
    assert len(sample["side_feats"]) == 2
    assert len(sample["side_feats"][0]) == len(tk.SIDE_FEATURE_KEYS)

    print("OK schema smoke")
    print("pmu_keys=", PMU_KEYS)
    print("tokens=", len(sample["tokens"]),
          "uop_positions=", sum(sample["is_uop"]))
    print("side_dim=", len(sample["side_feats"][0]),
          "global_tokens=", sample["global_tokens"],
          "attn_feature_positions=", sum(sample["is_attn_feat"]))
    print("tmp=", path)

    try:
        import torch
        from model.llm_wrapper import AttentionFeatureEncoder, UopEncoder
        from train.dataset import WindowDataset, make_collate
        from train.loss import PMULoss
    except Exception as exc:
        print(f"SKIP torch smoke: {exc}")
        return

    tok = _FakeTokenizer()
    ds = WindowDataset(path, tok, max_len=512, use_cache=False)
    assert len(ds) == 1
    batch = make_collate(tok.pad_token_id)([ds[0]])
    enc = UopEncoder(d_model=32, field_dim=8)
    uop_emb = enc(batch["uop_fields"])
    assert uop_emb.shape == (1, len(sample["tokens"]), 32)
    fenc = AttentionFeatureEncoder(
        d_model=32, n_features=len(tk.ATTN_FEATURE_KEYS))
    feat_emb = fenc(batch["attn_feat_ids"], batch["attn_feat_values"])
    assert feat_emb.shape == (1, len(sample["tokens"]), 32)
    pred = torch.zeros((1, 2, len(PMU_KEYS)), dtype=torch.float32)
    loss, logs = PMULoss()(pred, batch["label"], batch["core_mask"],
                           uops=batch["uops"], denoms=batch["denoms"])
    assert torch.isfinite(loss)
    assert "L_phys" in logs
    print("OK torch smoke loss=", float(loss.detach()))


if __name__ == "__main__":
    main()
