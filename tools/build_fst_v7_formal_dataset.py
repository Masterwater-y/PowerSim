#!/usr/bin/env python3
"""Build a canonical FST v7 dataset from an audited FS matrix.

The hot 64-byte records are cloned with Linux reflinks when available.  Files
with the legacy syscall-marker feature are scanned once and receive one sparse
128-byte v7 row per marker. Native FST v7 inputs are copied byte-for-byte so
producer-captured arguments, return values and boundary metadata are never
downgraded. Source result directories are read-only and are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from validate_fs_oracle_identity import validate_result_identity
from validate_kernel_events_oracle import validate_document


HEADER = struct.Struct("<8sIIIIQQ4Q")
SYSCALL_ROW = struct.Struct("<QQQQ6QQQQIIIHBBQ")
MAGIC = b"FSTRC01\0"
HEADER_BYTES = 72
RECORD_BYTES = 64
SYSCALL_BYTES = 128
FST_V7 = 7
FEATURE_SYSCALL_MARKERS = 1 << 1
FEATURE_DESTINATION_CLASS_COUNTS = 1 << 2
FEATURE_SYSCALL_METADATA = 1 << 3
DESTINATION_CLASS_COUNTS_MARKER = 1 << 31
LINUX_X86_64 = 1
SYSCALL_FIELDS = {
    "arguments": 1 << 0,
    "return_value": 1 << 1,
    "failure": 1 << 2,
    "errno": 1 << 3,
    "pre_timestamp": 1 << 4,
    "post_timestamp": 1 << 5,
    "pre_cpu": 1 << 6,
    "post_cpu": 1 << 7,
    "maybe_blocking": 1 << 8,
    "thread_id": 1 << 9,
}
RECORD_DTYPE = np.dtype(
    {
        "names": [
            "pc", "address", "op_class", "n_dst",
            "producer_classes", "reserved",
        ],
        "formats": ["<u8", "<u8", "<i2", "u1", ("u1", 4), "<u4"],
        "offsets": [0, 8, 52, 55, 56, 60],
        "itemsize": RECORD_BYTES,
    }
)


@dataclass(frozen=True)
class Header:
    version: int
    core_id: int
    records: int
    features: int
    metadata_offset: int = 0
    metadata_count: int = 0
    metadata_size: int = 0
    syscall_abi: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument(
        "--no-hashes", action="store_true",
        help="skip output SHA-256 (diagnostic only; not for a formal build)",
    )
    parser.add_argument(
        "--allow-missing-destination-classes", action="store_true",
        help=(
            "permit legacy traces without per-UOP Int/Float/Vec/CC "
            "destination counts (diagnostic migration only; never formal)"
        ),
    )
    return parser.parse_args()


def read_header(path: Path) -> Header:
    with path.open("rb") as source:
        raw = source.read(HEADER_BYTES)
    if len(raw) != HEADER_BYTES:
        raise ValueError(f"short FST header: {path}")
    magic, version, header_size, record_size, core_id, records, features, *reserved = HEADER.unpack(raw)
    if (
        magic != MAGIC
        or version not in (3, 4, 5, 6, 7)
        or header_size != HEADER_BYTES
        or record_size != RECORD_BYTES
    ):
        raise ValueError(f"unsupported FST header: {path}")
    records_end = HEADER_BYTES + records * RECORD_BYTES
    if version != FST_V7 and path.stat().st_size != records_end:
        raise ValueError(f"legacy FST has unexpected trailing bytes: {path}")
    if version == FST_V7:
        metadata_offset, metadata_count, metadata_size, abi = reserved
        expected = records_end
        if features & FEATURE_SYSCALL_METADATA:
            expected += metadata_count * metadata_size
            valid = (
                features & FEATURE_SYSCALL_MARKERS
                and metadata_offset == records_end
                and metadata_count > 0
                and metadata_size == SYSCALL_BYTES
                and abi == LINUX_X86_64
            )
        else:
            valid = (
                metadata_offset == metadata_count == metadata_size == 0
                and abi == LINUX_X86_64
            )
        if not valid or path.stat().st_size != expected:
            raise ValueError(f"invalid FST v7 table: {path}")
    return Header(version, core_id, records, features, *reserved)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        chunk = source.read(8 << 20)
        while chunk:
            digest.update(chunk)
            chunk = source.read(8 << 20)
    return digest.hexdigest()


def clone_file(source: Path, target: Path) -> str:
    command = ["cp", "--reflink=always", "--", str(source), str(target)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        return "reflink"
    shutil.copyfile(source, target)
    return "copy"


def syscall_rows(path: Path, header: Header) -> list[tuple[int, int, int]]:
    if not (header.features & FEATURE_SYSCALL_MARKERS):
        return []
    records = np.memmap(
        path, dtype=RECORD_DTYPE, mode="r", offset=HEADER_BYTES,
        shape=(header.records,),
    )
    ordinals = np.flatnonzero(records["op_class"] == -1)
    rows = [
        (int(ordinal), int(records["pc"][ordinal]), int(records["address"][ordinal]))
        for ordinal in ordinals
    ]
    del records
    if not rows:
        raise ValueError(f"syscall feature bit set but no marker exists: {path}")
    return rows


def destination_class_coverage(path: Path, header: Header) -> dict[str, int | bool]:
    """Validate the v7 per-record destination-class contract.

    The header feature bit is not sufficient: the old TaoTrace direct writer
    advertised schema 7 while leaving every hot record in the legacy layout.
    Formal timing data must prove that each record carries the packing marker
    and that the four encoded counts conserve n_dst.
    """
    records = np.memmap(
        path, dtype=RECORD_DTYPE, mode="r", offset=HEADER_BYTES,
        shape=(header.records,),
    )
    marked = (records["reserved"] & DESTINATION_CLASS_COUNTS_MARKER) != 0
    counts = records["producer_classes"] >> np.uint8(3)
    classified = counts.astype(np.uint16).sum(axis=1)
    mismatched = marked & (classified != records["n_dst"])
    marked_count = int(np.count_nonzero(marked))
    mismatched_count = int(np.count_nonzero(mismatched))
    destination_uops = int(np.count_nonzero(records["n_dst"]))
    missing_destination_uops = int(np.count_nonzero((records["n_dst"] != 0) & ~marked))
    del records
    if mismatched_count:
        raise ValueError(
            f"destination class counts do not conserve n_dst in "
            f"{mismatched_count} records: {path}"
        )
    feature = bool(header.features & FEATURE_DESTINATION_CLASS_COUNTS)
    complete = feature and marked_count == header.records
    return {
        "feature": feature,
        "marked_records": marked_count,
        "records": header.records,
        "destination_uops": destination_uops,
        "missing_destination_uops": missing_destination_uops,
        "complete": complete,
    }


def encode_row(record_ordinal: int, syscall_ordinal: int, number: int) -> bytes:
    return SYSCALL_ROW.pack(
        record_ordinal, syscall_ordinal, 0, number,
        0, 0, 0, 0, 0, 0,  # six invalid arguments
        0, 0, 0,              # invalid return/pre/post timestamps
        0, 0, 0,              # invalid errno/pre/post CPU
        0, 0, 0, 0,           # validity/arg-count/flags/reserved
    )


def decode_metadata_rows(
    path: Path, header: Header, hot_rows: list[tuple[int, int, int]]
) -> list[dict[str, Any]]:
    if not hot_rows:
        return []
    if (
        header.version != FST_V7
        or not header.features & FEATURE_SYSCALL_METADATA
        or header.metadata_count != len(hot_rows)
    ):
        raise ValueError(f"syscall metadata/hot-record count mismatch: {path}")
    rows = []
    with path.open("rb") as source:
        source.seek(header.metadata_offset)
        for expected, (record_ordinal, pc, number) in enumerate(hot_rows):
            raw = source.read(SYSCALL_BYTES)
            if len(raw) != SYSCALL_BYTES:
                raise ValueError(f"truncated syscall metadata: {path}")
            values = SYSCALL_ROW.unpack(raw)
            if (
                values[0] != record_ordinal
                or values[1] != expected
                or values[3] != number
                or values[19] != 0
            ):
                raise ValueError(f"misaligned syscall metadata: {path}")
            rows.append(
                {
                    "record_ordinal": record_ordinal,
                    "syscall_ordinal": expected,
                    "pc": pc,
                    "thread_id": values[2],
                    "number": number,
                    "arguments": list(values[4:10]),
                    "return_value_raw": values[10],
                    "pre_timestamp_us": values[11],
                    "post_timestamp_us": values[12],
                    "errno": values[13],
                    "pre_cpu": values[14],
                    "post_cpu": values[15],
                    "valid_fields": values[16],
                    "argument_count": values[17],
                    "flags": values[18],
                }
            )
    return rows


def metadata_event(core: int, row: dict[str, Any]) -> dict[str, Any]:
    valid = int(row["valid_fields"])
    flags = int(row["flags"])
    event: dict[str, Any] = {
        "schema": "fastsim-functional-syscall-v2",
        "event": "syscall",
        "abi": "linux-x86_64",
        "core_id": core,
        "record_ordinal": int(row["record_ordinal"]),
        "syscall_ordinal": int(row["syscall_ordinal"]),
        "pc": int(row["pc"]),
        "syscall_nr": int(row["number"]),
        "capture_flags": valid,
    }
    if valid & (1 << 9):
        event["thread_id"] = int(row["thread_id"])
    if valid & (1 << 0):
        event["args"] = [
            int(value)
            for value in row["arguments"][: int(row["argument_count"])]
        ]
        event["arg_count"] = int(row["argument_count"])
    if valid & (1 << 1):
        event["retval_raw"] = int(row["return_value_raw"])
    if valid & (1 << 2):
        event["failed"] = bool(flags & 1)
    if valid & (1 << 3):
        event["errno"] = int(row["errno"])
    if valid & (1 << 4):
        event["pre_timestamp_us"] = int(row["pre_timestamp_us"])
    if valid & (1 << 5):
        event["post_timestamp_us"] = int(row["post_timestamp_us"])
    if valid & (1 << 6):
        event["pre_cpu"] = int(row["pre_cpu"])
    if valid & (1 << 7):
        event["post_cpu"] = int(row["post_cpu"])
    if valid & (1 << 8):
        event["maybe_blocking"] = bool(flags & 2)
    return event


def upgrade_one(
    source: Path,
    target: Path,
    with_hash: bool,
    allow_missing_destination_classes: bool,
) -> dict[str, Any]:
    source_header = read_header(source)
    destination_classes = destination_class_coverage(source, source_header)
    if (
        not destination_classes["complete"]
        and not allow_missing_destination_classes
    ):
        raise ValueError(
            "formal FST requires destination_class_counts on every record; "
            f"feature={destination_classes['feature']} "
            f"marked={destination_classes['marked_records']}/"
            f"{destination_classes['records']} "
            f"unclassified_destination_uops="
            f"{destination_classes['missing_destination_uops']}: {source}"
        )
    source_page_map = Path(str(source) + ".vmap")
    target_page_map = Path(str(target) + ".vmap")
    source_address_space_map = Path(str(source) + ".asmap")
    target_address_space_map = Path(str(target) + ".asmap")
    source_instruction_page_map = Path(str(source) + ".ifmap")
    target_instruction_page_map = Path(str(target) + ".ifmap")
    source_instruction_map = Path(str(source) + ".imap")
    target_instruction_map = Path(str(target) + ".imap")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.recovering-{uuid.uuid4().hex[:10]}")
    method = clone_file(source, temporary)
    try:
        rows = syscall_rows(source, source_header)
        if source_header.version != FST_V7:
            records_end = HEADER_BYTES + source_header.records * RECORD_BYTES
            features = source_header.features
            reserved = [0, 0, 0, LINUX_X86_64]
            with temporary.open("r+b") as output:
                if rows:
                    features |= FEATURE_SYSCALL_MARKERS | FEATURE_SYSCALL_METADATA
                    reserved[:3] = [records_end, len(rows), SYSCALL_BYTES]
                    output.seek(records_end)
                    for syscall_ordinal, (record_ordinal, _pc, number) in enumerate(rows):
                        output.write(encode_row(record_ordinal, syscall_ordinal, number))
                else:
                    features &= ~FEATURE_SYSCALL_METADATA
                output.seek(0)
                output.write(
                    HEADER.pack(
                        MAGIC, FST_V7, HEADER_BYTES, RECORD_BYTES,
                        source_header.core_id, source_header.records, features,
                        *reserved,
                    )
                )
                output.flush()
                os.fsync(output.fileno())
        current = read_header(temporary)
        if current.version != FST_V7 or current.records != source_header.records:
            raise ValueError(f"post-upgrade validation failed: {temporary}")
        metadata = decode_metadata_rows(temporary, current, rows)
        temporary.replace(target)
        page_map_method = None
        if source_page_map.is_file():
            page_map_method = clone_file(source_page_map, target_page_map)
        address_space_map_method = None
        if source_address_space_map.is_file():
            address_space_map_method = clone_file(
                source_address_space_map, target_address_space_map
            )
        instruction_page_map_method = None
        if source_instruction_page_map.is_file():
            if source_header.version != FST_V7:
                raise ValueError(
                    f"instruction-page companion requires native FST v7: {source}"
                )
            instruction_page_map_method = clone_file(
                source_instruction_page_map, target_instruction_page_map
            )
        instruction_map_method = None
        if source_instruction_map.is_file():
            if source_header.version != FST_V7:
                raise ValueError(
                    f"static instruction companion requires native FST v7: {source}"
                )
            instruction_map_method = clone_file(
                source_instruction_map, target_instruction_map
            )
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "source": str(source), "target": str(target),
        "core_id": current.core_id, "records": current.records,
        "features": current.features, "syscalls": rows,
        "syscall_metadata": metadata,
        "sha256": sha256(target) if with_hash else None,
        "copy_method": method,
        "destination_class_coverage": destination_classes,
        "virtual_page_map": (
            str(target_page_map) if target_page_map.is_file() else None
        ),
        "virtual_page_map_sha256": (
            sha256(target_page_map)
            if with_hash and target_page_map.is_file() else None
        ),
        "virtual_page_map_copy_method": page_map_method,
        "address_space_map": (
            str(target_address_space_map)
            if target_address_space_map.is_file() else None
        ),
        "address_space_map_sha256": (
            sha256(target_address_space_map)
            if with_hash and target_address_space_map.is_file() else None
        ),
        "address_space_map_copy_method": address_space_map_method,
        "instruction_page_map": (
            str(target_instruction_page_map)
            if target_instruction_page_map.is_file() else None
        ),
        "instruction_page_map_sha256": (
            sha256(target_instruction_page_map)
            if with_hash and target_instruction_page_map.is_file() else None
        ),
        "instruction_page_map_copy_method": instruction_page_map_method,
        "static_instruction_map": (
            str(target_instruction_map)
            if target_instruction_map.is_file() else None
        ),
        "static_instruction_map_sha256": (
            sha256(target_instruction_map)
            if with_hash and target_instruction_map.is_file() else None
        ),
        "static_instruction_map_copy_method": instruction_map_method,
    }


def copy_case_metadata(source: Path, target: Path) -> None:
    for name in ("request.json", "config.ini", "effective-target.json"):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    source_oracle = source / "oracle"
    if source_oracle.is_dir():
        target_oracle = target / "oracle"
        target_oracle.mkdir(parents=True, exist_ok=True)
        for oracle_file in source_oracle.rglob("*"):
            if not oracle_file.is_file():
                continue
            destination = target_oracle / oracle_file.relative_to(source_oracle)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(oracle_file, destination)
    for name in (
        "uarch_profile.json",
        "syscall_capture.json",
        "initial-pte-state.json",
        "roi-entry-page-state.json",
    ):
        metadata = source / "tao_trace" / name
        if metadata.is_file():
            shutil.copy2(metadata, target / "tao_trace" / name)
    # Normalize the retired producer filename while accepting already-captured
    # datasets. The FST `.vmap` bit layout itself is unchanged.
    legacy_roi_entry_state = (
        source / "tao_trace" / "measurement-pte-state.json"
    )
    canonical_roi_entry_state = (
        target / "tao_trace" / "roi-entry-page-state.json"
    )
    if (
        legacy_roi_entry_state.is_file()
        and not canonical_roi_entry_state.is_file()
    ):
        shutil.copy2(legacy_roi_entry_state, canonical_roi_entry_state)


def rewrite_manifest(source: Path, target: Path) -> None:
    output = []
    for line in source.read_text(encoding="utf-8").splitlines():
        columns = line.split()
        if not columns:
            continue
        if len(columns) < 3 or not columns[2].endswith(".fst"):
            raise ValueError(f"unsupported manifest row: {line}")
        columns[2] = Path(columns[2]).name
        output.append(" ".join(columns))
    target.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    audit = json.loads(args.audit.resolve().read_text(encoding="utf-8"))
    cases = sorted(
        audit["cases"], key=lambda item: (int(item["cores"]), str(item["workload"]))
    )
    out = args.out.resolve()
    tasks: list[tuple[Path, Path, str]] = []
    case_info: dict[str, dict[str, Any]] = {}
    for case in cases:
        cores = int(case["cores"])
        workload = str(case["workload"])
        case_id = f"{cores:02d}c-{workload}"
        source = Path(case["result_dir"]).resolve()
        identity = validate_result_identity(source)
        if not identity["valid"]:
            fields = ", ".join(row["field"] for row in identity["mismatches"])
            raise ValueError(
                f"{case_id}: refusing an oracle-identity mismatch: {fields}"
            )
        oracle_path = source / "oracle" / "kernel_events.json"
        if not oracle_path.is_file():
            raise ValueError(f"{case_id}: missing kernel-events oracle")
        oracle_validation = validate_document(
            json.loads(oracle_path.read_text(encoding="utf-8")), 0.0
        )
        if not oracle_validation.get("formal_pmu_eligible", False):
            raise ValueError(
                f"{case_id}: formal dataset requires P0 v3 memory accounting"
            )
        target = out / "cases" / case_id
        (target / "tao_trace").mkdir(parents=True, exist_ok=True)
        copy_case_metadata(source, target)
        rewrite_manifest(
            source / "tao_trace" / "manifest.txt",
            target / "tao_trace" / "manifest.txt",
        )
        case_info[case_id] = {
            **case, "case_id": case_id, "source_result_dir": str(source),
            "result_dir": str(target), "oracle_identity": identity,
            "files": {},
        }
        for core in range(cores):
            tasks.append(
                (
                    source / "tao_trace" / f"core{core}.fst",
                    target / "tao_trace" / f"core{core}.fst",
                    case_id,
                )
            )

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = {
            executor.submit(
                upgrade_one,
                source,
                target,
                not args.no_hashes,
                args.allow_missing_destination_classes,
            ):
                (source, target, case_id)
            for source, target, case_id in tasks
        }
        completed = 0
        for future in as_completed(futures):
            source, target, case_id = futures[future]
            try:
                row = future.result()
                case_info[case_id]["files"][str(row["core_id"])] = row
                completed += 1
                if completed % 16 == 0 or completed == len(tasks):
                    print(f"upgraded {completed}/{len(tasks)}", flush=True)
            except Exception as error:  # noqa: BLE001 - preserve all failures
                failures.append(f"{source} -> {target}: {error}")
                print(f"[fail] {failures[-1]}", flush=True)
    if failures:
        raise SystemExit("\n".join(failures[:20]))

    total_records = total_syscalls = total_bytes = 0
    total_field_coverage: Counter[str] = Counter()
    index_cases = []
    for case_id, item in sorted(case_info.items()):
        target = Path(item["result_dir"])
        files = item["files"]
        if len(files) != int(item["cores"]):
            raise SystemExit(f"{case_id}: incomplete file set")
        sidecar_rows = []
        per_core_syscalls = {}
        case_field_coverage: Counter[str] = Counter()
        for core_text, row in sorted(files.items(), key=lambda pair: int(pair[0])):
            core = int(core_text)
            per_core_syscalls[core_text] = len(row["syscalls"])
            with (target / "tao_trace" / f"core{core}.syscalls.jsonl").open(
                "w", encoding="utf-8"
            ) as output:
                for metadata in row["syscall_metadata"]:
                    event = metadata_event(core, metadata)
                    text = json.dumps(event, sort_keys=True) + "\n"
                    output.write(text)
                    sidecar_rows.append((core, int(metadata["syscall_ordinal"]), text))
                    valid = int(metadata["valid_fields"])
                    for name, bit in SYSCALL_FIELDS.items():
                        if valid & bit:
                            case_field_coverage[name] += 1
            total_records += int(row["records"])
            total_syscalls += len(row["syscalls"])
            total_bytes += Path(row["target"]).stat().st_size
        total_field_coverage.update(case_field_coverage)
        with (target / "tao_trace" / "syscalls.jsonl").open("w", encoding="utf-8") as output:
            for _core, _ordinal, text in sorted(sidecar_rows):
                output.write(text)
        source_trace = json.loads(
            (Path(item["source_result_dir"]) / "tao_trace" / "trace.json").read_text()
        )
        source_trace.update(
            {
                "schema": "fastsim-fs-fst-v7-upgrade-v1",
                "source_result_dir": item["source_result_dir"],
                "fst_versions": {core: FST_V7 for core in files},
                "fst_feature_flags": {core: row["features"] for core, row in files.items()},
                "fst_sha256": {core: row["sha256"] for core, row in files.items()},
                "syscall_abi": "linux-x86_64",
                "syscall_metadata_embedded": True,
                "syscall_metadata_entry_bytes": SYSCALL_BYTES,
                "syscall_events": sum(per_core_syscalls.values()),
                "syscall_events_per_core": per_core_syscalls,
                "syscall_field_coverage": dict(sorted(case_field_coverage.items())),
                "syscall_optional_capture": (
                    "validity-mask governed; absent fields are unknown"
                ),
            }
        )
        for core, per_core in source_trace.get("per_core", {}).items():
            per_core["fst"] = str(target / "tao_trace" / f"core{core}.fst")
            per_core["sha256"] = files[str(core)]["sha256"]
            page_map = files[str(core)].get("virtual_page_map")
            if page_map:
                per_core["virtual_page_map"] = page_map
                per_core["virtual_page_map_sha256"] = files[str(core)][
                    "virtual_page_map_sha256"
                ]
            instruction_map = files[str(core)].get("static_instruction_map")
            if instruction_map:
                per_core["static_instruction_map"] = instruction_map
                per_core["static_instruction_map_sha256"] = files[str(core)][
                    "static_instruction_map_sha256"
                ]
        (target / "tao_trace" / "trace.json").write_text(
            json.dumps(source_trace, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (target / "complete.json").write_text(
            json.dumps({"status": "complete", "fst_version": FST_V7}, indent=2) + "\n",
            encoding="utf-8",
        )
        index_item = dict(item)
        index_item["files"] = {
            core: {
                key: value for key, value in row.items()
                if key not in ("syscalls", "syscall_metadata")
            } | {"syscall_events": len(row["syscalls"])}
            for core, row in files.items()
        }
        index_item["syscall_field_coverage"] = dict(
            sorted(case_field_coverage.items())
        )
        index_cases.append(index_item)

    index = {
        "schema": "fastsim-fs-fst-v7-dataset-v1",
        "source_audit": str(args.audit.resolve()),
        "cases": index_cases,
        "totals": {
            "cases": len(index_cases), "fst_files": len(tasks),
            "records": total_records, "syscalls": total_syscalls,
            "bytes": total_bytes,
        },
        "input_granularity": {
            "user_functional_records": True, "syscall_number": True,
            "destination_class_counts": not args.allow_missing_destination_classes,
            "syscall_optional_fields": bool(total_field_coverage),
            "syscall_field_coverage": dict(sorted(total_field_coverage.items())),
            "syscall_validity": "per-event valid_fields bitmap",
        },
        "oracle_validity": {
            "cpi": "usable",
            "pmu": (
                "accounting-eligible: v3 exactly-once and effective-target "
                "identity gates passed; per-event status is dictionary-bound"
            ),
            "pmu_contract_id": "perf-gem5-fastsim-x86-fs-v1",
            "pmu_event_dictionary": "configs/pmu-event-dictionary-v1.json",
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(index["totals"], sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
