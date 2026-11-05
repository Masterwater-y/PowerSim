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

    def test_asid_static_map_bit_preserves_privilege_populations(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path = Path(directory) / "source.fst"
            self.fixture(path, 1 << 12)
            result = AUDIT.audit_file(path)
            self.assertEqual(result["user_retired_instructions"], 1)
            self.assertEqual(result["kernel_retired_instructions"], 1)

    def test_memory_and_static_uop_companions_preserve_privilege_populations(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path=Path(directory)/'source.fst'
            self.fixture(path,(1<<13)|(1<<14))
            result=AUDIT.audit_file(path)
            self.assertEqual(result['user_retired_instructions'],1)
            self.assertEqual(result['kernel_retired_instructions'],1)

    def test_instruction_translation_bit_preserves_privilege_populations(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path = Path(directory) / "source.fst"
            self.fixture(path, 1 << 15)
            result = AUDIT.audit_file(path)
            self.assertEqual(result["user_retired_instructions"], 1)
            self.assertEqual(result["kernel_retired_instructions"], 1)

    def test_future_unknown_feature_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path = Path(directory) / "source.fst"
            self.fixture(path, 1 << 19)
            with self.assertRaisesRegex(ValueError, "unknown FST feature"):
                AUDIT.audit_file(path)

    def test_boundary_bundle_bit_preserves_privilege_only_scope(self):
        # This auditor reads privilege counts only. Companion admission belongs
        # to BinaryTraceSource/prepare and is deliberately not claimed here.
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path=Path(directory)/'source.fst';self.fixture(path,1<<18)
            self.assertFalse(Path(str(path)+'.archb.gz').exists())
            result=AUDIT.audit_file(path)
            self.assertEqual(result['user_retired_instructions'],1)
            self.assertEqual(result['kernel_retired_instructions'],1)
            self.assertNotIn('architectural_boundary_bridge_qualified',result)

    def test_mmu_controls_bit_preserves_privilege_only_scope(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path=Path(directory)/'source.fst';self.fixture(path,1<<17)
            result=AUDIT.audit_file(path)
            self.assertEqual(result['user_retired_instructions'],1)
            self.assertEqual(result['kernel_retired_instructions'],1)

    def test_core_functional_bit_preserves_privilege_only_scope(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as directory:
            path=Path(directory)/'source.fst';self.fixture(path,1<<16)
            result=AUDIT.audit_file(path)
            self.assertEqual(result['user_retired_instructions'],1)
            self.assertEqual(result['kernel_retired_instructions'],1)


if __name__ == "__main__":
    unittest.main()
