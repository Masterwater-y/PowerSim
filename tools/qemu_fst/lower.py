"""Convert canonical QEMU records to mixed-privilege FST v7.

This module invokes the dedicated gem5 EXTRAS component under
integrations/gem5/qemu_fst. The raw transport is qemu_tracer's committed
QEMU-FST record stream.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import json
import os
import shutil
import struct
import subprocess
import sysconfig
import time
from pathlib import Path

from tools.audit_fst_static_instruction_maps import audit_map
from tools.fst_wire import read_asmap

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONVERTER = (
    Path(os.environ.get(
        "QEMU_FST_GEM5_ROOT", PROJECT_ROOT.parent / "gem5_fastsim"))
    / "build/X86_QEMU_FST/gem5.fast"
)

def discover_trace_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(
        candidate for candidate in path.rglob("qemu-fst.*.raw*")
        if candidate.is_file()
    )


_RAW_HEADER = struct.Struct("<QIIII24x")
_RAW_MAGIC = 0x3257415244535446
_RAW_VERSION = 2


def _shard_core_id(shard: Path) -> int:
    opener = gzip.open if shard.suffix == ".gz" else open
    with opener(shard, "rb") as handle:
        data = handle.read(_RAW_HEADER.size)
    if len(data) != _RAW_HEADER.size:
        raise RuntimeError(f"short QEMU-FST file header: {shard}")
    magic, version, header_bytes, core, state_fields = _RAW_HEADER.unpack(data)
    if (
        magic != _RAW_MAGIC
        or version != _RAW_VERSION
        or header_bytes != _RAW_HEADER.size
        or state_fields != 125
    ):
        raise RuntimeError(f"invalid QEMU-FST file header: {shard}")
    return int(core)


def _map_shards_to_cores(trace_dir: Path, num_cores: int) -> dict[int, Path]:
    """Read each shard's CPU_ID once so every worker opens only its own shard."""
    shards = discover_trace_files(trace_dir)
    if len(shards) != num_cores:
        raise RuntimeError(
            f"QEMU-FST expected {num_cores} raw shards, found {len(shards)}"
        )
    mapping: dict[int, Path] = {}
    for shard in shards:
        core = _shard_core_id(shard)
        if core in mapping:
            raise RuntimeError(
                f"QEMU-FST raw shards share logical core {core}"
            )
        if not 0 <= core < num_cores:
            raise RuntimeError(
                f"QEMU-FST raw shard core {core} outside [0,{num_cores})"
            )
        mapping[core] = shard
    if sorted(mapping) != list(range(num_cores)):
        raise RuntimeError(
            f"QEMU-FST raw shards do not cover cores 0..{num_cores - 1}: "
            f"{sorted(mapping)}"
        )
    return mapping


def _common_end_trigger(trace_dir: Path, num_cores: int) -> int:
    marker = trace_dir / ".common-end-triggered"
    if not marker.is_file():
        raise FileNotFoundError(
            f"QEMU-FST common-end trigger is missing: {marker}"
        )
    values = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"invalid common-end trigger line: {line!r}")
        values[key] = value
    core = int(values["trigger_core"])
    if not 0 <= core < num_cores:
        raise ValueError(
            f"QEMU-FST trigger core {core} outside [0,{num_cores})"
        )
    return core


def _converter_environment() -> dict[str, str]:
    libpython = sysconfig.get_config_var("LIBDIR")
    if not libpython:
        raise RuntimeError("Python runtime library directory is unavailable")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["LD_LIBRARY_PATH"] = f"{libpython}:/opt/gcc-11.5.0/lib64"
    return env


def _cpu_node(cpu: int) -> int:
    nodes = sorted(
        (Path("/sys/devices/system/cpu") / f"cpu{cpu}").glob("node[0-9]*")
    )
    if len(nodes) != 1:
        raise RuntimeError(f"CPU {cpu} has no unique NUMA node")
    return int(nodes[0].name.removeprefix("node"))


def _physical_core(cpu: int) -> tuple[int, int]:
    topology = Path("/sys/devices/system/cpu") / f"cpu{cpu}" / "topology"
    package = int((topology / "physical_package_id").read_text())
    core = int((topology / "core_id").read_text())
    return package, core


