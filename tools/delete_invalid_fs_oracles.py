#!/usr/bin/env python3
"""Delete only FS result directories proven invalid by an identity audit."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    audit = json.loads(args.audit.resolve().read_text(encoding="utf-8"))
    root = args.result_root.resolve()
    if audit.get("valid_cases", -1) != 0 or not audit.get("mismatched_cases"):
        raise SystemExit("audit is not an all-mismatched invalid-data set")

    targets = []
    for row in audit.get("results", []):
        if row.get("valid") is not False or not row.get("mismatches"):
            raise SystemExit("audit contains a result not proven mismatched")
        target = Path(row["result_dir"]).resolve()
        if not contained(target, root) or target == root:
            raise SystemExit(f"unsafe or incomplete deletion target: {target}")
        if target.exists() and (
            not (target / "request.json").is_file()
            or not (target / "tao_trace/uarch_profile.json").is_file()
        ):
            raise SystemExit(f"existing deletion target lost its proof files: {target}")
        targets.append(target)
    targets = sorted(set(targets))
    if len(targets) != int(audit["mismatched_cases"]):
        raise SystemExit("deduplicated target count disagrees with audit")
    if any(left != right and contained(right, left) for left in targets for right in targets):
        raise SystemExit("nested deletion targets are forbidden")

    rows = []
    for target in targets:
        exists = target.exists()
        rows.append({
            "result_dir": str(target),
            "bytes_at_start": tree_size(target) if exists else 0,
            "status": "pending" if exists else "already_missing",
        })
    if args.execute:
        remaining = sum(row["status"] == "pending" for row in rows)
        completed = 0
        for target, row in zip(targets, rows):
            if row["status"] != "pending":
                continue
            shutil.rmtree(target)
            row["status"] = "deleted"
            completed += 1
            print(f"deleted {completed}/{remaining} {target}", flush=True)
    report = {
        "schema": "fastsim-invalid-fs-oracle-deletion-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "executed": bool(args.execute),
        "audit": str(args.audit.resolve()),
        "result_root": str(root),
        "targets": rows,
        "totals": {
            "directories": len(rows),
            "already_missing": sum(
                row["status"] == "already_missing" for row in rows
            ),
            "deleted": sum(row["status"] == "deleted" for row in rows),
            "pending": sum(row["status"] == "pending" for row in rows),
            "bytes_at_start": sum(row["bytes_at_start"] for row in rows),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["totals"], sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
