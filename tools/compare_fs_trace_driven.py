#!/usr/bin/env python3
"""Run FastSim, Sniper, and Zsim on matched FastSim FS FST slices."""

import argparse
import ast
import csv
import concurrent.futures
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_WORKLOADS = (
    "706.stockfish_r",
    "710.omnetpp_r",
    "777.zstd_r",
    "782.lbm_r",
    "811.tealeaf_s",
    "854.graph500_s",
)
ROI_TIME_RE = re.compile(r"Leaving ROI after ([0-9.]+) seconds")


def parse_args():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case-root",
        type=Path,
        default=root / "tmp/fs-c8-functional-roi-final-v2/cases",
    )
    parser.add_argument(
        "--gem5-summary",
        type=Path,
        default=root / "tmp/fs-c8-functional-roi-final-v2/summary.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "tmp/sniper-zsim-trace-driven/fs-c8-comparison-v1",
    )
    parser.add_argument("--workloads", nargs="+", default=list(DEFAULT_WORKLOADS))
    parser.add_argument("--cores", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--limit-instructions-per-core", type=int, default=0)
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument(
        "--simulators",
        nargs="+",
        choices=("fastsim", "sniper", "zsim"),
        default=("fastsim", "sniper", "zsim"),
    )
    parser.add_argument("--fastsim", type=Path, default=root / "build/fastsim")
    parser.add_argument(
        "--fastsim-config",
        type=Path,
        default=root / "configs/gem5-v28_1-fs-user.cfg",
    )
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        default=12,
        help="Accepted FastSim FS timing-walk service in cycles (default: 12).",
    )
    parser.add_argument(
        "--sniper-root",
        type=Path,
        default=root / "tmp/sniper-isa-audit",
    )
    parser.add_argument(
        "--sniper-config",
        default="fastsim-fs-c8",
    )
    parser.add_argument(
        "--sniper-binary",
        type=Path,
        default=root
        / "tmp/sniper-zsim-trace-driven/sniper-matched-build/sniper",
    )
    parser.add_argument(
        "--zsim-root",
        type=Path,
        default=Path("/data00/yinhaolang/Zsim/zsim"),
    )
    parser.add_argument(
        "--zsim-template",
        type=Path,
        default=Path(
            "/data00/yinhaolang/Zsim/zsim/tests/"
            "fastsim-fs-c8.template.cfg"
        ),
    )
    args = parser.parse_args()
    if args.cores <= 0:
        parser.error("--cores must be positive")
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    if args.limit_instructions_per_core < 0:
        parser.error("--limit-instructions-per-core must be nonnegative")
    if args.dtlb_page_walk_latency <= 0:
        parser.error("--dtlb-page-walk-latency must be positive")
    return args


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_baselines(path):
    with path.open(newline="") as handle:
        return {row["workload"]: row for row in csv.DictReader(handle)}


def read_manifest(path, cores, limit):
    entries = []
    base = path.resolve().parent
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 3:
            raise SystemExit("malformed manifest entry: %s" % raw_line)
        core = int(fields[0])
        fmt = fields[1]
        trace = Path(fields[2])
        if not trace.is_absolute():
            trace = base / trace
        source_core = core
        skip = 0
        take = 0
        if fmt in ("fastsim-binary-slice", "binary-slice"):
            if len(fields) != 6:
                raise SystemExit("malformed slice entry: %s" % raw_line)
            source_core = int(fields[3])
            skip = int(fields[4])
            take = int(fields[5])
        elif fmt in ("fastsim-binary", "binary"):
            if len(fields) == 4:
                source_core = int(fields[3])
        else:
            raise SystemExit(
                "comparison requires cold binary slices, got %s" % fmt
            )
        if limit:
            take = min(take, limit) if take else limit
            fmt = "fastsim-binary-slice"
        entries.append(
            {
                "core": core,
                "format": fmt,
                "path": trace.resolve(),
                "source_core": source_core,
                "skip": skip,
                "take": take,
            }
        )
    if [entry["core"] for entry in entries] != list(range(cores)):
        raise SystemExit("manifest core IDs must be dense 0..%d" % (cores - 1))
    for entry in entries:
        if entry["source_core"] != entry["core"]:
            raise SystemExit("comparison does not accept replicated core traces")
        if not entry["path"].is_file():
            raise SystemExit("missing FST: %s" % entry["path"])
        if entry["take"] <= 0:
            raise SystemExit("comparison requires bounded per-core slices")
    return entries


