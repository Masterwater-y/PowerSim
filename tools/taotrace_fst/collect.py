"""Collect a gem5 TaoTrace C4 reference with FastSim common-end semantics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.collect_common_end_fst import finalize_capture
from tools.fst_pipeline.descriptor import DEFAULT_DESCRIPTOR, load_pipeline, select


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "var/qemu_fst/runs/production/gem5"
)
DEFAULT_ASSET_ROOT = PROJECT_ROOT / "var/taotrace_fst/assets"
DEFAULT_GEM5 = Path(
    "/data00/xuhaoen/gem5_taotrace_fst/build/X86_TAOTRACE_FST/gem5.opt"
)
DEFAULT_CONFIG = Path(
    "/data00/yinhaolang/TCSim/configs/gem5/"
    "x86_fs_kvm_boot_checkpoint_tao.py"
)
DEFAULT_TCSIM_ROOT = Path("/data00/yinhaolang/TCSim")
DEFAULT_RESOURCE_DIR = Path("/data00/yinhaolang/gem5-fs/resources")
DEFAULT_AUX_DISK = (
    DEFAULT_TCSIM_ROOT
    / "data/spec2026_diskimg/spec2026-usergate-original.ext4"
)
DEFAULT_STOCKFISH_CHECKPOINT = (
    DEFAULT_TCSIM_ROOT
    / "ckpt/gem5-fs-roi/mesi-three-level-3GiB/4c/706.stockfish_r/"
    "5955a1b7a812368b411b/checkpoint"
)
DEFAULT_STOCKFISH_ELF = (
    DEFAULT_TCSIM_ROOT
    / "workloads/spec2026/benchspec/CPU/706.stockfish_r/exe/"
    "stockfish_base.gem5-x86-linux"
)
EFFECTIVE_TARGET_GENERATOR = PROJECT_ROOT / "tools/generate_fs_effective_target.py"
EVENT_DICTIONARY = PROJECT_ROOT / "configs/pmu-event-dictionary-v1.json"
CORES = 4
SAFETY_MAX_INSTS = 1_000_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} is missing: {resolved}")
    return resolved


def _require_directory(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"{label} is missing: {resolved}")
    return resolved


def _copy_asset(source: Path, destination: Path) -> dict[str, Any]:
    source = _require_file(source, "TaoTrace workload ELF")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
    source_sha256 = _sha256(source)
    destination_sha256 = _sha256(destination)
    if source_sha256 != destination_sha256:
        raise ValueError(f"copied TaoTrace ELF differs from source: {destination}")
    return {
        "source": str(source),
        "local_copy": str(destination),
        "sha256": destination_sha256,
    }


def _command(
    *,
    gem5: Path,
    config: Path,
    source_dir: Path,
    checkpoint: Path,
    resource_dir: Path,
    aux_disk: Path,
    target: int,
) -> list[str]:
    trace_scratch = source_dir / "trace-scratch"
    return [
        str(gem5),
        "-d",
        str(source_dir),
        str(config),
        "--action",
        "restore",
        "--restore-cpu-type",
        "o3",
        "--resume-at-roi",
        "--num-cores",
        str(CORES),
        "--checkpoint-dir",
        str(checkpoint),
        "--cache-hierarchy",
        "mesi-three-level",
        "--mem-size",
        "3GiB",
        "--resource-dir",
        str(resource_dir),
        "--aux-disk",
        str(aux_disk),
        "--detailed-warmup-insts",
        "0",
        "--stats-outfile",
        str(source_dir / "stats.txt"),
        "--roi-safety-max-insts-per-core",
        str(SAFETY_MAX_INSTS),
        "--wait-for-roi-workbegin",
        "--tao-trace-dir",
        str(trace_scratch),
        "--tao-trace-format",
        "fst",
        "--tao-measure-cpl",
        "--tao-native-anomaly-limit",
        "32",
        "--tao-functional-include-kernel",
        "--tao-functional-warmup",
        "--tao-functional-user-target",
        str(target),
        "--roi-stop-policy",
        "any-core",
    ]


def collect(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.descriptor)
    workloads = select(
        pipeline, args.workload or ["706.stockfish_r"]
    )
    if len(workloads) != 1 or workloads[0].name != "706.stockfish_r":
        raise ValueError(
            "the current TaoTrace producer is validated only for "
            "706.stockfish_r"
        )
    target = args.user_macro_target or pipeline.user_macro_target
    gem5 = _require_file(args.gem5, "common-end gem5")
    config = _require_file(args.config, "TaoTrace gem5 config")
    checkpoint = _require_directory(args.checkpoint, "Stockfish ROI checkpoint")
    resource_dir = _require_directory(args.resource_dir, "gem5 resource directory")
    aux_disk = _require_file(args.aux_disk, "Stockfish auxiliary disk")
    stockfish_elf = _require_file(args.stockfish_elf, "TaoTrace workload ELF")
    output_root = args.output_root.resolve()
    case_dir = output_root / f"c{CORES:02d}" / workloads[0].name
    if case_dir.exists():
        raise FileExistsError(f"TaoTrace reference already exists: {case_dir}")
    source_dir = case_dir / "source"
    command = _command(
        gem5=gem5,
        config=config,
        source_dir=source_dir,
        checkpoint=checkpoint,
        resource_dir=resource_dir,
        aux_disk=aux_disk,
        target=target,
    )
    provenance = {
        "schema": "minesim-taotrace-reference-v1",
        "producer": "gem5-taotrace",
        "workload": workloads[0].name,
        "topology": "c04",
        "measurement_scope": pipeline.measurement_scope,
        "measurement_policy": "first-core-macro-common-end",
        "target_user_macros": target,
        "elf_policy": "dual-elf",
        "qemu_elf": str(
            (PROJECT_ROOT / workloads[0].run_directory / workloads[0].binary)
            .resolve()
        ),
        "gem5_elf": {
            "source": str(stockfish_elf),
            "sha256": _sha256(stockfish_elf),
            "execution": "checkpoint-and-aux-disk",
        },
        "read_only_inputs": {
            "gem5": str(gem5),
            "gem5_sha256": _sha256(gem5),
            "config": str(config),
            "config_sha256": _sha256(config),
            "checkpoint": str(checkpoint),
            "resource_dir": str(resource_dir),
            "aux_disk": str(aux_disk),
            "aux_disk_sha256": _sha256(aux_disk),
        },
        "command": command,
    }
    if args.dry_run:
        print(json.dumps(provenance, indent=2, sort_keys=True))
        return 0
    source_dir.mkdir(parents=True)
    provenance["gem5_elf"]["local_copy"] = _copy_asset(
        stockfish_elf,
        args.asset_root.resolve() / "706.stockfish_r"
        / stockfish_elf.name,
    )["local_copy"]
    (source_dir / "collection.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["FASTSIM_EFFECTIVE_TARGET_GENERATOR"] = str(
        EFFECTIVE_TARGET_GENERATOR
    )
    environment["FASTSIM_EFFECTIVE_TARGET_PYTHON"] = sys.executable
    environment["FASTSIM_EVENT_DICTIONARY"] = str(EVENT_DICTIONARY)
    started = time.monotonic()
    with (source_dir / "run.log").open("w", encoding="utf-8") as sink:
        result = subprocess.run(
            command,
            env=environment,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"gem5 TaoTrace collection failed with exit {result.returncode}; "
            f"see {source_dir / 'run.log'}"
        )
    audit = finalize_capture(
        source_dir,
        cores=CORES,
        target=target,
        tcsim_root=args.tcsim_root,
    )
    promoted = source_dir / "tao_trace"
    for path in promoted.iterdir():
        if path.is_file():
            shutil.move(str(path), case_dir / path.name)
    trace_path = case_dir / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    for core, row in trace["per_core"].items():
        row["fst"] = f"core{core}.fst"
        for key in (
            "virtual_page_map",
            "address_space_map",
            "static_instruction_map",
            "dependency_companion",
        ):
            if key in row:
                row[key] = Path(row[key]).name
    trace["source_dir"] = str(source_dir)
    trace["elf_policy"] = "dual-elf"
    trace_path.write_text(
        json.dumps(trace, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (case_dir / "collection.json").write_text(
        json.dumps(
            {
                **provenance,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "common_end": audit,
                "source_dir": str(source_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(case_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.taotrace_fst.collect"
    )
    parser.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--user-macro-target", type=int)
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_STOCKFISH_CHECKPOINT)
    parser.add_argument("--resource-dir", type=Path, default=DEFAULT_RESOURCE_DIR)
    parser.add_argument("--aux-disk", type=Path, default=DEFAULT_AUX_DISK)
    parser.add_argument("--stockfish-elf", type=Path, default=DEFAULT_STOCKFISH_ELF)
    parser.add_argument("--tcsim-root", type=Path, default=DEFAULT_TCSIM_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.user_macro_target is not None and args.user_macro_target <= 0:
        parser.error("--user-macro-target must be positive")
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
