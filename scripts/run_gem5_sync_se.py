#!/usr/bin/env python3
"""Fail-closed preflight for the currently unsupported TaoTrace SE sync path.

Use run_gem5_sync_probe.py for FS. The legacy functional collector performs
physical guest-PTE snapshots; using SE emulation tables can issue invalid
physical accesses. Keeping this explicit gate prevents a zero-event trace
from masquerading as a successful SE synchronization capture.
"""
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", required=True)
parser.add_argument("--cores", type=int, default=2)
parser.add_argument("--rounds", type=int, default=3)
parser.add_argument("--work", type=int, default=200)
parser.add_argument("--functional-sync", action="store_true")
parser.add_argument("--profile", required=True)
parser.add_argument("--max-ticks", type=int, default=10000000000)
parser.parse_args()
parser.error("SE sync collection is unsupported by this TaoTrace revision: functional capture "
             "uses a guest physical PTE snapshot incompatible with the SE page table. "
             "Use scripts/run_gem5_sync_probe.py for the validated FS path.")
