"""Promote in-probe TaoTrace FST shards to the canonical coreN.fst layout.

The gem5 TaoTrace probe writes one ``*.records.micro.fst`` per switched core
plus per-core ``functional-boundary-coreN.json``. Downstream FastSim replay and
the QEMU-vs-TaoTrace comparator expect the normalized ``coreN.fst`` +
``manifest.txt`` + ``trace.json`` contract. This is the same promotion the
fastsim-branch collection orchestrator (gem5_fs_roi.py) performs; it is ported
here so minesim owns the full user-only chain locally.

Every promoted field is validated against real evidence and hard-fails on any
mismatch (invalid/incomplete FST header, phase-conservation break, scope
mismatch, privilege-feature-vs-kernel-flag mismatch). No field is defaulted or
synthesized to paper over a missing value.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from tools.audit_fst_static_instruction_maps import audit_map

CORE_RE = re.compile(r"(?:switch|cores)(\d*)\.core")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_fst_header(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(72)
    if len(header) != 72 or header[:8] != b"FSTRC01\0":
        raise ValueError(f"invalid FST header: {path}")
    version = int.from_bytes(header[8:12], "little")
    header_size = int.from_bytes(header[12:16], "little")
    record_size = int.from_bytes(header[16:20], "little")
    record_count = int.from_bytes(header[24:32], "little")
    features = int.from_bytes(header[32:40], "little")
    metadata_offset = int.from_bytes(header[40:48], "little")
    metadata_count = int.from_bytes(header[48:56], "little")
    metadata_size = int.from_bytes(header[56:64], "little")
    syscall_abi = int.from_bytes(header[64:72], "little")
    records_end = 72 + record_count * 64
    if version == 7 and features & (1 << 3):
        complete = (
            features & (1 << 1)
            and metadata_offset == records_end
            and metadata_count > 0
            and metadata_size == 128
            and syscall_abi == 1
            and path.stat().st_size == metadata_offset + metadata_count * 128
        )
    elif version == 7:
        complete = (
            metadata_offset == metadata_count == metadata_size == 0
            and syscall_abi == 1
            and path.stat().st_size == records_end
        )
    else:
        complete = path.stat().st_size == records_end
    if (version not in (5, 6, 7) or header_size != 72 or record_size != 64
            or not complete):
        raise ValueError(f"incomplete FST: {path}")
    return record_count


def _feature_flags(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(40)
    return int.from_bytes(header[32:40], "little")


def normalize(trace_dir: Path, cores: int, *, functional_warmup: bool = True,
              functional_include_kernel: bool = False) -> dict:
    """Promote in-place: switchN shards -> coreN.fst + manifest.txt + trace.json.

    Returns the trace.json metadata dict. Raises ValueError on any integrity
    violation so a corrupt capture never masquerades as a valid trace.
    """
    trace_dir = Path(trace_dir)
    if (trace_dir / "trace.json").is_file():
        return json.loads((trace_dir / "trace.json").read_text())
    shards: dict[int, Path] = {}
    for path in trace_dir.glob("*.records.micro.fst"):
        match = CORE_RE.search(path.name)
        if match is None:
            raise ValueError(f"cannot identify core from {path.name}")
        shards[int(match.group(1) or 0)] = path
    expected = list(range(cores))
    if sorted(shards) != expected:
        raise ValueError(f"expected cores {expected}, found {sorted(shards)}")
    expected_scope = "user-plus-kernel" if functional_include_kernel else "user"
    manifest_lines: list[str] = []
    per_core: dict[int, dict] = {}
    boundaries: dict[int, dict] = {}
    for core_id in expected:
        source = shards[core_id]
        target = trace_dir / f"core{core_id}.fst"
        record_count = _validate_fst_header(source)
        shutil.move(str(source), str(target))
        for suffix in (".vmap", ".asmap", ".imap"):
            src = Path(str(source) + suffix)
            if src.is_file():
                shutil.move(str(src), str(trace_dir / f"core{core_id}.fst{suffix}"))
        boundary_path = trace_dir / f"functional-boundary-core{core_id}.json"
        boundary = json.loads(boundary_path.read_text())
        if boundary.get("schema") != "tcsim-functional-boundary-v1":
            raise ValueError(f"invalid functional boundary: {boundary_path}")
        if bool(boundary.get("functional_warmup_enabled")) != functional_warmup:
            raise ValueError(f"functional warmup mismatch: {boundary_path}")
        if not boundary.get("measurement_started") or not boundary.get(
            "target_reached"
        ):
            raise ValueError(f"incomplete functional boundary: {boundary_path}")
        if boundary.get("trace_scope", "user") != expected_scope:
            raise ValueError(f"trace scope mismatch: {boundary_path}")
        if int(boundary.get("total_records", -1)) != record_count:
            raise ValueError(f"boundary record mismatch: {boundary_path}")
        if bool(_feature_flags(target) & (1 << 4)) != functional_include_kernel:
            raise ValueError(f"FST privilege feature mismatch: {target}")
        static_map = audit_map(target)
        if (
            not static_map["present"]
            or not static_map.get("instruction_rows", 0)
            or not static_map.get("operands_complete", False)
        ):
            raise ValueError(
                f"TaoTrace lacks a complete AS-scoped instruction map: "
                f"{target}"
            )
        if int(boundary.get("warmup_records", 0)) + int(
            boundary.get("measurement_records", 0)
        ) != record_count:
            raise ValueError(f"phase conservation failure: {boundary_path}")
        boundaries[core_id] = boundary
        per_core[core_id] = {
            "fst": target.name,
            "records": record_count,
            "sha256": _sha256(target),
            "warmup_records": int(boundary["warmup_records"]),
            "warmup_instructions": int(boundary["warmup_instructions"]),
            "measurement_records": int(boundary["measurement_records"]),
            "measurement_instructions": int(
                boundary["measurement_instructions"]
            ),
        }
        if functional_warmup:
            manifest_lines.append(
                f"{core_id} fastsim-binary-warmup-slice core{core_id}.fst "
                f"{core_id} {boundary['warmup_instructions']} "
                f"{boundary['measurement_instructions']} "
                f"{boundary['warmup_records']} "
                f"{boundary['measurement_records']}\n"
            )
        else:
            manifest_lines.append(
                f"{core_id} fastsim-binary core{core_id}.fst\n"
            )
    if functional_warmup and sum(
        row["warmup_instructions"] for row in per_core.values()
    ) <= 0:
        raise ValueError("empty functional warmup across all cores")
    (trace_dir / "manifest.txt").write_text(
        "".join(manifest_lines), encoding="utf-8"
    )
    metadata = {
        "schema": "tcsim-gem5-fs-functional-trace-v1",
        "cores": cores,
        "per_core": {str(k): v for k, v in per_core.items()},
        "functional_warmup_enabled": functional_warmup,
        "trace_scope": expected_scope,
        "functional_boundaries": {str(k): v for k, v in boundaries.items()},
    }
    (trace_dir / "trace.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata
