from __future__ import annotations

import os
import sysconfig
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TraceRuntime:
    gem5_root: Path
    libpython_dir: Path
    converter_path: Path | None = None
    detailed_gem5_path: Path | None = None

    @property
    def converter(self) -> Path:
        if self.converter_path is not None:
            return self.converter_path
        return self.gem5_root / "build" / "X86_DRMEMTRACE" / "gem5.fast"

    @property
    def detailed_gem5(self) -> Path:
        if self.detailed_gem5_path is not None:
            return self.detailed_gem5_path
        return self.gem5_root / "build" / "X86_MESI_Three_Level" / "gem5.opt"

    def runtime_environ(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        # Do not inherit an agent/toolchain LD_LIBRARY_PATH: an older injected
        # libstdc++ can make a correctly built gem5 fail before main().
        env["LD_LIBRARY_PATH"] = f"{self.libpython_dir}:/opt/gcc-11.5.0/lib64"
        return env


@dataclass(frozen=True)
class ValidationEnvironment:
    runtime: TraceRuntime
    dynamorio_root: Path

    @property
    def drrun(self) -> Path:
        return self.dynamorio_root / "bin64" / "drrun"


def load_trace_runtime(
    *,
    gem5_root: Path | None = None,
    converter: Path | None = None,
    detailed_gem5: Path | None = None,
) -> TraceRuntime:
    gem5_root = Path(
        gem5_root
        or os.environ.get("GEM5_ROOT", PROJECT_ROOT.parent / "gem5_fastsim")
    ).resolve()
    converter_path = Path(
        converter or os.environ.get("FASTSIM_DR_CONVERTER", "")
    ).resolve() if converter or os.environ.get("FASTSIM_DR_CONVERTER") else None
    detailed_gem5_path = Path(
        detailed_gem5 or os.environ.get("FASTSIM_DETAILED_GEM5", "")
    ).resolve() if detailed_gem5 or os.environ.get("FASTSIM_DETAILED_GEM5") else None
    libpython_dir = Path(str(sysconfig.get_config_var("LIBDIR") or "")).resolve()
    if not gem5_root.joinpath(".git").is_dir():
        raise FileNotFoundError(f"gem5 checkout does not exist: {gem5_root}")
    if not libpython_dir.is_dir():
        raise FileNotFoundError(f"libpython directory does not exist: {libpython_dir}")
    return TraceRuntime(
        gem5_root=gem5_root,
        libpython_dir=libpython_dir,
        converter_path=converter_path,
        detailed_gem5_path=detailed_gem5_path,
    )


def load_validation_environment(
    *,
    gem5_root: Path | None = None,
    converter: Path | None = None,
    detailed_gem5: Path | None = None,
    dynamorio_root: Path | None = None,
) -> ValidationEnvironment:
    runtime = load_trace_runtime(
        gem5_root=gem5_root,
        converter=converter,
        detailed_gem5=detailed_gem5,
    )
    dynamorio_root = Path(
        dynamorio_root
        or os.environ.get(
            "DYNAMORIO_ROOT",
            PROJECT_ROOT.parent / "DynamoRIO-Linux-11.3.0-1",
        )
    ).resolve()
    if not dynamorio_root.joinpath(
        "tools", "include", "drmemtrace"
    ).is_dir():
        raise FileNotFoundError(
            f"DynamoRIO root is invalid: {dynamorio_root}"
        )
    return ValidationEnvironment(
        runtime=runtime,
        dynamorio_root=dynamorio_root,
    )
