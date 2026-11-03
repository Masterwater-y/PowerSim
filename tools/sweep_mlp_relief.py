#!/usr/bin/env python3
"""Sweep core.mlp_exposure_relief over the guardrail case set.

For each relief value it re-simulates the 12 bidirectional guardrail cases,
reports each case's signed CPI error and absolute CPI error against gem5, the
LBM abs-error tail, and the aggregate CPI MAE, plus a throughput proxy
(user_uops_per_second). relief=0.0 must reproduce the frozen baseline exactly.

Usage:
  tools/sweep_mlp_relief.py --relief 0.0 0.25 0.5 0.75 1.0 [--jobs N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path):
    return json.loads(path.read_text())


def run_case(case: dict, base_config: str, scope: str, relief: float) -> dict:
    manifest = ROOT / case["manifest"]
    if not manifest.exists():
        return {**case, "status": "missing-manifest"}
    with tempfile.NamedTemporaryFile(
        suffix=".cfg", prefix="mlp-relief-", delete=False, mode="w"
    ) as cfg:
        cfg.write(f"config.include = {ROOT / base_config}\n")
        cfg.write(f"core.mlp_exposure_relief = {relief}\n")
        cfg_path = Path(cfg.name)
    with tempfile.NamedTemporaryFile(
        suffix=".json", prefix="mlp-relief-", delete=False
    ) as tmp:
        out = Path(tmp.name)
    cmd = [
        str(ROOT / "build/fastsim"), "simulate",
        "--config", str(cfg_path),
        "--manifest", str(manifest),
        "--measurement-scope", scope,
        "--cores", str(case["cores"]),
        "--output", str(out),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for p in (cfg_path,):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if proc.returncode != 0:
        return {**case, "status": "sim-failed", "log": proc.stdout[-2000:]}
    doc = load(out)
    try:
        out.unlink()
    except FileNotFoundError:
        pass
    sm = doc["scope_metrics"]
    measured = sm["perf_like_cpi"]
    gem5 = case["gem5_cpi"]
    signed = 100.0 * (measured - gem5) / gem5
    tput = sm.get("throughput", {}).get("user_uops_per_second")
    return {**case, "status": "ok", "measured_cpi": measured,
            "signed_error_percent": signed,
            "absolute_cpi_error": abs(measured - gem5),
            "user_uops_per_second": tput}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline",
                    default="configs/cpi-guardrail-baseline-v1.json")
    ap.add_argument("--relief", type=float, nargs="+",
                    default=[0.0, 0.25, 0.5, 0.75, 1.0])
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    base = load(ROOT / args.baseline)
    base_config = base["config"]
    scope = base["measurement_scope"]
    cases = base["cases"]

    all_rows = {}
    for relief in args.relief:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            rows = list(pool.map(
                lambda c: run_case(c, base_config, scope, relief), cases))
        rows.sort(key=lambda r: (r["cores"], r["case"]))
        all_rows[relief] = rows

    # Per-case table across relief values (signed error).
    reliefs = args.relief
    print("signed CPI error %% by relief:")
    header = f"{'case':28} {'cores':>5} " + \
        " ".join(f"{('r=%.2f' % r):>9}" for r in reliefs)
    print(header)
    base_rows = all_rows[reliefs[0]]
    for i, c in enumerate(base_rows):
        line = f"{c['case']:28} {c['cores']:>5} "
        vals = []
        for r in reliefs:
            row = all_rows[r][i]
            if row["status"] != "ok":
                vals.append(f"{row['status']:>9}")
            else:
                vals.append(f"{row['signed_error_percent']:>+9.3f}")
        print(line + " ".join(vals))

    print("\nabsolute CPI error (cycles/macro-inst) by relief:")
    print(header)
    for i, c in enumerate(base_rows):
        line = f"{c['case']:28} {c['cores']:>5} "
        vals = []
        for r in reliefs:
            row = all_rows[r][i]
            vals.append(f"{row.get('absolute_cpi_error', float('nan')):>9.5f}"
                        if row["status"] == "ok" else f"{row['status']:>9}")
        print(line + " ".join(vals))

    print("\naggregate over 12 cases:")
    print(f"{'relief':>8} {'CPI MAE':>10} {'LBM32 abs':>11} "
          f"{'LBM32 signed%':>14} {'tput Muops/s (mean)':>20}")
    for r in reliefs:
        rows = [x for x in all_rows[r] if x["status"] == "ok"]
        mae = sum(x["absolute_cpi_error"] for x in rows) / len(rows)
        lbm32 = next((x for x in rows if x["cores"] == 32 and
                      "lbm" in x["case"]), None)
        tputs = [x["user_uops_per_second"] for x in rows
                 if x.get("user_uops_per_second")]
        tmean = sum(tputs) / len(tputs) / 1e6 if tputs else float("nan")
        print(f"{r:>8.2f} {mae:>10.5f} "
              f"{(lbm32['absolute_cpi_error'] if lbm32 else float('nan')):>11.5f} "
              f"{(lbm32['signed_error_percent'] if lbm32 else float('nan')):>+14.3f} "
              f"{tmean:>20.3f}")

    if args.output:
        Path(args.output).write_text(
            json.dumps({str(k): v for k, v in all_rows.items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
