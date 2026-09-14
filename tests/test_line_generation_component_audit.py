import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_line_generation_components",
    ROOT / "tools" / "audit_line_generation_components.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def event(sequence, line, admission, callback, *, core=0, kind="load",
          native=False, l1_set=None, l2_set=None, llc_set=None,
          shared=False):
    return MODULE.Event(
        core=core,
        sequence=sequence,
        line=line,
        admission=admission,
        callback=callback,
        kind=kind,
        native_coalesced=native,
        shared_touch=shared,
        l1_set=line if l1_set is None else l1_set,
        l2_set=line if l2_set is None else l2_set,
        llc_set=line if llc_set is None else llc_set,
    )


class ComponentAuditTest(unittest.TestCase):
    def test_same_line_follower_matches_native(self):
        result = MODULE.analyze_events([
            event(1, 10, 0, 10),
            event(2, 10, 9, 10, native=True),
            event(3, 10, 10, 11),
        ])
        self.assertEqual(result["components"], 2)
        self.assertEqual(result["observed_clean_events"], 3)
        self.assertEqual(result["follower_confusion"], {
            "inferred_and_native": 1,
            "inferred_only": 0,
            "native_only": 0,
            "neither": 2,
        })

    def test_unsupported_event_poisons_only_overlap_component(self):
        events = [
            event(1, 10, 0, 10),
            event(2, 10, 5, 12, kind="store"),
            event(3, 20, 20, 30),
        ]
        events[1].reasons.add("unsupported_store")
        result = MODULE.analyze_events(events)
        self.assertEqual(result["observed_clean_events"], 1)
        self.assertEqual(result["event_primary_reason_counts"], {
            "observed_clean": 1,
            "unsupported_store": 2,
        })
        self.assertTrue(result["event_primary_reason_conserved"])

    def test_private_and_shared_set_conflicts_are_distinct(self):
        result = MODULE.analyze_events([
            event(1, 1, 0, 10, l1_set=0, l2_set=1),
            event(2, 2, 1, 9, l1_set=0, l2_set=2),
            event(3, 3, 20, 30, core=0, llc_set=7, shared=True),
            event(4, 4, 21, 29, core=1, llc_set=7, shared=True),
        ])
        reasons = result["component_reason_counts_nonexclusive"]
        self.assertEqual(reasons["private_l1_set_different_line"], 1)
        self.assertEqual(reasons["shared_llc_set_different_line"], 1)
        self.assertEqual(result["observed_clean_events"], 0)

    def test_cross_core_same_line_is_rejected(self):
        result = MODULE.analyze_events([
            event(1, 10, 0, 10, core=0),
            event(2, 10, 1, 9, core=1),
        ])
        self.assertEqual(
            result["component_reason_counts_nonexclusive"]["cross_core_same_line"], 1)
        self.assertEqual(result["event_primary_reason_counts"], {
            "cross_core_same_line": 2,
        })

    def test_zero_duration_does_not_attach(self):
        result = MODULE.analyze_events([
            event(1, 10, 5, 5),
            event(2, 10, 5, 6),
        ])
        self.assertEqual(result["components"], 2)
        self.assertEqual(result["follower_confusion"]["inferred_only"], 0)

    def test_follower_oracle_mismatch_fails_closed(self):
        result = MODULE.analyze_events([
            event(1, 10, 0, 10),
            event(2, 10, 9, 10, native=False),
        ])
        self.assertEqual(result["observed_clean_events"], 0)
        self.assertEqual(result["event_primary_reason_counts"], {
            "follower_oracle_mismatch": 2,
        })

    def test_late_resolution_replaces_unresolved_commit(self):
        commit = {
            "record": "commit", "thread_id": 0, "inst_seq_num": 7,
            "line_requests": 1, "native_admission_count": 0,
            "native_response_count": 0, "native_issuance_closed": False,
            "native_terminal_no_ruby": False,
        }
        resolution = dict(commit)
        resolution.update({
            "record": "resolution", "native_admission_count": 1,
            "native_response_count": 1, "native_issuance_closed": True,
            "native_first_admission_tick": 100,
            "native_last_response_tick": 200,
        })
        with tempfile.TemporaryDirectory() as temporary:
            trace_dir = pathlib.Path(temporary)
            oracle = trace_dir / "oracle"
            oracle.mkdir()
            path = oracle / "native-response-core0.jsonl"
            rows = [{"record": "metadata"}, commit, resolution,
                    {"record": "summary"}]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            commits, paths, exclusions = MODULE.native_commits(trace_dir)
        self.assertEqual(paths, [path])
        self.assertEqual(commits[0][(0, 7)]["native_last_response_tick"], 200)
        self.assertEqual(exclusions["no_native_admission"], 0)


if __name__ == "__main__":
    unittest.main()
