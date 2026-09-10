"""Canonical workload and orchestration configuration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from tools.qemu_fst.workloads import Workload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DESCRIPTOR = PROJECT_ROOT / "configs/fst_pipeline/spec2026_c4.json"


@dataclass(frozen=True)
class Pipeline:
    path: Path
    cores: int
    user_fst_target: int
    raw_macro_envelope: int
    warmup_timeout_seconds: int
    total_timeout_seconds: int
    measurement_scope: str
    environment: dict[str, Any]
    workloads: tuple[Workload, ...]
    pilots: tuple[str, ...]
    reference_unavailable: tuple[dict[str, str], ...]

    @property
    def memory(self) -> str:
        return str(self.environment["memory"])

    @property
    def kernel_args(self) -> tuple[str, ...]:
        return tuple(str(value) for value in self.environment["kernel_args"])

    @property
    def isa_baseline(self) -> str:
        return str(self.environment["isa_baseline"])

    @property
    def timezone(self) -> str:
        return str(self.environment["timezone"])

    @property
    def network(self) -> str:
        return str(self.environment["network"])


def _positive(document: dict[str, Any], key: str) -> int:
    value = int(document[key])
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def load_pipeline(path: Path = DEFAULT_DESCRIPTOR) -> Pipeline:
    resolved = path.resolve()
    document = json.loads(resolved.read_text(encoding="utf-8"))
    if document.get("schema") != "fastsim-fst-pipeline-c4-v1":
        raise ValueError(f"unsupported FST pipeline descriptor: {resolved}")
    if int(document.get("cores", 0)) != 4:
        raise ValueError("the canonical FST pipeline must define C4")
    rows = document.get("workloads")
    if not isinstance(rows, list) or not rows:
        raise ValueError("the canonical FST pipeline has no workloads")
    unavailable_rows = document.get("reference_unavailable")
    if not isinstance(unavailable_rows, list):
        raise ValueError(
            "the canonical FST pipeline reference_unavailable list is invalid"
        )
    unavailable_names = {
        str(row["name"]) for row in unavailable_rows if isinstance(row, dict)
    }
    workloads: list[Workload] = []
    pilots: list[str] = []
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid workload row")
        name = str(row.get("name", ""))
        if not name or name in names:
            raise ValueError(f"invalid workload identity: {name}")
        omp_threads = int(row.get("omp_threads", 0))
        if omp_threads != 4:
            raise ValueError(f"{name} must define C4")
        argv = row.get("argv")
        environment = row.get("environment")
        if not isinstance(argv, list) or not all(
            isinstance(value, str) for value in argv
        ):
            raise ValueError(f"invalid argv for {name}")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError(f"invalid environment for {name}")
        workload = Workload(
            name=name,
            binary=str(row["qemu_binary"]),
            argv=tuple(argv),
            environment=dict(environment),
            omp_threads=omp_threads,
            run_directory=str(row["run_directory"]),
        )
        workloads.append(workload)
        names.add(name)
        if bool(row.get("pilot", False)):
            pilots.append(name)
    if len(workloads) != 10:
        raise ValueError(
            f"the formal pipeline must contain ten workloads, got "
            f"{len(workloads)}"
        )
    if unavailable_names != {"854.graph500_s"}:
        raise ValueError(
            "Graph500 must be the only unavailable TaoTrace reference"
        )
    if not unavailable_names.issubset(names):
        raise ValueError(
            "reference_unavailable must name formal production workloads"
        )
    if len(pilots) != 3:
        raise ValueError("the canonical pipeline must define three pilots")
    target = _positive(document, "user_fst_target")
    envelope = _positive(document, "raw_macro_envelope")
    if envelope != target:
        raise ValueError("raw macro envelope must equal the user FST target")
    environment = document.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("the canonical environment is missing")
    kernel_args = environment.get("kernel_args")
    if not isinstance(kernel_args, list) or not kernel_args or not all(
        isinstance(value, str) for value in kernel_args
    ):
        raise ValueError("the canonical kernel argument list is invalid")
    for key in (
        "memory", "kernel", "rootfs_base", "isa_baseline", "timezone",
        "network",
    ):
        if not isinstance(environment.get(key), str) or not environment[key]:
            raise ValueError(f"the canonical environment {key} is invalid")
    if environment["network"] != "disabled":
        raise ValueError("the canonical QEMU guest network must be disabled")
    if document.get("measurement_scope") != "user":
        raise ValueError("the canonical QEMU FST measurement scope must be user")
    return Pipeline(
        path=resolved,
        cores=4,
        user_fst_target=target,
        raw_macro_envelope=envelope,
        warmup_timeout_seconds=_positive(
            document, "warmup_timeout_seconds"
        ),
        total_timeout_seconds=_positive(document, "total_timeout_seconds"),
        measurement_scope=str(document["measurement_scope"]),
        environment=dict(environment),
        workloads=tuple(workloads),
        pilots=tuple(pilots),
        reference_unavailable=tuple(
            {
                "name": str(row["name"]),
                "reason": str(row["reason"]),
            }
            for row in unavailable_rows
        ),
    )


def select(
    pipeline: Pipeline, requested: Sequence[str], *, pilots: bool = False
) -> list[Workload]:
    if pilots:
        if requested:
            raise ValueError("--pilot and --workload cannot be combined")
        requested = pipeline.pilots
    if not requested:
        return list(pipeline.workloads)
    by_name = {workload.name: workload for workload in pipeline.workloads}
    missing = sorted(set(requested) - set(by_name))
    if missing:
        raise ValueError(f"unknown formal workload(s): {', '.join(missing)}")
    return [by_name[name] for name in requested]
