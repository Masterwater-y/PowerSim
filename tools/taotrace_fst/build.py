"""Apply and build the canonical FastSim TaoTrace gem5 producer."""
from __future__ import annotations

import argparse
import os
import subprocess
import sysconfig
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
GEM5_ROOT = WORKSPACE_ROOT / "gem5_taotrace"
GEM5_BASE_COMMIT = "c8222cc67a399bfc01e8658dd14b30d5bfd634f9"
PATCH = PROJECT_ROOT / "patches/gem5-taotrace-fastsim.patch"
BUILD = GEM5_ROOT / "build/X86_TAOTRACE_FST"
BUILD_CONFIG = GEM5_ROOT / "build/X86_TAOTRACE_FST/gem5.build/config"
BUILD_OPTS = (
    PROJECT_ROOT / "tools/taotrace_fst/build_opts/X86_TAOTRACE_FST"
)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=GEM5_ROOT, env=env, check=True)


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=GEM5_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _patch_applies(reverse: bool) -> bool:
    command = ["git", "apply", "--check"]
    if reverse:
        command.append("--reverse")
    command.append(str(PATCH))
    return subprocess.run(
        command,
        cwd=GEM5_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _prepare_source(apply: bool) -> None:
    if _head() != GEM5_BASE_COMMIT:
        raise RuntimeError(
            f"TaoTrace requires gem5 base {GEM5_BASE_COMMIT}"
        )
    if _patch_applies(reverse=True):
        return
    if not apply:
        raise RuntimeError(
            "gem5_taotrace does not exactly materialize the canonical patch; "
            "use a clean base checkout with --apply"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=GEM5_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise RuntimeError(
            "--apply requires a clean tracked gem5 base checkout"
        )
    if not _patch_applies(reverse=False):
        raise RuntimeError("canonical TaoTrace patch does not apply cleanly")
    _run(["git", "apply", str(PATCH)])
    if not _patch_applies(reverse=True):
        raise RuntimeError("applied TaoTrace source does not match the patch")


def _environment() -> tuple[dict[str, str], Path]:
    env = os.environ.copy()
    gcc_bin = Path("/opt/gcc-11.5.0/bin")
    gcc_lib = Path("/opt/gcc-11.5.0/lib64")
    python = PROJECT_ROOT / ".gem5_build_env/venv/bin/python"
    if not python.is_file():
        raise FileNotFoundError(f"gem5 Python toolchain missing: {python}")
    values = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig,pathlib;"
            "print(pathlib.Path(sysconfig.get_config_var('BINDIR'))/"
            "'python3.11-config');"
            "print(sysconfig.get_config_var('LIBDIR'))",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    python_config = Path(values[0])
    python_lib = Path(values[1])
    env["CC"] = str(gcc_bin / "gcc")
    env["CXX"] = str(gcc_bin / "g++")
    env["PYTHON"] = str(python)
    env["PYTHON_CONFIG"] = str(python_config)
    env["TAOGEN_SHARED"] = str(
        GEM5_ROOT / "src/cpu/o3/probe/taogen_shared"
    )
    env["LD_LIBRARY_PATH"] = (
        f"{python_lib}:{gcc_lib}:{env.get('LD_LIBRARY_PATH', '')}"
    ).rstrip(":")
    env["LINKFLAGS_EXTRA"] = (
        f"-Wl,-rpath,{python_lib} -Wl,-rpath,{gcc_lib}"
    )
    return env, python


def run(args: argparse.Namespace) -> int:
    _prepare_source(args.apply)
    if args.check_only:
        return 0
    env, python = _environment()
    if not BUILD_CONFIG.is_file():
        _run(
            [
                str(python),
                "-m",
                "SCons",
                "defconfig",
                str(BUILD),
                str(BUILD_OPTS),
            ],
            env=env,
        )
    _run(
        [
            str(python),
            "-m",
            "SCons",
            str(BUILD / "gem5.fast"),
            f"-j{args.jobs}",
        ],
        env=env,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.taotrace_fst.build")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--jobs", type=int, default=min(os.cpu_count() or 1, 24)
    )
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    if args.apply and args.check_only:
        parser.error("--apply and --check-only are mutually exclusive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
