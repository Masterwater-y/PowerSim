"""semantic_gate_report.py — Phase 1 gate summary and pass/fail.

Reads per-variant ``final_report.json`` produced by train_phase1.py and
emits a single ``gate_summary.json`` plus a stdout table.  Applies the
docs/LLM语义建模方案.md §9 semantic-gate criteria:

  real vs pseudo:     Family-OOD WAPE relative improvement >= 5%
  real vs shuffle:    Family-OOD WAPE relative improvement >= 5%
  real vs side_only:  Family-OOD WAPE relative improvement >= 3%

Any of these unmet fails the gate. If a variant's report is missing the gate
is left as UNKNOWN (partial run).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional


def _load_report(dir_path: str) -> Optional[Dict[str, Any]]:
    p = os.path.join(dir_path, "final_report.json")
    if not os.path.isfile(p):
        return None
    with open(p, "r") as fh:
        return json.load(fh)


def _wape(report: Optional[Dict[str, Any]]) -> Optional[float]:
    if not report:
        return None
    fam = report.get("family_ood") or {}
    w = fam.get("wape")
    if isinstance(w, (int, float)) and w == w:  # not NaN
        return float(w)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True, help="ckpt dir for real variant")
    ap.add_argument("--pseudo", default="")
    ap.add_argument("--shuffle", default="")
    ap.add_argument("--side-only", default="")
    ap.add_argument("--rename", default="")
    ap.add_argument("--min-real-vs-pseudo", type=float, default=0.05)
    ap.add_argument("--min-real-vs-shuffle", type=float, default=0.05)
    ap.add_argument("--min-real-vs-side", type=float, default=0.03)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    variants = {
        "real": _load_report(args.real),
        "pseudo": _load_report(args.pseudo) if args.pseudo else None,
        "shuffle": _load_report(args.shuffle) if args.shuffle else None,
        "side_only": _load_report(args.side_only) if args.side_only else None,
        "rename": _load_report(args.rename) if args.rename else None,
    }
    wape = {k: _wape(v) for k, v in variants.items()}

    def improvement(base: str) -> Optional[float]:
        w_real, w_base = wape["real"], wape[base]
        if w_real is None or w_base is None:
            return None
        return (w_base - w_real) / max(1e-6, abs(w_base))

    gates = {
        "real_vs_pseudo": improvement("pseudo"),
        "real_vs_shuffle": improvement("shuffle"),
        "real_vs_side_only": improvement("side_only"),
    }
    print("== Phase 1 semantic gate ==")
    print(f"  real       WAPE={wape['real']}")
    for k in ["pseudo", "shuffle", "side_only", "rename"]:
        if wape[k] is not None:
            print(f"  {k:<10} WAPE={wape[k]}")
    for k, v in gates.items():
        if v is None:
            print(f"  {k}: UNKNOWN (missing variant)")
        else:
            print(f"  {k}: improvement={v*100:.2f}%")

    passes: Dict[str, Optional[bool]] = {}
    passes["real_vs_pseudo"] = None if gates["real_vs_pseudo"] is None else (
        gates["real_vs_pseudo"] >= args.min_real_vs_pseudo)
    passes["real_vs_shuffle"] = None if gates["real_vs_shuffle"] is None else (
        gates["real_vs_shuffle"] >= args.min_real_vs_shuffle)
    passes["real_vs_side_only"] = None if gates["real_vs_side_only"] is None else (
        gates["real_vs_side_only"] >= args.min_real_vs_side)
    known = [v for v in passes.values() if v is not None]
    if not known:
        print("[gate phase1] UNKNOWN — no variants to compare against")
        rc = 3
    elif all(v is True for v in known):
        print("[gate phase1] PASS")
        rc = 0
    else:
        print("[gate phase1] FAIL")
        rc = 2
    if args.output:
        with open(args.output, "w") as fh:
            json.dump({"wape": wape, "gates": gates, "passes": passes,
                       "rc": rc}, fh, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
