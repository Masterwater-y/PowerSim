import json
import os
from pathlib import Path
import subprocess
import unittest
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = Path(os.environ.get("FASTSIM_SYNC_LIBGOMP", ROOT / "tmp/functional-sync/runtime-v2/libgomp.a"))


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        if not LIBRARY.exists() and "FASTSIM_SYNC_LIBGOMP" not in os.environ:
            self.skipTest("optional integration requires an isolated sync libgomp build")
        workspace = tempfile.TemporaryDirectory(dir=LIBRARY.parent)
        self.addCleanup(workspace.cleanup)
        self.build_dir = Path(workspace.name)

    def test_real_runtime_team_reuse_and_barrier_members(self):
        self.assertTrue(LIBRARY.exists(), "instrumented libgomp has not been built")
        output = self.build_dir / "host-capture-probe"
        subprocess.run(["/opt/gcc-11/bin/gcc", "-O2", "-g", "-gdwarf-4", "-DFASTSIM_SYNC_HOST_CAPTURE", "-fopenmp", "-static", "-no-pie",
                        str(ROOT / "tests/workloads/openmp_sync_probe.c"),
                        str(ROOT / "tests/workloads/sync_host_capture.c"),
                        str(LIBRARY), "-pthread", "-ldl", "-o", str(output)], check=True)
        run = subprocess.run([str(output), "4", "3", "200"], capture_output=True, text=True, check=True)
        self.assertIn("result=599000 expected=599000", run.stdout)
        rows = [json.loads(s) for s in run.stderr.splitlines()]
        begins = [r for r in rows if r["event_kind"] == 1]
        self.assertEqual(len(begins), 3)
        self.assertEqual(len({r["region_instance"] for r in begins}), 3)
        ids = set()
        for begin in begins:
            region = [r for r in rows if r["region_instance"] == begin["region_instance"]]
            for kind in (3, 4, 5, 6, 7, 8):
                members = [r["member_index"] for r in region if r["event_kind"] == kind]
                self.assertEqual(sorted(members), [0, 1, 2, 3])
            ids.update(r["thread_id"] for r in region)
        self.assertEqual(len(ids), 4)

    def test_workshare_implicit_barrier_is_captured(self):
        # Reuse the real runtime binary produced by the first test.
        self.test_real_runtime_team_reuse_and_barrier_members()
        run = subprocess.run([str(self.build_dir / "host-capture-probe"), "4", "3", "200", "0", "1"],
                             capture_output=True, text=True, check=True)
        rows = [json.loads(s) for s in run.stderr.splitlines()]
        for region in {r["region_instance"] for r in rows}:
            arrivals = [r for r in rows if r["region_instance"] == region and r["event_kind"] == 5]
            self.assertEqual(len(arrivals), 8)
            self.assertEqual(len({r["generation"] for r in arrivals}), 2)

    def test_taskloop_and_cancellation_are_explicitly_unsupported(self):
        output = self.build_dir / "host-unsupported-probe"
        subprocess.run(["/opt/gcc-11/bin/gcc", "-O2", "-gdwarf-4", "-DFASTSIM_SYNC_HOST_CAPTURE", "-fopenmp", "-static", "-no-pie",
                        str(ROOT / "tests/workloads/openmp_sync_unsupported.c"),
                        str(ROOT / "tests/workloads/sync_host_capture.c"), str(LIBRARY),
                        "-pthread", "-ldl", "-o", str(output)], check=True)
        run = subprocess.run([str(output), "0"], capture_output=True, text=True, check=True)
        rows = [json.loads(s) for s in run.stderr.splitlines()]
        reasons = {r["sync_object_id"] for r in rows if r["event_kind"] == 9}
        self.assertTrue({2, 3}.issubset(reasons), rows)


if __name__ == "__main__":
    unittest.main()
