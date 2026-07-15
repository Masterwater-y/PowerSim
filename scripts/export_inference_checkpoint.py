#!/usr/bin/env python3
"""Strip optimizer/history tensors from a TCSim training checkpoint."""
from __future__ import annotations

import argparse
import os
import tempfile

import torch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        payload = torch.load(args.input, map_location="cpu", weights_only=True)
    except TypeError:
        payload = torch.load(args.input, map_location="cpu")
    if not isinstance(payload, dict) or "model" not in payload:
        raise SystemExit("input is not a training checkpoint with a model state")
    lean = {
        "model": payload["model"],
        "config": payload.get("config"),
        "step": int(payload.get("step", 0) or 0),
        "best_val": float(payload.get("best_val", float("nan"))),
        "source_checkpoint": os.path.abspath(args.input),
        "inference_only": True,
    }
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # PyTorch's zip writer rejects some dot-prefixed extensionless names.
    fd, temporary = tempfile.mkstemp(
        prefix="infer_ckpt_", suffix=".pt", dir=os.path.dirname(out)
    )
    os.close(fd)
    try:
        torch.save(lean, temporary)
        os.replace(temporary, out)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"[checkpoint] wrote inference-only checkpoint: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
