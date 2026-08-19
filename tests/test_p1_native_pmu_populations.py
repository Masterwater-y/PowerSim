#!/usr/bin/env python3
"""Regression tests for the fail-closed P1 native-population audit."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from audit_p1_native_pmu_populations import audit_result  # noqa: E402
from merge_kernel_events_oracle_v3 import merge  # noqa: E402
from test_p0_contract import row  # noqa: E402


def native_stats() -> str:
    return "\n".join(
        (
            "board.processor.switch0.core.numCycles 20",
            "board.processor.switch0.core.commitStats0.numOps 8",
            "board.processor.switch0.core.commitStats0.numInsts 6",
            "board.processor.switch0.core.mmu.dtb.rdAccesses 3",
            "board.processor.switch0.core.mmu.dtb.wrAccesses 2",
            "board.processor.switch0.core.mmu.dtb.rdMisses 1",
            "board.processor.switch0.core.mmu.dtb.wrMisses 0",
            "board.cache_hierarchy.ruby_system.l1_controllers0.Dcache.m_demand_accesses 5",
            "board.cache_hierarchy.ruby_system.l1_controllers0.Dcache.m_demand_misses 2",
            "board.cache_hierarchy.ruby_system.l2_controllers0.cache.m_demand_accesses 2",
            "board.cache_hierarchy.ruby_system.l2_controllers0.cache.m_demand_misses 1",
            *(f"board.cache_hierarchy.ruby_system.l3_controllers{i}.L2cache.m_demand_accesses {1 if i == 0 else 0}" for i in range(8)),
            *(f"board.cache_hierarchy.ruby_system.l3_controllers{i}.L2cache.m_demand_misses {1 if i == 0 else 0}" for i in range(8)),
            *(f"board.memory.mem_ctrl{i}.dram.readBursts {1 if i == 0 else 0}" for i in range(8)),
            *(f"board.memory.mem_ctrl{i}.dram.writeBursts 0" for i in range(8)),
        )
    ) + "\n"


class NativePopulationAuditTest(unittest.TestCase):
    def make_result(self, root: Path, legacy: bool = False) -> Path:
        result = root / "sample" / "cache" / "1c" / "workload" / "key" / "run"
        (result / "oracle").mkdir(parents=True)
        document = merge([row()])
        if legacy:
            document["aggregate"]["pmu_source"] = "taotrace-path-class-v2"
            document["per_core"][0]["pmu_source"] = "taotrace-path-class-v2"
        (result / "oracle" / "kernel_events.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        (result / "request.json").write_text(
            json.dumps(
                {
                    "gem5": {"binary_sha256": "abc"},
                    "workload_selection": {"workload": "workload"},
                }
            ),
            encoding="utf-8",
        )
        (result / "effective-target.json").write_text(
            json.dumps(
                {
                    "core": {"count": 1},
                    "cache": {"l3": {"num_banks": 8}},
                    "dram": {"num_channels": 8},
                }
            ),
            encoding="utf-8",
        )
        (result / "stats.txt").write_text(native_stats(), encoding="utf-8")
        return result

    def test_raw_native_difference_never_becomes_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_dir = self.make_result(Path(directory))
            document_path = result_dir / "oracle" / "kernel_events.json"
            document = json.loads(document_path.read_text(encoding="utf-8"))
            document["per_core"][0]["memory_accounting"][
                "late_packets_after_fallback"
            ] = 1
            document["aggregate"]["memory_accounting"][
                "late_packets_after_fallback"
            ] = 1
            document_path.write_text(json.dumps(document), encoding="utf-8")
            result = audit_result(result_dir)
        self.assertFalse(result["formal_comparable"])
        self.assertFalse(result["accuracy_metrics_emitted"])
        self.assertEqual(result["taotrace"]["aggregate"]["retired_uops"], 4)
        self.assertEqual(result["native"]["aggregate"]["retired_uops"], 8)
        self.assertEqual(
            result["window_evidence"][0]["native_minus_taotrace_retired_uops"],
            4,
        )
        for value in result["raw_diagnostic_ratios"].values():
            self.assertFalse(value["accuracy_metric_allowed"])
        self.assertEqual(
            result["path_attribution_timing_evidence"][
                "late_packets_over_fallback"
            ],
            1.0,
        )
        self.assertFalse(
            result["path_attribution_timing_evidence"][
                "accuracy_metric_allowed"
            ]
        )
        self.assertNotIn("ape", json.dumps(result).lower())

    def test_rejects_legacy_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_dir = self.make_result(Path(directory), legacy=True)
            with self.assertRaisesRegex(ValueError, "formal v3 oracle"):
                audit_result(result_dir)


if __name__ == "__main__":
    unittest.main()
