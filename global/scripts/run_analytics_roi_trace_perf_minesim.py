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

WORKLOAD_DIR = SIM_ROOT / "workloads" / "analytics_roi"
WORKLOAD = WORKLOAD_DIR / "analytics_roi"
COLLECT = SIM_ROOT / "dynamorio" / "collect_drmemtrace.sh"
DEFAULT_CONFIG = SIM_ROOT / "minesim" / "config" / "sapphire_rapids.cfg"
ROI_BEGIN = "0f1f840042424242"
ROI_END = "0f1f840043434343"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run analytics_roi with native ROI perf, ROI drmemtrace slicing, MineSim, and CounterPoint."
    )
    parser.add_argument("--warmup", type=int, default=2, help="warmup iterations before ROI")
    parser.add_argument("--roi-iters", type=int, default=8, help="iterations inside ROI")
    parser.add_argument("--repeats", type=int, default=3, help="native perf ROI repeat count")
    parser.add_argument("--cpu", default="0", help="CPU used by taskset")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="MineSim config")
    parser.add_argument("--out", default="", help="output directory; default creates timestamped directory")
    parser.add_argument("--name", default="", help="run name; default analytics_roi_<timestamp>")
    args = parser.parse_args(argv)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = args.name or f"analytics_roi_{timestamp}"
    out = Path(args.out) if args.out else GLOBAL_ROOT / "out" / f"analytics_roi_validation_{timestamp}"
    out.mkdir(parents=True, exist_ok=True)

    build_workload()
    perf_stats = collect_native_roi_perf(out, args.cpu, args.warmup, args.roi_iters, args.repeats)
    full_trace = collect_full_trace(out, run_name, args.warmup, args.roi_iters)
    minesim_dir = run_minesim_counterpoint(out, full_trace, args.config)
    comparison = compare_perf_and_minesim(out, perf_stats, minesim_dir)

    summary = {
        "ok": True,
        "run_name": run_name,
        "workload": str(WORKLOAD),
        "warmup": args.warmup,
        "roi_iters": args.roi_iters,
        "repeats": args.repeats,
        "full_trace": str(full_trace),
        "roi_begin_encoding": ROI_BEGIN,
        "roi_end_encoding": ROI_END,
        "perf_stats": str(out / "perf_stats.json"),
        "minesim_counterpoint": str(minesim_dir),
        "comparison_csv": str(out / "pmu_comparison.csv"),
        "comparison_json": str(out / "pmu_comparison.json"),
        "counterpoint_summary": load_json(minesim_dir / "summary.json"),
        "comparison": comparison,
    }
    write_json(summary, out / "final_summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_workload():
    run(["make"], cwd=WORKLOAD_DIR)


def workload_args(warmup, roi_iters, perf_json=None, no_perf=False):
    cmd = [
        str(WORKLOAD),
        "--warmup", str(warmup),
        "--roi-iters", str(roi_iters),
    ]
    if no_perf:
        cmd.append("--no-perf")
    else:
        cmd.extend(["--perf-json", str(perf_json)])
    return cmd


def make_native_cmd(cpu, base_cmd):
    cmd = ["taskset", "-c", cpu]
    if shutil.which("setarch"):
        cmd.extend(["setarch", platform.machine(), "-R"])
    if shutil.which("numactl"):
        cmd.extend(["numactl", "--physcpubind", cpu, "--localalloc"])
    cmd.extend(base_cmd)
    return cmd


def collect_native_roi_perf(out, cpu, warmup, roi_iters, repeats):
    perf_dir = out / "perf"
    perf_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for idx in range(repeats):
        perf_json = perf_dir / f"perf_roi_{idx}.json"
        stdout = perf_dir / f"perf_roi_{idx}.stdout"
        cmd = make_native_cmd(cpu, workload_args(warmup, roi_iters, perf_json=perf_json))
        run(cmd, cwd=SIM_ROOT, stdout_path=stdout)
        roi = load_json(perf_json)["roi"]
        samples.append({k: float(v) for k, v in roi.items()})

    counters = {}
    for sample in samples:
        for counter, value in sample.items():
            counters.setdefault(counter, []).append(value)

    stats = {
        "source": "analytics_roi internal perf_event_open",
        "repeats": repeats,
        "warmup_iters": warmup,
        "roi_iters": roi_iters,
        "samples": samples,
        "counters": {},
    }
    for counter, values in sorted(counters.items()):
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


def collect_full_trace(out, run_name, warmup, roi_iters):
    trace_root = out / "drmemtrace"
    run([
        str(COLLECT),
        "-o", str(trace_root),
        "-n", run_name,
        "--subdir-prefix", "analytics_roi",
        "--",
        *workload_args(warmup, roi_iters, no_perf=True),
    ], cwd=SIM_ROOT)

    traces = sorted((trace_root / run_name / "trace").glob("*.trace.gz"))
    traces = [p for p in traces if p.stat().st_size > 0 and WORKLOAD.name in p.name]
    if not traces:
        raise FileNotFoundError(f"no .trace.gz generated under {trace_root / run_name / 'trace'}")
    if len(traces) > 1:
        print(f"warning: multiple traces found; using {traces[0]}", file=sys.stderr)
    return traces[0]


def run_minesim_counterpoint(out, trace, config):
    run(["make", "minesim"], cwd=SIM_ROOT / "minesim")
    minesim_out = out / "minesim_counterpoint"
    run([
        sys.executable,
        str(CP_ROOT / "scripts" / "run_minesim_config_check.py"),
        "--config", str(config),
        "--trace", str(trace),
        "--roi-begin-encoding", ROI_BEGIN,
        "--roi-end-encoding", ROI_END,
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
        fields = ["counter", "perf_mean", "perf_stdev", "perf_volatility_pct",
                  "minesim", "abs_error", "relative_error", "relative_error_pct"]
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
