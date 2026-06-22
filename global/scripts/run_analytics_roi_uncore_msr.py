#!/usr/bin/env python3
import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


THIS = Path(__file__).resolve()
GLOBAL_ROOT = THIS.parents[1]
SIM_ROOT = GLOBAL_ROOT.parent
WORKLOAD_DIR = SIM_ROOT / "workloads" / "analytics_roi"
WORKLOAD = WORKLOAD_DIR / "analytics_roi"
UNCORE_DIR = SIM_ROOT / "uncore_msr"
UNCORE_BIN = UNCORE_DIR / "pmu_hybrid_collector"
DEFAULT_EVENTS = GLOBAL_ROOT / "configs" / "analytics_roi_uncore_msr_events.txt"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run analytics_roi under uncore_msr at 100ms granularity and align ROI by workload timestamps."
    )
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--roi-iters", type=int, default=8)
    ap.add_argument("--interval-ms", type=int, default=100)
    ap.add_argument("--workload-cpu", default="", help="CPU pinned for the workload; default auto-picks a non-sampling CPU on the socket")
    ap.add_argument("--socket", type=int, default=0)
    ap.add_argument("--events", default=str(DEFAULT_EVENTS))
    ap.add_argument("--sudo", action="store_true", help="prefix pmu_hybrid_collector with sudo")
    ap.add_argument("--kernel-mux", action="store_true")
    ap.add_argument("--switch-us", type=int, default=500)
    ap.add_argument("--out", default="")
    ap.add_argument("--name", default="")
    args = ap.parse_args(argv)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = args.name or f"analytics_roi_uncore_{timestamp}"
    out = Path(args.out) if args.out else GLOBAL_ROOT / "out" / f"analytics_roi_uncore_{timestamp}"
    out.mkdir(parents=True, exist_ok=True)

    build_binaries()
    workload_cpu = resolve_workload_cpu(args.socket, args.workload_cpu)

    collector_csv = out / "collector.csv"
    collector_log = out / "collector.log"
    roi_ts_json = out / "roi_timestamps.json"
    aligned_csv = out / "collector.roi_aligned.csv"
    summary_json = out / "summary.json"

    run_collector(args, workload_cpu, collector_csv, collector_log, roi_ts_json)
    roi_info = load_json(roi_ts_json)
    rows = load_csv_rows(collector_csv)
    alignment = align_rows_to_roi(rows, roi_info["roi_begin_ns"], roi_info["roi_end_ns"])
    write_aligned_csv(aligned_csv, alignment["rows"], alignment["fieldnames"])
    cpu_binding = parse_sampling_cpus(collector_log)
    separation_ok = all(
        cpu is None or cpu != workload_cpu
        for cpu in (cpu_binding.get("core_sampling_cpu"), cpu_binding.get("uncore_sampling_cpu"))
    )

    summary = {
        "ok": True,
        "run_name": run_name,
        "workload_cpu": workload_cpu,
        "sampling_cpus": cpu_binding,
        "cpu_separation_ok": separation_ok,
        "collector_csv": str(collector_csv),
        "collector_log": str(collector_log),
        "roi_timestamps": roi_info,
        "interval_ms": args.interval_ms,
        "aligned_csv": str(aligned_csv),
        "alignment": summarize_alignment(alignment, roi_info, args.interval_ms),
        "roi_counter_totals": compute_counter_totals(alignment["rows"], alignment["fieldnames"]),
    }
    write_json(summary, summary_json)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_binaries():
    run(["make"], cwd=WORKLOAD_DIR)
    run(["make"], cwd=UNCORE_DIR)


def run_collector(args, workload_cpu: int, collector_csv: Path, collector_log: Path, roi_ts_json: Path):
    workload_cmd = [
        "taskset", "-c", str(workload_cpu),
        str(WORKLOAD),
        "--warmup", str(args.warmup),
        "--roi-iters", str(args.roi_iters),
        "--no-perf",
        "--roi-ts-json", str(roi_ts_json),
    ]

    cmd = []
    if args.sudo:
        cmd.append("sudo")
    cmd.extend([
        str(UNCORE_BIN),
        "-i", str(args.interval_ms),
        "-s", str(args.socket),
        "-e", str(args.events),
        "-o", str(collector_csv),
    ])
    if args.kernel_mux:
        cmd.extend(["--kernel-mux", "--switch-us", str(args.switch_us)])
    cmd.extend(["--", *workload_cmd])

    run(cmd, cwd=UNCORE_DIR, stdout_path=collector_log, stderr_to_stdout=True)


def resolve_workload_cpu(socket: int, workload_cpu_arg: str) -> int:
    if workload_cpu_arg:
        return int(workload_cpu_arg)

    socket_cpus = detect_socket_cpus(socket)
    if len(socket_cpus) < 2:
        raise RuntimeError(
            f"socket {socket} has fewer than 2 visible CPUs; cannot auto-pick a workload CPU distinct from the sampling CPU"
        )
    # Core/uncore collectors pin their sampling threads to the first CPU on the socket.
    return socket_cpus[1]


