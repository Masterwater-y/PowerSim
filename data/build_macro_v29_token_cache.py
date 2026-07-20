#!/usr/bin/env python3
"""Build the macro-v29 native-token cache.

Design:

* The cache is keyed by (tokenizer_fingerprint, semantic_variant, binary_hash).
* Each PC in the static dictionary is tokenized once, using a context-free
  branch rendering (``.L_external`` label for architectural branches, whether
  or not the target falls inside a particular window).
* Branch metadata is stored so that at query time only the small subset of
  in-window branches must be retokenized with the correct
  ``.L_fwd_N`` / ``.L_back_N`` label.  All non-branch macros come straight from
  the cache.
* Semantic variants ``real``, ``pseudo``, ``register_rename`` are context-free
  and therefore cacheable.  ``mnemonic_shuffle`` depends on the whole window;
  it is intentionally NOT cached and must stay on the online path.

Layout::

    <cache-root>/manifest.json
    <cache-root>/<variant>/<binary_hash>.npz

Each npz contains::

    pcs             uint64[n_pcs]         sorted, unique PCs
    token_offsets   int64[n_pcs + 1]      CSR-style flat offsets
    token_ids       int32[total_tokens]   external-rendered token ids
    is_branch       bool[n_pcs]           architectural branch flag
    branch_target   int64[n_pcs]          absolute target PC or -1

Loading is O(1) per PC via a small dict built once per workload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train.macro_v29_dataset import (  # noqa: E402
    MacroContractError,
    ParquetInstructionResolver,
    SEMANTIC_TEXT_VARIANTS,
    SemanticVariantInstructionResolver,
    STATIC_DICT_SCHEMA_VERSION,
)

CACHE_SCHEMA_VERSION = "macro-v29-token-cache-1"
# The label used when the branch target is outside the current 256-macro
# window.  It must match ParquetInstructionResolver.render_window exactly.
EXTERNAL_LABEL = ".L_external"


def json_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tokenizer_fingerprint(tokenizer: Any) -> str:
    vocab = tokenizer.get_vocab()
    return json_fingerprint({
        "class": type(tokenizer).__name__,
        "vocab": sorted((str(token), int(index)) for token, index in vocab.items()),
        "special_tokens_map": getattr(tokenizer, "special_tokens_map", {}),
    })


def cache_variant_dir(cache_root: Path, variant: str) -> Path:
    return cache_root / variant


def cache_workload_path(cache_root: Path, variant: str, binary_hash: str) -> Path:
    return cache_variant_dir(cache_root, variant) / f"{binary_hash}.npz"


def load_static_manifest(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != STATIC_DICT_SCHEMA_VERSION:
            raise MacroContractError(
                f"static dictionary is not {STATIC_DICT_SCHEMA_VERSION}: "
                f"{row.get('binary_name')}"
            )
        if row.get("scan_scope") != "all_cores":
            raise MacroContractError(
                f"static dictionary lacks all-core coverage: {row.get('binary_name')}"
            )
        rows.append(row)
    return rows


def _tokenize_workload_worker(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Worker: render + tokenize one workload's unique PCs."""
    from transformers import AutoTokenizer  # local import for worker isolation

    variant = str(spec["variant"])
    parquet_path = str(spec["parquet_path"])
    binary_hash = str(spec["binary_hash"])
    out_path = Path(spec["out_path"])
    tokenizer_name = str(spec["tokenizer_name"])
    allow_download = bool(spec["allow_download"])
    expected_tokenizer_fingerprint = str(spec["tokenizer_fingerprint"])

    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_name, local_files_only=not allow_download,
    )
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer_fingerprint(tokenizer) != expected_tokenizer_fingerprint:
        raise MacroContractError(
            f"tokenizer fingerprint drifted in worker for {binary_hash}"
        )

    base = ParquetInstructionResolver(parquet_path)
    if variant == "real":
        resolver: Any = base
    elif variant in SEMANTIC_TEXT_VARIANTS:
        resolver = SemanticVariantInstructionResolver(base, variant)
    else:
        raise MacroContractError(f"unsupported semantic variant {variant!r}")

    pcs_sorted = np.asarray(
        sorted(pc for pc, row in base.rows.items() if bool(row["semantic_valid"])),
        dtype=np.uint64,
    )
    n_pcs = int(pcs_sorted.shape[0])
    skipped_pcs = sum(
        1 for row in base.rows.values() if not bool(row["semantic_valid"])
    )
    token_lists: List[List[int]] = []
    is_branch = np.zeros(n_pcs, dtype=np.bool_)
    branch_target = np.full(n_pcs, -1, dtype=np.int64)

    started = time.perf_counter()
    total_tokens = 0
    for index in range(n_pcs):
        pc = int(pcs_sorted[index])
        # Context-free rendering: the single-instruction call forces every
        # architectural branch to resolve as ``.L_external`` because no target
        # position exists in the local list.
        texts = resolver.render_window([pc])
        if len(texts) != 1:
            raise MacroContractError(
                f"resolver produced {len(texts)} texts for pc 0x{pc:x}"
            )
        text = str(texts[0]).strip()
        if not text or "\n" in text or "\r" in text:
            raise MacroContractError(
                f"invalid rendered instruction for pc 0x{pc:x}: {text!r}"
            )
        encoded = tokenizer(
            text + "\n", add_special_tokens=False, truncation=False,
        )
        ids = [int(value) for value in encoded["input_ids"]]
        if not ids:
            raise MacroContractError(f"tokenizer returned no ids for pc 0x{pc:x}")
        token_lists.append(ids)
        total_tokens += len(ids)
        row = base.rows[pc]
        if bool(row["is_branch"]):
            is_branch[index] = True
            branch_target[index] = int(row["target"])

    token_offsets = np.zeros(n_pcs + 1, dtype=np.int64)
    for index in range(n_pcs):
        token_offsets[index + 1] = token_offsets[index] + len(token_lists[index])
    token_ids = np.empty(int(token_offsets[-1]), dtype=np.int32)
    for index in range(n_pcs):
        token_ids[
            int(token_offsets[index]):int(token_offsets[index + 1])
        ] = np.asarray(token_lists[index], dtype=np.int32)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.npz")
    np.savez(
        str(tmp).removesuffix(".npz"),
        pcs=pcs_sorted,
        token_offsets=token_offsets,
        token_ids=token_ids,
        is_branch=is_branch,
        branch_target=branch_target,
    )
    os.replace(tmp, out_path)

    elapsed = time.perf_counter() - started
    return {
        "binary_hash": binary_hash,
        "n_pcs": n_pcs,
        "n_skipped_non_semantic_pcs": int(skipped_pcs),
        "n_tokens": int(token_offsets[-1]),
        "mean_tokens_per_pc": float(token_offsets[-1] / max(1, n_pcs)),
        "n_branches": int(is_branch.sum()),
        "elapsed_s": float(elapsed),
        "out_path": str(out_path),
    }


