#!/usr/bin/env python3
"""Focused regression tests for the P0 measurement contract."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from merge_kernel_events_oracle_v3 import merge  # noqa: E402
from compare_kernel_event_accuracy import pmu_event_status  # noqa: E402
from validate_kernel_events_oracle import (  # noqa: E402
    P0_PMU_FIELDS,
    validate_document,
)


PMU_FIELDS = {field: 0 for field in P0_PMU_FIELDS}
KERNEL_CLASSES = (
    "syscall",
    "page_fault",
    "irq",
    "scheduler",
    "idle",
    "unknown_kernel",
)


def row() -> dict:
    user = copy.deepcopy(PMU_FIELDS)
    user.update(
        {
            "retired_instructions": 2,
            "retired_uops": 4,
            "memory_uops": 2,
            "line_requests": 3,
            "l1d_accesses": 3,
            "l1d_hits": 2,
            "l1d_misses": 1,
            "l1d_tag_accesses": 3,
            "l1d_tag_hits": 2,
            "l1d_tag_misses": 1,
            "l2_accesses": 1,
            "l2_hits": 1,
            "private_l2_tag_accesses": 1,
            "private_l2_tag_hits": 1,
            "dtlb_accesses": 2,
            "dtlb_hits": 2,
        }
    )
    return {
        "core_id": 0,
        "measured_cycles": 10,
        "n_user": 4,
        "user_cycles": 10,
        "syscall_kernel_cycles": 0,
        "page_fault_kernel_cycles": 0,
        "irq_kernel_cycles": 0,
        "scheduler_kernel_cycles": 0,
        "idle_cycles": 0,
        "unknown_kernel_cycles": 0,
        "blocked_wall_cycles": 0,
        "cpi_user": 2.5,
        "cpi_user_plus_kernel": 2.5,
        "cycles_per_user_uop_user": 2.5,
        "cycles_per_user_uop_user_plus_kernel": 2.5,
        "user_retired_instructions": 2,
        "user_plus_kernel_retired_instructions": 2,
        "perf_like_cpi_user": 5.0,
        "perf_like_cpi_user_plus_kernel": 5.0,
        "pmu_source": "taotrace-path-class-v3",
        "pmu_contract_id": "perf-gem5-fastsim-x86-fs-v1",
        "idle_detection": "x86-halt-mwait-or-repeated-f3-90-v2",
        "poll_idle_pause_threshold": 128,
        "poll_idle_max_gap_commits": 64,
        "pmu_user": user,
        "pmu_user_plus_kernel": copy.deepcopy(user),
        "pmu_kernel_by_class": {
            name: copy.deepcopy(PMU_FIELDS) for name in KERNEL_CLASSES
        },
        "syscall_profiles": [],
        "event_counts": {name: 0 for name in KERNEL_CLASSES},
        "irq_vectors": {},
        "unknown_kernel_sources": [],
        "memory_accounting": {
            "committed_memory_uops": 2,
            "packet_attributed_uops": 1,
            "fallback_attributed_uops": 1,
            "explicitly_rejected_uops": 0,
            "line_requests": 3,
            "unaccounted_uops": 0,
            "duplicate_accounting_uops": 0,
            "dtlb_unknown_uops": 0,
            "late_packets_after_fallback": 0,
        },
    }


class P0ContractTest(unittest.TestCase):
    def test_event_dictionary_excludes_unavailable_accuracy(self) -> None:
        status = pmu_event_status()
        self.assertEqual(status["llc_tag_misses"]["mapping"], "diagnostic")
        for field in ("llc_merged_misses", "llc_unique_fills"):
            self.assertEqual(status[field]["mapping"], "diagnostic")
        for field in ("dram_reads", "dram_writes"):
            self.assertEqual(status[field]["mapping"], "unavailable")

    def test_merge_accepts_exactly_once_and_cross_line(self) -> None:
        document = merge([row()])
        result = validate_document(document, 0.0)
        self.assertTrue(result["formal_pmu_eligible"])
        self.assertEqual(
            document["aggregate"]["memory_accounting"]["line_requests"], 3
        )
        self.assertEqual(document["aggregate"]["perf_like_cpi_user"], 5.0)

    def test_rejects_unaccounted_memory_uop(self) -> None:
        broken = row()
        broken["memory_accounting"]["fallback_attributed_uops"] = 0
        broken["memory_accounting"]["unaccounted_uops"] = 1
        with self.assertRaisesRegex(ValueError, "coverage failed"):
            merge([broken])

    def test_rejects_scope_line_request_mismatch(self) -> None:
        broken = row()
        for scope in ("pmu_user", "pmu_user_plus_kernel"):
            pmu = broken[scope]
            pmu["line_requests"] = 2
            pmu["l1d_accesses"] = 2
            pmu["l1d_hits"] = 1
            pmu["l1d_tag_accesses"] = 2
            pmu["l1d_tag_hits"] = 1
        with self.assertRaisesRegex(ValueError, "line-request conservation"):
            merge([broken])

    def test_rejects_missing_split_hierarchy_field(self) -> None:
        broken = row()
        for scope in ("pmu_user", "pmu_user_plus_kernel"):
            del broken[scope]["llc_unique_fills"]
        for pmu in broken["pmu_kernel_by_class"].values():
            del pmu["llc_unique_fills"]
        with self.assertRaisesRegex(ValueError, "lacks P0 PMU fields"):
            merge([broken])

    def test_rejects_line_expansion_as_fake_dtlb_accesses(self) -> None:
        broken = row()
        broken["pmu_user"]["dtlb_accesses"] = 3
        broken["pmu_user"]["dtlb_hits"] = 3
        broken["pmu_user_plus_kernel"]["dtlb_accesses"] = 3
        broken["pmu_user_plus_kernel"]["dtlb_hits"] = 3
        with self.assertRaisesRegex(ValueError, "dTLB lookups"):
            merge([broken])

    def test_rejects_unclassified_dtlb_outcome(self) -> None:
        broken = row()
        broken["memory_accounting"]["dtlb_unknown_uops"] = 1
        with self.assertRaisesRegex(ValueError, "unknown committed dTLB"):
            merge([broken])


if __name__ == "__main__":
    unittest.main()
