#!/usr/bin/env python3
"""Classify gem5 committed-UOP zero-commit cycles on an exact trace slice.

The half-open measurement convention used by the FS oracle is represented as
``(first commit cycle, last commit cycle]``.  For every cycle without a commit,
the oldest not-yet-committed UOP is classified by the last committed-only stage
that it has reached.  This is an oracle audit, not a FastSim input feature.
"""

import argparse
import itertools
import json
import sys


TICKS_PER_CYCLE = 333

PATH_NAMES = {
    0: "l1",
    1: "l2",
    2: "llc",
    3: "remote",
    4: "dram",
}

COHERENCE_NAMES = {
    0: "unknown",
    1: "l1_hit",
    2: "remote_hit_clean",
    3: "remote_hit_dirty",
    4: "llc_hit",
    5: "dram",
    6: "writeback_required",
    7: "l2_hit",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--take", type=int, required=True)
    parser.add_argument("--core", type=int, required=True)
    return parser.parse_args()


def cycle(tick):
    value = int(tick)
    if value % TICKS_PER_CYCLE:
        raise RuntimeError("stage tick is not cycle aligned: {}".format(value))
    return value // TICKS_PER_CYCLE


def head_kind(record):
    if record.get("is_atomic"):
        return "atomic"
    if record.get("is_load") and not record.get("is_store"):
        return "load"
    if record.get("is_store") and not record.get("is_load"):
        return "store"
    if record.get("is_branch"):
        return "branch"
    return "non_memory"


def mshr_bucket(depth):
    if depth == 0:
        return "0"
    if depth == 1:
        return "1"
    if depth <= 3:
        return "2-3"
    if depth <= 7:
        return "4-7"
    if depth <= 15:
        return "8-15"
    return "16+"


def increment(counts, key, amount=1):
    counts[key] = counts.get(key, 0) + amount


def main():
    args = parse_args()
    if args.skip < 0 or args.take <= 0:
        raise SystemExit("--skip must be nonnegative and --take must be positive")

    rows = []
    with open(args.records, "r", encoding="utf-8") as records_file, \
            open(args.labels, "r", encoding="utf-8") as labels_file:
        records = itertools.islice(records_file, args.skip,
                                   args.skip + args.take)
        labels = itertools.islice(labels_file, args.skip,
                                  args.skip + args.take)
        for offset, (record_line, label_line) in enumerate(
                zip(records, labels)):
            record = json.loads(record_line)
            label = json.loads(label_line)
            if record.get("micro_seq") != label.get("micro_seq"):
                raise RuntimeError(
                    "record/label mismatch at slice offset {}".format(offset))
            fetch = cycle(label["fetch_tick"])
            issue_delta = int(label["issue_tick"])
            if issue_delta < 0 or issue_delta % TICKS_PER_CYCLE:
                raise RuntimeError(
                    "invalid issue delta at slice offset {}".format(offset))
            issue = fetch + issue_delta // TICKS_PER_CYCLE
            commit = cycle(label["commit_tick"])
            if not fetch <= issue <= commit:
                raise RuntimeError(
                    "nonmonotonic stages at slice offset {}".format(offset))
            rows.append({
                "sequence": int(label["micro_seq"]),
                "fetch": fetch,
                "issue": issue,
                "commit": commit,
                "kind": head_kind(record),
                "pc": int(record["macro_pc"]),
                "microop": bool(record.get("is_microop")),
                "last_microop": bool(record.get("is_last_microop")),
                "path_class": int(record.get("path_class", 0)),
                "coh_oracle": int(record.get("coh_oracle", 0)),
                "dtlb_hit": bool(record.get("dtlb_hit", 0)),
                "d_mshr_depth": int(record.get("d_mshr_depth", 0)),
            })

    if len(rows) != args.take:
        raise RuntimeError(
            "slice is short: expected {}, read {}".format(args.take, len(rows)))
    if any(left["commit"] > right["commit"]
           for left, right in zip(rows, rows[1:])):
        raise RuntimeError("slice is not in nondecreasing commit order")

    first_commit = rows[0]["commit"]
    last_commit = rows[-1]["commit"]
    elapsed = last_commit - first_commit
    cursor = 0
    while cursor < len(rows) and rows[cursor]["commit"] <= first_commit:
        cursor += 1

    counts = {
        "commit_productive": 0,
        "head_not_fetched": 0,
        "head_fetched_not_issued": 0,
        "head_issued_not_committed": 0,
        "drain_without_slice_head": 0,
    }
    kinds = {}
    commit_width_histogram = {}
    macro_completions_histogram = {}
    productive_cycles_ending_at_macro_boundary = 0
    issued_load_path_cycles = {}
    issued_load_coherence_cycles = {}
    issued_load_dtlb_cycles = {}
    issued_load_mshr_depth_cycles = {}
    issued_load_head_cycles_by_sequence = {}
    for current_cycle in range(first_commit + 1, last_commit + 1):
        commit_begin = cursor
        while cursor < len(rows) and rows[cursor]["commit"] == current_cycle:
            cursor += 1
        if cursor != commit_begin:
            committed = cursor - commit_begin
            completed_macros = sum(
                (not row["microop"]) or row["last_microop"]
                for row in rows[commit_begin:cursor])
            counts["commit_productive"] += 1
            commit_width_histogram[str(committed)] = \
                commit_width_histogram.get(str(committed), 0) + 1
            macro_completions_histogram[str(completed_macros)] = \
                macro_completions_histogram.get(str(completed_macros), 0) + 1
            productive_cycles_ending_at_macro_boundary += int(
                (not rows[cursor - 1]["microop"]) or
                rows[cursor - 1]["last_microop"])
            continue
        if cursor == len(rows):
            counts["drain_without_slice_head"] += 1
            continue
        head = rows[cursor]
        if head["fetch"] > current_cycle:
            stage = "head_not_fetched"
        elif head["issue"] > current_cycle:
            stage = "head_fetched_not_issued"
        else:
            stage = "head_issued_not_committed"
        counts[stage] += 1
        kinds.setdefault(stage, {})[head["kind"]] = \
            kinds.setdefault(stage, {}).get(head["kind"], 0) + 1
        if stage == "head_issued_not_committed" and head["kind"] == "load":
            path_name = PATH_NAMES.get(
                head["path_class"], "unknown_{}".format(head["path_class"]))
            coherence_name = COHERENCE_NAMES.get(
                head["coh_oracle"],
                "unknown_{}".format(head["coh_oracle"]))
            increment(issued_load_path_cycles, path_name)
            increment(issued_load_coherence_cycles, coherence_name)
            increment(issued_load_dtlb_cycles,
                      "hit" if head["dtlb_hit"] else "miss")
            increment(issued_load_mshr_depth_cycles,
                      mshr_bucket(head["d_mshr_depth"]))
            increment(issued_load_head_cycles_by_sequence,
                      str(head["sequence"]))

    if sum(counts.values()) != elapsed or cursor != len(rows):
        raise RuntimeError("cycle/record accounting is not conserved")

    result = {
        "schema": "fastsim.gem5-commit-gap-audit.v1",
        "oracle_only": True,
        "core": args.core,
        "skip": args.skip,
        "take": args.take,
        "first_sequence": rows[0]["sequence"],
        "last_sequence": rows[-1]["sequence"],
        "first_commit_cycle": first_commit,
        "last_commit_cycle": last_commit,
        "elapsed_cycles": elapsed,
        "cycle_counts": counts,
        "zero_commit_cycles": elapsed - counts["commit_productive"],
        "zero_commit_head_kinds": kinds,
        "issued_load_head_cycles": {
            "by_path": issued_load_path_cycles,
            "by_coherence": issued_load_coherence_cycles,
            "by_dtlb": issued_load_dtlb_cycles,
            "by_d_mshr_depth": issued_load_mshr_depth_cycles,
            "distinct_head_load_uops": len(issued_load_head_cycles_by_sequence),
            "max_cycles_one_head_load": max(
                issued_load_head_cycles_by_sequence.values())
                if issued_load_head_cycles_by_sequence else 0,
            "sum_cycles": sum(issued_load_head_cycles_by_sequence.values()),
        },
        "commit_width_histogram": commit_width_histogram,
        "macro_completions_per_productive_cycle_histogram":
            macro_completions_histogram,
        "productive_cycles_ending_at_macro_boundary":
            productive_cycles_ending_at_macro_boundary,
        "mean_uops_per_productive_commit_cycle":
            sum(int(width) * cycles
                for width, cycles in commit_width_histogram.items()) /
            counts["commit_productive"]
            if counts["commit_productive"] else None,
        "conserved": sum(counts.values()) == elapsed,
        "interpretation": {
            "head_not_fetched":
                "No committed-path UOP that can reach the ROB head has yet "
                "been fetched; this is a frontend-empty interval.",
            "head_fetched_not_issued":
                "The ROB-head UOP was fetched but had not issued; this mixes "
                "decode/rename/dispatch, dependency, and issue contention.",
            "head_issued_not_committed":
                "The ROB-head UOP issued but had not committed; for loads the "
                "trace's completeTick is not used because it is not a data-"
                "return timestamp in this gem5 tree.",
        },
    }
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
