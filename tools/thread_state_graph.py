"""Validate sparse OS ownership facts and build an oracle-free source DAG.

Legacy scheduler observations remain a declared context-boundary proxy. A
wait marker records intent, wakeup records runnable eligibility, and only a
switch-in records the next on-CPU owner. No log/global timestamp order is used.
"""
from collections import defaultdict
import hashlib
import heapq
import json
from pathlib import Path
import re
import struct
from task_clock_initial import effective_initial_state, validate_initial_resolution

EVENT_SCHEMA = "fastsim-thread-state-v1"
META_SCHEMA = "fastsim-thread-state-metadata-v1"
ACCOUNTING_CONTRACT = "task-oncpu-switch-entry-v1"
TASK_CLOCK_CONTRACT = "task-lifecycle-basecpu-active-v1"
TASK_CLOCK_DOMAIN = "gem5-basecpu-active-cycles-v1"
TASK_CLOCK_PCS = {"account_out": 0xffffffff81517a0a, "account_in": 0xffffffff81067eef}
COMMON_CUT_ID = "first-core-target-common-end-v1"
CERTIFICATE_FIELDS = ("accounting_contract", "kernel_stream_complete", "common_cut_complete",
                      "owner_continuity_to_common_cut", "common_cut_id")
KINDS = {"identity": 1, "wait_attempt": 2, "wait_return": 3,
         "wake_attempt": 4, "wake_return": 5, "sched_switch": 6, "wakeup": 7, "account_out": 8, "account_in": 9}
VERIFIED_WAKE_CAUSES = {"kernel-futex-call-path", "verified-futex-call-path"}
TIMING_KEYS = {"cycle", "cycles", "active_counter", "tick", "ticks", "timestamp", "time_seconds", "duration",
               "wall_time", "wall_time_seconds", "elapsed_time_seconds",
               "frequency_hz", "simFreq"}


def load_capture_evidence(trace_dir):
    """Validate current inputs and bind a cached oracle audit to this capture.

    The fingerprint covers functional facts, oracle observations, source
    headers and event records. Source size/mtime/ctime invalidate caches for
    changes elsewhere in a large FST, including non-event macro flags. This
    avoids hashing entire multi-GB streams and is not a full-content integrity
    certificate. Copying/touching sources may require a fresh offline audit.
    This provenance stays in oracle output; it never enters a replay plan.
    """
    trace_dir = Path(trace_dir)
    metadata = sorted((json.loads(path.read_text()) for path in
                       trace_dir.glob("thread-state-metadata-core*.json")),
                      key=lambda row: row["core_id"])
    expected = {f"thread-state-core{meta['core_id']}.jsonl" for meta in metadata}
    if {path.name for path in trace_dir.glob("thread-state-core*.jsonl")} != expected:
        raise ValueError("functional thread-state sidecars do not match metadata participants")
    rows = [json.loads(line) for meta in metadata
            for line in (trace_dir / f"thread-state-core{meta['core_id']}.jsonl").read_text().splitlines()
            if line.strip()]
    graph = build_graph(rows, metadata)
    functional = json.dumps({"metadata": metadata, "events": rows}, sort_keys=True,
                            separators=(",", ":")).encode()
    sources = []
    for meta in metadata:
        path = trace_dir / meta["source_file"]
        validate_initial_resolution(path,meta,rows)
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            header = handle.read(72)
            if (len(header) != 72 or struct.unpack("<8sIIIIQQ4Q", header)[:6] !=
                    (b"FSTRC01\0", 7, 72, 64, meta["core_id"], meta["source_record_count"])
                    or before.st_size < 72 + 64 * meta["source_record_count"]):
                raise ValueError("FST header/source population mismatch")
            digest.update(header)
            for event in rows:
                if event["core_id"] != meta["core_id"]:
                    continue
                handle.seek(72 + 64 * event["record_ordinal"])
                record = handle.read(64)
                if len(record) != 64 or struct.unpack_from("<Q", record)[0] != event["pc"]:
                    raise ValueError("thread-state event PC/source anchor mismatch")
                flags = struct.unpack_from("<H", record, 50)[0]
                if event["boundary"] == "after_record" and not (flags & 1 and (not flags & 1024 or flags & 2048)):
                    raise ValueError("after-record event must anchor a retired macro completion")
                digest.update(struct.pack("<Q", event["record_ordinal"]))
                digest.update(record)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("FST source changed while validating audit provenance")
        sources.append({"core_id":meta["core_id"], "source_file":meta["source_file"],
                        "source_record_count":meta["source_record_count"], "size_bytes":before.st_size,
                        "mtime_ns":before.st_mtime_ns, "ctime_ns":before.st_ctime_ns,
                        "header_and_event_records_sha256":digest.hexdigest()})
    provenance = {"schema":"fastsim-thread-state-audit-provenance-v1",
                  "functional_sha256":hashlib.sha256(functional).hexdigest(),
                  "observations_sha256":hashlib.sha256((trace_dir / "oracle/thread-state-observation.jsonl").read_bytes()).hexdigest(),
                  "sources":sources}
    return {"metadata":metadata, "rows":rows, "graph":graph, "provenance":provenance}


