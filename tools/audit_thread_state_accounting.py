#!/usr/bin/env python3
"""Independently audit gem5 task on-CPU ownership and common elapsed.

Oracle observations are used only offline. The functional compiler and
FastSim receive no observed timing. Switch-entry ownership supplies labelled
proxy spans. Strict active evidence requires the separate independent native
counter/lifecycle audit; hardware perf equivalence remains unverified.
"""
from collections import defaultdict
import argparse
import json
import mmap
from pathlib import Path
import struct

from thread_state_graph import load_capture_evidence
from time_accounting import assemble_gem5_time_accounting

# Confirmed in include/fastsim/types.hpp TraceFlag. The completion expression
# matches src/trace.cpp InstructionSliceTraceSource::completes_instruction.
K_RETIRES = 1 << 0
K_MICRO_OP = 1 << 10
K_LAST_MICRO_OP = 1 << 11
HEADER = struct.Struct("<8sIIIIQQ4Q")
RECORD_BYTES = 64
FLAGS_OFFSET = 50  # TraceRecord: four uint64, four uint32, size uint16.


def json_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _tick(row):
    value = row.get("commit_tick")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("oracle commit tick must be a nonnegative integer")
    return value


def _observations(rows, observations, start, end):
    expected = {row["event_id"]: row for row in rows}
    observed = {}
    for observation in observations:
        event_id = observation.get("event_id")
        if event_id not in expected or event_id in observed:
            raise ValueError("oracle observation has unknown or duplicate event ID")
        event = expected[event_id]
        if (observation.get("schema") != "fastsim-thread-state-observation-v1"
                or observation.get("oracle_only") is not True
                or any(observation.get(field) != event[field] for field in
                       ("core_id", "record_ordinal", "event_kind"))):
            raise ValueError("oracle observation identity/schema mismatch")
        tick = _tick(observation)
        if tick > end or (event["phase"] == "warmup" and tick > start) or (
                event["phase"] == "measurement" and tick < start):
            raise ValueError("oracle event tick outside phase/common ROI bounds")
        observed[event_id] = tick
    if set(observed) != set(expected):
        raise ValueError("missing oracle observations for functional event IDs")
    for core in {row["core_id"] for row in rows}:
        stream = sorted((row for row in rows if row["core_id"] == core),
                        key=lambda row: (row["record_ordinal"], row["boundary"] != "before_record"))
        ticks = [observed[row["event_id"]] for row in stream]
        if any(right < left for left, right in zip(ticks, ticks[1:])):
            raise ValueError("nonmonotonic oracle ticks within physical source order")
    return observed


def _owner_spans(stream, observed, start, end, task_ids):
    owner, cursor = None, start
    spans, totals = [], {"active": 0, "other": 0, "unknown": 0}

    def append(last):
        nonlocal cursor
        if last <= cursor:
            return
        classification = "unknown" if owner is None else "active" if owner in task_ids else "other"
        totals[classification] += last-cursor
        spans.append({"start_tick": cursor, "end_tick": last, "owner_pid": owner,
                      "classification": classification})
        cursor = last

    for event in stream:
        tick = observed[event["event_id"]]
        kind = event["event_kind"]
        if kind not in ("initial_owner", "sched_switch"):
            continue
        if tick > start:
            append(tick)
        owner = event["os_tid"] if kind == "initial_owner" else event["next_pid"]
    append(end)
    if sum(totals.values()) != end-start:
        raise ValueError("per-core observed owner ticks do not conserve")
    return spans, totals


def _macro_counts(source, meta, stream, task_ids):
    """Count measured retired macro completions before after-record owner updates."""
    counts = {"task": 0, "other": 0, "measured": 0, "retired_uops": 0}
    by_task = defaultdict(int)
    by_ordinal = {event["record_ordinal"]: event for event in stream
                  if event["boundary"] == "after_record"}
    owner = stream[0]["os_tid"]
    with source.open("rb") as handle:
        raw = handle.read(HEADER.size)
        if len(raw) != HEADER.size:
            raise ValueError("truncated FST header")
        header = HEADER.unpack(raw)
        if header[:6] != (b"FSTRC01\0", 7, 72, RECORD_BYTES, meta["core_id"], meta["source_record_count"]):
            raise ValueError("FST header/source population mismatch")
        if source.stat().st_size < HEADER.size + RECORD_BYTES*meta["source_record_count"]:
            raise ValueError("truncated FST record population")
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            initial_pc = struct.unpack_from("<Q", data, HEADER.size)[0]
            if initial_pc != stream[0]["pc"]:
                raise ValueError("initial owner PC/source anchor mismatch")
            for ordinal in range(meta["source_record_count"]):
                offset = HEADER.size + RECORD_BYTES*ordinal
                flags = struct.unpack_from("<H", data, offset+FLAGS_OFFSET)[0]
                retires = bool(flags & K_RETIRES)
                completes = retires and (not flags & K_MICRO_OP or bool(flags & K_LAST_MICRO_OP))
                event = by_ordinal.get(ordinal)
                if event:
                    if struct.unpack_from("<Q", data, offset)[0] != event["pc"]:
                        raise ValueError("thread-state event PC/source anchor mismatch")
                    if not completes:
                        raise ValueError("after-record event must anchor a retired macro completion")
                if ordinal >= meta["warmup_records"]:
                    counts["retired_uops"] += retires
                    if completes:
                        counts["measured"] += 1
                        if owner in task_ids:
                            counts["task"] += 1
                            by_task[owner] += 1
                        else:
                            counts["other"] += 1
                if event and event["event_kind"] == "sched_switch":
                    owner = event["next_pid"]
    if counts["task"] + counts["other"] != counts["measured"]:
        raise ValueError("measured macro owner population does not conserve")
    return counts, by_task


