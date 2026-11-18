import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from fixtures import make_fixture

ROOT = pathlib.Path(__file__).resolve().parents[2]
BINARY = pathlib.Path(
    os.environ.get(
        "TRACEBENCH_NATIVE_BINARY",
        str(ROOT / "tmp/e2e-throughput-20261116/zsim-parallel/native-zsim"),
    )
)


class NativeParallelInputTest(unittest.TestCase):
    def run_fixture(self, root, jobs):
        return subprocess.run(
            [str(BINARY), str(root / "manifest.txt"), str(root / "code.cfg")],
            env=dict(
                os.environ,
                LD_LIBRARY_PATH="/opt/gcc-11/lib64",
                TRACEBENCH_PREPARE_JOBS=str(jobs),
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_serial_and_parallel_inputs_match(self):
        root = pathlib.Path(
            tempfile.mkdtemp(prefix="native-prepare-", dir=ROOT / "tmp")
        )
        make_fixture(root, cores=4)
        data = []
        for jobs in [1, 4]:
            p = self.run_fixture(root, jobs)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            d = json.loads(p.stdout.splitlines()[-1])
            self.assertEqual(
                d.get("preparation_workers"), min(jobs, len(os.sched_getaffinity(0)))
            )
            data.append(d)
        for field in [
            "instructions",
            "uops",
            "semantic_hashes",
            "prepared_hashes",
            "unknown_code_records",
        ]:
            self.assertEqual(data[0][field], data[1][field], field)

    def test_invalid_worker_count_rejected(self):
        root = pathlib.Path(
            tempfile.mkdtemp(prefix="native-prepare-invalid-", dir=ROOT / "tmp")
        )
        make_fixture(root, cores=2)
        for jobs in ["0", "-1", "bad", "999999999999999999999999"]:
            with self.subTest(jobs=jobs):
                self.assertNotEqual(self.run_fixture(root, jobs).returncode, 0)

    def test_reader_failure_stops_before_model(self):
        root = pathlib.Path(
            tempfile.mkdtemp(prefix="native-prepare-fail-", dir=ROOT / "tmp")
        )
        make_fixture(root, cores=4)
        (root / "core2.fst").unlink()
        p = self.run_fixture(root, 4)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn('"core_engine"', p.stdout)


if __name__ == "__main__":
    unittest.main()
