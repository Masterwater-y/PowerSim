"""Convert QEMU native transactions to canonical mixed-privilege FST v7.

This module invokes the dedicated gem5 EXTRAS component under
integrations/gem5/qemu_fst. The raw transport is qemu_tracer's
trace_entry_t-compatible extension stream.
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
from pathlib import Path

from tools.audit_fst_static_instruction_maps import audit_map
from tools.fst_wire import read_asmap

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONVERTER = (
    PROJECT_ROOT.parent / "gem5_fastsim" / "build/X86_QEMU_FST/gem5.fast"
)

def discover_trace_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(
        candidate for candidate in path.rglob("drmemtrace.*.trace*")
        if candidate.is_file()
    )


# Raw trace_entry_t is a packed <type:u16, size:u16, addr:u64> record. A shard's
# logical core is published as a CPU_ID marker (TRACE_TYPE_MARKER, size
# TRACE_MARKER_TYPE_CPU_ID) near the shard head. These ordinals come from the
# drmemtrace trace_entry.h the producer and gem5 consumer share; the C++
# assembler re-validates the core against this same marker, so it remains the
# authoritative owner of the shard/core contract.
_TRACE_ENTRY = struct.Struct("<HHQ")
_TRACE_TYPE_MARKER = 28
_TRACE_MARKER_TYPE_CPU_ID = 3
_CPU_ID_SCAN_ENTRIES = 4096


def _shard_core_id(shard: Path) -> int:
    opener = gzip.open if shard.suffix == ".gz" else open
    with opener(shard, "rb") as handle:
        data = handle.read(_TRACE_ENTRY.size * _CPU_ID_SCAN_ENTRIES)
    for offset in range(0, len(data) - _TRACE_ENTRY.size + 1, _TRACE_ENTRY.size):
        entry_type, sub_type, value = _TRACE_ENTRY.unpack_from(data, offset)
        if (entry_type == _TRACE_TYPE_MARKER
                and sub_type == _TRACE_MARKER_TYPE_CPU_ID):
            return int(value)
    raise RuntimeError(f"no CPU_ID marker in QEMU raw shard head: {shard}")


def _stage_qemu_window(
    trace_dir: Path, temporary: Path,
) -> Path:
    files = discover_trace_files(trace_dir)
    if not files:
        raise FileNotFoundError(f"no qemu_tracer drmemtrace files under {trace_dir}")
    staged_trace = temporary / "qemu-window" / "trace"
    staged_trace.mkdir(parents=True)
    for index, source in enumerate(files):
        suffix = ".trace.gz" if source.suffix == ".gz" else ".trace"
        target = staged_trace / f"qemu-window.{index:04d}{suffix}"
        target.symlink_to(source)
    return staged_trace


def _map_shards_to_cores(staged_trace: Path, num_cores: int) -> dict[int, Path]:
    """Read each shard's CPU_ID once so every worker opens only its own shard."""
    shards = sorted(
        candidate for candidate in staged_trace.iterdir()
        if candidate.is_file()
        and candidate.name.startswith("qemu-window.")
        and (candidate.suffix == ".gz" or candidate.suffix == ".trace")
    )
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


def _converter_environment() -> dict[str, str]:
    libpython = sysconfig.get_config_var("LIBDIR")
    if not libpython:
        raise RuntimeError("Python runtime library directory is unavailable")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["LD_LIBRARY_PATH"] = f"{libpython}:/opt/gcc-11.5.0/lib64"
    return env


