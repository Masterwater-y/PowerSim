#!/usr/bin/env python3
"""Oracle-only, population-checked native/FastSim load lifecycle diagnostics.

No result from this module is a simulator timing input. Native Ruby facts come
from the native-response sideband; proxy path classes are deliberately unused.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import struct


IQ_LINE = re.compile(r"^\s*(\d+):\s+.*?switch(\d+)\.core\.iq:\s+(.*)$")
IQ_INSERT = re.compile(r"Adding (?:non-speculative )?instruction \[sn:(\d+)\] PC (.*) to the IQ")
IQ_RELEASE = re.compile(r"Completing mem instruction PC: (.*) \[sn:(\d+)\]")
IQ_ISSUE = re.compile(r"Thread \d+: Issuing instruction PC (.*) \[sn:(\d+)\]")


def parse_iq_event(line):
    match = IQ_LINE.match(line)
    if not match:
        return None
    tick, core, text = match.groups()
    event = IQ_INSERT.search(text)
    if event:
        sequence, pc_text = event.groups()
        kind = "insert"
    else:
        event = IQ_RELEASE.search(text)
        if not event:
            event = IQ_ISSUE.search(text)
            if not event:
                return None
            kind = "issue"
        else:
            kind = "release"
        pc_text, sequence = event.groups()
    pc = re.search(r"0x[0-9a-fA-F]+", pc_text)
    if not pc:
        raise ValueError("IQ event lacks PC")
    return dict(core=int(core), sequence=int(sequence), pc=int(pc[0], 16),
                tick=int(tick), kind=kind)


def load_iq(path, wanted):
    result = {}
    with Path(path).open() as stream:
        for line in stream:
            if not any(x in line for x in ("Adding ", "Completing mem instruction", "Issuing instruction")):
                continue
            event = parse_iq_event(line)
            if event is None:
                continue
            key = event["core"], event["sequence"]
            if key not in wanted:
                continue
            row = result.setdefault(key, dict(pc=event["pc"]))
            if row["pc"] != event["pc"]:
                raise ValueError(f"IQ PC changed for {key}")
            kind = event["kind"]
            if kind == "issue":
                row.setdefault("first_issue", event["tick"])
                row["last_issue"] = event["tick"]
                row["issue_attempts"] = row.get("issue_attempts", 0) + 1
                continue
            if kind in row:
                raise ValueError(f"duplicate IQ {kind} for {key}")
            row[kind] = event["tick"]
    return result


def overlap_summary(intervals, begin, end):
    if end <= begin:
        raise ValueError("positive observation window required")
    events = {}
    for first, last in intervals:
        if last < first:
            raise ValueError("inverted lifetime")
        first, last = max(first, begin), min(last, end)
        if last <= first:
            continue
        events[first] = events.get(first, 0) + 1
        events[last] = events.get(last, 0) - 1
    area = occupancy = peak = 0
    previous = begin
    for tick in sorted(events):
        area += occupancy * (tick - previous)
        occupancy += events[tick]
        peak = max(peak, occupancy)
        previous = tick
    if occupancy:
        raise ValueError("unbalanced interval population")
    return dict(area=area, mean=area / (end - begin), peak=peak)


def unique_line_overlap(intervals, begin, end):
    lines = {}
    for line, first, last in intervals:
        if last < first:
            raise ValueError("inverted unique-line lifetime")
        lines.setdefault(line, []).append((first, last))
    combined = []
    for spans in lines.values():
        spans.sort()
        first, last = spans[0]
        for following, finish in spans[1:]:
            if following <= last:
                last = max(last, finish)
            else:
                combined.append((first, last))
                first, last = following, finish
        combined.append((first, last))
    return overlap_summary(combined, begin, end)


def bind_native_service_groups(rows):
    leaders = {}
    for row in rows:
        if row["service_role"] != "leader" or row["native_fragments"] != 1:
            continue
        key = row["line"], row["native_response_tick"]
        leaders.setdefault(key, []).append(row)
    result = []
    for row in rows:
        row = dict(row)
        candidates = leaders.get((row["line"], row["native_response_tick"]), [])
        if row["native_fragments"] != 1:
            tier = "multi-fragment"
        elif not candidates:
            tier = "outside-observed-members"
        elif len(candidates) != 1:
            tier = "ambiguous"
        else:
            parent = candidates[0]
            hierarchy = parent.get("native_hierarchy") or {}
            l1, l2 = hierarchy.get("l1d", {}), hierarchy.get("l2", {})
            tier = ("uncore" if l1.get("tag_misses") and l2.get("tag_misses") else
                    "private-l2" if l1.get("tag_misses") and l2.get("hits") else
                    "local-l1" if l1.get("hits") else "unknown-native-hierarchy")
            row["native_parent_source_sequence"] = parent["source_sequence"]
        row["native_parent_tier"] = tier
        result.append(row)
    return result


def join_members(functional, native, iq, *, core):
    if len(functional) != len(native):
        raise ValueError(f"functional/native memory population mismatch: {len(functional)} != {len(native)}")
    sequences = [r["inst_seq_num"] for r in native]
    if len(set(sequences)) != len(sequences):
        raise ValueError("ambiguous core/sequence IQ identity across contexts")
    result = []
    for member, reference in zip(functional, sorted(native, key=lambda r: r["inst_seq_num"])):
        if reference["core_id"] != core:
            raise ValueError("native core mismatch")
        slot = iq.get((core, reference["inst_seq_num"]), {})
        if "pc" in slot and slot["pc"] != member["pc"]:
            raise ValueError(f"functional/native IQ PC mismatch at source {member['source_sequence']}")
        first = reference.get("native_first_admission_tick")
        last = reference.get("native_last_response_tick")
        accepted = reference["native_admission_count"]
        responses = reference["native_response_count"]
        duration = None
        if accepted and responses == accepted:
            if first is None or last is None or last < first:
                raise ValueError("missing/inverted native Ruby lifecycle")
            duration = last - first
        iq_duration = None
        if "insert" in slot and "release" in slot:
            iq_duration = slot["release"] - slot["insert"]
            if iq_duration < 0:
                raise ValueError("inverted native IQ lifetime")
        role = ("no-ruby" if not accepted else
                "merged" if reference.get("native_coalesced", 0) else
                "leader" if reference.get("native_hierarchy_request_count", 0) else "unknown")
        result.append(dict(member, native_sequence=reference["inst_seq_num"],
                           context=reference["thread_id"], service_role=role,
                           native_admission_tick=first if accepted else None,
                           native_response_tick=last if responses else None,
                           request_lifetime_ticks=duration,
                           iq_insert_tick=slot.get("insert"), iq_release_tick=slot.get("release"),
                           iq_lifetime_ticks=iq_duration,
                           first_issue_tick=slot.get("first_issue"),
                           last_issue_tick=slot.get("last_issue"),
                           issue_attempts=slot.get("issue_attempts"),
                           native_hierarchy=reference.get("native_hierarchy"),
                           native_fragments=accepted))
    return result


def read_functional_members(path, begin, end):
    result = []
    with Path(path).open("rb") as stream:
        header = stream.read(72)
        magic, version, size, record_size, _, records = struct.unpack_from("<8sIIIIQ", header)
        if magic != b"FSTRC01\0" or version != 7 or record_size != 64:
            raise ValueError("requires canonical v7 functional FST")
        if not 0 <= begin <= end <= records:
            raise ValueError("functional measurement bounds invalid")
        stream.seek(size + begin * record_size)
        for ordinal in range(begin, end):
            record = stream.read(64)
            if len(record) != 64:
                raise ValueError("truncated functional input")
            pc, address = struct.unpack_from("<QQ", record)
            width, flags, op = struct.unpack_from("<HHh", record, 48)
            # The native user reducer covers memory UOPs; retain loads/stores
            # together for the conserved order join before selecting loads.
            if op >= 0 and flags & (2 | 4 | 8):
                result.append(dict(source_sequence=ordinal, pc=pc, line=address // 64,
                                   flags=flags, size=width))
    return result


def load_native(path, core):
    commits, final = {}, {}
    all_commits = 0
    summary = None
    metadata = None
    with Path(path).open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["record"] == "metadata":
                if metadata is not None:
                    raise ValueError("duplicate native metadata")
                if (row.get("oracle_only") is not True or row.get("fst_input") is not False or
                    row.get("schema") != "taotrace-native-response-v8" or
                    row.get("lifecycle_tick_semantics") != "sequencer-acceptance-and-hit-callback-gem5-tick"):
                    raise ValueError("native metadata has unsupported lifecycle semantics")
                metadata = row
                continue
            if row["record"] == "summary":
                if summary is not None:
                    raise ValueError("duplicate native summary")
                summary = row
                continue
            if row["record"] == "commit":
                all_commits += 1
            if row["core_id"] != core or row["scope"] != "user":
                continue
            key = row["thread_id"], row["inst_seq_num"]
            if row["record"] == "commit":
                if key in commits:
                    raise ValueError("duplicate native commit")
                commits[key] = row
            elif row["record"] != "resolution":
                raise ValueError("unknown native lifecycle record")
            final[key] = row
    if not final.keys() <= commits.keys():
        raise ValueError("native resolution lacks committed member")
    if metadata is None:
        raise ValueError("native lifecycle metadata missing")
    if summary is None or summary["committed_memory_uops"] != all_commits:
        raise ValueError("native footer/commit population mismatch")
    return [final[key] for key in commits]


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(n=0, mean=None, p50=None, p95=None, p99=None, maximum=None)
    return dict(n=len(values), mean=sum(values) / len(values),
                p50=values[(len(values)-1)//2], p95=values[int((len(values)-1)*.95)],
                p99=values[int((len(values)-1)*.99)], maximum=values[-1])


def compare_members(native, model, *, period):
    """Pair already population-checked native members with committed model rows."""
    if period <= 0:
        raise ValueError("positive native clock period required")
    index = {}
    for row in model:
        key = row["source_sequence"]
        if key in index:
            raise ValueError("duplicate model source identity")
        index[key] = row
    result = []
    for member in native:
        if not member["flags"] & 2 or member["flags"] & (4 | 8):
            continue
        predicted = index.get(member["source_sequence"])
        row = dict(source_sequence=member["source_sequence"], pc=member["pc"],
                   line=member["line"], native_sequence=member["native_sequence"],
                   service_role=member["service_role"], model_available=predicted is not None)
        if predicted is None:
            result.append(row)
            continue
        if predicted["line"] != member["line"]:
            raise ValueError("functional/model physical line mismatch")
        row.update(native_iq_cycles=None if member["iq_lifetime_ticks"] is None else member["iq_lifetime_ticks"]/period,
                   native_request_cycles=None if member["request_lifetime_ticks"] is None else member["request_lifetime_ticks"]/period,
                   native_pre_admission_cycles=None if member["iq_insert_tick"] is None or member["native_admission_tick"] is None else (member["native_admission_tick"]-member["iq_insert_tick"])/period,
                   native_post_response_cycles=None if member["iq_release_tick"] is None or member["native_response_tick"] is None else (member["iq_release_tick"]-member["native_response_tick"])/period,
                   model_iq_cycles=predicted["iq_release"]-predicted["dispatch"],
                   model_request_cycles=predicted["post_parent_response"]-predicted["admission"],
                   model_pre_admission_cycles=predicted["admission"]-predicted["dispatch"],
                   model_post_response_cycles=predicted["iq_release"]-predicted["post_parent_response"],
                   model_private_level=predicted["private_level"])
        if any(v < 0 for k,v in row.items() if k.endswith("_cycles") and v is not None):
            raise ValueError("negative paired lifecycle phase")
        result.append(row)
    return result


def compare_files(source, native_members, model_edges, model_schema, model_output, output, core=0):
    source, output = Path(source), Path(output)
    native = bind_native_service_groups([json.loads(line) for line in Path(native_members).open()])
    fields = json.loads(Path(model_schema).read_text())["fields"]
    model = []
    with Path(model_edges).open() as stream:
        for values in csv.reader(stream):
            if len(values) != len(fields):
                raise ValueError("model lifecycle row width mismatch")
            row = dict(zip(fields, map(int, values)))
            if row["core"] != core:
                raise ValueError("unexpected model lifecycle core")
            model.append(row)
    clock = json.loads((source / "oracle" / f"task-clock-native-core{core}.json").read_text())
    period = clock["clock_period_ticks"]
    pairs = compare_members(native, model, period=period)
    paired_keys = {r["source_sequence"] for r in pairs if r["model_available"]}
    # Occupancy uses the exact same retired load subset on both providers.
    # Native multi-fragment Ruby intervals are aggregate UOP spans, not one
    # Sequencer-slot lease, and therefore remain outside this slot diagnostic.
    slot_keys = {r["source_sequence"] for r in native
                 if r["source_sequence"] in paired_keys and r["native_fragments"]==1}
    native_slots = [r for r in native if r["source_sequence"] in slot_keys]
    model_slots = [r for r in model if r["source_sequence"] in slot_keys]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".pairs.jsonl").open("w") as stream:
        for row in pairs:
            stream.write(json.dumps(row) + "\n")
    groups = {}
    for role in sorted({r["service_role"] for r in pairs}):
        selected = [r for r in pairs if r["service_role"]==role]
        groups[role] = dict(n=len(selected), model_missing=sum(not r["model_available"] for r in selected))
        for phase in ["iq", "request", "pre_admission", "post_response"]:
            for provider in ["native", "model"]:
                name = provider+"_"+phase+"_cycles"
                groups[role][name] = distribution(r.get(name) for r in selected)
    prediction = json.loads(Path(model_output).read_text())
    model_begin = prediction["totals"]["functional_warmup_barrier_cycles"]
    model_end = model_begin + prediction["time_accounting"]["elapsed_reference_cycles"]
    native_begin, native_end = clock["common_start_tick"], clock["common_end_tick"]
    occupancy = {}
    for name, intervals, begin, end, divisor in [
        ("native_iq", [(r["iq_insert_tick"],r["iq_release_tick"]) for r in native_slots if r["iq_insert_tick"] is not None and r["iq_release_tick"] is not None], native_begin,native_end,period),
        ("native_sequencer", [(r["native_admission_tick"],r["native_response_tick"]) for r in native_slots if r["request_lifetime_ticks"] is not None],native_begin,native_end,period),
        ("model_iq", [(r["dispatch"],r["iq_release"]) for r in model_slots],model_begin,model_end,1),
        ("model_sequencer", [(r["admission"],r["sequencer_release"]) for r in model_slots],model_begin,model_end,1),
    ]:
        value = overlap_summary(intervals, begin, end)
        value["area_cycles"] = value.pop("area") / divisor
        value["window_cycles"] = (end-begin)/divisor
        value["scope"] = "provided-retired-member-subset-in-provider-common-window"
        occupancy[name] = value
    for provider, intervals, begin, end, divisor in [
        ("native", [(r["line"],r["native_admission_tick"],r["native_response_tick"]) for r in native_slots
                    if r["service_role"]=="leader" and r["native_parent_tier"] in ("private-l2","uncore")],
         native_begin,native_end,period),
        ("model", [(r["line"],r["admission"],r["post_parent_response"]) for r in model_slots if r["private_level"]!=0],
         model_begin,model_end,1),
    ]:
        value = unique_line_overlap(intervals, begin, end)
        value["area_cycles"] = value.pop("area")/divisor
        value["window_cycles"] = (end-begin)/divisor
        value["scope"] = "provided-retired-load-demand-fill-lines-only"
        occupancy[provider+"_unique_miss_lines"] = value
    native_details = {}
    for role in sorted({r["service_role"] for r in native}):
        selected = [r for r in native if r["service_role"]==role]
        native_details[role] = {
            "iq_to_first_issue_cycles":distribution(None if r.get("first_issue_tick") is None or r["iq_insert_tick"] is None else (r["first_issue_tick"]-r["iq_insert_tick"])/period for r in selected),
            "first_issue_to_admission_cycles":distribution(None if r.get("first_issue_tick") is None or r["native_admission_tick"] is None else (r["native_admission_tick"]-r["first_issue_tick"])/period for r in selected),
            "issue_attempts":distribution(r.get("issue_attempts") for r in selected),
        }
    tiers = {tier:sum(r["native_parent_tier"]==tier for r in native) for tier in sorted({r["native_parent_tier"] for r in native})}
    summary = dict(schema="paired-load-lifecycle-diagnostic-v1", core=core,
                   native_clock_period_ticks=period, paired_loads=len(pairs),
                   model_missing=sum(not r["model_available"] for r in pairs),
                   model_outside_native_user_load_scope=sum(r["source_sequence"] not in paired_keys for r in model),
                   paired_single_fragment_slot_members=len(slot_keys),
                   distributions=groups, occupancy=occupancy,
                   native_issue_details=native_details, native_parent_tiers=tiers,
                   timing_input=False, full_queue_occupancy_certified=False,
                   causal_cpi_contribution_quantified=False)
    output.write_text(json.dumps(summary, indent=2))
    return summary


def extract_native(source, core, output):
    source, output = Path(source), Path(output)
    trace = source / "tao_trace"
    boundary = json.loads((trace / f"functional-boundary-core{core}.json").read_text())
    fst = trace / f"board.processor.switch{core}.core.tao_trace.tao_trace.records.micro.fst"
    functional = read_functional_members(fst, boundary["warmup_records"], boundary["total_records"])
    native = load_native(trace / "oracle" / f"native-response-core{core}.jsonl", core)
    iq = load_iq(source / "iq.log", {(core, r["inst_seq_num"]) for r in native})
    rows = join_members(functional, native, iq, core=core)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    summary = dict(schema="native-load-lifecycle-diagnostic-v1", core=core,
                   user_memory_uops=len(rows), iq_pc_verified=sum((core,r["native_sequence"]) in iq for r in rows),
                   iq_lifetime_missing=sum(r["iq_lifetime_ticks"] is None for r in rows),
                   ruby_lifetime_missing=sum(r["request_lifetime_ticks"] is None for r in rows),
                   roles={role:sum(r["service_role"]==role for r in rows) for role in {r["service_role"] for r in rows}})
    output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--core", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-members", type=Path)
    parser.add_argument("--model-edges", type=Path)
    parser.add_argument("--model-schema", type=Path)
    parser.add_argument("--model-output", type=Path)
    args = parser.parse_args()
    comparison = [args.native_members, args.model_edges, args.model_schema, args.model_output]
    if any(comparison) and not all(comparison):
        parser.error("comparison requires native-members, model-edges, model-schema and model-output")
    if all(comparison):
        result = compare_files(args.source, *comparison, args.output, args.core)
    else:
        result = extract_native(args.source, args.core, args.output)
    print(json.dumps(result, indent=2))
