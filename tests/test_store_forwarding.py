#!/usr/bin/env python3
"""CLI rollback and forwarded-load counter regression for store forwarding."""
import json, os, subprocess, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("FASTSIM_TEST_BINARY", str(ROOT / "build/fastsim")))
class StoreForwardingTest(unittest.TestCase):
    def test_default_and_cli_rollback(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tmp")) as directory:
            directory = Path(directory)
            cfg = directory / "test.cfg"
            cfg.write_text("core.model = interval_weave\nsim.interval_scheduler = time_epoch\ncore.response_queue_feedback = true\ncore.response_sparse_scoreboard = true\ncore.response_block_summary = true\ncore.response_memory_descriptor = true\ncore.response_monotone_iq_calendar = true\ncore.response_materialized_uop_fast_kernel = true\n")
            results = []
            for mode in (None, "true", "false"):
                output = directory / (str(mode) + ".json")
                command = [str(BINARY), "benchmark", "--config", str(cfg), "--measurement-scope", "user", "--cores", "2", "--instructions-per-core", "4096", "--output", str(output)]
                if mode is not None: command += ["--store-forwarding", mode]
                process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(output.read_text())
                self.assertIn("store_forwarding", result["configuration"], "mechanism switch must be explicit and default on")
                self.assertEqual(result["configuration"]["store_forwarding"],
                                 mode != "false")
                results.append(result)
            default, on, off = results
            self.assertEqual(on["threads"], default["threads"])
            self.assertEqual(on["totals"], default["totals"])
            for result in results:
                self.assertEqual(result["totals"]["forwarded_loads"], sum(c["forwarded_loads"] for c in result["cores"]))
            self.assertEqual(off["totals"]["forwarded_loads"], 0)
if __name__ == "__main__": unittest.main()
