#!/usr/bin/env python3
"""Exercise the active-kernel ledger, including output and timing neutrality.

Catches inactive instrumentation, trial-solve overcount, and a diagnostic that
changes the simulated timeline. Run after building build/fastsim.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IntervalTailAuditTest(unittest.TestCase):
    def test_fast_kernel_charge_is_conserved_without_timing_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            directory = Path(temporary)
            results = []
            for enabled in (False, True, "owners", "scalar"):
                config = directory / f"audit-{enabled}.cfg"
                config.write_text(
                    "core.model = interval_weave\n"
                    "sim.interval_scheduler = time_epoch\n"
                    "core.response_queue_feedback = true\n"
                    "core.response_sparse_scoreboard = true\n"
                    "core.response_block_summary = true\n"
                    "core.response_memory_descriptor = true\n"
                    "core.response_monotone_iq_calendar = true\n"
                    f"core.response_materialized_uop_fast_kernel = {str(enabled != 'scalar').lower()}\n"
                    f"core.response_interval_tail_audit = {str(bool(enabled) and enabled != 'scalar').lower()}\n"
                    f"sim.cpi_attribution = {str(enabled == 'scalar').lower()}\n"
                    + ("core.response_frontier_audit_stride_uops = 1\n"
                       if enabled in ("owners", "scalar") else "")
                )
                output = config.with_suffix(".json")
                process = subprocess.run(
                    [str(ROOT / "build/fastsim"), "benchmark", "--config",
                     str(config), "--measurement-scope", "user",
                     "--cores", "2", "--instructions-per-core", "4096",
                     "--output", str(output)],
                    capture_output=True, text=True,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                results.append(json.loads(output.read_text()))
            off, on, owners, scalar = results
            for core in scalar["cores"]:
                for sample in core["response_frontier_audit"]:
                    self.assertTrue(sample["cycle_causes_available"])
                    self.assertIsNone(sample["tail_dispatch_gate"])
            self.assertEqual(on["threads"], owners["threads"])
            self.assertEqual(on["interval_tail_audit"], owners["interval_tail_audit"])
            joined_sq, joined_tso, crossed_checkpoint = 0, 0, 0
            for core in owners["cores"]:
                samples = core["response_frontier_audit"]
                by_sequence = {s["sequence"]: s for s in samples}
                self.assertEqual(len(samples), len(by_sequence), "only accepted samples")
                for s in samples:
                    self.assertFalse(s["cycle_causes_available"])
                    if s["regular_store"]:
                        self.assertEqual(s["store_sq_release_cycle"],
                                         s["store_send_cycle"] + s["store_service_cycles"])
                        self.assertGreaterEqual(s["store_send_cycle"], s["actual_retire_cycle"])
                    if s["has_store"] and s["incoming_sq_release_valid"]:
                        parent = by_sequence[s["incoming_sq_release_sequence"]]
                        self.assertEqual(s["incoming_sq_release_cycle"], parent["store_sq_release_cycle"])
                        joined_sq += 1
                        crossed_checkpoint += parent["sequence"] < s["checkpoint_begin_sequence"]
                    if s["regular_store"] and s["incoming_tso_owner_valid"]:
                        parent = by_sequence[s["incoming_tso_owner_sequence"]]
                        self.assertEqual(s["incoming_tso_release_cycle"], parent["store_sq_release_cycle"])
                        joined_tso += 1
            self.assertGreater(joined_sq, 0)
            self.assertGreater(joined_tso, 0)
            self.assertGreater(crossed_checkpoint, 0)
            self.assertTrue("interval_tail_audit" in on,
                          "the materialized kernel must publish its live ledger")
            self.assertGreater(on["totals"]["exposed_memory_penalty_cycles"], 0)
            ledger = on["interval_tail_audit"]
            self.assertEqual(sum(c["charged_cycles"] for c in ledger),
                             on["totals"]["exposed_memory_penalty_cycles"])
            self.assertEqual(sum(c["accepted_uops"] for c in ledger),
                             on["causal_frontier"]["interval_accepted_uops"])
            for core in ledger:
                self.assertTrue(core["conserved"])
                self.assertEqual(core["charged_cycles"], sum(core[k] for k in (
                    "entry_carry_cycles", "base_advance_cycles",
                    "completion_advance_cycles", "commit_width_cycles")))
                self.assertEqual(core["completion_advance_cycles"],
                                 core["pre_issue_cycles"] + core["post_issue_cycles"])
                self.assertEqual(core["pre_issue_cycles"],
                                 sum(core["pre_issue_by_issue_gate"]))
                self.assertEqual(core["pre_issue_by_issue_gate"][1],
                                 sum(core["dispatch_pre_issue_by_gate"]))
                for sample in core["largest_checkpoints"]:
                    self.assertEqual(sample["witness_valid"],
                                     sample["witness_advance_cycles"] > 0)
                    self.assertLessEqual(sample["interval_gap_cycles"],
                                         sample["base_end_cycle"])
            self.assertEqual(sum(c["charged_cycles"]
                                 for c in off["interval_tail_audit"]), 0)
            self.assertEqual(off["threads"], on["threads"])
            for key in ("perf_like_cpi", "user_cycles", "user_uops"):
                if key in off["scope_metrics"]:
                    self.assertEqual(off["scope_metrics"][key], on["scope_metrics"][key])
            for key in ("exposed_memory_penalty_cycles", "l1d_misses", "l2_misses"):
                self.assertEqual(off["totals"][key], on["totals"][key])


if __name__ == "__main__":
    unittest.main()