def _functional_only(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if (key in TIMING_KEYS or key.endswith(("_tick", "_ticks", "_cycle", "_cycles"))
                    or "timestamp" in key or key == "oracle"):
                raise ValueError(f"timing/oracle pollution in functional field {key}")
            _functional_only(child)
    elif isinstance(value, list):
        for child in value:
            _functional_only(child)


def _integer(row, field, *, minimum=0, signed=False):
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{row.get('event_id', 'metadata')}: invalid integer {field}")
    lower = -(1 << 63) if signed else minimum
    upper = (1 << 63)-1 if signed else (1 << 64)-1
    if value < lower or value > upper:
        raise ValueError(f"{field} outside supported range")
    return value


def _topological_order(events, deps):
    successors = [[] for _ in events]
    remaining = [len(parents) for parents in deps]
    for index, parents in enumerate(deps):
        for parent in parents:
            if events[index]["phase"] == "warmup" and events[parent]["phase"] != "warmup":
                raise ValueError("reverse measurement-to-warmup phase dependency")
            successors[parent].append(index)
    ready = [index for index, count in enumerate(remaining) if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        index = heapq.heappop(ready)
        order.append(index)
        for child in successors[index]:
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(events):
        raise ValueError("thread-state dependency cycle")
    return order


def build_graph(rows, metadata):
    """Return validated events, immutable-plan operands and causal predecessors.

    Structural contradictions raise ValueError. Incomplete observed lifecycles
    and unsupported attribution produce explicit diagnostic coverage/status.
    Certified ownership establishes a recorded-owner proxy only. Independent
    native counter evidence is audited separately and never enters the plan.
    """
    if not metadata:
        raise ValueError("missing thread-state metadata")
    _functional_only(rows)
    _functional_only(metadata)
    metas = sorted(metadata, key=lambda m: m.get("core_id", -1))
    if [m.get("core_id") for m in metas] != list(range(len(metas))):
        raise ValueError("thread-state plan requires dense physical core metadata")
    per_core = defaultdict(list)
    event_ids, anchors = set(), set()
    for row in rows:
        if row.get("schema") != EVENT_SCHEMA:
            raise ValueError("unsupported thread-state event schema")
        core = _integer(row, "core_id")
        ordinal = _integer(row, "record_ordinal")
        _integer(row, "pc")
        match = re.fullmatch(r"core(\d+):e(\d+)", str(row.get("event_id", "")))
        if not match or int(match.group(1)) != core or row["event_id"] in event_ids:
            raise ValueError("missing, duplicate or mismatched thread-state event ID")
        event_ids.add(row["event_id"])
        if core >= len(metas):
            raise ValueError("event refers to missing physical core")
        kind = row.get("event_kind")
        if kind not in (*KINDS, "initial_owner"):
            raise ValueError("unsupported thread-state event kind")
        expected_boundary = "before_record" if kind == "initial_owner" else "after_record"
        if row.get("boundary") != expected_boundary:
            raise ValueError("event at unsupported record boundary")
        anchor = core, ordinal, expected_boundary
        if anchor in anchors:
            raise ValueError("ambiguous duplicate source record boundary")
        anchors.add(anchor)
        per_core[core].append(dict(row))
    diagnostics, cores = [], []
    for m in metas:
        if m.get("schema") != META_SCHEMA:
            raise ValueError("unsupported thread-state metadata schema")
        core = _integer(m, "core_id")
        total = _integer(m, "source_record_count", minimum=1)
        if _integer(m, "total_records", minimum=1) != total:
            raise ValueError("metadata total/source record count mismatch")
        warm = _integer(m, "warmup_records")
        measured = _integer(m, "measurement_records")
        source_name = m.get("source_file")
        if (warm + measured != total or not isinstance(source_name, str)
                or len(source_name) <= 4 or not source_name.endswith(".fst")
                or any(part in source_name for part in ("/", "\\", ".."))
                or any(ord(character) < 32 or ord(character) == 127 for character in source_name)):
            raise ValueError("metadata phase/source record conservation failed")
        if _integer(m, "event_count") != len(per_core[core]):
            raise ValueError("metadata event count mismatch")
        stream = sorted(per_core[core], key=lambda e: (e["record_ordinal"], e["boundary"] != "before_record"))
        snapshots = [e for e in stream if e["event_kind"] == "initial_owner"]
        if (m.get("initial_owner") is not True or len(snapshots) != 1
                or snapshots[0]["record_ordinal"] != 0
                or snapshots[0].get("state") != "RUNNING"):
            raise ValueError(f"core{core}: missing or invalid initial owner snapshot")
        for e in stream:
            ordinal = e["record_ordinal"]
            if ordinal >= total or e.get("phase") != ("warmup" if ordinal < warm else "measurement"):
                raise ValueError("event ordinal/phase outside retained source bounds")
        per_core[core] = stream
        cores.append({"core_id": core, "total_records": total, "warmup_records": warm,
                      "measurement_records": measured,
                      "initial_owner": _integer(snapshots[0], "os_tid")})
        certified = any(field in m for field in CERTIFICATE_FIELDS)
        if certified:
            if (m.get("accounting_contract") != ACCOUNTING_CONTRACT
                    or m.get("common_cut_id") != COMMON_CUT_ID
                    or any(m.get(field) is not True for field in CERTIFICATE_FIELDS[1:4])):
                raise ValueError(f"core{core}: incomplete or incompatible accounting/common-cut certificate")
            if any(field not in m for field in ("read_failures", "identity_failures", "unretained_hook_events")):
                raise ValueError(f"core{core}: accounting certificate lacks explicit collection failure counters")
        cores[-1]["owner_continuity_to_common_cut"] = certified
        for field in ("read_failures", "identity_failures", "unretained_hook_events"):
            failures = m.get(field, 0)
            if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
                raise ValueError(f"core{core}: invalid nonnegative integer {field}={failures!r}")
            if failures:
                raise ValueError(f"core{core}: incomplete thread-state collection ({field}={failures})")
        if not certified:
            diagnostics.append(f"core{core}:partial-or-proxy-coverage")
    events = [e for core in range(len(metas)) for e in per_core[core]
              if e["event_kind"] != "initial_owner"]
    index_by_id = {e["event_id"]: index for index, e in enumerate(events)}
    deps = [set() for _ in events]
    streams, tasks, lifetimes, runtime_bindings = defaultdict(list), {}, {}, {}
    owner_cores, waits, wakes, returns = defaultdict(set), {}, {}, {}
    wake_source_owners = {}
    normalized = {}
    summary = {"schema": "fastsim-thread-state-validation-v1", "valid": True,
               "active_status": "unavailable-context-boundary-proxy",
               "state_scope_status": "proxy", "strict_active_scope": False,
               "ownership_policy": "fixed-physical-core-recorded-owner-v1",
               "events": len(events), "cores": len(cores), "blocked_switches": 0,
               "runnable_switches": 0, "unknown_switches": 0,
               "unknown_wake_causes": 0, "known_waiter_wakes": 0,
               "proven_block_wake_admissions": 0,
               "known_causal_wakes": 0, "pending_wait_requests": 0,
               "wait_returns": {"normal": 0, "eagain": 0, "enosys": 0, "unsupported": 0}}
    certified = all(core["owner_continuity_to_common_cut"] for core in cores)
    summary.update(accounting_contract=ACCOUNTING_CONTRACT if certified else None,
                   perf_equivalence="unverified", ownership_status="complete" if certified else "proxy",
                   common_cut_id=COMMON_CUT_ID if certified else None)
    # An ownership certificate establishes no independent active counter.
    summary.update(task_clock_contract=None, task_clock_counter_domain=None, task_clock_cut_count=0)
    clock_present = ["task_clock_contract" in m for m in metas]
    if any(clock_present) and not all(clock_present):
        raise ValueError("incomplete task clock participants")
    if all(clock_present):
        for m, core in zip(metas, cores):
            if (m.get("task_clock_contract") != TASK_CLOCK_CONTRACT or
                    m.get("task_clock_counter_domain") != TASK_CLOCK_DOMAIN):
                raise ValueError("unsupported task clock contract/domain")
            for field in ("task_clock_initial_known", "task_clock_initial_enabled", "task_clock_common_cut_complete"):
                if not isinstance(m.get(field), bool):
                    raise ValueError("missing functional task clock state: " + field)
                core[field] = m[field]
            core["task_clock_initial_pid"] = _integer(m, "task_clock_initial_pid")
            if m["task_clock_initial_known"] and m["task_clock_initial_enabled"] and m["task_clock_initial_pid"] != core["initial_owner"]:
                raise ValueError("initial task clock eligibility differs from functional owner")
            if m["task_clock_initial_enabled"] and not m["task_clock_initial_known"]:
                raise ValueError("enabled task clock requires known initial eligibility")
            effective=effective_initial_state(m,per_core[core["core_id"]])
            for field in ("known","enabled","pid"):
                core["task_clock_initial_"+field]=effective[field]
        summary.update(task_clock_contract=TASK_CLOCK_CONTRACT, task_clock_counter_domain=TASK_CLOCK_DOMAIN,
                       active_status="unavailable-native-counter-audit", state_scope_status="task-lifecycle")

    def bind_lifetime(pid, task, owner_core=None):
        # PID 0 names one idle task per physical CPU, so it is not a global
        # lifetime identity. Other numeric PID reuse cannot fit this plan ABI.
        if pid and pid in lifetimes and lifetimes[pid] != task:
            raise ValueError("TID reuse or contradictory task lifetime requires a new plan ABI")
        if pid:
            lifetimes[pid] = task
            if owner_core is not None:
                owner_cores[pid].add(owner_core)

    for core, stream in per_core.items():
        first = stream[0]
        owner = _integer(first, "os_tid")
        owner_event, owner_ordinal = first["event_id"], first["record_ordinal"]
        bind_lifetime(owner, _integer(first, "task_id", minimum=1), core)
        previous = None
        clock_enabled = cores[core].get("task_clock_initial_enabled", False)
        clock_known = cores[core].get("task_clock_initial_known", False)
        clock_pid = cores[core].get("task_clock_initial_pid", 0)
        clock_pending = None
        for e in stream[1:]:
            i = index_by_id[e["event_id"]]
            streams[core].append(i)
            if previous is not None:
                deps[i].add(previous)
            previous = i
            kind = e["event_kind"]
            e["kind"] = KINDS[kind]
            if kind in ("identity", "wait_attempt", "wait_return", "wake_attempt", "wake_return"):
                tid = _integer(e, "os_tid", minimum=1)
                tgid = _integer(e, "os_tgid", minimum=1)
                runtime = _integer(e, "runtime_thread_id")
                bind_lifetime(tid, _integer(e, "task_id", minimum=1), core)
                if owner != tid:
                    raise ValueError(
                        f"{e['event_id']} core={core} record_ordinal={e['record_ordinal']}: "
                        f"runtime marker task is not the recorded CPU owner "
                        f"(expected_pid={owner}, actual_pid={tid}, kind={kind}, "
                        f"owner_event={owner_event}, owner_ordinal={owner_ordinal})")
                if kind == "identity":
                    task = (tgid, runtime)
                    if (tid in tasks and tasks[tid] != task
                            or task in runtime_bindings and runtime_bindings[task] != tid):
                        raise ValueError("conflicting runtime/OS task identity")
                    tasks[tid] = task
                    runtime_bindings[task] = tid
                    e["args"] = [tid, tgid, runtime]
                else:
                    if tasks.get(tid) != (tgid, runtime):
                        raise ValueError("wait/wake marker lacks matching runtime identity")
                    request = _integer(e, "request_id", minimum=1)
                    _integer(e, "sync_object_id", minimum=1)
                    detail = _integer(e, "detail", signed=kind.endswith("return"))
                    key = tid, request
                    e["args"] = [tid, request, detail]
                    if kind in ("wait_attempt", "wake_attempt"):
                        collection = waits if kind == "wait_attempt" else wakes
                        if key in collection:
                            raise ValueError("duplicate wait/wake request ID")
                        collection[key] = i
                    else:
                        returns[i] = key
            elif kind in ("account_out", "account_in"):
                if not all(clock_present) or e["pc"] != TASK_CLOCK_PCS[kind]:
                    raise ValueError("task clock cut lacks matching contract/PC")
                prev, nxt = _integer(e, "prev_pid"), _integer(e, "next_pid")
                bind_lifetime(prev, _integer(e, "prev_task_id", minimum=1), core)
                bind_lifetime(nxt, _integer(e, "next_task_id", minimum=1), core)
                if prev == nxt:
                    raise ValueError("same-task scheduling must bypass accounting cuts")
                if kind == "account_out":
                    if clock_pending is not None or (clock_known and (not clock_enabled or clock_pid != prev)) or owner != prev:
                        raise ValueError("unpaired or mismatched account_out")
                    clock_pending = (prev, nxt)
                    clock_enabled, clock_known = False, True
                else:
                    if clock_pending != (prev, nxt) or clock_enabled or owner != nxt:
                        raise ValueError("unpaired or mismatched account_in")
                    clock_pending = None
                    clock_enabled, clock_known, clock_pid = True, True, nxt
                e["args"] = [prev, nxt, 0]
                summary["task_clock_cut_count"] += 1
            elif kind == "sched_switch":
                prev, nxt = _integer(e, "prev_pid"), _integer(e, "next_pid")
                bind_lifetime(prev, _integer(e, "prev_task_id", minimum=1), core)
                bind_lifetime(nxt, _integer(e, "next_task_id", minimum=1), core)
                if all(clock_present) and prev != nxt and clock_pending != (prev, nxt):
                    raise ValueError("scheduler transition lacks matching account_out")
                if prev != owner:
                    raise ValueError(
                        f"{e['event_id']} core={core} record_ordinal={e['record_ordinal']}: "
                        f"scheduler previous PID contradicts recorded CPU owner "
                        f"(expected_pid={owner}, actual_pid={prev}, next_pid={nxt}, "
                        f"owner_event={owner_event}, owner_ordinal={owner_ordinal})")
                if not isinstance(e.get("preempt"), bool):
                    raise ValueError("scheduler preempt flag must be boolean")
                raw = _integer(e, "raw_state")
                if e["preempt"] or raw == 0:
                    state = 1
                elif e.get("state_status") in ("state-at-switch-hook", "verified-dequeue-sleep") and e.get("state_can_race_wakeup") is False:
                    state = 2
                else:
                    state = 3
                normalized[i] = state
                summary[{1: "runnable_switches", 2: "blocked_switches", 3: "unknown_switches"}[state]] += 1
                e["args"] = [prev, nxt, state]
                owner = nxt
                owner_event, owner_ordinal = e["event_id"], e["record_ordinal"]
            else:
                target = _integer(e, "target_pid", minimum=1)
                bind_lifetime(target, _integer(e, "target_task_id", minimum=1))
                request = e.get("request_id")
                if request is not None:
                    _integer(e, "request_id", minimum=1)
                e["args"] = [target, request or 0, 0]
                wake_source_owners[i] = owner
    for tid, physical in owner_cores.items():
        if len(physical) > 1:
            raise ValueError(f"task{tid}: migration across physical cores lacks a certified ownership order")

    closed_waits, closed_wakes = set(), set()
    for i, key in returns.items():
        e = events[i]
        is_wait = e["event_kind"] == "wait_return"
        attempts, closed = (waits, closed_waits) if is_wait else (wakes, closed_wakes)
        attempt = attempts.get(key)
        if attempt is None or key in closed:
            raise ValueError("return without unique matching wait/wake attempt")
        original = events[attempt]
        if e.get("attempt_event_id", original["event_id"]) != original["event_id"]:
            raise ValueError("return attempt event ID mismatch")
        if original["sync_object_id"] != e["sync_object_id"]:
            raise ValueError("return object differs from matching attempt")
        if original["core_id"] == e["core_id"] and original["record_ordinal"] >= e["record_ordinal"]:
            raise ValueError("return precedes attempt in source order")
        deps[i].add(attempt)
        closed.add(key)
        if is_wait:
            result = e["detail"]
            outcome = {0: "normal", -11: "eagain", -38: "enosys"}.get(result, "unsupported")
            summary["wait_returns"][outcome] += 1
            if outcome == "unsupported":
                diagnostics.append(f"{e['event_id']}:unsupported-wait-return={result}")
            if outcome in ("eagain", "enosys"):
                for switch_i in streams[e["core_id"]]:
                    transition = events[switch_i]
                    if (transition["event_kind"] == "sched_switch"
                            and transition["prev_pid"] == key[0] and normalized[switch_i] == 2
                            and original["record_ordinal"] < transition["record_ordinal"] < e["record_ordinal"]):
                        raise ValueError("EAGAIN/ENOSYS wait falsely classified as blocked")

    for i, e in enumerate(events):
        if e["event_kind"] != "wakeup":
            continue
        wait_id, cause_id = e.get("wait_attempt_event_id"), e.get("wake_attempt_event_id")
        request, cause_request = e.get("request_id"), e.get("wake_request_id")
        waiter = None
        if wait_id is not None or request is not None:
            waiter = index_by_id.get(wait_id)
            if (waiter is None or events[waiter]["event_kind"] != "wait_attempt"
                    or (events[waiter]["os_tid"], events[waiter]["request_id"]) != (e["target_pid"], request)
                    or events[waiter]["sync_object_id"] != e.get("sync_object_id")):
                raise ValueError("wakeup target wait request/event/object mismatch")
            summary["known_waiter_wakes"] += 1
            waiting = events[waiter]
            # Select only a unique local switch-out / following switch-in
            # inside this request's source lifetime. Cross-core migration or
            # absent switch-out is diagnosed rather than guessed.
            return_events = [j for j, key in returns.items() if key == (waiting["os_tid"], waiting["request_id"])
                             and events[j]["event_kind"] == "wait_return"]
            limit = events[return_events[0]]["record_ordinal"] if return_events and events[return_events[0]]["core_id"] == waiting["core_id"] else metas[waiting["core_id"]]["source_record_count"]
            switches = [j for j in streams[waiting["core_id"]]
                        if events[j]["event_kind"] == "sched_switch"
                        and waiting["record_ordinal"] < events[j]["record_ordinal"] < limit]
            outs = [j for j in switches if events[j]["prev_pid"] == e["target_pid"]]
            ins = [j for j in switches if events[j]["next_pid"] == e["target_pid"]]
            no_sleep_return = return_events and events[return_events[0]]["detail"] in (-11, -38)
            target_running_here = (wake_source_owners[i] == e["target_pid"]
                                   or e.get("self_wakeup") is True
                                   or e.get("source_pid") == e["target_pid"])
            if no_sleep_return:
                diagnostics.append(f"{e['event_id']}:nonblocking-wait-target-no-sleep-edge")
            elif (not target_running_here and len(outs) == 1 and len(ins) == 1
                  and normalized[outs[0]] == 2
                  and events[outs[0]]["next_pid"] != e["target_pid"]
                  and events[ins[0]]["prev_pid"] != e["target_pid"]
                  and events[outs[0]]["record_ordinal"] < events[ins[0]]["record_ordinal"]):
                # Pending runtime intent proves neither sleep nor the kernel
                # wake cause. Only a proven off-CPU block admits this target
                # chain; RUNNABLE/UNKNOWN and self-wake remain diagnostic.
                deps[i].update((waiter, outs[0]))
                deps[ins[0]].add(i)
                summary["proven_block_wake_admissions"] += 1
            else:
                diagnostics.append(f"{e['event_id']}:wait-request-association-without-proven-block")
        if e.get("cause_status") in VERIFIED_WAKE_CAUSES:
            cause = index_by_id.get(cause_id)
            if (waiter is None or cause is None or events[cause]["event_kind"] != "wake_attempt"
                    or events[cause]["request_id"] != cause_request
                    or events[cause]["os_tid"] != e.get("source_pid")
                    or events[cause]["sync_object_id"] != e.get("sync_object_id")):
                raise ValueError("verified wake cause lacks exact source request/object")
            deps[i].add(cause)
            e["args"][2] = 1
            summary["known_causal_wakes"] += 1
        else:
            # candidate_* fields and runtime bracketing never establish a
            # futex causal edge; IRQ/signal wakeups can occur inside a bracket.
            summary["unknown_wake_causes"] += 1
            diagnostics.append(f"{e['event_id']}:unknown-wake-cause")
    summary["pending_wait_requests"] = len(set(waits) - closed_waits)
    summary["pending_wake_requests"] = len(set(wakes) - closed_wakes)
    summary["tasks"] = len(tasks)
    summary["diagnostics"] = sorted(set(diagnostics))
    if summary["unknown_switches"]:
        summary["diagnostics"].append("racing-or-unverified-scheduler-state")
    if summary["pending_wait_requests"]:
        summary["diagnostics"].append("common-end-pending-wait-no-synthetic-wakeup")
    order = _topological_order(events, deps)
    return {"events": events, "dependencies": [sorted(parents) for parents in deps],
            "topological_order": order, "cores": cores,
            "tasks": [{"os_tid": tid, "os_tgid": values[0], "runtime_thread_id": values[1]}
                      for tid, values in sorted(tasks.items())], "summary": summary}