def audit(trace_dir):
    trace_dir = Path(trace_dir)
    evidence = load_capture_evidence(trace_dir)
    metadata, rows, graph = (evidence[field] for field in ("metadata", "rows", "graph"))
    task_ids = {task["os_tid"] for task in graph["tasks"]}
    if not task_ids:
        raise ValueError("observed task accounting requires explicit runtime task population")
    oracle_dir = trace_dir / "oracle"
    expected_files = {f"time-accounting-core{core}.json" for core in range(len(metadata))}
    paths = sorted(oracle_dir.glob("time-accounting-core*.json"))
    if {path.name for path in paths} != expected_files:
        raise ValueError("missing or extra per-core elapsed oracle files")
    clock = assemble_gem5_time_accounting([json.loads(path.read_text()) for path in paths], cores=len(metadata))
    if clock["elapsed_status"] != "available":
        raise ValueError("elapsed oracle unavailable: " + clock.get("elapsed_reason", "unknown"))
    start, end, period = clock["common_start_tick"], clock["common_end_tick"], clock["clock_period_ticks"]
    observed = _observations(rows, json_rows(oracle_dir / "thread-state-observation.jsonl"), start, end)
    for child, parents in enumerate(graph["dependencies"]):
        for parent in parents:
            if observed[graph["events"][child]["event_id"]] < observed[graph["events"][parent]["event_id"]]:
                raise ValueError("oracle observation time contradicts functional causal dependency")
    per_core, active, other, unknown = [], 0, 0, 0
    task_instructions, other_instructions, measured_instructions, retired_uops = 0, 0, 0, 0
    per_task_instructions, per_task_ticks = defaultdict(int), defaultdict(int)
    for meta in metadata:
        core = meta["core_id"]
        stream = sorted((row for row in rows if row["core_id"] == core),
                        key=lambda row: (row["record_ordinal"], row["boundary"] != "before_record"))
        spans, ticks = _owner_spans(stream, observed, start, end, task_ids)
        counts, task_counts = _macro_counts(trace_dir / meta["source_file"], meta, stream, task_ids)
        active += ticks["active"]
        other += ticks["other"]
        unknown += ticks["unknown"]
        task_instructions += counts["task"]
        other_instructions += counts["other"]
        measured_instructions += counts["measured"]
        retired_uops += counts["retired_uops"]
        for tid, count in task_counts.items():
            per_task_instructions[tid] += count
        for span in spans:
            if span["owner_pid"] in task_ids:
                per_task_ticks[span["owner_pid"]] += span["end_tick"]-span["start_tick"]
        per_core.append({"core_id": core, "active_cycles_proxy": ticks["active"]/period,
                         "inactive_cycles_proxy": ticks["other"]/period,
                         "unknown_cycles_proxy": ticks["unknown"]/period,
                         "task_macro_instructions": counts["task"],
                         "other_context_macro_instructions": counts["other"],
                         "measurement_macro_instructions": counts["measured"],
                         "measurement_retired_uops": counts["retired_uops"],
                         "owner_time_conserved": True, "macro_owner_population_conserved": True,
                         "observed_initial_tick": observed[stream[0]["event_id"]],
                         "owner_spans": spans})
    if load_capture_evidence(trace_dir)["provenance"] != evidence["provenance"]:
        raise ValueError("capture changed during independent accounting audit")
    result = {**clock, "valid": True, "oracle_only": True,
            "audit_provenance": evidence["provenance"],
            "active_cpi": None, "strict_active_scope": False,
            "coverage_status": "partial-proxy", "state_scope_status": "context-boundary-proxy",
            "active_cycles_proxy": active/period, "inactive_cycles_proxy": other/period,
            "unknown_cycles_proxy": unknown/period,
            "task_macro_instructions": task_instructions,
            "other_context_macro_instructions": other_instructions,
            "measurement_macro_instructions": measured_instructions,
            "measurement_retired_uops": retired_uops,
            "active_macro_cpi_proxy": active/period/task_instructions if task_instructions else None,
            "macro_denominator_scope": "recorded-owner workload tasks, user-plus-kernel",
            "macro_completion_source": "TraceRecord::retires and (!kMicroOp || kLastMicroOp)",
            "proxy_source": "thread-state-observation:commit_tick and FSTv7 recorded owner",
            "task_ids": sorted(task_ids), "per_core": per_core,
            "per_task": [{"os_tid": tid, "active_cycles_proxy": per_task_ticks[tid]/period,
                          "macro_instructions": per_task_instructions[tid]} for tid in sorted(task_ids)],
            "functional_validation": graph["summary"],
            "limitations": ["scheduler hook cuts are a proxy, not exact on-CPU accounting",
                            "interrupts remain attributed to the recorded CPU owner",
                            "state read races and incomplete collection prevent strict active CPI",
                            "leading common-start to initial-snapshot time remains unknown"]}
    result["perf_equivalence"] = "unverified"
    if graph["summary"].get("task_clock_contract"):
        from audit_task_clock import audit_task_clock
        result = audit_task_clock(trace_dir, evidence, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.trace_dir)
        path = args.output or args.trace_dir / "oracle/thread-state-accounting.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"output": str(path), "active_status": result["active_status"],
                          "elapsed_time_seconds": result["elapsed_time_seconds"]}))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
