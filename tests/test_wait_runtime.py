"""Runtime metadata tests; actual blocking classification belongs to kernel facts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = Path(os.environ.get("FASTSIM_WAIT_LIBGOMP", ROOT / "tmp/thread-state-active-elapsed/runtime/libgomp.a"))


class WaitRuntimeTest(unittest.TestCase):
    def setUp(self):
        if not LIBRARY.exists() and "FASTSIM_WAIT_LIBGOMP" not in os.environ:
            self.skipTest("optional integration requires an isolated thread-state libgomp build")
        self.assertTrue(LIBRARY.exists())
        workspace = tempfile.TemporaryDirectory(dir=LIBRARY.parent)
        self.addCleanup(workspace.cleanup)
        self.binary = Path(workspace.name) / "wait-probe"
        subprocess.run(["/opt/gcc-11/bin/gcc", "-O2", "-g", "-Wall", "-Werror", "-fopenmp",
                        "-I" + str(LIBRARY.parent / "src"),
                        "-I" + str(LIBRARY.parent / "src/config/linux/x86"),
                        str(ROOT / "tests/workloads/wait_runtime_probe.c"), str(LIBRARY),
                        "-pthread", "-ldl", "-o", str(self.binary)], check=True)

    def rows(self, mode):
        run = subprocess.run([str(self.binary), str(mode)], capture_output=True,
                             text=True, check=True, timeout=10,
                             env={**os.environ, "OMP_WAIT_POLICY": "PASSIVE", "GOMP_SPINCOUNT": "0"})
        rows = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertTrue(rows, "runtime emitted no identity/wait metadata")
        identities = {}
        pending = {}
        last_request = {}
        for row in rows:
            tid = row["tid"]
            if row["kind"] == 1:
                self.assertNotIn(tid, identities)
                self.assertGreater(row["runtime"], 0)
                self.assertGreater(row["detail"], 0)
                self.assertEqual((row["request"], row["object"]), (0, 0))
                identities[tid] = row["runtime"]
                last_request[tid] = 0
            else:
                self.assertIn(tid, identities, "identity must precede first attempt")
                self.assertEqual(row["runtime"], identities[tid])
                key = (tid, row["request"])
                if row["kind"] in (2, 4):
                    self.assertEqual(row["request"], last_request[tid] + 1)
                    last_request[tid] = row["request"]
                    pending[key] = row
                else:
                    self.assertIn(key, pending)
                    attempt = pending.pop(key)
                    self.assertEqual(row["kind"], attempt["kind"] + 1)
                    self.assertEqual(row["object"], attempt["object"])
        self.assertFalse(pending)
        self.assertEqual(len(set(identities.values())), len(identities))
        return rows

    def test_eagain_and_real_single_waiter_wake_have_request_pairs(self):
        rows = self.rows(0)
        returns = [r for r in rows if r["kind"] == 3]
        self.assertEqual([r["detail"] for r in returns], [-11, 0])
        self.assertEqual(len({r["tid"] for r in rows}), 2)
        self.assertEqual(len({r["object"] for r in rows if r["kind"] != 1}), 1)
        self.assertEqual([r["detail"] & 0xffffffff for r in rows if r["kind"] == 2], [1, 0])
        self.assertTrue(all(r["detail"] >> 32 == 128 for r in rows if r["kind"] == 2))
        self.assertEqual([r["detail"] for r in rows if r["kind"] == 5][-1], 1)

    def test_private_enosys_wait_and_wake_fallback_each_get_new_request(self):
        rows = self.rows(1)
        attempts = [r for r in rows if r["kind"] in (2, 4)]
        self.assertEqual([r["request"] for r in attempts], [1, 2, 3, 4])
        self.assertEqual([r["detail"] >> 32 for r in attempts], [128, 0, 129, 1])
        self.assertEqual([r["detail"] & 0xffffffff for r in attempts], [1]*4)
        self.assertEqual([r["detail"] for r in rows if r["kind"] in (3, 5)], [-38, -11, -38, 0])

    def test_complete_openmp_runtime_reuses_sync_thread_identity(self):
        rows = self.rows(2)
        self.assertEqual(len({r["tid"] for r in rows}), 4)
        self.assertTrue(any(r["kind"] == 2 for r in rows))
        self.assertTrue(any(r["kind"] == 4 for r in rows))

    def test_compute_without_futex_emits_identity_once(self):
        rows = self.rows(3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], 1)


if __name__ == "__main__":
    unittest.main()
