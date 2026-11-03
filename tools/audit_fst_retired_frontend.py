#!/usr/bin/env python3
"""Audit functional frontend companions through the maintained native reader."""
import argparse
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--fst', type=Path, action='append', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
binary = Path(os.environ.get('FASTSIM_FRONTEND_AUDIT', root / 'build/fastsim_audit_retired_frontend'))
raise SystemExit(subprocess.call([str(binary), *(str(p.resolve()) for p in args.fst)]))