def _fingerprint_only_tokenizer(
    tokenizer_name: str, *, allow_download: bool,
) -> tuple[str, int]:
    """Load the tokenizer just once in the driver to record its fingerprint."""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_name, local_files_only=not allow_download,
    )
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer_fingerprint(tokenizer), int(len(tokenizer))


def collect_workload_binary_hashes(
    manifest_path: Path,
    static_rows: Sequence[Mapping[str, Any]],
    *,
    splits: Sequence[str],
    workloads: set[str],
) -> List[Dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text())
    static_by_name: Dict[str, Mapping[str, Any]] = {
        str(row["binary_name"]): row for row in static_rows
    }
    seen: Dict[str, Dict[str, Any]] = {}
    for split in splits:
        rows = manifest.get("splits", {}).get(split)
        if not isinstance(rows, list):
            continue
        for entry in rows:
            workload = str(entry["workload"])
            if workloads and workload not in workloads:
                continue
            binary_name = workload.removeprefix("W_")
            static_row = static_by_name.get(binary_name)
            if static_row is None:
                raise MacroContractError(
                    f"no static dictionary row for workload {workload}"
                )
            binary_hash = str(static_row["binary_hash"])
            if binary_hash in seen:
                continue
            seen[binary_hash] = {
                "binary_name": binary_name,
                "binary_hash": binary_hash,
                "parquet": str(static_row["parquet"]),
            }
    return list(seen.values())