def _worker_cpus(num_cores: int, requested: tuple[int, ...] | None) -> tuple[int, ...]:
    allowed = sorted(os.sched_getaffinity(0))
    if requested is not None:
        if len(requested) != num_cores or len(set(requested)) != num_cores:
            raise ValueError(
                f"QEMU-FST requires {num_cores} distinct lower CPUs"
            )
        if not set(requested).issubset(allowed):
            raise ValueError(
                f"QEMU-FST lower CPUs {requested} exceed allowed cpuset "
                f"{allowed}"
            )
        candidates = list(requested)
    else:
        by_node: dict[int, list[int]] = {}
        seen_cores: set[tuple[int, int]] = set()
        for cpu in allowed:
            identity = _physical_core(cpu)
            if identity in seen_cores:
                continue
            seen_cores.add(identity)
            by_node.setdefault(_cpu_node(cpu), []).append(cpu)
        candidates = next(
            (
                cpus[:num_cores]
                for _, cpus in sorted(by_node.items())
                if len(cpus) >= num_cores
            ),
            [],
        )
        if len(candidates) != num_cores:
            raise RuntimeError(
                "QEMU-FST cannot find enough physical cores on one NUMA node"
            )
    identities = [_physical_core(cpu) for cpu in candidates]
    nodes = {_cpu_node(cpu) for cpu in candidates}
    if len(set(identities)) != num_cores or len(nodes) != 1:
        raise ValueError(
            "QEMU-FST lower CPUs must be distinct physical cores on one "
            f"NUMA node: {tuple(candidates)}"
        )
    return tuple(candidates)


def _measurement_manifest(
    boundaries: dict, num_cores: int, user_macro_target: int,
    trigger_core: int,
) -> str:
    lines = []
    for core in range(num_cores):
        row = boundaries[str(core)]
        warmup_instructions = int(row["warmup_instructions"])
        warmup_records = int(row["warmup_records"])
        measurement_instructions = int(row["measurement_instructions"])
        measurement_records = int(row["measurement_records"])
        if (
            int(row["target_user_macros"]) != user_macro_target
            or int(row["trigger_core"]) != trigger_core
            or bool(row["target_reached"]) != (core == trigger_core)
            or row["measurement_policy"] != "first-core-macro-common-end"
            or row["stop_reason"] != "first-core-user-macro-target"
            or int(row["measurement_user_macros"]) >
                user_macro_target
        ):
            raise RuntimeError(
                f"QEMU-FST core {core} boundary mismatch: {row}"
            )
        lines.append(
            f"{core} fastsim-binary-warmup-slice core{core}.fst "
            f"{core} {warmup_instructions} {measurement_instructions} "
            f"{warmup_records} {measurement_records}\n"
        )
    return "".join(lines)


def audit_qemu_instruction_maps(output_dir: Path, num_cores: int) -> None:
    paths = [output_dir / f"core{core}.fst" for core in range(num_cores)]
    for path in paths:
        address_spaces = {
            row.address_space_id
            for row in read_asmap(Path(str(path) + ".asmap")).entries
        }
        static_map = audit_map(path)
        if len(address_spaces) > 1:
            if static_map["present"]:
                raise ValueError(
                    f"multi-ASID QEMU-FST must omit .imap: {path}"
                )
            continue
        if not (
            static_map["present"]
            and static_map.get("instruction_rows", 0)
            and static_map.get("operands_complete", False)
        ):
            raise ValueError(
                f"single-ASID QEMU-FST lacks complete operands: {path}"
            )


def _run_converter(
    *,
    converter: Path,
    config: Path,
    input_shard: Path,
    temporary: Path,
    num_cores: int,
    user_macro_target: int,
    trigger_core: int,
    gem5_out: Path,
    env: dict[str, str],
    worker_core_index: int,
    host_cpu: int,
    numa_node: int,
) -> float:
    """Invoke one independent per-core lowering worker."""
    command = [
        "numactl",
        f"--physcpubind={host_cpu}",
        f"--membind={numa_node}",
        str(converter),
        "--outdir", str(gem5_out),
        str(config),
        "--input-shard", str(input_shard),
        "--output-dir", str(temporary),
        "--num-cores", str(int(num_cores)),
        "--user-macro-target", str(int(user_macro_target)),
        "--trigger-core", str(int(trigger_core)),
        "--worker-core-index", str(int(worker_core_index)),
    ]
    stem = f"worker.core{worker_core_index}"
    with (
        (temporary / f"{stem}.stdout").open("wb") as stdout,
        (temporary / f"{stem}.stderr").open("wb") as stderr,
    ):
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        elapsed = time.monotonic() - started
    if result.returncode != 0:
        raise RuntimeError(
            f"QEMU-FST {stem} failed rc={result.returncode}: "
            f"{temporary / f'{stem}.stderr'}"
        )
    return elapsed


