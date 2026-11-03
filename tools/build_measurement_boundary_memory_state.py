#!/usr/bin/env python3
"""Build functional-only cache-state seeds for a two-phase FastSim trace.

The raw TaoTrace mem_events stream is evidence, not a model input.  This tool
keeps only committed data accesses in the global-WORKBEGIN-to-first-emitted-
measurement-record gap and writes the four-column state format accepted by
FastSim.  Timing, path, MESI, sharer, queue, and oracle fields are discarded.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


SCHEMA = "fastsim-boundary-memory-state-v1"
WARMUP_FORMATS = {
    "fastsim-binary-warmup-slice",
    "binary-warmup-slice",
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_glob(directory, patterns, description):
    matches = []
    for pattern in patterns:
        matches.extend(directory.glob(pattern))
    matches = sorted(set(path.resolve() for path in matches))
    if len(matches) != 1:
        raise ValueError(
            f"expected one {description}, found {len(matches)}: "
            + ", ".join(map(str, matches)))
    return matches[0]


def read_manifest(path):
    entries = []
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 8 or fields[1] not in WARMUP_FORMATS:
            raise ValueError(
                f"{path}:{line_number}: expected exact-count binary "
                "warmup slice with eight fields")
        core = int(fields[0])
        source_core = int(fields[3])
        warmup_records = int(fields[6])
        take_records = int(fields[7])
        if core != len(entries) or take_records <= 0:
            raise ValueError(
                f"{path}:{line_number}: core IDs must be dense and take "
                "records must be positive")
        entries.append({
            "core": core,
            "source_core": source_core,
            "warmup_records": warmup_records,
            "fields": fields,
        })
    if not entries:
        raise ValueError(f"empty manifest: {path}")
    return entries


def workbegin_tick(path):
    ticks = [
        int(match.group(1))
        for match in re.finditer(r"\[fs-ckpt\] WORKBEGIN tick=(\d+)",
                                 path.read_text(errors="replace"))
    ]
    if len(ticks) != 1:
        raise ValueError(
            f"expected one source-defined WORKBEGIN tick in {path}, "
            f"found {len(ticks)}")
    return ticks[0]


def first_measurement_commit_tick(labels_path, warmup_records):
    with labels_path.open() as source:
        for ordinal, line in enumerate(source):
            if ordinal == warmup_records:
                row = json.loads(line)
                tick = int(row["commit_tick"])
                if tick <= 0:
                    raise ValueError(
                        f"invalid first measurement commit tick in "
                        f"{labels_path}")
                return tick
    raise ValueError(
        f"{labels_path} ended before measurement record ordinal "
        f"{warmup_records}")


def extract_accesses(path, core, begin_tick, end_tick, dram_size):
    accesses = []
    prior_tick = 0
    raw_commits = 0
    mmio_commits_excluded = 0
    with path.open() as source:
        for line_number, line in enumerate(source, 1):
            row = json.loads(line)
            if row.get("event_type") != "commit":
                continue
            if int(row.get("core_id", -1)) != core:
                raise ValueError(
                    f"{path}:{line_number}: commit core does not match "
                    f"sidecar core {core}")
            tick = int(row["commit_tick"])
            if tick < prior_tick:
                raise ValueError(
                    f"{path}:{line_number}: commit ticks are not ordered")
            prior_tick = tick
            raw_commits += 1
            if tick >= end_tick:
                break
            if tick < begin_tick:
                continue
            address = int(row["cacheline_addr"])
            size = int(row["size"])
            if address < 0 or size <= 0 or size > 64:
                raise ValueError(
                    f"{path}:{line_number}: unsupported committed cacheline "
                    "address or access size")
            if address >= dram_size or address + size > dram_size:
                mmio_commits_excluded += 1
                continue
            accesses.append({
                "physical_address": address,
                "size": size,
                "write": bool(int(row["is_store"])),
            })
    return accesses, raw_commits, mmio_commits_excluded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True, type=Path)
    parser.add_argument("--run-log", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-manifest", type=Path)
    args = parser.parse_args()

    trace_dir = args.trace_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_manifest = (args.output_manifest.resolve()
                       if args.output_manifest else output_dir / "manifest.txt")
    entries = read_manifest(args.manifest.resolve())
    begin_tick = workbegin_tick(args.run_log.resolve())
    uarch_profile_path = trace_dir / "uarch_profile.json"
    uarch_profile = json.loads(uarch_profile_path.read_text())
    dram_size = int(uarch_profile["dram"]["size_b"])
    if dram_size <= 0:
        raise ValueError("uarch profile has an invalid DRAM size")
    report = {
        "schema": "fastsim-boundary-memory-state-build-v1",
        "source_manifest": str(args.manifest.resolve()),
        "source_manifest_sha256": sha256(args.manifest.resolve()),
        "source_run_log": str(args.run_log.resolve()),
        "source_run_log_sha256": sha256(args.run_log.resolve()),
        "measurement_begin_tick": begin_tick,
        "uarch_profile": str(uarch_profile_path),
        "uarch_profile_sha256": sha256(uarch_profile_path),
        "dram_size_bytes": dram_size,
        "output_schema": SCHEMA,
        "retained_fields": [
            "sequence", "physical_address", "size", "operation"],
        "discarded_field_classes": [
            "tick", "path", "latency", "mesi", "coherence-oracle",
            "sharers", "queue-state"],
        "cores": [],
    }
    manifest_lines = []
    for entry in entries:
        core = entry["core"]
        labels = unique_glob(
            trace_dir,
            [f"*switch{entry['source_core']}*labels.micro.jsonl",
             f"*core{entry['source_core']}*labels.micro.jsonl"],
            f"core {core} labels stream")
        mem_events = unique_glob(
            trace_dir,
            [f"*switch{entry['source_core']}*mem_events.jsonl",
             f"*core{entry['source_core']}*mem_events.jsonl"],
            f"core {core} mem_events stream")
        boundary_path = trace_dir / f"functional-boundary-core{entry['source_core']}.json"
        boundary = json.loads(boundary_path.read_text())
        if int(boundary["warmup_records"]) != entry["warmup_records"]:
            raise ValueError(
                f"core {core} manifest/boundary warmup record mismatch")
        end_tick = first_measurement_commit_tick(
            labels, entry["warmup_records"])
        if end_tick < begin_tick:
            raise ValueError(
                f"core {core} first measurement record precedes WORKBEGIN")
        accesses, raw_commits, mmio_commits_excluded = extract_accesses(
            mem_events, entry["source_core"], begin_tick, end_tick,
            dram_size)
        state_path = output_dir / f"core{core}.boundary-memory"
        with state_path.open("w") as output:
            output.write(SCHEMA + "\n")
            for sequence, access in enumerate(accesses):
                operation = "W" if access["write"] else "R"
                output.write(
                    f"{sequence} 0x{access['physical_address']:x} "
                    f"{access['size']} {operation}\n")
        fields = list(entry["fields"])
        fields[1] = "fastsim-binary-warmup-state-slice"
        relative_state = os.path.relpath(state_path, output_manifest.parent)
        manifest_lines.append(" ".join(fields + [relative_state]))
        report["cores"].append({
            "core": core,
            "source_core": entry["source_core"],
            "first_measurement_commit_tick": end_tick,
            "boundary_gap_ticks": end_tick - begin_tick,
            "committed_accesses": len(accesses),
            "raw_commits_scanned": raw_commits,
            "mmio_commits_excluded": mmio_commits_excluded,
            "labels": str(labels),
            "labels_sha256": sha256(labels),
            "mem_events": str(mem_events),
            "mem_events_sha256": sha256(mem_events),
            "state": str(state_path),
            "state_sha256": sha256(state_path),
        })
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text("\n".join(manifest_lines) + "\n")
    report["output_manifest"] = str(output_manifest)
    report["output_manifest_sha256"] = sha256(output_manifest)
    report_path = output_dir / "build-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report_path)


if __name__ == "__main__":
    main()
