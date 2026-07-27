"""Versioned macro-ID to frozen-semantic-row sidecars for TCSim v29."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Dict, Mapping

import numpy as np

from train.macro_v29_dataset import CachedSemanticSource, MacroContractError


SEMANTIC_ID_SIDECAR_SCHEMA = "tcsim-v29-semantic-id-sidecar-v1"
SEMANTIC_ROW_FILE = "semantic_row_by_macro_id.npy"


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_key(trace_id: str) -> str:
    return hashlib.sha256(str(trace_id).encode("utf-8")).hexdigest()[:24]


def sidecar_dir(root: str | Path, trace_id: str) -> Path:
    return Path(root).resolve() / _sidecar_key(trace_id)


def _mapping_for_store(
    store: Any,
    semantic_cache: CachedSemanticSource,
    parquet: str | Path,
) -> tuple[str, Mapping[str, Any], np.ndarray]:
    if not bool(getattr(store, "has_macro_ids", False)):
        raise MacroContractError(
            f"TCSim cache {store.trace_id} lacks the v29 shared macro-ID sidecar"
        )
    macro_pc_table = np.asarray(store.macro_pc_table, dtype=np.uint64)
    binary_hash, arrays = semantic_cache._load_binary(str(Path(parquet).resolve()))
    lookup = semantic_cache._pc_indices[binary_hash]
    mapping = np.empty(len(macro_pc_table), dtype=np.uint32)
    for macro_id, pc in enumerate(macro_pc_table):
        semantic_row = lookup.get(int(pc))
        if semantic_row is None:
            raise MacroContractError(
                f"semantic sidecar cache miss trace={store.trace_id} "
                f"binary={binary_hash} macro_id={macro_id} pc=0x{int(pc):x}"
            )
        if not 0 <= int(semantic_row) <= np.iinfo(np.uint32).max:
            raise MacroContractError(
                "semantic sidecar row cannot be represented exactly as uint32"
            )
        mapping[macro_id] = int(semantic_row)
    if len(mapping) and int(mapping.max()) >= len(arrays["semantic"]):
        raise MacroContractError("semantic sidecar row exceeds semantic table")
    return binary_hash, arrays, mapping


def build_semantic_id_sidecar(
    store: Any,
    semantic_cache: CachedSemanticSource,
    parquet: str | Path,
    root: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Build one atomic, fail-closed mapping sidecar for a v29 trace."""
    output = sidecar_dir(root, str(store.trace_id))
    if output.exists() and not force:
        load_semantic_id_sidecar(store, semantic_cache, parquet, root)
        return output
    binary_hash, arrays, mapping = _mapping_for_store(
        store, semantic_cache, parquet,
    )
    tmp = output.with_name(f"{output.name}.tmp-{os.getpid()}")
    backup = output.with_name(f"{output.name}.old-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    np.save(tmp / SEMANTIC_ROW_FILE, mapping)
    macro_table_path = Path(store.cache_dir) / "macro_pc_table.npy"
    metadata: Dict[str, Any] = {
        "schema": SEMANTIC_ID_SIDECAR_SCHEMA,
        "trace_id": str(store.trace_id),
        "tcsim_cache_dir": str(Path(store.cache_dir).resolve()),
        "macro_id_contract": str(
            store.performance_sidecar_contract["macro_id_contract"]
        ),
        "macro_pc_count": int(len(store.macro_pc_table)),
        "macro_pc_table_sha256": _sha256_file(macro_table_path),
        "semantic_binary_hash": str(binary_hash),
        "semantic_rows": int(len(arrays["semantic"])),
        "semantic_cache_contract": dict(semantic_cache.contract),
        "semantic_parquet": str(Path(parquet).resolve()),
        "mapping_file": SEMANTIC_ROW_FILE,
        "mapping_dtype": "uint32",
    }
    (tmp / "meta.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        os.replace(output, backup)
    try:
        os.replace(tmp, output)
    except Exception:
        if backup.exists() and not output.exists():
            os.replace(backup, output)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    load_semantic_id_sidecar(store, semantic_cache, parquet, root)
    return output


def load_semantic_id_sidecar(
    store: Any,
    semantic_cache: CachedSemanticSource,
    parquet: str | Path,
    root: str | Path,
) -> np.ndarray:
    """Load and validate one sidecar against both TCSim and semantic cache."""
    directory = sidecar_dir(root, str(store.trace_id))
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        raise MacroContractError(f"semantic ID sidecar missing: {meta_path}")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if metadata.get("schema") != SEMANTIC_ID_SIDECAR_SCHEMA:
        raise MacroContractError("semantic ID sidecar schema mismatch")
    if metadata.get("trace_id") != str(store.trace_id):
        raise MacroContractError("semantic ID sidecar trace mismatch")
    if not bool(getattr(store, "has_macro_ids", False)):
        raise MacroContractError("semantic ID sidecar requires TCSim macro IDs")
    expected_macro_contract = str(
        store.performance_sidecar_contract["macro_id_contract"]
    )
    if metadata.get("macro_id_contract") != expected_macro_contract:
        raise MacroContractError("semantic ID sidecar macro-ID contract mismatch")
    macro_table_path = Path(store.cache_dir) / "macro_pc_table.npy"
    if metadata.get("macro_pc_table_sha256") != _sha256_file(macro_table_path):
        raise MacroContractError("semantic ID sidecar macro table hash mismatch")
    if int(metadata.get("macro_pc_count", -1)) != len(store.macro_pc_table):
        raise MacroContractError("semantic ID sidecar macro count mismatch")
    if metadata.get("semantic_cache_contract") != dict(semantic_cache.contract):
        raise MacroContractError("semantic ID sidecar cache contract mismatch")
    binary_hash, arrays = semantic_cache._load_binary(
        str(Path(parquet).resolve()),
    )
    if metadata.get("semantic_binary_hash") != binary_hash:
        raise MacroContractError("semantic ID sidecar binary hash mismatch")
    if int(metadata.get("semantic_rows", -1)) != len(arrays["semantic"]):
        raise MacroContractError("semantic ID sidecar semantic-row count mismatch")
    if metadata.get("mapping_dtype") != "uint32":
        raise MacroContractError("semantic ID sidecar mapping contract mismatch")
    if metadata.get("semantic_parquet") != str(Path(parquet).resolve()):
        raise MacroContractError("semantic ID sidecar static dictionary mismatch")
    mapping_path = directory / str(metadata.get("mapping_file", ""))
    mapping = np.load(mapping_path, mmap_mode="r")
    if mapping.dtype != np.uint32 or mapping.shape != (len(store.macro_pc_table),):
        raise MacroContractError("semantic ID sidecar mapping dtype/shape mismatch")
    if len(mapping) and int(mapping.max()) >= len(arrays["semantic"]):
        raise MacroContractError("semantic ID sidecar mapping is out of range")
    # Full deterministic equality validation is cheap because this table is
    # indexed by unique static macro PCs, not millions of dynamic UOPs.
    _expected_hash, _expected_arrays, expected = _mapping_for_store(
        store, semantic_cache, parquet,
    )
    if not np.array_equal(mapping, expected):
        raise MacroContractError("semantic ID sidecar mapping validation failed")
    return mapping
