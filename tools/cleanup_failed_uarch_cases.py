#!/usr/bin/env python3
"""Safely remove only failed/incomplete artifacts from a uarch collection."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENTRY_RE = re.compile(r"^\[gem5-fs-roi\] entry=(.+)$", re.MULTILINE)
OUTDIR_RE = re.compile(r"(?:^|\s)-d\s+([^\s'\"]+)", re.MULTILINE)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def disk_usage(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    result = subprocess.run(
        ["du", "-sb", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return int(result.stdout.split()[0])


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    checkpoint_root = args.checkpoint_root.resolve()
    status_path = run_root / "status.json"
    state = load(status_path)
    failed_keys = sorted(
        key
        for key, row in state.get("tasks", {}).items()
        if row.get("status") in {"failed", "cancelled"}
    )
    if not failed_keys:
        print(json.dumps({"failed_cases": 0, "paths": 0, "bytes": 0}))
        return 0

    targets: set[Path] = set()
    checkpoint_entries: set[Path] = set()
    for key in failed_keys:
        parts = key.split("/")
        if len(parts) != 3 or not re.fullmatch(r"c\d+", parts[1]):
            raise ValueError(f"unsafe task key in status: {key!r}")
        profile, core_dir, workload = parts
        case_root = run_root / "cases" / profile / core_dir / workload
        driver_root = run_root / "driver-tmp" / profile / core_dir / workload
        trace_root = run_root / "trace-scratch" / profile / core_dir / workload
        quarantine_root = (
            run_root / "quarantined-checkpoints" / profile / core_dir / workload
        )
        for path in (case_root, driver_root, trace_root, quarantine_root):
            if within(path, run_root):
                targets.add(path.resolve())

        record_path = case_root / "case.json"
        if record_path.is_file():
            result_dir = load(record_path).get("result_dir")
            if result_dir:
                result_path = Path(str(result_dir)).resolve()
                if within(result_path, run_root / "source"):
                    targets.add(result_path)

        collector_log = case_root / "collector.log"
        if not collector_log.is_file():
            continue
        text = collector_log.read_text(encoding="utf-8", errors="replace")
        for raw in OUTDIR_RE.findall(text):
            outdir = Path(raw).resolve()
            if within(outdir, run_root / "source" / "sample"):
                targets.add(outdir)
        for raw in ENTRY_RE.findall(text):
            entry = Path(raw.strip()).resolve()
            if not within(entry, checkpoint_root):
                raise ValueError(f"refusing checkpoint outside root: {entry}")
            checkpoint_entries.add(entry)
            relative = entry.relative_to(checkpoint_root)
            if len(relative.parts) == 4:
                profile_key, cores, entry_workload, cache_key = relative.parts
                prepare_log = (
                    run_root / "source" / "prepare" / profile_key / cores /
                    entry_workload / f"{cache_key}.log"
                )
                if within(prepare_log, run_root / "source" / "prepare"):
                    targets.add(prepare_log.resolve())

    preserved_complete_checkpoints = []
    for entry in checkpoint_entries:
        checkpoint = entry / "checkpoint"
        complete = (
            (checkpoint / "m5.cpt").is_file()
            and (checkpoint / "metadata.json").is_file()
        )
        if complete:
            preserved_complete_checkpoints.append(str(entry))
        else:
            targets.add(entry)

    existing = sorted(
        (path for path in targets if path.exists() or path.is_symlink()),
        key=lambda path: (len(path.parts), str(path)),
    )
    pruned: list[Path] = []
    for path in existing:
        if any(within(path, parent) for parent in pruned):
            continue
        pruned.append(path)
    sizes = {str(path): disk_usage(path) for path in pruned}
    total_bytes = sum(sizes.values())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = run_root / f"cleanup-failed-cases-{stamp}.json"
    report = {
        "schema": "fastsim-uarch-failed-cleanup-v1",
        "applied": bool(args.apply),
        "failed_cases": failed_keys,
        "paths": sizes,
        "total_bytes": total_bytes,
        "preserved_complete_checkpoints": preserved_complete_checkpoints,
    }
    atomic_json(report_path, report)

    if args.apply:
        for path in sorted(pruned, key=lambda item: len(item.parts), reverse=True):
            remove(path)
        for key in failed_keys:
            state["tasks"].pop(key, None)
        state.pop("summary", None)
        state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        state["last_failed_cleanup"] = str(report_path)
        atomic_json(status_path, state)

    print(json.dumps({
        "applied": bool(args.apply),
        "failed_cases": len(failed_keys),
        "paths": len(pruned),
        "bytes": total_bytes,
        "report": str(report_path),
        "preserved_complete_checkpoints": len(preserved_complete_checkpoints),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
