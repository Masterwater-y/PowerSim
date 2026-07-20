#!/usr/bin/env python3
"""Full-suite structural and native-token audit for the macro v29 contract.

The token p100 gate uses a conservative per-instruction upper bound over every
branch label form that ``ParquetInstructionResolver`` can emit.  Sliding sums
are then evaluated at every possible 256-macro cursor, so a p100 below the
context limit is a proof that the real renderer cannot overflow it.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.rebuild_v29_macro_static_suite import (  # noqa: E402
    discover_pc_arrays,
    normalized_binary_name,
)
from train.macro_v29_dataset import (  # noqa: E402
    DEFAULT_K_MACRO,
    MacroContractError,
    ParquetInstructionResolver,
    _BRANCH_TARGET_RE,
)


def add_histogram(total: np.ndarray, values: np.ndarray) -> np.ndarray:
    if not values.size:
        return total
    observed = np.bincount(values.astype(np.int64, copy=False))
    if len(observed) > len(total):
        total = np.pad(total, (0, len(observed) - len(total)))
    total[:len(observed)] += observed.astype(np.int64)
    return total


def histogram_quantile(histogram: np.ndarray, fraction: float) -> int:
    count = int(histogram.sum())
    if count <= 0:
        return 0
    target = max(1, int(np.ceil(float(fraction) * count)))
    return int(np.searchsorted(np.cumsum(histogram), target, side="left"))


def summarize_histogram(histogram: np.ndarray) -> Dict[str, int]:
    nonzero = np.flatnonzero(histogram)
    return {
        "n": int(histogram.sum()),
        "p50": histogram_quantile(histogram, 0.50),
        "p90": histogram_quantile(histogram, 0.90),
        "p99": histogram_quantile(histogram, 0.99),
        "p100": int(nonzero[-1]) if len(nonzero) else 0,
    }


def load_static_paths(manifest_path: Path) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != "real-x86-objdump-wide-v2":
            raise MacroContractError(
                f"static manifest row is not v2: {row.get('binary_name')}"
            )
        if row.get("scan_scope") != "all_cores":
            raise MacroContractError(
                f"static row lacks all-core coverage: {row.get('binary_name')}"
            )
        result[str(row["binary_name"])] = Path(row["parquet"])
    return result


def tokenize_lengths(tokenizer: Any, texts: Sequence[str]) -> list[int]:
    lengths: list[int] = []
    chunk = 4096
    for begin in range(0, len(texts), chunk):
        encoded = tokenizer(
            list(texts[begin:begin + chunk]),
            add_special_tokens=False,
            truncation=False,
            padding=False,
        )
        lengths.extend(len(ids) for ids in encoded["input_ids"])
    return lengths


def conservative_pc_token_lengths(
    resolver: ParquetInstructionResolver,
    tokenizer: Any,
    dynamic_pcs: Iterable[int],
    *,
    k_macro: int,
) -> Dict[int, int]:
    labels = [".L_external"]
    labels.extend(f".L_back_{index}" for index in range(k_macro))
    labels.extend(f".L_fwd_{index}" for index in range(k_macro))
    result: Dict[int, int] = {}
    for pc in sorted({int(value) for value in dynamic_pcs}):
        row = resolver.rows.get(pc)
        if row is None or not bool(row["semantic_valid"]):
            raise MacroContractError(f"unresolved dynamic pc 0x{pc:x}")
        mnemonic = str(row["mnemonic"])
        operands = str(row["operands"])
        if bool(row["is_branch"]) and _BRANCH_TARGET_RE.search(operands):
            variants = [
                f"{mnemonic} {_BRANCH_TARGET_RE.sub(label, operands, count=1)}".rstrip()
                + "\n"
                for label in labels
            ]
        else:
            variants = [f"{mnemonic} {operands}".rstrip() + "\n"]
        result[pc] = max(tokenize_lengths(tokenizer, variants))
    return result


def core_structure(
    pc_path: Path,
    architectural_branch_pcs: np.ndarray,
) -> tuple[Dict[str, int], np.ndarray, np.ndarray]:
    core_dir = pc_path.parent
    macro_pc_uop = np.load(pc_path, mmap_mode="r")
    macro_end = np.load(core_dir / "macro_end.npy", mmap_mode="r")
    commit_tick = np.load(core_dir / "commit_tick.npy", mmap_mode="r")
    branch_miss = np.load(core_dir / "branch_miss.npy", mmap_mode="r")
    if not (
        macro_pc_uop.ndim == macro_end.ndim == commit_tick.ndim == 1
        and branch_miss.ndim == 1
        and len(macro_pc_uop) == len(macro_end) == len(commit_tick)
        == len(branch_miss)
    ):
        raise MacroContractError(f"core array shape mismatch under {core_dir}")
    ends = np.flatnonzero(np.asarray(macro_end, dtype=np.uint8)).astype(np.int64)
    if not len(ends) or int(ends[-1]) != len(macro_end) - 1:
        raise MacroContractError(f"trace does not end on macro boundary: {core_dir}")
    begins = np.concatenate((np.asarray([0], dtype=np.int64), ends[:-1] + 1))
    counts = ends - begins + 1
    if np.any(counts <= 0):
        raise MacroContractError(f"empty macro under {core_dir}")
    pc_values = np.asarray(macro_pc_uop[begins], dtype=np.uint64)
    # Validate that every UOP belonging to one macro retains the same macro PC.
    within = ~np.asarray(macro_end[:-1], dtype=np.bool_)
    if np.any(macro_pc_uop[1:][within] != macro_pc_uop[:-1][within]):
        raise MacroContractError(f"macro_pc changes inside a macro: {core_dir}")
    ticks = np.asarray(commit_tick, dtype=np.int64)
    if np.any(ticks[1:] < ticks[:-1]):
        raise MacroContractError(f"commit ticks decrease: {core_dir}")
    macro_ticks = ticks[ends]
    miss_counts = np.add.reduceat(
        np.asarray(branch_miss, dtype=np.int64), begins,
    )
    architectural_branch = np.isin(
        pc_values, architectural_branch_pcs, assume_unique=False,
    )
    groups = np.concatenate((
        np.asarray([0], dtype=np.int64),
        np.flatnonzero(macro_ticks[1:] != macro_ticks[:-1]) + 1,
        np.asarray([len(macro_ticks)], dtype=np.int64),
    ))
    report = {
        "uops": int(len(macro_end)),
        "macros": int(len(ends)),
        "max_uops_per_macro": int(counts.max()),
        "max_same_tick_macros": int(np.diff(groups).max()),
        "architectural_branch_macros": int(architectural_branch.sum()),
        "max_miss_markers_per_architectural_branch": int(
            miss_counts[architectural_branch].max()
            if np.any(architectural_branch) else 0
        ),
        "microcode_miss_markers_excluded_from_branch_labels": int(
            miss_counts[~architectural_branch].sum()
        ),
    }
    return report, counts.astype(np.int64, copy=False), pc_values


def sliding_token_sums(lengths: np.ndarray, k_macro: int) -> np.ndarray:
    if len(lengths) < k_macro:
        return np.empty(0, dtype=np.int64)
    prefix = np.empty(len(lengths) + 1, dtype=np.int64)
    prefix[0] = 0
    np.cumsum(lengths, dtype=np.int64, out=prefix[1:])
    return prefix[k_macro:] - prefix[:-k_macro]


def macro_pc_values(pc_path: Path) -> np.ndarray:
    macro_end = np.load(pc_path.parent / "macro_end.npy", mmap_mode="r")
    ends = np.flatnonzero(np.asarray(macro_end, dtype=np.uint8)).astype(np.int64)
    begins = np.concatenate((np.asarray([0], dtype=np.int64), ends[:-1] + 1))
    return np.asarray(np.load(pc_path, mmap_mode="r")[begins], dtype=np.uint64)


def merge_histogram(total: np.ndarray, addition: np.ndarray) -> np.ndarray:
    if len(addition) > len(total):
        total = np.pad(total, (0, len(addition) - len(total)))
    total[:len(addition)] += addition.astype(np.int64, copy=False)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--traces-root",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/traces",
    )
    parser.add_argument(
        "--static-manifest",
        default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict/manifest.jsonl",
    )
    parser.add_argument(
        "--tokenizer", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--k-macro", type=int, default=DEFAULT_K_MACRO)
    parser.add_argument("--stride-macro", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--core0-only", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    if not 0 < args.stride_macro <= args.k_macro:
        raise ValueError("stride_macro must be in (0,k_macro]")
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer, local_files_only=True,
    )
    tokenizer_size = len(tokenizer)
    discovered = discover_pc_arrays(
        Path(args.traces_root), core0_only=bool(args.core0_only),
    )
    selected = (
        {normalized_binary_name(value) for value in args.workload}
        if args.workload else set(discovered)
    )
    static_paths = load_static_paths(Path(args.static_manifest))
    missing = selected - set(static_paths)
    if missing:
        raise MacroContractError(f"missing static v2 dictionaries: {sorted(missing)}")

    global_uop_hist = np.zeros(1, dtype=np.int64)
    global_token_hist = np.zeros(1, dtype=np.int64)
    workload_reports = []
    failures = []
    global_same_tick = 0
    started = time.perf_counter()
    for binary_name in sorted(selected):
        begin = time.perf_counter()
        paths = discovered[binary_name]
        resolver = ParquetInstructionResolver(static_paths[binary_name])
        architectural_branch_pcs = np.asarray(sorted(
            pc for pc, row in resolver.rows.items()
            if bool(row["is_branch"])
        ), dtype=np.uint64)
        dynamic_pcs: set[int] = set()
        structures = []
        workload_uop_hist = np.zeros(1, dtype=np.int64)
        for pc_path in paths:
            structure, counts, pc_values = core_structure(
                pc_path, architectural_branch_pcs,
            )
            structures.append(structure)
            dynamic_pcs.update(int(value) for value in np.unique(pc_values))
            workload_uop_hist = add_histogram(workload_uop_hist, counts)
        upper = conservative_pc_token_lengths(
            resolver, tokenizer, dynamic_pcs, k_macro=int(args.k_macro),
        )
        ordered_pcs = np.asarray(sorted(upper), dtype=np.uint64)
        ordered_lengths = np.asarray(
            [upper[int(pc)] for pc in ordered_pcs], dtype=np.int64,
        )
        workload_token_hist = np.zeros(1, dtype=np.int64)
        for pc_path in paths:
            pc_values = macro_pc_values(pc_path)
            positions = np.searchsorted(ordered_pcs, pc_values)
            if np.any(positions >= len(ordered_pcs)):
                raise MacroContractError(f"token length mapping failed for {binary_name}")
            if np.any(ordered_pcs[positions] != pc_values):
                raise MacroContractError(f"token length mapping failed for {binary_name}")
            window_sums = sliding_token_sums(
                ordered_lengths[positions], int(args.k_macro),
            )
            workload_token_hist = add_histogram(
                workload_token_hist, window_sums,
            )
        uop_summary = summarize_histogram(workload_uop_hist)
        token_summary = summarize_histogram(workload_token_hist)
        max_same_tick = max(
            int(value["max_same_tick_macros"]) for value in structures
        )
        max_uops = max(int(value["max_uops_per_macro"]) for value in structures)
        max_arch_miss = max(
            int(value["max_miss_markers_per_architectural_branch"])
            for value in structures
        )
        if max_arch_miss > 1:
            failures.append(
                f"{binary_name}: architectural branch has {max_arch_miss} "
                "branch-miss markers"
            )
        tie_guard = int(args.k_macro) - int(args.stride_macro)
        if tie_guard > 0 and max_same_tick > tie_guard:
            failures.append(
                f"{binary_name}: same-tick {max_same_tick} > tie guard {tie_guard}"
            )
        if token_summary["p100"] > int(args.max_tokens):
            failures.append(
                f"{binary_name}: conservative token p100 "
                f"{token_summary['p100']} > {args.max_tokens}"
            )
        report_row = {
            "binary_name": binary_name,
            "core_arrays": len(paths),
            "dynamic_uops": int(sum(value["uops"] for value in structures)),
            "dynamic_macros": int(sum(value["macros"] for value in structures)),
            "unique_dynamic_pcs": len(dynamic_pcs),
            "uops_per_macro": uop_summary,
            "max_same_tick_macros": max_same_tick,
            "architectural_branch_macros": int(sum(
                value["architectural_branch_macros"] for value in structures
            )),
            "max_miss_markers_per_architectural_branch": max_arch_miss,
            "microcode_miss_markers_excluded_from_branch_labels": int(sum(
                value["microcode_miss_markers_excluded_from_branch_labels"]
                for value in structures
            )),
            "conservative_native_tokens_per_256_macro": token_summary,
            "elapsed_s": time.perf_counter() - begin,
        }
        workload_reports.append(report_row)
        global_uop_hist = merge_histogram(global_uop_hist, workload_uop_hist)
        global_token_hist = merge_histogram(
            global_token_hist, workload_token_hist,
        )
        global_same_tick = max(global_same_tick, max_same_tick)
        print(
            f"[macro suite] {binary_name}: cores={len(paths)} "
            f"macros={report_row['dynamic_macros']} uop_max={max_uops} "
            f"tie_max={max_same_tick} token_p99={token_summary['p99']} "
            f"token_p100={token_summary['p100']} "
            f"dt={report_row['elapsed_s']:.1f}s",
            flush=True,
        )
    if len(tokenizer) != tokenizer_size:
        failures.append("tokenizer vocabulary changed during suite audit")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "kind": "all-sliding-windows-conservative-native-token-bound",
        "scan_scope": "core0_only" if args.core0_only else "all_cores",
        "tokenizer": args.tokenizer,
        "tokenizer_size": tokenizer_size,
        "k_macro": int(args.k_macro),
        "stride_macro": int(args.stride_macro),
        "tie_guard_macro": int(args.k_macro) - int(args.stride_macro),
        "boundary_policy": "allow_zero_cycle_replan",
        "uop_representation": "ragged-segment-no-truncation",
        "max_tokens": int(args.max_tokens),
        "n_workloads": len(workload_reports),
        "global": {
            "dynamic_uops": int(sum(row["dynamic_uops"] for row in workload_reports)),
            "dynamic_macros": int(sum(row["dynamic_macros"] for row in workload_reports)),
            "uops_per_macro": summarize_histogram(global_uop_hist),
            "max_same_tick_macros": global_same_tick,
            "conservative_native_tokens_per_256_macro": summarize_histogram(
                global_token_hist,
            ),
        },
        "workloads": workload_reports,
        "failures": failures,
        "elapsed_s": time.perf_counter() - started,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(rendered + "\n")
        os.replace(temporary, destination)
    print(rendered)
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
