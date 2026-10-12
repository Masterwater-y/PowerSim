#!/usr/bin/env python3
"""Validate functional synchronization facts; oracle time never becomes input.

This gate certifies the captured region/barrier protocol, not OS scheduling,
wait durations, physical PMU identity, or FastSim prediction accuracy.
"""
import argparse
import bisect
import json
import mmap
from pathlib import Path
import struct


def json_rows(path):
    with path.open() as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path}: row is not an object")
                yield row


def uint(row, key, positive=False):
    value = row[key]
    if type(value) is not int or value < (1 if positive else 0) or value >= 1 << 64:
        raise ValueError(f"invalid unsigned field {key}: {value!r}")
    return value


def validate(trace_dir: Path, allow_truncated=False):
    trace_dir = Path(trace_dir)
    errors, rows, metas, sizes, populations, boundaries = [], {}, [], {}, {}, []
    result = dict(valid=False, complete=False, events=0, regions=0,
                  barriers=0, truncated_regions=0, errors=errors)
    try:
        paths = sorted(trace_dir.glob("functional-sync-metadata-core*.json"))
        if not paths:
            raise ValueError("no functional synchronization metadata")
        for path in paths:
            meta = json.loads(path.read_text())
            if meta["schema"] != "fastsim-functional-sync-metadata-v1":
                raise ValueError("unknown metadata schema")
            core = uint(meta, "core_id")
            if core in sizes:
                raise ValueError("duplicate core metadata")
            if meta["initial_context"] != "empty":
                errors.append(f"core {core}: unknown initial synchronization context")
            source_name = meta["source_file"]
            if not isinstance(source_name, str) or Path(source_name).name != source_name:
                raise ValueError("source_file must be a filename within trace directory")
            source = trace_dir / source_name
            count = uint(meta, "source_record_count")
            marker = uint(meta, "marker_pc", positive=True)
            boundary = json.loads((trace_dir / f"functional-boundary-core{core}.json").read_text())
            if boundary.get("schema") != "tcsim-functional-boundary-v1":
                raise ValueError("unknown functional boundary schema")
            warmup = uint(boundary, "warmup_records")
            if uint(boundary, "core_id") != core or uint(boundary, "total_records") != count or \
                    warmup + uint(boundary, "measurement_records") != count or \
                    uint(boundary, "participant_count") != len(paths):
                raise ValueError("functional boundary/source population or participant mismatch")
            if boundary["measurement_policy"] != "first-core-target-common-end-v1":
                raise ValueError("unsupported functional boundary policy")
            closed = boundary["measurement_closed"]
            if type(closed) is not bool or closed != (meta["end_reason"] == "first_core_target_common_end"):
                raise ValueError("sync metadata/functional boundary end mismatch")
            boundaries.append(boundary)
            transitions = []
            if count:
                asmap = Path(str(source) + ".asmap").read_bytes()
                ah = struct.unpack_from("<8sIIIIQQQ", asmap)
                if ah[:6] != (b"FSTASM1\0", 1, 48, 16, core, count) or \
                        not ah[6] or len(asmap) != 48 + 16 * ah[6] or ah[7]:
                    raise ValueError("invalid source ASMAP header/population")
                transitions = [struct.unpack_from("<QQ", asmap, 48 + 16 * i) for i in range(ah[6])]
                if transitions[0][0] != 0 or any(o >= count or a == 0 for o, a in transitions) or \
                        any(transitions[i][0] <= transitions[i-1][0] for i in range(1, len(transitions))):
                    raise ValueError("invalid ASMAP transitions")
            transition_ordinals = [t[0] for t in transitions]
            metas.append(meta)
            populations[core] = count
            with source.open("rb") as f:
                header = f.read(72)
                if len(header) != 72:
                    raise ValueError(f"core {core}: truncated FST header")
                magic, version, hsize, rsize, fst_core, fst_count, features, *reserved = \
                    struct.unpack("<8sIIIIQQ4Q", header)
                if (magic, version, hsize, rsize, fst_core, fst_count) != \
                        (b"FSTRC01\0", 7, 72, 64, core, count):
                    raise ValueError(f"core {core}: FST header/source population mismatch")
                if source.stat().st_size < 72 + 64 * count:
                    raise ValueError(f"core {core}: truncated FST records")
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                    previous, anchors, n = -1, set(), 0
                    for row in json_rows(trace_dir / f"functional-sync-core{core}.jsonl"):
                        if row["schema"] != "fastsim-functional-sync-v1" or \
                                row["boundary"] != "after_record":
                            raise ValueError("unknown event schema or boundary")
                        if uint(row, "core_id") != core:
                            raise ValueError("event/metadata core identity mismatch")
                        ordinal = uint(row, "record_ordinal")
                        if ordinal >= count or ordinal <= previous:
                            raise ValueError(f"core {core}: invalid record ordinal {ordinal}")
                        previous = ordinal
                        pc = struct.unpack_from("<Q", mapping, 72 + ordinal * 64)[0]
                        flags = struct.unpack_from("<H", mapping, 72 + ordinal * 64 + 50)[0]
                        op_class = struct.unpack_from("<h", mapping, 72 + ordinal * 64 + 52)[0]
                        if pc != marker or uint(row, "pc") != marker:
                            raise ValueError(f"core {core}: anchor PC does not match marker")
                        if not flags & 1 or flags & (2 | 4 | 8) or op_class < 0:
                            raise ValueError("marker anchor is not a retired non-memory instruction")
                        asid = transitions[bisect.bisect_right(transition_ordinals, ordinal) - 1][1]
                        if row["address_space_id"] != asid:
                            raise ValueError("event address space disagrees with FST ASMAP")
                        expected_phase = "warmup" if ordinal < warmup else "measurement"
                        if row["phase"] != expected_phase:
                            raise ValueError("event phase disagrees with functional record boundary")
                        key = (core, ordinal)
                        if key in rows:
                            raise ValueError("duplicate event anchor")
                        for field in ("address_space_id", "thread_id", "region_instance",
                                      "sync_object_id", "team_size"):
                            uint(row, field, positive=True)
                        for field in ("generation", "member_index", "event_kind"):
                            uint(row, field)
                        if row["member_index"] >= row["team_size"] or \
                                row["phase"] not in ("warmup", "measurement"):
                            raise ValueError("invalid member or phase")
                        if row["event_kind"] not in range(1, 9):
                            errors.append(f"unsupported synchronization event {row['event_kind']}")
                        if any(k in row for k in ("commit_tick", "timestamp", "wait_cycles")):
                            raise ValueError("timing oracle leaked into functional sidecar")
                        rows[key] = row
                        anchors.add(ordinal)
                        n += 1
                    if n != uint(meta, "event_count"):
                        raise ValueError("sidecar/metadata event population mismatch")
                    # Search in C via mmap.find, checking only the PC column of hot records.
                    needle, position, observed = struct.pack("<Q", marker), 72, set()
                    hot_end = 72 + count * 64
                    while True:
                        position = mapping.find(needle, position, hot_end)
                        if position < 0:
                            break
                        if (position - 72) % 64 == 0:
                            observed.add((position - 72) // 64)
                        position += 1
                    if observed != anchors:
                        raise ValueError("FST marker population differs from sidecar anchors")
            sizes[core] = n

        observations = list(json_rows(trace_dir / "oracle/functional-sync-observation.jsonl"))
        sequence, seen, last_ordinals, ordered = -1, set(), {}, []
        last_tick = -1
        for observation in observations:
            if observation["schema"] != "fastsim-sync-observation-v1":
                raise ValueError("unknown observation schema")
            seq = uint(observation, "sequence")
            tick = uint(observation, "commit_tick")
            if seq != sequence + 1 or tick < last_tick:
                raise ValueError("invalid global observation sequence/tick")
            sequence, last_tick = seq, tick
            key = (uint(observation, "core_id"), uint(observation, "record_ordinal"))
            if key not in rows or key in seen:
                raise ValueError("observation/event association missing or duplicate")
            if key[1] <= last_ordinals.get(key[0], -1):
                raise ValueError("observation reverses per-core ordinal")
            last_ordinals[key[0]] = key[1]
            seen.add(key)
            ordered.append(rows[key])
        if seen != set(rows):
            raise ValueError("observation/event population mismatch")

        regions, active, barrier_objects = {}, {}, {}
        for row in ordered:
            asid, rid = row["address_space_id"], row["region_instance"]
            key, member, tid, kind = (asid, rid), row["member_index"], row["thread_id"], row["event_kind"]
            n = row["team_size"]
            if kind == 1:
                if key in regions or member != 0:
                    errors.append("duplicate region begin or non-master parallel_begin")
                    continue
                regions[key] = dict(size=n, members={}, ended=set(), joined=False, barriers={},
                                    pending={}, last_generation={}, final_generation=None, master=tid)
            region = regions.get(key)
            if region is None:
                errors.append(f"region {key}: missing parallel_begin/initial context")
                continue
            if region["size"] != n:
                errors.append("inconsistent team size")
            if kind == 3:
                if member == 0 and tid != region["master"]:
                    errors.append("master identity differs from parallel_begin")
                if member in region["members"] or region["joined"]:
                    errors.append("duplicate/late implicit task begin")
                if tid in region["members"].values():
                    errors.append("thread identity reused for another team member")
                if (asid, tid, rid) in active:
                    errors.append("duplicate active task identity")
                region["members"][member] = tid
                active[(asid, tid, rid)] = member
            elif kind in (2, 4, 5, 6, 7, 8):
                if region["members"].get(member) != tid:
                    errors.append("member/thread identity mismatch or task not started")
            if kind == 4:
                if member in region["pending"]:
                    errors.append("implicit task end with outstanding barrier")
                if member in region["ended"]:
                    errors.append("duplicate implicit task end")
                region["ended"].add(member)
                active.pop((asid, tid, rid), None)
            if kind in (5, 6, 7, 8):
                final = kind in (7, 8)
                bkey = (row["sync_object_id"], row["generation"])
                if (asid, rid) in barrier_objects and barrier_objects[(asid, rid)] != bkey[0]:
                    errors.append("team barrier object identity changed")
                barrier_objects[(asid, rid)] = bkey[0]
                barrier = region["barriers"].setdefault(
                    bkey, dict(final=final, arrived=set(), departed=set()))
                if barrier["final"] != final:
                    errors.append("barrier generation mixes final and non-final")
                if kind in (5, 7):
                    if member in region["pending"]:
                        errors.append("barrier arrival with outstanding prior barrier")
                    last = region["last_generation"].get(member)
                    if row["generation"] % 8 or (last is not None and row["generation"] != last + 8):
                        errors.append("invalid barrier generation progression")
                    region["pending"][member] = bkey
                    if final:
                        fg = region["final_generation"]
                        if fg is not None and fg != bkey:
                            errors.append("multiple final barrier generations")
                        region["final_generation"] = bkey
                    if member in barrier["arrived"]:
                        errors.append("duplicate barrier arrival")
                    if final and member not in region["ended"]:
                        errors.append("final barrier before implicit task end")
                    if not final and member in region["ended"]:
                        errors.append("normal barrier after implicit task end")
                    barrier["arrived"].add(member)
                else:
                    if region["pending"].get(member) != bkey:
                        errors.append("departure does not match member's outstanding barrier")
                    else:
                        region["pending"].pop(member)
                    region["last_generation"][member] = row["generation"]
                    if len(barrier["arrived"]) != n:
                        errors.append("barrier departure before all participants arrived")
                    if member in barrier["departed"] or member not in barrier["arrived"]:
                        errors.append("duplicate/unmatched barrier departure")
                    barrier["departed"].add(member)
            if kind == 2:
                final_barriers = [b for b in region["barriers"].values() if b["final"]]
                if member != 0 or region["joined"] or len(region["ended"]) != n or \
                        not final_barriers or not any(len(b["arrived"]) == n and 0 in b["departed"]
                                                      for b in final_barriers):
                    errors.append("parallel_end before all final arrivals/master departure")
                region["joined"] = True

        truncated = 0
        for region in regions.values():
            if not region["joined"] or len(region["members"]) != region["size"] or \
                    any(len(b["departed"]) != region["size"] for b in region["barriers"].values()):
                truncated += 1
        if not rows:
            errors.append("no synchronization events observed")
        if len({(b["measurement_closed"], b["common_end_tick"]) for b in boundaries}) != 1:
            errors.append("participant functional boundaries do not share a common end")
        boundary_truncation = all(m["end_reason"] == "first_core_target_common_end" for m in metas)
        if truncated and not (allow_truncated and boundary_truncation):
            errors.append("unclosed regions without permitted common-end truncation")
        result.update(events=len(rows), regions=len(regions),
                      barriers=sum(len(r["barriers"]) for r in regions.values()),
                      truncated_regions=truncated, events_by_core=sizes,
                      source_records_by_core=populations,
                      complete=not errors and not truncated, valid=not errors)
    except (ValueError, KeyError, TypeError, OSError, struct.error) as exc:
        errors.append(str(exc))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-truncated", action="store_true")
    args = parser.parse_args()
    result = validate(args.trace_dir, args.allow_truncated)
    body = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    print(body, end="")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
