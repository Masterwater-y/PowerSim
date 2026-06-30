"""§7 第 1 步 smoke：构造 mock 1-core windows.jsonl（含 uops_per_core 与 cpi_uop label），
过 dataset → collate → PMULoss(uops=...) → backward，确认 cpi_uop 重构链路打通。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch

from data.build_windows import encode_multicore_sample
from model import tokenizer as tk
from model.llm_wrapper import build_tokenizer
from model.regression_head import PMU_KEYS, K
from train.dataset import WindowDataset, make_collate
from train.loss import PMULoss


def make_mock_record(i: int) -> dict:
    return {
        "op_class": 1,  # IntAlu
        "n_src": 1, "n_dst": 1,
        "_rd_bucket": tk.RD_NONMEM,
        "_stride_bucket": tk.ST_NONMEM,
        "path_class": 0, "i_path_class": 0,
        "coh_oracle": 0, "d_mshr_depth": 0,
        "dtlb_hit": 1, "itlb_hit": 1, "mispredicted": 0,
        "_commit_tick": 1000 + 10 * i,
        "_mispredicted": 0,
        "is_int": 1, "is_microop": 0, "is_last_microop": 1,
        "is_branch": 0, "is_load": 0, "is_store": 0, "is_atomic": 0,
        "is_fp": 0, "is_simd": 0, "is_serialize": 0,
        "is_call": 0, "is_return": 0,
        "is_branch_cond": 0, "is_branch_indirect": 0,
        "producer_dists": [], "producer_classes": [],
        "thread_id": 0, "micro_seq": i,
        "macro_pc": 0x4000 + i, "micro_pc": 0x4000 + i,
        "vaddr": 0, "size": 0,
    }


def make_mock_sample(uops: int, cycles: int, instr_macro: int):
    cpi_uop = cycles / max(uops, 1)
    label_values = {
        "cpi_uop": cpi_uop,
        "branch_miss": 0.0,
        "l1d_ld_miss": 0.0,
        "l1d_st_miss": 0.0,
        "l2_ld_miss": 0.0,
        "l2_st_miss": 0.0,
        "l1i_miss": 0.0,
        "llc_miss": 0.0,
        "dtlb_miss": 0.0,
        "mshr_avg": 0.0,
    }
    label = [label_values[k] for k in PMU_KEYS]
    pmu = dict(label_values)
    pmu.update({
        "cycles": float(cycles),
        "instr_retired": float(instr_macro),
        "uops": float(uops),
        "t_start_tick": 1000.0,
        "cpi_macro": cycles / max(instr_macro, 1),
        "_denoms": {
            "branch_count": 0,
            "loads": 0,
            "stores": 0,
            "atomics": 0,
            "mem_ops": 0,
        },
    })
    win = [make_mock_record(i) for i in range(uops)]
    return encode_multicore_sample(
        tokens=[],
        labels=[label],
        per_core_windows={0: (win, pmu)},
        cores=[0],
        cfg={"cfg_tokens": {}, "cfg_hash": "A0"},
        sample_meta={
            "id": "mock,seg0",
            "workload": "mock",
            "cfg_hash": "A0",
            "n_core": 1,
            "t_start_rel": [0.0],
        },
        timing_by_core={0: {"t_start_rel": 0.0}},
    )


def main() -> None:
    tok = build_tokenizer()
    pad_id = tok.pad_token_id

    with tempfile.TemporaryDirectory() as td:
        jsonl_path = os.path.join(td, "windows.jsonl")
        with open(jsonl_path, "w") as f:
            for uops, cycles, macro in [
                (50, 80, 40),
                (60, 120, 45),
                (40, 50, 35),
                (55, 90, 42),
            ]:
                f.write(json.dumps(make_mock_sample(uops, cycles, macro)) + "\n")

        ds = WindowDataset(
            jsonl_path, tok, max_len=4096,
            max_cores=tk.MAX_CORES, require_cache=False,
        )
        print(f"[smoke] |ds| = {len(ds)}")
        assert len(ds) == 4

        collate = make_collate(pad_id)
        batch = collate([ds[i] for i in range(4)])
        print(f"[smoke] batch keys: {sorted(batch.keys())}")
        print(f"[smoke] uops: {batch['uops'].tolist()}")
        print(f"[smoke] instr_retired: {batch['instr_retired'].tolist()}")
        assert "uops" in batch and "instr_retired" in batch

        loss_fn = PMULoss()
        pred = torch.randn(4, 1, K, requires_grad=True)
        with torch.no_grad():
            # sigmoid rat01 维度
            for i, k in enumerate(PMU_KEYS):
                from model.regression_head import KEY_SPACE
                if KEY_SPACE.get(k) == "rat01":
                    pred[..., i] = torch.sigmoid(pred[..., i])

        loss, logs = loss_fn(pred, batch["label"], batch["core_mask"],
                             uops=batch["uops"])
        print(f"[smoke] loss = {float(loss):.4f}")
        for k, v in logs.items():
            print(f"  {k} = {float(v):.4f}")
        loss.backward()
        print("[smoke] backward OK, log_var.grad norm =",
              float(loss_fn.log_var.grad.norm()))

        assert "L_cpi_uop" in logs
        assert torch.isfinite(loss).item()
        print("[smoke] cpi_uop end-to-end OK")


if __name__ == "__main__":
    main()
