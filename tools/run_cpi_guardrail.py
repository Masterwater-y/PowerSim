#!/usr/bin/env python3
"""Bidirectional CPI regression guardrail.

Re-simulates the frozen guardrail case set and fails if any case's signed
relative CPI error drifts outside its band around the frozen baseline. The set
deliberately pairs the LBM overestimate tail with the systematically
underestimating tealeaf/namd cases so that any change aimed at reducing the LBM
memory-exposure overestimate cannot silently push the underestimating cases
further below gem5.

Usage:
  tools/run_cpi_guardrail.py [--baseline configs/cpi-guardrail-baseline-v1.json]
                             [--jobs N] [--output OUT.json]

Exit status is non-zero if any band is violated, so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path):
    return json.loads(path.read_text())


def run_case(case: dict, config: str, scope: str) -> dict:
    manifest = ROOT / case["manifest"]
    if not manifest.exists():
        return {**case, "status": "missing-manifest", "measured_cpi": None}
    with tempfile.NamedTemporaryFile(
        suffix=".json", prefix="cpi-guardrail-", delete=False
    ) as tmp:
        out = Path(tmp.name)
    cmd = [
        str(ROOT / "build/fastsim"), "simulate",
        "--config", str(ROOT / config),
        "--manifest", str(manifest),
        "--measurement-scope", scope,
        "--cores", str(case["cores"]),
        "--output", str(out),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        return {**case, "status": "sim-failed", "measured_cpi": None,
                "log": proc.stdout[-2000:]}
    doc = load(out)
    try:
        out.unlink()
    except FileNotFoundError:
        pass
    sm = doc["scope_metrics"]
    # Macro-instruction CPI: user-plus-kernel cycles per completed macroinstruction.
    measured = sm["perf_like_cpi"]
    gem5 = case["gem5_cpi"]
    signed = 100.0 * (measured - gem5) / gem5
    return {**case, "status": "ok", "measured_cpi": measured,
            "measured_signed_error_percent": signed,
            "measured_absolute_cpi_error": abs(measured - gem5)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline",
                    default="configs/cpi-guardrail-baseline-v1.json")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    base = load(ROOT / args.baseline)
    band = base["tolerance"]["signed_error_percent_band"]
    config = base["config"]
    scope = base["measurement_scope"]

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_case, c, config, scope)
                   for c in base["cases"]]
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda r: (r["cores"], r["case"]))

    violations = []
    print(f"{'case':28} {'cores':>5} {'frozen%':>9} {'now%':>9} "
          f"{'band':>12} {'verdict':>8}")
    for r in results:
        if r["status"] != "ok":
            violations.append({**r, "reason": r["status"]})
            print(f"{r['case']:28} {r['cores']:>5} "
                  f"{'--':>9} {'--':>9} {'':>12} {r['status']:>8}")
            continue
        frozen = r["frozen_signed_error_percent"]
        now = r["measured_signed_error_percent"]
        lo, hi = frozen - band, frozen + band
        ok = lo <= now <= hi
        if not ok:
            violations.append({**r, "reason": "band",
                               "lower": lo, "upper": hi})
        print(f"{r['case']:28} {r['cores']:>5} {frozen:>+9.3f} "
              f"{now:>+9.3f} [{lo:>+6.2f},{hi:>+6.2f}] "
              f"{'OK' if ok else 'FAIL':>8}")

    summary = {
        "schema": "fastsim-cpi-guardrail-result-v1",
        "baseline": args.baseline,
        "band_signed_error_percent": band,
        "cases": len(results),
        "violations": violations,
        "passed": not violations,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nguardrail: {len(results)} cases, "
          f"{len(violations)} violation(s), "
          f"{'PASS' if summary['passed'] else 'FAIL'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