def detect_socket_cpus(socket: int):
    cpus = []
    cpu_root = Path("/sys/devices/system/cpu")
    for cpu_dir in sorted(cpu_root.glob("cpu[0-9]*")):
        pkg = cpu_dir / "topology" / "physical_package_id"
        if not pkg.exists():
            continue
        try:
            if int(pkg.read_text(encoding="utf-8").strip()) == socket:
                cpus.append(int(cpu_dir.name[3:]))
        except ValueError:
            continue
    return cpus


def parse_sampling_cpus(collector_log: Path):
    text = collector_log.read_text(encoding="utf-8", errors="replace")
    core_cpu = None
    uncore_cpu = None

    m = re.search(r"\[core-perf\] Socket \d+: sampling on CPU (\d+)", text)
    if m:
        core_cpu = int(m.group(1))
    m = re.search(r"\[perf-hybrid\] Socket \d+: sampling on CPU (\d+) \(io_cpu=(\d+)\)", text)
    if m:
        uncore_cpu = int(m.group(1))

    return {
        "core_sampling_cpu": core_cpu,
        "uncore_sampling_cpu": uncore_cpu,
    }


def load_csv_rows(path: Path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, reader.fieldnames or []


def align_rows_to_roi(csv_data, roi_begin_ns: int, roi_end_ns: int):
    rows, fieldnames = csv_data
    selected = []
    nearest_begin = None
    nearest_end = None

    for row in rows:
        timestamp_ns = int(round(float(row["timestamp_ms"]) * 1_000_000.0))
        row["_timestamp_ns"] = timestamp_ns

        begin_dist = abs(timestamp_ns - roi_begin_ns)
        end_dist = abs(timestamp_ns - roi_end_ns)
        if nearest_begin is None or begin_dist < nearest_begin["distance_ns"]:
            nearest_begin = {"timestamp_ns": timestamp_ns, "distance_ns": begin_dist}
        if nearest_end is None or end_dist < nearest_end["distance_ns"]:
            nearest_end = {"timestamp_ns": timestamp_ns, "distance_ns": end_dist}

        if roi_begin_ns <= timestamp_ns <= roi_end_ns:
            selected.append(row)

    if not selected and rows:
        begin_ts = nearest_begin["timestamp_ns"]
        end_ts = nearest_end["timestamp_ns"]
        lo = min(begin_ts, end_ts)
        hi = max(begin_ts, end_ts)
        selected = [row for row in rows if lo <= row["_timestamp_ns"] <= hi]

    return {
        "rows": selected,
        "fieldnames": fieldnames,
        "total_rows": len(rows),
        "nearest_begin": nearest_begin,
        "nearest_end": nearest_end,
    }


def summarize_alignment(alignment, roi_info, interval_ms):
    rows = alignment["rows"]
    if rows:
        first_ts = rows[0]["_timestamp_ns"]
        last_ts = rows[-1]["_timestamp_ns"]
    else:
        first_ts = None
        last_ts = None
    return {
        "roi_begin_ns": roi_info["roi_begin_ns"],
        "roi_end_ns": roi_info["roi_end_ns"],
        "roi_elapsed_ns": roi_info["roi_elapsed_ns"],
        "interval_ms": interval_ms,
        "total_csv_rows": alignment["total_rows"],
        "aligned_row_count": len(rows),
        "first_aligned_timestamp_ns": first_ts,
        "last_aligned_timestamp_ns": last_ts,
        "nearest_begin": alignment["nearest_begin"],
        "nearest_end": alignment["nearest_end"],
    }


def compute_counter_totals(rows, fieldnames):
    totals = {}
    for name in fieldnames:
        if name in ("timestamp_ms", "time_name"):
            continue
        total = 0.0
        has_value = False
        for row in rows:
            raw = row.get(name, "")
            if raw == "":
                continue
            has_value = True
            total += float(raw)
        if has_value:
            if total.is_integer():
                totals[name] = int(total)
            else:
                totals[name] = total
    return totals


def write_aligned_csv(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed_fieldnames = [f for f in fieldnames if f != "_timestamp_ns"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=trimmed_fieldnames)
        writer.writeheader()
        for row in rows:
            out_row = {k: v for k, v in row.items() if k in trimmed_fieldnames}
            writer.writerow(out_row)


def run(cmd, cwd: Path, stdout_path: Path = None, stderr_to_stdout: bool = False):
    print("+", " ".join(str(x) for x in cmd), file=sys.stderr)
    if stdout_path is None:
        subprocess.run([str(x) for x in cmd], cwd=str(cwd), check=True)
        return
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8") as f:
        subprocess.run(
            [str(x) for x in cmd],
            cwd=str(cwd),
            check=True,
            stdout=f,
            stderr=subprocess.STDOUT if stderr_to_stdout else None,
        )


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
