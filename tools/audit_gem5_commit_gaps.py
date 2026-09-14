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
    parser.add_argument(
        "--native-responses",
        help="optional taotrace-native-response-v6/v7 JSONL for exact Ruby phases")
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--take", type=int, required=True)
    parser.add_argument("--core", type=int, required=True)
    parser.add_argument("--clock-period-ticks", type=int, default=333,
                        help="target clock period from matching CPL/config metadata")
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


def lifecycle_resolved(row):
    return int(row.get("native_response_count", 0)) == int(
        row.get("native_admission_count", 0)) and (
            bool(row.get("native_issuance_closed")) or
            bool(row.get("native_terminal_no_ruby")))


def load_native_facts(path, core, target_sequences):
    """Return terminal native facts keyed by committed instruction sequence."""
    facts = {}
    pending = set()
    metadata_seen = False
    with open(path, "r", encoding="utf-8") as native_file:
        for line_number, native_line in enumerate(native_file, start=1):
            row = json.loads(native_line)
            record = row.get("record")
            if record == "metadata":
                if metadata_seen:
                    raise RuntimeError("duplicate native-response metadata")
                metadata_seen = True
                if row.get("oracle_only") is not True or \
                        row.get("fst_input") is not False:
                    raise RuntimeError(
                        "native-response input is not an oracle-only sideband")
                schema = row.get("schema")
                if schema not in {
                    "taotrace-native-response-v6",
                    "taotrace-native-response-v7",
                }:
                    raise RuntimeError(
                        "unsupported native-response schema {}".format(schema))
                if schema == "taotrace-native-response-v7" and row.get(
                    "lifecycle_tick_semantics"
                ) != "sequencer-acceptance-and-hit-callback-gem5-tick":
                    raise RuntimeError(
                        "native-response v7 lacks exact lifecycle tick semantics")
                continue
            if record == "summary":
                continue
            if record not in {"commit", "resolution"}:
                raise RuntimeError(
                    "unsupported native-response record {} at line {}".format(
                        record, line_number))
            if int(row.get("core_id", -1)) != core:
                raise RuntimeError(
                    "native-response core mismatch at line {}".format(
                        line_number))
            sequence = int(row["inst_seq_num"])
            if sequence not in target_sequences:
                continue
            if record == "commit":
                if sequence in facts or sequence in pending:
                    raise RuntimeError(
                        "duplicate native commit for micro_seq {}".format(
                            sequence))
                if lifecycle_resolved(row):
                    facts[sequence] = row
                else:
                    pending.add(sequence)
                continue
            if sequence not in pending:
                raise RuntimeError(
                    "native resolution without pending commit for micro_seq {}"
                    .format(sequence))
            if not lifecycle_resolved(row):
                raise RuntimeError(
                    "non-terminal native resolution for micro_seq {}".format(
                        sequence))
            pending.remove(sequence)
            facts[sequence] = row
    if not metadata_seen:
        raise RuntimeError("native-response sideband lacks metadata")
    if pending:
        raise RuntimeError(
            "{} selected native lifecycles remain unresolved (first {})".format(
                len(pending), min(pending)))
    return facts


def native_lifecycle_phase(fact, current_cycle):
    if fact is None:
        return "native_fact_unavailable"
    admissions = int(fact.get("native_admission_count", 0))
    responses = int(fact.get("native_response_count", 0))
    if bool(fact.get("native_terminal_no_ruby")):
        return "no_ruby_terminal"
    if admissions <= 0 or responses <= 0:
        return "invalid_native_lifecycle"
    first_admission = cycle(fact["native_first_admission_tick"])
    last_admission = cycle(fact["native_last_admission_tick"])
    last_response = cycle(fact["native_last_response_tick"])
    if current_cycle < first_admission:
        return "before_first_ruby_admission"
    if current_cycle < last_admission:
        return "between_split_ruby_admissions"
    if current_cycle < last_response:
        return "waiting_for_native_response"
    return "native_response_arrived_waiting_commit"


