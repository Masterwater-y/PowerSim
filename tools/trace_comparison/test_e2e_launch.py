import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from e2e_launch import verify_case, verify_command_inputs
from source_identity import stat_identity

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent


class E2ELaunchTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(
            tempfile.mkdtemp(prefix="e2e-launch-", dir=ROOT / "tmp")
        )
        trace = self.root / "input"
        trace.mkdir()
        self.fst = trace / "core0.fst"
        self.fst.write_bytes(b"x")
        os.utime(self.fst, ns=(10**9, 10**9))
        self.receipt = dict(
            policy="local-sha256-and-prelaunch-ctime-v1",
            cutoff_ns=2**63,
            cases={
                "fixture": dict(
                    source_identity="sha256:fixture",
                    artifacts=[
                        dict(
                            path=str(self.fst),
                            role="fst",
                            sha256=hashlib.sha256(b"x").hexdigest(),
                            stat=stat_identity(self.fst),
                        )
                    ],
                    directories={str(trace): stat_identity(trace)},
                )
            },
        )

    def test_unchanged_validated_inputs(self):
        self.assertEqual(verify_case(self.receipt, "fixture"), "sha256:fixture")

    def test_same_size_edit_rejected(self):
        self.fst.write_bytes(b"y")
        with self.assertRaises(ValueError):
            verify_case(self.receipt, "fixture")

    def test_companion_addition_rejected(self):
        pathlib.Path(str(self.fst) + ".deps").write_bytes(b"y")
        with self.assertRaises(ValueError):
            verify_case(self.receipt, "fixture")

    def test_wrong_manifest_rejected(self):
        manifest = self.root / "manifest.txt"
        manifest.write_text("fixture")
        row = self.receipt["cases"]["fixture"]
        row.update(
            manifest=str(manifest),
            manifest_sha256=hashlib.sha256(b"fixture").hexdigest(),
        )
        verify_command_inputs(
            ["engine", "simulate", "--manifest", str(manifest)], "fastsim", row
        )
        with self.assertRaises(ValueError):
            verify_command_inputs(
                ["engine", "simulate", "--manifest", str(self.root / "wrong.txt")],
                "fastsim",
                row,
            )
        manifest.write_text("changed")
        with self.assertRaises(ValueError):
            verify_case(self.receipt, "fixture")

    def test_ancillary_plan_change_rejected(self):
        plan = self.root / "thread-state.plan"
        plan.write_bytes(b"plan")
        os.utime(plan, ns=(10**9, 10**9))
        self.receipt["cases"]["fixture"]["ancillary_artifacts"] = [
            dict(
                path=str(plan),
                role="thread_state_plan",
                sha256=hashlib.sha256(b"plan").hexdigest(),
                stat=stat_identity(plan),
            )
        ]
        verify_case(self.receipt, "fixture")
        plan.write_bytes(b"evil")
        with self.assertRaises(ValueError):
            verify_case(self.receipt, "fixture")

    def test_launcher_includes_prepare_and_excludes_post_model_output(self):
        manifest = self.root / "manifest.txt"
        manifest.write_text("fixture")
        self.receipt["cases"]["fixture"].update(
            manifest=str(manifest),
            manifest_sha256=hashlib.sha256(b"fixture").hexdigest(),
        )
        (self.root / "receipt.json").write_text(json.dumps(self.receipt))
        script = "import time\ntime.sleep(.03)\nprint('[TRACEBENCH-END] host_monotonic_ns='+str(time.monotonic_ns()),flush=True)\ntime.sleep(.2)\n"
        (self.root / "fake.py").write_text(script)
        (self.root / "command.json").write_text(
            json.dumps(
                [
                    sys.executable,
                    str(self.root / "fake.py"),
                    "--manifest",
                    str(manifest),
                ]
            )
        )
        cmd = [
            sys.executable,
            str(HERE / "e2e_launch.py"),
            "--receipt",
            str(self.root / "receipt.json"),
            "--case",
            "fixture",
            "--engine",
            "fastsim",
            "--cores",
            "1",
            "--command",
            str(self.root / "command.json"),
            "--cwd",
            str(self.root),
            "--log",
            str(self.root / "engine.log"),
            "--metrics",
            str(self.root / "timing.json"),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads((self.root / "timing.json").read_text())
        self.assertGreaterEqual(d["e2e_seconds"], 0.03)
        self.assertGreater(d["process_wall_seconds"] - d["e2e_seconds"], 0.15)
        self.assertTrue(d["preprocessing_included"])


class TrustGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = pathlib.Path(
            tempfile.mkdtemp(prefix="trust-gate-", dir=ROOT / "tmp")
        )
        p = cls.root / "main.cpp"
        p.write_text(
            '#include "tracebench_trust.hpp"\nint main(){try{return tracebench::validation_enabled()?0:7;}catch(...){return 9;}}\n'
        )
        cls.binary = cls.root / "gate"
        subprocess.run(
            ["g++", "-std=c++11", "-I" + str(HERE), str(p), "-o", str(cls.binary)],
            check=True,
        )

    def probe(self, mode, proof=None):
        env = dict(os.environ, TRACEBENCH_VALIDATED_INPUT=mode)
        env.pop("TRACEBENCH_RECEIPT_SHA256", None)
        if proof:
            env["TRACEBENCH_RECEIPT_SHA256"] = proof
        return subprocess.run([str(self.binary)], env=env).returncode

    def test_default_is_strict(self):
        self.assertEqual(self.probe("0"), 0)

    def test_missing_proof_rejected(self):
        self.assertEqual(self.probe("1"), 9)

    def test_verified_mode(self):
        self.assertEqual(self.probe("1", "a" * 64), 7)


if __name__ == "__main__":
    unittest.main()
