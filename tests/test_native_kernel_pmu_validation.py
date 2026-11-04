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
    CASE_SCHEMA,
    EVENT_DICTIONARY,
    PMU_CONTRACT_ID,
    PMU_FIELDS,
    aggregate_pmu_accuracy,
    build_pmu_comparison,
    discover_cases,
    existing_report,
    load_pmu_event_contract,
    pmu_population_checks,
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

    def test_full_kernel_capture_cannot_score_against_idle_excluding_reference(self):
        reference = {"user_retired_instructions": 2,
                     "user_plus_kernel_retired_instructions": 3}
        scope = {"user_trace_instructions": 2, "native_kernel_trace_instructions": 1}
        self.assertTrue(all(pmu_population_checks(scope, reference).values()))
        scope["native_kernel_trace_instructions"] = 2  # additional idle macro
        self.assertFalse(pmu_population_checks(scope, reference)["pmu_instruction_population_match"])
        self.assertFalse(all(pmu_population_checks(scope, {}).values()))

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
            report.update(status="failed", errors=["pmu_instruction_population_match"],
                          pmu_validation={"status": "source-gate-failed"})
            report["cpi"]["status"] = "scored-active"
            report["elapsed"] = {"status": "scored", "absolute_error_seconds": 0.001}
            (case_dir / "validation.json").write_text(json.dumps(report))
            independent = write_summary(args, [{"case_id": "01c-workload", "result_dir": "/result"}])
            self.assertEqual(independent["failed_cases"], 1)
            self.assertEqual(independent["pmu_scored_cases"], 0)
            self.assertEqual(independent["active_cpi_accuracy"]["cases"], 1)
            self.assertAlmostEqual(independent["active_cpi_accuracy"]["cpi_mae"], 0.2)
            self.assertEqual(independent["elapsed_accuracy"]["cases"], 1)
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
            # This replay includes one kernel macro; give the independent
            # reference that same population instead of the user-only fixture.
            from test_p0_contract import row
            from merge_kernel_events_oracle_v3 import merge
            reference_row = row()
            reference_row["user_plus_kernel_retired_instructions"] = 3
            reference_row["perf_like_cpi_user_plus_kernel"] = 10 / 3
            reference_row["pmu_user_plus_kernel"].update(retired_instructions=3, retired_uops=5)
            reference_row["pmu_kernel_by_class"]["scheduler"].update(retired_instructions=1, retired_uops=1)
            reference_path = result / "oracle/kernel_events.json"
            reference_path.write_text(json.dumps(merge([reference_row])))
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
            self.assertAlmostEqual(report["cpi"]["absolute_error"], abs(4.0 - 10 / 3))
            self.assertTrue(report["cpi"]["instruction_population_match"])
            self.assertEqual(report["cpi"]["status"], "legacy-diagnostic")
            args.perf_retirement = True
            missing_retirement = run_case(case, args)
            self.assertEqual(missing_retirement['status'], 'failed')
            self.assertNotIn('perf_retirement_validation', missing_retirement)
            args.perf_retirement = False
            # Additional retained idle work must not get a plausible PMU APE.
            original_simulation = json.dumps(simulation)
            original_trace = (trace_dir / "trace.json").read_text()
            simulation["scope_metrics"]["native_kernel_trace_instructions"] = 2
            simulation["scope_metrics"]["native_kernel_trace_uops"] = 2
            simulation["totals"]["native_kernel_trace"].update(records=2, retired_instructions=2)
            simulation["scope_metrics"]["pmu"].update(retired_instructions=4, retired_uops=6)
            stats_path.write_text(json.dumps(simulation))
            extra_idle_trace = json.loads(original_trace)
            extra_idle_trace["functional_boundaries"]["0"].update(measurement_records=6, measurement_instructions=4)
            (trace_dir / "trace.json").write_text(json.dumps(extra_idle_trace))
            idle_mismatch = run_case(case, args)
            self.assertEqual(idle_mismatch["status"], "failed")
            self.assertEqual(idle_mismatch["pmu_validation"]["status"], "source-gate-failed")
            self.assertEqual(idle_mismatch["pmu"], {})
            self.assertIn("pmu_instruction_population_match", idle_mismatch["errors"])
            simulation = json.loads(original_simulation)
            stats_path.write_text(original_simulation)
            (trace_dir / "trace.json").write_text(original_trace)
            # Explicit unknown task state cannot reuse the legacy CPI above.
            simulation["time_accounting"] = {
                "schema": "fastsim-time-accounting-v1",
                "scope": "user-plus-kernel", "active_scope": "workload-task",
                "active_status": "unavailable-os-state", "active_cycles": None,
                "unknown_cycles": 4, "elapsed_status": "available",
                "elapsed_reference_cycles": 12, "elapsed_time_seconds": 0.012,
                "reference_frequency_hz": 1000,
            }
            stats_path.write_text(json.dumps(simulation))
            unknown = run_case(case, args)
            self.assertEqual(unknown["status"], "passed", unknown["errors"])
            self.assertEqual(unknown["cpi"]["status"], "unavailable")
            self.assertIsNone(unknown["cpi"]["absolute_error"])
            self.assertEqual(unknown["elapsed"]["fastsim_seconds"], 0.012)
            self.assertEqual(unknown["pmu_validation"]["status"], "scored")
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
            role_path = trace_dir / "core0.fst.mreq"
            role_path.write_bytes(b"request-role-version-one")
            role_before = validation_fingerprint(case, args)["sha256"]
            role_path.write_bytes(b"request-role-version-two")
            role_after = validation_fingerprint(case, args)["sha256"]
            self.assertNotEqual(role_before, role_after)
            origin_path=trace_dir/'core0.fst.fsem'
            origin_path.write_bytes(b'functional-origin-v1')
            origin_before=validation_fingerprint(case,args)['sha256']
            origin_path.write_bytes(b'functional-origin-v2')
            self.assertNotEqual(origin_before,validation_fingerprint(case,args)['sha256'])
            xlat=trace_dir/'core0.fst.xlat.gz';xlat.write_bytes(b'xlat-one')
            xlat_before=validation_fingerprint(case,args)['sha256']
            xlat.write_bytes(b'xlat-two')
            self.assertNotEqual(xlat_before,validation_fingerprint(case,args)['sha256'])
            request_facts=trace_dir/'core0.fst.sreq';request_facts.write_bytes(b'flags-one')
            facts_before=validation_fingerprint(case,args)['sha256']
            request_facts.write_bytes(b'flags-two')
            self.assertNotEqual(facts_before,validation_fingerprint(case,args)['sha256'])
            static_facts=trace_dir/'core0.fst.uops.gz';static_facts.write_bytes(b'static-one')
            static_before=validation_fingerprint(case,args)['sha256']
            static_facts.write_bytes(b'static-two')
            self.assertNotEqual(static_before,validation_fingerprint(case,args)['sha256'])
        self.assertNotEqual(before, after)

    def test_passed_report_cache_rechecks_current_audited_trace_evidence(self):
        import struct
        from audit_thread_state_accounting import audit
        from test_task_clock_audit import capture_fixture
        from test_thread_state_graph import certify

        for mutation in ("metadata-read-failure", "non-event-macro"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = root / "result"
                trace = result / "tao_trace"
                trace.mkdir(parents=True)
                capture_fixture(trace)
                metadata_path = trace / "thread-state-metadata-core0.json"
                metadata = certify(json.loads(metadata_path.read_text()))
                metadata_path.write_text(json.dumps(metadata))
                audit_path = trace / "oracle/thread-state-accounting.json"
                audit_path.write_text(json.dumps(audit(trace)))
                unchanged_audit = audit_path.read_bytes()
                (result / "oracle").symlink_to("tao_trace/oracle", target_is_directory=True)
                for path, text in (
                    (result / "request.json", "{}"),
                    (trace / "trace.json", "{}"),
                    (trace / "manifest.txt", "fixture"),
                    (result / "oracle/kernel_events.json", "{}"),
                    (result / "oracle/native-summary-core0.json", "{}"),
                    (root / "fastsim", "binary-v1"),
                    (root / "config.cfg", "config-v1"),
                ):
                    path.write_text(text)
                args = SimpleNamespace(
                    fastsim=root / "fastsim", config=root / "config.cfg",
                    event_dictionary=EVENT_DICTIONARY, output_dir=root / "reports",
                    max_attempts=3,
                )
                case = {"case_id": "audited-case", "result_dir": str(result), "cores": 1}
                before = validation_fingerprint(case, args)
                self.assertEqual(before["current_time_accounting_evidence"]["active_status"], "available")
                report_path = args.output_dir / "cases/audited-case/validation.json"
                report_path.parent.mkdir(parents=True)
                report_path.write_text(json.dumps({
                    "schema": CASE_SCHEMA, "result_dir": str(result), "status": "passed",
                    "validation_fingerprint": before,
                }))
                self.assertIsNotNone(existing_report(case, args))

                if mutation == "metadata-read-failure":
                    metadata["read_failures"] = 1
                    metadata_path.write_text(json.dumps(metadata))
                else:
                    source = trace / metadata["source_file"]
                    data = bytearray(source.read_bytes())
                    # Ordinal7 has no scheduler event; its retirement flag
                    # still changes the independently owned macro denominator.
                    struct.pack_into("<H", data, 72 + 7 * 64 + 50, 0)
                    source.write_bytes(data)

                after = validation_fingerprint(case, args)
                self.assertEqual(audit_path.read_bytes(), unchanged_audit)
                self.assertNotEqual(before["sha256"], after["sha256"])
                self.assertEqual(after["current_time_accounting_evidence"]["active_status"], "unavailable-invalid-audit")
                self.assertIsNone(existing_report(case, args))

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