def write_manifest(path, entries):
    with path.open("w") as handle:
        for entry in entries:
            handle.write(
                "%d fastsim-binary-slice %s %d %d %d\n"
                % (
                    entry["core"],
                    entry["path"],
                    entry["source_core"],
                    entry["skip"],
                    entry["take"],
                )
            )


def run_logged(command, cwd, stdout_path, stderr_path, env=None):
    started = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=stdout,
            stderr=stderr,
            env=env,
            check=False,
        )
    elapsed = time.monotonic() - started
    if result.returncode:
        raise SystemExit(
            "command failed (%d): %s\nstderr: %s"
            % (result.returncode, " ".join(command), stderr_path)
        )
    return elapsed


def run_fastsim(args, case_dir, manifest):
    out = case_dir / "fastsim"
    out.mkdir(parents=True, exist_ok=True)
    stats_path = out / "stats.json"
    if args.reuse_existing and stats_path.is_file():
        return
    command = [
        str(args.fastsim.resolve()),
        "simulate",
        "--measurement-scope",
        "user",
        "--config",
        str(args.fastsim_config.resolve()),
        "--manifest",
        str(manifest.resolve()),
        "--cores",
        str(args.cores),
        "--dtlb-miss-model",
        "timing_walk",
        "--dtlb-page-walk-latency",
        str(args.dtlb_page_walk_latency),
        "--allow-mmio-escape",
        "true",
        "--allow-cross-page-without-virtual-token",
        "true",
        "--dram-size",
        "3221225472",
        "--output",
        str(stats_path.resolve()),
    ]
    elapsed = run_logged(
        command, args.fastsim.parent, out / "stdout.log", out / "stderr.log"
    )
    (out / "e2e-seconds.txt").write_text("%.9f\n" % elapsed)
    (out / "command.json").write_text(json.dumps(command, indent=2) + "\n")


def sniper_specs(entries):
    return ",".join(
        "fastsim-slice@%d@%d@%s"
        % (entry["skip"], entry["take"], entry["path"])
        for entry in entries
    )


def run_sniper(args, case_dir, entries):
    out = case_dir / "sniper"
    out.mkdir(parents=True, exist_ok=True)
    stats_path = out / "simulation/sim.stats.sqlite3"
    if args.reuse_existing and stats_path.is_file():
        return
    command = [
        str((args.sniper_root / "run-sniper").resolve()),
        "-n",
        str(args.cores),
        "-d",
        str(out.resolve()),
        "-c",
        "address_translation_schemes/baseline",
        "-c",
        args.sniper_config,
        "-ggeneral/total_cores=%d" % args.cores,
        "--sim-end=last",
        "--traces=" + sniper_specs(entries),
    ]
    env = os.environ.copy()
    env["SNIPER_SIM_LD_LIBRARY_PATH"] = (
        "/root/.local/share/uv/python/"
        "cpython-3.11.14-linux-x86_64-gnu/lib"
    )
    env["SNIPER_STANDALONE_BINARY"] = str(
        args.sniper_binary.resolve()
    )
    elapsed = run_logged(
        command,
        args.sniper_root,
        out / "console.log",
        out / "console.err",
        env=env,
    )
    (out / "e2e-seconds.txt").write_text("%.9f\n" % elapsed)
    (out / "command.json").write_text(json.dumps(command, indent=2) + "\n")


def run_zsim(args, case_dir, manifest):
    out = case_dir / "zsim"
    out.mkdir(parents=True, exist_ok=True)
    stats_path = out / "zsim.out"
    if args.reuse_existing and stats_path.is_file():
        return
    config = args.zsim_template.read_text().replace(
        "@MANIFEST@", str(manifest.resolve())
    )
    config_path = out / "config.cfg"
    config_path.write_text(config)
    command = [
        str((args.zsim_root / "build/opt/zsim").resolve()),
        str(config_path.resolve()),
    ]
    elapsed = run_logged(
        command, out, out / "stdout.log", out / "stderr.log"
    )
    (out / "e2e-seconds.txt").write_text("%.9f\n" % elapsed)
    (out / "command.json").write_text(json.dumps(command, indent=2) + "\n")


