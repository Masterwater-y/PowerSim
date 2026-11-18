import importlib.util
from pathlib import Path
import sys
import json
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import analyze_load_lifecycle as audit


class LoadLifecycleAuditTests(unittest.TestCase):
    def test_native_footer_is_not_a_member_and_population_is_checked(self):
        row = dict(record="commit", core_id=0, thread_id=1, inst_seq_num=42,
                   scope="user", native_admission_count=0, native_response_count=0)
        metadata = dict(record="metadata", oracle_only=True, fst_input=False,
                        schema="taotrace-native-response-v8",
                        lifecycle_tick_semantics="sequencer-acceptance-and-hit-callback-gem5-tick")
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            path = Path(directory) / "native.jsonl"
            path.write_text(json.dumps(metadata)+"\n"+json.dumps(row) + "\n" + json.dumps(
                dict(record="summary", committed_memory_uops=1)) + "\n")
            self.assertEqual(len(audit.load_native(path, 0)), 1)
            path.write_text(json.dumps(metadata)+"\n"+json.dumps(row) + "\n" + json.dumps(
                dict(record="summary", committed_memory_uops=2)) + "\n")
            with self.assertRaisesRegex(ValueError, "population"):
                audit.load_native(path, 0)
            path.write_text(json.dumps(row)+"\n"+json.dumps(
                dict(record="summary", committed_memory_uops=1))+"\n")
            with self.assertRaisesRegex(ValueError, "metadata"):
                audit.load_native(path, 0)

    def test_iq_core_sequence_pc_and_actual_release(self):
        insert = audit.parse_iq_event(
            "  100: board.processor.switch0.core.iq: Adding instruction "
            "[sn:42] PC (0x405c60=>0x405c63).(0=>1) to the IQ.\n"
        )
        release = audit.parse_iq_event(
            "  180: board.processor.switch0.core.iq: Completing mem instruction "
            "PC: (0x405c60=>0x405c63).(0=>1) [sn:42]\n"
        )
        self.assertEqual(insert, dict(core=0, sequence=42, pc=0x405C60,
                                      tick=100, kind="insert"))
        self.assertEqual(release["kind"], "release")
        self.assertEqual(release["tick"], 180)

    def test_half_open_overlap_and_zero_duration(self):
        result = audit.overlap_summary([(0, 10), (5, 15), (10, 20), (10, 10)], 0, 20)
        self.assertEqual(result["area"], 30)
        self.assertEqual(result["peak"], 2)
        self.assertEqual(result["mean"], 1.5)

    def test_issue_attempt_is_distinct_from_iq_release(self):
        event = audit.parse_iq_event(
            "  140: board.processor.switch0.core.iq: Thread 0: Issuing instruction "
            "PC (0x405c60=>0x405c63).(0=>1) [sn:42]\n"
        )
        self.assertEqual(event["kind"], "issue")
        self.assertEqual(event["sequence"], 42)
        self.assertEqual(event["tick"], 140)

    def test_missing_boundary_iq_insert_is_unknown_not_zero(self):
        functional = [dict(source_sequence=7, pc=0x405C60, line=123, flags=2)]
        native = [dict(core_id=0, thread_id=8, inst_seq_num=42,
                       native_first_admission_tick=100, native_last_response_tick=180,
                       native_admission_count=1, native_response_count=1,
                       native_coalesced=1, native_hierarchy_request_count=0)]
        rows = audit.join_members(functional, native, {}, core=0)
        self.assertIsNone(rows[0]["iq_lifetime_ticks"])
        self.assertEqual(rows[0]["request_lifetime_ticks"], 80)
        self.assertEqual(rows[0]["service_role"], "merged")

    def test_population_and_pc_mismatch_reject(self):
        functional = [dict(source_sequence=7, pc=0x405C60, line=123, flags=2)]
        with self.assertRaisesRegex(ValueError, "population"):
            audit.join_members(functional, [], {}, core=0)
        native = [dict(core_id=0, thread_id=8, inst_seq_num=42,
                       native_admission_count=0, native_response_count=0)]
        iq = {(0, 42): dict(insert=100, release=180, pc=0x405C65)}
        with self.assertRaisesRegex(ValueError, "PC"):
            audit.join_members(functional, native, iq, core=0)

    def test_context_collision_cannot_share_an_iq_identity(self):
        functional = [dict(source_sequence=i, pc=1, line=2, flags=2) for i in range(2)]
        native = [dict(core_id=0, thread_id=tid, inst_seq_num=42,
                       native_admission_count=0, native_response_count=0) for tid in [8, 9]]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            audit.join_members(functional, native, {}, core=0)

    def test_stage_comparison_preserves_pre_admission_wait(self):
        native = [dict(source_sequence=7, pc=1, line=2, flags=2, size=4,
                       native_sequence=42, service_role="merged",
                       native_admission_tick=200, native_response_tick=300,
                       iq_insert_tick=0, iq_release_tick=310,
                       request_lifetime_ticks=100, iq_lifetime_ticks=310)]
        model = [dict(source_sequence=7, line=2, dispatch=10, admission=12,
                      post_parent_response=20, iq_release=21, private_level=0)]
        result = audit.compare_members(native, model, period=10)
        row = result[0]
        self.assertEqual(row["native_pre_admission_cycles"], 20)
        self.assertEqual(row["model_pre_admission_cycles"], 2)
        self.assertEqual(row["native_iq_cycles"], 31)
        self.assertEqual(row["model_iq_cycles"], 11)
        model[0]["line"] = 9
        with self.assertRaisesRegex(ValueError, "line"):
            audit.compare_members(native, model, period=10)

    def test_same_line_callback_parent_uses_native_facts_not_proxy(self):
        leader = dict(source_sequence=1, line=9, native_response_tick=300,
                      service_role="leader", native_fragments=1,
                      native_hierarchy={"l1d":{"tag_misses":1,"hits":0},
                                        "l2":{"tag_misses":1,"hits":0}})
        alias = dict(source_sequence=2, line=9, native_response_tick=300,
                     service_role="merged", native_fragments=1)
        boundary = dict(source_sequence=3, line=10, native_response_tick=300,
                        service_role="merged", native_fragments=1)
        result = audit.bind_native_service_groups([leader, alias, boundary])
        self.assertEqual(result[1]["native_parent_tier"], "uncore")
        self.assertEqual(result[1]["native_parent_source_sequence"], 1)
        self.assertEqual(result[2]["native_parent_tier"], "outside-observed-members")

    def test_unique_line_overlap_unions_duplicate_members(self):
        result = audit.unique_line_overlap([(9,0,10),(9,5,15),(8,10,20)],0,20)
        self.assertEqual(result["area"], 25)
        self.assertEqual(result["peak"], 2)


if __name__ == "__main__":
    unittest.main()