def native_hierarchy_outcome(fact):
    """Classify by exact Ruby events, deepest observed level first."""
    if fact is None:
        return "native_fact_unavailable"
    if bool(fact.get("native_terminal_no_ruby")):
        return "no_ruby_terminal"
    hierarchy = fact.get("native_hierarchy", {})
    if int(hierarchy.get("memory_read_transactions", 0)):
        return "ruby_memory_read"
    llc = hierarchy.get("llc", {})
    if int(llc.get("remote_supplies", 0)):
        return "llc_remote_supply"
    if int(llc.get("hits", 0)):
        return "llc_hit"
    if int(llc.get("permission_upgrades", 0)):
        return "llc_permission_upgrade"
    l2 = hierarchy.get("l2", {})
    if int(l2.get("hits", 0)):
        return "private_l2_hit"
    if int(l2.get("permission_upgrades", 0)):
        return "private_l2_permission_upgrade"
    l1d = hierarchy.get("l1d", {})
    if int(l1d.get("hits", 0)):
        return "l1d_hit"
    if int(l1d.get("permission_upgrades", 0)):
        return "l1d_permission_upgrade"
    if int(fact.get("native_coalesced", 0)):
        return "sequencer_coalesced"
    return "native_hierarchy_unclassified"


def native_timing(fact, issue_cycle, commit_cycle):
    """Expose exact native lifecycle deltas without changing classification."""
    if fact is None or bool(fact.get("native_terminal_no_ruby")):
        return None
    if int(fact.get("native_admission_count", 0)) <= 0 or \
            int(fact.get("native_response_count", 0)) <= 0:
        return None
    first_admission = cycle(fact["native_first_admission_tick"])
    last_admission = cycle(fact["native_last_admission_tick"])
    last_response = cycle(fact["native_last_response_tick"])
    return {
        "first_admission_cycle": first_admission,
        "last_admission_cycle": last_admission,
        "last_response_cycle": last_response,
        "issue_to_first_admission_cycles": first_admission - issue_cycle,
        "first_admission_to_last_response_cycles":
            last_response - first_admission,
        "issue_to_last_response_cycles": last_response - issue_cycle,
        "last_response_to_commit_cycles": commit_cycle - last_response,
    }


def producer_ready_cycle(row):
    """Best available data-ready edge for an encoded register producer."""
    fact = row.get("native")
    if row["kind"] in {"load", "atomic"} and fact is not None:
        if int(fact.get("native_response_count", 0)) > 0:
            return cycle(fact["native_last_response_tick"])
        if bool(fact.get("native_terminal_no_ruby")):
            return row["complete"]
    return row["complete"]


