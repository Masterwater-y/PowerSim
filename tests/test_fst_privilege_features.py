"""Privilege accounting stays valid with independently audited new features."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "audit_fst_privilege", Path(__file__).resolve().parents[1] / "tools/audit_fst_privilege.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class PrivilegeFeatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_root = Path(__file__).resolve().parents[1] / "tmp"
        cls.temporary_root.mkdir(exist_ok=True)

    def fixture(self, path, extra_features):
        rows = []
        for op in (1, -3):
            fields = list(AUDIT.RECORD.unpack(bytes(64)))
            fields[9], fields[10] = AUDIT.RETIRE, op
            rows.append(AUDIT.RECORD.pack(*fields))
        path.write_bytes(AUDIT.HEADER.pack(
            AUDIT.MAGIC, 7, 72, 64, 1, 2, AUDIT.FEATURE_PRIVILEGE | extra_features,
            0, 0, 0, 0) + b"".join(rows))

    def test_dynamic_translation_and_spr_bits_preserve_privilege_counts(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path = Path(directory) / "source.fst"
            self.fixture(path, (1 << 9) | (1 << 10) | (1 << 11))
            result = AUDIT.audit_file(path)
            self.assertEqual(result["user_retired_instructions"], 1)
            self.assertEqual(result["kernel_retired_instructions"], 1)

    def test_future_unknown_feature_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path = Path(directory) / "source.fst"
            self.fixture(path, 1 << 12)
            with self.assertRaisesRegex(ValueError, "unknown FST feature"):
                AUDIT.audit_file(path)


if __name__ == "__main__":
    unittest.main()