def build_variant(
    *,
    cache_root: Path,
    variant: str,
    workloads_meta: Sequence[Mapping[str, Any]],
    tokenizer_name: str,
    tokenizer_fp: str,
    allow_download: bool,
    num_workers: int,
    force: bool,
) -> Dict[str, Any]:
    variant_dir = cache_variant_dir(cache_root, variant)
    variant_dir.mkdir(parents=True, exist_ok=True)
    specs: List[Dict[str, Any]] = []
    for meta in workloads_meta:
        out_path = cache_workload_path(cache_root, variant, meta["binary_hash"])
        if out_path.is_file() and not force:
            continue
        specs.append({
            "variant": variant,
            "binary_hash": meta["binary_hash"],
            "parquet_path": meta["parquet"],
            "out_path": str(out_path),
            "tokenizer_name": tokenizer_name,
            "tokenizer_fingerprint": tokenizer_fp,
            "allow_download": allow_download,
        })
    started = time.perf_counter()
    reports: List[Dict[str, Any]] = []
    if specs:
        if num_workers <= 1:
            for spec in specs:
                reports.append(_tokenize_workload_worker(spec))
                print(f"[cache {variant}] done {reports[-1]}", flush=True)
        else:
            ctx = mp.get_context("spawn")
            with ctx.Pool(processes=int(num_workers)) as pool:
                for report in pool.imap_unordered(_tokenize_workload_worker, specs):
                    reports.append(report)
                    print(f"[cache {variant}] done {report}", flush=True)
    return {
        "variant": variant,
        "workloads_total": len(workloads_meta),
        "workloads_built": len(reports),
        "workloads_skipped": len(workloads_meta) - len(specs),
        "elapsed_s": float(time.perf_counter() - started),
        "reports": reports,
    }


def parse_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build macro-v29 native-token cache for LLMSim training",
    )
    parser.add_argument(
        "--manifest",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json",
    )
    parser.add_argument(
        "--static-manifest",
        default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict/manifest.jsonl",
    )
    parser.add_argument(
        "--cache-root",
        default="/data00/yinhaolang/LLMSim/data/v29_macro_token_cache",
    )
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--variants", default="real")
    parser.add_argument(
        "--splits",
        default="train,validation,development_heldout,seed0_inference,"
                "deployment_inference,final_untouched",
    )
    parser.add_argument("--workloads", default="")
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    static_manifest_path = Path(args.static_manifest)
    cache_root = Path(args.cache_root)
    static_rows = load_static_manifest(static_manifest_path)
    variants = [item for item in args.variants.split(",") if item.strip()]
    for variant in variants:
        if variant not in SEMANTIC_TEXT_VARIANTS or variant == "mnemonic_shuffle":
            raise MacroContractError(
                f"variant {variant!r} is not cacheable; supported: "
                "real, pseudo, register_rename"
            )
    splits = [item for item in args.splits.split(",") if item.strip()]
    workloads = parse_csv(args.workloads)
    workloads_meta = collect_workload_binary_hashes(
        manifest_path, static_rows, splits=splits, workloads=workloads,
    )
    if not workloads_meta:
        raise MacroContractError("no matching workloads to cache")

    tokenizer_fp, tokenizer_size = _fingerprint_only_tokenizer(
        args.base_model, allow_download=bool(args.allow_download),
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest_out = cache_root / "manifest.json"
    manifest_payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "static_manifest": str(static_manifest_path),
        "dataset_manifest": str(manifest_path),
        "tokenizer_name": str(args.base_model),
        "tokenizer_fingerprint": tokenizer_fp,
        "tokenizer_size": tokenizer_size,
        "workloads": workloads_meta,
        "external_label": EXTERNAL_LABEL,
        "variants_requested": variants,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    started = time.perf_counter()
    all_reports: List[Dict[str, Any]] = []
    for variant in variants:
        report = build_variant(
            cache_root=cache_root,
            variant=variant,
            workloads_meta=workloads_meta,
            tokenizer_name=str(args.base_model),
            tokenizer_fp=tokenizer_fp,
            allow_download=bool(args.allow_download),
            num_workers=int(args.num_workers),
            force=bool(args.force),
        )
        all_reports.append(report)
        print(
            f"[cache summary] variant={variant} "
            f"built={report['workloads_built']} "
            f"skipped={report['workloads_skipped']} "
            f"elapsed={report['elapsed_s']:.1f}s",
            flush=True,
        )
    manifest_payload["build_reports"] = all_reports
    manifest_payload["total_elapsed_s"] = float(time.perf_counter() - started)
    tmp = manifest_out.with_suffix(manifest_out.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True))
    os.replace(tmp, manifest_out)
    print(
        f"[cache done] variants={variants} workloads={len(workloads_meta)} "
        f"root={cache_root}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