def parse_fastsim(case_dir, cores):
    stats = json.loads((case_dir / "fastsim/stats.json").read_text())
    totals = stats["totals"]
    uops = totals["retired_uops"]
    cycles = [core["cycles"] for core in stats["cores"]]
    return {
        "uops": uops,
        "uops_by_core": [core["uops"] for core in stats["cores"]],
        "instructions": totals["retired_instructions"],
        "cycles": cycles,
        "label_cycles": cores * max(cycles),
        "active_cycles": sum(cycles),
        "sim_seconds": stats["wall_time_seconds"],
        "e2e_seconds": float(
            (case_dir / "fastsim/e2e-seconds.txt").read_text()
        ),
        "branch_misses": sum(core["branch_misses"] for core in stats["cores"]),
        "branch_misses_by_core": [
            core["branch_misses"] for core in stats["cores"]
        ],
    }


def parse_sniper(case_dir, sniper_root, cores):
    sys.path.insert(0, str((sniper_root / "tools").resolve()))
    import sniper_lib

    results = sniper_lib.get_results(
        resultsdir=str(case_dir / "sniper/simulation")
    )["results"]
    uops_by_core = results["performance_model.instruction_count"][:cores]
    synchronized_cycles = results["performance_model.cycle_count"][:cores]
    elapsed_time = results["performance_model.elapsed_time"][:cores]
    nonidle_time = results["performance_model.nonidle_elapsed_time"][:cores]
    cycles = [
        (
            nonidle * synchronized / elapsed
            if elapsed
            else 0
        )
        for nonidle, synchronized, elapsed in zip(
            nonidle_time, synchronized_cycles, elapsed_time
        )
    ]
    console_path = case_dir / "sniper/console.log"
    if not console_path.is_file():
        console_path = case_dir / "sniper/logs/console.log"
    console = console_path.read_text(errors="replace")
    match = ROI_TIME_RE.search(console)
    sim_seconds = float(match.group(1)) if match else None
    return {
        "uops": sum(uops_by_core),
        "uops_by_core": uops_by_core,
        "instructions": sum(uops_by_core),
        "cycles": cycles,
        "label_cycles": cores * max(cycles),
        "active_cycles": sum(cycles),
        "sim_seconds": sim_seconds,
        "e2e_seconds": float(
            (case_dir / "sniper/e2e-seconds.txt").read_text()
        ),
        "branch_misses": sum(
            results.get("branch_predictor.num-incorrect", [0] * cores)[:cores]
        ),
        "branch_misses_by_core": results.get(
            "branch_predictor.num-incorrect", [0] * cores
        )[:cores],
    }


def parse_zsim_stats(path, cores):
    values = {"cycles": [], "instrs": [], "uops": [], "mispredBranches": []}
    sim_time_ns = 0
    in_time = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if line == " time: # Simulator time breakdown":
            in_time = True
            continue
        if in_time:
            if line.startswith("  ") and not line.startswith("   "):
                key, value = stripped.split(":", 1)
                if key in ("bound", "weave"):
                    sim_time_ns += int(value.strip())
                continue
            in_time = False
        for key in values:
            if stripped.startswith(key + ":"):
                values[key].append(
                    int(stripped.split(":", 1)[1].split("#", 1)[0].strip())
                )
    for key in values:
        if len(values[key]) != cores:
            raise SystemExit(
                "expected %d Zsim %s stats, got %d"
                % (cores, key, len(values[key]))
            )
    return values, sim_time_ns / 1e9


def parse_zsim(case_dir, cores):
    values, sim_seconds = parse_zsim_stats(
        case_dir / "zsim/zsim.out", cores
    )
    cycles = values["cycles"]
    return {
        "uops": sum(values["uops"]),
        "uops_by_core": values["uops"],
        "instructions": sum(values["instrs"]),
        "cycles": cycles,
        "label_cycles": cores * max(cycles),
        "active_cycles": sum(cycles),
        "sim_seconds": sim_seconds,
        "e2e_seconds": float(
            (case_dir / "zsim/e2e-seconds.txt").read_text()
        ),
        "branch_misses": sum(values["mispredBranches"]),
        "branch_misses_by_core": values["mispredBranches"],
    }


