"""Build the four components required by the canonical QEMU-FST workflow."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
GEM5_ROOT = WORKSPACE_ROOT / "gem5_fastsim"
TAOTRACE_ROOT = WORKSPACE_ROOT / "gem5_taotrace_fst"
QEMU_ROOT = WORKSPACE_ROOT / "qemu"
TRACER_ROOT = WORKSPACE_ROOT / "qemu_tracer"
GEM5_COMPONENT = PROJECT_ROOT / "integrations/gem5/qemu_fst"
TAOTRACE_COMPONENT = PROJECT_ROOT / "integrations/gem5/taotrace"
GEM5_BUILD = GEM5_ROOT / "build/X86_QEMU_FST"
TAOTRACE_BUILD = TAOTRACE_ROOT / "build/X86_TAOTRACE_FST"
FASTSIM_BUILD = PROJECT_ROOT / "build"
COMPONENTS = ("fastsim", "producer", "gem5", "taotrace")
DEFAULT_JOBS = min(os.cpu_count() or 1, 24)
GEM5_BASE_COMMIT = "c8222cc67a399bfc01e8658dd14b30d5bfd634f9"
FASTSIM_UPSTREAM_COMMIT = "3fdc307d07e7209d723e0d637f31054ba32dc72d"
QEMU_UPSTREAM_COMMIT = "98b060da3a4f92b2a994ead5b16a87e783baf77c"
TRACER_UPSTREAM_COMMIT = "abd8dc2b2a4d09ceb93caf34b3932589f150bfd2"
QEMU_VERSION = "11.0.0"
QEMU_PLUGIN_VERSION = 6
GCC_BIN = Path("/opt/gcc-11.5.0/bin")
GCC_LIB = Path("/opt/gcc-11.5.0/lib64")
CANONICAL_PYTHON = WORKSPACE_ROOT / "py3.11/bin/python3.11"


def _run(command: Sequence[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    python_lib = Path(sysconfig.get_config_var("LIBDIR") or "")
    if not python_lib.is_dir():
        raise RuntimeError("Python runtime library directory is unavailable")
    env["CC"] = str(GCC_BIN / "gcc")
    env["CXX"] = str(GCC_BIN / "g++")
    env["LD_LIBRARY_PATH"] = (
        f"{python_lib}:{GCC_LIB}:{env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    env["LDFLAGS"] = " ".join(
        [
            env.get("LDFLAGS", ""),
            f"-Wl,-rpath,{python_lib}",
            f"-Wl,-rpath,{GCC_LIB}",
        ]
    ).strip()
    return env


def _require_canonical_python() -> None:
    if (
        not CANONICAL_PYTHON.is_file()
        or not os.path.samefile(sys.executable, CANONICAL_PYTHON)
    ):
        raise RuntimeError(
            "QEMU-FST build must run with the workspace Python "
            f"{CANONICAL_PYTHON}; observed {sys.executable}"
        )


def _require_base_commit(root: Path, commit: str, name: str) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{name} HEAD must descend from baseline {commit}"
        )


def _require_qemu_tracer_baselines(env: dict[str, str]) -> None:
    _require_base_commit(QEMU_ROOT, QEMU_UPSTREAM_COMMIT, "QEMU upstream")
    _require_base_commit(
        TRACER_ROOT, TRACER_UPSTREAM_COMMIT, "qemu_tracer upstream")
    version = (QEMU_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version != QEMU_VERSION:
        raise RuntimeError(
            f"QEMU version must be {QEMU_VERSION}, observed {version}"
        )
    glib_cflags = subprocess.run(
        ["pkg-config", "--cflags", "glib-2.0"],
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.split()
    source = (
        "#include <qemu-plugin.h>\n"
        "#include <qemu-fst-observation.h>\n"
        f"_Static_assert(QEMU_PLUGIN_VERSION == {QEMU_PLUGIN_VERSION}, "
        '"unexpected QEMU Plugin API");\n'
        "_Static_assert(QEMU_FST_X86_STATE_FIELD_COUNT == 125, "
        '"unexpected QEMU-FST state layout");\n'
    )
    subprocess.run(
        [
            env["CC"], "-std=gnu11", "-fsyntax-only",
            f"-I{QEMU_ROOT / 'include/plugins'}",
            *glib_cflags, "-x", "c", "-",
        ],
        env=env,
        input=source,
        text=True,
        check=True,
    )


def _build_fastsim(jobs: int, env: dict[str, str]) -> None:
    _run(
        [
            "cmake", "-S", str(PROJECT_ROOT), "-B", str(FASTSIM_BUILD),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_C_COMPILER={env['CC']}",
            f"-DCMAKE_CXX_COMPILER={env['CXX']}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
    )
    for target in ("fastsim", "fastsim_fst_writer", "fastsim_tests"):
        _run(
            [
                "cmake", "--build", str(FASTSIM_BUILD),
                "--target", target, "--parallel", str(jobs),
            ],
            cwd=PROJECT_ROOT,
            env=env,
        )
    _run(
        [str(FASTSIM_BUILD / "fastsim_tests")],
        cwd=PROJECT_ROOT,
        env=env,
    )


def _build_producer(jobs: int, env: dict[str, str]) -> None:
    producer_env = env.copy()
    producer_env["JOBS"] = str(jobs)
    _run(
        [
            "bash", str(TRACER_ROOT / "scripts/build_native_fst.sh"),
        ],
        cwd=TRACER_ROOT,
        env=producer_env,
    )


def _gem5_toolchain() -> tuple[Path, tuple[str, ...], Path, Path]:
    python = Path(sys.executable)
    if not python.is_file():
        raise FileNotFoundError(
            f"gem5 Python/SCons toolchain is missing: {python}"
        )
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
    else:
        # EXTRAS consumes this library's wire-format implementation as well
        # as its headers. An existing archive may predate a format change.
        _run(
            ["cmake", "--build", str(FASTSIM_BUILD), "--target",
             "fastsim_fst_writer", "--parallel", str(jobs)],
            cwd=PROJECT_ROOT,
            env=env,
        )
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
        common = ["--ignore-style", f"EXTRAS={GEM5_COMPONENT}"]
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


def _build_taotrace(jobs: int, env: dict[str, str]) -> None:
    if not (TAOTRACE_ROOT / ".git").exists():
        raise FileNotFoundError(
            f"not a TaoTrace gem5 checkout: {TAOTRACE_ROOT}"
        )
    _require_base_commit(
        TAOTRACE_ROOT, GEM5_BASE_COMMIT, "TaoTrace gem5 upstream",
    )
    build_options = TAOTRACE_COMPONENT / "build_opts/X86_TAOTRACE_FST"
    if not build_options.is_file():
        raise FileNotFoundError(
            f"TaoTrace build options are missing: {build_options}"
        )
    python, scons, python_config, python_libdir = _gem5_toolchain()
    taotrace_env = env.copy()
    taotrace_env["PYTHON"] = str(python)
    taotrace_env["PYTHON_CONFIG"] = str(python_config)
    taotrace_env["LD_LIBRARY_PATH"] = (
        f"{python_libdir}:{taotrace_env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    taotrace_env["LINKFLAGS_EXTRA"] = " ".join(
        [
            taotrace_env.get("LINKFLAGS_EXTRA", ""),
            f"-Wl,-rpath,{python_libdir}",
            "-Wl,-rpath,/opt/gcc-11.5.0/lib64",
        ]
    ).strip()
    build_dir = TAOTRACE_ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    lock = build_dir / ".taotrace.lock"
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _run(
            [
                *scons, "--config=force", "defconfig",
                str(TAOTRACE_BUILD), str(build_options), "--ignore-style",
            ],
            cwd=TAOTRACE_ROOT,
            env=taotrace_env,
        )
        _run(
            [
                *scons, str(TAOTRACE_BUILD / "gem5.opt"),
                "--ignore-style", f"-j{jobs}",
            ],
            cwd=TAOTRACE_ROOT,
            env=taotrace_env,
        )


def run(args) -> int:
    requested = tuple(dict.fromkeys(args.component)) or COMPONENTS
    _require_canonical_python()
    env = _environment()
    _require_base_commit(
        PROJECT_ROOT, FASTSIM_UPSTREAM_COMMIT, "FastSim upstream")
    unknown = set(requested).difference(
        {"fastsim", "producer", "gem5", "taotrace"}
    )
    if unknown:
        raise ValueError(
            f"unknown QEMU-FST build component(s): {sorted(unknown)}"
        )
    if any(component in requested for component in ("producer", "gem5")):
        _require_qemu_tracer_baselines(env)
    builders = {
        "fastsim": _build_fastsim,
        "producer": _build_producer,
        "gem5": _build_gem5,
        "taotrace": _build_taotrace,
    }
    for component in ("fastsim", "producer", "gem5", "taotrace"):
        if component in requested:
            builders[component](args.jobs, env)
    return 0
