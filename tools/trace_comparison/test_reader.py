"""Check production FST framing and operation projection before native timing."""

import json
import pathlib
import struct
import subprocess
import tempfile
import unittest
from fixtures import make_fixture

ROOT = pathlib.Path(__file__).resolve().parents[2]


class ReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = pathlib.Path(
            tempfile.mkdtemp(prefix="fst-reader-test-", dir=ROOT / "tmp")
        )
        cls.binary = cls.root / "probe"
        subprocess.run(
            [
                "g++",
                "-std=c++11",
                "-O2",
                "-I" + str(ROOT / "tools/trace_comparison"),
                str(ROOT / "tools/trace_comparison/reader_probe.cpp"),
                "-lz",
                "-o",
                str(cls.binary),
            ],
            check=True,
        )

    def fixture(self):
        root = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        make_fixture(root, macros=1, tail=False)
        return root / "core0.fst"

    def probe(self, path):
        return subprocess.run(
            [str(self.binary), str(path)], capture_output=True, text=True
        )

    def test_valid_syscall_metadata_trailer(self):
        path = self.fixture()
        blob = bytearray(path.read_bytes())
        struct.pack_into("<Q", blob, 32, 32 | 8 | 2)
        struct.pack_into("<3Q", blob, 40, len(blob), 1, 128)
        struct.pack_into("<h", blob, 72 + 52, -1)
        path.write_bytes(blob + bytes(128))
        result = self.probe(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["records"], 1)

    def test_unadvertised_trailer_is_rejected(self):
        path = self.fixture()
        path.write_bytes(path.read_bytes() + bytes(128))
        self.assertNotEqual(self.probe(path).returncode, 0)

    def test_spr_store_address_projection(self):
        path = self.fixture()
        blob = bytearray(path.read_bytes())
        struct.pack_into("<Q", blob, 32, 32 | 1024)
        struct.pack_into("<h", blob, 72 + 52, 61)
        path.write_bytes(blob)
        result = self.probe(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["first_op"], 99)


if __name__ == "__main__":
    unittest.main()
