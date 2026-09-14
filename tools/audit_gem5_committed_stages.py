#!/usr/bin/env python3
"""Summarize comparable committed-UOP timing labels from a gem5 Tao trace.

The trace stores issueTick/completeTick as deltas from fetchTick, while
commitTick is absolute.  This tool deliberately reports only fetch->issue and
issue->commit as comparable stage intervals: in the current gem5 tree a load's
completeTick is recorded at address-generation, not at LSQ data return.
"""

import argparse
import itertools
import json
import sys


TICKS_PER_CYCLE = 333.0


def group_for(record):
    is_load = bool(record.get("is_load"))
    is_store = bool(record.get("is_store"))
    is_atomic = bool(record.get("is_atomic"))
    if is_atomic:
        return "atomic"
    if is_load and not is_store:
        return "load"
    if is_store and not is_load:
        return "store"
    if not is_load and not is_store:
        return "non_memory"
    return "mixed_memory"


def empty_accumulator():
    return {
        "uops": 0,
        "fetch_to_issue_ticks": 0,
        "issue_to_commit_ticks": 0,
        "fetch_to_commit_ticks": 0,
        "invalid_timing_uops": 0,
    }


def add(accumulator, label):
    fetch = int(label["fetch_tick"])
    issue_delta = int(label["issue_tick"])
    commit = int(label["commit_tick"])
    issue = fetch + issue_delta
    accumulator["uops"] += 1
    if issue_delta < 0 or commit < issue:
        accumulator["invalid_timing_uops"] += 1
        return
    accumulator["fetch_to_issue_ticks"] += issue_delta
    accumulator["issue_to_commit_ticks"] += commit - issue
    accumulator["fetch_to_commit_ticks"] += commit - fetch


def finalize(accumulator):
    result = dict(accumulator)
    valid = accumulator["uops"] - accumulator["invalid_timing_uops"]
    result["valid_timing_uops"] = valid
    for field in (
        "fetch_to_issue_ticks",
        "issue_to_commit_ticks",
        "fetch_to_commit_ticks",
    ):
        name = field.replace("_ticks", "_cycles_mean")
        result[name] = accumulator[field] / TICKS_PER_CYCLE / valid \
            if valid else None
    return result


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--core", type=int)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--take", type=int)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.skip < 0 or (args.take is not None and args.take <= 0):
        raise SystemExit("--skip must be nonnegative and --take positive")
    with open(args.records, "r") as records_file:
        record_lines = itertools.islice(
            records_file, args.skip,
            None if args.take is None else args.skip + args.take)
        trace_records = [json.loads(line) for line in record_lines]
    if args.take is not None and len(trace_records) != args.take:
        raise RuntimeError(
            "slice is short: expected {}, read {}".format(
                args.take, len(trace_records)))

    # Syscall sidecar rows share the functional record stream but have no O3
    # stage label.  Target drain may also make the two files differ by a few
    # rows, so join hardware UOPs by their stable micro_seq identity.
    records_by_sequence = {
        int(record["micro_seq"]): record
        for record in trace_records if "micro_seq" in record
    }
    labels_by_sequence = {}
    with open(args.labels, "r") as labels_file:
        for label_line in labels_file:
            label = json.loads(label_line)
            sequence = int(label["micro_seq"])
            if sequence in records_by_sequence:
                labels_by_sequence[sequence] = label
    missing = sorted(set(records_by_sequence) - set(labels_by_sequence))
    if missing:
        raise RuntimeError(
            "{} selected UOPs lack stage labels (first micro_seq {})".format(
                len(missing), missing[0]))

    groups = {"all": empty_accumulator()}
    line_count = 0
    for sequence, record in records_by_sequence.items():
        label = labels_by_sequence[sequence]
        group = group_for(record)
        if group not in groups:
            groups[group] = empty_accumulator()
        add(groups["all"], label)
        add(groups[group], label)
        line_count += 1

    output = {
        "schema": "fastsim.gem5-committed-stage-audit.v1",
        "core": args.core,
        "ticks_per_cycle": TICKS_PER_CYCLE,
        "records": line_count,
        "trace_records": len(trace_records),
        "auxiliary_records_without_stage_labels":
            len(trace_records) - line_count,
        "skip": args.skip,
        "take": args.take,
        "groups": {name: finalize(value)
                   for name, value in sorted(groups.items())},
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
