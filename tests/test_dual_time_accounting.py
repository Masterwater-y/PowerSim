#!/usr/bin/env python3
"""Active CPI must never inherit elapsed capacity when state is unknown."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import run_native_kernel_fastsim_validation as validator


def accounting(schema="fastsim-time-accounting-v1", **changes):
    return {
        "schema": schema, "scope": "user-plus-kernel",
        "active_scope": "cpu-unhalted", "active_status": "available",
        "active_source": "synthetic-test-exact-state", "active_cycles": 30,
        "inactive_cycles": 10, "unknown_cycles": 0,
        "elapsed_status": "available", "elapsed_reference_cycles": 40,
        "elapsed_time_seconds": 0.04, "reference_frequency_hz": 1000,
        **changes,
    }


def documents():
    return ({
        "measurement_scope": "user-plus-kernel",
        "scope_metrics": {"sum_core_cycles": 999,
                          "perf_like_cpi_denominator_instructions": 10,
                          "perf_like_cpi": 99.9,
                          "pmu": {"retired_instructions": 10}},
        "time_accounting": accounting(),
    }, {"aggregate": {"perf_like_cpi_user_plus_kernel": 99,
                       "user_plus_kernel_retired_instructions": 20},
         "time_accounting": accounting("gem5-time-accounting-v1",
                                        active_cycles=40)})


def cpl(core, first=100, last=500):
    return {"core_id": core, "first_tick": first, "last_tick": last,
            "clock_period_ticks": 10, "measured_ticks": last-first,
            "span_ticks": last-first, "tick_sum_matches": 1,
            "user_ticks": last-first-100, "syscall_kernel_ticks": 0,
            "page_fault_kernel_ticks": 0, "irq_kernel_ticks": 0,
            "scheduler_kernel_ticks": 0, "idle_ticks": 100,
            "unknown_kernel_ticks": 0}


def oracle_clock(core, **changes):
    result=accounting("gem5-time-accounting-v1",core_id=core,oracle_only=True,
                      active_scope="workload-task",active_status="proxy-scheduler-boundary",
                      active_source="kernel-hook-observations",active_cycles=None,
                      inactive_cycles=None,unknown_cycles=None,common_start_tick=100,
                      common_end_tick=500,clock_period_ticks=10,ticks_per_second=10000,
                      elapsed_source="common-serial-marker-and-first-core-target")
    result.update(changes)
    return result


class DualTimeAccountingTest(unittest.TestCase):
    def test_explicit_failed_collection_cannot_be_reported_as_available(self):
        for field in ("read_failures","identity_failures","unretained_hook_events"):
            simulation,oracle=documents()
            for record in (simulation["time_accounting"],oracle["time_accounting"]):
                record.update(active_scope="workload-task",ownership_status="complete",task_ids=[101],
                              task_macro_instructions=10,accounting_contract="task-oncpu-switch-entry-v1")
            oracle["time_accounting"][field]=1
            self.assertEqual(self.compare(simulation,oracle)["cpi"]["status"],"unavailable")

    def test_loader_reads_independent_certified_audit_before_proxy_clocks(self):
        from audit_thread_state_accounting import audit
        from test_thread_state_accounting_oracle import fixture
        from test_thread_state_graph import certify
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace)
            metadata=trace/"thread-state-metadata-core0.json"
            metadata.write_text(json.dumps(certify(json.loads(metadata.read_text()))))
            audited=audit(trace)
            (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audited))
            loaded=validator.load_gem5_time_accounting(trace,{},cores=1)
            self.assertEqual(loaded["active_status"],"available")
            self.assertEqual(loaded["active_cycles"],3)
            audited["common_end_tick"]=161
            (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audited))
            loaded=validator.load_gem5_time_accounting(trace,{},cores=1)
            self.assertNotEqual(loaded["active_status"],"available")
            self.assertIsNone(loaded["active_cycles"])
            audited["common_end_tick"]=160
            audited["active_cycles"]=4
            (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audited))
            loaded=validator.load_gem5_time_accounting(trace,{},cores=1)
            self.assertNotEqual(loaded["active_status"],"available")

    def test_cached_active_audit_is_bound_to_current_functional_and_source_evidence(self):
        import struct
        from audit_thread_state_accounting import audit
        from test_thread_state_accounting_oracle import fixture
        from test_thread_state_graph import certify
        for mutation in ("read-failure","closure-removed","sidecar","header","non-anchor-macro","observation","source-removed"):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);fixture(trace)
                metadata=trace/"thread-state-metadata-core0.json"
                meta=certify(json.loads(metadata.read_text()));metadata.write_text(json.dumps(meta))
                (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audit(trace)))
                if mutation=="read-failure":
                    meta["read_failures"]=1;metadata.write_text(json.dumps(meta))
                elif mutation=="closure-removed":
                    for key in ("accounting_contract","common_cut_id","kernel_stream_complete","common_cut_complete","owner_continuity_to_common_cut"):
                        meta.pop(key)
                    metadata.write_text(json.dumps(meta))
                elif mutation=="sidecar":
                    path=trace/"thread-state-core0.jsonl";path.write_text(path.read_text().replace('"cause_status": "unknown"','"cause_status": "changed"'))
                elif mutation=="observation":
                    path=trace/"oracle/thread-state-observation.jsonl";path.write_text(path.read_text().replace('"commit_tick": 120','"commit_tick": 121'))
                elif mutation=="source-removed":(trace/meta["source_file"]).unlink()
                else:
                    path=trace/meta["source_file"];data=bytearray(path.read_bytes())
                    if mutation=="header":struct.pack_into("<Q",data,24,9)
                    else:struct.pack_into("<H",data,72+7*64+50,0)
                    path.write_bytes(data)
                loaded=validator.load_gem5_time_accounting(trace,{},cores=1)
                self.assertNotEqual(loaded["active_status"],"available")
                self.assertIsNone(loaded["active_cycles"])

    def test_loader_binds_case_root_oracle_to_tao_trace_functional_sources(self):
        from audit_thread_state_accounting import audit
        from test_thread_state_accounting_oracle import fixture
        from test_thread_state_graph import certify
        with tempfile.TemporaryDirectory() as directory:
            case=Path(directory);trace=case/"tao_trace";trace.mkdir();fixture(trace)
            metadata=trace/"thread-state-metadata-core0.json"
            meta=certify(json.loads(metadata.read_text()));metadata.write_text(json.dumps(meta))
            (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audit(trace)))
            (case/"oracle").symlink_to("tao_trace/oracle",target_is_directory=True)
            for source in (case,trace):
                loaded=validator.load_gem5_time_accounting(source,{},cores=1)
                self.assertEqual(loaded["active_status"],"available",loaded.get("active_reason"))
                self.assertEqual(loaded["active_cycles"],3)
                self.assertEqual(loaded["task_macro_instructions"],2)
            meta["read_failures"]=1;metadata.write_text(json.dumps(meta))
            loaded=validator.load_gem5_time_accounting(case,{},cores=1)
            self.assertNotEqual(loaded["active_status"],"available")
            self.assertIsNone(loaded["active_cycles"])

    def test_fabricated_active_audit_without_functional_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);(trace/"oracle").mkdir()
            audited={**oracle_clock(0),**accounting("gem5-time-accounting-v1",oracle_only=True,valid=True,
                       active_scope="workload-task",ownership_status="complete",task_ids=[101],participants=[0],
                       per_core=[{"core_id":0,"task_macro_instructions":10}],task_macro_instructions=10,
                       accounting_contract="task-oncpu-switch-entry-v1")}
            (trace/"oracle/time-accounting-core0.json").write_text(json.dumps(oracle_clock(0)))
            (trace/"oracle/thread-state-accounting.json").write_text(json.dumps(audited))
            loaded=validator.load_gem5_time_accounting(trace,{},cores=1)
            self.assertNotEqual(loaded["active_status"],"available")

    def test_task_contract_must_be_present_supported_and_compatible(self):
        for contract in (None,"perf-unhalted-v1","task-oncpu-switch-entry-v2"):
            with self.subTest(contract=contract):
                simulation,oracle=documents()
                for record in (simulation["time_accounting"],oracle["time_accounting"]):
                    record.update(active_scope="workload-task",ownership_status="complete",
                                  task_ids=[101],task_macro_instructions=10,
                                  accounting_contract="task-oncpu-switch-entry-v1",
                                  perf_equivalence="unverified")
                if contract is None:oracle["time_accounting"].pop("accounting_contract")
                else:oracle["time_accounting"]["accounting_contract"]=contract
                result=self.compare(simulation,oracle)["cpi"]
                self.assertEqual(result["status"],"unavailable")
                self.assertIsNone(result["absolute_error"])

    def compare(self, simulation, oracle):
        method = getattr(validator, "build_time_comparison", None)
        self.assertTrue(callable(method), "dual-time comparison is missing")
        return method(simulation, oracle)

    def gem5(self, rows, **kwargs):
        method = getattr(validator, "build_gem5_time_accounting", None)
        self.assertTrue(callable(method), "elapsed oracle helper is missing")
        return method(rows, ticks_per_second=10000, cores=2, **kwargs)

    def test_active_cpi_uses_own_macro_denominator_and_not_elapsed(self):
        result = self.compare(*documents())
        self.assertEqual(result["cpi"]["status"], "scored-active")
        self.assertEqual(result["cpi"]["fastsim"], 3)
        self.assertEqual(result["cpi"]["gem5"], 2)
        self.assertEqual(result["cpi"]["absolute_error"], 1)
        self.assertEqual(result["cpi"]["relative_error"], 0.5)
        self.assertFalse(result["cpi"]["instruction_population_match"])

    def test_unavailable_active_keeps_elapsed_and_never_falls_back(self):
        simulation, oracle = documents()
        simulation["time_accounting"].update(active_cycles=None,
                                            active_status="unavailable-os-state")
        result = self.compare(simulation, oracle)
        for field in ("fastsim", "gem5", "absolute_error", "relative_error"):
            self.assertIsNone(result["cpi"][field])
        self.assertEqual(result["cpi"]["status"], "unavailable")
        self.assertEqual(result["elapsed"]["status"], "scored")
        self.assertEqual(result["elapsed"]["fastsim_seconds"], 0.04)

    def test_scope_source_unknown_and_invalid_counts_fail_closed(self):
        for replacement in ({"active_scope": "workload-task"},
                            {"scope": "user"}, {"unknown_cycles": 1},
                            {"active_source": ""},
                            {"active_status": "proxy-cpl-classifier"},
                            {"active_cycles": float("nan")},
                            {"active_cycles": -1}, {"active_cycles": True},
                            {"schema": "unrecognized-v1"}):
            with self.subTest(replacement=replacement):
                simulation, oracle = documents()
                simulation["time_accounting"].update(replacement)
                result = self.compare(simulation, oracle)
                self.assertIsNone(result["cpi"]["absolute_error"])
                self.assertEqual(result["cpi"]["status"], "unavailable")

    def test_zero_denominator_is_unavailable(self):
        simulation, oracle = documents()
        simulation["scope_metrics"]["perf_like_cpi_denominator_instructions"] = 0
        self.assertIsNone(self.compare(simulation, oracle)["cpi"]["absolute_error"])

    def test_task_active_requires_complete_matching_task_population(self):
        simulation, oracle = documents()
        for record in (simulation["time_accounting"], oracle["time_accounting"]):
            record["active_scope"] = "workload-task"
        self.assertIsNone(self.compare(simulation, oracle)["cpi"]["absolute_error"])
        for record,count in ((simulation["time_accounting"],10), (oracle["time_accounting"],20)):
            record.update(task_ids=[101, 102], ownership_status="complete",task_macro_instructions=count,
                          accounting_contract="task-oncpu-switch-entry-v1")
        self.assertEqual(self.compare(simulation, oracle)["cpi"]["absolute_error"], 1)
        oracle["time_accounting"]["task_ids"] = [101, 103]
        self.assertIsNone(self.compare(simulation, oracle)["cpi"]["absolute_error"])

    def test_task_cpi_uses_each_task_macro_population_instead_of_physical_totals(self):
        simulation,oracle=documents()
        simulation["scope_metrics"].update(perf_like_cpi_denominator_instructions=1000,
                                           pmu={"retired_instructions":1000})
        oracle["aggregate"]["user_plus_kernel_retired_instructions"]=1000
        for record,cycles,count in ((simulation["time_accounting"],300,100),
                                    (oracle["time_accounting"],400,200)):
            record.update(active_scope="workload-task",task_ids=[101,102],
                          ownership_status="complete",active_cycles=cycles,
                          task_macro_instructions=count,accounting_contract="task-oncpu-switch-entry-v1",
                          perf_equivalence="unverified")
        result=self.compare(simulation,oracle)["cpi"]
        self.assertEqual(result["status"],"scored-active")
        self.assertEqual(result["fastsim"],3)
        self.assertEqual(result["gem5"],2)
        self.assertEqual(result["absolute_error"],1)
        self.assertFalse(result["instruction_population_match"])
        self.assertEqual(result["perf_equivalence"],"unverified")

    def test_available_task_cycles_without_valid_task_macro_denominators_are_unavailable(self):
        for invalid in (None,0,-1,True,100.5,float("nan"),float("inf")):
            for provider in ("fastsim","gem5"):
                with self.subTest(invalid=invalid,provider=provider):
                    simulation,oracle=documents()
                    for record in (simulation["time_accounting"],oracle["time_accounting"]):
                        record.update(active_scope="workload-task",task_ids=[101],
                                      ownership_status="complete",task_macro_instructions=100,
                                      accounting_contract="task-oncpu-switch-entry-v1")
                    record=simulation["time_accounting"] if provider=="fastsim" else oracle["time_accounting"]
                    if invalid is None:del record["task_macro_instructions"]
                    else:record["task_macro_instructions"]=invalid
                    result=self.compare(simulation,oracle)["cpi"]
                    self.assertEqual(result["status"],"unavailable")
                    self.assertIsNone(result["absolute_error"])

    def test_loader_reads_run_tick_frequency_without_assuming_one_terahertz(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory)
            (result / "oracle").mkdir()
            (result / "oracle/cpl_class.jsonl").write_text("\n".join(
                json.dumps(cpl(core)) for core in range(2)))
            (result / "stats.txt").write_text("simFreq 10000 # ticks per second\n")
            loaded = validator.load_gem5_time_accounting(result, {}, cores=2)
            self.assertEqual(loaded["elapsed_time_seconds"], 0.04)
            (result / "stats.txt").unlink()
            loaded = validator.load_gem5_time_accounting(result, {}, cores=2)
            self.assertIsNone(loaded["elapsed_time_seconds"])

    def test_new_per_core_elapsed_oracle_combines_one_global_window(self):
        assemble=getattr(validator,"assemble_gem5_time_accounting",None)
        self.assertTrue(callable(assemble),"new per-core elapsed assembler missing")
        result=assemble([oracle_clock(1),oracle_clock(0)],cores=2)
        self.assertEqual(result["elapsed_time_seconds"],0.04)
        self.assertEqual(result["elapsed_reference_cycles"],40)
        self.assertEqual(result["active_status"],"proxy-scheduler-boundary")
        self.assertIsNone(result["active_cycles"])

    def test_new_oracle_skew_missing_and_inconsistent_clocks_fail_closed(self):
        assemble=getattr(validator,"assemble_gem5_time_accounting",None)
        self.assertTrue(callable(assemble),"new per-core elapsed assembler missing")
        variants=[[oracle_clock(0)],
                  [oracle_clock(0),oracle_clock(1,common_start_tick=110)],
                  [oracle_clock(0),oracle_clock(1,common_end_tick=510)],
                  [oracle_clock(0),oracle_clock(1,ticks_per_second=100000)],
                  [oracle_clock(0),oracle_clock(1,elapsed_time_seconds=0.4)],
                  [oracle_clock(0),oracle_clock(1,oracle_only=False)],
                  [oracle_clock(0),oracle_clock(1,scope="user")]]
        for rows in variants:
            with self.subTest(rows=rows):
                result=assemble(rows,cores=2)
                self.assertIsNone(result["elapsed_time_seconds"])
                self.assertNotEqual(result["elapsed_status"],"available")

    def test_partial_new_elapsed_files_never_fall_back_to_legacy_cpl(self):
        with tempfile.TemporaryDirectory() as directory:
            result=Path(directory);oracle=result/"oracle";oracle.mkdir()
            (oracle/"time-accounting-core0.json").write_text(json.dumps(oracle_clock(0)))
            (oracle/"cpl_class.jsonl").write_text("\n".join(json.dumps(cpl(core)) for core in range(2)))
            (result/"stats.txt").write_text("simFreq 10000\n")
            loaded=validator.load_gem5_time_accounting(result,{},cores=2)
            self.assertIsNone(loaded["elapsed_time_seconds"])
            (oracle/"time-accounting-core1.json").write_text(json.dumps(oracle_clock(1)))
            loaded=validator.load_gem5_time_accounting(result,{},cores=2)
            self.assertEqual(loaded["elapsed_time_seconds"],0.04)

    def test_explicit_missing_oracle_active_does_not_score_legacy_reference(self):
        simulation, oracle = documents()
        del oracle["time_accounting"]
        self.assertIsNone(self.compare(simulation, oracle)["cpi"]["absolute_error"])

    def test_legacy_outputs_are_explicitly_diagnostic(self):
        simulation, oracle = documents()
        del simulation["time_accounting"]
        del oracle["time_accounting"]
        result = self.compare(simulation, oracle)
        self.assertEqual(result["cpi"]["status"], "legacy-diagnostic")
        self.assertEqual(result["cpi"]["fastsim"], 99.9)
        self.assertAlmostEqual(result["cpi"]["absolute_error"], 0.9)

    def test_elapsed_is_one_common_window_not_summed_cpu_capacity(self):
        result = self.gem5([cpl(0), cpl(1)])
        self.assertEqual(result["elapsed_status"], "available")
        self.assertEqual(result["elapsed_reference_cycles"], 40)
        self.assertEqual(result["elapsed_time_seconds"], 0.04)
        self.assertEqual(result["reference_frequency_hz"], 1000)
        self.assertIsNone(result["active_cycles"])
        self.assertEqual(result["active_status"], "proxy-cpl-classifier")

    def test_skewed_starts_require_explicit_common_start(self):
        rows = [cpl(0), cpl(1, first=110)]
        result = self.gem5(rows)
        self.assertEqual(result["elapsed_status"], "unavailable-common-start")
        self.assertIsNone(result["elapsed_time_seconds"])
        result = self.gem5(rows, common_start_tick=90, common_end_tick=500)
        self.assertEqual(result["elapsed_time_seconds"], 0.041)

    def test_missing_core_skewed_end_or_tick_nonconservation_fail_closed(self):
        for rows in ([cpl(0)], [cpl(0), cpl(1, last=490)],
                     [cpl(0), {**cpl(1), "user_ticks": 1}],
                     [cpl(0), {**cpl(1), "clock_period_ticks": 20}]):
            with self.subTest(rows=rows):
                result = self.gem5(rows)
                self.assertNotEqual(result["elapsed_status"], "available")
                self.assertIsNone(result["elapsed_time_seconds"])

    def test_elapsed_inconsistent_frequency_and_scope_are_not_scored(self):
        for fields in ({"elapsed_time_seconds": 0.4}, {"scope": "user"},
                       {"reference_frequency_hz": 0}):
            simulation, oracle = documents()
            simulation["time_accounting"].update(fields)
            self.assertIsNone(self.compare(simulation, oracle)["elapsed"]["absolute_error_seconds"])

    def test_summary_separates_active_and_legacy_coverage_and_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            reports = [
                {"case_id": "active", "cpi": {"status": "scored-active", "fastsim": 3,
                    "gem5": 2, "absolute_relative_error": 0.5, "absolute_error": 1},
                 "elapsed": {"status": "scored", "fastsim_seconds": 0.04,
                    "gem5_seconds": 0.02, "absolute_error_seconds": 0.02}},
                {"case_id": "legacy", "cpi": {"status": "legacy-diagnostic", "fastsim": 99,
                    "gem5": 2, "absolute_relative_error": 48.5, "absolute_error": 97}},
                {"case_id": "unknown", "cpi": {"status": "unavailable", "fastsim": None,
                    "gem5": None, "absolute_relative_error": None, "absolute_error": None}},
            ]
            discovered = []
            for report in reports:
                report.update(schema=validator.CASE_SCHEMA, status="passed", cores=2,
                              workload="fixture", result_dir="/result/"+report["case_id"])
                path = output / "cases" / report["case_id"]
                path.mkdir(parents=True)
                (path / "validation.json").write_text(json.dumps(report))
                discovered.append({"case_id": report["case_id"], "result_dir": report["result_dir"]})
            summary = validator.write_summary(SimpleNamespace(output_dir=output,
                expected_cases=3, max_attempts=3), discovered)
            self.assertEqual(summary["active_cpi_accuracy"]["cases"], 1)
            self.assertEqual(summary["active_cpi_accuracy"]["cpi_mae"], 1)
            self.assertEqual(summary["legacy_cpi_accuracy"]["cases"], 1)
            self.assertEqual(summary["cpi_unavailable_cases"], 1)
            self.assertEqual(summary["elapsed_accuracy"]["cases"], 1)
            self.assertEqual(summary["elapsed_accuracy"]["mae_seconds"], 0.02)
            self.assertIn("elapsed_absolute_error_seconds", (output / "summary.csv").read_text())
            self.assertIn("active_scope", (output / "summary.csv").read_text())
            self.assertIn("fastsim_active_source", (output / "summary.csv").read_text())
            self.assertIn("legacy-diagnostic", (output / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
