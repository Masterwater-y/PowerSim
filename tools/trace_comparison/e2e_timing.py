"""Primary wall-clock interval includes preparation; audits run separately."""

import re


def elapsed_seconds(start_ns, endpoints, process_end_ns, participants):
    if set(endpoints) != set(range(participants)):
        raise ValueError("missing or unexpected completion participant")
    if any(not start_ns < t <= process_end_ns for t in endpoints.values()):
        raise ValueError("invalid monotonic clock ordering")
    return (max(endpoints.values()) - start_ns) / 1e9


def parse_endpoints(text, engine, cores):
    if engine != "sniper":
        rows = re.findall(r"\[TRACEBENCH-END\] host_monotonic_ns=(\d+)", text)
        if len(rows) != 1:
            raise ValueError("expected exactly one engine completion marker")
        return {0: int(rows[0])}
    rows = re.findall(
        r"\[FST-ENDPOINT:(\d+)\] complete_macros=(\d+) model_elapsed_fs=(\d+) stop=(\d+) host_monotonic_ns=(\d+)",
        text,
    )
    result = {}
    for core, macros, model, stop, host in rows:
        c = int(core)
        if c in result or int(stop):
            raise ValueError("duplicate or stopped Sniper endpoint")
        result[c] = int(host)
    if set(result) != set(range(cores)):
        raise ValueError("incomplete Sniper endpoints")
    return result
