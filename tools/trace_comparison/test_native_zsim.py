"""Runtime acceptance for the native zsim FST adapter."""

import json
import os
import pathlib
import subprocess
import unittest
import tempfile
from fixtures import make_fixture, memory_at_first_record

ROOT = pathlib.Path(__file__).resolve().parents[2]
NATIVE_BINARY = pathlib.Path(
    os.environ.get(
        "TRACEBENCH_NATIVE_BINARY",
        str(ROOT / "tmp/native-trace-adapters-20261116/native-zsim"),
    )
)


class NativeZsimTest(unittest.TestCase):
    def test_native_pipeline_and_weave(self):
        binary = NATIVE_BINARY
        self.assertTrue(binary.exists(), "build the native adapter first")
        env = dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64")
        p = subprocess.run(
            [str(binary), "--selftest"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertEqual(d["core_engine"], "zsim::OOOCore")
        self.assertGreater(d["weave_phases"], 0)
        self.assertEqual(d["cores"], 2)
        self.assertGreater(d["weave_limit"], 0)
        self.assertEqual(d["instructions"], [128, 128])

    def test_complete_population_and_fragment(self):
        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-test-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture)
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertEqual(d["instructions"], [128, 128])
        self.assertEqual(d["uops"], [129, 129])
        self.assertEqual(d["unknown_code_records"], [0, 0])
        self.assertGreater(d["weave_phases"], 0)

    def test_long_macro_is_not_trimmed(self):
        import struct

        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-long-macro-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture, macros=1, tail=False)
        count = 4097
        for c in range(2):
            f = fixture / f"core{c}.fst"
            blob = f.read_bytes()
            header = bytearray(blob[:72])
            struct.pack_into("<Q", header, 24, count)
            records = []
            for i in range(count):
                r = bytearray(blob[72:])
                struct.pack_into(
                    "<H", r, 50, 1 | 1024 | (2048 if i == count - 1 else 0)
                )
                records.append(r)
            f.write_bytes(header + b"".join(records))
            for suffix, offset in [(".deps", 24), (".imap", 24)]:
                p = pathlib.Path(str(f) + suffix)
                b = bytearray(p.read_bytes())
                struct.pack_into("<Q", b, offset, count)
                p.write_bytes(b)
        manifest.write_text(
            "".join(
                f"{c} binary-warmup-slice core{c}.fst {c} 0 1 0 {count}\n"
                for c in range(2)
            )
        )
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertEqual(d["instructions"], [1, 1])
        self.assertEqual(d["uops"], [count, count])

    def test_serializing_store_preserves_memory_access(self):
        import struct

        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-store-fence-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture, macros=1, tail=False)
        for c in range(2):
            f = fixture / f"core{c}.fst"
            blob = bytearray(f.read_bytes())
            struct.pack_into("<Q", blob, 32, 96)
            struct.pack_into("<Q", blob, 72 + 8, 0x100000)
            struct.pack_into("<H", blob, 72 + 50, 1 | 4 | 1024 | 2048 | 4096 | 8192)
            struct.pack_into("<H", blob, 72 + 48, 8)
            f.write_bytes(blob)
            h = 14695981039346656037
            for b in blob[72:]:
                h = ((h ^ b) * 1099511628211) & ((1 << 64) - 1)
            pathlib.Path(str(f) + ".mreq").write_bytes(
                struct.pack("<8sIIIIQQQII", b"FSTMREQ1", 2, 56, c, 1, 1, 0, h, 24, 0)
            )
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertEqual(d["instructions"], [1, 1])
        self.assertEqual(d["uops"], [1, 1])

    def test_native_retirement_width_is_in_endpoint(self):
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), "--retirement-selftest"],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout)
        self.assertGreater(d["retirement_tail"], d["max_completion"])

    def test_eof_drains_actual_native_rob_tail(self):
        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-eof-drain-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(
            fixture, cores=1, macros=32, tail=False, dependent=False
        )
        memory_at_first_record(fixture)
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertGreaterEqual(d["bound_eof_cycles"][0], d["bound_retirement_tail"][0])
        self.assertGreaterEqual(d["native_cycles"][0], d["bound_retirement_tail"][0])

    def test_functional_warmup_has_no_pending_fill_latency(self):
        import struct

        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-warmup-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture, cores=1, macros=2, tail=False, dependent=False)
        memory_at_first_record(fixture)
        f = fixture / "core0.fst"
        b = bytearray(f.read_bytes())
        b[136:200] = b[72:136]
        f.write_bytes(b)
        h = 14695981039346656037
        for byte in b[72:]:
            h = ((h ^ byte) * 1099511628211) & ((1 << 64) - 1)
        pathlib.Path(str(f) + ".mreq").write_bytes(
            struct.pack("<8sIIIIQQQII", b"FSTMREQ1", 2, 56, 0, 1, 2, 0, h, 24, 0)
        )
        manifest.write_text("0 binary-warmup-slice core0.fst 0 1 1 1 1\n")
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = json.loads(p.stdout.splitlines()[-1])
        self.assertLess(d["native_cycles"][0], 64)
        self.assertEqual(d["instructions"], [1])

    def test_truncated_input_rejected(self):
        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-zsim-truncated-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture)
        f = fixture / "core0.fst"
        f.write_bytes(f.read_bytes()[:-8])
        binary = NATIVE_BINARY
        p = subprocess.run(
            [str(binary), str(manifest), str(fixture / "code.cfg")],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rejected input", p.stderr)


if __name__ == "__main__":
    unittest.main()
