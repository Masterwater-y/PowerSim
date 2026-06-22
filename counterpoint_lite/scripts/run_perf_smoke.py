#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


THIS = Path(__file__).resolve()
ROOT = THIS.parents[1]
SIM_ROOT = ROOT.parent
DEFAULT_MODEL = ROOT / "examples" / "model_spr_core_minimal.json"
DEFAULT_MAPPING = ROOT / "configs" / "simulator_mappings.json"
DEFAULT_WORKLOAD_DIR = SIM_ROOT / "workloads" / "cache_bench"
DEFAULT_WORKLOAD = DEFAULT_WORKLOAD_DIR / "cache_bench"
EVENTS = ["instructions", "cycles", "branch-misses", "LLC-load-misses", "dTLB-load-misses"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a perf-based CounterPoint Lite smoke test")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    parser.add_argument("--workload", default=str(DEFAULT_WORKLOAD))
    parser.add_argument("--iters", type=int, default=1)
    parser.add_argument("--out", default=str(ROOT / "out" / "perf_smoke"))
    parser.add_argument("--cpu", default="0", help="CPU for taskset; use empty string to disable pinning")
    parser.add_argument("--negative-factor", type=float, default=100.0)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ensure_workload(Path(args.workload))
    perf_csv = out / "perf_stat.csv"
    workload_stdout = out / "workload.stdout"
    run_perf(Path(args.workload), args.iters, args.cpu, perf_csv, workload_stdout)

    obs = out / "observation.perf.json"
    run_cli([
        "import-observation", "--kind", "perf-stat",
        "--input", str(perf_csv),
        "--mapping", args.mapping,
        "--output", str(obs),
    ])

    positive = out / "positive"
    positive_rc = run_cli([
        "run", "--model", args.model,
        "--observation", str(obs),
        "--output-dir", str(positive),
    ], allow_infeasible=True)

    neg_obs = out / "observation.perf.negative.json"
    make_negative_observation(obs, neg_obs, "cache.llc.load_misses", args.negative_factor)
    negative = out / "negative_llc"
    negative_rc = run_cli([
        "run", "--model", args.model,
        "--observation", str(neg_obs),
        "--output-dir", str(negative),
    ], allow_infeasible=True)

    positive_report = load_json(positive / "report.json")
    negative_report = load_json(negative / "report.json")
    negative_diag = load_json(negative / "diagnosis.json")
    top_component = None
    if negative_diag.get("ranked_components"):
        top_component = negative_diag["ranked_components"][0].get("component")

    ok = (
        positive_rc == 0
        and positive_report.get("verdict") == "feasible"
        and negative_rc == 1
        and negative_report.get("verdict") == "infeasible"
        and top_component == "llc_cha"
    )
    summary = {
        "ok": ok,
        "out": str(out),
        "perf_csv": str(perf_csv),
        "positive_verdict": positive_report.get("verdict"),
        "negative_verdict": negative_report.get("verdict"),
        "negative_top_component": top_component,
        "events": EVENTS,
        "workload": [str(args.workload), str(args.iters)],
    }
    write_json(summary, out / "summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def ensure_workload(workload):
    if workload.exists() and os.access(workload, os.X_OK):
        return
    if workload.parent.name == "cache_bench" and (workload.parent / "Makefile").exists():
        subprocess.run(["make", "-C", str(workload.parent)], check=True)
    if not workload.exists():
        raise FileNotFoundError(f"workload not found: {workload}")


def run_perf(workload, iters, cpu, perf_csv, workload_stdout):
    if shutil.which("perf") is None:
        raise RuntimeError("perf not found")
    cmd = ["perf", "stat", "-x,", "-e", ",".join(EVENTS), "--", str(workload), str(iters)]
    if cpu:
        if shutil.which("taskset") is not None:
            cmd = ["taskset", "-c", str(cpu)] + cmd
    with open(workload_stdout, "w", encoding="utf-8") as out, open(perf_csv, "w", encoding="utf-8") as err:
        proc = subprocess.run(cmd, stdout=out, stderr=err, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"perf smoke command failed with {proc.returncode}; see {perf_csv} and {workload_stdout}")


def run_cli(args, allow_infeasible=False):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "counterpoint_lite"] + args, env=env, text=True)
    if proc.returncode != 0 and not (allow_infeasible and proc.returncode == 1):
        raise RuntimeError(f"counterpoint_lite {' '.join(args)} failed with {proc.returncode}")
    return proc.returncode


def make_negative_observation(src, dst, counter_name, factor):
    obs = load_json(src)
    for c in obs.get("counters", []):
        if c.get("name") == counter_name:
            value = float(c["value"]) * factor
            c["value"] = value
            c["ci_low"] = value * 0.99
            c["ci_high"] = value * 1.01
            break
    else:
        raise KeyError(f"counter {counter_name} not found in {src}")
    obs.setdefault("source", {})["kind"] = "perf-stat-negative-smoke"
    write_json(obs, dst)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

