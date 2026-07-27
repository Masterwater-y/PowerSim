"""Macro-native view over a TCSim v29 packed trace.

The old Phase-1 dataset predicts one scalar CPI.  This module instead converts
packed per-UOP arrays into windows of K_macro architectural instructions while
strictly separating model inputs, labels and control-only keys.

Structured UOP records are retained as a numeric side channel.  They are never
rendered as language tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sys
from typing import Any, Dict, Iterable, List, Mapping, Protocol, Sequence

import numpy as np


DATASET_SCHEMA_VERSION = "global-time-v29-macro-native-2"
MODEL_INPUT_CONTRACT = "functional-only-v29-macro-ragged-prefix"
SEMANTIC_MODEL_INPUT_CONTRACT = "functional-only-v29-cached-macro-soft-token-1"
NULL_MODEL_INPUT_CONTRACT = "functional-only-v29-learned-null-macro-token-1"
STATIC_DICT_SCHEMA_VERSION = "real-x86-objdump-wide-v2"
DEFAULT_K_MACRO = 256
DEFAULT_HORIZONS = (16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0)
SEMANTIC_TEXT_VARIANTS = (
    "real", "pseudo", "mnemonic_shuffle", "register_rename",
)

# Copied from TCSim tcsim/v29/contracts.py, packed-3.
V29_FIELD_SIZES = (
    90, 64, 5, 17, 9, 10, 5, 12, 10, 10, 14, 18,
    32, 3, 34, 257, 257, 9, 10, 9, 18,
    3, 10, 10, 10, 10,
)
V29_FIELD_PAD_IDS = np.asarray(V29_FIELD_SIZES, dtype=np.uint16)
COMMON_MODEL_INPUT_KEYS = frozenset({
    "uop_fields", "uop_valid_mask", "uop_to_macro",
    "uop_access", "uop_semantic_flags",
    "uop_count", "valid_macro_mask",
    "dynamic_uop_fields", "chunk_summary", "relation_features",
    "state_features", "uarch_features",
})
NATIVE_MODEL_INPUT_KEYS = frozenset({
    "input_ids", "attention_mask", "token_to_macro",
    "macro_token_start", "macro_token_end",
})
SEMANTIC_MODEL_INPUT_KEYS = frozenset({"static_semantic", "static_anchor"})
NULL_MODEL_INPUT_KEYS = frozenset({"null_semantic_marker"})
MODEL_INPUT_ALLOWLIST = frozenset(
    COMMON_MODEL_INPUT_KEYS | NATIVE_MODEL_INPUT_KEYS
    | SEMANTIC_MODEL_INPUT_KEYS | NULL_MODEL_INPUT_KEYS
)
_BRANCH_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:0x)?([0-9a-fA-F]{5,})(?![A-Za-z0-9_])"
)
_LOCAL_BRANCH_LABEL_RE = re.compile(r"\.L_(?:fwd|back)_\d+|\.L_external")
_REGISTER_RENAME = {
    "rax": "rbx", "rbx": "rax", "rcx": "rdx", "rdx": "rcx",
    "rsi": "rdi", "rdi": "rsi", "r8": "r9", "r9": "r8",
    "r10": "r11", "r11": "r10", "r12": "r13", "r13": "r12",
    "r14": "r15", "r15": "r14",
    "eax": "ebx", "ebx": "eax", "ecx": "edx", "edx": "ecx",
    "esi": "edi", "edi": "esi", "ax": "bx", "bx": "ax",
    "cx": "dx", "dx": "cx", "si": "di", "di": "si",
    "al": "bl", "bl": "al", "ah": "bh", "bh": "ah",
    "cl": "dl", "dl": "cl", "ch": "dh", "dh": "ch",
    "sil": "dil", "dil": "sil",
}
for _left, _right in ((8, 9), (10, 11), (12, 13), (14, 15)):
    for _suffix in ("d", "w", "b"):
        _REGISTER_RENAME[f"r{_left}{_suffix}"] = f"r{_right}{_suffix}"
        _REGISTER_RENAME[f"r{_right}{_suffix}"] = f"r{_left}{_suffix}"
_REGISTER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(
        re.escape(name)
        for name in sorted(_REGISTER_RENAME, key=len, reverse=True)
    ) + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FILE_SHA256_CACHE: Dict[tuple[str, int, int, int], str] = {}


def _file_sha256(path: str | Path) -> str:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    cache_key = (
        str(resolved), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns),
    )
    cached = _FILE_SHA256_CACHE.get(cache_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    value = digest.hexdigest()
    _FILE_SHA256_CACHE[cache_key] = value
    return value


def _json_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


class MacroContractError(RuntimeError):
    """The packed trace cannot satisfy the macro-native contract."""


class MacroTokenOverflow(MacroContractError):
    """A complete macro window does not fit the configured token budget."""


class InstructionResolver(Protocol):
    def render_window(self, macro_pcs: Sequence[int]) -> List[str]:
        """Return one newline-free assembly string for every input PC."""

    def is_architectural_branch(self, macro_pc: int) -> bool:
        """Return the static x86 branch class, excluding microcode control."""


@dataclass
class NativeTokenSequence:
    input_ids: np.ndarray
    attention_mask: np.ndarray
    token_to_macro: np.ndarray
    macro_token_start: np.ndarray
    macro_token_end: np.ndarray
    texts: List[str]


@dataclass
class MacroWindow:
    """Strict separation prevents labels/control keys leaking into the model."""

    model_inputs: Dict[str, np.ndarray]
    labels: Dict[str, np.ndarray]
    control: Dict[str, Any]

    def attach_tokens(self, sequence: NativeTokenSequence) -> None:
        expected = int(self.model_inputs["valid_macro_mask"].sum())
        if len(sequence.texts) != expected:
            raise MacroContractError(
                f"token/macro count mismatch: {len(sequence.texts)} != {expected}"
            )
        self.model_inputs.update({
            "input_ids": sequence.input_ids,
            "attention_mask": sequence.attention_mask,
            "token_to_macro": sequence.token_to_macro,
            "macro_token_start": sequence.macro_token_start,
            "macro_token_end": sequence.macro_token_end,
        })

    def attach_semantics(
        self,
        static_semantic: np.ndarray,
        static_anchor: np.ndarray,
    ) -> None:
        expected_shape = (len(self.model_inputs["valid_macro_mask"]),)
        semantic = np.asarray(static_semantic)
        anchor = np.asarray(static_anchor)
        if semantic.ndim != 2 or semantic.shape[:1] != expected_shape:
            raise MacroContractError(
                "static_semantic must be [K_macro,D_semantic]"
            )
        if anchor.ndim != 2 or anchor.shape[:1] != expected_shape:
            raise MacroContractError(
                "static_anchor must be [K_macro,D_qwen]"
            )
        if semantic.shape[1] <= 0 or anchor.shape[1] <= 0:
            raise MacroContractError("semantic and anchor widths must be positive")
        valid = np.asarray(self.model_inputs["valid_macro_mask"], dtype=np.bool_)
        if np.any(semantic[~valid] != 0) or np.any(anchor[~valid] != 0):
            raise MacroContractError("padded semantic rows must be exactly zero")
        self.model_inputs.update({
            "static_semantic": semantic,
            "static_anchor": anchor,
        })

    def attach_learned_null(self) -> None:
        macros = len(self.model_inputs["valid_macro_mask"])
        self.model_inputs["null_semantic_marker"] = np.zeros(
            macros, dtype=np.uint8,
        )


class ParquetInstructionResolver:
    """Validated PC-to-real-assembly resolver that fails closed."""

    def __init__(self, parquet_path: str | Path):
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyarrow is required for static dictionaries") from exc
        self.path = str(parquet_path)
        table = pq.read_table(self.path)
        required = {
            "schema_version", "module_pc", "size_bytes", "bytes_hex",
            "mnemonic", "operands", "is_branch", "branch_relative_target",
            "binary_hash", "section_name", "decode_source", "bb_id",
            "cfg_next", "cfg_taken",
        }
        missing = required - set(table.column_names)
        if missing:
            raise MacroContractError(
                f"static dictionary is missing columns: {sorted(missing)}"
            )
        data = table.select(sorted(required)).to_pydict()
        rows: Dict[int, Dict[str, Any]] = {}
        for index, pc_value in enumerate(data["module_pc"]):
            schema = str(data["schema_version"][index])
            if schema != STATIC_DICT_SCHEMA_VERSION:
                raise MacroContractError(
                    f"static schema {schema!r} != {STATIC_DICT_SCHEMA_VERSION!r}"
                )
            pc = int(pc_value)
            size = int(data["size_bytes"][index])
            byte_text = str(data["bytes_hex"][index])
            if not 1 <= size <= 15 or len(byte_text) != size * 2:
                raise MacroContractError(
                    f"invalid instruction bytes at 0x{pc:x}: "
                    f"size={size} bytes={byte_text!r}"
                )
            if pc in rows:
                raise MacroContractError(f"duplicate static pc 0x{pc:x}")
            mnemonic = str(data["mnemonic"][index]).strip().lower()
            rows[pc] = {
                "binary_hash": str(data["binary_hash"][index]),
                "section_name": str(data["section_name"][index]),
                "decode_source": str(data["decode_source"][index]),
                "size_bytes": size,
                "bytes_hex": byte_text.lower(),
                "mnemonic": mnemonic,
                "operands": str(data["operands"][index] or "").strip(),
                "is_branch": bool(data["is_branch"][index]),
                "target": int(data["branch_relative_target"][index]),
                "bb_id": int(data["bb_id"][index]),
                "cfg_next": int(data["cfg_next"][index]),
                "cfg_taken": int(data["cfg_taken"][index]),
                "semantic_valid": mnemonic not in {"", "(bad)", ".byte"},
            }
        self.rows = rows
        binary_hashes = {str(row["binary_hash"]) for row in rows.values()}
        if len(binary_hashes) != 1:
            raise MacroContractError(
                f"static dictionary must contain one binary hash, got "
                f"{len(binary_hashes)}"
            )
        self.binary_hash = next(iter(binary_hashes))

    def coverage(self, macro_pcs: Iterable[int]) -> Dict[str, Any]:
        values = sorted({int(value) for value in macro_pcs})
        missing = [pc for pc in values if pc not in self.rows]
        invalid = [
            pc for pc in values
            if pc in self.rows and not bool(self.rows[pc]["semantic_valid"])
        ]
        return {
            "n_unique": len(values),
            "n_missing": len(missing),
            "n_invalid": len(invalid),
            "missing": missing,
            "invalid": invalid,
        }

    def is_architectural_branch(self, macro_pc: int) -> bool:
        pc = int(macro_pc)
        row = self.rows.get(pc)
        if row is None or not bool(row["semantic_valid"]):
            raise MacroContractError(f"no semantic static branch class for pc 0x{pc:x}")
        return bool(row["is_branch"])

    def render_window(self, macro_pcs: Sequence[int]) -> List[str]:
        pcs = [int(value) for value in macro_pcs]
        positions: Dict[int, List[int]] = {}
        for index, pc in enumerate(pcs):
            positions.setdefault(pc, []).append(index)
        rendered: List[str] = []
        for index, pc in enumerate(pcs):
            row = self.rows.get(pc)
            if row is None:
                raise MacroContractError(f"no static instruction for pc 0x{pc:x}")
            if not bool(row["semantic_valid"]):
                raise MacroContractError(
                    f"pc 0x{pc:x} does not decode to a semantic x86 instruction"
                )
            mnemonic = str(row["mnemonic"])
            operands = str(row["operands"])
            if bool(row["is_branch"]):
                target = int(row["target"])
                target_positions = positions.get(target, [])
                if target_positions:
                    nearest = min(
                        target_positions, key=lambda value: abs(value - index)
                    )
                    delta = nearest - index
                    direction = "fwd" if delta >= 0 else "back"
                    label = f".L_{direction}_{abs(delta)}"
                else:
                    label = ".L_external"
                operands = _BRANCH_TARGET_RE.sub(label, operands, count=1)
            text = f"{mnemonic} {operands}".rstrip()
            if "\n" in text or "\r" in text:
                raise MacroContractError(f"multi-line instruction at pc 0x{pc:x}")
            rendered.append(text)
        return rendered


class SemanticVariantInstructionResolver:
    """Deterministic, label-free text controls for the semantic gate.

    The transformations operate only on validated static assembly.  They do
    not inspect commit ticks, branch outcomes, addresses, workload names, or
    any model label.  ``mnemonic_shuffle`` preserves the exact mnemonic
    multiset while breaking opcode/operand and dynamic-position alignment.
    """

    def __init__(
        self,
        base: ParquetInstructionResolver,
        variant: str,
    ) -> None:
        normalized = str(variant)
        if normalized not in SEMANTIC_TEXT_VARIANTS:
            raise ValueError(
                f"semantic text variant {normalized!r} is not one of "
                f"{SEMANTIC_TEXT_VARIANTS}"
            )
        self.base = base
        self.variant = normalized

    def coverage(self, macro_pcs: Iterable[int]) -> Dict[str, Any]:
        return self.base.coverage(macro_pcs)

    def is_architectural_branch(self, macro_pc: int) -> bool:
        return self.base.is_architectural_branch(macro_pc)

    @staticmethod
    def _split(text: str) -> tuple[str, str]:
        pieces = str(text).split(maxsplit=1)
        return pieces[0], pieces[1] if len(pieces) == 2 else ""

    def _pseudo(self, pcs: Sequence[int], real: Sequence[str]) -> List[str]:
        output: List[str] = []
        for pc, text in zip(pcs, real):
            row = self.base.rows[int(pc)]
            mnemonic = str(row["mnemonic"]).lower()
            _real_mnemonic, operands = self._split(text)
            label_match = _LOCAL_BRANCH_LABEL_RE.search(operands)
            label = label_match.group(0) if label_match else ".L_external"
            if mnemonic.startswith("ret"):
                rendered = "ret"
            elif mnemonic.startswith("call"):
                rendered = f"call {label}"
            elif bool(row["is_branch"]):
                rendered = f"jmp {label}"
            elif "[" in operands and "]" in operands:
                rendered = "mov rax, qword ptr [rbx]"
            elif mnemonic.startswith(("cmp", "test")):
                rendered = "cmp rax, rbx"
            else:
                rendered = "add rax, rbx"
            output.append(rendered)
        return output

    @staticmethod
    def _mnemonic_shuffle(real: Sequence[str]) -> List[str]:
        split = [SemanticVariantInstructionResolver._split(text) for text in real]
        mnemonics = [mnemonic for mnemonic, _operands in split]
        if len(mnemonics) > 1:
            payload = (
                "macro-v29-mnemonic-shuffle-v1\n" + "\n".join(real)
            ).encode("utf-8")
            seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
            order = list(range(len(mnemonics)))
            random.Random(seed).shuffle(order)
            shuffled = [mnemonics[index] for index in order]
            if shuffled == mnemonics:
                shuffled = shuffled[1:] + shuffled[:1]
        else:
            shuffled = mnemonics
        return [
            f"{mnemonic} {operands}".rstrip()
            for mnemonic, (_old, operands) in zip(shuffled, split)
        ]

    @staticmethod
    def _register_rename(real: Sequence[str]) -> List[str]:
        def replace(match: re.Match[str]) -> str:
            original = match.group(1)
            renamed = _REGISTER_RENAME.get(original.lower(), original.lower())
            return renamed.upper() if original.isupper() else renamed

        return [_REGISTER_RE.sub(replace, text) for text in real]

    def render_window(self, macro_pcs: Sequence[int]) -> List[str]:
        pcs = [int(value) for value in macro_pcs]
        real = self.base.render_window(pcs)
        if self.variant == "real":
            return real
        if self.variant == "pseudo":
            return self._pseudo(pcs, real)
        if self.variant == "mnemonic_shuffle":
            return self._mnemonic_shuffle(real)
        if self.variant == "register_rename":
            return self._register_rename(real)
        raise AssertionError(f"unhandled semantic variant {self.variant}")


def tokenize_macro_texts(
    texts: Sequence[str],
    tokenizer: Any,
    *,
    k_macro: int = DEFAULT_K_MACRO,
    max_tokens: int = 4096,
) -> NativeTokenSequence:
    """Tokenize complete instructions with the unmodified model tokenizer."""

    if len(texts) > k_macro:
        raise MacroContractError(f"{len(texts)} texts exceed K_macro={k_macro}")
    token_ids: List[int] = []
    token_to_macro: List[int] = []
    starts = np.full(k_macro, -1, dtype=np.int32)
    ends = np.full(k_macro, -1, dtype=np.int32)
    tokenizer_size_before = len(tokenizer)
    for macro_index, original in enumerate(texts):
        text = str(original).strip()
        if not text or "\n" in text or "\r" in text:
            raise MacroContractError(
                f"invalid one-line assembly for macro {macro_index}: {original!r}"
            )
        encoded = tokenizer(
            text + "\n",
            add_special_tokens=False,
            truncation=False,
        )
        ids = [int(value) for value in encoded["input_ids"]]
        if not ids:
            raise MacroContractError(f"tokenizer returned no ids for macro {macro_index}")
        starts[macro_index] = len(token_ids)
        token_ids.extend(ids)
        token_to_macro.extend([macro_index] * len(ids))
        ends[macro_index] = len(token_ids)
        if len(token_ids) > max_tokens:
            raise MacroTokenOverflow(
                f"complete {len(texts)}-macro sequence needs {len(token_ids)} "
                f"tokens, exceeding max_tokens={max_tokens}"
            )
    if len(tokenizer) != tokenizer_size_before:
        raise MacroContractError("tokenizer vocabulary changed during encoding")
    values = np.asarray(token_ids, dtype=np.int64)
    return NativeTokenSequence(
        input_ids=values,
        attention_mask=np.ones(values.shape, dtype=np.int64),
        token_to_macro=np.asarray(token_to_macro, dtype=np.int32),
        macro_token_start=starts,
        macro_token_end=ends,
        texts=[str(value).strip() for value in texts],
    )


class CachedTokenSource:
    """Per-PC prebuilt token cache with online branch-label override.

    The cache stores context-free ids that use the ``.L_external`` label for
    every architectural branch.  For any window we look up the per-PC ids from
    memory-mapped npz arrays and only pay the tokenizer cost for the small
    subset of architectural branches whose target actually falls inside the
    current 256-macro window.  Non-cacheable variants (mnemonic_shuffle) fall
    back to the online tokenizer path.
    """

    CACHE_SCHEMA_VERSION = "macro-v29-token-cache-1"

    def __init__(
        self,
        cache_root: str | Path,
        tokenizer: Any,
        *,
        variant: str,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.variant = str(variant)
        manifest_path = self.cache_root / "manifest.json"
        if not manifest_path.is_file():
            raise MacroContractError(
                f"token cache manifest not found: {manifest_path}"
            )
        self.manifest = json.loads(manifest_path.read_text())
        if self.manifest.get("schema_version") != self.CACHE_SCHEMA_VERSION:
            raise MacroContractError(
                f"token cache schema {self.manifest.get('schema_version')!r}"
                f" != {self.CACHE_SCHEMA_VERSION!r}"
            )
        expected_fp = str(self.manifest.get("tokenizer_fingerprint", ""))
        if expected_fp:
            # Import lazily to avoid a hard import cycle with the training
            # driver which computes the fingerprint the same way.
            payload = {
                "class": type(tokenizer).__name__,
                "vocab": sorted(
                    (str(token), int(index))
                    for token, index in tokenizer.get_vocab().items()
                ),
                "special_tokens_map": getattr(
                    tokenizer, "special_tokens_map", {},
                ),
            }
            observed_fp = hashlib.sha256(json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
            if observed_fp != expected_fp:
                raise MacroContractError(
                    "tokenizer fingerprint does not match the cache; rebuild "
                    "the cache after changing the tokenizer"
                )
        self._parquet_to_hash: Dict[str, str] = {}
        for entry in self.manifest.get("workloads", []):
            self._parquet_to_hash[
                str(Path(str(entry["parquet"])).resolve())
            ] = str(entry["binary_hash"])
        self._per_workload: Dict[str, Dict[str, Any]] = {}
        self._pc_index_cache: Dict[str, Dict[int, int]] = {}

    def has_variant(self) -> bool:
        variant_dir = self.cache_root / self.variant
        return variant_dir.is_dir()

    def _workload_arrays(self, parquet_path: str) -> Dict[str, Any]:
        key = str(Path(str(parquet_path)).resolve())
        binary_hash = self._parquet_to_hash.get(key)
        if binary_hash is None:
            raise MacroContractError(
                f"token cache does not know parquet {key}"
            )
        cached = self._per_workload.get(binary_hash)
        if cached is not None:
            return cached
        path = self.cache_root / self.variant / f"{binary_hash}.npz"
        if not path.is_file():
            raise MacroContractError(
                f"token cache missing variant '{self.variant}' for "
                f"binary_hash={binary_hash} (expected {path})"
            )
        # ``mmap_mode`` here keeps the token id buffer paged in from disk on
        # demand rather than materializing every workload up-front.
        archive = np.load(path, mmap_mode="r")
        arrays = {
            "pcs": np.asarray(archive["pcs"], dtype=np.uint64),
            "token_offsets": np.asarray(
                archive["token_offsets"], dtype=np.int64,
            ),
            "token_ids": archive["token_ids"],
            "is_branch": np.asarray(archive["is_branch"], dtype=np.bool_),
            "branch_target": np.asarray(
                archive["branch_target"], dtype=np.int64,
            ),
        }
        self._per_workload[binary_hash] = arrays
        pc_index = {int(pc): index for index, pc in enumerate(arrays["pcs"])}
        self._pc_index_cache[binary_hash] = pc_index
        return arrays

    def _pc_indices(
        self, parquet_path: str, pcs: Sequence[int],
    ) -> tuple[np.ndarray, Dict[str, Any]]:
        arrays = self._workload_arrays(parquet_path)
        binary_hash = self._parquet_to_hash[
            str(Path(str(parquet_path)).resolve())
        ]
        lookup = self._pc_index_cache[binary_hash]
        indices = np.empty(len(pcs), dtype=np.int64)
        for local, pc in enumerate(pcs):
            index = lookup.get(int(pc))
            if index is None:
                raise MacroContractError(
                    f"token cache lacks pc 0x{int(pc):x} (variant={self.variant})"
                )
            indices[local] = index
        return indices, arrays

    def tokenize_window(
        self,
        pcs: Sequence[int],
        parquet_path: str,
        resolver: "InstructionResolver",
        tokenizer: Any,
        *,
        k_macro: int = DEFAULT_K_MACRO,
        max_tokens: int = 4096,
    ) -> NativeTokenSequence:
        indices, arrays = self._pc_indices(parquet_path, pcs)
        offsets = arrays["token_offsets"]
        token_ids_arr = arrays["token_ids"]
        is_branch = arrays["is_branch"]
        branch_target = arrays["branch_target"]

        positions: Dict[int, List[int]] = {}
        for local, pc in enumerate(pcs):
            positions.setdefault(int(pc), []).append(local)

        override_indices: List[int] = []
        for local in range(len(pcs)):
            row_index = int(indices[local])
            if not bool(is_branch[row_index]):
                continue
            target = int(branch_target[row_index])
            if target in positions:
                override_indices.append(local)

        override_texts: Dict[int, str] = {}
        if override_indices:
            rendered = resolver.render_window(list(pcs))
            for local in override_indices:
                override_texts[local] = str(rendered[local]).strip()

        starts = np.full(k_macro, -1, dtype=np.int32)
        ends = np.full(k_macro, -1, dtype=np.int32)
        segments: List[np.ndarray] = []
        to_macro_segments: List[np.ndarray] = []
        cursor = 0
        for local, pc in enumerate(pcs):
            row_index = int(indices[local])
            if local in override_texts:
                text = override_texts[local]
                if not text or "\n" in text or "\r" in text:
                    raise MacroContractError(
                        f"invalid override rendering for macro {local}"
                    )
                encoded = tokenizer(
                    text + "\n", add_special_tokens=False, truncation=False,
                )
                ids_np = np.asarray(
                    [int(value) for value in encoded["input_ids"]],
                    dtype=np.int64,
                )
                if ids_np.size == 0:
                    raise MacroContractError(
                        f"tokenizer returned no ids for override macro {local}"
                    )
            else:
                start = int(offsets[row_index])
                stop = int(offsets[row_index + 1])
                if stop <= start:
                    raise MacroContractError(
                        f"empty cached tokens for pc 0x{int(pc):x}"
                    )
                ids_np = np.asarray(token_ids_arr[start:stop], dtype=np.int64)
            starts[local] = cursor
            segments.append(ids_np)
            to_macro_segments.append(
                np.full(ids_np.shape[0], local, dtype=np.int32),
            )
            cursor += int(ids_np.shape[0])
            ends[local] = cursor
            if cursor > max_tokens:
                raise MacroTokenOverflow(
                    f"complete {len(pcs)}-macro sequence needs {cursor} "
                    f"tokens, exceeding max_tokens={max_tokens}"
                )
        values = np.concatenate(segments) if segments else np.zeros(0, dtype=np.int64)
        to_macro = (
            np.concatenate(to_macro_segments)
            if to_macro_segments
            else np.zeros(0, dtype=np.int32)
        )
        # ``texts`` is only used for debugging surface parity; the fast cache
        # path never re-renders the whole window when no override is required.
        rendered_texts = [""] * len(pcs)
        if override_texts and len(override_texts) == len(pcs):
            for local, text in override_texts.items():
                rendered_texts[local] = text
        return NativeTokenSequence(
            input_ids=values,
            attention_mask=np.ones(values.shape, dtype=np.int64),
            token_to_macro=to_macro,
            macro_token_start=starts,
            macro_token_end=ends,
            texts=rendered_texts,
        )


class CachedSemanticSource:
    """Version-locked, fail-closed static macro semantic cache.

    The hot path only gathers frozen vectors.  It never loads the offline
    encoder and never substitutes an unknown vector for a missing PC.
    """

    CACHE_SCHEMA_VERSION = "macro-v29-semantic-cache-1"

    def __init__(
        self,
        cache_root: str | Path,
        *,
        fixed_permutation_seed: int | None = None,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.fixed_permutation_seed = (
            None
            if fixed_permutation_seed is None
            else int(fixed_permutation_seed)
        )
        manifest_path = self.cache_root / "manifest.json"
        if not manifest_path.is_file():
            raise MacroContractError(
                f"semantic cache manifest not found: {manifest_path}"
            )
        manifest_bytes = manifest_path.read_bytes()
        self.manifest = json.loads(manifest_bytes)
        identity_manifest = dict(self.manifest)
        # Build timing/recompute reports are audit metadata, not cache
        # identity.  Excluding them keeps the checkpoint contract stable when
        # an identical cache is re-audited without changing any vectors.
        identity_manifest.pop("build_reports", None)
        identity_manifest.pop("total_elapsed_s", None)
        identity_manifest.pop("built_at", None)
        self.manifest_hash = _json_fingerprint(identity_manifest)
        if self.manifest.get("schema_version") != self.CACHE_SCHEMA_VERSION:
            raise MacroContractError(
                f"semantic cache schema {self.manifest.get('schema_version')!r} "
                f"!= {self.CACHE_SCHEMA_VERSION!r}"
            )
        required = {
            "semantic_encoder_model", "semantic_encoder_revision",
            "semantic_encoder_artifact_fingerprint",
            "semantic_encoder_config_fingerprint", "tokenizer_fingerprint",
            "semantic_prompt_schema_version", "semantic_pooling_policy",
            "semantic_dim", "anchor_dim", "anchor_policy", "binaries",
            "offline_encoder_frozen", "model_facing_identity_fields",
        }
        missing = required - set(self.manifest)
        if missing:
            raise MacroContractError(
                f"semantic cache manifest lacks fields: {sorted(missing)}"
            )
        self.semantic_dim = int(self.manifest["semantic_dim"])
        self.anchor_dim = int(self.manifest["anchor_dim"])
        if self.semantic_dim <= 0 or self.anchor_dim <= 0:
            raise MacroContractError("semantic cache dimensions must be positive")
        if self.manifest["offline_encoder_frozen"] is not True:
            raise MacroContractError("offline semantic encoder must be frozen")
        if self.manifest["model_facing_identity_fields"] != []:
            raise MacroContractError(
                "semantic cache exposes identity fields to the online model"
            )
        binaries = self.manifest["binaries"]
        if not isinstance(binaries, list) or not binaries:
            raise MacroContractError("semantic cache has no binary entries")
        self._parquet_to_hash: Dict[str, str] = {}
        self._binary_files: Dict[str, str] = {}
        self._binary_integrity: Dict[str, Dict[str, str]] = {}
        for entry in binaries:
            if not isinstance(entry, Mapping):
                raise MacroContractError("invalid semantic binary entry")
            binary_hash = str(entry.get("binary_hash", ""))
            parquet = str(entry.get("parquet", ""))
            cache_file = str(entry.get("cache_file", ""))
            parquet_sha256 = str(entry.get("parquet_sha256", ""))
            shard_sha256 = str(entry.get("shard_sha256", ""))
            pc_set_hash = str(entry.get("pc_set_hash", ""))
            if not all((
                binary_hash, parquet, cache_file, parquet_sha256,
                shard_sha256, pc_set_hash,
            )):
                raise MacroContractError(
                    "semantic binary entry requires binary/parquet/shard/PC-set "
                    "identity and SHA256 fields"
                )
            resolved = str(Path(parquet).resolve())
            if resolved in self._parquet_to_hash:
                raise MacroContractError(
                    f"duplicate semantic parquet entry: {resolved}"
                )
            self._parquet_to_hash[resolved] = binary_hash
            self._binary_files[binary_hash] = cache_file
            if _file_sha256(resolved) != parquet_sha256:
                raise MacroContractError(
                    f"static parquet hash mismatch for binary={binary_hash}"
                )
            self._binary_integrity[binary_hash] = {
                "shard_sha256": shard_sha256,
                "pc_set_hash": pc_set_hash,
            }
        self._arrays: Dict[str, Dict[str, np.ndarray]] = {}
        self._pc_indices: Dict[str, Dict[int, int]] = {}
        self._fixed_permutations: Dict[str, np.ndarray] = {}
        self._fixed_permutation_reports: Dict[str, Dict[str, Any]] = {}

    @property
    def contract(self) -> Dict[str, Any]:
        """Checkpoint-facing cache provenance (no workload/split identity)."""

        contract = {
            "semantic_encoder_model": str(
                self.manifest["semantic_encoder_model"]
            ),
            "semantic_encoder_revision": str(
                self.manifest["semantic_encoder_revision"]
            ),
            "semantic_encoder_artifact_fingerprint": str(
                self.manifest["semantic_encoder_artifact_fingerprint"]
            ),
            "semantic_encoder_config_fingerprint": str(
                self.manifest["semantic_encoder_config_fingerprint"]
            ),
            "semantic_encoder_tokenizer_fingerprint": str(
                self.manifest["tokenizer_fingerprint"]
            ),
            "semantic_prompt_schema_version": str(
                self.manifest["semantic_prompt_schema_version"]
            ),
            "semantic_pooling_policy": str(
                self.manifest["semantic_pooling_policy"]
            ),
            "semantic_cache_manifest_hash": self.manifest_hash,
            "semantic_dim": self.semantic_dim,
            "anchor_policy": str(self.manifest["anchor_policy"]),
            "anchor_dim": self.anchor_dim,
        }
        for key in (
            "semantic_encoder_adapter_schema",
            "semantic_encoder_lora_adapter_fingerprint",
        ):
            if key in self.manifest:
                contract[key] = self.manifest[key]
        return contract

    def _load_binary(self, parquet_path: str | Path) -> tuple[str, Dict[str, np.ndarray]]:
        parquet = str(Path(parquet_path).resolve())
        binary_hash = self._parquet_to_hash.get(parquet)
        if binary_hash is None:
            raise MacroContractError(
                f"semantic cache does not know parquet {parquet}"
            )
        cached = self._arrays.get(binary_hash)
        if cached is not None:
            return binary_hash, cached
        cache_file = self.cache_root / self._binary_files[binary_hash]
        if not cache_file.is_file():
            raise MacroContractError(
                f"semantic cache shard is missing: {cache_file}"
            )
        integrity = self._binary_integrity[binary_hash]
        if _file_sha256(cache_file) != integrity["shard_sha256"]:
            raise MacroContractError(
                f"semantic shard hash mismatch for binary={binary_hash}"
            )
        archive = np.load(cache_file, mmap_mode="r", allow_pickle=False)
        required = {"pcs", "semantic", "anchor", "semantic_key_hashes"}
        if not required.issubset(archive.files):
            raise MacroContractError(
                f"semantic shard {cache_file} lacks "
                f"{sorted(required - set(archive.files))}"
            )
        arrays = {
            "pcs": np.asarray(archive["pcs"], dtype=np.uint64),
            "semantic": np.asarray(archive["semantic"]),
            "anchor": np.asarray(archive["anchor"]),
            "semantic_key_hashes": np.asarray(archive["semantic_key_hashes"]),
        }
        pcs = arrays["pcs"]
        semantic = arrays["semantic"]
        anchor = arrays["anchor"]
        key_hashes = arrays["semantic_key_hashes"]
        if pcs.ndim != 1 or len(pcs) == 0:
            raise MacroContractError(f"semantic shard {cache_file} has no PCs")
        if np.any(pcs[1:] <= pcs[:-1]):
            raise MacroContractError("semantic cache PCs must be sorted and unique")
        if _json_fingerprint([int(value) for value in pcs]) != integrity["pc_set_hash"]:
            raise MacroContractError(
                f"semantic shard PC-set hash mismatch for binary={binary_hash}"
            )
        if semantic.shape != (len(pcs), self.semantic_dim):
            raise MacroContractError(
                f"semantic shape {semantic.shape} != "
                f"({len(pcs)},{self.semantic_dim})"
            )
        if anchor.shape != (len(pcs), self.anchor_dim):
            raise MacroContractError(
                f"anchor shape {anchor.shape} != "
                f"({len(pcs)},{self.anchor_dim})"
            )
        if key_hashes.shape != (len(pcs),):
            raise MacroContractError("semantic key hash shape mismatch")
        if not np.isfinite(semantic).all() or not np.isfinite(anchor).all():
            raise MacroContractError("semantic cache contains non-finite vectors")
        self._arrays[binary_hash] = arrays
        self._pc_indices[binary_hash] = {
            int(pc): index for index, pc in enumerate(pcs)
        }
        return binary_hash, arrays

    def _fixed_permutation(
        self,
        binary_hash: str,
        pcs: np.ndarray,
    ) -> np.ndarray:
        """Return one deterministic, marginal-preserving PC permutation.

        The mapping is scoped to a static binary and therefore stays constant
        across train/validation/deployment windows.  Semantic and anchor rows
        use the same mapping so this intervention destroys only their
        association with the requested PC, not their joint distribution.
        """

        cached = self._fixed_permutations.get(binary_hash)
        if cached is not None:
            return cached
        if self.fixed_permutation_seed is None:
            raise RuntimeError("fixed semantic permutation was not enabled")
        count = int(len(pcs))
        order = sorted(
            range(count),
            key=lambda index: hashlib.sha256(
                f"{self.fixed_permutation_seed}:{binary_hash}:"
                f"{int(pcs[index])}".encode("utf-8")
            ).digest(),
        )
        permutation = np.arange(count, dtype=np.int64)
        if count > 1:
            for position, source_index in enumerate(order):
                permutation[source_index] = order[(position + 1) % count]
        fixed_points = int(np.count_nonzero(
            permutation == np.arange(count, dtype=np.int64)
        ))
        fingerprint = hashlib.sha256(
            permutation.astype("<i8", copy=False).tobytes()
        ).hexdigest()
        self._fixed_permutations[binary_hash] = permutation
        self._fixed_permutation_reports[binary_hash] = {
            "binary_hash": str(binary_hash),
            "pc_count": count,
            "fixed_points": fixed_points,
            "permutation_fingerprint": fingerprint,
        }
        return permutation

    @property
    def intervention_report(self) -> Dict[str, Any]:
        enabled = self.fixed_permutation_seed is not None
        return {
            "mode": "fixed_semantic_permute" if enabled else "full_real",
            "seed": self.fixed_permutation_seed,
            "mapping_scope": "per_binary_static_pc" if enabled else None,
            "paired_fields": ["static_semantic", "static_anchor"] if enabled else [],
            "marginal_preserved": bool(enabled),
            "binaries": [
                self._fixed_permutation_reports[key]
                for key in sorted(self._fixed_permutation_reports)
            ],
        }

    def gather_window(
        self,
        pcs: Sequence[int],
        parquet_path: str | Path,
        *,
        k_macro: int = DEFAULT_K_MACRO,
    ) -> tuple[np.ndarray, np.ndarray]:
        if len(pcs) > int(k_macro):
            raise MacroContractError(
                f"semantic window has {len(pcs)} PCs > K={k_macro}"
            )
        binary_hash, arrays = self._load_binary(parquet_path)
        lookup = self._pc_indices[binary_hash]
        indices = np.empty(len(pcs), dtype=np.int64)
        for local, pc in enumerate(pcs):
            index = lookup.get(int(pc))
            if index is None:
                raise MacroContractError(
                    f"semantic cache miss for binary={binary_hash} "
                    f"pc=0x{int(pc):x}; hot-path fallback is forbidden"
                )
            indices[local] = index
        if self.fixed_permutation_seed is not None and len(indices):
            permutation = self._fixed_permutation(
                binary_hash, arrays["pcs"],
            )
            indices = permutation[indices]
        semantic = np.zeros(
            (int(k_macro), self.semantic_dim), dtype=arrays["semantic"].dtype,
        )
        anchor = np.zeros(
            (int(k_macro), self.anchor_dim), dtype=arrays["anchor"].dtype,
        )
        if len(indices):
            semantic[:len(indices)] = arrays["semantic"][indices]
            anchor[:len(indices)] = arrays["anchor"][indices]
        return semantic, anchor


class PackedCoreMacroView:
    """Memory-mapped macro view for one packed3 core directory."""

    REQUIRED_ARRAYS = (
        "fields", "macro_end", "macro_pc", "commit_tick", "branch",
        "branch_miss", "access", "semantic_flags", "resource",
        "physical_line", "functional_line", "functional_page", "producer_log",
    )

    def __init__(
        self,
        core_dir: str | Path,
        *,
        tick_per_cycle: float,
        k_macro: int = DEFAULT_K_MACRO,
        horizons: Sequence[float] = DEFAULT_HORIZONS,
        validate: bool = True,
    ):
        self.core_dir = Path(core_dir)
        self.tick_per_cycle = float(tick_per_cycle)
        self.k_macro = int(k_macro)
        self.horizons = np.asarray(
            tuple(float(value) for value in horizons), dtype=np.float32,
        )
        if self.tick_per_cycle <= 0:
            raise MacroContractError("tick_per_cycle must be positive")
        if self.k_macro <= 0:
            raise MacroContractError("k_macro must be positive")
        self.arrays: Dict[str, np.ndarray] = {}
        for name in self.REQUIRED_ARRAYS:
            path = self.core_dir / f"{name}.npy"
            if not path.is_file():
                raise MacroContractError(f"missing packed array {path}")
            self.arrays[name] = np.load(path, mmap_mode="r")
        self.n_uops = int(self.arrays["macro_end"].shape[0])
        lengths = {name: int(array.shape[0]) for name, array in self.arrays.items()}
        if any(length != self.n_uops for length in lengths.values()):
            raise MacroContractError(f"per-UOP length mismatch: {lengths}")
        fields = self.arrays["fields"]
        if fields.ndim != 2 or fields.shape[1] != len(V29_FIELD_SIZES):
            raise MacroContractError(f"fields shape {fields.shape} is not [N,26]")
        self.macro_uop_end = np.flatnonzero(
            np.asarray(self.arrays["macro_end"], dtype=np.uint8)
        ).astype(np.int64) + 1
        if not len(self.macro_uop_end) or int(self.macro_uop_end[-1]) != self.n_uops:
            raise MacroContractError("packed core does not end on a macro boundary")
        self.macro_uop_begin = np.concatenate((
            np.asarray([0], dtype=np.int64),
            self.macro_uop_end[:-1],
        ))
        self.uops_per_macro = self.macro_uop_end - self.macro_uop_begin
        self.n_macros = int(len(self.macro_uop_end))
        self.macro_end_tick = np.asarray(
            self.arrays["commit_tick"][self.macro_uop_end - 1], dtype=np.int64,
        )
        self.macro_pc = np.asarray(
            self.arrays["macro_pc"][self.macro_uop_begin], dtype=np.uint64,
        )
        if validate:
            self.validate_contract()

    def validate_contract(self) -> Dict[str, Any]:
        if np.any(self.uops_per_macro <= 0):
            raise MacroContractError("empty macro span")
        maximum = int(self.uops_per_macro.max())
        ticks = np.asarray(self.arrays["commit_tick"], dtype=np.int64)
        if np.any(ticks[1:] < ticks[:-1]):
            raise MacroContractError("commit_tick is not non-decreasing")
        if np.any(self.macro_end_tick[1:] < self.macro_end_tick[:-1]):
            raise MacroContractError("macro-end commit tick is not non-decreasing")
        macro_end = np.asarray(self.arrays["macro_end"], dtype=np.bool_)
        pc = np.asarray(self.arrays["macro_pc"], dtype=np.uint64)
        within_macro = ~macro_end[:-1]
        if np.any(pc[1:][within_macro] != pc[:-1][within_macro]):
            raise MacroContractError("macro_pc changes inside a macro span")
        branch = np.asarray(self.arrays["branch"], dtype=np.bool_)
        miss = np.asarray(self.arrays["branch_miss"], dtype=np.bool_)
        if np.any(miss & ~branch):
            raise MacroContractError("branch_miss is set on a non-branch UOP")
        return {
            "n_uops": self.n_uops,
            "n_macros": self.n_macros,
            "max_uops_per_macro": maximum,
            "mean_uops_per_macro": float(self.uops_per_macro.mean()),
            "max_same_tick_macros": int(self._max_same_tick_macros()),
        }

    def _max_same_tick_macros(self) -> int:
        if not len(self.macro_end_tick):
            return 0
        boundaries = np.concatenate((
            np.asarray([0], dtype=np.int64),
            np.flatnonzero(
                self.macro_end_tick[1:] != self.macro_end_tick[:-1]
            ).astype(np.int64) + 1,
            np.asarray([len(self.macro_end_tick)], dtype=np.int64),
        ))
        return int(np.diff(boundaries).max())

    def cursor_at_tick(self, state_tick: int) -> int:
        return int(np.searchsorted(self.macro_end_tick, int(state_tick), side="right"))

    def window_at_tick(self, state_tick: int) -> MacroWindow:
        return self.window_from_cursor(
            self.cursor_at_tick(state_tick), state_tick=int(state_tick),
        )

    def window_from_cursor(
        self,
        cursor: int,
        *,
        state_tick: int | None,
        include_labels: bool = True,
    ) -> MacroWindow:
        cursor = int(cursor)
        if include_labels and state_tick is None:
            raise MacroContractError("labels require state_tick")
        normalized_state_tick = int(state_tick) if state_tick is not None else None
        if not 0 <= cursor <= self.n_macros:
            raise MacroContractError(f"macro cursor {cursor} out of range")
        stop = min(self.n_macros, cursor + self.k_macro)
        n_valid = stop - cursor
        valid = np.zeros(self.k_macro, dtype=np.bool_)
        valid[:n_valid] = True
        uop_count = np.zeros(self.k_macro, dtype=np.int16)
        begins = np.full(self.k_macro, -1, dtype=np.int64)
        ends = np.full(self.k_macro, -1, dtype=np.int64)
        pcs = np.zeros(self.k_macro, dtype=np.uint64)
        commit_target = np.zeros(self.k_macro, dtype=np.float32)

        for local, macro_index in enumerate(range(cursor, stop)):
            begin = int(self.macro_uop_begin[macro_index])
            end = int(self.macro_uop_end[macro_index])
            count = end - begin
            uop_count[local] = count
            begins[local] = begin
            ends[local] = end
            pcs[local] = self.macro_pc[macro_index]
            if include_labels:
                delta_tick = (
                    int(self.macro_end_tick[macro_index])
                    - int(normalized_state_tick)
                )
                if delta_tick <= 0:
                    raise MacroContractError(
                        f"non-positive macro target at cursor {cursor}, local {local}"
                    )
                commit_target[local] = float(delta_tick / self.tick_per_cycle)

        if n_valid:
            flat_begin = int(self.macro_uop_begin[cursor])
            flat_end = int(self.macro_uop_end[stop - 1])
        else:
            flat_begin = flat_end = self.n_uops
        flat_count = flat_end - flat_begin
        uop_fields = np.asarray(
            self.arrays["fields"][flat_begin:flat_end], dtype=np.uint16,
        ).copy()
        uop_valid_mask = np.ones(flat_count, dtype=np.bool_)
        uop_access = np.asarray(
            self.arrays["access"][flat_begin:flat_end], dtype=np.uint8,
        ).copy()
        uop_semantic_flags = np.asarray(
            self.arrays["semantic_flags"][flat_begin:flat_end], dtype=np.uint8,
        ).copy()
        uop_to_macro = np.repeat(
            np.arange(n_valid, dtype=np.int16),
            uop_count[:n_valid].astype(np.int64),
        )
        if len(uop_to_macro) != flat_count:
            raise MacroContractError("ragged UOP-to-macro mapping length mismatch")

        labels: Dict[str, np.ndarray] = {}
        if include_labels:
            prefix_target = (
                commit_target[:, None] <= self.horizons[None, :]
            ) & valid[:, None]
            progress_target = prefix_target.sum(axis=0).astype(np.float32)
            labels = {
                "commit_time_target_macro": commit_target,
                "prefix_target": prefix_target.astype(np.float32),
                "progress_target_macro": progress_target,
            }
        return MacroWindow(
            model_inputs={
                "uop_fields": uop_fields,
                "uop_valid_mask": uop_valid_mask,
                "uop_to_macro": uop_to_macro,
                "uop_access": uop_access,
                "uop_semantic_flags": uop_semantic_flags,
                "uop_count": uop_count,
                "valid_macro_mask": valid,
            },
            labels=labels,
            control={
                "schema_version": DATASET_SCHEMA_VERSION,
                "model_input_contract": MODEL_INPUT_CONTRACT,
                "state_tick": normalized_state_tick,
                "macro_cursor": cursor,
                "macro_pc": pcs,
                "macro_uop_begin": begins,
                "macro_uop_end": ends,
                "horizons": self.horizons.copy(),
                "n_valid_macros": n_valid,
            },
        )

    def attach_native_tokens(
        self,
        window: MacroWindow,
        resolver: InstructionResolver,
        tokenizer: Any,
        *,
        max_tokens: int = 4096,
        token_cache: "CachedTokenSource | None" = None,
        parquet_path: str | None = None,
    ) -> None:
        n_valid = int(window.control["n_valid_macros"])
        pcs = [
            int(value)
            for value in np.asarray(window.control["macro_pc"])[:n_valid]
        ]
        if token_cache is not None and parquet_path is not None:
            sequence = token_cache.tokenize_window(
                pcs,
                parquet_path,
                resolver,
                tokenizer,
                k_macro=self.k_macro,
                max_tokens=max_tokens,
            )
        else:
            texts = resolver.render_window(pcs)
            sequence = tokenize_macro_texts(
                texts, tokenizer, k_macro=self.k_macro, max_tokens=max_tokens,
            )
        window.attach_tokens(sequence)
        self._attach_branch_labels(window, resolver, pcs)

    def attach_cached_semantics(
        self,
        window: MacroWindow,
        resolver: InstructionResolver,
        semantic_cache: CachedSemanticSource,
        *,
        parquet_path: str,
    ) -> None:
        n_valid = int(window.control["n_valid_macros"])
        pcs = [
            int(value)
            for value in np.asarray(window.control["macro_pc"])[:n_valid]
        ]
        semantic, anchor = semantic_cache.gather_window(
            pcs, parquet_path, k_macro=self.k_macro,
        )
        window.attach_semantics(semantic, anchor)
        window.control["model_input_contract"] = SEMANTIC_MODEL_INPUT_CONTRACT
        self._attach_branch_labels(window, resolver, pcs)

    def attach_learned_null(
        self,
        window: MacroWindow,
        resolver: InstructionResolver,
    ) -> None:
        n_valid = int(window.control["n_valid_macros"])
        pcs = [
            int(value)
            for value in np.asarray(window.control["macro_pc"])[:n_valid]
        ]
        window.attach_learned_null()
        window.control["model_input_contract"] = NULL_MODEL_INPUT_CONTRACT
        self._attach_branch_labels(window, resolver, pcs)

    def _attach_branch_labels(
        self,
        window: MacroWindow,
        resolver: InstructionResolver,
        pcs: Sequence[int],
    ) -> None:
        if window.labels:
            branch_mask = np.zeros(self.k_macro, dtype=np.bool_)
            branch_miss_target = np.zeros(self.k_macro, dtype=np.float32)
            begins = np.asarray(window.control["macro_uop_begin"])
            ends = np.asarray(window.control["macro_uop_end"])
            for local, pc in enumerate(pcs):
                if not resolver.is_architectural_branch(pc):
                    continue
                branch_mask[local] = True
                begin = int(begins[local])
                end = int(ends[local])
                misses = int(np.asarray(
                    self.arrays["branch_miss"][begin:end], dtype=np.uint8,
                ).sum())
                if misses > 1:
                    raise MacroContractError(
                        f"architectural branch macro at pc 0x{pc:x} has "
                        f"{misses} branch-miss markers"
                    )
                branch_miss_target[local] = float(misses)
            window.labels.update({
                "branch_mask": branch_mask,
                "branch_miss_target": branch_miss_target,
            })


class PackedTraceMacroContext:
    """Build multi-core macro contexts with the version-locked TCSim features."""

    def __init__(
        self,
        trace_root: str | Path,
        *,
        tcsim_root: str | Path = "/data00/yinhaolang/TCSim",
        k_macro: int = DEFAULT_K_MACRO,
    ):
        self.trace_root = Path(trace_root)
        self.meta = json.loads((self.trace_root / "meta.json").read_text())
        if self.meta.get("dataset_schema") != "global-time-v29-packed-3":
            raise MacroContractError("trace is not a packed3 v29 cache")
        self.tick_per_cycle = float(self.meta["tick_per_cycle"])
        self.roi_origin_tick = int(self.meta["sample_grid"]["start_tick"])
        self.sample_period_cycles = float(
            self.meta["sample_grid"]["sample_period_cycles"]
        )
        self.horizons = tuple(float(value) for value in self.meta["horizons"])
        if self.horizons != tuple(DEFAULT_HORIZONS):
            raise MacroContractError(
                f"trace horizons {self.horizons} != {DEFAULT_HORIZONS}"
            )
        self.core_ids = tuple(int(value) for value in self.meta["core_ids"])
        self.core_meta = {
            int(value["core_id"]): value for value in self.meta["cores"]
        }
        self.views = {
            core_id: PackedCoreMacroView(
                self.trace_root / "cores" / str(core_id),
                tick_per_cycle=self.tick_per_cycle,
                k_macro=k_macro,
            )
            for core_id in self.core_ids
        }
        self.k_macro = int(k_macro)
        root_text = str(Path(tcsim_root))
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        try:
            from tcsim.v29.dataset import (
                _context_features_numpy,
                _summarize_window_numpy,
            )
        except ImportError as exc:
            raise MacroContractError(
                f"cannot import version-locked TCSim v29 from {root_text}"
            ) from exc
        self._context_features_numpy = _context_features_numpy
        self._summarize_window_numpy = _summarize_window_numpy
        self.uarch_features = np.asarray(
            self.meta["uarch_features"], dtype=np.float32,
        )

    @staticmethod
    def _pad_slice(
        array: np.ndarray,
        begin: int,
        end: int,
        length: int,
        *,
        fill: int | float,
    ) -> np.ndarray:
        source = np.asarray(array[begin:end])
        output = np.full(
            (length,) + source.shape[1:], fill, dtype=source.dtype,
        )
        output[:len(source)] = source
        return output

    def context_at_tick(
        self,
        state_tick: int,
        resolver: InstructionResolver,
        tokenizer: Any | None,
        *,
        max_tokens: int = 4096,
        token_cache: "CachedTokenSource | None" = None,
        semantic_cache: "CachedSemanticSource | None" = None,
        parquet_path: str | None = None,
        semantic_input_mode: str | None = None,
    ) -> List[MacroWindow]:
        cursors = {
            core_id: self.views[core_id].cursor_at_tick(int(state_tick))
            for core_id in self.core_ids
        }
        return self.context_from_cursors(
            cursors,
            resolver,
            tokenizer,
            state_tick=int(state_tick),
            state_time_cycles=(
                int(state_tick) - self.roi_origin_tick
            ) / self.tick_per_cycle,
            include_labels=True,
            last_commit_cycles=None,
            max_tokens=max_tokens,
            token_cache=token_cache,
            semantic_cache=semantic_cache,
            parquet_path=parquet_path,
            semantic_input_mode=semantic_input_mode,
        )

    def context_from_cursors(
        self,
        cursors: Mapping[int, int],
        resolver: InstructionResolver,
        tokenizer: Any | None,
        *,
        state_tick: int | None,
        state_time_cycles: float,
        include_labels: bool,
        last_commit_cycles: Mapping[int, float] | None,
        max_tokens: int = 4096,
        token_cache: "CachedTokenSource | None" = None,
        semantic_cache: "CachedSemanticSource | None" = None,
        parquet_path: str | None = None,
        semantic_input_mode: str | None = None,
    ) -> List[MacroWindow]:
        mode = str(semantic_input_mode or (
            "cached_macro_soft_token"
            if semantic_cache is not None else "native_token"
        ))
        if mode not in {
            "native_token", "cached_macro_soft_token",
            "learned_null_macro_token",
        }:
            raise MacroContractError(f"unsupported semantic input mode {mode!r}")
        entries = [
            (core_id, int(cursors[core_id]))
            for core_id in self.core_ids
            if int(cursors[core_id]) < self.views[core_id].n_macros
        ]
        if not entries:
            raise MacroContractError("empty active macro context")
        windows = [
            self.views[core_id].window_from_cursor(
                cursor,
                state_tick=state_tick,
                include_labels=include_labels,
            )
            for core_id, cursor in entries
        ]
        spans = []
        for window in windows:
            n_valid = int(window.control["n_valid_macros"])
            begin = int(window.control["macro_uop_begin"][0])
            end = int(window.control["macro_uop_end"][n_valid - 1])
            spans.append((begin, end))
        flat_length = max(end - begin for begin, end in spans)
        chunks = []
        summaries = []
        for (core_id, _cursor), window, (begin, end) in zip(
            entries, windows, spans,
        ):
            view = self.views[core_id]
            valid = np.zeros(flat_length, dtype=np.bool_)
            valid[:end - begin] = True
            chunk_numpy = {
                "resource": self._pad_slice(
                    view.arrays["resource"], begin, end, flat_length, fill=-1,
                ),
                "physical_line": self._pad_slice(
                    view.arrays["physical_line"], begin, end, flat_length, fill=-1,
                ),
                "access": self._pad_slice(
                    view.arrays["access"], begin, end, flat_length, fill=0,
                ),
                "valid_uop_mask": valid,
            }
            chunks.append({"_numpy": chunk_numpy})
            summaries.append(self._summarize_window_numpy(
                self._pad_slice(
                    view.arrays["fields"], begin, end, flat_length, fill=0,
                ),
                chunk_numpy["resource"],
                valid,
                self._pad_slice(
                    view.arrays["semantic_flags"], begin, end, flat_length, fill=0,
                ),
                self._pad_slice(
                    view.arrays["functional_line"], begin, end, flat_length, fill=-1,
                ),
                self._pad_slice(
                    view.arrays["functional_page"], begin, end, flat_length, fill=-1,
                ),
                self._pad_slice(
                    view.arrays["producer_log"], begin, end, flat_length, fill=0.0,
                ),
                self._pad_slice(
                    view.arrays["macro_pc"], begin, end, flat_length, fill=0,
                ),
                self._pad_slice(
                    view.arrays["macro_end"], begin, end, flat_length, fill=0,
                ),
                flat_length,
            ).astype(np.float32))
        dynamic, relations = self._context_features_numpy(chunks)
        active_fraction = len(entries) / max(1, len(self.core_ids))
        for row, ((core_id, cursor), window, (begin, end)) in enumerate(zip(
            entries, windows, spans,
        )):
            dynamic_flat = np.asarray(
                dynamic[row, :end - begin], dtype=np.int64,
            ).copy()
            if len(dynamic_flat) != len(window.model_inputs["uop_fields"]):
                raise MacroContractError(
                    "ragged static/dynamic UOP side lengths differ"
                )
            if include_labels:
                previous_tick = (
                    int(self.views[core_id].macro_end_tick[cursor - 1])
                    if cursor > 0
                    else int(self.core_meta[core_id]["roi_begin_tick"])
                )
                elapsed = max(
                    0.0,
                    (int(state_tick) - previous_tick) / self.tick_per_cycle,
                )
            else:
                if last_commit_cycles is None:
                    raise MacroContractError(
                        "free context requires predicted last_commit_cycles"
                    )
                elapsed = max(
                    0.0,
                    float(state_time_cycles)
                    - float(last_commit_cycles.get(core_id, 0.0)),
                )
            roi_age = max(0.0, float(state_time_cycles))
            window.model_inputs.update({
                "dynamic_uop_fields": dynamic_flat,
                "chunk_summary": summaries[row],
                "relation_features": np.asarray(
                    relations[row], dtype=np.float32,
                ),
                "state_features": np.asarray([
                    math.log1p(elapsed) / 8.0,
                    math.log1p(elapsed) / 8.0,
                    math.log1p(roi_age) / 16.0,
                    float(cursor == 0),
                    active_fraction,
                ], dtype=np.float32),
                "uarch_features": self.uarch_features.copy(),
            })
            window.control["core_id"] = int(core_id)
            if mode == "cached_macro_soft_token":
                if semantic_cache is None:
                    raise MacroContractError(
                        "cached semantic mode requires a semantic cache"
                    )
                if not parquet_path:
                    raise MacroContractError(
                        "semantic input requires a static parquet path"
                    )
                self.views[core_id].attach_cached_semantics(
                    window,
                    resolver,
                    semantic_cache,
                    parquet_path=parquet_path,
                )
            elif mode == "native_token":
                if tokenizer is None:
                    raise MacroContractError(
                        "native-token input requires a tokenizer"
                    )
                self.views[core_id].attach_native_tokens(
                    window, resolver, tokenizer, max_tokens=max_tokens,
                    token_cache=token_cache, parquet_path=parquet_path,
                )
            else:
                if semantic_cache is not None or token_cache is not None:
                    raise MacroContractError(
                        "learned-null mode must not receive a semantic/token cache"
                    )
                self.views[core_id].attach_learned_null(window, resolver)
        return windows


def macro_block_partition(
    trace_id: str,
    block_id: int,
    policy: Mapping[str, Any],
) -> str:
    """Deterministically assign a whole time block to train or validation."""

    percent = int(policy.get("validation_percent", 10))
    if not 0 < percent < 100:
        raise ValueError("validation_percent must be in (0,100)")
    seed = int(policy.get("seed", 20260716))
    payload = f"{seed}:{trace_id}:block:{int(block_id)}".encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 100
    return "validation" if bucket < percent else "train"


def eligible_macro_sample_indices(
    context: PackedTraceMacroContext,
    policy: Mapping[str, Any],
) -> List[int]:
    """Return macro-safe oracle samples for one guarded block partition.

    Eligibility is recomputed from macro retirement boundaries.  The packed3
    UOP cursors are deliberately not trusted because K=256 now means macros.
    """

    partition = str(policy.get("partition", "")).lower()
    if partition not in {"train", "validation", "all"}:
        raise ValueError(f"unsupported macro partition {partition!r}")
    if not bool(policy.get("require_full_lookahead_within_block", True)):
        raise MacroContractError(
            "macro training requires full lookahead within one guarded block"
        )
    guard_cycles = float(policy.get("guard_cycles", max(context.horizons)))
    if guard_cycles < max(context.horizons):
        raise MacroContractError(
            f"guard_cycles={guard_cycles} is smaller than max horizon "
            f"{max(context.horizons)}"
        )
    grid = context.meta["sample_grid"]
    block_cycles = float(grid["block_cycles"])
    if 2.0 * guard_cycles >= block_cycles:
        raise MacroContractError("guard removes the entire time block")
    start_tick = int(grid["start_tick"])
    block_ticks = int(grid["block_ticks"])
    expected_block_ticks = int(round(block_cycles * context.tick_per_cycle))
    if block_ticks != expected_block_ticks:
        raise MacroContractError(
            f"block tick mismatch: {block_ticks} != {expected_block_ticks}"
        )
    sample_ticks = np.load(context.trace_root / "sample_ticks.npy", mmap_mode="r")
    sample_block_ids = np.load(
        context.trace_root / "sample_block_ids.npy", mmap_mode="r",
    )
    if sample_ticks.ndim != 1 or sample_block_ids.shape != sample_ticks.shape:
        raise MacroContractError("sample tick/block arrays have incompatible shapes")

    eligible: List[int] = []
    trace_id = str(context.meta["trace_id"])
    for index, (tick_value, block_value) in enumerate(zip(
        sample_ticks, sample_block_ids,
    )):
        tick = int(tick_value)
        block_id = int(block_value)
        if (
            partition != "all"
            and macro_block_partition(trace_id, block_id, policy) != partition
        ):
            continue
        block_start_tick = start_tick + block_id * block_ticks
        block_end_tick = block_start_tick + block_ticks
        position_cycles = (tick - block_start_tick) / context.tick_per_cycle
        if (
            position_cycles < guard_cycles
            or position_cycles >= block_cycles - guard_cycles
        ):
            continue
        contained = True
        for core_id in context.core_ids:
            view = context.views[core_id]
            cursor = view.cursor_at_tick(tick)
            # Train/validation examples must have the full fixed-K macro
            # lookahead.  Padded tails are deployment-only.
            if cursor + context.k_macro > view.n_macros:
                contained = False
                break
            previous_tick = (
                int(view.macro_end_tick[cursor - 1])
                if cursor > 0
                else int(context.core_meta[core_id]["roi_begin_tick"])
            )
            if previous_tick < block_start_tick:
                contained = False
                break
            if int(view.macro_end_tick[cursor + context.k_macro - 1]) >= block_end_tick:
                contained = False
                break
        if contained:
            eligible.append(index)
    return eligible


def contiguous_macro_sequences(
    eligible_indices: Sequence[int],
    sample_block_ids: Sequence[int] | np.ndarray,
    *,
    sequence_length: int = 4,
    sequence_stride: int | None = None,
) -> List[tuple[int, ...]]:
    """Group eligible samples without crossing a gap or block boundary."""

    length = int(sequence_length)
    stride = int(sequence_stride if sequence_stride is not None else length)
    if length <= 0 or stride <= 0:
        raise ValueError("sequence length and stride must be positive")
    indices = [int(value) for value in eligible_indices]
    if any(right <= left for left, right in zip(indices, indices[1:])):
        raise MacroContractError("eligible sample indices must be strictly increasing")
    block_ids = np.asarray(sample_block_ids)
    runs: List[List[int]] = []
    current: List[int] = []
    for index in indices:
        if not 0 <= index < len(block_ids):
            raise MacroContractError(f"sample index {index} is out of range")
        if current and (
            index != current[-1] + 1
            or int(block_ids[index]) != int(block_ids[current[-1]])
        ):
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)
    sequences: List[tuple[int, ...]] = []
    for run in runs:
        for offset in range(0, len(run) - length + 1, stride):
            sequences.append(tuple(run[offset:offset + length]))
    return sequences


class MacroV29SequenceDataset:
    """Lazy guarded-block dataset of contiguous macro-native contexts.

    Each source mapping contains ``trace_root`` (or ``cache_dir``), either a
    validated ``resolver`` or a ``static_dict`` path, and ``sample_split``.
    """

    def __init__(
        self,
        sources: Sequence[Mapping[str, Any]],
        tokenizer: Any | None,
        *,
        semantic_variant: str = "real",
        semantic_input_mode: str = "native_token",
        sequence_length: int = 4,
        sequence_stride: int | None = None,
        max_tokens: int = 4096,
        tcsim_root: str | Path = "/data00/yinhaolang/TCSim",
        token_cache_root: str | Path | None = None,
        semantic_cache_root: str | Path | None = None,
    ) -> None:
        if semantic_variant not in SEMANTIC_TEXT_VARIANTS:
            raise ValueError(
                f"semantic_variant must be one of {SEMANTIC_TEXT_VARIANTS}"
            )
        self.tokenizer = tokenizer
        self.semantic_variant = str(semantic_variant)
        self.semantic_input_mode = str(semantic_input_mode)
        if self.semantic_input_mode not in {
            "native_token", "cached_macro_soft_token",
            "learned_null_macro_token",
        }:
            raise ValueError(
                "semantic_input_mode must be native_token, "
                "cached_macro_soft_token, or learned_null_macro_token"
            )
        if self.semantic_input_mode == "native_token" and tokenizer is None:
            raise MacroContractError("native-token dataset requires a tokenizer")
        if token_cache_root is not None and semantic_cache_root is not None:
            raise MacroContractError(
                "native token cache and semantic cache are mutually exclusive"
            )
        if self.semantic_input_mode == "cached_macro_soft_token":
            if semantic_cache_root is None:
                raise MacroContractError(
                    "cached macro soft-token input requires semantic_cache_root"
                )
            if self.semantic_variant != "real":
                raise MacroContractError(
                    "semantic cache v1 currently contains validated real assembly; "
                    "build a versioned variant cache before using text controls"
                )
        elif semantic_cache_root is not None:
            raise MacroContractError(
                "semantic_cache_root requires cached_macro_soft_token input mode"
            )
        if (
            self.semantic_input_mode == "learned_null_macro_token"
            and token_cache_root is not None
        ):
            raise MacroContractError(
                "learned-null input must not use a native token cache"
            )
        self.sequence_length = int(sequence_length)
        self.sequence_stride = int(
            sequence_stride
            if sequence_stride is not None
            else self.sequence_length
        )
        self.max_tokens = int(max_tokens)
        self.contexts: List[PackedTraceMacroContext] = []
        self.resolvers: List[InstructionResolver] = []
        self.parquet_paths: List[str] = []
        self.sequences: List[tuple[int, tuple[int, ...]]] = []
        self.sample_trace_ids: List[str] = []
        self.sample_core_counts: List[int] = []
        resolver_cache: Dict[tuple[str, str], InstructionResolver] = {}
        contracts = set()
        if token_cache_root is not None:
            if self.semantic_input_mode != "native_token":
                raise MacroContractError(
                    "token_cache_root is only valid for native-token input"
                )
            self.token_cache: CachedTokenSource | None = CachedTokenSource(
                token_cache_root, tokenizer, variant=self.semantic_variant,
            )
            if not self.token_cache.has_variant():
                raise MacroContractError(
                    f"token cache root {token_cache_root} does not contain "
                    f"variant {self.semantic_variant!r}"
                )
        else:
            self.token_cache = None
        self.semantic_cache = (
            CachedSemanticSource(semantic_cache_root)
            if semantic_cache_root is not None else None
        )
        for source in sources:
            trace_root = source.get("trace_root", source.get("cache_dir", ""))
            if not trace_root:
                raise ValueError("macro source lacks trace_root/cache_dir")
            context = PackedTraceMacroContext(
                str(trace_root), tcsim_root=tcsim_root,
            )
            resolver = source.get("resolver")
            static_dict = source.get("static_dict")
            if resolver is None:
                if not static_dict:
                    raise ValueError("macro source lacks resolver/static_dict")
                static_key = str(Path(str(static_dict)).resolve())
                cache_key = (static_key, self.semantic_variant)
                resolver = resolver_cache.get(cache_key)
                if resolver is None:
                    base_resolver = ParquetInstructionResolver(static_key)
                    resolver = (
                        base_resolver
                        if self.semantic_variant == "real"
                        else SemanticVariantInstructionResolver(
                            base_resolver, self.semantic_variant,
                        )
                    )
                    resolver_cache[cache_key] = resolver
            elif self.semantic_variant != "real":
                if isinstance(resolver, SemanticVariantInstructionResolver):
                    if resolver.variant != self.semantic_variant:
                        raise MacroContractError(
                            f"source resolver variant {resolver.variant!r} != "
                            f"dataset variant {self.semantic_variant!r}"
                        )
                elif isinstance(resolver, ParquetInstructionResolver):
                    resolver = SemanticVariantInstructionResolver(
                        resolver, self.semantic_variant,
                    )
                else:
                    raise MacroContractError(
                        "non-real semantic variants require a validated parquet "
                        "instruction resolver"
                    )
            policy = source.get("sample_split")
            if not isinstance(policy, Mapping):
                raise ValueError("macro source lacks a sample_split mapping")
            eligible = eligible_macro_sample_indices(context, policy)
            block_ids = np.load(
                context.trace_root / "sample_block_ids.npy", mmap_mode="r",
            )
            source_sequences = contiguous_macro_sequences(
                eligible,
                block_ids,
                sequence_length=self.sequence_length,
                sequence_stride=self.sequence_stride,
            )
            store_index = len(self.contexts)
            self.contexts.append(context)
            self.resolvers.append(resolver)
            self.parquet_paths.append(
                str(Path(str(static_dict)).resolve()) if static_dict else ""
            )
            contracts.add((
                context.horizons,
                context.sample_period_cycles,
                str(context.meta.get("resource_decoder_hash", "")),
            ))
            for indices in source_sequences:
                self.sequences.append((store_index, indices))
                self.sample_trace_ids.append(str(context.meta["trace_id"]))
                self.sample_core_counts.append(len(context.core_ids))
        if len(contracts) != 1:
            raise MacroContractError(
                "one macro training run requires identical horizon/period/decoder "
                f"contracts, got {len(contracts)}"
            )
        if not self.sequences:
            raise MacroContractError("macro dataset has no eligible sequences")
        self.contract = next(iter(contracts))
        self.trace_sample_counts = Counter(self.sample_trace_ids)

    def __len__(self) -> int:
        return len(self.sequences)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        context_index, sample_indices = self.sequences[int(index)]
        context = self.contexts[context_index]
        resolver = self.resolvers[context_index]
        parquet_path = (
            self.parquet_paths[context_index]
            if self.token_cache is not None or self.semantic_cache is not None
            else None
        )
        sample_ticks = np.load(
            context.trace_root / "sample_ticks.npy", mmap_mode="r",
        )
        sample_block_ids = np.load(
            context.trace_root / "sample_block_ids.npy", mmap_mode="r",
        )
        ticks = tuple(int(sample_ticks[value]) for value in sample_indices)
        blocks = tuple(int(sample_block_ids[value]) for value in sample_indices)
        if len(set(blocks)) != 1:
            raise MacroContractError("a macro sequence crosses a guarded block")
        return {
            "contexts": [
                context.context_at_tick(
                    tick,
                    resolver,
                    self.tokenizer,
                    max_tokens=self.max_tokens,
                    token_cache=self.token_cache,
                    semantic_cache=self.semantic_cache,
                    parquet_path=parquet_path,
                    semantic_input_mode=self.semantic_input_mode,
                )
                for tick in ticks
            ],
            "trace_id": str(context.meta["trace_id"]),
            "sample_indices": sample_indices,
            "sample_ticks": ticks,
            "sample_block_id": blocks[0],
            "sample_period_cycles": context.sample_period_cycles,
            "horizons": context.horizons,
        }


def model_input_keys(window: MacroWindow) -> frozenset[str]:
    """Return the exact model-facing allowlist for contract tests."""
    return frozenset(window.model_inputs)


def assert_model_input_allowlist(window: MacroWindow) -> None:
    keys = model_input_keys(window)
    unexpected = keys - MODEL_INPUT_ALLOWLIST
    if unexpected:
        raise MacroContractError(
            f"unexpected model input keys: {sorted(unexpected)}"
        )
    forbidden_fragments = (
        "commit_tick", "branch_miss_target", "macro_pc", "physical_line",
        "resource_key", "path_class", "coh_oracle",
    )
    leaked = [
        key for key in keys
        if any(fragment in key for fragment in forbidden_fragments)
    ]
    if leaked:
        raise MacroContractError(f"label/control key leak: {sorted(leaked)}")
    has_native = bool(keys & NATIVE_MODEL_INPUT_KEYS)
    has_semantic = bool(keys & SEMANTIC_MODEL_INPUT_KEYS)
    has_null = bool(keys & NULL_MODEL_INPUT_KEYS)
    if sum((has_native, has_semantic, has_null)) != 1:
        raise MacroContractError(
            "model input must contain exactly one semantic representation"
        )
    representation_keys = (
        NATIVE_MODEL_INPUT_KEYS if has_native
        else SEMANTIC_MODEL_INPUT_KEYS if has_semantic
        else NULL_MODEL_INPUT_KEYS
    )
    required = (
        COMMON_MODEL_INPUT_KEYS | representation_keys
    )
    missing = required - keys
    if missing:
        raise MacroContractError(
            f"model input is incomplete: missing {sorted(missing)}"
        )


def semantic_input_mode(window: MacroWindow) -> str:
    keys = model_input_keys(window)
    if NATIVE_MODEL_INPUT_KEYS.issubset(keys):
        return "native_token"
    if SEMANTIC_MODEL_INPUT_KEYS.issubset(keys):
        return "cached_macro_soft_token"
    if NULL_MODEL_INPUT_KEYS.issubset(keys):
        return "learned_null_macro_token"
    raise MacroContractError("window has no complete semantic input representation")


def collate_macro_contexts(
    contexts: Sequence[Sequence[MacroWindow]],
    *,
    pad_token_id: int = 0,
) -> Dict[str, Any]:
    """Collate one or more global contexts into active-core rows."""

    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("torch is required for collation") from exc
    flat: List[MacroWindow] = []
    sample_ptr = [0]
    core_slots: List[int] = []
    metadata: List[Dict[str, Any]] = []
    input_modes: set[str] = set()
    for context in contexts:
        if not context:
            raise MacroContractError("cannot collate an empty global context")
        for slot, window in enumerate(context):
            assert_model_input_allowlist(window)
            input_modes.add(semantic_input_mode(window))
            flat.append(window)
            core_slots.append(slot)
            metadata.append(dict(window.control))
        sample_ptr.append(len(flat))
    if len(input_modes) != 1:
        raise MacroContractError(
            f"cannot collate mixed semantic input modes: {sorted(input_modes)}"
        )
    input_mode = next(iter(input_modes))

    result: Dict[str, Any] = {}
    if input_mode == "native_token":
        token_length = max(
            len(window.model_inputs["input_ids"]) for window in flat
        )

        def pad_1d(value: np.ndarray, fill: int) -> np.ndarray:
            array = np.asarray(value)
            if len(array) == token_length:
                return array
            output = np.full(token_length, fill, dtype=array.dtype)
            output[:len(array)] = array
            return output

        result.update({
            "input_ids": torch.from_numpy(np.stack([
                pad_1d(window.model_inputs["input_ids"], int(pad_token_id))
                for window in flat
            ])),
            "attention_mask": torch.from_numpy(np.stack([
                pad_1d(window.model_inputs["attention_mask"], 0)
                for window in flat
            ])),
            "token_to_macro": torch.from_numpy(np.stack([
                pad_1d(window.model_inputs["token_to_macro"], -1)
                for window in flat
            ])),
        })
    else:
        if input_mode == "cached_macro_soft_token":
            for key in sorted(SEMANTIC_MODEL_INPUT_KEYS):
                result[key] = torch.from_numpy(np.stack([
                    np.asarray(window.model_inputs[key]) for window in flat
                ]))
    ragged_keys = {
        "uop_fields", "uop_valid_mask", "uop_to_macro", "uop_access",
        "uop_semantic_flags", "dynamic_uop_fields",
    }
    ragged_length = max(
        len(window.model_inputs["uop_valid_mask"]) for window in flat
    )
    for window in flat:
        lengths = {
            key: len(window.model_inputs[key]) for key in ragged_keys
        }
        if len(set(lengths.values())) != 1:
            raise MacroContractError(f"ragged UOP side length mismatch: {lengths}")
        observed = int(window.model_inputs["uop_count"].sum())
        if observed != lengths["uop_valid_mask"]:
            raise MacroContractError(
                f"ragged UOP count mismatch: {observed} != "
                f"{lengths['uop_valid_mask']}"
            )

    def pad_ragged(value: np.ndarray, fill: int | np.ndarray) -> np.ndarray:
        array = np.asarray(value)
        output = np.empty(
            (ragged_length,) + array.shape[1:], dtype=array.dtype,
        )
        output[...] = fill
        output[:len(array)] = array
        return output

    ragged_fill: Dict[str, int | np.ndarray] = {
        "uop_fields": V29_FIELD_PAD_IDS,
        "uop_valid_mask": 0,
        "uop_to_macro": -1,
        "uop_access": 0,
        "uop_semantic_flags": 0,
        "dynamic_uop_fields": 8,
    }
    for key in sorted(ragged_keys):
        result[key] = torch.from_numpy(np.stack([
            pad_ragged(window.model_inputs[key], ragged_fill[key])
            for window in flat
        ]))
    active_input_keys = (
        COMMON_MODEL_INPUT_KEYS
        | (
            NATIVE_MODEL_INPUT_KEYS
            if input_mode == "native_token"
            else SEMANTIC_MODEL_INPUT_KEYS
            if input_mode == "cached_macro_soft_token"
            else NULL_MODEL_INPUT_KEYS
        )
    )
    fixed_model_keys = sorted(
        active_input_keys
        - {"input_ids", "attention_mask", "token_to_macro"}
        - SEMANTIC_MODEL_INPUT_KEYS
        - ragged_keys
    )
    for key in fixed_model_keys:
        result[key] = torch.from_numpy(np.stack([
            np.asarray(window.model_inputs[key]) for window in flat
        ]))
    label_keys = sorted(flat[0].labels)
    if any(set(window.labels) != set(label_keys) for window in flat):
        raise MacroContractError("label keys differ across contexts")
    for key in label_keys:
        result[key] = torch.from_numpy(np.stack([
            np.asarray(window.labels[key]) for window in flat
        ]))
    result.update({
        "sample_ptr": torch.tensor(sample_ptr, dtype=torch.long),
        "core_slots": torch.tensor(core_slots, dtype=torch.long),
        "meta": metadata,
    })
    return result


def collate_macro_sequences(
    items: Sequence[Mapping[str, Any]],
    *,
    pad_token_id: int,
) -> Dict[str, Any]:
    """Collate contiguous sequences while retaining context and row grouping."""

    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("torch is required for collation") from exc
    if not items:
        raise MacroContractError("cannot collate an empty sequence batch")
    contexts: List[Sequence[MacroWindow]] = []
    sequence_ptr = [0]
    row_sequence: List[int] = []
    row_sequence_step: List[int] = []
    sample_indices: List[int] = []
    sample_ticks: List[int] = []
    sample_block_ids: List[int] = []
    trace_ids: List[str] = []
    period = float(items[0]["sample_period_cycles"])
    horizons = tuple(float(value) for value in items[0]["horizons"])
    for sequence_index, item in enumerate(items):
        item_contexts = list(item["contexts"])
        indices = tuple(int(value) for value in item["sample_indices"])
        ticks = tuple(int(value) for value in item["sample_ticks"])
        if not item_contexts or len(item_contexts) != len(indices) or len(ticks) != len(indices):
            raise MacroContractError("sequence context/index/tick lengths differ")
        if any(right != left + 1 for left, right in zip(indices, indices[1:])):
            raise MacroContractError("sequence sample indices are not contiguous")
        if float(item["sample_period_cycles"]) != period:
            raise MacroContractError("sample periods differ inside one batch")
        if tuple(float(value) for value in item["horizons"]) != horizons:
            raise MacroContractError("horizons differ inside one batch")
        block_id = int(item["sample_block_id"])
        for step, context in enumerate(item_contexts):
            if not context:
                raise MacroContractError("sequence contains an empty context")
            contexts.append(context)
            row_sequence.extend([sequence_index] * len(context))
            row_sequence_step.extend([step] * len(context))
            sample_indices.append(indices[step])
            sample_ticks.append(ticks[step])
            sample_block_ids.append(block_id)
        sequence_ptr.append(len(contexts))
        trace_ids.append(str(item["trace_id"]))
    result = collate_macro_contexts(contexts, pad_token_id=pad_token_id)
    if len(row_sequence) != int(result["sample_ptr"][-1]):
        raise MacroContractError("sequence row metadata does not match collated rows")
    result.update({
        "sequence_ptr": torch.tensor(sequence_ptr, dtype=torch.long),
        "row_sequence": torch.tensor(row_sequence, dtype=torch.long),
        "row_sequence_step": torch.tensor(row_sequence_step, dtype=torch.long),
        "sample_indices": torch.tensor(sample_indices, dtype=torch.long),
        "sample_ticks": torch.tensor(sample_ticks, dtype=torch.long),
        "sample_block_ids": torch.tensor(sample_block_ids, dtype=torch.long),
        "trace_id": trace_ids,
        "sample_period_cycles": period,
        "horizons": torch.tensor(horizons, dtype=torch.float32),
    })
    return result