def main():
    global TICKS_PER_CYCLE
    args = parse_args()
    if args.clock_period_ticks <= 0:
        raise ValueError("clock period must be positive")
    TICKS_PER_CYCLE = args.clock_period_ticks
    if args.skip < 0 or args.take <= 0:
        raise SystemExit("--skip must be nonnegative and --take must be positive")

    selected_trace_records = []
    with open(args.records, "r", encoding="utf-8") as records_file:
        for record_line in itertools.islice(
                records_file, args.skip, args.skip + args.take):
            selected_trace_records.append(json.loads(record_line))
    if len(selected_trace_records) != args.take:
        raise RuntimeError(
            "record slice is short: expected {}, read {}".format(
                args.take, len(selected_trace_records)))
    selected_records = [
        record for record in selected_trace_records
        if "micro_seq" in record
    ]
    auxiliary_records = len(selected_trace_records) - len(selected_records)

    native_facts = {}
    native_memory_sequences = {
        int(record["seq_num"])
        for record in selected_records
        if record.get("is_load") or record.get("is_store") or
        record.get("is_atomic")
    }
    if args.native_responses:
        native_facts = load_native_facts(
            args.native_responses, args.core, native_memory_sequences)
        missing_native = sorted(native_memory_sequences - set(native_facts))
        if missing_native:
            raise RuntimeError(
                "{} selected memory UOPs lack native facts (first micro_seq {})"
                .format(len(missing_native), missing_native[0]))

    # Syscall sidecar rows live in the functional record stream but have no O3
    # stage label.  They make a positional records/labels join silently shift
    # every later UOP.  Target drain can also make the files differ near EOF,
    # so join committed hardware UOPs by their stable micro_seq identity.
    target_sequences = {
        int(record["micro_seq"]) for record in selected_records
    }
    labels_by_sequence = {}
    with open(args.labels, "r", encoding="utf-8") as labels_file:
        for label_line in labels_file:
            label = json.loads(label_line)
            sequence = int(label["micro_seq"])
            if sequence in target_sequences:
                labels_by_sequence[sequence] = label

    rows = []
    missing_labels = []
    for offset, record in enumerate(selected_records):
        sequence = int(record["micro_seq"])
        label = labels_by_sequence.get(sequence)
        if label is None:
            missing_labels.append(sequence)
            continue
        if record.get("micro_seq") != label.get("micro_seq"):
            raise RuntimeError(
                "record/label mismatch at slice offset {}".format(offset))
        fetch = cycle(label["fetch_tick"])
        issue_delta = int(label["issue_tick"])
        complete_delta = int(label["complete_tick"])
        if issue_delta < 0 or complete_delta < 0 or \
                issue_delta % TICKS_PER_CYCLE or \
                complete_delta % TICKS_PER_CYCLE:
            raise RuntimeError(
                "invalid issue/complete delta at slice offset {}".format(
                    offset))
        issue = fetch + issue_delta // TICKS_PER_CYCLE
        complete = fetch + complete_delta // TICKS_PER_CYCLE
        commit = cycle(label["commit_tick"])
        if not fetch <= issue <= complete <= commit:
            raise RuntimeError(
                "nonmonotonic stages at slice offset {}".format(offset))
        rows.append({
            "index": len(rows),
            "sequence": int(label["micro_seq"]),
            "dynamic_sequence": int(record.get("seq_num", -1)),
            "fetch": fetch,
            "issue": issue,
            "complete": complete,
            "commit": commit,
            "kind": head_kind(record),
            "pc": int(record["macro_pc"]),
            "op_class": int(record.get("op_class", 0)),
            "kernel": int(record.get("cpl", 3)) != 3,
            "serializing": bool(record.get("is_serialize", 0)),
            "branch": bool(record.get("is_branch", 0)),
            "mispredicted": bool(label.get("mispredicted", 0)),
            "microop": bool(record.get("is_microop")),
            "last_microop": bool(record.get("is_last_microop")),
            "source_registers": int(record.get("n_src", 0)),
            "path_class": int(record.get("path_class", 0)),
            "coh_oracle": int(record.get("coh_oracle", 0)),
            "dtlb_hit": bool(record.get("dtlb_hit", 0)),
            "d_mshr_depth": int(record.get("d_mshr_depth", 0)),
            "d_walker_levels": int(record.get("d_walker_levels", 0)),
            "d_walker_dram_misses":
                int(record.get("d_walker_dram_misses", 0)),
            "vaddr": int(record.get("vaddr", 0)),
            "paddr": int(record.get("paddr", 0)),
            "producer_distances": tuple(
                int(distance)
                for distance in record.get("producer_dists", [])
                if int(distance) != 0),
            "native": native_facts.get(int(record.get("seq_num", -1))),
        })

    if missing_labels:
        raise RuntimeError(
            "{} selected UOPs lack stage labels (first micro_seq {})".format(
                len(missing_labels), missing_labels[0]))
    if not rows:
        raise RuntimeError("selected slice has no committed hardware UOPs")
    if any(left["commit"] > right["commit"]
           for left, right in zip(rows, rows[1:])):
        raise RuntimeError("slice is not in nondecreasing commit order")

    rows_by_sequence = {row["sequence"]: row for row in rows}
    encoded_dependency_timing = {
        "uops_with_available_encoded_producers": 0,
        "uops_with_prior_slice_producer_unavailable": 0,
        "uops_issued_before_latest_producer_ready": 0,
        "uops_issued_at_latest_producer_ready": 0,
        "uops_issued_after_latest_producer_ready": 0,
    }
    for row in rows:
        latest_ready = None
        latest_outcome = None
        unavailable = False
        for distance in row["producer_distances"]:
            producer = rows_by_sequence.get(row["sequence"] - distance)
            if producer is None:
                unavailable = True
                continue
            ready = producer_ready_cycle(producer)
            if latest_ready is None or ready > latest_ready:
                latest_ready = ready
                latest_outcome = native_hierarchy_outcome(
                    producer.get("native")) \
                    if producer["kind"] in {"load", "atomic"} else \
                    producer["kind"]
        row["latest_encoded_producer_ready"] = latest_ready
        row["latest_encoded_producer_outcome"] = latest_outcome
        row["encoded_producer_unavailable"] = unavailable
        if unavailable:
            encoded_dependency_timing[
                "uops_with_prior_slice_producer_unavailable"] += 1
        if latest_ready is None:
            continue
        encoded_dependency_timing[
            "uops_with_available_encoded_producers"] += 1
        if row["issue"] < latest_ready:
            relation = "uops_issued_before_latest_producer_ready"
        elif row["issue"] == latest_ready:
            relation = "uops_issued_at_latest_producer_ready"
        else:
            relation = "uops_issued_after_latest_producer_ready"
        encoded_dependency_timing[relation] += 1

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
    issued_load_native_phase_cycles = {}
    issued_load_native_outcome_cycles = {}
    issued_load_native_phase_sequences = {}
    issued_load_native_outcome_sequences = {}
    fetched_not_issued_reason_cycles = {}
    fetched_not_issued_reason_sequences = {}
    fetched_not_issued_dependency_outcome_cycles = {}
    fetched_not_issued_op_class_cycles = {}
    fetched_not_issued_scope_cycles = {}
    fetched_not_issued_serializing_cycles = {}
    fetched_not_issued_truncation_cycles = {}
    fetched_not_issued_cycles_by_sequence = {}
    not_fetched_predecessor_cycles = {}
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
        if stage == "head_fetched_not_issued":
            producer_ready = head["latest_encoded_producer_ready"]
            if producer_ready is not None and producer_ready > current_cycle:
                reason = "waiting_for_encoded_register_producer"
                outcome = head["latest_encoded_producer_outcome"]
                increment(
                    fetched_not_issued_dependency_outcome_cycles,
                    outcome if outcome is not None else "unknown_producer")
            elif head["encoded_producer_unavailable"]:
                reason = "prior_slice_register_producer_unavailable"
            else:
                reason = "encoded_producers_ready_or_absent"
            increment(fetched_not_issued_reason_cycles, reason)
            fetched_not_issued_reason_sequences.setdefault(
                reason, set()).add(head["sequence"])
            increment(
                fetched_not_issued_op_class_cycles,
                str(head["op_class"]))
            increment(
                fetched_not_issued_scope_cycles,
                "kernel" if head["kernel"] else "user")
            increment(
                fetched_not_issued_serializing_cycles,
                "serializing" if head["serializing"] else
                "non_serializing")
            increment(
                fetched_not_issued_truncation_cycles,
                "source_count_exceeds_encoded_producers"
                if head["source_registers"] >
                len(head["producer_distances"]) else
                "all_sources_have_or_need_no_encoded_producer")
            increment(
                fetched_not_issued_cycles_by_sequence,
                str(head["sequence"]))
        elif stage == "head_not_fetched":
            previous = rows[head["index"] - 1] \
                if head["index"] != 0 else None
            if previous is None:
                predecessor = "slice_entry"
            elif previous["mispredicted"]:
                predecessor = "mispredicted_branch"
            elif previous["serializing"]:
                predecessor = "serializing_uop"
            elif previous["kernel"] != head["kernel"]:
                predecessor = "privilege_transition"
            else:
                predecessor = "other"
            increment(not_fetched_predecessor_cycles, predecessor)
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
            if args.native_responses:
                native_phase = native_lifecycle_phase(
                    head["native"], current_cycle)
                native_outcome = native_hierarchy_outcome(head["native"])
                increment(issued_load_native_phase_cycles, native_phase)
                increment(issued_load_native_outcome_cycles, native_outcome)
                issued_load_native_phase_sequences.setdefault(
                    native_phase, set()).add(head["sequence"])
                issued_load_native_outcome_sequences.setdefault(
                    native_outcome, set()).add(head["sequence"])

    if sum(counts.values()) != elapsed or cursor != len(rows):
        raise RuntimeError("cycle/record accounting is not conserved")

    result = {
        "schema": "fastsim.gem5-commit-gap-audit.v1",
        "clock_period_ticks": TICKS_PER_CYCLE,
        "oracle_only": True,
        "core": args.core,
        "skip": args.skip,
        "take": args.take,
        "uops_with_stage_labels": len(rows),
        "auxiliary_records_without_stage_labels": auxiliary_records,
        "first_sequence": rows[0]["sequence"],
        "last_sequence": rows[-1]["sequence"],
        "first_commit_cycle": first_commit,
        "last_commit_cycle": last_commit,
        "elapsed_cycles": elapsed,
        "cycle_counts": counts,
        "zero_commit_cycles": elapsed - counts["commit_productive"],
        "zero_commit_head_kinds": kinds,
        "fetched_not_issued_head_cycles": {
            "by_encoded_dependency_state": fetched_not_issued_reason_cycles,
            "distinct_uops_by_encoded_dependency_state": {
                name: len(sequences)
                for name, sequences in
                fetched_not_issued_reason_sequences.items()
            },
            "waiting_cycles_by_latest_producer_outcome":
                fetched_not_issued_dependency_outcome_cycles,
            "by_op_class": fetched_not_issued_op_class_cycles,
            "by_scope": fetched_not_issued_scope_cycles,
            "by_serializing_marker":
                fetched_not_issued_serializing_cycles,
            "by_possible_four_distance_truncation":
                fetched_not_issued_truncation_cycles,
            "top_uops": [
                {
                    "micro_seq": int(sequence),
                    "dynamic_seq":
                        rows_by_sequence[int(sequence)]["dynamic_sequence"],
                    "cycles": cycles,
                    "pc": rows_by_sequence[int(sequence)]["pc"],
                    "kind": rows_by_sequence[int(sequence)]["kind"],
                    "op_class":
                        rows_by_sequence[int(sequence)]["op_class"],
                    "kernel": rows_by_sequence[int(sequence)]["kernel"],
                    "serializing":
                        rows_by_sequence[int(sequence)]["serializing"],
                    "source_registers":
                        rows_by_sequence[int(sequence)]["source_registers"],
                    "encoded_producers": len(
                        rows_by_sequence[int(sequence)][
                            "producer_distances"]),
                }
                for sequence, cycles in sorted(
                    fetched_not_issued_cycles_by_sequence.items(),
                    key=lambda item: (-item[1], int(item[0])))[:20]
            ],
            "interpretation":
                "Encoded producer readiness uses native Ruby response for "
                "load/atomic producers and committed completeTick for other "
                "producers. Remaining cycles include issue/FU/LSQ contention "
                "and dependencies absent from the four-distance FST field.",
        },
        "not_fetched_head_cycles": {
            "by_immediate_committed_predecessor":
                not_fetched_predecessor_cycles,
            "warning":
                "The immediate committed predecessor is correlation only; "
                "a squash/trap can be caused by an uncommitted instruction "
                "or external event that is absent from the FST.",
        },
        "encoded_dependency_timing": encoded_dependency_timing,
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
            "top_uops": [
                {
                    "micro_seq": int(sequence),
                    "cycles": cycles,
                    "pc": rows_by_sequence[int(sequence)]["pc"],
                    "op_class": rows_by_sequence[int(sequence)]["op_class"],
                    "path_class":
                        rows_by_sequence[int(sequence)]["path_class"],
                    "coh_oracle":
                        rows_by_sequence[int(sequence)]["coh_oracle"],
                    "dtlb_hit":
                        rows_by_sequence[int(sequence)]["dtlb_hit"],
                    "d_mshr_depth":
                        rows_by_sequence[int(sequence)]["d_mshr_depth"],
                    "d_walker_levels":
                        rows_by_sequence[int(sequence)]["d_walker_levels"],
                    "d_walker_dram_misses":
                        rows_by_sequence[int(sequence)][
                            "d_walker_dram_misses"],
                    "vaddr": rows_by_sequence[int(sequence)]["vaddr"],
                    "paddr": rows_by_sequence[int(sequence)]["paddr"],
                    "issue_cycle":
                        rows_by_sequence[int(sequence)]["issue"],
                    "commit_cycle":
                        rows_by_sequence[int(sequence)]["commit"],
                    "native_hierarchy_outcome": native_hierarchy_outcome(
                        rows_by_sequence[int(sequence)].get("native"))
                        if args.native_responses else None,
                    "native_timing": native_timing(
                        rows_by_sequence[int(sequence)].get("native"),
                        rows_by_sequence[int(sequence)]["issue"],
                        rows_by_sequence[int(sequence)]["commit"])
                        if args.native_responses else None,
                }
                for sequence, cycles in sorted(
                    issued_load_head_cycles_by_sequence.items(),
                    key=lambda item: (-item[1], int(item[0])))[:20]
            ],
            "legacy_proxy_warning":
                "path/coherence/dTLB fields are observer-side proxy labels; "
                "do not use them as native Ruby or DRAM truth.",
            "by_exact_native_lifecycle_phase":
                issued_load_native_phase_cycles
                if args.native_responses else None,
            "distinct_uops_by_exact_native_lifecycle_phase": {
                name: len(sequences)
                for name, sequences in
                issued_load_native_phase_sequences.items()
            } if args.native_responses else None,
            "by_exact_native_hierarchy_outcome":
                issued_load_native_outcome_cycles
                if args.native_responses else None,
            "distinct_uops_by_exact_native_hierarchy_outcome": {
                name: len(sequences)
                for name, sequences in
                issued_load_native_outcome_sequences.items()
            } if args.native_responses else None,
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
