#!/usr/bin/env python3
"""Regression tests for native cache/branch miss PMU validation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_native_kernel_fastsim_validation import (  # noqa: E402
    BRANCH_MISS_SOURCE,
    EVENT_DICTIONARY,
    PMU_CONTRACT_ID,
    PMU_FIELDS,
    aggregate_pmu_accuracy,
    build_pmu_comparison,
    discover_cases,
    load_pmu_event_contract,
    run_case,
    validation_fingerprint,
    write_summary,
)


def oracle(branch_misses=90):
    return {
        "pmu_source": "taotrace-path-class-v3",
        "pmu_contract_id": PMU_CONTRACT_ID,
        "branch_miss_source": BRANCH_MISS_SOURCE,
        "pmu_user_plus_kernel": {
            "branch_misses": branch_misses,
            # These intentionally disagree with native SLICC truth.  The
            # repaired validator must never read them for cache accuracy.
            "l1d_tag_misses": 9001,
            "private_l2_tag_misses": 9002,
            "llc_tag_misses": 9003,
        },
    }


def native(l1d=100, l2=40, llc=20, comparable=True):
    return {
        "native_hierarchy_semantic_comparable": comparable,
        "scope_metrics": {
            "user_plus_kernel": {
                "native_ruby_pmu": {
                    "hierarchy": {
                        "l1d": {"tag_misses": l1d},
                        "l2": {"tag_misses": l2},
                        "llc": {"tag_misses": llc},
                    }
                }
            }
        },
    }


def prediction(branch=100, l1d=110, l2=44, llc=18):
    return {
        "branch_misses": branch,
        "l1d_tag_misses": l1d,
        "private_l2_tag_misses": l2,
        "llc_tag_misses": llc,
    }


class NativeKernelPmuValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_pmu_event_contract(EVENT_DICTIONARY)

    def test_contract_selects_only_cache_and_branch_misses(self):
        self.assertEqual(
            PMU_FIELDS,
            (
                "branch_misses",
                "l1d_tag_misses",
                "private_l2_tag_misses",
                "llc_tag_misses",
            ),
        )
        self.assertEqual(set(self.contract), set(PMU_FIELDS))

    def test_cache_reference_comes_from_native_slicc(self):
        rows = build_pmu_comparison(
            prediction(), oracle(), native(), self.contract
        )
        self.assertEqual(rows["branch_misses"]["reference"], 90)
        self.assertEqual(rows["l1d_tag_misses"]["reference"], 100)
        self.assertEqual(rows["private_l2_tag_misses"]["reference"], 40)
        self.assertEqual(rows["llc_tag_misses"]["reference"], 20)
        for field in PMU_FIELDS[1:]:
            self.assertTrue(
                rows[field]["reference_source"].startswith("native-ruby-slicc:")
            )

    def test_incomplete_native_hierarchy_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "fully conserved"):
            build_pmu_comparison(
                prediction(), oracle(), native(comparable=False), self.contract
            )

    def test_wrong_branch_oracle_source_fails_closed(self):
        document = oracle()
        document["branch_miss_source"] = "committed-dyninst-mispredicted"
        with self.assertRaisesRegex(ValueError, BRANCH_MISS_SOURCE):
            build_pmu_comparison(
                prediction(), document, native(), self.contract
            )

    def test_aggregate_reports_wape_bias_and_tail(self):
        first = build_pmu_comparison(
            prediction(branch=110, l1d=110, l2=44, llc=18),
            oracle(branch_misses=100),
            native(l1d=100, l2=40, llc=20),
            self.contract,
        )
        second = build_pmu_comparison(
            prediction(branch=180, l1d=180, l2=70, llc=45),
            oracle(branch_misses=200),
            native(l1d=200, l2=80, llc=40),
            self.contract,
        )
        reports = [
            {
                "status": "passed",
                "pmu_validation": {"status": "scored"},
                "pmu": first,
            },
            {
                "status": "passed",
                "pmu_validation": {"status": "scored"},
                "pmu": second,
            },
        ]
        result = aggregate_pmu_accuracy(reports)
        branch = result["branch_misses"]
        self.assertEqual(branch["cases"], 2)
        self.assertEqual(branch["finite_ape_cases"], 2)
        self.assertAlmostEqual(branch["wape_percent"], 10.0)
        self.assertAlmostEqual(branch["signed_bias_percent"], -10.0 / 300 * 100)
        self.assertAlmostEqual(branch["p99_ape_percent"], 10.0)
        self.assertEqual(
            result["l1d_tag_misses"]["reference_source"],
            "native-ruby-slicc:l1d.tag_misses",
        )
        self.assertEqual(
            result["branch_misses"]["reference_source"],
            f"kernel_events-v3:{BRANCH_MISS_SOURCE}",
        )

    def test_summary_emits_only_selected_pmu_fields(self):
        comparison = build_pmu_comparison(
            prediction(), oracle(), native(), self.contract
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            case_dir = output / "cases" / "01c-workload"
            case_dir.mkdir(parents=True)
            report = {
                "schema": "fastsim-native-kernel-validation-case-v2",
                "case_id": "01c-workload",
                "cores": 1,
                "workload": "workload",
                "result_dir": "/result",
                "status": "passed",
                "attempts": 1,
                "errors": [],
                "trace_counts": {
                    "kernel_uops": 1,
                    "kernel_instructions": 1,
                },
                "cpi": {
                    "fastsim": 1.2,
                    "gem5": 1.0,
                    "absolute_relative_error": 0.2,
                },
                "pmu_validation": {"status": "scored"},
                "pmu": comparison,
            }
            (case_dir / "validation.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            args = SimpleNamespace(
                output_dir=output, expected_cases=1, max_attempts=3
            )
            summary = write_summary(
                args,
                [
                    {
                        "case_id": "01c-workload",
                        "result_dir": "/result",
                    }
                ],
            )
            markdown = (output / "summary.md").read_text(encoding="utf-8")
            csv_header = (output / "summary.csv").read_text(
                encoding="utf-8"
            ).splitlines()[0]
        self.assertEqual(summary["pmu_scored_cases"], 1)
        self.assertIn("native-ruby-slicc:l1d.tag_misses", markdown)
        self.assertIn("branch_misses_predicted", csv_header)
        self.assertNotIn("dtlb_misses", csv_header)
        self.assertIn("absolute_cpi_error", csv_header)
        self.assertAlmostEqual(summary["cpi_mae"], 0.2)
        self.assertAlmostEqual(summary["cases"][0]["absolute_cpi_error"], 0.2)

    def test_rescore_archived_output_uses_native_gate_without_running_simulator(self):
        # The fixture's legacy L1 miss is 1; native SLICC L1 miss is 0.
        # A nonexistent simulator ensures rescoring cannot silently rerun it.
        from test_p1_native_response_sideband import NativeResponseSidebandTest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = NativeResponseSidebandTest().make_result_summary(root)
            request_path = result / "request.json"
            request = json.loads(request_path.read_text())
            request.update({"sampling": {"functional_include_kernel": True},
                            "profile": {"cores": 1}})
            request_path.write_text(json.dumps(request))
            trace_dir = result / "tao_trace"
            trace_dir.mkdir()
            (trace_dir / "manifest.txt").write_text("fixture\n")
            (trace_dir / "trace.json").write_text(json.dumps({
                "trace_scope": "user-plus-kernel",
                "functional_boundaries": {"0": {
                    "measurement_user_records": 4, "measurement_records": 5,
                    "measurement_user_instructions": 2, "measurement_instructions": 3,
                }},
            }))
            stats_dir = root / "stats"
            stats_dir.mkdir()
            stats_path = stats_dir / "formal-01c-workload.json"
            pmu = prediction(branch=0, l1d=0, l2=0, llc=0)
            pmu.update({"retired_uops": 5, "retired_instructions": 3,
                        "llc_tag_accesses": 0})
            simulation = {
                "measurement_scope": "user-plus-kernel",
                "configuration": {"cores": 1, "native_kernel_trace": True},
                "scope_metrics": {
                    "pmu_source": "fastsim-functional-native-user-plus-kernel-v1",
                    "pmu_contract_id": PMU_CONTRACT_ID, "pmu": pmu,
                    "user_trace_uops": 4, "native_kernel_trace_uops": 1,
                    "user_trace_instructions": 2, "native_kernel_trace_instructions": 1,
                    "perf_like_cpi": 4.0,
                },
                "totals": {
                    "native_kernel_trace": {"records": 1, "retired_instructions": 1,
                                            "memory_uops": 1},
                    "syscall_uops": 0, "llc_outcomes_conserved": True,
                    **{name: {"events": 0} for name in (
                        "synthetic_syscall_kernel", "synthetic_page_fault_kernel",
                        "synthetic_irq_kernel", "synthetic_kernel_total")},
                },
                "cha": [],
            }
            stats_path.write_text(json.dumps(simulation))
            args = SimpleNamespace(
                output_dir=root / "reports", stats_dir=stats_dir,
                fastsim=root / "does-not-exist", config=root / "does-not-exist.cfg",
                event_dictionary=EVENT_DICTIONARY, repo_root=ROOT,
            )
            case = {"case_id": "formal-01c-workload", "cores": 1,
                    "workload": "workload", "result_dir": str(result)}
            report = run_case(case, args)
            self.assertEqual(report["status"], "passed", report["errors"])
            self.assertEqual(report["pmu"]["l1d_tag_misses"]["reference"], 0)
            self.assertEqual(report["fastsim_output"], str(stats_path))
            self.assertEqual(report["cpi"]["absolute_error"], 1.0)
            self.assertFalse(report["cpi"]["instruction_population_match"])
            before = validation_fingerprint(case, args)["sha256"]
            simulation["scope_metrics"]["pmu"]["l1d_tag_misses"] = 1
            stats_path.write_text(json.dumps(simulation))
            self.assertNotEqual(before, validation_fingerprint(case, args)["sha256"])
            simulation["measurement_scope"] = "user"
            stats_path.write_text(json.dumps(simulation))
            wrong_scope = run_case(case, args)
            self.assertEqual(wrong_scope["status"], "failed")
            self.assertEqual(wrong_scope["pmu_validation"]["status"], "source-gate-failed")
            # Missing native evidence must never fall back to legacy cache PMU.
            (result / "oracle/native-summary-core0.json").unlink()
            failed = run_case(case, args)
            self.assertEqual(failed["status"], "failed")
            self.assertNotIn("pmu", failed)

    def test_source_discovery_preserves_case_ids_for_archived_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "formal-32c-816.nab_s"
            result.mkdir()
            (result / "request.json").write_text(json.dumps({
                "profile": {"cores": 32},
                "workload_selection": {"workload": "816.nab_s"},
            }))
            cases = discover_cases([], source_root=root)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["case_id"], "formal-32c-816.nab_s")
            self.assertEqual(cases[0]["cores"], 32)

    def test_rescore_cli_missing_case_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/run_native_kernel_fastsim_validation.py"),
                "--source-root", str(root), "--stats-dir", str(root),
                "--output-dir", str(root / "report"), "--expected-cases", "1",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("discovered=0/1", result.stdout)

    def test_fingerprint_invalidates_changed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result"
            oracle_dir = result / "oracle"
            trace_dir = result / "tao_trace"
            oracle_dir.mkdir(parents=True)
            trace_dir.mkdir()
            for path, text in (
                (result / "request.json", "{}"),
                (trace_dir / "trace.json", "{}"),
                (trace_dir / "manifest.txt", "trace"),
                (oracle_dir / "kernel_events.json", "{}"),
                (oracle_dir / "native-summary-core0.json", "{}"),
            ):
                path.write_text(text, encoding="utf-8")
            fastsim = root / "fastsim"
            config = root / "config.cfg"
            fastsim.write_text("binary-v1", encoding="utf-8")
            config.write_text("config-v1", encoding="utf-8")
            args = SimpleNamespace(
                fastsim=fastsim,
                config=config,
                event_dictionary=EVENT_DICTIONARY,
            )
            case = {"result_dir": str(result), "cores": 1}
            before = validation_fingerprint(case, args)["sha256"]
            config.write_text("config-version-two", encoding="utf-8")
            after = validation_fingerprint(case, args)["sha256"]
        self.assertNotEqual(before, after)

    def test_fingerprint_invalidates_changed_included_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result"
            oracle_dir = result / "oracle"
            trace_dir = result / "tao_trace"
            oracle_dir.mkdir(parents=True)
            trace_dir.mkdir()
            for path, text in (
                (result / "request.json", "{}"),
                (trace_dir / "trace.json", "{}"),
                (trace_dir / "manifest.txt", "trace"),
                (oracle_dir / "kernel_events.json", "{}"),
                (oracle_dir / "native-summary-core0.json", "{}"),
            ):
                path.write_text(text, encoding="utf-8")
            fastsim = root / "fastsim"
            config = root / "config.cfg"
            included = root / "base.cfg"
            fastsim.write_text("binary-v1", encoding="utf-8")
            config.write_text(
                "config.include = base.cfg\nlocal.value = 1\n",
                encoding="utf-8",
            )
            included.write_text("base.value = 1\n", encoding="utf-8")
            args = SimpleNamespace(
                fastsim=fastsim,
                config=config,
                event_dictionary=EVENT_DICTIONARY,
            )
            case = {"result_dir": str(result), "cores": 1}
            before = validation_fingerprint(case, args)
            included.write_text("base.value = 2\n", encoding="utf-8")
            after = validation_fingerprint(case, args)
        self.assertIn("config_include/0001", before["inputs"])
        self.assertNotEqual(before["sha256"], after["sha256"])


if __name__ == "__main__":
    unittest.main()
