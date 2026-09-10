#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.audit_fst_static_instruction_maps import audit_map
from tools.audit_fst_virtual_page_map import audit_file
from tools.qemu_fst._assets import build_workload_disk
from tools.qemu_fst.capture import FstAssets, collect_workload
from tools.qemu_fst.lower import DEFAULT_CONVERTER, convert_qemu_fst_trace
from tools.qemu_fst.workloads import Workload, run_fastsim


WORKSPACE_ROOT = PROJECT_ROOT.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--user-fst-target", type=int, default=100_000)
    parser.add_argument("--raw-root", type=Path)
    args = parser.parse_args()

    output = args.output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    raw = output / "raw"
    if args.raw_root is not None:
        raw.symlink_to(args.raw_root.resolve(), target_is_directory=True)
    else:
        case = output / "case"
        case.mkdir()
        binary = case / "multi_asid"
        subprocess.run(
            [
                "/opt/gcc-11.5.0/bin/gcc",
                "-O2", "-g", "-Wall", "-Wextra", "-std=gnu11",
                "-static", "-fno-pie", "-no-pie",
                "-pthread",
                f"-I{PROJECT_ROOT / 'tools/qemu_fst/resources'}",
                "-o", str(binary),
                str(PROJECT_ROOT / "test/qemu_fst/multi_asid.c"),
            ],
            check=True,
        )
        workload = Workload(
            name="multi-asid",
            binary="multi_asid",
            argv=(),
            environment={},
            omp_threads=4,
            run_directory=str(case),
        )
        assets_root = output / "assets"
        workload_disk = build_workload_disk(
            [workload], destination=assets_root,
        )
        assets = FstAssets(
            kernel=(
                PROJECT_ROOT
                / "var/qemu_fst/resources/x86-linux-kernel-6.8.0-52-generic-1.0.0"
            ),
            rootfs=(
                PROJECT_ROOT
                / "var/qemu_fst/assets/fst-pipeline-ubuntu-24.04.raw"
            ),
            workload_disk=workload_disk,
            qemu=WORKSPACE_ROOT / "qemu_tracer_qemu/build/qemu-system-x86_64",
            plugin=WORKSPACE_ROOT / "qemu_tracer/backend/dumper/build/libdumper.so",
            launcher=WORKSPACE_ROOT / "qemu_tracer/scripts/run_fst_x86.sh",
        )
        collect_workload(
            workload=workload,
            memory="3G",
            timeout_seconds=1800,
            warmup_timeout_seconds=300,
            kernel_args=(
                "earlyprintk=ttyS0",
                "console=ttyS0",
                "lpj=7999923",
                "root=/dev/sda2",
                "mce=off",
                "nomce",
                "nokaslr",
                "mitigations=off",
                "nopti",
                "idle=poll",
            ),
            network="disabled",
            raw_macro_envelope=args.user_fst_target,
            assets=assets,
            output_dir=raw,
        )
    fst = output / "fst"
    convert_qemu_fst_trace(
        trace_dir=raw,
        output_dir=fst,
        num_cores=4,
        converter=DEFAULT_CONVERTER,
        measurement_user_record_target=args.user_fst_target,
    )
    audits = []
    for core in range(4):
        path = fst / f"core{core}.fst"
        audit = audit_file(path, False)
        if audit["address_spaces"] < 2:
            raise RuntimeError(
                f"core {core} did not observe multiple address spaces"
            )
        static_map = audit_map(path)
        if not static_map["present"]:
            raise RuntimeError(
                f"multi-ASID stream omitted AS-scoped imap: {path}"
            )
        if len(static_map["address_spaces"]) < 2:
            raise RuntimeError(
                f"multi-ASID imap omitted an address space: {path}"
            )
        audits.append(
            {
                "core_id": audit["core_id"],
                "records": audit["records"],
                "entries": audit["entries"],
                "address_spaces": audit["address_spaces"],
                "address_space_switches": audit["address_space_switches"],
                "static_instruction_address_spaces": static_map[
                    "address_spaces"
                ],
                "map_present": audit["map_present"],
                "address_space_map_present": audit[
                    "address_space_map_present"
                ],
            }
        )

    replay = output / "replay"
    replay.mkdir()
    run_fastsim(
        fastsim=PROJECT_ROOT / "build/fastsim",
        config=PROJECT_ROOT / "configs/gem5-v28_1-fs-user.cfg",
        manifest=fst / "manifest.txt",
        output=replay / "stats.json",
        log=replay / "fastsim.log",
        dram_size=3 * 1024**3,
    )
    report = {
        "schema": "qemu-fst-multi-asid-pilot-v1",
        "target_user_uops_per_core": args.user_fst_target,
        "cores": audits,
        "replay": str(replay / "stats.json"),
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
