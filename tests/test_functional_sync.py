"""Behavioral fixtures for the synchronization companion validation gate."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def fixture(root, events=None, end_reason="workload_exit"):
    # Master can finish the join before the worker's departure marker commits.
    order = events or [(1, 0), (3, 0), (3, 1), (4, 0), (7, 0),
                       (4, 1), (7, 1), (8, 0), (2, 0), (8, 1)]
    rows = []
    streams = [[], []]
    for kind, member in order:
        row = dict(schema="fastsim-functional-sync-v1", producer="gem5-commit",
                   core_id=member, record_ordinal=len(streams[member]),
                   boundary="after_record", address_space_id=4096,
                   thread_id=member + 1, event_kind=kind, region_instance=1,
                   sync_object_id=8192, generation=0, member_index=member,
                   team_size=2, phase="measurement", pc=0x401000)
        streams[member].append(row)
        rows.append(row)
    (root / "oracle").mkdir()
    observations = [dict(schema="fastsim-sync-observation-v1", sequence=i,
                         core_id=row["core_id"], record_ordinal=row["record_ordinal"],
                         commit_tick=i * 100) for i, row in enumerate(rows)]
    (root / "oracle/functional-sync-observation.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in observations))
    for core, stream in enumerate(streams):
        fst = root / f"core{core}.fst"
        header = struct.pack("<8sIIIIQQ4Q", b"FSTRC01\0", 7, 72, 64, core,
                             len(stream), 0, 0, 0, 0, 0)
        # Explicitly retired, non-memory, non-micro single instruction.
        record = struct.pack("<4Q4IHHhBB4BI", 0x401000, 0, 0, 0,
                             0, 0, 0, 0, 1, 1, 0, 0, 0, 255, 255, 255, 255, 0)
        fst.write_bytes(header + record * len(stream))
        Path(str(fst) + ".asmap").write_bytes(
            struct.pack("<8sIIIIQQQ", b"FSTASM1\0", 1, 48, 16, core, len(stream), 1, 0)
            + struct.pack("<QQ", 0, 4096))
        (root / f"functional-boundary-core{core}.json").write_text(json.dumps(
            dict(schema="tcsim-functional-boundary-v1", core_id=core, total_records=len(stream), warmup_records=0,
                 measurement_records=len(stream), participant_count=2,
                 measurement_closed=end_reason == "first_core_target_common_end",
                 common_end_tick=1000 if end_reason == "first_core_target_common_end" else 0,
                 measurement_policy="first-core-target-common-end-v1")))
        (root / f"functional-sync-core{core}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in stream))
        metadata = dict(schema="fastsim-functional-sync-metadata-v1", core_id=core,
                        source_file=fst.name, source_record_count=len(stream),
                        event_count=len(stream), marker_pc=0x401000,
                        initial_context="empty", end_reason=end_reason)
        (root / f"functional-sync-metadata-core{core}.json").write_text(json.dumps(metadata))
    return streams


class SyncValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "tools/validate_functional_sync.py"
        if not path.exists():
            return
        spec = importlib.util.spec_from_file_location("sync_validator", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def validate(self, root, **kwargs):
        self.assertTrue((ROOT / "tools/validate_functional_sync.py").exists(),
                        "functional sync validator has not been implemented")
        return self.module.validate(root, **kwargs)

    def test_complete_final_barrier_and_delayed_worker_departure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            result = self.validate(root)
            self.assertTrue(result["valid"], result)
            self.assertTrue(result["complete"])
            self.assertEqual(result["regions"], 1)
            self.assertEqual(result["events"], 10)

    def test_duplicate_arrival_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root, [(1, 0), (3, 0), (3, 1), (4, 0), (7, 0), (7, 0)])
            result = self.validate(root)
            self.assertFalse(result["valid"])
            self.assertTrue(any("duplicate" in e for e in result["errors"]), result)

    def test_early_departure_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root, [(1, 0), (3, 0), (3, 1), (4, 0), (7, 0), (8, 0),
                           (4, 1), (7, 1), (8, 1), (2, 0)])
            result = self.validate(root)
            self.assertFalse(result["valid"])
            self.assertTrue(any("before all" in e for e in result["errors"]), result)

    def test_fst_anchor_pc_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fst = root / "core0.fst"
            data = bytearray(fst.read_bytes())
            struct.pack_into("<Q", data, 72, 0x400000)
            fst.write_bytes(data)
            result = self.validate(root)
            self.assertFalse(result["valid"])
            self.assertTrue(any("PC" in e for e in result["errors"]), result)

    def test_out_of_range_ordinal_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root)
            streams[0][0]["record_ordinal"] = 1000
            (root / "functional-sync-core0.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in streams[0]))
            result = self.validate(root)
            self.assertFalse(result["valid"])
            self.assertTrue(any("ordinal" in e for e in result["errors"]), result)

    def test_common_end_is_truncated_not_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root, [(1, 0), (3, 0), (3, 1), (4, 0), (7, 0)],
                    "first_core_target_common_end")
            result = self.validate(root, allow_truncated=True)
            self.assertTrue(result["valid"], result)
            self.assertFalse(result["complete"])
            self.assertEqual(result["truncated_regions"], 1)
            self.assertFalse(self.validate(root)["valid"])

    def test_unclosed_workload_exit_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root, [(1, 0), (3, 0), (3, 1), (4, 0), (7, 0)])
            self.assertFalse(self.validate(root, allow_truncated=True)["valid"])

    def test_wrong_generation_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root)
            streams[1][-1]["generation"] = 8
            (root / "functional-sync-core1.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in streams[1]))
            self.assertFalse(self.validate(root)["valid"])

    def test_wrong_source_population_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            path = root / "functional-sync-metadata-core0.json"
            meta = json.loads(path.read_text())
            meta["source_record_count"] += 1
            path.write_text(json.dumps(meta))
            self.assertFalse(self.validate(root)["valid"])

    def test_unknown_initial_context_never_certifies_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            path = root / "functional-sync-metadata-core0.json"
            meta = json.loads(path.read_text())
            meta["initial_context"] = "unknown"
            path.write_text(json.dumps(meta))
            result = self.validate(root, allow_truncated=True)
            self.assertFalse(result["complete"])
            self.assertFalse(result["valid"])

    def test_dropped_marker_cannot_hide_in_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root)
            streams[1].pop()
            (root / "functional-sync-core1.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in streams[1]))
            path = root / "functional-sync-metadata-core1.json"
            meta = json.loads(path.read_text())
            meta["event_count"] -= 1
            path.write_text(json.dumps(meta))
            result = self.validate(root)
            self.assertFalse(result["valid"])
            self.assertTrue(any("marker population" in e for e in result["errors"]), result)

    def test_one_thread_cannot_claim_two_members(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root)
            for row in streams[1]:
                row["thread_id"] = 1
            (root / "functional-sync-core1.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in streams[1]))
            self.assertFalse(self.validate(root)["valid"])

    def test_barrier_generation_cannot_go_backwards(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root, [(1, 0), (3, 0), (3, 1),
                                    (5, 0), (5, 1), (6, 0), (6, 1),
                                    (5, 0), (5, 1), (6, 0), (6, 1),
                                    (4, 0), (7, 0), (4, 1), (7, 1),
                                    (8, 0), (2, 0), (8, 1)])
            for core, stream in enumerate(streams):
                arrivals = departures = 0
                for row in stream:
                    if row["event_kind"] == 5:
                        row["generation"] = 8 if arrivals == 0 else 0
                        arrivals += 1
                    elif row["event_kind"] == 6:
                        row["generation"] = 8 if departures == 0 else 0
                        departures += 1
                    elif row["event_kind"] in (7, 8):
                        row["generation"] = 16
                (root / f"functional-sync-core{core}.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in stream))
            self.assertFalse(self.validate(root)["valid"])

    def test_phase_and_asid_must_match_functional_companions(self):
        for field, value in (("phase", "warmup"), ("address_space_id", 9999)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                streams = fixture(root)
                for core, stream in enumerate(streams):
                    for row in stream:
                        row[field] = value
                    (root / f"functional-sync-core{core}.jsonl").write_text(
                        "".join(json.dumps(r) + "\n" for r in stream))
                self.assertFalse(self.validate(root)["valid"])

    def test_master_identity_is_bound_to_parallel_begin(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            streams = fixture(root)
            streams[0][0]["thread_id"] = 999
            (root / "functional-sync-core0.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in streams[0]))
            self.assertFalse(self.validate(root)["valid"])

    def test_unknown_boundary_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            path = root / "functional-boundary-core0.json"
            data = json.loads(path.read_text())
            data["schema"] = "unknown-boundary"
            path.write_text(json.dumps(data))
            self.assertFalse(self.validate(root)["valid"])


if __name__ == "__main__":
    unittest.main()
