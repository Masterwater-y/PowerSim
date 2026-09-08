"""Canonical C4 workload selection and replay helpers."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON: {path}") from error
    if not isinstance(document, dict):
        raise ValueError(f"JSON object expected: {path}")
    return document


def load_workloads(path: Path) -> list[Workload]:
    document = _load_json(path.resolve())
    if document.get("cores") != CANONICAL_CORES:
        raise ValueError(f"workload descriptor must define C4: {path}")
    rows = document.get("workloads")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"workload descriptor has no workloads: {path}")
    workloads = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"invalid workload descriptor row: {path}")
        name = str(row.get("name", ""))
        if not name or name in seen:
            raise ValueError(f"invalid or duplicate workload name: {name}")
        argv = row.get("argv")
        environment = row.get("environment")
        if not isinstance(argv, list) or not all(
            isinstance(value, str) for value in argv
        ):
            raise ValueError(f"invalid argv for {name}: {path}")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError(f"invalid environment for {name}: {path}")
        run_directory = row.get("run_directory")
        if run_directory is not None and (
            not isinstance(run_directory, str) or not run_directory
        ):
            raise ValueError(f"invalid run_directory for {name}: {path}")
        workloads.append(
            Workload(
                name=name,
                binary=str(row["binary"]),
                argv=tuple(argv),
                environment=dict(environment),
                omp_threads=int(row["omp_threads"]),
                run_directory=run_directory,
            )
        )
        seen.add(name)
    return workloads


def selected_workloads(
    all_workloads: Sequence[Workload], requested: Sequence[str]
) -> list[Workload]:
    if not requested:
        return list(all_workloads)
    by_name = {item.name: item for item in all_workloads}
    missing = sorted(set(requested) - set(by_name))
    if missing:
        raise ValueError(f"unknown QEMU-FST workload(s): {', '.join(missing)}")
    return [by_name[name] for name in requested]


def fastsim_binary(configured: Path | None = None) -> Path:
    path = configured or PROJECT_ROOT / "build/fastsim"
    if path.is_file():
        return path.resolve()
    raise FileNotFoundError(f"FastSim binary is missing: {path}")


def run_fastsim(*, fastsim: Path, config: Path, manifest: Path,
                output: Path, log: Path, measurement_scope: str = "user") -> None:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = "/opt/gcc-11.5.0/lib64"
    result = subprocess.run(
        [str(fastsim), "simulate", "--config", str(config), "--manifest",
         str(manifest), "--measurement-scope", measurement_scope,
         "--output", str(output)],
        cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode or not output.is_file():
        raise RuntimeError(f"FastSim replay failed rc={result.returncode}: {log}")
