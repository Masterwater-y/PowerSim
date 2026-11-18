"""Exercise rejection of incomplete timing/population and changed source inputs."""

import copy
import hashlib
import json
import pathlib
import os
import tempfile
import unittest
from report_matrix import evaluate
from source_identity import audit_artifact, check_inventory

ROOT = pathlib.Path(__file__).resolve().parents[2]


class ReportGateTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(
            tempfile.mkdtemp(prefix="native-report-", dir=ROOT / "tmp")
        )
        trace = self.root / "trace"
        trace.mkdir()
        (trace / "oracle").mkdir()
        manifest = trace / "manifest.txt"
        manifest.write_text(
            "".join(
                f"{c} binary-warmup-slice core{c}.fst {c} 0 10 0 20\n" for c in range(2)
            )
        )
        sources = []
        for c in range(2):
            path = trace / f"core{c}.fst"
            path.write_bytes(bytes([c]))
            # Captured inputs predate replay; avoid relying on sub-jiffy inode
            # timestamp changes when the test immediately overwrites one byte.
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            sources.append(
                dict(
                    artifacts=[
                        dict(
                            role="fst",
                            path=str(path),
                            size_bytes=1,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    ]
                )
            )
        identity = dict(
            status="complete", source_identity="sha256:fixture", sources=sources
        )
        (trace / "oracle/thread-state-accounting.json").write_text(
            json.dumps(
                dict(
                    valid=True,
                    participants=[0, 1],
                    per_core=[
                        dict(
                            core_id=c,
                            retirement_stop=dict(record_ordinal=20, tick=1000),
                        )
                        for c in range(2)
                    ],
                    common_start_tick=0,
                    ticks_per_second=1000,
                    reference_frequency_hz=100,
                )
            )
        )
        self.receipt = dict(
            source_identity=identity["source_identity"],
            artifacts=[
                audit_artifact(art, 2**63)
                for source in sources
                for art in source["artifacts"]
            ],
            directories=check_inventory(identity),
        )
        stats = self.root / "stats.json"
        stats.write_text(
            json.dumps(
                dict(
                    functional_source_identity=identity,
                    time_accounting=dict(
                        elapsed_source="target-common-origin-to-last-completion",
                        elapsed_time_seconds=1,
                    ),
                    scope_metrics=dict(
                        user_trace_uops=40, throughput=dict(user_uops_per_second=40)
                    ),
                )
            )
        )
        common = dict(
            returncode=0,
            population_match=True,
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            instructions=[10, 10],
            uops=[20, 20],
            process_wall_seconds=2,
        )
        self.results = {
            k: {"fixture": copy.deepcopy(common)} for k in ["fastsim", "zsim", "sniper"]
        }
        self.results["fastsim"]["fixture"]["statistics_file"] = str(stats)
        self.results["zsim"]["fixture"]["statistics"] = dict(
            semantic_hashes=["1", "2"],
            unknown_code_records=[0, 0],
            bound_eof_cycles=[100, 100],
            bound_retirement_tail=[99, 99],
            native_cycles=[100, 100],
            frequency_hz=100,
            weave_phases=1,
            measurement_seconds=1,
        )
        self.results["sniper"]["fixture"].update(
            dict(
                semantic_hashes={"0": "1", "1": "2"},
                unknown_code_records={"0": 0, "1": 0},
                begin={str(c): dict(model_fs=0, host_ns=0) for c in range(2)},
                end={
                    str(c): dict(model_fs=10**15, host_ns=10**9, macros=10, stop=0)
                    for c in range(2)
                },
            )
        )
        self.frozen = {
            k: {"fixture": dict(manifest=str(manifest), effective_values={})}
            for k in self.results
        }

    def run_report(self):
        return evaluate("fixture", self.frozen, self.results, self.receipt)

    def test_complete_case(self):
        self.assertEqual(len(self.run_report()), 3)

    def test_missing_zsim_timing_rejected(self):
        for field in ["bound_eof_cycles", "bound_retirement_tail", "native_cycles"]:
            with self.subTest(field=field):
                z = self.results["zsim"]["fixture"]["statistics"]
                previous = z[field]
                z[field] = []
                with self.assertRaises(ValueError):
                    self.run_report()
                z[field] = previous

    def test_stopped_sniper_endpoint_rejected(self):
        self.results["sniper"]["fixture"]["end"]["0"]["stop"] = 1
        with self.assertRaises(ValueError):
            self.run_report()

    def test_missing_sniper_participant_rejected(self):
        del self.results["sniper"]["fixture"]["begin"]["1"]
        with self.assertRaises(ValueError):
            self.run_report()

    def test_same_size_input_edit_rejected(self):
        pathlib.Path(self.receipt["artifacts"][0]["path"]).write_bytes(b"x")
        with self.assertRaises(ValueError):
            self.run_report()

    def test_added_companion_rejected(self):
        pathlib.Path(self.receipt["artifacts"][0]["path"] + ".imap").write_bytes(b"x")
        with self.assertRaises(ValueError):
            self.run_report()

    def test_artifact_hash_mismatch_rejected(self):
        art = copy.deepcopy(self.receipt["artifacts"][0])
        art["size_bytes"] = art["stat"]["size_bytes"]
        art["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            audit_artifact(art, 2**63)

    def test_postlaunch_ctime_rejected(self):
        art = copy.deepcopy(self.receipt["artifacts"][0])
        art["size_bytes"] = art["stat"]["size_bytes"]
        with self.assertRaises(ValueError):
            audit_artifact(art, art["stat"]["ctime_ns"])


if __name__ == "__main__":
    unittest.main()
