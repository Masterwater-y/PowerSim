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
DEFAULT_CONFIG = SIM_ROOT / "minesim" / "config" / "sapphire_rapids.cfg"
DEFAULT_MAPPING = ROOT / "configs" / "simulator_mappings.json"
DEFAULT_MINESIM = SIM_ROOT / "minesim"
DEFAULT_TRACE = SIM_ROOT / "dynamorio" / "traces" / "analytics_st_run1" / "trace" / "analytics_st.analytics_st.64672.7709.trace.gz"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate constraints from MineSim config, run MineSim, and diagnose component mismatches")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    parser.add_argument("--minesim-dir", default=str(DEFAULT_MINESIM))
    parser.add_argument("--trace", default=str(DEFAULT_TRACE))
    parser.add_argument("--limit", type=int, default=-1, help="MineSim instruction limit; -1 means full trace")
    parser.add_argument("--roi-begin-encoding", default="")
    parser.add_argument("--roi-end-encoding", default="")
    parser.add_argument("--out", default=str(ROOT / "out" / "minesim_config_check"))
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    minesim_dir = Path(args.minesim_dir)
    trace = Path(args.trace)
    config = Path(args.config)
    ensure_minesim(minesim_dir)
    if not trace.exists():
        raise FileNotFoundError(f"trace not found: {trace}")
    if not config.exists():
        raise FileNotFoundError(f"config not found: {config}")

    model = out / "model.from_minesim_config.json"
    signatures_json = out / "signatures.json"
    signatures_csv = out / "signatures.csv"
    stdout = out / "minesim.stdout"
    stderr = out / "minesim.stderr"
    observation = out / "observation.minesim.json"
    report = out / "report.json"
    violations = out / "violations.csv"
    diagnosis = out / "diagnosis.json"

    run_cli(["gen-minesim-model", "--config", str(config), "--name", "minesim_config_cone", "--output", str(model)])
    run_cli(["enumerate", "--model", str(model), "--output-json", str(signatures_json), "--output-csv", str(signatures_csv)])
    run_minesim(minesim_dir, trace, args.limit, stdout, stderr,
                args.roi_begin_encoding, args.roi_end_encoding)
    run_cli(["import-observation", "--kind", "minesim-stats", "--input", str(stdout), "--mapping", args.mapping, "--output", str(observation)])
    check_rc = run_cli(["check", "--signatures", str(signatures_json), "--observation", str(observation), "--output", str(report), "--violations-csv", str(violations)], allow_infeasible=True)
    run_cli(["diagnose", "--report", str(report), "--model", str(model), "--signatures", str(signatures_json), "--output", str(diagnosis)])

    rep = load_json(report)
    diag = load_json(diagnosis)
    obs = load_json(observation)
    summary = {
        "ok": True,
        "out": str(out),
        "trace": str(trace),
        "config": str(config),
        "limit": args.limit,
        "check_exit_code": check_rc,
        "verdict": rep.get("verdict"),
        "max_normalized_violation": rep.get("objective", {}).get("max_normalized_violation"),
        "ranked_components": diag.get("ranked_components", []),
        "observed_counters": {c["name"]: c["value"] for c in obs.get("counters", [])},
    }
    write_json(summary, out / "summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def ensure_minesim(minesim_dir):
    binary = minesim_dir / "build" / "minesim"
    if binary.exists() and os.access(binary, os.X_OK):
        return
    subprocess.run(["make", "-C", str(minesim_dir), "minesim"], check=True)
    if not binary.exists():
        raise FileNotFoundError(f"MineSim binary not found after build: {binary}")


def run_minesim(minesim_dir, trace, limit, stdout, stderr, roi_begin_encoding="", roi_end_encoding=""):
    cmd = [str(minesim_dir / "build" / "minesim"), str(trace)]
    if limit != -1:
        cmd.append(str(limit))
    if roi_begin_encoding:
        cmd.extend(["--roi-begin-encoding", roi_begin_encoding])
    if roi_end_encoding:
        cmd.extend(["--roi-end-encoding", roi_end_encoding])
    env = os.environ.copy()
    gcc11_lib = "/opt/gcc-11/lib64"
    if os.path.isdir(gcc11_lib):
        env["LD_LIBRARY_PATH"] = gcc11_lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    with open(stdout, "w", encoding="utf-8") as out, open(stderr, "w", encoding="utf-8") as err:
        proc = subprocess.run(cmd, cwd=str(minesim_dir), stdout=out, stderr=err, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"MineSim failed with {proc.returncode}; see {stdout} and {stderr}")


def run_cli(args, allow_infeasible=False):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "counterpoint_lite"] + args, env=env, text=True)
    if proc.returncode != 0 and not (allow_infeasible and proc.returncode == 1):
        raise RuntimeError(f"counterpoint_lite {' '.join(args)} failed with {proc.returncode}")
    return proc.returncode


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
