"""Validate the first-core macro target's shared FST/CPI measurement end.

The target counts measured CPL3 architectural macros. FST records and their
paired CPI rows retain actual UOP populations without truncation. No timing
value from this offline evidence is written into a FastSim instruction stream.
"""
from __future__ import annotations

POLICY = "first-core-macro-common-end"


def validate_common_end(boundaries, cpi_rows, cpl_rows, *, cores, target):
    def index(rows, label):
        result = {int(row["core_id"]): row for row in rows}
        if len(result) != len(rows) or set(result) != set(range(cores)):
            raise ValueError(f"unexpected {label} participants")
        return result

    if cores < 1 or target < 1:
        raise ValueError("positive participant count and target required")
    rows = index(boundaries, "FST")
    cpi = index(cpi_rows, "CPI")
    cpl = index(cpl_rows, "CPL")
    endpoints = set()
    triggers = set()
    counts = {}
    for core, row in rows.items():
        if row.get("measurement_policy") != POLICY:
            raise ValueError(f"core {core}: missing first-core common-end policy")
        if not row.get("measurement_started") or not row.get("measurement_closed"):
            raise ValueError(f"core {core}: measurement did not close")
        if row.get("stop_reason") != "first-core-user-macro-target":
            raise ValueError(f"core {core}: invalid stop reason")
        if int(row.get("target_user_macros", -1)) != target or int(
                row.get("participant_count", -1)) != cores:
            raise ValueError(f"core {core}: target/participant mismatch")
        end = int(row.get("common_end_tick", 0))
        cpl_end = int(cpl[core]["last_tick"])
        if end <= 0 or (cpl_end != 0 and cpl_end != end):
            raise ValueError(f"core {core}: CPL/common end mismatch")
        endpoints.add(end)
        triggers.add(int(row.get("trigger_core", -1)))
        count = int(row["measurement_user_records"])
        if count < 0 or int(cpi[core]["n_user"]) != count:
            raise ValueError(f"core {core}: FST/CPI user population mismatch")
        if int(row["warmup_records"]) + int(row["measurement_records"]) != int(
                row["total_records"]) or count > int(row["measurement_records"]):
            raise ValueError(f"core {core}: functional population mismatch")
        counts[core] = count
    if len(endpoints) != 1 or len(triggers) != 1:
        raise ValueError("per-core windows lack a common end/trigger")
    trigger = next(iter(triggers))
    if (trigger not in rows or
            int(rows[trigger]["measurement_user_instructions"]) != target or
            not rows[trigger].get("target_reached")):
        raise ValueError("trigger core did not reach the user target")
    return dict(measurement_policy=POLICY, common_end_tick=next(iter(endpoints)),
                trigger_core=trigger, target_user_macros=target, cores=cores,
                per_core_user_uops=counts, user_uops=sum(counts.values()))
