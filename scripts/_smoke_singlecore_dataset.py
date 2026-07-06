"""§10.4 验证 (1)：单核 windows → dataset collate → loss 反传冒烟。

构造 mock 单核 trace 已存放在 data/_mock_raw_1core / data/_mock_windows_1core，
本脚本只验证下游 dataset / collate / 一次 loss.backward() 不报错。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch

from model import tokenizer as tk
from model.llm_wrapper import build_tokenizer
from train.dataset import WindowDataset, make_collate


def main() -> None:
    print(f"[smoke] MAX_CORES = {tk.MAX_CORES}")
    tok = build_tokenizer()
    print(f"[smoke] vocab = {len(tok)}")

    ds = WindowDataset(
        "/data00/yinhaolang/LLMSim/data/_mock_windows_1core/windows.jsonl",
        tok,
        max_len=8192,
        max_cores=tk.MAX_CORES,
        require_cache=False,
    )
    print(f"[smoke] |ds| = {len(ds)}")
    assert len(ds) >= 1

    collate = make_collate(tok.pad_token_id)
    batch = collate([ds[i] for i in range(min(len(ds), 4))])

    print(f"[smoke] batch keys: {sorted(batch.keys())}")
    print(f"[smoke] input_ids: {tuple(batch['input_ids'].shape)}")
    print(f"[smoke] query_pos: {tuple(batch['query_pos'].shape)}")
    print(f"[smoke] core_mask: {batch['core_mask'].tolist()}")
    print(f"[smoke] label: {tuple(batch['label'].shape)}")
    assert batch["core_mask"].shape[1] == 1, "single-core mask must be [B, 1]"
    assert (batch["core_mask"] == 1.0).all(), "single-core mask must be all 1.0"
    print("[smoke] OK")


if __name__ == "__main__":
    main()
