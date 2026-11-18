#!/usr/bin/env python3
"""Run a project command on the fixed, verified high-frequency CPU set."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CPU_AFFINITY = "48-63"
CPUS = tuple(range(48, 64))
MIN_FREQUENCY_KHZ = 2_000_000


def inspect_host(cpus=CPUS):
    allowed = os.sched_getaffinity(0)
    rows = []
    for cpu in cpus:
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
    physical = [(r["socket"], int((Path(f"/sys/devices/system/cpu/cpu{r['cpu']}/topology/core_id")).read_text())) for r in rows]
    if len(set(physical)) != len(physical):
        raise RuntimeError("selected CPUs do not represent distinct physical cores")
    return dict(cpu_affinity=",".join(str(cpu) for cpu in cpus), minimum_frequency_khz=MIN_FREQUENCY_KHZ,
                socket=1, numa_node=1, cpus=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cores", type=int, choices=range(1, 17), help="number of distinct physical cores to bind")
    parser.add_argument("--check", action="store_true",
                        help="print the verified placement without running a command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if args.check and command:
        parser.error("--check does not accept a command")
    if not args.check and not command:
        parser.error("provide -- COMMAND ARGS...")
    selected = CPUS[:args.cores] if args.cores else CPUS
    if "simulate" in command and "--cores" in command:
        try:
            target_cores = int(command[command.index("--cores") + 1])
        except (ValueError, IndexError):
            parser.error("invalid simulated core count")
        if not 1 <= target_cores <= len(CPUS):
            parser.error("target core count exceeds qualified physical-core pool")
        if args.cores and args.cores != target_cores:
            parser.error("physical core count must equal simulated core count")
        if Path(command[0]).name == "taskset" and (len(command) < 3 or command[1] not in ("-c", "--cpu-list")):
            parser.error("for simulation use taskset -c followed by an explicit CPU list")
        if Path(command[0]).name == "taskset":
            try:
                parsed = []
                for part in command[2].split(","):
                    endpoints = part.split("-")
                    if len(endpoints) == 1:
                        parsed.append(int(endpoints[0]))
                    elif len(endpoints) == 2:
                        parsed.extend(range(int(endpoints[0]), int(endpoints[1]) + 1))
                    else:
                        raise ValueError("invalid CPU range")
                selected = tuple(parsed)
            except ValueError:
                parser.error("invalid nested taskset CPU list")
            if len(selected) != target_cores or len(set(selected)) != target_cores or not set(selected).issubset(CPUS):
                parser.error("nested taskset must select one qualified physical CPU per simulated core")
        else:
            selected = CPUS[:target_cores]
    try:
        host = inspect_host(selected)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.check:
        print(json.dumps(host, indent=2))
        return 0
    print(json.dumps(dict(host_evaluation=host)), file=sys.stderr, flush=True)
    # project_tmp.sh provides TMPDIR/TMP/TEMP only for this command.
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/project_tmp.sh"),
         "taskset", "-c", host["cpu_affinity"], *command],
        cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
