#!/usr/bin/env python3
"""Promote a first-core common-end native FST capture with its paired oracle.

This intentionally does not call the legacy per-core target collector: a slow
core below N, a partial final macro, or an open syscall at the shared event is
valid. Preserve the bytes and the observed macro/UOP counts; never trim or pad.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

from common_end_capture import POLICY, validate_common_end


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def finalize_capture(result, *, cores, target, tcsim_root):
    sys.path.insert(0, str(Path(tcsim_root) / "scripts"))
    import gem5_fs_roi as roi

    result = Path(result)
    scratch, final = result / "trace-scratch", result / "tao_trace"
    boundaries = [json.loads((scratch / f"functional-boundary-core{i}.json").read_text())
                  for i in range(cores)]
    roi._promote_oracle(scratch, result / "oracle")
    cpi = json.loads((result / "oracle/cpi.json").read_text())
    cpl = [json.loads(line) for line in
           (result / "oracle/cpl_class.jsonl").read_text().splitlines() if line.strip()]
    gate = validate_common_end(boundaries, cpi["per_core"], cpl,
                              cores=cores, target=target)
    sources = {}
    for source in scratch.glob("*.records.micro.fst"):
        match = roi.CORE_RE.search(source.name)
        if match is None:
            raise ValueError(f"unidentified FST: {source}")
        core = int(match.group(1) or 0)
        if core in sources:
            raise ValueError(f"duplicate core {core}")
        sources[core] = source
    if set(sources) != set(range(cores)):
        raise ValueError("missing captured FST participants")
    final.mkdir(parents=True, exist_ok=True)
    per_core, manifest = {}, []
    for core, boundary in enumerate(boundaries):
        source = sources[core]
        records = roi._validate_fst_header(source)
        if records != boundary["total_records"] or boundary["trace_scope"] != "user-plus-kernel":
            raise ValueError(f"core {core}: FST/boundary population or scope mismatch")
        with source.open("rb") as handle:
            header = struct.unpack("<8sIIIIQQ4Q", handle.read(72))
        if header[4] != core or header[6] & 48 != 48:
            raise ValueError(f"core {core}: complete dependencies and CPL required")
        deps = Path(str(source) + ".deps")
        with deps.open("rb") as handle:
            dep = struct.unpack("<8sIIIIQQQ", handle.read(48))
        if dep[:6] != (b"FSTDEP1\0", 1, 48, core, 0, records) or (
                deps.stat().st_size != 48 + dep[6] * 16 + dep[7] * 4):
            raise ValueError(f"core {core}: incomplete dependency companion")
        destination = final / f"core{core}.fst"
        if destination.exists():
            raise FileExistsError(destination)
        shutil.move(str(source), destination)
        row = dict(boundary, fst=str(destination), records=records,
                   sha256=sha256(destination), fst_version=header[1],
                   syscall_metadata_count=header[8], complete_dependencies=True)
        for suffix, key in ((".vmap", "virtual_page_map"),
                            (".asmap", "address_space_map"),
                            (".imap", "static_instruction_map"),
                            (".deps", "dependency_companion")):
            companion = Path(str(source) + suffix)
            if companion.is_file():
                target_path = Path(str(destination) + suffix)
                shutil.move(str(companion), target_path)
                row[key] = str(target_path)
                row[key + "_sha256"] = sha256(target_path)
                row[key + "_bytes"] = target_path.stat().st_size
        per_core[str(core)] = row
        manifest.append(
            f"{core} fastsim-binary-warmup-slice {destination.name} {core} "
            f"{boundary['warmup_instructions']} {boundary['measurement_instructions']} "
            f"{boundary['warmup_records']} {boundary['measurement_records']}\n")
        save(final / f"functional-boundary-core{core}.json", boundary)
    metadata = dict(schema="tcsim-gem5-fs-functional-trace-v1", cores=cores,
                    converter=dict(path="in-probe-fst", sha256=None), per_core=per_core,
                    functional_warmup_enabled=True, trace_scope="user-plus-kernel",
                    measurement_policy=POLICY, common_end=gate,
                    functional_boundaries={str(r["core_id"]): r for r in boundaries},
                    scratch_dir=str(scratch))
    for filename, key in (("uarch_profile.json", None),
                          ("syscall_capture.json", "syscall_capture"),
                          ("initial-pte-state.json", "initial_pte_state"),
                          ("measurement-pte-state.json", "measurement_pte_state")):
        source = scratch / filename
        if source.is_file():
            shutil.copy2(source, final / filename)
            if key:
                metadata[key] = json.loads(source.read_text())
    (final / "manifest.txt").write_text("".join(manifest))
    save(final / "trace.json", metadata)
    save(result / "common-end-audit.json", gate)
    return gate


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--target", type=int, default=10000000)
    parser.add_argument("--tcsim-root", type=Path, default="/data00/yinhaolang/TCSim")
    args = parser.parse_args()
    print(json.dumps(finalize_capture(args.result, cores=args.cores, target=args.target,
                                    tcsim_root=args.tcsim_root), ensure_ascii=False))
