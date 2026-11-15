#!/usr/bin/env python3
"""Run a project command on the fixed, verified high-frequency CPU set."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CPU_AFFINITY = "52-63"
CPUS = tuple(range(52, 64))
MIN_FREQUENCY_KHZ = 2_000_000


def inspect_host():
    allowed = os.sched_getaffinity(0)
    rows = []
    for cpu in CPUS:
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}")
        if cpu not in allowed:
            raise RuntimeError(f"CPU {cpu} is unavailable; requalify the fixed CPU set")
        governor = (path / "cpufreq/scaling_governor").read_text().strip()
        khz = int((path / "cpufreq/scaling_cur_freq").read_text())
        socket = int((path / "topology/physical_package_id").read_text())
        nodes = sorted(path.glob("node[0-9]*"))
        numa_nodes = [int(node.name[4:]) for node in nodes]
        if governor != "performance" or khz < MIN_FREQUENCY_KHZ:
            raise RuntimeError(
                f"CPU {cpu} has governor={governor}, frequency={khz} kHz; "
                "high-frequency evaluation requires existing performance policy")
        if socket != 1 or numa_nodes != [1]:
            raise RuntimeError(f"CPU {cpu} placement changed; requalify socket/NUMA")
        rows.append(dict(cpu=cpu, governor=governor, frequency_khz=khz,
                         socket=socket, numa_node=numa_nodes[0]))
    return dict(cpu_affinity=CPU_AFFINITY, minimum_frequency_khz=MIN_FREQUENCY_KHZ,
                socket=1, numa_node=1, cpus=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="print the verified placement without running a command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        host = inspect_host()
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.check:
        if args.command:
            parser.error("--check does not accept a command")
        print(json.dumps(host, indent=2))
        return 0
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("provide -- COMMAND ARGS...")
    print(json.dumps(dict(host_evaluation=host)), file=sys.stderr, flush=True)
    # project_tmp.sh provides TMPDIR/TMP/TEMP only for this command.
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/project_tmp.sh"),
         "taskset", "-c", CPU_AFFINITY, *command],
        cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
