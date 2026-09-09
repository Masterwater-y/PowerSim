"""Convert QEMU trace_entry_t shards to canonical FST v7.

This module invokes the dedicated gem5 EXTRAS component under
integrations/gem5/qemu_fst. The raw transport is qemu_tracer's
trace_entry_t-compatible extension stream.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONVERTER = (
    PROJECT_ROOT.parent / "gem5_fastsim" / "build/X86_QEMU_FST/gem5.fast"
)

def _trace_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(
        candidate for candidate in path.rglob("drmemtrace.*.trace*")
        if candidate.is_file()
    )


def _stage_qemu_window(
    trace_dir: Path, temporary: Path,
) -> Path:
    files = _trace_files(trace_dir)
    if not files:
        raise FileNotFoundError(f"no qemu_tracer drmemtrace files under {trace_dir}")
    staged_trace = temporary / "qemu-window" / "trace"
    staged_trace.mkdir(parents=True)
    for index, source in enumerate(files):
        suffix = ".trace.gz" if source.suffix == ".gz" else ".trace"
        target = staged_trace / f"qemu-window.{index:04d}{suffix}"
        target.symlink_to(source)
    return staged_trace


def _converter_environment() -> dict[str, str]:
    libpython = sysconfig.get_config_var("LIBDIR")
    if not libpython:
        raise RuntimeError("Python runtime library directory is unavailable")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["LD_LIBRARY_PATH"] = f"{libpython}:/opt/gcc-11.5.0/lib64"
    return env


def _measurement_manifest(
    boundaries: object, num_cores: int,
) -> str:
    if not isinstance(boundaries, dict):
        raise RuntimeError("QEMU-FST converter produced invalid boundaries")
    lines = []
    for core in range(num_cores):
        row = boundaries.get(str(core))
        if not isinstance(row, dict):
            raise RuntimeError(
                f"QEMU-FST converter omitted core {core} boundaries"
            )
        warmup_instructions = int(row["warmup_instructions"])
        warmup_records = int(row["warmup_records"])
        measurement_instructions = int(row["measurement_instructions"])
        measurement_records = int(row["measurement_records"])
        if measurement_instructions <= 0 or measurement_records <= 0:
            raise RuntimeError(
                f"QEMU-FST converter produced empty measurement core {core}"
            )
        lines.append(
            f"{core} fastsim-binary-warmup-slice core{core}.fst "
            f"{core} {warmup_instructions} {measurement_instructions} "
            f"{warmup_records} {measurement_records}\n"
        )
    return "".join(lines)


def convert_qemu_fst_trace(
    *,
    trace_dir: Path,
    output_dir: Path,
    num_cores: int,
    force: bool = False,
    converter: Path | None = None,
    measurement_user_record_target: int,
) -> tuple[Path, ...]:
    """Lower a QEMU trace_entry_t tree to FastSim FST v7.

    QEMU's hint-window is the producer's ROI authority. Its same-stream
    markers carry the fixed ASID, per-macro CPL3 pre-state, memory facts and
    syscall evidence. No legacy auxiliary input is accepted on this path.
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
        if not force:
            raise FileExistsError(
                f"QEMU-FST output already exists: {output_dir}"
            )
        shutil.rmtree(output_dir)
    if not 1 <= int(num_cores) <= 32:
        raise ValueError(f"core count must be in [1,32], got {num_cores}")
    if int(measurement_user_record_target) <= 0:
        raise ValueError(
            "QEMU-FST conversion requires a positive measured CPL3 FST-record "
            "target"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.with_name(f".{output_dir.name}.staging")
    if temporary.exists():
        if not force:
            raise FileExistsError(
                f"stale QEMU-FST staging directory: {temporary}"
            )
        shutil.rmtree(temporary)
    temporary.mkdir()
    gem5_out = temporary / "gem5"
    staged_trace = _stage_qemu_window(trace_dir, temporary)
    command = [
        str(converter),
        "--outdir", str(gem5_out),
        str(config),
        "--input-trace", str(staged_trace),
        "--output-dir", str(temporary),
        "--num-cores", str(int(num_cores)),
        "--measurement-user-uops", str(int(measurement_user_record_target)),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=_converter_environment(),
            stdout=(temporary / "converter.stdout").open("wb"),
            stderr=(temporary / "converter.stderr").open("wb"),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"QEMU-FST converter failed rc={result.returncode}: "
                f"{temporary / 'converter.stderr'}"
            )
        paths = []
        for core_id in range(int(num_cores)):
            target = temporary / f"core{core_id}.fst"
            if not target.is_file() or target.stat().st_size <= 72:
                raise RuntimeError(
                    f"QEMU-FST converter produced empty core {core_id}"
                )
            for suffix in (".asmap", ".vmap"):
                companion = target.with_name(target.name + suffix)
                if not companion.is_file() or companion.stat().st_size == 0:
                    raise RuntimeError(
                        "QEMU-FST converter produced no required companion: "
                        f"{companion}"
                    )
            paths.append(target)
        boundary_path = temporary / "boundaries.json"
        if not boundary_path.is_file():
            raise RuntimeError(
                "QEMU-FST converter produced no boundaries.json"
            )
        boundaries = json.loads(
            boundary_path.read_text(encoding="utf-8")
        ).get("cores")
        shutil.rmtree(gem5_out, ignore_errors=True)
        shutil.rmtree(temporary / "qemu-window", ignore_errors=True)
        (temporary / "manifest.txt").write_text(
            _measurement_manifest(boundaries, int(num_cores)),
            encoding="utf-8",
        )
        os.replace(temporary, output_dir)
        return tuple(output_dir / p.name for p in paths)
    except Exception:
        raise
