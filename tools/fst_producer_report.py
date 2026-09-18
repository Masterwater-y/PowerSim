"""Emit an agent-readable functional-feature report for two FST producers."""
from __future__ import annotations

import argparse
import json
import struct
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from tools.audit_fst_dependencies import (
    DEPENDENCIES,
    HEADER,
    ROW,
    record_hash,
)
from tools.fst_wire import (
    FEATURE_COMPLETE_DEPENDENCIES,
    FST_HEADER,
    FST_MAGIC,
    FST_RECORD,
    FST_VERSION,
)


RECORD_DTYPE = np.dtype(
    {
        "names": [
            "producer_dists", "size", "flags", "op_class", "n_src",
            "producer_classes",
        ],
        "formats": ["<u4", "<u2", "<u2", "<i2", "u1", "u1"],
        "offsets": [32, 48, 50, 52, 54, 56],
        "itemsize": FST_RECORD.size,
    }
)
CHUNK_RECORDS = 1 << 20

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
SERIALIZE = 1 << 13
DESTINATION_CLASS_COUNTS_MARKER = 1 << 31
DISTANCE_BUCKETS = (
    "1", "2", "3-4", "5-8", "9-16", "17-32", "33-64", "65-128",
    "129-256", "257-512", "513-1024", "1025-2048", "2049-4096",
    "4097-8192", "8193-16384", "16385-32768", "32769-65536",
    "65537-131072", "131073-262144", "262145-524288",
    "524289-1048576", "1048577-2097152", "2097153-4194304",
    "4194305-8388608", "8388609-16777216", "16777217-33554432",
    "33554433-67108864", "67108865-134217728",
    "134217729-268435456", "268435457-536870912",
    "536870913-1073741824", "1073741825-2147483648",
    "2147483649-4294967295",
)


