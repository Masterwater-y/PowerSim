#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


THIS = Path(__file__).resolve()
ROOT = THIS.parents[1]
SIM_ROOT = ROOT.parent
DEFAULT_MODEL = ROOT / "examples" / "model_spr_core_minimal.json"
DEFAULT_MAPPING = ROOT / "configs" / "simulator_mappings.json"
DEFAULT_MINESIM = SIM_ROOT / "minesim"
DEFAULT_TRACE = SIM_ROOT / "dynamorio" / "traces" / "analytics_st_run1" / "trace" / "analytics_st.analytics_st.64672.7709.trace.gz"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a MineSim-based CounterPoint Lite smoke test")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    parser.add_argument("--minesim-dir", default=str(DEFAULT_MINESIM))
    parser.add_argument("--trace", default=str(DEFAULT_TRACE))
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--out", default=str(ROOT / "out" / "minesim_smoke"))
    parser.add_argument("--negative-factor", type=float, default=100.0)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    minesim_dir = Path(args.minesim_dir)
    trace = Path(args.trace)
    ensure_minesim(minesim_dir)
    if not trace.exists():
        raise FileNotFoundError(f"MineSim trace not found: {trace}")

    stdout = out / "minesim.stdout"
    stderr = out / "minesim.stderr"
    run_minesim(minesim_dir, trace, args.limit, stdout, stderr)

    obs = out / "observation.minesim.json"
    run_cli([
        "import-observation", "--kind", "minesim-stats",
        "--input", str(stdout),
        "--mapping", args.mapping,
        "--output", str(obs),
    ])

    positive = out / "positive"
    positive_rc = run_cli([
        "run", "--model", args.model,
        "--observation", str(obs),
        "--output-dir", str(positive),
    ], allow_infeasible=True)

    neg_obs = out / "observation.minesim.negative.json"
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
    top_component = negative_diag.get("ranked_components", [{}])[0].get("component") if negative_diag.get("ranked_components") else None
    obs_obj = load_json(obs)
    observed_counters = {c["name"]: c["value"] for c in obs_obj.get("counters", [])}

    ok = (
        positive_rc == 0
        and positive_report.get("verdict") == "feasible"
        and negative_rc == 1
        and negative_report.get("verdict") == "infeasible"
        and top_component == "llc_cha"
        and all(k in observed_counters for k in ["core.instructions", "core.cycles", "branch.misses", "cache.llc.load_misses", "tlb.dtlb_load_misses"])
    )
    summary = {
        "ok": ok,
        "out": str(out),
        "minesim_stdout": str(stdout),
        "trace": str(trace),
        "limit": args.limit,
        "observed_counters": observed_counters,
        "positive_verdict": positive_report.get("verdict"),
        "negative_verdict": negative_report.get("verdict"),
        "negative_top_component": top_component,
    }
    write_json(summary, out / "summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def ensure_minesim(minesim_dir):
    binary = minesim_dir / "build" / "minesim"
    if binary.exists() and os.access(binary, os.X_OK):
        return
    subprocess.run(["make", "-C", str(minesim_dir), "minesim"], check=True)
    if not binary.exists():
        raise FileNotFoundError(f"MineSim binary not found after build: {binary}")


def run_minesim(minesim_dir, trace, limit, stdout, stderr):
    cmd = [str(minesim_dir / "build" / "minesim"), str(trace), str(limit)]
    with open(stdout, "w", encoding="utf-8") as out, open(stderr, "w", encoding="utf-8") as err:
        proc = subprocess.run(cmd, cwd=str(minesim_dir), stdout=out, stderr=err, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"MineSim failed with {proc.returncode}; see {stdout} and {stderr}")


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
    obs.setdefault("source", {})["kind"] = "minesim-negative-smoke"
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
