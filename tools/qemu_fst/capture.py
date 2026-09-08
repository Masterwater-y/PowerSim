"""Drive one isolated QEMU full-system capture from local canonical assets."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .workloads import Workload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 28_800
MAX_CAPTURE_ATTEMPTS = 2


@dataclass(frozen=True)
class FstAssets:
    """Explicit QEMU-FST inputs owned by this workspace."""
    kernel: Path
    initramfs: Path
    workload_disk: Path
    qemu: Path
    plugin: Path
    launcher: Path

    def require(self) -> None:
        for name, path in (
            ("kernel", self.kernel), ("initramfs", self.initramfs),
            ("workload disk", self.workload_disk),
            ("qemu", self.qemu), ("plugin", self.plugin), ("launcher", self.launcher),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"QEMU-FST {name} is not a file: {path}")


@dataclass(frozen=True)
class CaptureResult:
    workload: str
    trace_dir: Path
    log: Path
    attempt: int
    raw_macro_envelope: int


def collect_workload(
    *,
    workload: Workload,
    memory: str = "3G",
    timeout_seconds: int = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    force: bool = False,
    raw_macro_envelope: int,
    assets: FstAssets,
    output_dir: Path,
    qemu_library_dir: Path | None = None,
) -> CaptureResult:
    assets.require()
    if raw_macro_envelope <= 0:
        raise ValueError("QEMU-FST capture envelope must be positive")
    out = output_dir.resolve()
    env = os.environ.copy()
    if qemu_library_dir is not None:
        env["LD_LIBRARY_PATH"] = (
            f"{qemu_library_dir.resolve()}:{env.get('LD_LIBRARY_PATH', '')}"
        ).rstrip(":")
    for attempt in range(1, MAX_CAPTURE_ATTEMPTS + 1):
        if out.exists():
            if not force and attempt == 1:
                raise FileExistsError(
                    f"capture output already exists: {out} (use --force)"
                )
            _rm_tree(out)
        out.mkdir(parents=True)
        command = [
            "bash", str(assets.launcher),
            "--qemu", str(assets.qemu),
            "--plugin", str(assets.plugin),
            "--kernel", str(assets.kernel),
            "--initrd", str(assets.initramfs),
            "--workload-disk", str(assets.workload_disk),
            "--workload", workload.name,
            "--out", str(out),
            "--cores", "4",
            "--mem", memory,
            "--sampling", "hint",
            "--capture-user-instruction-limit",
            str(raw_macro_envelope),
            "--timeout-seconds", str(int(timeout_seconds)),
        ]
        result = subprocess.run(command, cwd=str(PROJECT_ROOT), env=env)
        log = out / "qemu-system.log"
        if result.returncode == 0 and _capture_completed(
            log, workload.name, out, expected_cores=4
        ):
            break
        if attempt == MAX_CAPTURE_ATTEMPTS:
            raise RuntimeError(
                f"QEMU-FST capture failed after {attempt} attempts "
                f"(rc={result.returncode}): see {log}"
            )
        time.sleep(1)
    return CaptureResult(
        workload=workload.name,
        trace_dir=out,
        log=out / "qemu-system.log",
        attempt=attempt,
        raw_macro_envelope=raw_macro_envelope,
    )


def _capture_completed(
    log: Path, workload_name: str, trace_dir: Path, *, expected_cores: int
) -> bool:
    if not log.is_file():
        return False
    output = log.read_text(encoding="utf-8", errors="replace")
    shards = sorted(trace_dir.glob("**/*.trace.gz"))
    return (
        f"[qemu-fst-init] workload={workload_name} uid=1000 gid=1000" in output
        and (trace_dir / ".capture-complete").is_file()
        and len(shards) == expected_cores
        and all(path.stat().st_size > 0 for path in shards)
    )


def _rm_tree(path: Path) -> None:
    shutil.rmtree(path)
