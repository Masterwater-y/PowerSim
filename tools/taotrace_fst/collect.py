"""User-only gem5 TaoTrace FST collection driver for the minesim mainline.

Restores each SPEC2026 C4 ROI checkpoint under the local gem5_taotrace build
and captures a pure user-scope FST v7 (``--tao-functional-user-only``), so the
result is directly symmetric with the QEMU-FST user-only producer. The ROI
checkpoint is scope-independent; only the collection-time switch differs from
the upstream user-plus-kernel capture.

Command shape is reproduced from the fastsim-branch formal collection:
    gem5.opt -d <outdir> x86_fs_kvm_boot_checkpoint_tao.py
        --action restore --restore-cpu-type o3 --resume-at-roi --num-cores 4
        --checkpoint-dir <ckpt> --cache-hierarchy mesi-three-level
        --mem-size 3GiB --resource-dir <resources> --aux-disk <disk>
        --detailed-warmup-insts 0 --stats-outfile <stats>
        --roi-user-records-per-core N --roi-safety-max-insts-per-core M
        --wait-for-roi-workbegin --tao-trace-dir <trace> --tao-trace-format fst
        --tao-measure-cpl --tao-native-anomaly-limit 32
        --tao-functional-warmup --tao-functional-user-target N
        --tao-functional-user-only
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path

from .normalize import normalize

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = Path(__file__).resolve().parent
TAO_CONFIG = SCRIPT_ROOT / "x86_fs_kvm_boot_checkpoint_tao.py"
# Sidecar generator + PMU event dictionary the wrapper resolves via env.
EFFECTIVE_TARGET_GENERATOR = SCRIPT_ROOT / "generate_fs_effective_target.py"
EVENT_DICTIONARY = SCRIPT_ROOT / "pmu-event-dictionary-v1.json"

# Canonical local gem5 TaoTrace FS build materialized by
# tools.taotrace_fst.build.
GEM5 = Path(
    "/data00/xuhaoen/gem5_taotrace/build/X86_TAOTRACE_FST/gem5.fast"
)
# Upstream kernel/resource tree is large and stable; reused read-only.
RESOURCE_DIR = Path("/data00/yinhaolang/gem5-fs/resources")

DEFAULT_PLAN = (
    PROJECT_ROOT
    / "var/qemu_fst/diagnostics/taotrace-checkpoints/collect-plan.json"
)
DEFAULT_TARGET = 10_000_000
SAFETY_MULTIPLIER_INSTS = 1_000_000_000


def _require_collection_root(path: Path) -> Path:
    resolved = path.resolve()
    qemu_run_root = (PROJECT_ROOT / "var/qemu_fst/runs").resolve()
    diagnostic_reference_root = (
        PROJECT_ROOT / "var/qemu_fst/diagnostics/taotrace-reference"
    ).resolve()
    if any(
        resolved == protected or protected in resolved.parents
        for protected in (qemu_run_root, diagnostic_reference_root)
    ):
        raise ValueError(
            "TaoTrace collection cannot write a QEMU run or promoted "
            "reference root; collect into a new diagnostics scratch path "
            "and promote it explicitly"
        )
    return resolved


def _collect_one(
    workload: str, spec: dict, out_root: Path, target: int,
) -> dict:
    out_dir = out_root / workload
    trace_dir = out_dir / "fst"
    stats = out_dir / "stats.txt"
    if out_dir.exists():
        raise FileExistsError(
            f"TaoTrace collection output already exists: {out_dir}"
        )
    trace_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "run.log"
    command = [
        str(GEM5), "-d", str(out_dir), str(TAO_CONFIG),
        "--action", "restore", "--restore-cpu-type", "o3", "--resume-at-roi",
        "--num-cores", "4",
        "--checkpoint-dir", str(spec["local_checkpoint"]),
        "--cache-hierarchy", "mesi-three-level", "--mem-size", "3GiB",
        "--resource-dir", str(RESOURCE_DIR),
        "--aux-disk", str(spec["local_aux_disk"]),
        "--detailed-warmup-insts", "0",
        "--stats-outfile", str(stats),
        "--roi-user-records-per-core", str(target),
        "--roi-safety-max-insts-per-core", str(SAFETY_MULTIPLIER_INSTS),
        "--wait-for-roi-workbegin",
        "--tao-trace-dir", str(trace_dir),
        "--tao-trace-format", "fst",
        "--tao-measure-cpl",
        "--tao-native-anomaly-limit", "32",
        "--tao-functional-warmup",
        "--tao-functional-user-target", str(target),
        # minesim mainline switch: pure user scope, symmetric with QEMU-FST.
        "--tao-functional-user-only",
    ]
    started = time.time()
    with log.open("w", encoding="utf-8") as sink:
        result = subprocess.run(command, stdout=sink, stderr=subprocess.STDOUT)
    elapsed = time.time() - started
    normalize_error = None
    if result.returncode == 0:
        try:
            normalize(trace_dir, 4, functional_warmup=True,
                      functional_include_kernel=False)
        except Exception as error:  # promotion validates real evidence
            normalize_error = str(error)
    shards = sorted(trace_dir.glob("core*.fst"))
    ok = (result.returncode == 0 and normalize_error is None
          and len(shards) == 4 and all(p.stat().st_size > 0 for p in shards))
    return {
        "workload": workload, "reused": False, "returncode": result.returncode,
        "elapsed_s": round(elapsed, 1), "ok": ok, "trace_dir": trace_dir,
        "log": log, "normalize_error": normalize_error,
    }


def run(args: argparse.Namespace) -> int:
    out_root = _require_collection_root(Path(args.output_root))
    if not GEM5.is_file():
        raise FileNotFoundError(f"gem5 TaoTrace binary missing: {GEM5}")
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    names = args.workload or sorted(plan)
    missing = sorted(set(names) - set(plan))
    if missing:
        raise ValueError(f"unknown workload(s): {', '.join(missing)}")
    env = os.environ.copy()
    env.setdefault("LD_LIBRARY_PATH", "/opt/gcc-11.5.0/lib64")
    # The tao wrapper shells out to the effective-target sidecar generator.
    env["FASTSIM_EFFECTIVE_TARGET_GENERATOR"] = str(EFFECTIVE_TARGET_GENERATOR)
    env["FASTSIM_EFFECTIVE_TARGET_PYTHON"] = os.environ.get(
        "FASTSIM_EFFECTIVE_TARGET_PYTHON", "/usr/bin/python3"
    )
    env["FASTSIM_EVENT_DICTIONARY"] = str(EVENT_DICTIONARY)
    os.environ.update(env)
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.jobs, len(names))
    ) as executor:
        futures = {
            executor.submit(
                _collect_one, name, plan[name], out_root, args.target,
            ): name
            for name in names
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    failures = [r for r in results if not r.get("ok")]
    for r in sorted(results, key=lambda x: x["workload"]):
        state = "ok" if r["ok"] else f"FAIL rc={r['returncode']}"
        print(f"{r['workload']}: {state} {r['elapsed_s']}s -> "
              f"{r['trace_dir']}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.taotrace_fst.collect")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
