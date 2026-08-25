#!/usr/bin/env python3
"""Audit committed same-address store-to-load constraints in a gem5 slice.

This is an oracle-only diagnosis.  It joins functional addresses with gem5
committed stage labels to determine whether ROB-head load stalls are associated
with an older overlapping store that FastSim's register producer distances do
not describe.  No field reported here is consumed by FastSim.
"""

import argparse
import itertools
import json
import sys


TICKS_PER_CYCLE = 333


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--take", type=int, required=True)
    parser.add_argument("--core", type=int, required=True)
    parser.add_argument("--rob-entries", type=int, default=192)
    return parser.parse_args()


def cycle(tick):
    value = int(tick)
    if value % TICKS_PER_CYCLE:
        raise RuntimeError("tick is not cycle aligned: {}".format(value))
    return value // TICKS_PER_CYCLE


def bucket(distance):
    if distance <= 4:
        return "1-4"
    if distance <= 16:
        return "5-16"
    if distance <= 64:
        return "17-64"
    if distance <= 192:
        return "65-192"
    return "193+"


def increment(counts, key, amount=1):
    counts[key] = counts.get(key, 0) + amount


def load_constraint(load, producer, rob_entries):
    if producer is None:
        return "no_prior_overlapping_store"
    distance = load["index"] - producer["index"]
    if distance > rob_entries:
        return "prior_store_outside_rob"
    if producer["commit"] <= load["issue"]:
        return "prior_store_committed_before_load_issue"
    if producer["complete"] > load["issue"]:
        return "prior_store_address_not_ready_at_load_issue"
    return "prior_store_inflight_at_load_issue"


