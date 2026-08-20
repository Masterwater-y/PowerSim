#!/usr/bin/env python3
"""Validate and summarize TaoTrace exact-window front-end accounting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TERMINALS = (
    "translation_squashes",
    "translation_faults",
    "no_good_address_terminals",
    "retry_discards",
    "icache_responses",
    "icache_squashed_responses",
)
STATUSES = (
    "running_cycles",
    "idle_cycles",
    "squashing_cycles",
    "blocked_cycles",
    "fetching_cycles",
    "trap_pending_cycles",
    "quiesce_pending_cycles",
    "itlb_wait_cycles",
    "icache_wait_response_cycles",
    "icache_wait_retry_cycles",
    "icache_access_complete_cycles",
    "ftq_wait_cycles",
    "no_good_addr_cycles",
)
FRONTEND_SCHEMA = "taotrace-scoped-frontend-v1"
FRONTEND_SCOPE = "exact-cpl-first-event-to-functional-target-window"


def _load_rows(path: Path) -> list[dict]:
    if path.is_dir():
        merged = path / "kernel_events.json"
        if merged.exists():
            path = merged
        else:
            paths = sorted(path.glob("kernel-events-core*.json"))
            if not paths:
                paths = sorted(path.glob("**/kernel-events-core*.json"))
            return [json.loads(item.read_text()) for item in paths]
    payload = json.loads(path.read_text())
    if isinstance(payload.get("per_core"), list):
        return payload["per_core"]
    return [payload]


def _check(row: dict) -> list[str]:
    frontend = row.get("frontend_accounting")
    if not isinstance(frontend, dict):
        return ["missing frontend_accounting"]
    errors = []
    if frontend.get("schema") != FRONTEND_SCHEMA:
        errors.append(f"bad schema {frontend.get('schema')!r}")
    if frontend.get("scope") != FRONTEND_SCOPE:
        errors.append(f"bad scope {frontend.get('scope')!r}")
    lhs = int(frontend["inflight_at_start"]) + int(
        frontend["requests_started"]
    )
    rhs = sum(int(frontend[field]) for field in TERMINALS) + int(
        frontend["inflight_at_end"]
    )
    if lhs != rhs or frontend.get("request_population_conserved") is not True:
        errors.append(f"request population {lhs} != {rhs}")
    mode = int(frontend["user_mode_requests_started"]) + int(
        frontend["kernel_mode_requests_started"]
    )
    if mode != int(frontend["requests_started"]):
        errors.append("request mode population does not conserve")
    reasons = sum(
        int(frontend[field])
        for field in (
            "invalid_same_block_refetches",
            "invalid_new_block_requests",
            "valid_block_changes",
        )
    )
    if reasons != int(frontend["requests_started"]):
        errors.append("request reason population does not conserve")
    sends = int(frontend["icache_requests_sent"]) + int(
        frontend["icache_send_rejects"]
    )
    if sends != int(frontend["icache_send_attempts"]):
        errors.append("send attempts do not conserve")
    status = sum(int(frontend[field]) for field in STATUSES)
    if status != int(frontend["status_cycle_samples"]):
        errors.append("status cycles do not conserve")
    # Fetch.tick runs before Commit.tick.  Therefore the target cycle can add
    # at most one final sample.  A larger positive delta proves that frontend
    # collection opened before this core's CPL numerator window.  Negative
    # deltas are legal while an O3 CPU is quiesced and Fetch.tick is stopped.
    measured = int(row.get("measured_cycles", 0))
    samples = int(frontend["status_cycle_samples"])
    if samples > measured + 1:
        errors.append(
            f"frontend samples extend past CPL window: {samples} > "
            f"{measured} + 1"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    print(
        "input core requests squash/req refetch/req iwait/cycle "
        "retry/cycle sample-minus-measured"
    )
    for source in args.inputs:
        rows = _load_rows(source)
        if not rows:
            print(f"{source}: no kernel-event rows")
            failed = True
            continue
        for row in rows:
            frontend = row.get("frontend_accounting", {})
            errors = _check(row)
            requests = int(frontend.get("requests_started", 0))
            samples = int(frontend.get("status_cycle_samples", 0))
            measured = int(row.get("measured_cycles", 0))
            ratio = lambda value, base: value / base if base else 0.0
            print(
                f"{source} {row.get('core_id', '?')} {requests} "
                f"{ratio(int(frontend.get('squash_events', 0)), requests):.6f} "
                f"{ratio(int(frontend.get('invalid_same_block_refetches', 0)), requests):.6f} "
                f"{ratio(int(frontend.get('icache_wait_response_cycles', 0)), samples):.6f} "
                f"{ratio(int(frontend.get('icache_wait_retry_cycles', 0)), samples):.6f} "
                f"{samples - measured}"
            )
            for error in errors:
                print(f"ERROR {source} core={row.get('core_id', '?')}: {error}")
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
