from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .projection import (
    DESTINATION_CLASS_MARKER,
    FST_FIELD_NAMES,
    FST_HEADER,
    FST_MAGIC,
    FST_RECORD,
    FST_VERSION,
    MEMORY_FLAGS,
)


DEFAULT_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_SAMPLE_LIMIT = 8
SYSCALL_OP_CLASS = -1


@dataclass(frozen=True)
class FstHeaderInfo:
    raw: bytes
    magic: bytes
    version: int
    header_size: int
    record_size: int
    core_id: int
    record_count: int
    feature_flags: int
    reserved: tuple[int, int, int, int]


def _empty_domain() -> dict[str, Any]:
    return {
        "status": "pass",
        "mismatch_records": 0,
        "field_counts": {},
        "samples": [],
    }


def _empty_domains() -> dict[str, dict[str, Any]]:
    return {
        "structure": _empty_domain(),
        "core_reconstructable": _empty_domain(),
    }


def _mark_domain(domains: dict[str, dict[str, Any]], name: str) -> None:
    domains[name]["status"] = "fail"


def _format_value(value: int | str | None) -> dict[str, Any]:
    if isinstance(value, int):
        return {"dec": value, "hex": f"{value:#x}"}
    return {"value": value}


def _add_mismatch(
    domains: dict[str, dict[str, Any]],
    *,
    domain: str,
    field: str,
    core: int | None,
    ordinal: int | None,
    gem5: int | str | None,
    dr: int | str | None,
    sample_limit: int,
) -> None:
    target = domains[domain]
    _mark_domain(domains, domain)
    counts = target["field_counts"]
    counts[field] = int(counts.get(field, 0)) + 1
    if len(target["samples"]) < sample_limit:
        sample: dict[str, Any] = {
            "field": field,
            "gem5": _format_value(gem5),
            "dr": _format_value(dr),
        }
        if core is not None:
            sample["core"] = core
        if ordinal is not None:
            sample["ordinal"] = ordinal
        target["samples"].append(sample)


def _read_header(path: Path) -> FstHeaderInfo:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"truncated FST header: {path}")
    (
        magic,
        version,
        header_size,
        record_size,
        core_id,
        record_count,
        feature_flags,
        r0,
        r1,
        r2,
        r3,
    ) = FST_HEADER.unpack(raw)
    return FstHeaderInfo(
        raw=raw,
        magic=magic,
        version=version,
        header_size=header_size,
        record_size=record_size,
        core_id=core_id,
        record_count=record_count,
        feature_flags=feature_flags,
        reserved=(r0, r1, r2, r3),
    )


def _validate_header_shape(path: Path, info: FstHeaderInfo) -> str | None:
    if info.magic != FST_MAGIC:
        return "magic"
    if info.version != FST_VERSION:
        return "version"
    if info.header_size != FST_HEADER.size:
        return "header_size"
    if info.record_size != FST_RECORD.size:
        return "record_size"
    expected_size = FST_HEADER.size + info.record_count * FST_RECORD.size
    if path.stat().st_size != expected_size:
        return "file_size"
    return None


def _pc_bounds(path: Path, count: int) -> dict[str, str | None]:
    if count == 0:
        return {"first_pc": None, "last_pc": None}
    with path.open("rb") as source:
        source.seek(FST_HEADER.size)
        first = FST_RECORD.unpack(source.read(FST_RECORD.size))[0]
        source.seek(FST_HEADER.size + (count - 1) * FST_RECORD.size)
        last = FST_RECORD.unpack(source.read(FST_RECORD.size))[0]
    return {"first_pc": f"{first:#x}", "last_pc": f"{last:#x}"}


def _field_domain(
    field: str, gem5_record: tuple[int, ...], dr_record: tuple[int, ...],
) -> tuple[str, str] | None:
    gem5_flags = int(gem5_record[9])
    dr_flags = int(dr_record[9])
    gem5_memory = bool(gem5_flags & MEMORY_FLAGS)
    dr_memory = bool(dr_flags & MEMORY_FLAGS)
    gem5_syscall = int(gem5_record[10]) == SYSCALL_OP_CLASS
    dr_syscall = int(dr_record[10]) == SYSCALL_OP_CLASS
    if field == "address" and (gem5_syscall or dr_syscall):
        return "core_reconstructable", "syscall_number"
    if field == "address" and (gem5_memory or dr_memory):
        # Each producer validates its own physical-address provenance. Their
        # executions have independent virtual and physical layouts, so neither
        # raw PA values nor cache-line placement is a cross-producer contract.
        return None
    if field == "reserved":
        gem5_marker = int(gem5_record[17]) & DESTINATION_CLASS_MARKER
        dr_marker = int(dr_record[17]) & DESTINATION_CLASS_MARKER
        if gem5_marker != dr_marker:
            return "core_reconstructable", "destination_class_marker"
        if gem5_memory or dr_memory:
            # Token values intern virtual pages within the producer address
            # space, so cross-producer raw token IDs are not comparable.
            return None
    return "core_reconstructable", field


