#!/usr/bin/env python3
"""Render a TSim-style text report from deployment report.json."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.inference.reporting import write_deployment_text_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="merged deployment report.json")
    parser.add_argument("--out", help="default: report.txt beside input JSON")
    args = parser.parse_args()
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.input)), "report.txt")
    with open(args.input, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    write_deployment_text_report(out, report, source=args.input)
    print(f"[text-report] traces={len(report.get('traces', []))} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
