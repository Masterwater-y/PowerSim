#!/usr/bin/env python3
"""Compile portable thread-state sidecars into a sparse, oracle-free plan."""
import argparse
import json
import struct
from pathlib import Path
from thread_state_graph import build_graph


def compile_plan(trace_dir, output, summary_output=None, *, require_sources=False):
    trace_dir = Path(trace_dir)
    metadata = [json.loads(path.read_text()) for path in
                sorted(trace_dir.glob("thread-state-metadata-core*.json"))]
    rows = []
    expected = {f"thread-state-core{m['core_id']}.jsonl" for m in metadata}
    found = {path.name for path in trace_dir.glob("thread-state-core*.jsonl")}
    if expected != found:
        raise ValueError("thread-state event files do not match metadata participants")
    for m in metadata:
        rows.extend(json.loads(line) for line in
                    (trace_dir / f"thread-state-core{m['core_id']}.jsonl").read_text().splitlines()
                    if line.strip())
    graph = build_graph(rows, metadata)
    checked_sources = 0
    for m in metadata:
        source = trace_dir / m["source_file"]
        if (require_sources or graph["summary"]["active_status"] == "available") and not source.is_file():
            raise ValueError(f"missing required FST source: {source}")
        if source.is_file():
            with source.open("rb") as handle:
                raw = handle.read(72)
            if len(raw) != 72:
                raise ValueError("truncated FST source header")
            header = struct.unpack("<8sIIIIQQ4Q", raw)
            if header[:6] != (b"FSTRC01\0", 7, 72, 64, m["core_id"], m["source_record_count"]):
                raise ValueError("FST header/source population mismatch")
            if source.stat().st_size < 72 + 64*m["source_record_count"]:
                raise ValueError("truncated FST source records")
            checked_sources += 1
    graph["summary"]["source_header_validated_cores"] = checked_sources
    graph["summary"]["source_header_missing_cores"] = len(metadata)-checked_sources
    events, cores, tasks = graph["events"], graph["cores"], graph["tasks"]
    if not events or not tasks:
        raise ValueError("runtime thread-state plan requires nonempty events and tasks")
    if len({task["runtime_thread_id"] for task in tasks}) != len(tasks):
        raise ValueError("first-slice runtime task identities must be globally unique")
    lines = ["FASTSIM_THREAD_STATE_PLAN_V1",
             f"CORES {len(cores)} EVENTS {len(events)} TASKS {len(tasks)}"]
    for core in cores:
        lines.append("CORE " + " ".join(str(core[field]) for field in
                     ("core_id", "total_records", "warmup_records", "measurement_records", "initial_owner")))
    for task in tasks:
        lines.append("TASK " + " ".join(str(task[field]) for field in
                     ("os_tid", "os_tgid", "runtime_thread_id")))
    if graph["summary"]["active_status"] == "available":
        lines.append("ACCOUNTING " + graph["summary"]["accounting_contract"])
        lines.extend(f"CLOSURE {core['core_id']} 1" for core in cores)
    for index, e in enumerate(events):
        parents = graph["dependencies"][index]
        fields = [index, e["core_id"], e["record_ordinal"], e["pc"], e["kind"],
                  int(e["phase"] == "measurement"), *e["args"], len(parents), *parents]
        lines.append("EVENT " + " ".join(map(str, fields)))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n")
    summary = graph["summary"]
    if summary_output is not None:
        path = Path(summary_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--require-sources", action="store_true",
                        help="Require each retained FST source and validate its header/count")
    args = parser.parse_args()
    try:
        print(json.dumps(compile_plan(args.trace_dir, args.output, args.summary,
                                      require_sources=args.require_sources), indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
