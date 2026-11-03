import struct
import tempfile
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from audit_fst_syscall_metadata import audit_file
from audit_task_pmu import functional_populations


def raw_trace(path, prefix=19):
    header = struct.pack("<8sIIIIQQ4Q", b"FSTRC01\0", 7, 72, 64, 0, prefix,
                         (1 << 8) | (1 << 3), 72 + 64 * prefix, 1, 128, 1)
    record = struct.Struct("<QQQQ4IHHhBB4BI")
    data = bytearray(header)
    for i in range(prefix):
        flags = 1 | 1024 | ((2048 | 16) if i == 24 else 0)
        data.extend(record.pack(0x462682, 0, 0, 0, 0, 0, 0, 0, 0, flags, 1, 0, 0,
                                255, 255, 255, 255, 0))
    # Existing 128-byte sparse ABI metadata, anchored at the first real UOP.
    data.extend(struct.pack("<16Q", *([0] * 16)))
    path.write_bytes(data)


class RawSyscallMicroopsTest(unittest.TestCase):
    def test_existing_sparse_metadata_accepts_declared_raw_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core0.records.micro.fst"
            raw_trace(path)
            audited = audit_file(path)
            self.assertEqual(audited["syscalls"], 1)
            self.assertEqual(audited["missing_return_by_syscall"], {0: 1})

    def test_privilege_audit_and_formal_converter_preserve_raw_entry(self):
        from audit_fst_privilege import audit_file as audit_privilege
        from build_fst_v7_formal_dataset import read_header, syscall_rows, decode_metadata_rows
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core0.records.micro.fst"
            raw_trace(path)
            self.assertEqual(audit_privilege(path)["user_retired_uops"], 19)
            header = read_header(path)
            rows = syscall_rows(path, header)
            self.assertEqual(rows, [(0, 0x462682, 0)])
            self.assertEqual(len(decode_metadata_rows(path, header, rows)), 1)

    def test_actual_micro_prefix_counts_macro_and_branch_only_when_committed(self):
        for prefix, warmup, wanted_uops, wanted_macros, wanted_branches in (
            (1, 0, 1, 0, 0), (12, 0, 12, 0, 0), (19, 0, 19, 0, 0),
            (24, 0, 24, 0, 0), (25, 0, 25, 1, 1), (25, 12, 13, 1, 1),
        ):
            with self.subTest(prefix=prefix, warmup=warmup), tempfile.TemporaryDirectory() as directory:
                trace = Path(directory); path = trace / "core0.records.micro.fst"
                raw_trace(path, prefix)
                meta = dict(core_id=0, source_file=path.name, source_record_count=prefix, warmup_records=warmup)
                rows = [dict(core_id=0, record_ordinal=0, boundary="before_record", os_tid=101, event_kind="initial_owner")]
                observed = functional_populations(trace, meta, rows)[101]
                self.assertEqual((observed["retired_uops"], observed["retired_instructions"], observed["branches"]),
                                 (wanted_uops, wanted_macros, wanted_branches))

    def test_raw_metadata_rejects_nonretiring_memory_kernel_and_nonmicro_anchors(self):
        for flags, op in ((1024, 1), (1 | 1024 | 2, 1), (1 | 1024, -2), (1, 1)):
            with self.subTest(flags=flags, op=op), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "core0.records.micro.fst"
                raw_trace(path)
                data = bytearray(path.read_bytes())
                struct.pack_into("<Hh", data, 72 + 50, flags, op)
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "row/hot record mismatch"):
                    audit_file(path)

    def test_raw_capability_rejects_collapsed_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory); path = trace / "core0.records.micro.fst"
            raw_trace(path)
            data = bytearray(path.read_bytes()); struct.pack_into("<h", data, 72 + 52, -1)
            path.write_bytes(data)
            meta = dict(core_id=0, source_file=path.name, source_record_count=19, warmup_records=0)
            rows = [dict(core_id=0, record_ordinal=0, boundary="before_record", os_tid=101, event_kind="initial_owner")]
            with self.assertRaisesRegex(ValueError, "raw syscall"):
                functional_populations(trace, meta, rows)

if __name__ == "__main__":
    unittest.main()
