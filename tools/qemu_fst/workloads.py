"""Canonical C4 workload selection and replay helpers."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CORES = 4


@dataclass(frozen=True)
class Workload:
    name: str
    binary: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    omp_threads: int
    run_directory: str | None = None


def fastsim_binary(configured: Path | None = None) -> Path:
    path = configured or PROJECT_ROOT / "build/fastsim"
    if path.is_file():
        return path.resolve()
    raise FileNotFoundError(f"FastSim binary is missing: {path}")


def parse_memory_bytes(value: str) -> int:
    suffixes = {
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("memory size must not be empty")
    suffix = normalized[-1]
    if suffix in suffixes:
        number = normalized[:-1]
        scale = suffixes[suffix]
    else:
        number = normalized
        scale = 1
    if not number.isdigit() or int(number) <= 0:
        raise ValueError(f"invalid memory size: {value}")
    return int(number) * scale


def run_fastsim(*, fastsim: Path, config: Path, manifest: Path,
                output: Path, log: Path, dram_size: int,
                measurement_scope: str = "user") -> None:
    if dram_size <= 0:
        raise ValueError("FastSim DRAM size must be positive")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = "/opt/gcc-11.5.0/lib64"
    result = subprocess.run(
        [str(fastsim), "simulate", "--config", str(config), "--manifest",
         str(manifest), "--measurement-scope", measurement_scope,
         "--allow-mmio-escape", "true",
         "--dram-size", str(dram_size),
         "--output", str(output)],
        cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode or not output.is_file():
        raise RuntimeError(f"FastSim replay failed rc={result.returncode}: {log}")
