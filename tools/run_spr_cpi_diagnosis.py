#!/usr/bin/env python3
"""Controlled SPR CPI ablations, scored only by the maintained native validator.

Every variant retains the complete functional manifest and task plan. No native
timing or cache outcome enters the model. Outputs are mechanism diagnostics,
not hardware certification or isolated host-throughput measurements.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from run_native_kernel_fastsim_validation import config_include_chain


VARIANTS = {
    "base": {},
    "gap": {"core.spr_shared_fu_gap_aware": "true"},
    "window": {"core.spr_store_window_feedback": "true"},
    "gap-window": {
        "core.spr_shared_fu_gap_aware": "true",
        "core.spr_store_window_feedback": "true",
    },
    "prepare-off": {"core.legacy_prepare_timing": "false"},
    "gap-window-prefetch": {
        "core.spr_shared_fu_gap_aware": "true",
        "core.spr_store_window_feedback": "true",
        "core.spr_l1_prefetch_l2_only": "true",
        "core.spr_l2_llc_resident_prefetch": "true",
    },
    "store-audit": {
        "core.spr_store_window_feedback": "true",
        "sim.cpi_attribution": "true",
        "core.response_materialized_uop_fast_kernel": "false",
    },
    "tail-audit": {"core.response_interval_tail_audit": "true"},
}


def load(path):
    return json.loads(path.read_text())


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_population(document):
    return {
        "identity": document["functional_source_identity"],
        "tasks": document["time_accounting"]["task_ids"],
        "macros": [
            (row["core_id"], row["task_macro_instructions"])
            for row in document["time_accounting"]["per_core"]
        ],
        "uops": [(row["core"], row["uops"]) for row in document["cores"]],
    }


def execute(command, log, repo):
    with log.open("w") as stream:
        stream.write("command=" + json.dumps(command) + "\n")
        stream.flush()
        return subprocess.run(command, cwd=repo, stdout=stream,
                              stderr=subprocess.STDOUT).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1 or len(set(args.case)) != len(args.case):
        parser.error("positive jobs and unique cases required")
    if len(set(args.variant)) != len(args.variant):
        parser.error("variants must be unique")
    repo = Path(__file__).resolve().parents[1]
    campaign = args.campaign_root.resolve()
    out = args.output_dir.resolve()
    binary = args.fastsim.resolve()
    if not binary.is_file() or not binary.stat().st_size or not os.access(binary, os.X_OK):
        parser.error("FastSim must be a nonempty executable; rebuild missing targets first")
    if out.exists() and not args.resume:
        parser.error("output already exists; select a new directory or --resume")
    out.mkdir(parents=True, exist_ok=True)
    binary_hash = sha(binary)
    source_root = out / "sources"
    source_root.mkdir(exist_ok=True)
    for case in args.case:
        if Path(case).name != case:
            parser.error("case IDs must be directory names")
        source = campaign / "sources" / case
        if not (source / "request.json").is_file():
            parser.error(f"missing prepared source {case}")
        link = source_root / case
        if not link.exists():
            link.symlink_to(source, target_is_directory=True)
        if link.resolve() != source.resolve():
            parser.error(f"source link changed for {case}")
    identity_command = [
        sys.executable, str(repo / "tools/validate_fs_oracle_identity.py"),
        "--output", str(out / "identity.json"),
    ]
    # Path.rglob does not descend directory symlinks. Audit the resolved
    # selected results explicitly rather than silently discovering zero cases.
    for case in args.case:
        identity_command += ["--result", str((source_root / case).resolve())]
    if execute(identity_command, out / "identity.log", repo):
        raise RuntimeError("independent hardware identity gate failed")
    identity = load(out / "identity.json")["results"]
    if len(identity) != len(args.case) or not all(r["valid"] for r in identity):
        raise RuntimeError("identity case coverage failed")

    def replay(item):
        case, variant = item
        dest = out / variant
        dest.mkdir(exist_ok=True)
        config = dest / (case + ".cfg")
        base_config = campaign / "derived-config" / case / "recorded.cfg"
        text = "config.include = " + str(base_config) + "\n"
        text += "".join(f"{key} = {value}\n"
                        for key, value in VARIANTS[variant].items())
        if config.exists() and config.read_text() != text:
            raise RuntimeError(f"refusing changed replay config {config}")
        config.write_text(text)
        command = [
            str(binary), "simulate", "--config", str(config),
            "--manifest", str(campaign / "sources" / case /
                              "tao_trace/manifest.txt"),
            "--measurement-scope", "user-plus-kernel", "--cores", "4",
            "--perf-cache-input-identity", "true",
            "--output", str(dest / (case + ".json")),
        ]
        receipt_path = dest / (case + ".receipt.json")
        receipt = {
            "binary_sha256": binary_hash,
            "command": command,
            "overrides": VARIANTS[variant],
            "config_chain": [
                {"path": str(path), "sha256": sha(path)}
                for path in config_include_chain(config)
            ],
        }
        if receipt_path.exists() and (dest / (case + ".json")).exists():
            previous = load(receipt_path)
            if (all(previous.get(k) == v for k, v in receipt.items()) and
                    previous.get("returncode") == 0 and
                    previous.get("output_sha256") == sha(dest / (case + ".json"))):
                return previous
            raise RuntimeError(f"existing replay lacks matching receipt: {case}/{variant}")
        started = time.monotonic()
        receipt["returncode"] = execute(command, dest / (case + ".log"), repo)
        receipt["host_wall_seconds"] = time.monotonic() - started
        if sha(binary) != binary_hash:
            raise RuntimeError("binary changed during campaign")
        if receipt["returncode"] == 0:
            document = load(dest / (case + ".json"))
            archived = load(campaign / "combined-stats" / (case + ".json"))
            receipt["same_functional_population_as_campaign"] = (
                model_population(document) == model_population(archived))
            if not receipt["same_functional_population_as_campaign"]:
                receipt["returncode"] = 1
                receipt["error"] = "source/task/per-core macro/UOP population changed"
            receipt["output_sha256"] = sha(dest / (case + ".json"))
        save(receipt_path, receipt)
        return receipt

    items = [(case, variant) for variant in args.variant for case in args.case]
    save(out / "plan.json", {
        "pid": os.getpid(), "binary_sha256": binary_hash,
        "cases": args.case, "variants": args.variant,
        "jobs": args.jobs, "hypotheses": [
            "shared-FU future reservation hides earlier free slots",
            "serial store drain differs from bounded four-line window",
            "prepare calendar changes exposed SQ capacity waits",
            "resident prefetch changes demand service and CPI",
        ],
        "scope": "same-functional-input workload-task active CPI diagnostic",
        "throughput": "not isolated; no throughput acceptance claim",
    })
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        receipts = list(pool.map(replay, items))
    save(out / "replays.json", receipts)
    if any(row["returncode"] for row in receipts):
        raise RuntimeError("one or more replay/population gates failed")

    rows = []
    for variant in args.variant:
        dest = out / variant
        validation = dest / "validation"
        command = [
            sys.executable, str(repo / "tools/run_native_kernel_fastsim_validation.py"),
            "--source-root", str(source_root), "--stats-dir", str(dest),
            "--output-dir", str(validation), "--expected-cases", str(len(args.case)),
            "--jobs", str(args.jobs), "--retired-cache-per-event",
        ]
        rc = execute(command, dest / "validation.log", repo)
        if rc:
            raise RuntimeError(f"maintained source/native gate failed: {variant}")
        for case in args.case:
            scored = load(validation / "cases" / case / "validation.json")
            cpi = scored["cpi"]
            if (scored["status"] != "passed" or cpi["status"] != "scored-active" or
                    not cpi.get("per_core_instruction_population_match")):
                raise RuntimeError(f"active CPI gate failed: {case}/{variant}")
            accounting = scored["time_accounting"]
            predicted, reference = accounting["fastsim"], accounting["gem5"]
            completion = predicted["per_core"]
            slowest = max(completion, key=lambda r: r["completion_reference_cycles"])
            rows.append({
                "case": case, "variant": variant, "cpi": cpi,
                "core_cycle_sum": predicted["active_cycles"],
                "per_core": completion, "native_per_core": reference["per_core"],
                "elapsed_seconds": predicted["elapsed_time_seconds"],
                "slowest_completion_core": slowest["core_id"],
                "slowest_completion_cycles": slowest["completion_reference_cycles"],
                "validation": str(validation / "cases" / case / "validation.json"),
            })
    save(out / "comparison.json", rows)
    lines = [
        "# SPR CPI controlled diagnosis", "",
        "Same full functional input and task population; native truth selected by "
        "the maintained validator. Strict functional start alignment and hardware "
        "equivalence remain unverified. Concurrent replay wall times are not a "
        "throughput comparison.", "",
        "| case | variant | gem5 CPI | FastSim CPI | absolute error (cycles/macro) | signed relative error | active cycle sum | slowest core |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        cpi = row["cpi"]
        lines.append(
            f"| {row['case']} | {row['variant']} | {cpi['gem5']:.9f} | "
            f"{cpi['fastsim']:.9f} | {cpi['absolute_error']:.9f} | "
            f"{cpi['relative_error']:+.4%} | {row['core_cycle_sum']} | "
            f"{row['slowest_completion_core']} |")
    lines += ["", "| variant | case count | CPI MAE (cycles/macro) | CPI MAPE |",
              "|---|---:|---:|---:|"]
    for variant in args.variant:
        selected = [r["cpi"] for r in rows if r["variant"] == variant]
        lines.append(
            f"| {variant} | {len(selected)} | "
            f"{sum(c['absolute_error'] for c in selected)/len(selected):.9f} | "
            f"{sum(c['absolute_relative_error'] for c in selected)/len(selected):.4%} |")
    (out / "comparison.md").write_text("\n".join(lines) + "\n")
    save(out / "completion.json", {"passed": True, "rows": len(rows),
                                   "binary_sha256": binary_hash})


if __name__ == "__main__":
    main()