def main():
    args = parse_args()
    if args.skip < 0 or args.take <= 0 or args.rob_entries <= 0:
        raise SystemExit("invalid nonpositive slice/ROB option")

    rows = []
    with open(args.records, "r", encoding="utf-8") as records_file, \
            open(args.labels, "r", encoding="utf-8") as labels_file:
        records = itertools.islice(
            records_file, args.skip, args.skip + args.take)
        labels = itertools.islice(
            labels_file, args.skip, args.skip + args.take)
        for index, (record_line, label_line) in enumerate(
                zip(records, labels)):
            record = json.loads(record_line)
            label = json.loads(label_line)
            if record.get("micro_seq") != label.get("micro_seq"):
                raise RuntimeError("record/label mismatch at {}".format(index))
            fetch = cycle(label["fetch_tick"])
            issue_delta = int(label["issue_tick"])
            complete_delta = int(label["complete_tick"])
            if issue_delta < 0 or complete_delta < 0 or \
                    issue_delta % TICKS_PER_CYCLE or \
                    complete_delta % TICKS_PER_CYCLE:
                raise RuntimeError("invalid stage delta at {}".format(index))
            issue = fetch + issue_delta // TICKS_PER_CYCLE
            complete = fetch + complete_delta // TICKS_PER_CYCLE
            commit = cycle(label["commit_tick"])
            if not fetch <= issue <= complete <= commit:
                raise RuntimeError("nonmonotonic stage at {}".format(index))
            rows.append({
                "index": index,
                "sequence": int(label["micro_seq"]),
                "pc": int(record.get("macro_pc", record.get("pc", 0))),
                "fetch": fetch,
                "issue": issue,
                "complete": complete,
                "commit": commit,
                "address": int(record.get("paddr", 0)),
                "size": int(record.get("size", 0)),
                "load": bool(record.get("is_load", 0)) and
                        not bool(record.get("is_store", 0)),
                "store": bool(record.get("is_store", 0)) and
                         not bool(record.get("is_load", 0)),
                "producer_dists": tuple(
                    int(value) for value in record.get("producer_dists", [])
                    if int(value) != 0),
                "prior_store": None,
                "prior_same_pc_store": None,
            })

    if len(rows) != args.take:
        raise RuntimeError(
            "slice is short: expected {}, read {}".format(args.take, len(rows)))

    latest_store_by_byte = {}
    latest_store_by_pc = {}
    load_counts = {
        "loads": 0,
        "loads_with_prior_overlapping_store": 0,
        "loads_with_prior_store_within_rob": 0,
        "loads_with_explicit_producer_distance_to_store": 0,
        "loads_without_explicit_producer_distance_to_store": 0,
        "loads_with_prior_same_pc_store_within_rob": 0,
        "loads_waiting_exactly_for_prior_same_pc_store_completion": 0,
        "loads_waiting_exactly_for_nonoverlapping_same_pc_store": 0,
    }
    constraint_loads = {}
    constraint_issue_to_commit_cycles = {}
    distance_loads = {}
    same_pc_store_wakeup_relation_loads = {}
    same_pc_store_wakeup_relation_issue_to_commit_cycles = {}
    for row in rows:
        if row["load"] and row["address"] != 0 and row["size"] != 0:
            load_counts["loads"] += 1
            producer_indices = {
                latest_store_by_byte.get(byte)
                for byte in range(row["address"], row["address"] + row["size"])
            }
            producer_indices.discard(None)
            producer = rows[max(producer_indices)] \
                if producer_indices else None
            row["prior_store"] = producer
            constraint = load_constraint(row, producer, args.rob_entries)
            increment(constraint_loads, constraint)
            increment(constraint_issue_to_commit_cycles, constraint,
                      row["commit"] - row["issue"])
            if producer is not None:
                load_counts["loads_with_prior_overlapping_store"] += 1
                distance = row["index"] - producer["index"]
                increment(distance_loads, bucket(distance))
                if distance <= args.rob_entries:
                    load_counts["loads_with_prior_store_within_rob"] += 1
                    if distance in row["producer_dists"]:
                        load_counts[
                            "loads_with_explicit_producer_distance_to_store"] += 1
                    else:
                        load_counts[
                            "loads_without_explicit_producer_distance_to_store"] += 1
            same_pc_store = latest_store_by_pc.get(row["pc"])
            if same_pc_store is not None:
                same_pc_store = rows[same_pc_store]
                same_pc_distance = row["index"] - same_pc_store["index"]
                if same_pc_distance <= args.rob_entries:
                    row["prior_same_pc_store"] = same_pc_store
                    load_counts[
                        "loads_with_prior_same_pc_store_within_rob"] += 1
                    delta = row["issue"] - same_pc_store["complete"]
                    if delta < 0:
                        relation = "load_issued_before_store_completion"
                    elif delta == 0:
                        relation = "load_issued_at_store_completion"
                        load_counts[
                            "loads_waiting_exactly_for_prior_same_pc_store_completion"] += 1
                        left = max(row["address"], same_pc_store["address"])
                        right = min(
                            row["address"] + row["size"],
                            same_pc_store["address"] + same_pc_store["size"])
                        if right <= left:
                            load_counts[
                                "loads_waiting_exactly_for_nonoverlapping_same_pc_store"] += 1
                    elif delta <= 2:
                        relation = "load_issued_1_2_cycles_after_store_completion"
                    else:
                        relation = "load_issued_3plus_cycles_after_store_completion"
                    increment(same_pc_store_wakeup_relation_loads, relation)
                    increment(
                        same_pc_store_wakeup_relation_issue_to_commit_cycles,
                        relation, row["commit"] - row["issue"])
        if row["store"] and row["address"] != 0 and row["size"] != 0:
            for byte in range(row["address"], row["address"] + row["size"]):
                latest_store_by_byte[byte] = row["index"]
            latest_store_by_pc[row["pc"]] = row["index"]

    first_commit = rows[0]["commit"]
    last_commit = rows[-1]["commit"]
    cursor = 0
    while cursor < len(rows) and rows[cursor]["commit"] <= first_commit:
        cursor += 1
    head_load_stall_cycles = {}
    head_load_stall_uops = {}
    head_load_sequences = {}
    head_load_same_pc_store_wakeup_cycles = {}
    head_load_same_pc_store_wakeup_sequences = {}
    for current_cycle in range(first_commit + 1, last_commit + 1):
        committed = False
        while cursor < len(rows) and rows[cursor]["commit"] == current_cycle:
            committed = True
            cursor += 1
        if committed or cursor == len(rows):
            continue
        head = rows[cursor]
        if not head["load"] or head["issue"] > current_cycle:
            continue
        constraint = load_constraint(
            head, head["prior_store"], args.rob_entries)
        increment(head_load_stall_cycles, constraint)
        head_load_sequences.setdefault(constraint, set()).add(head["sequence"])
        same_pc_store = head["prior_same_pc_store"]
        if same_pc_store is None:
            wakeup = "no_prior_same_pc_store_within_rob"
        else:
            delta = head["issue"] - same_pc_store["complete"]
            if delta < 0:
                wakeup = "load_issued_before_store_completion"
            elif delta == 0:
                left = max(head["address"], same_pc_store["address"])
                right = min(
                    head["address"] + head["size"],
                    same_pc_store["address"] + same_pc_store["size"])
                wakeup = "load_issued_at_nonoverlapping_store_completion" \
                    if right <= left else \
                    "load_issued_at_overlapping_store_completion"
            elif delta <= 2:
                wakeup = "load_issued_1_2_cycles_after_store_completion"
            else:
                wakeup = "load_issued_3plus_cycles_after_store_completion"
        increment(head_load_same_pc_store_wakeup_cycles, wakeup)
        head_load_same_pc_store_wakeup_sequences.setdefault(
            wakeup, set()).add(head["sequence"])

    for constraint, sequences in head_load_sequences.items():
        head_load_stall_uops[constraint] = len(sequences)

    if cursor != len(rows):
        raise RuntimeError("commit scan did not consume the exact slice")
    result = {
        "schema": "fastsim.gem5-memory-dependency-audit.v1",
        "oracle_only": True,
        "core": args.core,
        "skip": args.skip,
        "take": args.take,
        "rob_entries": args.rob_entries,
        "elapsed_cycles": last_commit - first_commit,
        "load_counts": load_counts,
        "loads_by_constraint": constraint_loads,
        "load_issue_to_commit_cycles_by_constraint":
            constraint_issue_to_commit_cycles,
        "loads_by_prior_store_distance": distance_loads,
        "loads_by_same_pc_store_wakeup_relation":
            same_pc_store_wakeup_relation_loads,
        "load_issue_to_commit_cycles_by_same_pc_store_wakeup_relation":
            same_pc_store_wakeup_relation_issue_to_commit_cycles,
        "issued_head_load_stall_cycles_by_constraint":
            head_load_stall_cycles,
        "distinct_issued_head_load_uops_by_constraint":
            head_load_stall_uops,
        "issued_head_load_stall_cycles_by_same_pc_store_wakeup":
            head_load_same_pc_store_wakeup_cycles,
        "distinct_issued_head_load_uops_by_same_pc_store_wakeup": {
            name: len(sequences)
            for name, sequences in
            head_load_same_pc_store_wakeup_sequences.items()
        },
        "interpretation": {
            "prior_store_inflight_at_load_issue":
                "The most recent overlapping store was inside the ROB and "
                "had generated its address, but had not committed when the "
                "load issued; this is a store-forward/disambiguation edge.",
            "prior_store_address_not_ready_at_load_issue":
                "The most recent overlapping store was inside the ROB and "
                "its address-generation completeTick followed load issue.",
        },
    }
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
