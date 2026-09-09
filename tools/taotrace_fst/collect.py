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

# Local gem5 TaoTrace FS build (X86_MESI_Three_Level -> X86_TAOTRACE_FST).
GEM5 = Path("/data00/xuhaoen/gem5_taotrace/build/X86_MESI_Three_Level/gem5.opt")
# Upstream kernel/resource tree is large and stable; reused read-only.
RESOURCE_DIR = Path("/data00/yinhaolang/gem5-fs/resources")

DEFAULT_PLAN = PROJECT_ROOT / "var/qemu_fst/tao_collect_plan.json"
DEFAULT_OUT_ROOT = PROJECT_ROOT / "var/qemu_fst/taotrace_useronly/c04"
DEFAULT_TARGET = 10_000_000
SAFETY_MULTIPLIER_INSTS = 1_000_000_000


def _collect_one(workload: str, spec: dict, out_root: Path, target: int,
                 force: bool) -> dict:
    out_dir = out_root / workload
    trace_dir = out_dir / "tao_trace"
    stats = out_dir / "stats.txt"
    done = trace_dir / "core0.fst"
    if done.is_file() and not force:
        return {"workload": workload, "reused": True, "trace_dir": trace_dir}
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
    if not GEM5.is_file():
        raise FileNotFoundError(f"gem5 TaoTrace binary missing: {GEM5}")
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    names = args.workload or sorted(plan)
    missing = sorted(set(names) - set(plan))
    if missing:
        raise ValueError(f"unknown workload(s): {', '.join(missing)}")
    out_root = Path(args.output_root)
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
                args.force,
            ): name
            for name in names
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    failures = [r for r in results if not r.get("reused") and not r.get("ok")]
    for r in sorted(results, key=lambda x: x["workload"]):
        if r.get("reused"):
            print(f"{r['workload']}: reused {r['trace_dir']}")
        else:
            state = "ok" if r["ok"] else f"FAIL rc={r['returncode']}"
            print(f"{r['workload']}: {state} {r['elapsed_s']}s -> "
                  f"{r['trace_dir']}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.taotrace_fst.collect")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
