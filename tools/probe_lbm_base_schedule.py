#!/usr/bin/env python3
"""LBM C32 base-schedule elasticity probe (diagnostic, no code/default change).

The falsified memory-exposure analysis proved LBM C32's +7.54% overestimate is
not on the memory-response frontier (halving all memory latency moved CPI only
-0.73%). This probe attributes the dominant remainder to a concrete
base-schedule dimension by overriding the interval core's structural knobs one
at a time and measuring LBM C32 CPI elasticity against the frozen baseline
(4.200373397) and gem5 (3.906).

Each knob is swept above and below its maintained value. The dimension whose
perturbation moves CPI the most is the base-schedule bottleneck the interval
model puts on LBM's critical path relative to gem5's O3 overlap. Pure config
overrides on top of configs/gem5-fs-native-kernel.cfg; nothing is committed and
no default behavior changes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_CPI = 4.200373397
GEM5_CPI = 3.906
MANIFEST_CASE = "formal-32c-782.lbm_r"

# Maintained base-schedule values (configs/gem5-v28_1-time-epoch.cfg).
MAINTAINED = {
    "core.fetch_width": 8,
    "core.dispatch_width": 8,
    "core.issue_width": 8,
    "core.commit_width": 8,
    "core.rob_entries": 192,
    "core.iq_entries": 64,
}

# Sweep points per knob (below and above maintained).
SWEEP = {
    "core.fetch_width": [4, 8, 16],
    "core.dispatch_width": [4, 8, 16],
    "core.issue_width": [4, 8, 16],
    "core.commit_width": [4, 8, 16],
    "core.rob_entries": [96, 192, 384],
    "core.iq_entries": [32, 64, 128],
}


def manifest_for(case: str) -> str:
    d = json.loads((ROOT / "configs/cpi-guardrail-baseline-v1.json").read_text())
    return [c["manifest"] for c in d["cases"] if c["case"] == case][0]


def run(overrides: dict[str, int]) -> float | None:
    with tempfile.NamedTemporaryFile(
        suffix=".cfg", prefix="lbm-base-", delete=False, mode="w"
    ) as cfg:
        cfg.write(f"config.include = {ROOT}/configs/gem5-fs-native-kernel.cfg\n")
        # Keep the frozen BASE_CPI comparison on its original timing mode.
        cfg.write("core.store_wait_relief = false\n")
        for k, v in overrides.items():
            cfg.write(f"{k} = {v}\n")
        cfg_path = Path(cfg.name)
    out = Path(tempfile.NamedTemporaryFile(
        suffix=".json", prefix="lbm-base-", delete=False).name)
    cmd = [
        str(ROOT / "build/fastsim"), "simulate",
        "--config", str(cfg_path),
        "--manifest", str(ROOT / manifest_for(MANIFEST_CASE)),
        "--measurement-scope", "user-plus-kernel",
        "--cores", "32", "--output", str(out),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _rm(p: Path) -> None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    _rm(cfg_path)
    if proc.returncode != 0:
        print("  SIM FAILED:", proc.stdout[-500:])
        _rm(out)
        return None
    cpi = json.loads(out.read_text())["scope_metrics"]["perf_like_cpi"]
    _rm(out)
    return cpi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--knobs", nargs="*", default=list(SWEEP))
    args = ap.parse_args()

    print(f"LBM C32 base-schedule elasticity (baseline {BASE_CPI}, "
          f"gem5 {GEM5_CPI})")
    print(f"{'knob':22} {'value':>7} {'CPI':>13} {'d vs base':>11} "
          f"{'signed% vs gem5':>16}")
    for knob in args.knobs:
        for val in SWEEP[knob]:
            ov = {knob: val}
            cpi = run(ov)
            if cpi is None:
                continue
            d = cpi - BASE_CPI
            sg = 100.0 * (cpi - GEM5_CPI) / GEM5_CPI
            tag = "  (maintained)" if val == MAINTAINED[knob] else ""
            print(f"{knob:22} {val:>7} {cpi:>13.7f} {d:>+11.6f} "
                  f"{sg:>+15.3f}%{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
