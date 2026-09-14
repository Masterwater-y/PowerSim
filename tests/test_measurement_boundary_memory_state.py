#!/usr/bin/env python3

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "build_measurement_boundary_memory_state.py"


class MeasurementBoundaryMemoryStateTest(unittest.TestCase):
    def test_extracts_only_gap_commits_and_drops_oracles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace"
            trace.mkdir()
            fst = root / "core0.fst"
            fst.write_bytes(b"test-placeholder")
            manifest = root / "source-manifest.txt"
            manifest.write_text(
                f"0 fastsim-binary-warmup-slice {fst} 0 1 1 1 1\n")
            (root / "run.log").write_text(
                "[fs-ckpt] WORKBEGIN tick=100\n")
            (trace / "functional-boundary-core0.json").write_text(
                json.dumps({"warmup_records": 1}) + "\n")
            (trace / "uarch_profile.json").write_text(
                json.dumps({"dram": {"size_b": 0x10000}}) + "\n")
            labels = trace / "board.switch0.labels.micro.jsonl"
            labels.write_text(
                json.dumps({"commit_tick": 90}) + "\n" +
                json.dumps({"commit_tick": 120}) + "\n")
            mem_events = trace / "board.switch0.mem_events.jsonl"
            rows = [
                {"event_type": "commit", "core_id": 0,
                 "commit_tick": 99, "cacheline_addr": 0x1000,
                 "size": 8, "is_store": 0, "path_class": 5},
                {"event_type": "request", "core_id": 0,
                 "commit_tick": 105, "cacheline_addr": 0x2000,
                 "size": 8, "is_store": 0, "coh_oracle": 1},
                {"event_type": "commit", "core_id": 0,
                 "commit_tick": 105, "cacheline_addr": 0x2000,
                 "size": 8, "is_store": 0, "path_class": 5},
                {"event_type": "commit", "core_id": 0,
                 "commit_tick": 118, "cacheline_addr": 0x10000,
                 "size": 1, "is_store": 0, "path_class": 5},
                {"event_type": "commit", "core_id": 0,
                 "commit_tick": 119, "cacheline_addr": 0x3000,
                 "size": 4, "is_store": 1, "mesi_before": 3},
                {"event_type": "commit", "core_id": 0,
                 "commit_tick": 120, "cacheline_addr": 0x4000,
                 "size": 8, "is_store": 0},
            ]
            mem_events.write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
            output = root / "output"
            subprocess.run(
                [sys.executable, str(TOOL),
                 "--trace-dir", str(trace),
                 "--run-log", str(root / "run.log"),
                 "--manifest", str(manifest),
                 "--output-dir", str(output)],
                check=True, capture_output=True, text=True)
            state = (output / "core0.boundary-memory").read_text()
            self.assertEqual(
                state,
                "fastsim-boundary-memory-state-v1\n"
                "0 0x2000 8 R\n"
                "1 0x3000 4 W\n")
            self.assertNotIn("tick", state)
            self.assertNotIn("oracle", state)
            self.assertNotIn("mesi", state)
            output_manifest = (output / "manifest.txt").read_text()
            self.assertIn("fastsim-binary-warmup-state-slice", output_manifest)
            report = json.loads((output / "build-report.json").read_text())
            self.assertEqual(report["cores"][0]["committed_accesses"], 2)
            self.assertEqual(report["cores"][0]["mmio_commits_excluded"], 1)


if __name__ == "__main__":
    unittest.main()
