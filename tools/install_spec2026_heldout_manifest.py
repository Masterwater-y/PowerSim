#!/usr/bin/env python3

"""Install the curated SPEC CPU 2026 heldout entries into TCSim's manifest."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", type=Path)
    parser.add_argument("overlay", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    return parser.parse_args()


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise SystemExit(f"unsupported manifest schema in {path}")
    if not isinstance(manifest.get("workloads"), dict):
        raise SystemExit(f"missing workloads object in {path}")
    return manifest


def main() -> None:
    args = parse_args()
    base = load_manifest(args.base)
    overlay = load_manifest(args.overlay)

    remove_workloads = overlay.get("remove_workloads", [])
    if not isinstance(remove_workloads, list) or not all(
        isinstance(name, str) and name for name in remove_workloads
    ):
        raise SystemExit(f"invalid remove_workloads list in {args.overlay}")
    overlap = sorted(set(remove_workloads) & set(overlay["workloads"]))
    if overlap:
        raise SystemExit(
            "workloads cannot be both removed and installed: " + ", ".join(overlap)
        )

    replace_workloads = overlay.get("replace_workloads", [])
    if not isinstance(replace_workloads, list) or not all(
        isinstance(name, str) and name for name in replace_workloads
    ):
        raise SystemExit(f"invalid replace_workloads list in {args.overlay}")
    unknown_replacements = sorted(set(replace_workloads) - set(overlay["workloads"]))
    if unknown_replacements:
        raise SystemExit(
            "replace_workloads entries must also be installed: "
            + ", ".join(unknown_replacements)
        )
    replace_workloads = set(replace_workloads)

    removed = [name for name in remove_workloads if name in base["workloads"]]
    for name in remove_workloads:
        base["workloads"].pop(name, None)

    conflicts = []
    for name, entry in overlay["workloads"].items():
        existing = base["workloads"].get(name)
        if existing is not None and existing != entry and name not in replace_workloads:
            conflicts.append(name)
    if conflicts:
        joined = ", ".join(sorted(conflicts))
        raise SystemExit(
            f"refusing to replace conflicting TCSim workload entries: {joined}"
        )

    base["workloads"].update(overlay["workloads"])
    temp_dir = args.temp_dir or args.overlay.parent.parent / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = temp_dir / f"{args.base.name}.heldout.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(base, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        try:
            os.replace(temporary, args.base)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            # FastSim and TCSim may be separate managed mounts.  Keep the
            # temporary file under FastSim/tmp as required, then copy across
            # the mount boundary and verify the installed bytes.
            shutil.copyfile(temporary, args.base)
            if args.base.read_bytes() != temporary.read_bytes():
                raise SystemExit(f"manifest copy verification failed: {args.base}")
    finally:
        if temporary.exists():
            temporary.unlink()
    print(
        "[spec2026-heldout] manifest entries installed: "
        + ", ".join(sorted(overlay["workloads"]))
    )
    if removed:
        print(
            "[spec2026-heldout] deprecated entries removed: "
            + ", ".join(sorted(removed))
        )


if __name__ == "__main__":
    main()
