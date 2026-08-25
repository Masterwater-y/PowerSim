#!/usr/bin/env python3
"""Join gem5 MemDepUnit debug wakeups with committed TaoTrace records.

This is an oracle-only diagnostic.  It reconstructs the producer of each
``Waking up a dependent inst`` message from the immediately following
MemDepUnit completion and then joins both sequence numbers to an exact
committed trace slice.  The output distinguishes real same-PC StoreSet edges
from timing coincidences inferred from stage labels.
"""

import argparse
import itertools
import json
import re
import sys


TICKS_PER_CYCLE = 333
CORE_RE = re.compile(r"board\.processor\.switch(\d+)\.core\.memDep0")
WAKE_RE = re.compile(r"Waking up a dependent inst, \[sn:(\d+)\]")
COMPLETE_RE = re.compile(
    r"Completed mem instruction PC .* \[sn:(\d+)\]\."
)
READY_RE = re.compile(r"Adding instruction \[sn:(\d+)\] to the ready list")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", required=True)
    parser.add_argument("--records", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--take", type=int, required=True)
    parser.add_argument("--core", type=int, required=True)
    parser.add_argument("--rob-entries", type=int, default=192)
    parser.add_argument("--sample-limit", type=int, default=16)
    return parser.parse_args()


def absolute_stage_cycles(label):
    fetch_tick = int(label["fetch_tick"])
    issue_tick = int(label["issue_tick"])
    complete_tick = int(label["complete_tick"])
    if fetch_tick % TICKS_PER_CYCLE or issue_tick % TICKS_PER_CYCLE or \
            complete_tick % TICKS_PER_CYCLE:
        raise RuntimeError("stage tick is not cycle aligned")
    return (
        fetch_tick // TICKS_PER_CYCLE + issue_tick // TICKS_PER_CYCLE,
        fetch_tick // TICKS_PER_CYCLE + complete_tick // TICKS_PER_CYCLE,
    )


def overlap(left, right):
    if not left["address"] or not right["address"] or \
            not left["size"] or not right["size"]:
        return None
    return max(left["address"], right["address"]) < min(
        left["address"] + left["size"],
        right["address"] + right["size"],
    )


def relation(delta):
    if delta < 0:
        return "consumer_issued_before_producer_completion"
    if delta == 0:
        return "consumer_issued_at_producer_completion"
    if delta <= 2:
        return "consumer_issued_1_2_cycles_after_producer_completion"
    return "consumer_issued_3plus_cycles_after_producer_completion"


def increment(mapping, key, amount=1):
    mapping[key] = mapping.get(key, 0) + amount


def load_slice(args):
    lookback = min(args.skip, args.rob_entries)
    begin = args.skip - lookback
    end = args.skip + args.take
    rows = {}
    with open(args.records, "r", encoding="utf-8") as records_file, \
            open(args.labels, "r", encoding="utf-8") as labels_file:
        records = itertools.islice(records_file, begin, end)
        labels = itertools.islice(labels_file, begin, end)
        for global_index, (record_line, label_line) in enumerate(
                zip(records, labels), begin):
            record = json.loads(record_line)
            label = json.loads(label_line)
            if record.get("micro_seq") != label.get("micro_seq"):
                raise RuntimeError(
                    "record/label mismatch at line {}".format(global_index)
                )
            issue, complete = absolute_stage_cycles(label)
            seq_num = int(record["seq_num"])
            rows[seq_num] = {
                "global_index": global_index,
                "target": args.skip <= global_index < end,
                "micro_seq": int(record["micro_seq"]),
                "seq_num": seq_num,
                "pc": int(record.get("macro_pc", record.get("pc", 0))),
                "address": int(record.get("paddr", 0)),
                "size": int(record.get("size", 0)),
                "load": bool(record.get("is_load", 0)),
                "store": bool(record.get("is_store", 0)),
                "atomic": bool(record.get("is_atomic", 0)),
                "producer_dists": tuple(
                    int(value)
                    for value in record.get("producer_dists", [])
                    if int(value)
                ),
                "issue": issue,
                "complete": complete,
            }
    target_count = sum(row["target"] for row in rows.values())
    if target_count != args.take:
        raise RuntimeError(
            "slice is short: expected {}, read {}".format(
                args.take, target_count
            )
        )
    return rows


def load_wakeup_edges(args):
    pending = []
    edges = []
    with open(args.debug, "r", encoding="utf-8") as debug_file:
        for line in debug_file:
            core_match = CORE_RE.search(line)
            if core_match is None or int(core_match.group(1)) != args.core:
                continue
            wake_match = WAKE_RE.search(line)
            if wake_match is not None:
                tick = int(line.split(":", 1)[0])
                pending.append({
                    "consumer": int(wake_match.group(1)),
                    "tick": tick,
                    "made_ready": False,
                })
                continue
            ready_match = READY_RE.search(line)
            if ready_match is not None and pending:
                ready_consumer = int(ready_match.group(1))
                for wakeup in reversed(pending):
                    if wakeup["consumer"] == ready_consumer:
                        wakeup["made_ready"] = True
                        break
                continue
            complete_match = COMPLETE_RE.search(line)
            if complete_match is None or not pending:
                continue
            producer = int(complete_match.group(1))
            for wakeup in pending:
                edges.append((
                    producer,
                    wakeup["consumer"],
                    wakeup["tick"],
                    wakeup["made_ready"],
                ))
            pending.clear()
    if pending:
        raise RuntimeError("unterminated MemDepUnit wakeup group")
    return edges


def main():
    args = parse_args()
    if args.skip < 0 or args.take <= 0 or args.rob_entries <= 0 or \
            args.sample_limit < 0:
        raise SystemExit("invalid slice, ROB, or sample option")
    rows = load_slice(args)
    wakeup_edges = load_wakeup_edges(args)

    counts = {
        "debug_wakeup_edges_all": len(wakeup_edges),
        "target_consumer_wakeup_edges": 0,
        "target_consumer_resolved_producer_edges": 0,
        "regular_store_producer_edges": 0,
        "regular_load_consumer_edges": 0,
        "regular_store_consumer_edges": 0,
        "same_pc_regular_store_edges": 0,
        "same_pc_nonoverlap_regular_store_edges": 0,
        "same_pc_explicit_register_duplicate_edges": 0,
        "same_pc_within_rob_edges": 0,
        "same_pc_wakeup_tick_matches_producer_complete": 0,
        "same_pc_wakeup_made_consumer_ready": 0,
    }
    consumer_types = {}
    same_pc_issue_relations = {}
    same_pc_edges_by_pc = {}
    samples = []
    distinct_target_consumers = set()
    distinct_same_pc_consumers = set()
    for producer_seq, consumer_seq, wake_tick, wake_made_ready in wakeup_edges:
        consumer = rows.get(consumer_seq)
        if consumer is None or not consumer["target"]:
            continue
        counts["target_consumer_wakeup_edges"] += 1
        distinct_target_consumers.add(consumer_seq)
        producer = rows.get(producer_seq)
        if producer is None:
            continue
        counts["target_consumer_resolved_producer_edges"] += 1
        regular_producer = producer["store"] and not producer["atomic"]
        if regular_producer:
            counts["regular_store_producer_edges"] += 1
        if consumer["load"] and not consumer["atomic"]:
            consumer_type = "load"
            counts["regular_load_consumer_edges"] += 1
        elif consumer["store"] and not consumer["atomic"]:
            consumer_type = "store"
            counts["regular_store_consumer_edges"] += 1
        elif consumer["atomic"]:
            consumer_type = "atomic"
        else:
            consumer_type = "other"
        increment(consumer_types, consumer_type)

        if not regular_producer or producer["pc"] != consumer["pc"]:
            continue
        counts["same_pc_regular_store_edges"] += 1
        distinct_same_pc_consumers.add(consumer_seq)
        pc_key = "0x{:x}".format(consumer["pc"])
        pc_counts = same_pc_edges_by_pc.setdefault(
            pc_key, {"load": 0, "store": 0, "atomic": 0, "other": 0}
        )
        pc_counts[consumer_type] += 1
        distance = consumer["global_index"] - producer["global_index"]
        if 0 < distance <= args.rob_entries:
            counts["same_pc_within_rob_edges"] += 1
        if distance in consumer["producer_dists"]:
            counts["same_pc_explicit_register_duplicate_edges"] += 1
        address_overlap = overlap(producer, consumer)
        if address_overlap is False:
            counts["same_pc_nonoverlap_regular_store_edges"] += 1
        if wake_tick // TICKS_PER_CYCLE == producer["complete"]:
            counts[
                "same_pc_wakeup_tick_matches_producer_complete"
            ] += 1
        if wake_made_ready:
            counts["same_pc_wakeup_made_consumer_ready"] += 1
        issue_delta = consumer["issue"] - producer["complete"]
        increment(same_pc_issue_relations, relation(issue_delta))
        if len(samples) < args.sample_limit:
            samples.append({
                "producer_micro_seq": producer["micro_seq"],
                "producer_seq_num": producer_seq,
                "consumer_micro_seq": consumer["micro_seq"],
                "consumer_seq_num": consumer_seq,
                "consumer_type": consumer_type,
                "pc": consumer["pc"],
                "distance": distance,
                "address_overlap": address_overlap,
                "register_edge_duplicate":
                    distance in consumer["producer_dists"],
                "producer_complete_cycle": producer["complete"],
                "wakeup_cycle": wake_tick // TICKS_PER_CYCLE,
                "wakeup_made_consumer_ready": wake_made_ready,
                "consumer_issue_cycle": consumer["issue"],
            })

    counts["distinct_target_consumers"] = len(distinct_target_consumers)
    counts["distinct_same_pc_consumers"] = len(distinct_same_pc_consumers)
    result = {
        "schema": "fastsim.gem5-store-set-debug-audit.v1",
        "oracle_only": True,
        "core": args.core,
        "skip": args.skip,
        "take": args.take,
        "rob_entries": args.rob_entries,
        "counts": counts,
        "target_consumer_types": consumer_types,
        "same_pc_consumer_issue_relation": same_pc_issue_relations,
        "same_pc_edges_by_pc": dict(sorted(
            same_pc_edges_by_pc.items(),
            key=lambda item: -sum(item[1].values()),
        )),
        "samples": samples,
    }
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
