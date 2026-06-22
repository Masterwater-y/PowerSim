#!/usr/bin/env python3
import argparse
import csv
import json
import math
import platform
import shutil
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path


THIS = Path(__file__).resolve()
GLOBAL_ROOT = THIS.parents[1]
SIM_ROOT = GLOBAL_ROOT.parent
CP_ROOT = SIM_ROOT / "counterpoint_lite"

DEFAULT_WORKLOAD_DIR = SIM_ROOT / "workloads" / "analytics_st"
DEFAULT_WORKLOAD = DEFAULT_WORKLOAD_DIR / "analytics_st"
DEFAULT_COLLECT = SIM_ROOT / "dynamorio" / "collect_drmemtrace.sh"
DEFAULT_CONFIG = SIM_ROOT / "minesim" / "config" / "sapphire_rapids.cfg"
DEFAULT_MAPPING = CP_ROOT / "configs" / "simulator_mappings.json"
DEFAULT_EVENTS = [
    "cycles",
    "instructions",
    "branch-misses",
    "LLC-load-misses",
    "dTLB-load-misses",
]


def add_bool_flag(parser, name, default, help_text):
    group = parser.add_mutually_exclusive_group()
    flag = f"--{name}"
    no_flag = f"--no-{name}"
    dest = name.replace("-", "_")
    group.add_argument(flag, dest=dest, action="store_true", help=help_text)
    group.add_argument(no_flag, dest=dest, action="store_false", help=f"disable: {help_text}")
    parser.set_defaults(**{dest: default})


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect analytics_st drmemtrace, perf stat PMU means, MineSim simulation, and CounterPoint diagnosis."
    )
    parser.add_argument("--iter", type=int, default=8, help="analytics_st iteration count")
    parser.add_argument("--repeats", type=int, default=5, help="perf stat repeat count")
    parser.add_argument("--cpu", default="0", help="CPU used by taskset for the single-thread workload")
    parser.add_argument("--perf-iter", type=int, default=0, help="analytics_st iteration count used only for perf; 0 means reuse --iter")
    parser.add_argument("--perf-warmup", type=int, default=2, help="number of warmup executions before measured perf runs")
    add_bool_flag(parser, "perf-disable-aslr", True, "run perf workload under setarch -R to reduce address-layout noise")
    add_bool_flag(parser, "perf-local-memory", True, "run perf workload under numactl --localalloc to reduce NUMA placement noise")
    parser.add_argument("--events", default=",".join(DEFAULT_EVENTS), help="comma-separated perf events")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="MineSim microarchitecture config")
    parser.add_argument("--out", default="", help="output directory; default creates a timestamped directory")
    parser.add_argument("--name", default="", help="run name; default analytics_st_<timestamp>")
    parser.add_argument("--skip-trace", action="store_true", help="backward-compatible alias: skip drmemtrace collection and use --trace")
    parser.add_argument("--reuse-trace", action="store_true", help="skip drmemtrace collection; use --trace if set, otherwise use the newest existing analytics_st trace")
    parser.add_argument("--trace-search-root", default=str(GLOBAL_ROOT / "out"), help="root directory searched by --reuse-trace when --trace is not set")
    parser.add_argument("--perf-only", action="store_true", help="only run perf collection and skip trace/minesim/counterpoint")
    parser.add_argument("--trace", default="", help="existing .trace.gz used when --skip-trace or --reuse-trace is set")
    args = parser.parse_args(argv)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = args.name or f"analytics_st_{timestamp}"
    out = Path(args.out) if args.out else GLOBAL_ROOT / "out" / f"analytics_st_validation_{timestamp}"
    out.mkdir(parents=True, exist_ok=True)

    events = [e.strip() for e in args.events.split(",") if e.strip()]
    perf_iter = args.perf_iter if args.perf_iter > 0 else args.iter
    trace_cmd = [str(DEFAULT_WORKLOAD), str(args.iter)]
    perf_cmd = make_perf_workload_cmd(args.cpu, perf_iter, args.perf_disable_aslr, args.perf_local_memory)

    build_workload()
    perf_stats = collect_perf(out, perf_cmd, events, args.repeats, args.perf_warmup)

    trace = None
    minesim_dir = None
    comparison = []
    if not args.perf_only:
        reuse_trace = args.reuse_trace or args.skip_trace
        trace = resolve_reused_trace(args.trace, args.trace_search_root) if reuse_trace else collect_trace(out, run_name, trace_cmd)
        minesim_dir = run_minesim_counterpoint(out, trace, args.config)
        comparison = compare_perf_and_minesim(out, perf_stats, minesim_dir)

    summary = {
        "ok": True,
        "run_name": run_name,
        "trace_workload": trace_cmd,
        "perf_workload": perf_cmd,
        "perf_iter": perf_iter,
        "perf_warmup": args.perf_warmup,
        "trace": None if trace is None else str(trace),
        "config": str(args.config),
        "out": str(out),
        "perf_stats": str(out / "perf_stats.json"),
        "minesim_counterpoint": None if minesim_dir is None else str(minesim_dir),
        "comparison_csv": None if args.perf_only else str(out / "pmu_comparison.csv"),
        "comparison_json": None if args.perf_only else str(out / "pmu_comparison.json"),
        "counterpoint_summary": None if minesim_dir is None else load_json(minesim_dir / "summary.json"),
        "comparison": comparison,
    }
    write_json(summary, out / "final_summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_workload():
    run(["make"], cwd=DEFAULT_WORKLOAD_DIR)


def collect_trace(out, run_name, workload_cmd):
    trace_root = out / "drmemtrace"
    run([
        str(DEFAULT_COLLECT),
        "-o", str(trace_root),
        "-n", run_name,
        "--subdir-prefix", "analytics_st",
        "--",
        *workload_cmd,
    ], cwd=SIM_ROOT)
    traces = sorted((trace_root / run_name / "trace").glob("*.trace.gz"))
    traces = [p for p in traces if p.stat().st_size > 0 and DEFAULT_WORKLOAD.name in p.name]
    if not traces:
        raise FileNotFoundError(f"no .trace.gz generated under {trace_root / run_name / 'trace'}")
    if len(traces) > 1:
        print(f"warning: multiple traces found; using {traces[0]}", file=sys.stderr)
    return traces[0]


def resolve_reused_trace(trace_arg, search_root):
    if trace_arg:
        trace = Path(trace_arg).expanduser()
        if not trace.is_file():
            raise FileNotFoundError(f"--trace does not exist: {trace}")
        if trace.stat().st_size <= 0:
            raise ValueError(f"--trace is empty: {trace}")
        return trace.resolve()

    root = Path(search_root).expanduser()
    candidates = [
        p for p in root.glob("analytics_st_validation_*/drmemtrace/*/trace/*.trace.gz")
        if p.is_file() and p.stat().st_size > 0 and DEFAULT_WORKLOAD.name in p.name
    ]
    if not candidates:
        raise FileNotFoundError(
            f"--reuse-trace could not find an existing analytics_st .trace.gz under {root}; "
            "pass --trace /path/to/file.trace.gz or run once without --reuse-trace"
        )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    trace = candidates[0].resolve()
    print(f"reusing trace: {trace}", file=sys.stderr)
    return trace


def make_perf_workload_cmd(cpu, perf_iter, disable_aslr, local_memory):
    cmd = ["taskset", "-c", cpu]
    if disable_aslr and shutil.which("setarch"):
        cmd.extend(["setarch", platform.machine(), "-R"])
    if local_memory and shutil.which("numactl"):
        cmd.extend(["numactl", "--physcpubind", cpu, "--localalloc"])
    cmd.extend([str(DEFAULT_WORKLOAD), str(perf_iter)])
    return cmd


def collect_perf(out, workload_cmd, events, repeats, warmup):
    perf_dir = out / "perf"
    perf_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    event_arg = ",".join(events)
    for idx in range(warmup):
        warmup_log = perf_dir / f"warmup_{idx}.stdout"
        run(workload_cmd, cwd=SIM_ROOT, stdout_path=warmup_log)
    for idx in range(repeats):
        output = perf_dir / f"perf_{idx}.csv"
        run([
            "perf", "stat",
            "-x", ",",
            "-e", event_arg,
            "-o", str(output),
            "--",
            *workload_cmd,
        ], cwd=SIM_ROOT)
        samples.append(parse_perf_csv(output))

    aliases = load_json(DEFAULT_MAPPING).get("perf-stat", {})
    by_counter = {}
    for sample in samples:
        for event, value in sample.items():
            counter = aliases.get(event, event)
            by_counter.setdefault(counter, []).append(value)

    stats = {
        "events": events,
        "repeats": repeats,
        "warmup_runs": warmup,
        "measured_workload": workload_cmd,
        "samples": samples,
        "counters": {},
    }
    for counter, values in sorted(by_counter.items()):
        mean = statistics.fmean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        cv = stdev / mean if mean else 0.0
        stats["counters"][counter] = {
            "mean": mean,
            "stdev": stdev,
            "volatility_cv": cv,
            "volatility_pct": cv * 100.0,
            "samples": values,
        }
    write_json(stats, out / "perf_stats.json")
    return stats


def parse_perf_csv(path):
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            raw_value = row[0].strip().replace(",", "")
            event = row[2].strip()
            if not raw_value or raw_value.startswith("<") or not event:
                continue
            try:
                values[event] = float(raw_value)
            except ValueError:
                continue
    return values


def run_minesim_counterpoint(out, trace, config):
    run(["make", "minesim"], cwd=SIM_ROOT / "minesim")
    minesim_out = out / "minesim_counterpoint"
    run([
        sys.executable,
        str(CP_ROOT / "scripts" / "run_minesim_config_check.py"),
        "--config", str(config),
        "--trace", str(trace),
        "--out", str(minesim_out),
    ], cwd=SIM_ROOT)
    return minesim_out


def compare_perf_and_minesim(out, perf_stats, minesim_dir):
    minesim_obs = load_json(minesim_dir / "observation.minesim.json")
    minesim_values = {c["name"]: float(c["value"]) for c in minesim_obs.get("counters", [])}
    rows = []
    for counter, perf in sorted(perf_stats["counters"].items()):
        sim = minesim_values.get(counter)
        perf_mean = float(perf["mean"])
        if sim is None:
            abs_err = None
            rel_err = None
        else:
            abs_err = sim - perf_mean
            rel_err = abs_err / perf_mean if perf_mean else math.inf
        rows.append({
            "counter": counter,
            "perf_mean": perf_mean,
            "perf_stdev": perf["stdev"],
            "perf_volatility_pct": perf["volatility_pct"],
            "minesim": sim,
            "abs_error": abs_err,
            "relative_error": rel_err,
            "relative_error_pct": None if rel_err is None else rel_err * 100.0,
        })

    with open(out / "pmu_comparison.csv", "w", encoding="utf-8", newline="") as f:
        fields = ["counter", "perf_mean", "perf_stdev", "perf_volatility_pct", "minesim",
                  "abs_error", "relative_error", "relative_error_pct"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    write_json({"rows": rows}, out / "pmu_comparison.json")
    return rows


def run(cmd, cwd, stdout_path=None):
    print("+", " ".join(str(x) for x in cmd), file=sys.stderr)
    if stdout_path is None:
        subprocess.run([str(x) for x in cmd], cwd=str(cwd), check=True)
        return
    stdout_path = Path(stdout_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8") as f:
        subprocess.run([str(x) for x in cmd], cwd=str(cwd), check=True, stdout=f, stderr=subprocess.STDOUT)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(obj, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