def _compare_records(
    *,
    domains: dict[str, dict[str, Any]],
    core: int,
    ordinal: int,
    gem5_record: tuple[int, ...],
    dr_record: tuple[int, ...],
    sample_limit: int,
) -> set[str]:
    record_domains: set[str] = set()
    for index, (gem5_value, dr_value) in enumerate(zip(gem5_record, dr_record)):
        if gem5_value == dr_value:
            continue
        field = FST_FIELD_NAMES[index]
        classification = _field_domain(field, gem5_record, dr_record)
        if classification is None:
            continue
        domain, domain_field = classification
        record_domains.add(domain)
        _add_mismatch(
            domains,
            domain=domain,
            field=domain_field,
            core=core,
            ordinal=ordinal,
            gem5=int(gem5_value),
            dr=int(dr_value),
            sample_limit=sample_limit,
        )
    return record_domains


def _compare_structure_field(
    domains: dict[str, dict[str, Any]],
    *,
    core: int | None,
    field: str,
    gem5: int | str | None,
    dr: int | str | None,
    sample_limit: int,
) -> None:
    domains["structure"]["mismatch_records"] += 1
    _add_mismatch(
        domains,
        domain="structure",
        field=field,
        core=core,
        ordinal=None,
        gem5=gem5,
        dr=dr,
        sample_limit=sample_limit,
    )


def _merge_domain_counts(
    total: dict[str, dict[str, Any]],
    local: dict[str, dict[str, Any]],
    sample_limit: int,
) -> None:
    for domain, payload in local.items():
        if payload["status"] != "pass":
            _mark_domain(total, domain)
        total[domain]["mismatch_records"] += int(payload["mismatch_records"])
        for field, count in payload["field_counts"].items():
            counts = total[domain]["field_counts"]
            counts[field] = int(counts.get(field, 0)) + int(count)
        remaining = sample_limit - len(total[domain]["samples"])
        if remaining > 0:
            total[domain]["samples"].extend(payload["samples"][:remaining])


