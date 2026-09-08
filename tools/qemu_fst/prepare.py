"""Prepare the canonical SPEC CPU 2026 QEMU-FST workload assets."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from ._assets import (
    ASSET_ROOT,
    USER_ONLY_WORKLOAD_DISK_NAME,
    build_user_assets,
)
from ._initramfs import INITRAMFS_NAME
from .workloads import PROJECT_ROOT, Workload, load_workloads


DEFAULT_DESCRIPTOR = PROJECT_ROOT / "configs/qemu_fst/spec2026_c4.json"
DEFAULT_SOURCE_ROOT = Path(
    "/data00/yinhaolang/TCSim/workloads/spec2026/benchspec/CPU"
)
LOCAL_ROOT = PROJECT_ROOT / "var/qemu_fst/workloads"
BUILD_ROOT = LOCAL_ROOT / "build"
RUN_ROOT = LOCAL_ROOT / "run"
MARKER_HEADER = PROJECT_ROOT / "tools/qemu_fst/resources/gem5_roi_marker.h"
HISTORICAL_BUILD_DIRECTORY = "build_base_gem5-x86-linux.0000"
HISTORICAL_RUN_DIRECTORY = "run_base_test_gem5-x86-linux.0000"
HISTORICAL_CONFIG_ROOT = (
    "/data00/yinhaolang/TCSim/workloads/spec2026/config"
)


def _descriptor_rows(path: Path) -> dict[str, dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row["name"]): row
        for row in document["workloads"]
    }


def _ignore_build_artifacts(
    source_binaries: set[str],
):
    def ignore(_directory: str, names: list[str]) -> set[str]:
        ignored = {
            name
            for name in names
            if name.endswith((".o", ".a", ".so"))
            or name in source_binaries
        }
        return ignored

    return ignore


def _copy_tree(source: Path, destination: Path, *, force: bool,
               ignore=None) -> None:
    if destination.exists():
        if not force:
            raise FileExistsError(
                f"local SPEC workload tree exists: {destination}"
            )
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def _build_commands(build_dir: Path) -> list[str]:
    source = build_dir / "make.out"
    commands = [
        line.replace(HISTORICAL_CONFIG_ROOT, str(MARKER_HEADER.parent), 1)
        for line in source.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        if line.startswith("/opt/gcc-11.5.0/bin/gcc")
        or line.startswith("/opt/gcc-11.5.0/bin/g++")
    ]
    if not commands:
        raise ValueError(f"historical build commands are absent: {source}")
    return commands


def _is_static_elf(path: Path) -> bool:
    output = subprocess.run(
        ["file", "-b", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    return "ELF 64-bit" in output and "statically linked" in output


def materialize_spec2026(
    workloads: Sequence[Workload],
    *,
    descriptor: Path = DEFAULT_DESCRIPTOR,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    force: bool = False,
) -> list[Path]:
    """Copy historical build/run trees and build independent QEMU ELFs."""
    if not MARKER_HEADER.is_file():
        raise FileNotFoundError(MARKER_HEADER)
    rows = _descriptor_rows(descriptor)
    source_binaries = {
        str(row["source_binary"]) for row in rows.values()
    }
    outputs = []
    for workload in workloads:
        row = rows.get(workload.name)
        if row is None:
            raise ValueError(
                f"SPEC build metadata is missing for {workload.name}"
            )
        source_binary = str(row["source_binary"])
        historical = source_root / workload.name
        source_build = historical / "build" / HISTORICAL_BUILD_DIRECTORY
        source_run = historical / "run" / HISTORICAL_RUN_DIRECTORY
        if not source_build.is_dir() or not source_run.is_dir():
            raise FileNotFoundError(
                f"historical SPEC trees are incomplete: {historical}"
            )

        build_dir = BUILD_ROOT / workload.name
        run_dir = RUN_ROOT / workload.name
        published = run_dir / workload.binary
        if not force and run_dir.is_dir() and published.is_file() and (
            _is_static_elf(published)
        ):
            outputs.append(published)
            continue
        if not force and (build_dir.exists() or run_dir.exists()):
            raise FileExistsError(
                f"incomplete local SPEC workload tree: {workload.name}"
            )
        _copy_tree(
            source_build,
            build_dir,
            force=True,
            ignore=_ignore_build_artifacts(source_binaries),
        )
        _copy_tree(
            source_run,
            run_dir,
            force=True,
            ignore=lambda _directory, names: {
                name for name in names
                if name.endswith("_base.gem5-x86-linux")
            },
        )
        commands = _build_commands(build_dir)
        build_script = build_dir / "build-qemu-elf.sh"
        build_script.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            + "\n".join(commands)
            + "\n",
            encoding="utf-8",
        )
        build_script.chmod(0o755)
        log = build_dir / "build-qemu-elf.log"
        with log.open("w", encoding="utf-8") as output:
            subprocess.run(
                [str(build_script)],
                cwd=build_dir,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
        built = build_dir / source_binary
        if not built.is_file() or not _is_static_elf(built):
            raise RuntimeError(
                f"independent static QEMU ELF was not built: {built}"
            )
        shutil.copy2(built, published)
        published.chmod(0o755)
        outputs.append(published)
    return outputs


def run(_args) -> int:
    workloads = load_workloads(DEFAULT_DESCRIPTOR)
    binaries = materialize_spec2026(workloads)
    initramfs = ASSET_ROOT / INITRAMFS_NAME
    workload_disk = ASSET_ROOT / USER_ONLY_WORKLOAD_DISK_NAME
    if initramfs.is_file() and initramfs.stat().st_size > 0 and (
        workload_disk.is_file() and workload_disk.stat().st_size > 0
    ):
        prepared = (initramfs, workload_disk)
    elif initramfs.exists() or workload_disk.exists() or ASSET_ROOT.exists():
        raise FileExistsError(f"incomplete QEMU-FST asset root: {ASSET_ROOT}")
    else:
        prepared = build_user_assets(workloads, destination=ASSET_ROOT)
    for binary in binaries:
        print(binary)
    print(f"initramfs: {prepared[0]}")
    print(f"workload disk: {prepared[1]}")
    return 0
