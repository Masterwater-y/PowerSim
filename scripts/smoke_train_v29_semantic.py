#!/usr/bin/env python3
"""One-step end-to-end smoke for the cached-macro training mainline.

The cache vectors are intentionally synthetic and the online backbone is tiny;
this verifies wiring/contracts only and cannot be used as semantic or accuracy
evidence.  Real-Qwen cache and GPU smoke are separate acceptance gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train.macro_v29_dataset import ParquetInstructionResolver
from train.train_macro_v29 import load_macro_sources


def build_synthetic_cache(
    cache_root: Path,
    static_dict: str,
    *,
    semantic_dim: int = 16,
    anchor_dim: int = 32,
) -> Dict[str, Any]:
    resolver = ParquetInstructionResolver(static_dict)
    pcs = np.asarray(sorted(
        pc for pc, row in resolver.rows.items() if bool(row["semantic_valid"])
    ), dtype=np.uint64)
    generator = np.random.default_rng(20260719)
    semantic = generator.standard_normal(
        (len(pcs), int(semantic_dim)), dtype=np.float32,
    ).astype(np.float16)
    anchor = generator.standard_normal(
        (len(pcs), int(anchor_dim)), dtype=np.float32,
    ).astype(np.float16)
    hashes = np.asarray([
        hashlib.sha256(f"synthetic:{int(pc)}".encode()).hexdigest().encode("ascii")
        for pc in pcs
    ], dtype="S64")
    shard_root = cache_root / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    shard_relative = f"shards/{resolver.binary_hash}.npz"
    np.savez(
        cache_root / shard_relative,
        pcs=pcs,
        semantic=semantic,
        anchor=anchor,
        semantic_key_hashes=hashes,
    )
    shard_path = cache_root / shard_relative
    parquet_path = Path(static_dict).resolve()
    pc_set_hash = hashlib.sha256(json.dumps(
        [int(value) for value in pcs],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    manifest = {
        "schema_version": "macro-v29-semantic-cache-1",
        "semantic_encoder_model": "SYNTHETIC-CONTRACT-SMOKE-NOT-QWEN",
        "semantic_encoder_revision": "synthetic-fixed-20260719",
        "semantic_encoder_artifact_fingerprint": "0" * 64,
        "semantic_encoder_config_fingerprint": "1" * 64,
        "tokenizer_fingerprint": "2" * 64,
        "semantic_prompt_schema_version": "synthetic-smoke-v1",
        "semantic_pooling_policy": "synthetic-smoke",
        "semantic_dim": int(semantic_dim),
        "anchor_dim": int(anchor_dim),
        "anchor_policy": "synthetic-smoke",
        "offline_encoder_frozen": True,
        "model_facing_identity_fields": [],
        "binaries": [{
            "binary_hash": resolver.binary_hash,
            "parquet": str(parquet_path),
            "cache_file": shard_relative,
            "n_pcs": len(pcs),
            "parquet_sha256": hashlib.sha256(
                parquet_path.read_bytes()
            ).hexdigest(),
            "shard_sha256": hashlib.sha256(shard_path.read_bytes()).hexdigest(),
            "pc_set_hash": pc_set_hash,
        }],
    }
    (cache_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return {"binary_hash": resolver.binary_hash, "n_pcs": len(pcs)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json",
    )
    parser.add_argument(
        "--static-manifest",
        default=str(REPO_ROOT / "data/v28_1/static_dict/manifest.jsonl"),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output)
    if (output / "run.json").exists():
        raise RuntimeError(f"smoke output already contains a run: {output}")
    output.mkdir(parents=True, exist_ok=True)
    sources = load_macro_sources(
        args.manifest,
        args.static_manifest,
        split="train",
        cores={8},
        workloads=set(),
        workload_roles=set(),
        max_sources=1,
        partition_override="train",
    )
    static_dict = str(sources[0]["static_dict"])
    cache_root = output / "synthetic_semantic_cache"
    cache_report = build_synthetic_cache(cache_root, static_dict)
    command = [
        sys.executable,
        str(REPO_ROOT / "train/train_macro_v29.py"),
        "--manifest", str(args.manifest),
        "--static-manifest", str(args.static_manifest),
        "--cores", "8",
        "--max-train-sources", "1",
        "--max-validation-sources", "1",
        "--semantic-input-mode", "cached_macro_soft_token",
        "--semantic-cache-root", str(cache_root),
        "--semantic-variant", "real",
        "--tiny-backbone",
        "--tiny-width", "32",
        "--sequence-length", "1",
        "--sequence-stride", "1",
        "--batch-size", "1",
        "--num-workers", "0",
        "--max-steps", "1",
        "--eval-every", "1",
        "--eval-batches", "1",
        "--save-every", "0",
        "--dry-run",
        "--dtype", "fp32",
        "--output", str(output),
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        return int(completed.returncode)
    final = json.loads((output / "final_report.json").read_text())
    contract = final["checkpoint_contract"]
    failures = []
    expected = {
        "semantic_input_mode": "cached_macro_soft_token",
        "task_init_source": "fresh",
        "init_timing_checkpoint": None,
        "supervision_mode": "real_labels_only",
        "distillation_enabled": False,
        "online_backbone_input_unit": "macro",
    }
    for key, value in expected.items():
        if contract.get(key) != value:
            failures.append(f"{key}={contract.get(key)!r} != {value!r}")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "kind": "synthetic-cache-tiny-backbone-contract-smoke",
        "cache": cache_report,
        "train_final": final,
        "failures": failures,
        "semantic_or_accuracy_evidence": False,
    }
    (output / "semantic_smoke_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
