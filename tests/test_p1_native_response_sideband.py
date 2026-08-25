#!/usr/bin/env python3
"""Regression tests for the P1 Ruby native-response sideband audit."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from audit_p1_native_response_sideband import (  # noqa: E402
    apply_collection_tolerance,
    audit_result,
    matrix_results,
)
from merge_kernel_events_oracle_v3 import merge  # noqa: E402
from test_p0_contract import row  # noqa: E402


def event(
    record: str,
    seq: int,
    source: str,
    responses: int,
    hits: int,
    misses: int,
) -> dict:
    return {
        "record": record,
        "core_id": 0,
        "thread_id": 7,
        "inst_seq_num": seq,
        "scope": "user",
        "attribution_source": source,
        "proxy_path_class": 0 if seq == 1 else 4,
        "line_requests": 1 if seq == 1 else 2,
        "native_response_count": responses,
        "native_external_hits": hits,
        "native_external_misses": misses,
        "native_coalesced": 0,
        "native_responder_machine_mask": 1,
        "native_responder_machine_unknown": 0,
    }


def event_v2(
    record: str,
    seq: int,
    source: str,
    admissions: int,
    responses: int,
    hits: int,
    misses: int,
    *,
    closed: bool,
    terminal: bool,
    reason_mask: int = 0,
    resolution_kind: str | None = None,
) -> dict:
    value = event(record, seq, source, responses, hits, misses)
    value.update(
        {
            "native_admission_count": admissions,
            "native_aliased_admissions": 0,
            "native_issuance_closed": closed,
            "native_terminal_no_ruby": terminal,
            "native_terminal_reason_mask": reason_mask,
        }
    )
    if resolution_kind is not None:
        value["resolution_kind"] = resolution_kind
    return value


def hierarchy(*, l1d_hit: int = 0, l2_miss: int = 0, llc_miss: int = 0,
              unique_fills: int = 0, ruby_memory_fetches: int = 0,
              memory_read_transactions: int = 0) -> dict:
    def level(hits: int = 0, tag_misses: int = 0) -> dict:
        return {
            "accesses": hits + tag_misses,
            "hits": hits,
            "tag_misses": tag_misses,
            "permission_upgrades": 0,
            "merged_misses": 0,
            "remote_supplies": 0,
        }

    return {
        "l1d": level(hits=l1d_hit),
        "l2": level(tag_misses=l2_miss),
        "llc": level(tag_misses=llc_miss),
        "unique_fills": unique_fills,
        "ruby_memory_fetches": ruby_memory_fetches,
        "memory_read_transactions": memory_read_transactions,
    }


class NativeResponseSidebandTest(unittest.TestCase):
    def test_matrix_accepts_only_explicit_successful_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory)
            result = matrix / "result"
            result.mkdir()
            status = {
                "tasks": {
                    "4c/workload": {
                        "sample": {
                            "status": "skipped",
                            "reason": "current successful result exists",
                            "result_dir": str(result),
                        }
                    }
                }
            }
            (matrix / "status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            self.assertEqual(list(matrix_results(matrix)), [result])
            status["tasks"]["4c/workload"]["sample"]["reason"] = "failed"
            (matrix / "status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "not a successful sample"):
                list(matrix_results(matrix))

    def make_result(self, root: Path, duplicate: bool = False) -> Path:
        result = root / "sample" / "cache" / "1c" / "workload" / "key" / "run"
        oracle = result / "oracle"
        oracle.mkdir(parents=True)
        (oracle / "kernel_events.json").write_text(
            json.dumps(merge([row()])), encoding="utf-8"
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
        rows = [
            {
                "schema": "taotrace-native-response-v1",
                "record": "metadata",
                "oracle_only": True,
                "fst_input": False,
            },
            event("commit", 1, "packet", 1, 1, 0),
            event("commit", 2, "fallback", 0, 0, 0),
            event("response", 2, "fallback", 1, 0, 1),
        ]
        if duplicate:
            rows.append(event("response", 2, "fallback", 1, 0, 1))
        rows.append(
            {
                "record": "summary",
                "committed_memory_uops": 2,
                "packet_source_uops": 1,
                "fallback_source_uops": 1,
                "response_at_commit_uops": 1,
                "late_response_uops": 1,
                "response_without_native_fact_uops": 0,
                "pending_without_response_uops": 0,
            }
        )
        (oracle / "native-response-core0.jsonl").write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_v2(self, root: Path, unresolved: bool = False) -> Path:
        result = root / "sample" / "cache" / "1c" / "workload" / "key" / "run"
        oracle = result / "oracle"
        oracle.mkdir(parents=True)
        (oracle / "kernel_events.json").write_text(
            json.dumps(merge([row()])), encoding="utf-8"
        )
        (result / "request.json").write_text(
            json.dumps(
                {
                    "gem5": {"binary_sha256": "def"},
                    "workload_selection": {"workload": "workload"},
                }
            ),
            encoding="utf-8",
        )
        rows = [
            {
                "schema": "taotrace-native-response-v2",
                "record": "metadata",
                "oracle_only": True,
                "fst_input": False,
                "target_stop": "committed-native-drain",
            },
            event_v2(
                "commit", 1, "packet", 1, 1, 1, 0,
                closed=True, terminal=False,
            ),
            event_v2(
                "commit", 2, "fallback", 0, 0, 0, 0,
                closed=False, terminal=False,
            ),
        ]
        if not unresolved:
            rows.append(
                event_v2(
                    "resolution", 2, "fallback", 0, 0, 0, 0,
                    closed=False, terminal=True, reason_mask=4,
                    resolution_kind="no_ruby_terminal",
                )
            )
        rows.append(
            {
                "record": "summary",
                "committed_memory_uops": 2,
                "packet_source_uops": 1,
                "fallback_source_uops": 1,
                "response_at_commit_uops": 1,
                "terminal_at_commit_uops": 0,
                "late_response_uops": 0,
                "late_terminal_uops": 0 if unresolved else 1,
                "response_without_native_fact_uops": 0,
                "unresolved_lifecycle_uops": 1 if unresolved else 0,
                "target_drain_polls": 3,
            }
        )
        (oracle / "native-response-core0.jsonl").write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_v3(self, root: Path, corrupt: bool = False) -> Path:
        result = self.make_result_v2(root)
        sideband = result / "oracle" / "native-response-core0.jsonl"
        rows = [json.loads(line) for line in sideband.read_text().splitlines()]
        rows[0].update(
            {
                "schema": "taotrace-native-response-v3",
                "hierarchy_source": "ruby-slicc-controller-actions",
                "ruby_memory_fetch_semantics": (
                    "l2-to-directory-fetch-not-dram-transaction"
                ),
            }
        )
        rows[1]["native_hierarchy"] = hierarchy(l1d_hit=1)
        rows[2]["native_hierarchy"] = hierarchy()
        rows[3]["native_hierarchy"] = hierarchy()
        if corrupt:
            rows[1]["native_hierarchy"]["l1d"]["accesses"] = 2
        sideband.write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_v4(self, root: Path) -> Path:
        result = self.make_result_v3(root)
        sideband = result / "oracle" / "native-response-core0.jsonl"
        rows = [json.loads(line) for line in sideband.read_text().splitlines()]
        rows[0].update(
            {
                "schema": "taotrace-native-response-v4",
                "memory_read_transaction_semantics": (
                    "accepted-ruby-memory-port-read-packet"
                ),
                "hierarchy_identity_transport": (
                    "context-id-inst-seq-num-no-request-retention"
                ),
            }
        )
        rows[1]["native_hierarchy"]["memory_read_transactions"] = 1
        sideband.write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_v5(self, root: Path, missing_lookup: bool = False) -> Path:
        result = self.make_result_v4(root)
        sideband = result / "oracle" / "native-response-core0.jsonl"
        rows = [json.loads(line) for line in sideband.read_text().splitlines()]
        rows[0].update(
            {
                "schema": "taotrace-native-response-v5",
                "hierarchy_request_semantics": (
                    "sequencer-mandatory-queue-enqueue"
                ),
            }
        )
        for value in rows[1:-1]:
            value["native_hierarchy_request_count"] = 0
        # An initially aliased write can later be reissued and perform a real
        # lookup.  v5 records that enqueue directly instead of guessing from
        # the aliased-admission bit.
        rows[1]["native_aliased_admissions"] = 1
        rows[1]["native_hierarchy_request_count"] = 1
        if missing_lookup:
            rows[1]["native_hierarchy"] = hierarchy()
        sideband.write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_v6(self, root: Path) -> Path:
        result = self.make_result_v5(root)
        sideband = result / "oracle" / "native-response-core0.jsonl"
        rows = [json.loads(line) for line in sideband.read_text().splitlines()]
        rows[0].update(
            {
                "schema": "taotrace-native-response-v6",
                "measurement_boundary_semantics": (
                    "preboundary-inflight-ledger-retire-cleanup"
                ),
            }
        )
        sideband.write_text(
            "\n".join(json.dumps(value) for value in rows) + "\n",
            encoding="utf-8",
        )
        return result

    def make_result_summary(self, root: Path, keep_jsonl: bool = False) -> Path:
        result = self.make_result_v6(root)
        audited = audit_result(result)
        core = audited["per_core"][0]
        scalar_fields = (
            "committed_memory_uops",
            "packet_source_uops",
            "fallback_source_uops",
            "response_at_commit_uops",
            "terminal_at_commit_uops",
            "late_response_uops",
            "late_terminal_uops",
            "pending_without_response_uops",
            "response_without_native_fact_uops",
            "native_outcome_uops",
            "native_no_ruby_uops",
            "native_admission_fragments",
            "native_hierarchy_request_fragments",
            "native_response_fragments",
            "native_external_hit_fragments",
            "native_external_miss_fragments",
            "native_coalesced_fragments",
            "native_responder_machine_unknown",
            "native_hierarchy_complete_uops",
            "native_hierarchy_incomplete_uops",
            "native_outcome_coverage",
            "target_drain_polls",
        )
        populations = json.loads(json.dumps(core["native_ruby_pmu_by_scope"]))
        for population in populations.values():
            population["ruby_aliased_admission_fragments"] = 0
        populations["user"]["ruby_aliased_admission_fragments"] = 1
        summary = {
            "schema": "taotrace-native-summary-v1",
            "record": "summary",
            "oracle_only": True,
            "fst_input": False,
            "core_id": 0,
            "source_sideband_schema": "taotrace-native-response-v6",
            "lifecycle_join": "context-id-inst-seq-num",
            "target_stop": "committed-native-drain",
            "hierarchy_source": "ruby-slicc-controller-actions",
            "hierarchy_identity_transport": (
                "context-id-inst-seq-num-no-request-retention"
            ),
            "hierarchy_request_semantics": (
                "sequencer-mandatory-queue-enqueue"
            ),
            "measurement_boundary_semantics": (
                "preboundary-inflight-ledger-retire-cleanup"
            ),
            "ruby_memory_fetch_semantics": (
                "l2-to-directory-fetch-not-dram-transaction"
            ),
            "memory_read_transaction_semantics": (
                "accepted-ruby-memory-port-read-packet"
            ),
            "full_jsonl_enabled": keep_jsonl,
            **{field: core[field] for field in scalar_fields},
            "terminal_reason_counts": core["terminal_reason_counts"],
            "proxy_native_confusion": core["proxy_native_confusion"],
            "native_ruby_pmu_by_scope": populations,
            "anomalies": {
                "limit": 32,
                "retained": 0,
                "dropped": 0,
                "samples": [],
            },
        }
        (result / "oracle" / "native-summary-core0.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        sideband = result / "oracle" / "native-response-core0.jsonl"
        if keep_jsonl:
            sideband.write_text("this must never be scanned\n", encoding="utf-8")
        else:
            sideband.unlink()
        return result

    def test_complete_join_is_conserved_but_not_formal_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        self.assertTrue(result["target_drain_complete"])
        self.assertFalse(result["formal_comparable"])
        self.assertFalse(result["accuracy_metrics_emitted"])
        aggregate = result["aggregate"]
        self.assertEqual(aggregate["native_outcome_uops"], 2)
        self.assertEqual(aggregate["native_outcome_coverage"], 1.0)
        self.assertEqual(
            aggregate["proxy_native_confusion"]["proxy_l1"][
                "native_hit_fragments"
            ],
            1,
        )
        self.assertEqual(
            aggregate["proxy_native_confusion"]["proxy_dram"][
                "native_miss_fragments"
            ],
            1,
        )

    def test_duplicate_response_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_dir = self.make_result(Path(directory), duplicate=True)
            with self.assertRaisesRegex(ValueError, "duplicate response identity"):
                audit_result(result_dir)

    def test_v2_lifecycle_distinguishes_no_ruby_and_closes_drain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v2(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        self.assertTrue(result["target_drain_complete"])
        aggregate = result["aggregate"]
        self.assertEqual(aggregate["native_outcome_uops"], 1)
        self.assertEqual(aggregate["native_no_ruby_uops"], 1)
        self.assertEqual(aggregate["native_admission_fragments"], 1)
        self.assertEqual(aggregate["native_response_fragments"], 1)
        self.assertEqual(aggregate["terminal_reason_counts"]["store_forward"], 1)

    def test_v2_unresolved_lifecycle_fails_target_drain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(
                self.make_result_v2(Path(directory), unresolved=True)
            )
        self.assertFalse(result["structural_conservation"])
        self.assertFalse(result["target_drain_complete"])

    def test_v3_hierarchy_is_conserved_and_aggregated_by_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v3(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        population = result["native_ruby_pmu_by_scope"]["user"]
        self.assertEqual(population["memory_uops"], 2)
        self.assertEqual(population["ruby_admission_fragments"], 1)
        self.assertEqual(population["no_ruby_uops"], 1)
        self.assertEqual(population["hierarchy"]["l1d"]["accesses"], 1)
        self.assertEqual(population["hierarchy"]["l1d"]["hits"], 1)
        self.assertEqual(population["hierarchy_complete_uops"], 2)
        self.assertEqual(population["hierarchy_incomplete_uops"], 0)
        self.assertFalse(result["native_hierarchy_semantic_comparable"])
        self.assertFalse(result["hardware_pmu_formal"])

    def test_v3_hierarchy_population_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_dir = self.make_result_v3(Path(directory), corrupt=True)
            with self.assertRaisesRegex(ValueError, "accesses=2, outcomes=1"):
                audit_result(result_dir)

    def test_v4_memory_read_transactions_are_separate_population(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v4(Path(directory)))
        population = result["native_ruby_pmu_by_scope"]["user"]
        self.assertEqual(
            population["hierarchy"]["memory_read_transactions"], 1
        )
        self.assertEqual(population["hierarchy"]["ruby_memory_fetches"], 0)

    def test_v5_uses_exact_hierarchy_enqueue_not_alias_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v5(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        population = result["native_ruby_pmu_by_scope"]["user"]
        self.assertEqual(population["ruby_admission_fragments"], 1)
        self.assertEqual(population["ruby_hierarchy_request_fragments"], 1)
        self.assertEqual(population["hierarchy_complete_uops"], 2)
        self.assertEqual(population["hierarchy_incomplete_uops"], 0)
        self.assertFalse(result["native_hierarchy_semantic_comparable"])
        self.assertFalse(result["hardware_pmu_formal"])

    def test_v5_missing_slicc_outcome_fails_structural_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(
                self.make_result_v5(Path(directory), missing_lookup=True)
            )
        self.assertFalse(result["structural_conservation"])
        self.assertEqual(
            result["aggregate"]["native_hierarchy_incomplete_uops"], 1
        )
        self.assertTrue(
            result["structural_conservation_without_hierarchy_completion"]
        )
        self.assertEqual(result["hierarchy_gap_ratio"], 1.0)
        self.assertFalse(apply_collection_tolerance(result, 0.02))
        self.assertTrue(apply_collection_tolerance(result, 1.0))
        self.assertFalse(result["native_hierarchy_semantic_comparable"])

    def test_scope_metrics_save_both_cpi_denominators_and_native_pmu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v5(Path(directory)))
        self.assertEqual(set(result["scope_metrics"]), {
            "user", "user_plus_kernel"
        })
        for scope in result["scope_metrics"].values():
            self.assertIn("cycles_per_user_uop", scope)
            self.assertIn("perf_like_cpi", scope)
            self.assertIn("p0_pmu", scope)
            self.assertIn("native_ruby_pmu", scope)
        self.assertEqual(
            result["scope_metrics"]["user"]["native_ruby_pmu"],
            result["scope_metrics"]["user_plus_kernel"]["native_ruby_pmu"],
        )

    def test_v6_declares_preboundary_inflight_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_v6(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        self.assertEqual(result["sideband_schema"], "taotrace-native-response-v6")
        self.assertTrue(result["native_hierarchy_semantic_comparable"])
        self.assertFalse(result["hardware_pmu_formal"])

    def test_online_summary_replaces_full_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(self.make_result_summary(Path(directory)))
        self.assertTrue(result["structural_conservation"])
        self.assertTrue(result["target_drain_complete"])
        self.assertEqual(result["sideband_schema"], "taotrace-native-summary-v1")
        self.assertTrue(result["native_hierarchy_semantic_comparable"])
        self.assertFalse(result["hardware_pmu_formal"])
        self.assertTrue(result["per_core"][0]["summary_only"])
        self.assertEqual(
            result["native_ruby_pmu_by_scope"]["user"]["hierarchy"]["l1d"][
                "hits"
            ],
            1,
        )

    def test_online_summary_is_preferred_when_debug_jsonl_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_result(
                self.make_result_summary(Path(directory), keep_jsonl=True)
            )
        self.assertTrue(result["structural_conservation"])
        self.assertFalse(result["per_core"][0]["summary_only"])


if __name__ == "__main__":
    unittest.main()