def _boundaries(directory: Path) -> dict[str, dict[str, Any]]:
    path = directory / "boundaries.json"
    if path.is_file():
        rows = json.loads(path.read_text(encoding="utf-8")).get("cores")
        if isinstance(rows, dict):
            return rows
        raise ValueError(f"invalid boundaries document: {path}")
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("functional-boundary-core*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        core = int(row["core_id"])
        if str(core) in rows:
            raise ValueError(f"duplicate boundary for core {core}: {directory}")
        rows[str(core)] = row
    if not rows:
        raise FileNotFoundError(f"no FST boundaries under {directory}")
    return rows


def _header(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"short FST header: {path}")
    values = FST_HEADER.unpack(raw)
    magic, version, header_size, record_size, core, records, features = values[:7]
    if (
        magic != FST_MAGIC
        or version != FST_VERSION
        or header_size != FST_HEADER.size
        or record_size != FST_RECORD.size
    ):
        raise ValueError(f"not canonical FST v7: {path}")
    if not features & FEATURE_COMPLETE_DEPENDENCIES:
        raise ValueError(f"complete dependencies are required: {path}")
    return int(core), int(records)


def _range(row: dict[str, Any], records: int) -> tuple[int, int, int]:
    warmup = int(row.get("warmup_records", -1))
    measurement = int(row.get("measurement_records", -1))
    expected_user = int(row.get("measurement_user_records", -1))
    if warmup < 0 or measurement < 0 or expected_user <= 0:
        raise ValueError(f"invalid FST boundary: {row}")
    end = warmup + measurement
    if end > records:
        raise ValueError(f"boundary exceeds FST records: {row}")
    return warmup, end, expected_user


def _sparse(values: Counter[int]) -> dict[str, int]:
    return {str(key): int(values[key]) for key in sorted(values) if values[key]}


def _density(value: int, macros: int) -> float | None:
    return None if macros == 0 else value / macros


def _empty_counts() -> dict[str, Any]:
    return {
        "cpl3_uops": 0,
        "cpl0_uops": 0,
        "macros": 0,
        "op_class": Counter(),
        "memory": {"load": 0, "store": 0, "atomic": 0, "sizes": Counter()},
        "branch": {
            "conditional": 0, "indirect": 0, "call": 0, "return": 0,
            "taken": 0,
        },
        "serialize_uops": 0,
        "syscall_markers": 0,
        "dependencies": {
            "inline": 0, "extension": 0, "producer_classes": Counter(),
            "distance_buckets": [0] * len(DISTANCE_BUCKETS),
        },
    }


def _add_distances(
    buckets: list[int], distances: np.ndarray,
) -> None:
    if not len(distances):
        return
    indexes = np.floor(np.log2(distances)).astype(np.intp)
    indexes = np.minimum(indexes, len(buckets) - 1)
    for index, count in enumerate(np.bincount(indexes, minlength=len(buckets))):
        buckets[index] += int(count)


def _validate_extensions_and_summarize(
    path: Path, start: int, end: int, counts: dict[str, Any],
) -> None:
    companion = Path(str(path) + ".deps")
    with path.open("rb") as source, companion.open("rb") as deps:
        header = HEADER.unpack(source.read(HEADER.size))
        _, _, _, _, core, records, features = header[:7]
        if not features & FEATURE_COMPLETE_DEPENDENCIES:
            raise ValueError(f"complete dependencies are not declared: {path}")
        raw_header = deps.read(DEPENDENCIES.size)
        if len(raw_header) != DEPENDENCIES.size:
            raise ValueError(f"dependency table size/count mismatch: {path}")
        values = DEPENDENCIES.unpack(raw_header)
        magic, version, header_size, row_core, flags, row_records, rows, extras = values
        if (
            magic, version, header_size, row_core, flags, row_records
        ) != (b"FSTDEP1\0", 1, DEPENDENCIES.size, core, 0, records):
            raise ValueError(f"dependency header identity mismatch: {path}")
        expected_size = DEPENDENCIES.size + ROW.size * rows + 4 * extras
        if companion.stat().st_size != expected_size:
            raise ValueError(f"dependency table size/count mismatch: {path}")
        previous = -1
        extras_seen = 0
        for _ in range(rows):
            raw_row = deps.read(ROW.size)
            if len(raw_row) != ROW.size:
                raise ValueError(f"dependency table size/count mismatch: {path}")
            ordinal, size, fingerprint = ROW.unpack(raw_row)
            if not previous < ordinal < records or not 1 <= size <= 251:
                raise ValueError(f"invalid dependency extension row: {path}")
            source.seek(FST_HEADER.size + ordinal * FST_RECORD.size)
            raw = source.read(FST_RECORD.size)
            if record_hash(raw) != fingerprint:
                raise ValueError(f"hot record/extension mismatch at {ordinal}")
            record = FST_RECORD.unpack(raw)
            inline = tuple(value for value in record[4:8] if value)
            if (
                not all(record[4:8])
                or inline != tuple(sorted(inline))
                or len(inline) + size > record[11]
            ):
                raise ValueError(f"invalid complete dependency row: {path}")
            raw_distances = deps.read(4 * size)
            if len(raw_distances) != 4 * size:
                raise ValueError(f"dependency table size/count mismatch: {path}")
            distances = np.frombuffer(raw_distances, dtype="<u4")
            if len(distances) != size or np.any(distances[:-1] >= distances[1:]):
                raise ValueError(f"invalid dependency extension order: {path}")
            if np.any(distances > ordinal):
                raise ValueError(f"dependency precedes trace at {ordinal}")
            if start <= ordinal < end:
                counts["dependencies"]["extension"] += int(size)
                _add_distances(counts["dependencies"]["distance_buckets"], distances)
                counts["dependencies"]["producer_classes"][255] += int(size)
            previous = int(ordinal)
            extras_seen += int(size)
        if extras_seen != extras or deps.read(1):
            raise ValueError(f"unconsumed dependency data: {path}")


def _summarize_core(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    core, records = _header(path)
    start, end, expected_user = _range(row, records)
    counts = _empty_counts()
    mapped = np.memmap(
        path, dtype=RECORD_DTYPE, mode="r", offset=FST_HEADER.size,
        shape=(records,),
    )
    for offset in range(start, end, CHUNK_RECORDS):
        rows = mapped[offset:min(offset + CHUNK_RECORDS, end)]
        op_class = rows["op_class"].astype(np.int32, copy=False)
        flags = rows["flags"].astype(np.uint32, copy=False)
        user = op_class >= -1
        kernel = ~user
        counts["cpl3_uops"] += int(user.sum())
        counts["cpl0_uops"] += int(kernel.sum())
        user_classes, user_counts = np.unique(
            op_class[user], return_counts=True,
        )
        counts["op_class"].update(
            {int(key): int(value) for key, value in zip(user_classes, user_counts)}
        )
        macro_complete = (
            ((flags & MICRO_OP) == 0) | ((flags & LAST_MICRO_OP) != 0)
        )
        counts["macros"] += int(np.count_nonzero(user & macro_complete))
        memory = user & ((flags & (LOAD | STORE | ATOMIC)) != 0)
        for name, flag in (("load", LOAD), ("store", STORE), ("atomic", ATOMIC)):
            counts["memory"][name] += int(np.count_nonzero(user & (flags & flag != 0)))
        sizes, size_counts = np.unique(rows["size"][memory], return_counts=True)
        counts["memory"]["sizes"].update(
            {int(key): int(value) for key, value in zip(sizes, size_counts)}
        )
        branch = user & ((flags & BRANCH) != 0)
        for name, flag in (
            ("conditional", CONDITIONAL), ("indirect", INDIRECT), ("call", CALL),
            ("return", RETURN), ("taken", TAKEN),
        ):
            counts["branch"][name] += int(np.count_nonzero(branch & (flags & flag != 0)))
        counts["serialize_uops"] += int(
            np.count_nonzero(user & ((flags & SERIALIZE) != 0))
        )
        counts["syscall_markers"] += int(np.count_nonzero(op_class == -1))
        inline = rows["producer_dists"][user].reshape(-1)
        inline = inline[inline != 0]
        counts["dependencies"]["inline"] += int(len(inline))
        _add_distances(counts["dependencies"]["distance_buckets"], inline)
        inline_distances = rows["producer_dists"][user]
        inline_classes = rows["producer_classes"][user]
        classes = inline_classes[inline_distances != 0] & 0x7
        classes = classes[classes != 7]
        class_keys, class_counts = np.unique(classes, return_counts=True)
        counts["dependencies"]["producer_classes"].update(
            {int(key): int(value) for key, value in zip(class_keys, class_counts)}
        )
    del mapped
    _validate_extensions_and_summarize(path, start, end, counts)
    if counts["cpl3_uops"] != expected_user:
        raise ValueError(f"boundary user UOP population disagrees with FST: {path}")
    macros = counts["macros"]
    dependencies = counts["dependencies"]
    return {
        "core": core,
        "measurement_records": end - start,
        "cpl3_uops": counts["cpl3_uops"],
        "cpl0_uops": counts["cpl0_uops"],
        "macros": macros,
        "uops_per_macro": _density(counts["cpl3_uops"], macros),
        "op_class": _sparse(counts["op_class"]),
        "memory": {
            **{key: counts["memory"][key] for key in ("load", "store", "atomic")},
            "sizes": _sparse(counts["memory"]["sizes"]),
            **{
                f"{key}_per_macro": _density(counts["memory"][key], macros)
                for key in ("load", "store", "atomic")
            },
        },
        "branch": {
            **counts["branch"],
            **{
                f"{key}_per_macro": _density(counts["branch"][key], macros)
                for key in counts["branch"]
            },
        },
        "serialize_uops": counts["serialize_uops"],
        "serialize_per_macro": _density(counts["serialize_uops"], macros),
        "syscall_markers": counts["syscall_markers"],
        "syscalls_per_macro": _density(counts["syscall_markers"], macros),
        "dependencies": {
            "inline": dependencies["inline"],
            "extension": dependencies["extension"],
            "total": dependencies["inline"] + dependencies["extension"],
            "per_uop": _density(
                dependencies["inline"] + dependencies["extension"],
                counts["cpl3_uops"],
            ),
            "producer_classes": _sparse(dependencies["producer_classes"]),
            "distance_buckets": dict(
                zip(DISTANCE_BUCKETS, dependencies["distance_buckets"])
            ),
        },
    }


def _merge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged = _empty_counts()
    for row in rows:
        merged["cpl3_uops"] += row["cpl3_uops"]
        merged["cpl0_uops"] += row["cpl0_uops"]
        merged["macros"] += row["macros"]
        merged["op_class"].update({int(k): v for k, v in row["op_class"].items()})
        for key in ("load", "store", "atomic"):
            merged["memory"][key] += row["memory"][key]
        merged["memory"]["sizes"].update(
            {int(k): v for k, v in row["memory"]["sizes"].items()}
        )
        for key in merged["branch"]:
            merged["branch"][key] += row["branch"][key]
        merged["serialize_uops"] += row["serialize_uops"]
        merged["syscall_markers"] += row["syscall_markers"]
        for key in ("inline", "extension"):
            merged["dependencies"][key] += row["dependencies"][key]
        merged["dependencies"]["producer_classes"].update(
            {int(k): v for k, v in row["dependencies"]["producer_classes"].items()}
        )
        for index, key in enumerate(DISTANCE_BUCKETS):
            merged["dependencies"]["distance_buckets"][index] += (
                row["dependencies"]["distance_buckets"][key]
            )
    macros = merged["macros"]
    total_dependencies = (
        merged["dependencies"]["inline"] + merged["dependencies"]["extension"]
    )
    return {
        "measurement_records": sum(row["measurement_records"] for row in rows),
        "cpl3_uops": merged["cpl3_uops"],
        "cpl0_uops": merged["cpl0_uops"],
        "macros": macros,
        "uops_per_macro": _density(merged["cpl3_uops"], macros),
        "op_class": _sparse(merged["op_class"]),
        "memory": {
            **{key: merged["memory"][key] for key in ("load", "store", "atomic")},
            "sizes": _sparse(merged["memory"]["sizes"]),
            **{
                f"{key}_per_macro": _density(merged["memory"][key], macros)
                for key in ("load", "store", "atomic")
            },
        },
        "branch": {
            **merged["branch"],
            **{
                f"{key}_per_macro": _density(merged["branch"][key], macros)
                for key in merged["branch"]
            },
        },
        "serialize_uops": merged["serialize_uops"],
        "serialize_per_macro": _density(merged["serialize_uops"], macros),
        "syscall_markers": merged["syscall_markers"],
        "syscalls_per_macro": _density(merged["syscall_markers"], macros),
        "dependencies": {
            "inline": merged["dependencies"]["inline"],
            "extension": merged["dependencies"]["extension"],
            "total": total_dependencies,
            "per_uop": _density(total_dependencies, merged["cpl3_uops"]),
            "producer_classes": _sparse(merged["dependencies"]["producer_classes"]),
            "distance_buckets": dict(
                zip(DISTANCE_BUCKETS, merged["dependencies"]["distance_buckets"])
            ),
        },
    }


def _delta(gem5: Any, qemu: Any) -> Any:
    if isinstance(gem5, dict):
        return {
            key: _delta(gem5.get(key, 0), qemu.get(key, 0))
            for key in sorted(set(gem5) | set(qemu))
        }
    difference = qemu - gem5
    return {
        "gem5": gem5,
        "qemu": qemu,
        "qemu_minus_gem5": difference,
        "relative_to_gem5": None if gem5 == 0 else difference / gem5,
    }


def producer_report(
    *, workload: str, gem5_fst_dir: Path, qemu_fst_dir: Path, cores: int = 4,
) -> dict[str, Any]:
    if not workload:
        raise ValueError("workload must not be empty")
    directories = {
        "gem5": gem5_fst_dir.resolve(),
        "qemu": qemu_fst_dir.resolve(),
    }
    expected = {str(core) for core in range(cores)}
    producers: dict[str, Any] = {}
    for name, directory in directories.items():
        boundaries = _boundaries(directory)
        if set(boundaries) != expected:
            raise ValueError(f"{name} FST boundaries do not match cores={cores}")
        rows = [
            _summarize_core(directory / f"core{core}.fst", boundaries[str(core)])
            for core in range(cores)
        ]
        producers[name] = {
            "per_core": {str(row["core"]): row for row in rows},
            "aggregate": _merge(rows),
        }
    return {
        "workload": workload,
        "cores": cores,
        "scope": "producer-local-measurement-fst-features",
        "producers": producers,
        "delta": {
            "per_core": {
                str(core): _delta(
                    producers["gem5"]["per_core"][str(core)],
                    producers["qemu"]["per_core"][str(core)],
                )
                for core in range(cores)
            },
            "aggregate": _delta(
                producers["gem5"]["aggregate"], producers["qemu"]["aggregate"]
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--gem5-fst-dir", type=Path, required=True)
    parser.add_argument("--qemu-fst-dir", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=4)
    args = parser.parse_args(argv)
    print(json.dumps(producer_report(
        workload=args.workload,
        gem5_fst_dir=args.gem5_fst_dir,
        qemu_fst_dir=args.qemu_fst_dir,
        cores=args.cores,
    ), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
