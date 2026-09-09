"""Package the local user-only workload disk."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .workloads import Workload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = PROJECT_ROOT / "var/qemu_fst/assets"
USER_ONLY_WORKLOAD_DISK_NAME = "user-only-workloads.ext4"


def _write_user_workload_disk(
    workloads: Sequence[Workload], staging: Path,
) -> Path:
    source_root = PROJECT_ROOT / "workloads/dr_validation/bin/qemu_fst"
    tree = staging / "tree"
    cases = tree / "cases"
    cases.mkdir(parents=True)
    for workload in workloads:
        descriptor = cases / workload.name
        if workload.run_directory is None:
            source = source_root / workload.binary
            if not source.is_file():
                raise FileNotFoundError(source)
            descriptor.mkdir()
            binary = descriptor / "program"
            shutil.copy2(source, binary)
        else:
            source = Path(workload.run_directory)
            if not source.is_absolute():
                source = PROJECT_ROOT / source
            if not source.is_dir():
                raise FileNotFoundError(source)
            shutil.copytree(source, descriptor, symlinks=True)
            binary = descriptor / workload.binary
            if not binary.is_file():
                raise FileNotFoundError(binary)
            (descriptor / "program").symlink_to(workload.binary)
        binary.chmod(0o755)
        (descriptor / "argv").write_bytes(
            b"\0".join(
                value.encode("utf-8")
                for value in (workload.binary, *workload.argv)
            ) + b"\0"
        )
        environment = {
            "PATH": "/bin:/sbin:/usr/bin:/usr/sbin",
            "HOME": "/work",
            "TMPDIR": "/work",
            "ACC_NUM_CORES": str(workload.omp_threads),
            "OMP_NUM_THREADS": str(workload.omp_threads),
            "OMP_THREAD_LIMIT": str(workload.omp_threads),
            "OMP_DYNAMIC": "false",
            "OMP_PROC_BIND": "true",
            "OMP_PLACES": "cores",
            "OMP_STACKSIZE": "120M",
            **workload.environment,
        }
        (descriptor / "env").write_bytes(
            b"\0".join(
                f"{key}={value}".encode("utf-8")
                for key, value in sorted(environment.items())
            ) + b"\0"
        )
        for path in descriptor.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_dir():
                path.chmod(path.stat().st_mode | 0o777)
            elif path == binary or path.stat().st_mode & 0o111:
                path.chmod(0o755)
            else:
                path.chmod(path.stat().st_mode | 0o666)
        descriptor.chmod(0o777)

    image = staging / USER_ONLY_WORKLOAD_DISK_NAME
    content_bytes = sum(
        path.stat().st_size
        for path in tree.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    image_bytes = max(
        64 * 1024 * 1024,
        ((content_bytes * 6 // 5 + 64 * 1024 * 1024 - 1)
         // (64 * 1024 * 1024))
        * (64 * 1024 * 1024),
    )
    with image.open("wb") as output:
        output.truncate(image_bytes)
    subprocess.run(
        [
            "mkfs.ext4", "-F", "-L", "qemu-user-fst",
            "-d", str(tree), str(image),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.rmtree(tree)
    return image


def build_workload_disk(
    workloads: Sequence[Workload],
    *,
    destination: Path = ASSET_ROOT,
) -> Path:
    staging = destination.with_name(f".{destination.name}.workload-staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        image = _write_user_workload_disk(workloads, staging)
        destination.mkdir(parents=True, exist_ok=True)
        published = destination / USER_ONLY_WORKLOAD_DISK_NAME
        os.replace(image, published)
        staging.rmdir()
        return published
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
