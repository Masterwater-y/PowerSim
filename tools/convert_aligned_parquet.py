#!/usr/bin/env python3
"""Convert gem5/TaoTrace aligned Parquet into FastSim's canonical trace.

Only functional columns are read.  Timing/cache/coherence oracle columns are
deliberately ignored.  The output is one little-endian `.fst` file per input
core plus a manifest directly consumable by `fastsim simulate`.
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
from pathlib import Path
from typing import Iterable, List

try:
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
except ImportError as error:
    raise SystemExit(
        "pyarrow and numpy are required; run this tool in the same "
        "environment used to build TCSim datasets"
    ) from error


MAGIC = b"FSTRC01\0"
HEADER = struct.Struct("<8sIIIIQQQQQQ")
RECORD = struct.Struct("<QQQQ4IHHhBB4BI")
VERSION = 6
HEADER_SIZE = HEADER.size
RECORD_SIZE = RECORD.size
RECORD_DTYPE = np.dtype(
    [
        ("pc", "<u8"),
        ("address", "<u8"),
        ("target", "<u8"),
        ("next_pc", "<u8"),
        ("producer_dists", "<u4", (4,)),
        ("size", "<u2"),
        ("flags", "<u2"),
        ("op_class", "<i2"),
        ("n_src", "u1"),
        ("n_dst", "u1"),
        ("producer_classes", "u1", (4,)),
        ("reserved", "<u4"),
    ],
    align=False,
)
if RECORD_DTYPE.itemsize != RECORD_SIZE:
    raise RuntimeError("numpy and struct canonical record layouts disagree")

RETIRE = 1 << 0
LOAD = 1 << 1
STORE = 1 << 2
ATOMIC = 1 << 3
BRANCH = 1 << 4
CONDITIONAL = 1 << 5
INDIRECT = 1 << 6
CALL = 1 << 7
RETURN = 1 << 8
TAKEN = 1 << 9
MICRO_OP = 1 << 10
LAST_MICRO_OP = 1 << 11
PHYSICAL_ADDRESS = 1 << 12
SERIALIZE = 1 << 13
BRANCH_OUTCOME_VALID = 1 << 14
VIRTUAL_PAGE_TOKEN = 1 << 15
FEATURE_VIRTUAL_PAGE_TOKENS = 1 << 0
FEATURE_SYSCALL_MARKERS = 1 << 1
FEATURE_DESTINATION_CLASS_COUNTS = 1 << 2
DESTINATION_CLASS_COUNTS_MARKER = 1 << 31
SYSCALL_OP_CLASS = -1

REQUIRED = {
    "core_id",
    "macro_pc",
    "paddr",
    "size",
    "is_load",
    "is_store",
    "is_atomic",
    "is_branch",
    "is_branch_cond",
    "is_branch_indirect",
    "is_call",
    "is_return",
    "is_microop",
    "is_last_microop",
    "is_serialize",
}
VIRTUAL_ADDRESS = {"vaddr"}
BRANCH_OUTCOMES = {"branch_taken", "branch_target", "branch_next_pc"}
CORE_TIMING = {
    "op_class",
    "n_src",
    "n_dst",
    "producer_dists",
    "producer_classes",
}
SYSCALL_FIELDS = {"is_syscall", "instr_type"}
DESTINATION_CLASSES = {"destination_class_counts"}


def discover_inputs(patterns: Iterable[str]) -> List[str]:
    result: List[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            result.extend(matches)
        elif os.path.isfile(pattern):
            result.append(pattern)
        else:
            raise SystemExit(f"input pattern matched no files: {pattern}")
    if not result:
        raise SystemExit("no input parquet files")
    return result


def numeric_column(batch, name: str, dtype, default: int = 0):
    index = batch.schema.get_field_index(name)
    if index < 0:
        return np.full(batch.num_rows, default, dtype=dtype)
    column = batch.column(index)
    if column.null_count:
        column = pc.fill_null(column, default)
    return column.to_numpy(zero_copy_only=False).astype(dtype, copy=False)


def valid_column(batch, name: str):
    index = batch.schema.get_field_index(name)
    if index < 0:
        return np.zeros(batch.num_rows, dtype=np.bool_)
    return pc.is_valid(batch.column(index)).to_numpy(
        zero_copy_only=False
    ).astype(np.bool_, copy=False)


def fixed_list_column(batch, name: str, dtype, width: int, default: int):
    index = batch.schema.get_field_index(name)
    if index < 0:
        return np.full((batch.num_rows, width), default, dtype=dtype)
    column = batch.column(index)
    if column.null_count:
        raise SystemExit(f"required list field {name} contains null rows")
    values = column.values
    if values.null_count:
        values = pc.fill_null(values, default)
    result = values.to_numpy(zero_copy_only=False).astype(dtype, copy=False)
    if result.size != batch.num_rows * width:
        raise SystemExit(f"{name} is not a fixed-size list[{width}]")
    return result.reshape(batch.num_rows, width)


def write_header(
    output, core_id: int, count: int, feature_flags: int = 0
) -> None:
    output.seek(0)
    output.write(
        HEADER.pack(
            MAGIC,
            VERSION,
            HEADER_SIZE,
            RECORD_SIZE,
            core_id,
            count,
            feature_flags,
            0,
            0,
            0,
            0,
        )
    )


def convert_file(
    input_path: str,
    output_path: str,
    allow_missing_branch_outcomes: bool,
    allow_missing_core_features: bool,
    allow_missing_virtual_addresses: bool,
    page_size_bits: int,
) -> tuple[int, int, bool, int]:
    parquet = pq.ParquetFile(input_path)
    names = set(parquet.schema_arrow.names)
    missing = sorted(REQUIRED - names)
    if missing:
        raise SystemExit(f"{input_path}: missing functional columns: {missing}")
    if "vaddr" not in names and not allow_missing_virtual_addresses:
        raise SystemExit(
            f"{input_path}: missing functional column vaddr; virtual and "
            "physical addresses are both required for timing replay. Use "
            "--allow-missing-virtual-addresses only for legacy/cache-only "
            "experiments."
        )
    has_branch_columns = BRANCH_OUTCOMES <= names
    if not has_branch_columns and not allow_missing_branch_outcomes:
        raise SystemExit(
            f"{input_path}: branch_taken/branch_target/branch_next_pc are "
            "incomplete; refusing to fabricate branch PMU. Use "
            "--allow-missing-branch-outcomes only for cache-only experiments."
        )
    missing_core = sorted(CORE_TIMING - names)
    if missing_core and not allow_missing_core_features:
        raise SystemExit(
            f"{input_path}: missing interval-core functional fields: "
            f"{missing_core}; use --allow-missing-core-features only for "
            "scalar/legacy replay"
        )
    has_destination_classes = "destination_class_counts" in names

    selected = sorted(
        REQUIRED
        | (VIRTUAL_ADDRESS & names)
        | (BRANCH_OUTCOMES & names)
        | (CORE_TIMING & names)
        | (SYSCALL_FIELDS & names)
        | (DESTINATION_CLASSES & names)
    )
    count = 0
    core_id = -1
    branch_ready = has_branch_columns
    page_bytes = 1 << page_size_bits
    page_mask = page_bytes - 1
    virtual_page_tokens: dict[tuple[int, int], int] = {}
    next_virtual_page_token = 1
    has_syscall_markers = False
    with open(output_path, "w+b") as output:
        write_header(output, 0, 0)
        for batch in parquet.iter_batches(batch_size=131_072, columns=selected):
            rows = batch.num_rows
            for name in REQUIRED - {"paddr"}:
                missing_value = ~valid_column(batch, name)
                if np.any(missing_value):
                    row = count + int(np.flatnonzero(missing_value)[0])
                    raise SystemExit(
                        f"{input_path}: required field {name} is null at "
                        f"row {row}"
                    )
            row_cores = numeric_column(batch, "core_id", np.uint32)
            unique_cores = np.unique(row_cores)
            if unique_cores.size != 1:
                raise SystemExit(
                    f"{input_path}: multiple core IDs in one input file"
                )
            row_core = int(unique_cores[0])
            if core_id < 0:
                core_id = row_core
            elif row_core != core_id:
                raise SystemExit(
                    f"{input_path}: multiple core IDs in one input file"
                )

            encoded = np.zeros(rows, dtype=RECORD_DTYPE)
            encoded["producer_classes"] = 255
            encoded["pc"] = numeric_column(batch, "macro_pc", np.uint64)
            is_load = numeric_column(batch, "is_load", np.bool_)
            is_store = numeric_column(batch, "is_store", np.bool_)
            is_atomic = numeric_column(batch, "is_atomic", np.bool_)
            is_memory = is_load | is_store | is_atomic
            paddr_valid = valid_column(batch, "paddr")
            missing_paddr = is_memory & ~paddr_valid
            if np.any(missing_paddr):
                row = count + int(np.flatnonzero(missing_paddr)[0])
                raise SystemExit(
                    f"{input_path}: memory row {row} has null paddr"
                )
            encoded["address"] = numeric_column(batch, "paddr", np.uint64)
            vaddr_valid = valid_column(batch, "vaddr")
            missing_vaddr = is_memory & ~vaddr_valid
            if np.any(missing_vaddr) and not allow_missing_virtual_addresses:
                row = count + int(np.flatnonzero(missing_vaddr)[0])
                raise SystemExit(
                    f"{input_path}: memory row {row} has null vaddr"
                )
            virtual_address = numeric_column(batch, "vaddr", np.uint64)
            encoded["target"] = numeric_column(
                batch, "branch_target", np.uint64
            )
            encoded["next_pc"] = numeric_column(
                batch, "branch_next_pc", np.uint64
            )
            sizes = numeric_column(batch, "size", np.uint64)
            invalid_size = is_memory & ((sizes == 0) | (sizes > 0xFFFF))
            if np.any(invalid_size):
                row = count + int(np.flatnonzero(invalid_size)[0])
                raise SystemExit(
                    f"{input_path}: memory row {row} has invalid size"
                )
            encoded["size"] = np.minimum(sizes, 0xFFFF).astype(np.uint16)
            token_valid = is_memory & vaddr_valid & paddr_valid
            offset_mismatch = token_valid & (
                (virtual_address & page_mask)
                != (encoded["address"] & page_mask)
            )
            if np.any(offset_mismatch):
                row = count + int(np.flatnonzero(offset_mismatch)[0])
                raise SystemExit(
                    f"{input_path}: memory row {row} has mismatched "
                    "vaddr/paddr page offsets"
                )
            cross_page = token_valid & (
                (virtual_address & page_mask) + sizes > page_bytes
            )
            if np.any(cross_page):
                row = count + int(np.flatnonzero(cross_page)[0])
                raise SystemExit(
                    f"{input_path}: memory row {row} crosses a virtual page; "
                    "one canonical record cannot represent two translations"
                )
            token_rows = np.flatnonzero(token_valid)
            if token_rows.size:
                pairs = np.column_stack(
                    (
                        virtual_address[token_rows] >> page_size_bits,
                        encoded["address"][token_rows] >> page_size_bits,
                    )
                )
                unique_pairs, inverse = np.unique(
                    pairs, axis=0, return_inverse=True
                )
                batch_tokens = np.empty(
                    unique_pairs.shape[0], dtype=np.uint32
                )
                for pair_index, pair in enumerate(unique_pairs):
                    identity = (int(pair[0]), int(pair[1]))
                    token = virtual_page_tokens.get(identity)
                    if token is None:
                        if next_virtual_page_token >= DESTINATION_CLASS_COUNTS_MARKER:
                            raise SystemExit(
                                f"{input_path}: more than 31-bit virtual-page "
                                "identities"
                            )
                        token = next_virtual_page_token
                        next_virtual_page_token += 1
                        virtual_page_tokens[identity] = token
                    batch_tokens[pair_index] = token
                encoded["reserved"][token_rows] = batch_tokens[inverse]
            encoded["op_class"] = numeric_column(
                batch, "op_class", np.int16
            )
            is_syscall = numeric_column(batch, "is_syscall", np.bool_)
            if "instr_type" in names:
                # TaoTrace::InstrType::SYS is seven.  The explicit boolean is
                # the portable contract; this keeps existing captures usable.
                is_syscall |= (
                    numeric_column(batch, "instr_type", np.uint8) == 7
                )
            if np.any(is_syscall):
                encoded["op_class"][is_syscall] = SYSCALL_OP_CLASS
                has_syscall_markers = True
            encoded["n_src"] = numeric_column(batch, "n_src", np.uint8)
            encoded["n_dst"] = numeric_column(batch, "n_dst", np.uint8)
            encoded["producer_dists"] = fixed_list_column(
                batch, "producer_dists", np.uint32, 4, 0
            )
            producer_classes = fixed_list_column(
                batch, "producer_classes", np.uint8, 4, 255
            )
            if has_destination_classes:
                destination_classes = fixed_list_column(
                    batch, "destination_class_counts", np.uint8, 4, 0
                )
                invalid_producer = (producer_classes > 3) & (
                    producer_classes != 255
                )
                if np.any(invalid_producer):
                    row = count + int(np.argwhere(invalid_producer)[0][0])
                    raise SystemExit(
                        f"{input_path}: invalid producer register class at row {row}"
                    )
                if np.any(destination_classes > 31):
                    row = count + int(
                        np.argwhere(destination_classes > 31)[0][0]
                    )
                    raise SystemExit(
                        f"{input_path}: destination class count exceeds 31 at row {row}"
                    )
                classified = destination_classes.astype(np.uint16).sum(axis=1)
                mismatched = classified != encoded["n_dst"]
                if np.any(mismatched):
                    row = count + int(np.flatnonzero(mismatched)[0])
                    raise SystemExit(
                        f"{input_path}: destination class counts do not sum to n_dst at row {row}"
                    )
                packed_producers = np.where(
                    producer_classes == 255, 7, producer_classes
                ).astype(np.uint8)
                encoded["producer_classes"] = (
                    (destination_classes << 3) | packed_producers
                )
                encoded["reserved"] |= np.uint32(
                    DESTINATION_CLASS_COUNTS_MARKER
                )
            else:
                encoded["producer_classes"] = producer_classes

            flags = np.full(rows, RETIRE, dtype=np.uint16)
            flags[paddr_valid] |= PHYSICAL_ADDRESS
            flags[token_valid] |= VIRTUAL_PAGE_TOKEN
            flag_columns = (
                ("is_branch", BRANCH),
                ("is_branch_cond", CONDITIONAL),
                ("is_branch_indirect", INDIRECT),
                ("is_call", CALL),
                ("is_return", RETURN),
                ("branch_taken", TAKEN),
                ("is_microop", MICRO_OP),
                ("is_last_microop", LAST_MICRO_OP),
                ("is_serialize", SERIALIZE),
            )
            flags[is_load] |= LOAD
            flags[is_store] |= STORE
            flags[is_atomic] |= ATOMIC
            flags[is_syscall] |= SERIALIZE
            for name, bit in flag_columns:
                enabled = numeric_column(batch, name, np.bool_)
                flags[enabled] |= bit
            is_branch = numeric_column(batch, "is_branch", np.bool_)
            if has_branch_columns:
                outcome_valid = np.ones(rows, dtype=np.bool_)
                for name in BRANCH_OUTCOMES:
                    outcome_valid &= valid_column(batch, name)
                incomplete = is_branch & ~outcome_valid
                if np.any(incomplete):
                    branch_ready = False
                    if not allow_missing_branch_outcomes:
                        row = count + int(np.flatnonzero(incomplete)[0])
                        raise SystemExit(
                            f"{input_path}: branch row {row} has a null "
                            "committed outcome field"
                        )
                flags[is_branch & outcome_valid] |= BRANCH_OUTCOME_VALID
            encoded["flags"] = flags
            output.write(encoded.tobytes())
            count += rows
        if core_id < 0:
            raise SystemExit(f"{input_path}: empty trace")
        feature_flags = (
            FEATURE_VIRTUAL_PAGE_TOKENS if virtual_page_tokens else 0
        )
        if has_syscall_markers:
            feature_flags |= FEATURE_SYSCALL_MARKERS
        if has_destination_classes:
            feature_flags |= FEATURE_DESTINATION_CLASS_COUNTS
        write_header(output, core_id, count, feature_flags)
    return core_id, count, branch_ready, len(virtual_page_tokens)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Input path or glob; repeat for multiple globs",
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest", default="manifest.txt")
    parser.add_argument(
        "--allow-missing-branch-outcomes",
        action="store_true",
        help="Permit legacy traces for cache-only experiments",
    )
    parser.add_argument(
        "--allow-missing-core-features",
        action="store_true",
        help="Permit scalar-only legacy traces without dependency/op-class fields",
    )
    parser.add_argument(
        "--allow-missing-virtual-addresses",
        action="store_true",
        help="Permit legacy/cache-only traces without vaddr page identities",
    )
    parser.add_argument(
        "--page-size-bits",
        type=int,
        default=12,
        help="Base-page offset width used by virtual-page tokens (default: 12)",
    )
    args = parser.parse_args()
    if not 9 <= args.page_size_bits <= 30:
        raise SystemExit("--page-size-bits must be in [9, 30]")

    inputs = discover_inputs(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for input_path in inputs:
        stem = Path(input_path).stem
        temporary = out_dir / f".{stem}.tmp"
        core_id, count, branch_ready, virtual_pages = convert_file(
            input_path,
            str(temporary),
            args.allow_missing_branch_outcomes,
            args.allow_missing_core_features,
            args.allow_missing_virtual_addresses,
            args.page_size_bits,
        )
        output = out_dir / f"core{core_id}.fst"
        if output.exists():
            raise SystemExit(f"duplicate core {core_id}: {output}")
        temporary.replace(output)
        entries.append((core_id, output.name, count, branch_ready))
        print(
            f"core={core_id} records={count} branch_ready={branch_ready} "
            f"virtual_pages={virtual_pages} "
            f"output={output}"
        )

    entries.sort()
    expected = list(range(len(entries)))
    actual = [entry[0] for entry in entries]
    if actual != expected:
        raise SystemExit(f"core IDs are not dense: {actual}")
    manifest_path = out_dir / args.manifest
    with manifest_path.open("w") as manifest:
        manifest.write("# <core-id> <format> <path>\n")
        for core_id, filename, _, _ in entries:
            manifest.write(f"{core_id} fastsim-binary {filename}\n")
    print(f"manifest={manifest_path}")


if __name__ == "__main__":
    main()