def _lower_multicore_workers(
    *,
    converter: Path,
    config: Path,
    trace_dir: Path,
    temporary: Path,
    num_cores: int,
    user_macro_target: int,
    trigger_core: int,
    env: dict[str, str],
    lower_cpus: tuple[int, ...] | None,
) -> dict:
    """Run one independent gem5 worker per logical core."""

    shard_by_core = _map_shards_to_cores(trace_dir, num_cores)
    worker_cpus = _worker_cpus(num_cores, lower_cpus)
    numa_node = _cpu_node(worker_cpus[0])

    def lower_core(core: int) -> float:
        worker_out = temporary / f"gem5-worker-core{core}"
        elapsed = _run_converter(
            converter=converter,
            config=config,
            input_shard=shard_by_core[core],
            temporary=temporary,
            num_cores=num_cores,
            user_macro_target=user_macro_target,
            trigger_core=trigger_core,
            gem5_out=worker_out,
            env=env,
            worker_core_index=core,
            host_cpu=worker_cpus[core],
            numa_node=numa_node,
        )
        shutil.rmtree(worker_out, ignore_errors=True)
        return elapsed

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=num_cores
    ) as executor:
        futures = {
            executor.submit(lower_core, core): core
            for core in range(num_cores)
        }
        worker_seconds = {
            futures[future]: future.result()
            for future in concurrent.futures.as_completed(futures)
        }
    print(
        "QEMU-FST lower workers: "
        + ", ".join(
            f"core{core}=cpu{worker_cpus[core]}:{worker_seconds[core]:.2f}s"
            for core in range(num_cores)
        ),
        flush=True,
    )

    boundaries: dict = {}
    for core in range(num_cores):
        fragment = temporary / f"core{core}.boundaries.json"
        boundaries[str(core)] = json.loads(
            fragment.read_text(encoding="utf-8")
        )[str(core)]
        fragment.unlink()
    (temporary / "boundaries.json").write_text(
        json.dumps({"cores": boundaries}), encoding="utf-8"
    )
    return boundaries


def convert_qemu_fst_trace(
    *,
    trace_dir: Path,
    output_dir: Path,
    num_cores: int,
    converter: Path | None = None,
    user_macro_target: int,
    lower_cpus: tuple[int, ...] | None = None,
) -> tuple[Path, ...]:
    """Lower canonical QEMU-FST record shards to FastSim FST v7.

    QEMU's hint-window and first-core macro target are the producer's ROI
    authority. Each committed record carries its phase, architectural state
    delta, memory facts and event evidence.

    One independent gem5 worker process lowers each logical-core shard. The
    multi-core output is published only after every worker succeeds.
    """
    trace_dir = trace_dir.resolve()
    output_dir = output_dir.resolve()
    converter = Path(converter or DEFAULT_CONVERTER).resolve()
    config = (
        PROJECT_ROOT / "integrations" / "gem5" / "qemu_fst"
        / "config" / "qemu_fst_to_v7.py"
    )
    if not converter.is_file():
        raise FileNotFoundError(
            f"gem5 converter missing: {converter}; "
            "run python -m tools.fst_pipeline build --component gem5"
        )
    if output_dir.exists():
        raise FileExistsError(
            f"QEMU-FST output already exists: {output_dir}"
        )
    if not 2 <= int(num_cores) <= 32:
        raise ValueError(f"core count must be in [2,32], got {num_cores}")
    if int(user_macro_target) <= 0:
        raise ValueError(
            "QEMU-FST conversion requires a positive measured CPL3 macro "
            "target"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.with_name(f".{output_dir.name}.staging")
    if temporary.exists():
        raise FileExistsError(
            f"stale QEMU-FST staging directory: {temporary}"
        )
    temporary.mkdir()
    trigger_core = _common_end_trigger(trace_dir, int(num_cores))
    converter_env = _converter_environment()
    try:
        boundaries = _lower_multicore_workers(
            converter=converter,
            config=config,
            trace_dir=trace_dir,
            temporary=temporary,
            num_cores=int(num_cores),
            user_macro_target=int(user_macro_target),
            trigger_core=trigger_core,
            env=converter_env,
            lower_cpus=lower_cpus,
        )
        # File/companion semantics belong to the FST reader used by replay.
        paths = [temporary / f"core{core_id}.fst"
                 for core_id in range(int(num_cores))]
        (temporary / "manifest.txt").write_text(
            _measurement_manifest(
                boundaries, int(num_cores),
                int(user_macro_target),
                trigger_core,
            ),
            encoding="utf-8",
        )
        audit_qemu_instruction_maps(temporary, int(num_cores))
        os.replace(temporary, output_dir)
        return tuple(output_dir / p.name for p in paths)
    except Exception:
        raise
