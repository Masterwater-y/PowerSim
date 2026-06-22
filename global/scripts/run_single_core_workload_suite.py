#!/usr/bin/env python3
import argparse
import csv
import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path


THIS = Path(__file__).resolve()
GLOBAL_ROOT = THIS.parents[1]
SIM_ROOT = GLOBAL_ROOT.parent
CP_ROOT = SIM_ROOT / "counterpoint_lite"
SNIPER_ROOT = SIM_ROOT / "snipersim"
DEFAULT_CONFIG = SIM_ROOT / "minesim" / "config" / "sapphire_rapids.cfg"
DEFAULT_MAPPING = CP_ROOT / "configs" / "simulator_mappings.json"
DEFAULT_COLLECT = SIM_ROOT / "dynamorio" / "collect_drmemtrace.sh"
DEFAULT_EVENTS = ["cycles", "instructions", "branch-misses", "LLC-load-misses", "dTLB-load-misses"]
SNIPER_FREQ_GHZ = 2.6


def fmean(values):
    if hasattr(statistics, "fmean"):
        return statistics.fmean(values)
    return sum(values) / len(values)

WORKLOADS = {
    # Keep default suite inputs close to the 10M-instruction policy when the
    # workload CLI allows it. Some legacy workloads already exceed 10M at
    # iter=1, so their minimum legal scale remains iter=1.
    "log_state": {"dir": SIM_ROOT / "workloads" / "log_state", "bin": "log_state", "iter": 1},
    "graph_walk": {"dir": SIM_ROOT / "workloads" / "graph_walk", "bin": "graph_walk", "iter": 1},
    "codec_pipeline": {"dir": SIM_ROOT / "workloads" / "codec_pipeline", "bin": "codec_pipeline", "iter": 1},
    "branch_dense": {"dir": SIM_ROOT / "workloads" / "branch_dense", "bin": "branch_dense", "iter": 5},
    "dep_chain": {"dir": SIM_ROOT / "workloads" / "dep_chain", "bin": "dep_chain", "iter": 1},
    "mlp_stream": {"dir": SIM_ROOT / "workloads" / "mlp_stream", "bin": "mlp_stream", "iter": 1},
    "cache_bench": {"dir": SIM_ROOT / "workloads" / "cache_bench", "bin": "cache_bench", "iter": 1},
}

