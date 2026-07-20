#!/usr/bin/env python3
"""Build frozen-Qwen static macro semantics for the macro-v29 mainline.

Only validated static information is encoded.  Dynamic timing labels, trace
state, workload/split names, addresses, and microarchitectural oracles never
enter the prompt or the model-facing cache values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train.macro_v29_dataset import (  # noqa: E402
    CachedSemanticSource,
    MacroContractError,
    ParquetInstructionResolver,
    STATIC_DICT_SCHEMA_VERSION,
)


CACHE_SCHEMA_VERSION = CachedSemanticSource.CACHE_SCHEMA_VERSION
PROMPT_SCHEMA_VERSION = "macro-static-bb-prefix-v1"
POOLING_POLICY = "causal-final-summary-position-v1"
ANCHOR_POLICY = "mean_normalized_target_input_embedding-v1"
CONTEXT_POLICY = "same-bb-previous-4-plus-target-v1"
ATTENTION_IMPLEMENTATION = "sdpa"
RECOMPUTE_ABS_TOLERANCE = 5.0e-3
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])([+-]?(?:0x[0-9a-fA-F]+|[0-9]{5,}))"
    r"(?![A-Za-z0-9_])"
)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def json_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tokenizer_fingerprint(tokenizer: Any) -> str:
    return json_fingerprint({
        "class": type(tokenizer).__name__,
        "padding_side": str(tokenizer.padding_side),
        "vocab": sorted(
            (str(token), int(index))
            for token, index in tokenizer.get_vocab().items()
        ),
        "special_tokens_map": getattr(tokenizer, "special_tokens_map", {}),
    })


def normalized_assembly(text: str) -> str:
    """Remove likely binary identity while retaining small semantic immediates."""

    value = " ".join(str(text).strip().split())

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        try:
            number = int(token, 0)
        except ValueError:
            return "<imm_large>"
        return token.lower() if abs(number) <= 4096 else "<imm_large>"

    return _NUMBER_RE.sub(replace, value)


def semantic_key_fingerprint(parts: Mapping[str, Any]) -> str:
    """Stable key helper kept public for provenance-sensitivity tests."""

    return json_fingerprint(dict(parts))


def load_static_manifest(path: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != STATIC_DICT_SCHEMA_VERSION:
            raise MacroContractError("static dictionary schema mismatch")
        if row.get("scan_scope") != "all_cores":
            raise MacroContractError("static dictionary lacks all-core coverage")
        name = str(row["binary_name"])
        if name in rows:
            raise MacroContractError(f"duplicate static binary {name}")
        rows[name] = row
    if not rows:
        raise MacroContractError("empty static manifest")
    return rows


def _chunked_macro_pcs(core_dir: Path) -> set[int]:
    macro_pc = np.load(core_dir / "macro_pc.npy", mmap_mode="r")
    macro_end = np.load(core_dir / "macro_end.npy", mmap_mode="r")
    if macro_pc.shape != macro_end.shape or macro_pc.ndim != 1:
        raise MacroContractError(f"invalid macro arrays in {core_dir}")
    values: set[int] = set()
    chunk = 4_000_000
    for begin in range(0, len(macro_pc), chunk):
        end = min(begin + chunk, len(macro_pc))
        ending = np.asarray(macro_end[begin:end], dtype=np.uint8) != 0
        values.update(
            int(value)
            for value in np.unique(np.asarray(macro_pc[begin:end])[ending])
        )
    return values


def collect_required_binaries(
    manifest_path: Path,
    static_rows: Mapping[str, Mapping[str, Any]],
    *,
    splits: Sequence[str],
    cores: set[int],
    workload_filter: set[str],
    max_binaries: int,
) -> tuple[List[Dict[str, Any]], int]:
    """Join selected traces to binary hashes and unique dynamic macro PCs."""

    manifest = json.loads(manifest_path.read_text())
    grouped: Dict[str, Dict[str, Any]] = {}
    selected_traces = 0
    for split in splits:
        rows = manifest.get("splits", {}).get(split)
        if not isinstance(rows, list):
            continue
        for trace in rows:
            n_cores = int(trace["n_cores"])
            workload = str(trace["workload"])
            if cores and n_cores not in cores:
                continue
            if workload_filter and workload not in workload_filter:
                continue
            binary_name = workload.removeprefix("W_")
            static = static_rows.get(binary_name)
            if static is None:
                raise MacroContractError(
                    f"no static dictionary for selected binary {binary_name}"
                )
            binary_hash = str(static["binary_hash"])
            if (
                max_binaries > 0
                and binary_hash not in grouped
                and len(grouped) >= max_binaries
            ):
                continue
            entry = grouped.setdefault(binary_hash, {
                "binary_hash": binary_hash,
                "parquet": str(Path(str(static["parquet"])).resolve()),
                "pcs": set(),
            })
            trace_root = Path(str(trace["cache_dir"]))
            meta = json.loads((trace_root / "meta.json").read_text())
            if int(len(meta["core_ids"])) != n_cores:
                raise MacroContractError(f"trace core-count mismatch: {trace_root}")
            for core_id in meta["core_ids"]:
                entry["pcs"].update(
                    _chunked_macro_pcs(trace_root / "cores" / str(int(core_id)))
                )
            selected_traces += 1
    if not grouped:
        raise MacroContractError("no selected trace binaries for semantic cache")
    return [grouped[key] for key in sorted(grouped)], selected_traces


def _branch_class(pc: int, row: Mapping[str, Any]) -> str:
    if not bool(row["is_branch"]):
        return "non_branch"
    target = int(row["target"])
    if target < 0:
        return "indirect_or_return"
    if target < int(pc):
        return "direct_loop_back"
    if target > int(pc):
        return "direct_forward"
    return "direct_self_loop"


def build_static_records(
    resolver: ParquetInstructionResolver,
    required_pcs: Iterable[int],
    *,
    encoder_provenance: Mapping[str, Any],
    previous_instructions: int = 4,
) -> List[Dict[str, Any]]:
    pcs = sorted({int(value) for value in required_pcs})
    coverage = resolver.coverage(pcs)
    if coverage["n_missing"] or coverage["n_invalid"]:
        raise MacroContractError(
            "semantic static join is incomplete: "
            f"missing={coverage['n_missing']} invalid={coverage['n_invalid']}"
        )
    blocks: Dict[tuple[str, int], List[int]] = {}
    for pc, row in resolver.rows.items():
        if not bool(row["semantic_valid"]):
            continue
        blocks.setdefault(
            (str(row["section_name"]), int(row["bb_id"])), []
        ).append(int(pc))
    for block in blocks.values():
        block.sort()
    records: List[Dict[str, Any]] = []
    for pc in pcs:
        row = resolver.rows[pc]
        block = blocks[(str(row["section_name"]), int(row["bb_id"]))]
        position = block.index(pc)
        context_pcs = block[max(0, position - int(previous_instructions)):position + 1]
        context = [
            normalized_assembly(text)
            for text in resolver.render_window(context_pcs)
        ]
        target_text = context[-1]
        branch_class = _branch_class(pc, row)
        prompt = (
            "Architecture: x86-64\n"
            "Basic-block context (identity-normalized):\n  "
            + "\n  ".join(context)
            + "\nTarget instruction:\n  "
            + target_text
            + "\nStatic control-flow class: "
            + branch_class
            + "\nSemantic representation:"
        )
        context_hash = json_fingerprint({
            "context": context,
            "branch_class": branch_class,
        })
        key_parts = {
            "binary_build_id": resolver.binary_hash,
            "module_relative_instruction_offset": int(pc),
            "instruction_size": int(row["size_bytes"]),
            "instruction_bytes": str(row["bytes_hex"]),
            "normalized_assembly": target_text,
            "normalized_local_context_hash": context_hash,
            "decoder": (
                f"{STATIC_DICT_SCHEMA_VERSION}:{row['decode_source']}"
            ),
            **dict(encoder_provenance),
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "pooling_policy": POOLING_POLICY,
            "anchor_policy": ANCHOR_POLICY,
        }
        records.append({
            "pc": int(pc),
            "prompt": prompt,
            "anchor_text": target_text + "\n",
            "semantic_key_hash": semantic_key_fingerprint(key_parts),
        })
    return records


def _cached_artifact_path(model_name: str, filename: str, allow_download: bool):
    from transformers.utils.hub import cached_file

    try:
        return cached_file(
            model_name,
            filename,
            local_files_only=not allow_download,
            _raise_exceptions_for_gated_repo=False,
            _raise_exceptions_for_missing_entries=False,
            _raise_exceptions_for_connection_errors=False,
        )
    except (OSError, ValueError):
        return None


def model_artifact_fingerprint(model_name: str, *, allow_download: bool) -> str:
    """Hash the exact local config/tokenizer/index/weight artifacts."""

    names = [
        "config.json", "tokenizer_config.json", "tokenizer.json",
        "model.safetensors.index.json", "model.safetensors",
        "pytorch_model.bin.index.json", "pytorch_model.bin",
    ]
    resolved: Dict[str, Path] = {}
    for name in names:
        path = _cached_artifact_path(model_name, name, allow_download)
        if path:
            resolved[name] = Path(path)
    for index_name in (
        "model.safetensors.index.json", "pytorch_model.bin.index.json",
    ):
        path = resolved.get(index_name)
        if path is None:
            continue
        index = json.loads(path.read_text())
        for shard in sorted(set(index.get("weight_map", {}).values())):
            shard_path = _cached_artifact_path(model_name, shard, allow_download)
            if shard_path is None:
                raise MacroContractError(f"missing local model shard {shard}")
            resolved[str(shard)] = Path(shard_path)
    weight_names = {
        name for name in resolved
        if name.endswith((".safetensors", ".bin"))
        and not name.endswith("index.json")
    }
    if not weight_names:
        raise MacroContractError(
            f"could not resolve local model weights for {model_name}"
        )
    return json_fingerprint([
        {"name": name, "sha256": file_sha256(resolved[name])}
        for name in sorted(resolved)
    ])


def encode_records(
    records: Sequence[Mapping[str, Any]],
    tokenizer: Any,
    model: Any,
    *,
    device: Any,
    batch_size: int,
    max_prompt_tokens: int,
) -> tuple[np.ndarray, np.ndarray]:
    import torch

    if not records:
        raise MacroContractError("cannot encode an empty semantic record list")
    semantic_parts: List[np.ndarray] = []
    anchor_parts: List[np.ndarray] = []
    embedding = model.get_input_embeddings()
    model.eval()
    with torch.inference_mode():
        for begin in range(0, len(records), int(batch_size)):
            rows = records[begin:begin + int(batch_size)]
            prompts = [str(row["prompt"]) for row in rows]
            encoded = tokenizer(
                prompts,
                padding=True,
                add_special_tokens=False,
                truncation=False,
                return_tensors="pt",
            )
            if int(encoded["attention_mask"].sum(dim=1).max()) > int(max_prompt_tokens):
                raise MacroContractError(
                    f"semantic prompt exceeds max_prompt_tokens={max_prompt_tokens}"
                )
            encoded = {key: value.to(device) for key, value in encoded.items()}
            position_ids = encoded["attention_mask"].long().cumsum(dim=-1) - 1
            position_ids.masked_fill_(encoded["attention_mask"] == 0, 0)
            output = model(
                **encoded,
                position_ids=position_ids,
                output_hidden_states=False,
                use_cache=False,
            )
            hidden = output.last_hidden_state
            positions = torch.arange(
                encoded["attention_mask"].shape[1], device=device,
            ).unsqueeze(0)
            last = (
                positions * encoded["attention_mask"].long()
            ).max(dim=1).values
            semantic = hidden[
                torch.arange(hidden.shape[0], device=hidden.device), last,
            ]

            anchor_encoded = tokenizer(
                [str(row["anchor_text"]) for row in rows],
                padding=True,
                add_special_tokens=False,
                truncation=False,
                return_tensors="pt",
            )
            anchor_ids = anchor_encoded["input_ids"].to(device)
            anchor_mask = anchor_encoded["attention_mask"].to(device)
            anchor_tokens = embedding(anchor_ids)
            anchor = (
                anchor_tokens * anchor_mask.unsqueeze(-1).to(anchor_tokens.dtype)
            ).sum(dim=1) / anchor_mask.sum(dim=1, keepdim=True).clamp_min(1).to(
                anchor_tokens.dtype
            )
            semantic_parts.append(
                semantic.float().cpu().numpy().astype(np.float16)
            )
            anchor_parts.append(anchor.float().cpu().numpy().astype(np.float16))
    semantic_array = np.concatenate(semantic_parts, axis=0)
    anchor_array = np.concatenate(anchor_parts, axis=0)
    if not np.isfinite(semantic_array).all() or not np.isfinite(anchor_array).all():
        raise MacroContractError("offline encoder produced non-finite semantics")
    return semantic_array, anchor_array


def verify_encoded_prefix(
    records: Sequence[Mapping[str, Any]],
    stored_semantic: np.ndarray,
    stored_anchor: np.ndarray,
    tokenizer: Any,
    model: Any,
    *,
    device: Any,
    batch_size: int,
    max_prompt_tokens: int,
    verify_samples: int,
) -> Dict[str, float | int]:
    """Recompute a prefix with the same first-batch execution shape.

    BF16 transformer kernels are not invariant to changing the batch matrix
    shape.  Recomputing only ``verify_samples`` records would therefore audit
    a different numerical program than the one that produced the cache.  The
    first build batch is replayed in full, while only the requested prefix is
    compared with the stored FP16 values.
    """

    verify_count = min(int(verify_samples), len(records))
    if verify_count <= 0:
        return {
            "verify_samples": 0,
            "replay_samples": 0,
            "semantic_max_abs_error": 0.0,
            "anchor_max_abs_error": 0.0,
            "max_recompute_abs_error": 0.0,
        }
    replay_count = min(len(records), max(int(batch_size), verify_count))
    recomputed, recomputed_anchor = encode_records(
        records[:replay_count],
        tokenizer,
        model,
        device=device,
        batch_size=int(batch_size),
        max_prompt_tokens=int(max_prompt_tokens),
    )
    semantic_error = float(np.max(np.abs(
        np.asarray(stored_semantic[:verify_count], dtype=np.float32)
        - recomputed[:verify_count].astype(np.float32)
    )))
    anchor_error = float(np.max(np.abs(
        np.asarray(stored_anchor[:verify_count], dtype=np.float32)
        - recomputed_anchor[:verify_count].astype(np.float32)
    )))
    max_error = max(semantic_error, anchor_error)
    if max_error > RECOMPUTE_ABS_TOLERANCE:
        raise MacroContractError(
            "semantic recompute error "
            f"semantic={semantic_error} anchor={anchor_error} "
            f"exceeds tolerance={RECOMPUTE_ABS_TOLERANCE}"
        )
    return {
        "verify_samples": verify_count,
        "replay_samples": replay_count,
        "semantic_max_abs_error": semantic_error,
        "anchor_max_abs_error": anchor_error,
        "max_recompute_abs_error": max_error,
    }


def parse_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_csv_ints(value: str) -> set[int]:
    return {int(item) for item in parse_csv(value)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build macro-v29 frozen-Qwen semantic cache",
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
        default="/data00/yinhaolang/LLMSim/data/v29_macro_semantic_cache",
    )
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument(
        "--splits",
        default="train,validation,development_heldout,seed0_inference,"
                "deployment_inference,final_untouched",
    )
    parser.add_argument("--cores", default="8")
    parser.add_argument("--workloads", default="")
    parser.add_argument("--max-binaries", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-prompt-tokens", type=int, default=256)
    parser.add_argument("--verify-samples", type=int, default=4)
    parser.add_argument("--device", default="")
    parser.add_argument(
        "--dtype", choices=("fp32", "bf16", "fp16"), default="bf16",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    import torch
    from transformers import AutoConfig, AutoModel, AutoTokenizer

    device = torch.device(
        args.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
    )
    if device.type == "cpu" and args.dtype != "fp32":
        args.dtype = "fp32"
    dtype = {
        "fp32": torch.float32,
        "bf16": torch.bfloat16,
        "fp16": torch.float16,
    }[args.dtype]
    manifest_path = Path(args.manifest)
    static_manifest_path = Path(args.static_manifest)
    static_rows = load_static_manifest(static_manifest_path)
    binaries, selected_traces = collect_required_binaries(
        manifest_path,
        static_rows,
        splits=parse_csv(args.splits),
        cores=parse_csv_ints(args.cores),
        workload_filter=set(parse_csv(args.workloads)),
        max_binaries=int(args.max_binaries),
    )
    print(
        f"[semantic cache] selected traces={selected_traces} "
        f"binaries={len(binaries)} device={device}",
        flush=True,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, local_files_only=not bool(args.allow_download),
    )
    tokenizer.padding_side = "right"
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise MacroContractError("semantic tokenizer has no pad/EOS token")
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer_fp = tokenizer_fingerprint(tokenizer)
    config = AutoConfig.from_pretrained(
        args.base_model, local_files_only=not bool(args.allow_download),
    )
    config.output_hidden_states = False
    config.use_cache = False
    config_fp = json_fingerprint(config.to_dict())
    artifact_fp = model_artifact_fingerprint(
        args.base_model, allow_download=bool(args.allow_download),
    )
    revision = str(getattr(config, "_commit_hash", None) or artifact_fp)
    encoder_provenance = {
        "semantic_encoder_model_revision": revision,
        "semantic_encoder_artifact_fingerprint": artifact_fp,
        "semantic_encoder_config_fingerprint": config_fp,
        "tokenizer_hash": tokenizer_fp,
        "semantic_encoder_compute_dtype": str(args.dtype),
        "semantic_encoder_attention_implementation": ATTENTION_IMPLEMENTATION,
        "semantic_encoder_batch_size": int(args.batch_size),
    }
    model = AutoModel.from_pretrained(
        args.base_model,
        config=config,
        torch_dtype=dtype,
        attn_implementation=ATTENTION_IMPLEMENTATION,
        local_files_only=not bool(args.allow_download),
    ).to(device).eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)

    cache_root = Path(args.cache_root)
    shard_root = cache_root / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    manifest_binaries: List[Dict[str, Any]] = []
    build_reports: List[Dict[str, Any]] = []
    semantic_dim = int(config.hidden_size)
    anchor_dim = int(model.get_input_embeddings().embedding_dim)
    manifest_out = cache_root / "manifest.json"
    existing_manifest: Dict[str, Any] | None = None
    existing_entries: Dict[str, Mapping[str, Any]] = {}
    if manifest_out.exists() and not bool(args.force):
        existing_manifest = json.loads(manifest_out.read_text())
        expected_provenance = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "static_dict_schema_version": STATIC_DICT_SCHEMA_VERSION,
            "static_manifest_sha256": file_sha256(static_manifest_path),
            "source_manifest_sha256": file_sha256(manifest_path),
            "semantic_encoder_model": str(args.base_model),
            "semantic_encoder_revision": revision,
            "semantic_encoder_artifact_fingerprint": artifact_fp,
            "semantic_encoder_config_fingerprint": config_fp,
            "semantic_encoder_compute_dtype": str(args.dtype),
            "semantic_encoder_attention_implementation": ATTENTION_IMPLEMENTATION,
            "semantic_encoder_batch_size": int(args.batch_size),
            "tokenizer_fingerprint": tokenizer_fp,
            "semantic_prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "semantic_pooling_policy": POOLING_POLICY,
            "context_policy": CONTEXT_POLICY,
            "anchor_policy": ANCHOR_POLICY,
            "semantic_dim": semantic_dim,
            "anchor_dim": anchor_dim,
        }
        for key, expected in expected_provenance.items():
            if existing_manifest.get(key) != expected:
                raise MacroContractError(
                    f"existing semantic cache provenance differs at {key}; "
                    "use --force and a fresh training run"
                )
        observed_selection = existing_manifest.get("selection_contract")
        expected_selection = {
            "cores": sorted(parse_csv_ints(args.cores)),
            "selected_trace_count": selected_traces,
            "selected_binary_count": len(binaries),
        }
        if observed_selection != expected_selection:
            raise MacroContractError(
                "existing semantic cache selection differs; use --force"
            )
        existing_entries = {
            str(entry["binary_hash"]): entry
            for entry in existing_manifest.get("binaries", [])
        }
        if set(existing_entries) != {
            str(binary["binary_hash"]) for binary in binaries
        }:
            raise MacroContractError(
                "existing semantic cache binary set differs; use --force"
            )
    started_all = time.perf_counter()
    for binary in binaries:
        binary_hash = str(binary["binary_hash"])
        parquet = str(binary["parquet"])
        pcs = sorted(int(value) for value in binary["pcs"])
        shard_relative = f"shards/{binary_hash}.npz"
        shard_path = cache_root / shard_relative
        started = time.perf_counter()
        reuse_shard = (
            shard_path.exists()
            and not bool(args.force)
            and existing_manifest is not None
        )
        if shard_path.exists() and not bool(args.force) and existing_manifest is None:
            print(
                f"[semantic cache] rebuild uncommitted shard {binary_hash}",
                flush=True,
            )
        if reuse_shard:
            entry = existing_entries.get(binary_hash)
            if entry is None:
                raise MacroContractError(
                    f"existing manifest lacks binary {binary_hash}"
                )
            if file_sha256(shard_path) != str(entry.get("shard_sha256", "")):
                raise MacroContractError(
                    f"existing semantic shard checksum mismatch: {binary_hash}"
                )
            existing = np.load(shard_path, allow_pickle=False)
            existing_pcs = np.asarray(existing["pcs"], dtype=np.uint64)
            if np.array_equal(existing_pcs, np.asarray(pcs, dtype=np.uint64)):
                if existing["semantic"].shape != (len(pcs), semantic_dim):
                    raise MacroContractError(
                        f"existing semantic width mismatch for {binary_hash}"
                    )
                if existing["anchor"].shape != (len(pcs), anchor_dim):
                    raise MacroContractError(
                        f"existing anchor width mismatch for {binary_hash}"
                    )
                print(f"[semantic cache] reuse {binary_hash} pcs={len(pcs)}", flush=True)
            else:
                raise MacroContractError(
                    f"existing shard PC set differs for {binary_hash}; use --force"
                )
        else:
            resolver = ParquetInstructionResolver(parquet)
            if resolver.binary_hash != binary_hash:
                raise MacroContractError(
                    f"parquet binary hash {resolver.binary_hash} != {binary_hash}"
                )
            records = build_static_records(
                resolver, pcs, encoder_provenance=encoder_provenance,
            )
            semantic, anchor = encode_records(
                records,
                tokenizer,
                model,
                device=device,
                batch_size=int(args.batch_size),
                max_prompt_tokens=int(args.max_prompt_tokens),
            )
            key_hashes = np.asarray(
                [str(row["semantic_key_hash"]).encode("ascii") for row in records],
                dtype="S64",
            )
            temporary = shard_path.with_suffix(".tmp.npz")
            np.savez(
                str(temporary).removesuffix(".npz"),
                pcs=np.asarray(pcs, dtype=np.uint64),
                semantic=semantic,
                anchor=anchor,
                semantic_key_hashes=key_hashes,
            )
            try:
                with np.load(temporary, allow_pickle=False) as stored:
                    verification = verify_encoded_prefix(
                        records,
                        stored["semantic"],
                        stored["anchor"],
                        tokenizer,
                        model,
                        device=device,
                        batch_size=int(args.batch_size),
                        max_prompt_tokens=int(args.max_prompt_tokens),
                        verify_samples=int(args.verify_samples),
                    )
                os.replace(temporary, shard_path)
            finally:
                temporary.unlink(missing_ok=True)
            build_reports.append({
                "binary_hash": binary_hash,
                "n_pcs": len(pcs),
                **verification,
                "elapsed_s": time.perf_counter() - started,
            })
            print(
                f"[semantic cache] built {binary_hash} pcs={len(pcs)} "
                f"seconds={time.perf_counter() - started:.1f}",
                flush=True,
            )
        manifest_binaries.append({
            "binary_hash": binary_hash,
            "parquet": parquet,
            "parquet_sha256": file_sha256(parquet),
            "cache_file": shard_relative,
            "n_pcs": len(pcs),
            "pc_set_hash": json_fingerprint(pcs),
            "shard_sha256": file_sha256(shard_path),
        })

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "static_dict_schema_version": STATIC_DICT_SCHEMA_VERSION,
        "static_manifest_sha256": file_sha256(static_manifest_path),
        "source_manifest_sha256": file_sha256(manifest_path),
        "selection_contract": {
            "cores": sorted(parse_csv_ints(args.cores)),
            "selected_trace_count": selected_traces,
            "selected_binary_count": len(binaries),
        },
        "semantic_encoder_model": str(args.base_model),
        "semantic_encoder_revision": revision,
        "semantic_encoder_artifact_fingerprint": artifact_fp,
        "semantic_encoder_config_fingerprint": config_fp,
        "semantic_encoder_compute_dtype": str(args.dtype),
        "semantic_encoder_attention_implementation": ATTENTION_IMPLEMENTATION,
        "semantic_encoder_batch_size": int(args.batch_size),
        "tokenizer_fingerprint": tokenizer_fp,
        "tokenizer_size": len(tokenizer),
        "semantic_prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "semantic_pooling_policy": POOLING_POLICY,
        "context_policy": CONTEXT_POLICY,
        "anchor_policy": ANCHOR_POLICY,
        "semantic_dim": semantic_dim,
        "anchor_dim": anchor_dim,
        "storage_dtype": "float16",
        "offline_encoder_frozen": True,
        "model_facing_identity_fields": [],
        "binaries": manifest_binaries,
        "build_reports": build_reports,
        "total_elapsed_s": time.perf_counter() - started_all,
    }
    temporary_manifest = manifest_out.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary_manifest, manifest_out)
    loaded = CachedSemanticSource(cache_root)
    if loaded.semantic_dim != semantic_dim or loaded.anchor_dim != anchor_dim:
        raise MacroContractError("written semantic manifest failed reload validation")
    print(
        f"[semantic cache done] root={cache_root} "
        f"manifest_sha256={loaded.manifest_hash}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