def _measurement_manifest(
    boundaries: dict, num_cores: int, user_fst_target: int,
    raw_user_macro_envelope: int,
) -> str:
    lines = []
    for core in range(num_cores):
        row = boundaries[str(core)]
        warmup_instructions = int(row["warmup_instructions"])
        warmup_records = int(row["warmup_records"])
        measurement_instructions = int(row["measurement_instructions"])
        measurement_records = int(row["measurement_records"])
        measurement_user_records = int(
            row["measurement_user_records"]
        )
        raw_measurement_user_macros = int(
            row["raw_measurement_user_macros"]
        )
        if (
            measurement_user_records < user_fst_target
            or raw_measurement_user_macros != raw_user_macro_envelope
            or not row["target_reached"]
            or row["stop_reason"] != "qemu-per-core-user-uop-target"
            or int(row["overshoot"])
            != measurement_user_records - user_fst_target
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
    staged_trace: Path,
    input_shard: Path,
    temporary: Path,
    num_cores: int,
    user_fst_target: int,
    gem5_out: Path,
    env: dict[str, str],
    worker_core_index: int,
) -> None:
    """Invoke one independent per-core lowering worker."""
    command = [
        str(converter),
        "--outdir", str(gem5_out),
        str(config),
        "--input-trace", str(staged_trace),
        "--input-shard", str(input_shard),
        "--output-dir", str(temporary),
        "--num-cores", str(int(num_cores)),
        "--user-fst-target", str(int(user_fst_target)),
        "--worker-core-index", str(int(worker_core_index)),
    ]
    stem = f"worker.core{worker_core_index}"
    with (
        (temporary / f"{stem}.stdout").open("wb") as stdout,
        (temporary / f"{stem}.stderr").open("wb") as stderr,
    ):
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"QEMU-FST {stem} failed rc={result.returncode}: "
            f"{temporary / f'{stem}.stderr'}"
        )


def _lower_multicore_workers(
    *,
    converter: Path,
    config: Path,
    staged_trace: Path,
    temporary: Path,
    num_cores: int,
    user_fst_target: int,
    raw_user_macro_envelope: int,
    env: dict[str, str],
) -> dict:
    """Run one independent gem5 worker per logical core."""

    shard_by_core = _map_shards_to_cores(staged_trace, num_cores)

    def lower_core(core: int) -> None:
        worker_out = temporary / f"gem5-worker-core{core}"
        _run_converter(
            converter=converter,
            config=config,
            staged_trace=staged_trace,
            input_shard=shard_by_core[core],
            temporary=temporary,
            num_cores=num_cores,
            user_fst_target=user_fst_target,
            gem5_out=worker_out,
            env=env,
            worker_core_index=core,
        )
        shutil.rmtree(worker_out, ignore_errors=True)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=num_cores
    ) as executor:
        futures = [
            executor.submit(lower_core, core)
            for core in range(num_cores)
        ]
        for future in futures:
            future.result()

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
    user_fst_target: int,
    raw_user_macro_envelope: int,
) -> tuple[Path, ...]:
    """Lower a QEMU trace_entry_t tree to FastSim FST v7.

    QEMU's hint-window is the producer's ROI authority. Its same-stream
    markers carry each transaction's ASID, privilege, pre-state, memory facts
    and event evidence. No legacy auxiliary input is accepted on this path.

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
    if int(user_fst_target) <= 0:
        raise ValueError(
            "QEMU-FST conversion requires a positive measured CPL3 FST "
            "UOP target"
        )
    if int(raw_user_macro_envelope) <= 0:
        raise ValueError(
            "QEMU-FST conversion requires a positive measured CPL3 raw "
            "macro envelope"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.with_name(f".{output_dir.name}.staging")
    if temporary.exists():
        raise FileExistsError(
            f"stale QEMU-FST staging directory: {temporary}"
        )
    temporary.mkdir()
    staged_trace = _stage_qemu_window(trace_dir, temporary)
    converter_env = _converter_environment()
    try:
        boundaries = _lower_multicore_workers(
            converter=converter,
            config=config,
            staged_trace=staged_trace,
            temporary=temporary,
            num_cores=int(num_cores),
            user_fst_target=int(
                user_fst_target
            ),
            raw_user_macro_envelope=int(raw_user_macro_envelope),
            env=converter_env,
        )
        # File/companion semantics belong to the FST reader used by replay.
        paths = [temporary / f"core{core_id}.fst"
                 for core_id in range(int(num_cores))]
        shutil.rmtree(temporary / "qemu-window", ignore_errors=True)
        (temporary / "manifest.txt").write_text(
            _measurement_manifest(
                boundaries, int(num_cores),
                int(user_fst_target),
                int(raw_user_macro_envelope),
            ),
            encoding="utf-8",
        )
        audit_qemu_instruction_maps(temporary, int(num_cores))
        os.replace(temporary, output_dir)
        return tuple(output_dir / p.name for p in paths)
    except Exception:
        raise