DEFAULT_WORKLOADS = [
    "log_state",
    "graph_walk",
    "codec_pipeline",
    "branch_dense",
    "cache_bench",
]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run single-core workloads through perf, MineSim, and SniperSim.")
    parser.add_argument("--workloads", default=",".join(DEFAULT_WORKLOADS), help="comma-separated workload names")
    parser.add_argument("--iters", default="", help="optional comma-separated name=iter overrides")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--cpu", default="0")
    parser.add_argument("--events", default=",".join(DEFAULT_EVENTS))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default="")
    parser.add_argument("--skip-sniper", action="store_true")
    parser.add_argument("--skip-trace", action="store_true", help="skip trace/MineSim and only run perf/Sniper")
    args = parser.parse_args(argv)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else GLOBAL_ROOT / "out" / f"single_core_suite_{timestamp}"
    out.mkdir(parents=True, exist_ok=True)
    events = [e.strip() for e in args.events.split(",") if e.strip()]
    iter_overrides = parse_iter_overrides(args.iters)
    selected = [w.strip() for w in args.workloads.split(",") if w.strip()]

    all_rows = []
    workload_summaries = {}
    for name in selected:
        if name not in WORKLOADS:
            raise ValueError(f"unknown workload {name}; valid={sorted(WORKLOADS)}")
        spec = WORKLOADS[name]
        iters = iter_overrides.get(name, spec["iter"])
        wout = out / name
        wout.mkdir(parents=True, exist_ok=True)
        binary = build_workload(spec)
        cmd = [str(binary), str(iters)]
        perf_cmd = make_perf_cmd(args.cpu, cmd)

        perf_stats = collect_perf(wout, perf_cmd, events, args.repeats, args.warmup)
        trace = None
        minesim_dir = None
        if not args.skip_trace:
            trace = collect_trace(wout, name, cmd)
            minesim_dir = run_minesim_counterpoint(wout, trace, args.config)

        sniper_dir = None
        sniper_values = {}
        if not args.skip_sniper:
            sniper_dir = run_sniper(wout, cmd)
            sniper_values = parse_sniper_stats(sniper_dir / "sim.stats.sqlite3")

        minesim_values = {}
        if minesim_dir:
            obs = load_json(minesim_dir / "observation.minesim.json")
            minesim_values = {c["name"]: float(c["value"]) for c in obs.get("counters", [])}

        rows = compare(name, perf_stats, minesim_values, sniper_values)
        write_rows(wout / "tripartite_comparison.csv", rows)
        write_json({"rows": rows}, wout / "tripartite_comparison.json")
        all_rows.extend(rows)
        workload_summaries[name] = {
            "iters": iters,
            "binary": str(binary),
            "perf_stats": str(wout / "perf_stats.json"),
            "trace": None if trace is None else str(trace),
            "minesim_counterpoint": None if minesim_dir is None else str(minesim_dir),
            "sniper_output": None if sniper_dir is None else str(sniper_dir),
            "comparison_csv": str(wout / "tripartite_comparison.csv"),
        }

    write_rows(out / "suite_tripartite_comparison.csv", all_rows)
    summary = {"ok": True, "out": str(out), "workloads": workload_summaries, "rows": all_rows}
    write_json(summary, out / "suite_summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parse_iter_overrides(text):
    out = {}
    if not text:
        return out
    for item in text.split(","):
        if not item.strip():
            continue
        name, value = item.split("=", 1)
        out[name.strip()] = int(value)
    return out


def build_workload(spec):
    run(["make"], cwd=spec["dir"])
    binary = spec["dir"] / spec["bin"]
    if not binary.exists():
        raise FileNotFoundError(binary)
    return binary


def make_perf_cmd(cpu, workload_cmd):
    # Run the workload directly so perf counts line up with the traced binary
    # itself, not wrapper processes such as taskset/setarch/numactl.
    _ = cpu
    return list(workload_cmd)


def collect_perf(out, workload_cmd, events, repeats, warmup):
    perf_dir = out / "perf"
    perf_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for idx in range(warmup):
        run(workload_cmd, cwd=SIM_ROOT, stdout_path=perf_dir / f"warmup_{idx}.stdout")
    for idx in range(repeats):
        output = perf_dir / f"perf_{idx}.csv"
        run(["perf", "stat", "-x", ",", "-e", ",".join(events), "-o", str(output), "--", *workload_cmd], cwd=SIM_ROOT)
        samples.append(parse_perf_csv(output))

    aliases = load_json(DEFAULT_MAPPING).get("perf-stat", {})
    by_counter = {}
    for sample in samples:
        for event, value in sample.items():
            by_counter.setdefault(aliases.get(event, event), []).append(value)
    stats = {"events": events, "repeats": repeats, "warmup_runs": warmup, "measured_workload": workload_cmd, "samples": samples, "counters": {}}
    for counter, values in sorted(by_counter.items()):
        mean = fmean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        stats["counters"][counter] = {
            "mean": mean,
            "stdev": stdev,
            "volatility_pct": (stdev / mean * 100.0) if mean else 0.0,
            "samples": values,
        }
    write_json(stats, out / "perf_stats.json")
    return stats


def parse_perf_csv(path):
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            raw = row[0].strip().replace(",", "")
            event = row[2].strip()
            if not raw or raw.startswith("<") or not event:
                continue
            try:
                values[event] = float(raw)
            except ValueError:
                pass
    return values


def collect_trace(out, name, workload_cmd):
    trace_root = out / "drmemtrace"
    run([str(DEFAULT_COLLECT), "-o", str(trace_root), "-n", name, "--subdir-prefix", name, "--", *workload_cmd], cwd=SIM_ROOT)
    trace_dir = trace_root / name / "trace"
    traces = sorted(list(trace_dir.glob("*.trace.gz")) + list(trace_dir.glob("*.trace.zip")))
    binary_name = Path(workload_cmd[0]).name
    traces = [p for p in traces if p.stat().st_size > 0 and binary_name in p.name]
    if not traces:
        raise FileNotFoundError(f"no trace for {binary_name} under {trace_dir}")
    return traces[0]


def run_minesim_counterpoint(out, trace, config):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = "/opt/gcc-11/lib64:" + env.get("LD_LIBRARY_PATH", "")
    minesim_out = out / "minesim_counterpoint"
    run([sys.executable, str(CP_ROOT / "scripts" / "run_minesim_config_check.py"), "--config", str(config), "--trace", str(trace), "--out", str(minesim_out)], cwd=SIM_ROOT, env=env)
    return minesim_out


def run_sniper(out, workload_cmd):
    sniper_out = out / "sniper"
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = ":".join([
        "/opt/gcc-11/lib64",
        str(SNIPER_ROOT / "xed_kit" / "lib"),
        str(SNIPER_ROOT / "lib"),
        str(SNIPER_ROOT / "libtorch" / "lib"),
        env.get("LD_LIBRARY_PATH", ""),
    ])
    run(["./run-sniper", "-n", "1", "-d", str(sniper_out), "-c", "xeon-platinum-8457c-spr", "--", *workload_cmd], cwd=SNIPER_ROOT, env=env)
    return sniper_out


def parse_sniper_stats(sqlite_path):
    if not sqlite_path.exists():
        return {}
    raw = {}
    con = sqlite3.connect(sqlite_path)
    try:
        cur = con.cursor()
        row = cur.execute("select prefixname from prefixes order by prefixid desc limit 1").fetchone()
        prefix = row[0] if row else "stop"
        query = """
            select names.objectname, names.metricname, `values`.value
            from `values`
            join names on names.nameid = `values`.nameid
            join prefixes on prefixes.prefixid = `values`.prefixid
            where prefixes.prefixname = ? and `values`.core = 0
        """
        for obj, metric, value in cur.execute(query, (prefix,)):
            raw[f"{obj}.{metric}"] = float(value)
    finally:
        con.close()
    return {
        "core.instructions": raw.get("performance_model.instruction_count"),
        "core.cycles": None if raw.get("performance_model.elapsed_time") is None else raw["performance_model.elapsed_time"] * SNIPER_FREQ_GHZ * 1e-6,
        "branch.misses": raw.get("branch_predictor.num-incorrect"),
        "cache.llc.load_misses": raw.get("L3.load-misses"),
        "tlb.dtlb_load_misses": raw.get("dtlb.miss"),
    }


def compare(workload, perf_stats, minesim_values, sniper_values):
    rows = []
    for counter, perf in sorted(perf_stats["counters"].items()):
        perf_mean = float(perf["mean"])
        row = {
            "workload": workload,
            "counter": counter,
            "perf_mean": perf_mean,
            "perf_stdev": float(perf["stdev"]),
            "perf_volatility_pct": float(perf["volatility_pct"]),
            "minesim": minesim_values.get(counter),
            "sniper": sniper_values.get(counter),
        }
        for sim_name in ["minesim", "sniper"]:
            value = row[sim_name]
            if value is None:
                row[f"{sim_name}_abs_error"] = None
                row[f"{sim_name}_relative_error_pct"] = None
            else:
                abs_err = value - perf_mean
                row[f"{sim_name}_abs_error"] = abs_err
                row[f"{sim_name}_relative_error_pct"] = (abs_err / perf_mean * 100.0) if perf_mean else math.inf
        rows.append(row)
    return rows


def write_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["workload", "counter", "perf_mean", "perf_stdev", "perf_volatility_pct",
              "minesim", "minesim_abs_error", "minesim_relative_error_pct",
              "sniper", "sniper_abs_error", "sniper_relative_error_pct"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(cmd, cwd, stdout_path=None, env=None):
    print("+", " ".join(str(x) for x in cmd), file=sys.stderr)
    if stdout_path is None:
        subprocess.run([str(x) for x in cmd], cwd=str(cwd), env=env, check=True)
        return
    stdout_path = Path(stdout_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8") as f:
        subprocess.run([str(x) for x in cmd], cwd=str(cwd), env=env, check=True, stdout=f, stderr=subprocess.STDOUT)


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
