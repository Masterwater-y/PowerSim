"""Drive one isolated QEMU full-system capture from local canonical assets.

Lifecycle is managed through the official QEMUMachine/QMP API of the same v11
tree that builds qemu-system-x86_64 and qemu-img. The ROI phase is reported by
the producer through file markers under the dump directory
(.measurement-started for the warmup deadline, .capture-complete for orderly
shutdown), not by scraping the guest serial log. QMP only manages the process
lifecycle: it does not decide CPL, macro counts, or the records target.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .workloads import Workload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
QEMU_PYTHON_ROOT = WORKSPACE_ROOT / "qemu/python"
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 28_800


def _qemu_machine_class():
    """Import QEMUMachine from the v11 tree that provides qemu-system-x86_64."""
    build_site = sorted(
        (WORKSPACE_ROOT / "qemu/build/pyvenv/lib").glob(
            "python*/site-packages"
        )
    )
    for path in (QEMU_PYTHON_ROOT, *build_site):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from qemu.machine import QEMUMachine

    return QEMUMachine


@dataclass(frozen=True)
class FstAssets:
    """Explicit QEMU-FST inputs owned by this workspace."""
    kernel: Path
    rootfs: Path
    workload_disk: Path
    qemu: Path
    plugin: Path

    def require(self) -> None:
        for name, path in (
            ("kernel", self.kernel), ("rootfs", self.rootfs),
            ("workload disk", self.workload_disk),
            ("qemu", self.qemu),
            ("qemu-img", self.qemu.with_name("qemu-img")),
            ("plugin", self.plugin),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"QEMU-FST {name} is not a file: {path}")


@dataclass(frozen=True)
class CaptureResult:
    workload: str
    trace_dir: Path
    log: Path
    user_macro_target: int


def collect_workload(
    *,
    workload: Workload,
    memory: str = "3G",
    timeout_seconds: int = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    warmup_timeout_seconds: int = 600,
    kernel_args: Sequence[str] = (),
    network: str = "disabled",
    user_macro_target: int,
    assets: FstAssets,
    output_dir: Path,
) -> CaptureResult:
    assets.require()
    if user_macro_target <= 0:
        raise ValueError("QEMU-FST macro target must be positive")
    if warmup_timeout_seconds <= 0:
        raise ValueError("QEMU-FST warmup timeout must be positive")
    if not kernel_args or any(
        not isinstance(value, str) or not value for value in kernel_args
    ):
        raise ValueError("QEMU-FST kernel arguments must be non-empty strings")
    if network != "disabled":
        raise ValueError("QEMU-FST only supports a disabled guest network")
    if "/" in workload.name or ".." in workload.name:
        raise ValueError(f"invalid workload name: {workload.name}")
    out = output_dir.resolve()
    if out.exists():
        raise FileExistsError(f"capture output already exists: {out}")
    out.mkdir(parents=True)

    cores = 4
    log = out / "qemu-system.log"
    rootfs_overlay = out / "rootfs-overlay.qcow2"
    workload_overlay = out / "workload-overlay.qcow2"
    qemu_img = assets.qemu.with_name("qemu-img")
    _create_overlay(qemu_img, assets.rootfs, rootfs_overlay)
    _create_overlay(qemu_img, assets.workload_disk, workload_overlay)

    plugin_arg = (
        f"{assets.plugin},dump={out},sampling=hint,"
        "fst-native-kernel=true,trace-mem=true,trace-value=true,"
        f"user-macro-target={user_macro_target},"
        f"max-global-trace-refs={cores},exit-after-tracing=true"
    )
    cmdline = (
        f"{' '.join(kernel_args)} panic=-1 "
        "init=/usr/local/sbin/qemu-fst-guest-runner "
        f"QEMU_FST_WORKLOAD={workload.name}"
    )
    args = [
        "-L", str(assets.qemu.parent.parent / "pc-bios"),
        "-machine", "pc-i440fx-10.0",
        "-accel", "tcg,thread=multi",
        "-smp",
        f"cpus={cores},maxcpus={cores},sockets=1,cores={cores},threads=1",
        "-m", memory,
        "-kernel", str(assets.kernel),
        "-append", cmdline,
        "-drive",
        f"file={rootfs_overlay},if=ide,index=0,media=disk,"
        "format=qcow2,cache=none",
        "-drive",
        f"file={workload_overlay},if=ide,index=1,media=disk,"
        "format=qcow2,cache=none",
        "-nographic", "-nic", "none", "-no-reboot",
        "-cpu", "qemu64",
        "-rtc", "base=2026-01-01T00:00:00,clock=vm",
        "-serial", f"file:{log}",
        "-plugin", plugin_arg,
    ]

    QEMUMachine = _qemu_machine_class()
    complete_marker = out / ".capture-complete"
    common_end_marker = out / ".common-end-triggered"
    measurement_marker = out / ".measurement-started"
    failure: str | None = None
    machine = QEMUMachine(
        str(assets.qemu),
        args=args,
        base_temp_dir=str(out),
        log_dir=str(out),
        qmp_timer=min(timeout_seconds, 30),
    )
    started = time.monotonic()
    with machine as vm:
        vm.launch()
        deadline = started + timeout_seconds
        warmup_deadline = started + warmup_timeout_seconds
        while vm.is_running():
            if complete_marker.is_file():
                vm.qmp("quit")
                break
            if (
                not measurement_marker.is_file()
                and time.monotonic() >= warmup_deadline
            ):
                failure = f"warmup timed out after {warmup_timeout_seconds}s"
                break
            if time.monotonic() >= deadline:
                failure = f"capture timed out after {timeout_seconds}s"
                break
            time.sleep(0.1)
        if failure is not None:
            vm.kill()
        else:
            vm.wait()
        rc = vm.exitcode()

    if failure is not None or rc:
        raise RuntimeError(
            f"QEMU-FST capture failed ({failure or f'rc={rc}'}); "
            f"artifacts preserved at {out}"
        )
    if not _capture_completed(out, cores):
        raise RuntimeError(
            f"QEMU-FST capture produced no complete shard set at {out}"
        )
    # Success: only the transient overlays are removed.
    rootfs_overlay.unlink(missing_ok=True)
    workload_overlay.unlink(missing_ok=True)
    return CaptureResult(
        workload=workload.name,
        trace_dir=out,
        log=log,
        user_macro_target=user_macro_target,
    )


def _create_overlay(qemu_img: Path, base: Path, overlay: Path) -> None:
    overlay.unlink(missing_ok=True)
    subprocess.run(
        [
            str(qemu_img), "create", "-q", "-f", "qcow2", "-F", "raw",
            "-b", str(base), str(overlay),
        ],
        check=True,
    )


def _capture_completed(trace_dir: Path, expected_cores: int) -> bool:
    if not (trace_dir / ".capture-complete").is_file():
        return False
    shards = sorted(trace_dir.glob("**/*.trace.gz"))
    return (
        len(shards) == expected_cores
        and all(path.stat().st_size > 0 for path in shards)
    )
