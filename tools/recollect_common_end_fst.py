#!/usr/bin/env python3
"""Replace an explicit previous corpus using first-core 10M common-end capture.

Requires a passing pilot before deleting only inventoried FSTs/companions.
Preserves checkpoints, disks, prior oracles and reports. Each case has a durable
command and status; no retries or silently resumed partial captures.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from collect_common_end_fst import finalize_capture, save, sha256
from common_end_capture import POLICY

ROOT = Path(__file__).resolve().parents[1]


def environment():
    result = dict(os.environ)
    result.pop("PYTHONHOME", None)
    result.update(
        FASTSIM_EFFECTIVE_TARGET_GENERATOR=str(ROOT / "tools/generate_fs_effective_target.py"),
        FASTSIM_EFFECTIVE_TARGET_PYTHON="/data00/yinhaolang/infer/.venv/bin/python",
        FASTSIM_EVENT_DICTIONARY=str(ROOT / "configs/pmu-event-dictionary-v1.json"),
        TAOGEN_SHARED="/data00/yinhaolang/taogen/shared")
    return result


def prepare(args):
    previous = json.loads(args.previous_inventory.read_text())
    cases = previous["cases"]
    if len(cases) != 40 or {(c["cores"]) for c in cases} != {4, 8, 16, 32}:
        raise ValueError("expected the explicit previous 10-workload/4-core-count corpus")
    if len({c["case"] for c in cases}) != 40:
        raise ValueError("duplicate previous cases")
    binary_sha = sha256(args.collector)
    pilot = json.loads(args.pilot_audit.read_text())
    if pilot.get("measurement_policy") != POLICY or pilot.get("collector_sha256") != binary_sha:
        raise ValueError("a passing pilot from this exact collector is required")
    if shutil.disk_usage(ROOT).free < 512 * 1024**3:
        raise ValueError("less than 512 GiB free before replacement")
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "inventory.json").exists():
        raise FileExistsError("refusing to overwrite a previous collection")
    prepared, deletion = [], []
    for old in cases:
        source = Path(old["result_dir"])
        folder = args.output / "source" / old["case"]
        folder.mkdir(parents=True)
        command = old["command"][:]
        checkpoint = Path(command[command.index("--checkpoint-dir") + 1])
        if not (checkpoint / "m5.cpt").is_file():
            raise FileNotFoundError(checkpoint)
        if not Path(command[command.index("--aux-disk") + 1]).is_file():
            raise FileNotFoundError("missing workload disk")
        command[0] = str(args.collector)
        for flag, value in {"-d": str(folder), "--stats-outfile": str(folder / "stats.txt"),
                            "--tao-trace-dir": str(folder / "trace-scratch"),
                            "--roi-user-records-per-core": "10000000",
                            "--tao-functional-user-target": "10000000"}.items():
            command[command.index(flag) + 1] = value
        if "--roi-stop-policy" in command:
            command[command.index("--roi-stop-policy") + 1] = "any-core"
        else:
            command += ["--roi-stop-policy", "any-core"]
        wrapper = Path(command[command.index("-d") + 2])
        if not wrapper.is_file():
            raise FileNotFoundError(wrapper)
        trace = json.loads((source / "tao_trace/trace.json").read_text())
        if set(map(int, trace["per_core"])) != set(range(old["cores"])):
            raise ValueError("previous trace participants mismatch")
        for core, row in trace["per_core"].items():
            fst = Path(row["fst"])
            if not fst.is_absolute():
                fst = source / "tao_trace" / fst
            if fst.parent.resolve() != (source / "tao_trace").resolve():
                raise ValueError(f"unexpected deletion source: {fst}")
            for path in [fst] + [Path(str(fst) + s) for s in (".vmap", ".asmap", ".imap", ".deps")]:
                if path == fst or path.exists():
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT / "tmp"):
                        raise ValueError(f"unsafe deletion entry: {path}")
                    stat = path.stat()
                    deletion.append(dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))
        request = json.loads((source / "request.json").read_text())
        request["gem5"] = dict(binary=str(args.collector), binary_sha256=binary_sha,
                               config=str(wrapper), config_sha256=sha256(wrapper))
        request["sampling"].update(roi_stop_policy="any-core", measurement_policy=POLICY,
                                    functional_trace_dir=str(folder / "tao_trace"),
                                    oracle_dir=str(folder / "oracle"))
        request["recollection"] = dict(reason=POLICY, previous_result=str(source),
                                        old_traces_deleted_before_collection=True)
        save(folder / "request.json", request)
        save(folder / "previous-trace.json", trace)
        shutil.copy2(source / "effective-target.json", folder / "previous-effective-target.json")
        case = dict(old, result_dir=str(folder), previous_result_dir=str(source), command=command,
                    previous_reference=old.get("reference"), reference=None,
                    previous_manifest=old.get("manifest"),
                    manifest=str(folder / "tao_trace/manifest.txt"),
                    original_manifest=str(folder / "tao_trace/manifest.txt"),
                    collector_sha256=binary_sha, wrapper_sha256=sha256(wrapper))
        prepared.append(case)
        save(folder / "collection-plan.json", case)
    if len({r["path"] for r in deletion}) != len(deletion):
        raise ValueError("duplicate deletion entries")
    if sum(r["path"].endswith(".fst") for r in deletion) != 600:
        raise ValueError("expected exactly 600 previous FSTs")
    save(args.output / "inventory.json", dict(cases=prepared, measurement_policy=POLICY,
         collector_sha256=binary_sha, jobs=args.jobs, deletion=deletion,
         deletion_bytes=sum(r["bytes"] for r in deletion), pilot=str(args.pilot_audit)))
    # The user explicitly requested deleting this corpus before recollecting.
    for row in deletion:
        path = Path(row["path"])
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (row["bytes"], row["mtime_ns"]):
            raise RuntimeError(f"deletion source changed: {path}")
        path.unlink()
    save(args.output / "deletion-complete.json", dict(files=len(deletion), fst_files=600,
         bytes=sum(r["bytes"] for r in deletion), completed=time.time(),
         all_removed=all(not Path(r["path"]).exists() for r in deletion)))
    return prepared


def collect(case, args):
    folder = Path(case["result_dir"])
    command = ["numactl", "--cpunodebind=" + str(case["node"]),
               "--membind=" + str(case["node"])] + case["command"]
    status = dict(case=case["case"], phase="collecting", started=time.time(), command=command)
    try:
        with (folder / "run.log").open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment())
            status["pid"] = process.pid
            save(folder / "status.json", status)
            print("START", case["case"], process.pid, flush=True)
            try:
                rc = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RuntimeError("collection timeout")
        if rc:
            raise RuntimeError(f"gem5 exit {rc}")
        status.update(phase="finalizing", collection_seconds=time.time() - status["started"])
        save(folder / "status.json", status)
        gate = finalize_capture(folder, cores=case["cores"], target=10000000, tcsim_root=args.tcsim_root)
        from validate_fs_oracle_identity import validate_result_identity
        from validate_kernel_events_oracle import validate_document
        identity = validate_result_identity(folder)
        save(folder / "identity.json", identity)
        if not identity["valid"]:
            raise ValueError(f"target identity failed: {identity}")
        oracle = validate_document(json.loads((folder / "oracle/kernel_events.json").read_text()),
                                   max_unknown_ratio=0.0)
        save(folder / "oracle-gate.json", oracle)
        if not oracle["formal_pmu_eligible"]:
            raise ValueError(f"oracle gate failed: {oracle}")
        status.update(phase="complete", common_end=gate)
    except Exception as error:
        status.update(phase="failed", error=str(error))
    status.update(finished=time.time(), wall_seconds=time.time() - status["started"])
    save(folder / "status.json", status)
    print(status["phase"].upper(), case["case"], round(status["wall_seconds"], 1), flush=True)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--pilot-audit", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=28800)
    parser.add_argument("--tcsim-root", type=Path, default="/data00/yinhaolang/TCSim")
    args = parser.parse_args()
    args.output = args.output.resolve()
    cases = prepare(args)
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(collect, case, args) for case in cases]
        for future in as_completed(futures):
            results.append(future.result())
            save(args.output / "collection-results.json", results)
    summary = dict(finished=time.time(), cases=len(results),
                   complete=sum(r["phase"] == "complete" for r in results),
                   failed=sum(r["phase"] == "failed" for r in results))
    save(args.output / "collection-finished.json", summary)
    print(json.dumps(summary), flush=True)
    return int(summary["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