def _compare_core_records(
    *,
    gem5_path: Path,
    dr_path: Path,
    core: int,
    record_count: int,
    chunk_bytes: int,
    sample_limit: int,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    domains = _empty_domains()
    compared_records = 0
    compared_bytes = 0
    chunk_bytes = max(FST_RECORD.size, chunk_bytes)
    chunk_bytes -= chunk_bytes % FST_RECORD.size
    with gem5_path.open("rb") as gem5_source, dr_path.open("rb") as dr_source:
        gem5_source.seek(FST_HEADER.size)
        dr_source.seek(FST_HEADER.size)
        while compared_records < record_count:
            remaining_records = record_count - compared_records
            records_in_chunk = min(remaining_records, chunk_bytes // FST_RECORD.size)
            read_size = records_in_chunk * FST_RECORD.size
            gem5_chunk = gem5_source.read(read_size)
            dr_chunk = dr_source.read(read_size)
            if len(gem5_chunk) != read_size or len(dr_chunk) != read_size:
                _compare_structure_field(
                    domains,
                    core=core,
                    field="chunk_size",
                    gem5=len(gem5_chunk),
                    dr=len(dr_chunk),
                    sample_limit=sample_limit,
                )
                break
            compared_bytes += read_size
            if gem5_chunk == dr_chunk:
                compared_records += records_in_chunk
                continue
            for index in range(records_in_chunk):
                start = index * FST_RECORD.size
                end = start + FST_RECORD.size
                gem5_raw = gem5_chunk[start:end]
                dr_raw = dr_chunk[start:end]
                if gem5_raw == dr_raw:
                    continue
                ordinal = compared_records + index + 1
                record_domains = _compare_records(
                    domains=domains,
                    core=core,
                    ordinal=ordinal,
                    gem5_record=FST_RECORD.unpack(gem5_raw),
                    dr_record=FST_RECORD.unpack(dr_raw),
                    sample_limit=sample_limit,
                )
                for domain in record_domains:
                    domains[domain]["mismatch_records"] += 1
            compared_records += records_in_chunk
    return domains, compared_records, compared_bytes


def compare_fst_pairs(
    gem5_paths: Sequence[Path],
    dr_paths: Sequence[Path],
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    domains = _empty_domains()
    if len(gem5_paths) != len(dr_paths):
        _compare_structure_field(
            domains,
            core=None,
            field="core_count",
            gem5=len(gem5_paths),
            dr=len(dr_paths),
            sample_limit=sample_limit,
        )
        return {
            "schema": "fastsim-dr-fst-layered-comparison-v1",
            "status": "fail",
            "records": 0,
            "bytes_compared": 0,
            "per_core": [],
            "domains": domains,
        }

    total_records = 0
    total_bytes = 0
    per_core: list[dict[str, Any]] = []
    for expected_core, (gem5_path, dr_path) in enumerate(zip(gem5_paths, dr_paths)):
        core_domains = _empty_domains()
        try:
            gem5_header = _read_header(gem5_path)
            dr_header = _read_header(dr_path)
        except ValueError as error:
            _compare_structure_field(
                core_domains,
                core=expected_core,
                field="header",
                gem5=str(error),
                dr=None,
                sample_limit=sample_limit,
            )
            _merge_domain_counts(domains, core_domains, sample_limit)
            per_core.append({"core": expected_core, "status": "fail", "records": 0})
            continue

        for side, path, header in (
            ("gem5", gem5_path, gem5_header),
            ("dr", dr_path, dr_header),
        ):
            invalid = _validate_header_shape(path, header)
            if invalid:
                _compare_structure_field(
                    core_domains,
                    core=expected_core,
                    field=f"{side}.{invalid}",
                    gem5="valid",
                    dr="invalid",
                    sample_limit=sample_limit,
                )
        for field in ("core_id", "record_count", "feature_flags", "reserved"):
            gem5_value = getattr(gem5_header, field)
            dr_value = getattr(dr_header, field)
            if gem5_value != dr_value:
                _compare_structure_field(
                    core_domains,
                    core=expected_core,
                    field=field,
                    gem5=str(gem5_value) if field == "reserved" else int(gem5_value),
                    dr=str(dr_value) if field == "reserved" else int(dr_value),
                    sample_limit=sample_limit,
                )
        if gem5_header.core_id != expected_core or dr_header.core_id != expected_core:
            _compare_structure_field(
                core_domains,
                core=expected_core,
                field="manifest_core_order",
                gem5=gem5_header.core_id,
                dr=dr_header.core_id,
                sample_limit=sample_limit,
            )

        structure_failed = core_domains["structure"]["status"] == "fail"
        compared_records = 0
        compared_bytes = 0
        bounds = {"first_pc": None, "last_pc": None}
        if not structure_failed:
            bounds = _pc_bounds(gem5_path, gem5_header.record_count)
            record_domains, compared_records, compared_bytes = _compare_core_records(
                gem5_path=gem5_path,
                dr_path=dr_path,
                core=expected_core,
                record_count=gem5_header.record_count,
                chunk_bytes=chunk_bytes,
                sample_limit=sample_limit,
            )
            _merge_domain_counts(core_domains, record_domains, sample_limit)
        _merge_domain_counts(domains, core_domains, sample_limit)
        total_records += compared_records
        total_bytes += compared_bytes
        core_failed = (
            core_domains["structure"]["status"] == "fail"
            or core_domains["core_reconstructable"]["status"] == "fail"
        )
        per_core.append({
            "core": expected_core,
            "status": "fail" if core_failed else "pass",
            "records": compared_records,
            "bytes_compared": compared_bytes,
            "domains": core_domains,
            **bounds,
        })

    failed = (
        domains["structure"]["status"] == "fail"
        or domains["core_reconstructable"]["status"] == "fail"
    )
    return {
        "schema": "fastsim-dr-fst-layered-comparison-v1",
        "status": "fail" if failed else "pass",
        "records": total_records,
        "bytes_compared": total_bytes,
        "per_core": per_core,
        "domains": domains,
    }
