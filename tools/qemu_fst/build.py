"""Build the four artifacts required by the canonical QEMU-FST workflow."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sysconfig
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
GEM5_ROOT = WORKSPACE_ROOT / "gem5_fastsim"
QEMU_ROOT = WORKSPACE_ROOT / "qemu_tracer_qemu"
TRACER_ROOT = WORKSPACE_ROOT / "qemu_tracer"
QEMU_LOCAL_DEPS = WORKSPACE_ROOT / "qemu-local-deps/glib-2.66.8"
GEM5_COMPONENT = PROJECT_ROOT / "integrations/gem5/qemu_fst"
GEM5_BUILD = GEM5_ROOT / "build/X86_QEMU_FST"
FASTSIM_BUILD = PROJECT_ROOT / "build"
COMPONENTS = ("fastsim", "qemu", "dumper", "gem5")
DEFAULT_JOBS = min(os.cpu_count() or 1, 24)
GEM5_BASE_COMMIT = "c8222cc67a399bfc01e8658dd14b30d5bfd634f9"


def _run(command: Sequence[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    gcc_bin = Path("/opt/gcc-11.5.0/bin")
    gcc_lib = Path("/opt/gcc-11.5.0/lib64")
    python_lib = Path(sysconfig.get_config_var("LIBDIR") or "")
    if not python_lib.is_dir():
        raise RuntimeError("Python runtime library directory is unavailable")
    env.setdefault("CC", str(gcc_bin / "gcc"))
    env.setdefault("CXX", str(gcc_bin / "g++"))
    env["LD_LIBRARY_PATH"] = (
        f"{python_lib}:{gcc_lib}:{env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    env["LDFLAGS"] = " ".join(
        [
            env.get("LDFLAGS", ""),
            f"-Wl,-rpath,{python_lib}",
            f"-Wl,-rpath,{gcc_lib}",
        ]
    ).strip()
    return env


def _build_fastsim(jobs: int, env: dict[str, str]) -> None:
    _run(
        [
            "cmake", "-S", str(PROJECT_ROOT), "-B", str(FASTSIM_BUILD),
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=PROJECT_ROOT,
        env=env,
    )
    for target in ("fastsim", "fastsim_fst_writer"):
        _run(
            [
                "cmake", "--build", str(FASTSIM_BUILD),
                "--target", target, "--parallel", str(jobs),
            ],
            cwd=PROJECT_ROOT,
            env=env,
        )


def _build_qemu(jobs: int, env: dict[str, str]) -> None:
    build_dir = QEMU_ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    pkgconfig = QEMU_LOCAL_DEPS / "lib/pkgconfig"
    libdir = QEMU_LOCAL_DEPS / "lib"
    if not pkgconfig.is_dir() or not libdir.is_dir():
        raise FileNotFoundError(
            f"QEMU local GLib dependency is missing: {QEMU_LOCAL_DEPS}"
        )
    qemu_env = env.copy()
    qemu_env["PKG_CONFIG_PATH"] = (
        f"{pkgconfig}:{qemu_env.get('PKG_CONFIG_PATH', '')}"
    ).rstrip(":")
    qemu_env["LD_LIBRARY_PATH"] = (
        f"{libdir}:{qemu_env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    qemu_env["LDFLAGS"] = " ".join(
        [qemu_env.get("LDFLAGS", ""), f"-Wl,-rpath,{libdir}"]
    ).strip()
    _run(
        [
            str(QEMU_ROOT / "configure"),
            "--target-list=x86_64-softmmu",
            "--enable-plugins",
            "--disable-werror",
            "--disable-dbus-display",
        ],
        cwd=build_dir,
        env=qemu_env,
    )
    _run(
        ["ninja", "-C", str(build_dir), "-j", str(jobs),
         "qemu-system-x86_64"],
        cwd=QEMU_ROOT,
        env=qemu_env,
    )


def _build_dumper(jobs: int, env: dict[str, str]) -> None:
    _run(
        [
            "make", "-C", str(TRACER_ROOT / "backend/dumper"),
            f"-j{jobs}", f"QEMU_SOURCE={QEMU_ROOT}",
            f"CC={env['CC']}", f"CXX={env['CXX']}",
            f"LDFLAGS={env['LDFLAGS']} -lstdc++fs",
        ],
        cwd=TRACER_ROOT,
        env=env,
    )


def _gem5_toolchain() -> tuple[Path, tuple[str, ...], Path, Path]:
    venv = PROJECT_ROOT / ".gem5_build_env/venv"
    python = venv / "bin/python"
    if not python.is_file():
        raise FileNotFoundError(f"gem5 Python/SCons toolchain is missing: {venv}")
    subprocess.run(
        [str(python), "-m", "SCons", "--version"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    scons_command = (str(python), "-m", "SCons")
    paths = subprocess.run(
        [
            str(python), "-c",
            "import sys,sysconfig;"
            "print(f'{sys.version_info.major}.{sys.version_info.minor}');"
            "print(sysconfig.get_config_var('BINDIR') or '');"
            "print(sysconfig.get_config_var('LIBDIR') or '')",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    if len(paths) != 3 or paths[0] != "3.11":
        raise RuntimeError("gem5 build requires the workspace Python 3.11 toolchain")
    python_config = Path(paths[1]) / "python3.11-config"
    python_libdir = Path(paths[2])
    if not python_config.is_file() or not python_libdir.is_dir():
        raise RuntimeError("gem5 Python 3.11 toolchain is incomplete")
    return python, scons_command, python_config, python_libdir


def _require_pristine_gem5_checkout() -> None:
    if not (GEM5_ROOT / ".git").exists():
        raise FileNotFoundError(f"not a gem5 checkout: {GEM5_ROOT}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=GEM5_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if head != GEM5_BASE_COMMIT:
        raise RuntimeError(
            "QEMU-FST EXTRAS requires the pristine gem5 v25.1.0.1 checkout "
            f"{GEM5_BASE_COMMIT}; observed {head}"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=GEM5_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise RuntimeError(
            "QEMU-FST EXTRAS must not share a modified gem5 source tree; "
            "keep the TaoTrace reference overlay in a separate checkout"
        )


def _build_gem5(jobs: int, env: dict[str, str]) -> None:
    _require_pristine_gem5_checkout()
    writer = FASTSIM_BUILD / "libfastsim_fst_writer.a"
    if not writer.is_file():
        _build_fastsim(jobs, env)
    python, scons, python_config, python_libdir = _gem5_toolchain()
    gem5_env = env.copy()
    gem5_env["PYTHON"] = str(python)
    gem5_env["PYTHON_CONFIG"] = str(python_config)
    gem5_env["LD_LIBRARY_PATH"] = (
        f"{python_libdir}:{gem5_env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    gem5_env["LINKFLAGS_EXTRA"] = " ".join(
        [
            gem5_env.get("LINKFLAGS_EXTRA", ""),
            f"-Wl,-rpath,{python_libdir}",
            "-Wl,-rpath,/opt/gcc-11.5.0/lib64",
        ]
    ).strip()
    build_dir = GEM5_ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    lock = build_dir / ".qemu_fst.lock"
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        common = [f"EXTRAS={GEM5_COMPONENT}"]
        _run(
            [
                *scons, "--config=force", "defconfig",
                str(GEM5_BUILD),
                str(GEM5_COMPONENT / "build_opts/X86_QEMU_FST"),
                *common,
            ],
            cwd=GEM5_ROOT,
            env=gem5_env,
        )
        _run(
            [
                *scons, str(GEM5_BUILD / "gem5.fast"),
                *common,
                f"FASTSIM_INCLUDE_ROOT={PROJECT_ROOT / 'include'}",
                f"FASTSIM_WRITER_LIB={writer}",
                f"QEMU_TRACE_INCLUDE_ROOT={TRACER_ROOT}",
                f"-j{jobs}",
            ],
            cwd=GEM5_ROOT,
            env=gem5_env,
        )


def run(args) -> int:
    requested = tuple(dict.fromkeys(args.component)) or COMPONENTS
    env = _environment()
    builders = {
        "fastsim": _build_fastsim,
        "qemu": _build_qemu,
        "dumper": _build_dumper,
        "gem5": _build_gem5,
    }
    for component in COMPONENTS:
        if component in requested:
            builders[component](args.jobs, env)
    return 0
