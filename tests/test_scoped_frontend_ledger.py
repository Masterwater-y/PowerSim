#!/usr/bin/env python3
"""Regression tests for the exact TaoTrace-window front-end ledger."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from merge_kernel_events_oracle_v3 import merge  # noqa: E402
from test_p0_contract import row  # noqa: E402
from validate_kernel_events_oracle import validate_frontend_accounting  # noqa: E402


def frontend() -> dict:
    value = {
        "schema": "taotrace-scoped-frontend-v1",
        "scope": "exact-cpl-first-event-to-functional-target-window",
        "inflight_at_start": 1,
        "requests_started": 3,
        "user_mode_requests_started": 2,
        "kernel_mode_requests_started": 1,
        "invalid_same_block_refetches": 1,
        "invalid_new_block_requests": 1,
        "valid_block_changes": 1,
        "translations_completed": 3,
        "icache_send_attempts": 5,
        "icache_requests_sent": 3,
        "icache_send_rejects": 2,
        "translation_squashes": 0,
        "translation_faults": 0,
        "no_good_address_terminals": 0,
        "retry_discards": 0,
        "icache_responses": 2,
        "icache_squashed_responses": 1,
        "inflight_at_end": 1,
        "squash_events": 2,
        "squash_events_with_outstanding": 1,
        "status_cycle_samples": 10,
        "request_to_response_ticks": 3000,
        "request_to_response_cycles": 3,
        "request_to_response_samples": 2,
        "request_population_conserved": True,
        "request_mode_conserved": True,
        "request_reason_conserved": True,
        "send_accounting_conserved": True,
    }
    status_fields = (
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
    value.update({field: 0 for field in status_fields})
    value["running_cycles"] = 4
    value["icache_wait_response_cycles"] = 6
    return value


class ScopedFrontendLedgerTest(unittest.TestCase):
    def test_patch_uses_exact_boundary_and_real_fetch_hooks(self) -> None:
        patch = (
            ROOT / "patches" / "p3-external-scoped-frontend-ledger.patch"
        ).read_text(encoding="utf-8")
        self.assertIn("TaoTraceFrontendRegistry::enableContext", patch)
        self.assertIn("TaoTraceFrontendRegistry::disableContext", patch)
        self.assertIn("noteRequestStart", patch)
        self.assertIn("noteStatusCycle", patch)
        self.assertIn(
            "exact-cpl-first-event-to-functional-target-window", patch
        )

    def test_merge_preserves_and_conserves_frontend_population(self) -> None:
        first = row()
        first["frontend_accounting"] = frontend()
        second = copy.deepcopy(first)
        second["core_id"] = 1
        document = merge([first, second])
        aggregate = document["aggregate"]["frontend_accounting"]
        self.assertEqual(aggregate["requests_started"], 6)
        self.assertEqual(aggregate["icache_wait_response_cycles"], 12)
        self.assertEqual(aggregate["status_sample_minus_measured_cycles"], 0)
        self.assertTrue(aggregate["request_population_conserved"])

    def test_merge_rejects_missing_terminal(self) -> None:
        broken = row()
        broken["frontend_accounting"] = frontend()
        broken["frontend_accounting"]["icache_responses"] -= 1
        with self.assertRaisesRegex(ValueError, "request_population"):
            merge([broken])

    def test_validator_rejects_marker_to_cpl_cycle_leak(self) -> None:
        broken = row()
        broken["frontend_accounting"] = frontend()
        broken["measured_cycles"] = 8
        with self.assertRaisesRegex(ValueError, "extend past CPL window"):
            validate_frontend_accounting(broken, "per_core[0]")


if __name__ == "__main__":
    unittest.main()