def rel_error(value, reference):
    return (value - reference) / reference if reference else None


def result_row(workload, simulator, result, baseline, full_roi):
    uops = result["uops"]
    label_cpi = result["label_cycles"] / uops
    active_cpi = result["active_cycles"] / uops
    gem5_cpi = float(baseline["gem5_uop_cpi"]) if full_roi else None
    sim_seconds = result["sim_seconds"]
    return {
        "workload": workload,
        "simulator": simulator,
        "uops": uops,
        "instructions": result["instructions"],
        "label_scope_cycles": result["label_cycles"],
        "active_cycles": result["active_cycles"],
        "uop_cpi": label_cpi,
        "active_uop_cpi": active_cpi,
        "gem5_uop_cpi": gem5_cpi,
        "cpi_signed_error": (
            rel_error(label_cpi, gem5_cpi) if gem5_cpi is not None else None
        ),
        "sim_seconds": sim_seconds,
        "e2e_seconds": result["e2e_seconds"],
        "sim_uops_per_second": (
            uops / sim_seconds if sim_seconds and sim_seconds > 0 else None
        ),
        "e2e_uops_per_second": uops / result["e2e_seconds"],
        "branch_misses": result["branch_misses"],
    }


def percentile(values, q):
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = int(math.floor(index))
    high = int(math.ceil(index))
    if low == high:
        return ordered[low]
    weight = index - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def write_outputs(args, rows, case_reports):
    args.output.mkdir(parents=True, exist_ok=True)
    csv_path = args.output / "summary.csv"
    fields = list(rows[0]) if rows else []
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    aggregate = {}
    for simulator in args.simulators:
        selected = [row for row in rows if row["simulator"] == simulator]
        errors = [
            abs(row["cpi_signed_error"])
            for row in selected
            if row["cpi_signed_error"] is not None
        ]
        aggregate[simulator] = {
            "cases": len(selected),
            "cpi_abs_error_mean": statistics.mean(errors) if errors else None,
            "cpi_abs_error_median": (
                statistics.median(errors) if errors else None
            ),
            "cpi_abs_error_p90": percentile(errors, 0.90) if errors else None,
            "cpi_abs_error_max": max(errors) if errors else None,
            "minimum_sim_uops_per_second": min(
                row["sim_uops_per_second"]
                for row in selected
                if row["sim_uops_per_second"] is not None
            ),
            "median_sim_uops_per_second": statistics.median(
                row["sim_uops_per_second"]
                for row in selected
                if row["sim_uops_per_second"] is not None
            ),
            "minimum_e2e_uops_per_second": min(
                row["e2e_uops_per_second"] for row in selected
            ),
        }
    report = {
        "schema": "fastsim-sniper-zsim-fs-comparison-v2",
        "cores": args.cores,
        "limit_instructions_per_core": args.limit_instructions_per_core,
        "rows": rows,
        "cases": case_reports,
        "aggregate": aggregate,
        "model_alignment": {
            "common": [
                "same committed functional FST v6 records",
                "same per-core macro-aligned skip/take boundaries",
                "3 GHz target clock",
                "8 cores, 8-wide target, ROB 192, IQ 64, LQ/SQ 32",
                "same FastSim FU pools, unit counts, latencies, and "
                "non-pipelined divide/sqrt occupancy",
                "same tournament direction predictor, BTB, RAS, and "
                "indirect-target geometry",
                "L1I/L1D 32 KiB 8-way, private L2 1 MiB 8-way",
                "shared LLC 64 MiB 16-way",
                "8 DRAM channels, 2 ranks/channel, 16 banks/rank, "
                "8 KiB rows, 43-cycle tCL/tRCD/tRP, 10-cycle burst",
            ],
            "sniper_limitations": [
                "Sniper ROB does not separately expose FastSim's fetch "
                "queue and fetch/decode/rename/writeback stage calendars",
                "Sniper uses one shared LLC controller, parametric MSI, and "
                "a mesh rather than eight Ruby CHAs and the fixed 5-cycle NoC",
                "Sniper detailed DDR matches topology and primary timing but "
                "not FastSim's open_adaptive FR-FCFS selection policy",
                "Sniper does not time FST virtual-page-token DTLB misses",
                "offline physical FST replay disables online clock-skew barrier",
            ],
            "zsim_limitations": [
                "Zsim does not expose a separate writeback-width calendar",
                "Zsim coherence/cache path is not gem5 Ruby MESI_Three_Level",
                "Zsim DDR matches topology and primary timing but does not "
                "model FastSim's four bank groups per rank",
                "Zsim does not time FST virtual-page-token DTLB misses",
                "FST mode uses single-thread inline weave because Pin workers "
                "start only after guest execution",
            ],
        },
    }
    (args.output / "summary.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

    markdown = [
        "# FastSim / Sniper / Zsim FS trace-driven comparison",
        "",
        "| Workload | Simulator | UOP CPI | gem5 UOP CPI | Error | "
        "M UOP/s (sim / e2e) | UOPs |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        error = (
            "%.3f%%" % (100 * row["cpi_signed_error"])
            if row["cpi_signed_error"] is not None
            else "n/a"
        )
        gem5 = (
            "%.6f" % row["gem5_uop_cpi"]
            if row["gem5_uop_cpi"] is not None
            else "n/a"
        )
        sim_rate = (
            row["sim_uops_per_second"] / 1e6
            if row["sim_uops_per_second"] is not None
            else float("nan")
        )
        markdown.append(
            "| %(workload)s | %(simulator)s | %(cpi).6f | %(gem5)s | "
            "%(error)s | %(sim).3f / %(e2e).3f | %(uops)d |"
            % {
                "workload": row["workload"],
                "simulator": row["simulator"],
                "cpi": row["uop_cpi"],
                "gem5": gem5,
                "error": error,
                "sim": sim_rate,
                "e2e": row["e2e_uops_per_second"] / 1e6,
                "uops": row["uops"],
            }
        )
    markdown.extend(
        [
            "",
            "## Alignment limits",
            "",
            "- Sniper matches target widths, windows, FUs, predictor, caches, "
            "and primary DRAM topology/timing, but its ROB abstraction does "
            "not expose every FastSim pipeline stage and its MSI/NoC/DDR "
            "scheduler differ from Ruby/open_adaptive.",
            "- Zsim matches target widths, windows, FUs, predictor, caches, "
            "and primary DRAM topology/timing, but its coherence path, "
            "writeback calendar, and DRAM bank-group model remain different.",
            "- Sniper and Zsim do not time FST virtual-page-token DTLB misses.",
            "- All simulators consume only functional FST inputs; gem5 labels "
            "are read after simulation for accuracy scoring.",
        ]
    )
    (args.output / "summary.md").write_text("\n".join(markdown) + "\n")


def run_case(args, workload, baseline):
    source_manifest = args.case_root / workload / "roi-slice.manifest.txt"
    if not source_manifest.is_file():
        raise RuntimeError("missing case manifest: %s" % source_manifest)
    entries = read_manifest(
        source_manifest,
        args.cores,
        args.limit_instructions_per_core,
    )
    case_dir = args.output / "cases" / workload
    case_dir.mkdir(parents=True, exist_ok=True)
    manifest = case_dir / "manifest.txt"
    write_manifest(manifest, entries)
    print("[compare] %s" % workload, flush=True)

    if "fastsim" in args.simulators:
        run_fastsim(args, case_dir, manifest)
    if "sniper" in args.simulators:
        run_sniper(args, case_dir, entries)
    if "zsim" in args.simulators:
        run_zsim(args, case_dir, manifest)

    results = {}
    if "fastsim" in args.simulators:
        results["fastsim"] = parse_fastsim(case_dir, args.cores)
    if "sniper" in args.simulators:
        results["sniper"] = parse_sniper(case_dir, args.sniper_root, args.cores)
    if "zsim" in args.simulators:
        results["zsim"] = parse_zsim(case_dir, args.cores)
    uop_counts = {name: result["uops"] for name, result in results.items()}
    if len(set(uop_counts.values())) != 1:
        raise RuntimeError(
            "%s UOP conservation mismatch: %s" % (workload, uop_counts)
        )
    uops_by_core = {
        name: result["uops_by_core"] for name, result in results.items()
    }
    if len({tuple(counts) for counts in uops_by_core.values()}) != 1:
        raise RuntimeError(
            "%s per-core UOP conservation mismatch: %s"
            % (workload, uops_by_core)
        )
    branch_misses_by_core = {
        name: result["branch_misses_by_core"]
        for name, result in results.items()
    }
    if len(
        {tuple(counts) for counts in branch_misses_by_core.values()}
    ) != 1:
        raise RuntimeError(
            "%s per-core branch-predictor mismatch: %s"
            % (workload, branch_misses_by_core)
        )
    case_report = {
        "source_manifest": str(source_manifest.resolve()),
        "run_manifest": str(manifest.resolve()),
        "uop_counts": uop_counts,
        "uops_by_core": uops_by_core,
        "branch_misses_by_core": branch_misses_by_core,
    }
    full_roi = args.limit_instructions_per_core == 0
    rows = [
        result_row(workload, simulator, result, baseline, full_roi)
        for simulator, result in results.items()
    ]
    print("[compare] %s done" % workload, flush=True)
    return workload, rows, case_report


def main():
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    baselines = read_baselines(args.gem5_summary)
    rows = []
    case_reports = {}

    if args.jobs == 1:
        completed = [
            run_case(args, workload, baselines[workload])
            for workload in args.workloads
        ]
    else:
        completed = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.jobs
        ) as executor:
            futures = {
                executor.submit(
                    run_case, args, workload, baselines[workload]
                ): workload
                for workload in args.workloads
            }
            for future in concurrent.futures.as_completed(futures):
                completed.append(future.result())

    order = {workload: index for index, workload in enumerate(args.workloads)}
    completed.sort(key=lambda item: order[item[0]])
    for workload, case_rows, case_report in completed:
        rows.extend(case_rows)
        case_reports[workload] = case_report
    write_outputs(args, rows, case_reports)

    resolved_case_configs = {}
    for workload in args.workloads:
        case_config = {}
        sniper_config = (
            args.output
            / "cases"
            / workload
            / "sniper"
            / "simulation"
            / "sim.cfg"
        )
        zsim_config = (
            args.output / "cases" / workload / "zsim" / "config.cfg"
        )
        if sniper_config.is_file():
            case_config["sniper_sim_cfg_sha256"] = sha256(sniper_config)
        if zsim_config.is_file():
            case_config["zsim_cfg_sha256"] = sha256(zsim_config)
        resolved_case_configs[workload] = case_config

    run_manifest = {
        "fastsim": {
            "path": str(args.fastsim.resolve()),
            "sha256": sha256(args.fastsim.resolve()),
        },
        "fastsim_config": {
            "path": str(args.fastsim_config.resolve()),
            "sha256": sha256(args.fastsim_config.resolve()),
        },
        "sniper": {
            "root": str(args.sniper_root.resolve()),
            "binary": str(args.sniper_binary.resolve()),
            "binary_sha256": sha256(args.sniper_binary.resolve()),
            "config": str(
                (args.sniper_root / ("config/" + args.sniper_config + ".cfg"))
                .resolve()
            ),
            "config_sha256": sha256(
                (
                    args.sniper_root
                    / ("config/" + args.sniper_config + ".cfg")
                ).resolve()
            ),
        },
        "zsim": {
            "root": str(args.zsim_root.resolve()),
            "binary_sha256": sha256(
                (args.zsim_root / "build/opt/libzsim.so").resolve()
            ),
            "config_template": str(args.zsim_template.resolve()),
            "config_template_sha256": sha256(args.zsim_template.resolve()),
        },
        "comparison_tool": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256(Path(__file__).resolve()),
        },
        "resolved_case_configs": resolved_case_configs,
    }
    (args.output / "run-manifest.json").write_text(
        json.dumps(run_manifest, indent=2) + "\n"
    )
    print("[compare] wrote %s" % (args.output / "summary.md"))


if __name__ == "__main__":
    main()
