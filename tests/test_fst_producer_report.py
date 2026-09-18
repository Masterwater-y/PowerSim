from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.audit_fst_dependencies import record_hash
from tools.fst_producer_report import producer_report
from tools.fst_wire import (
    FEATURE_COMPLETE_DEPENDENCIES,
    FEATURE_DESTINATION_CLASSES,
    FEATURE_IO_DOMAINS,
    FEATURE_PRIVILEGE_RECORDS,
    FST_ASMAP_ENTRY,
    FST_ASMAP_HEADER,
    FST_HEADER,
    FST_RECORD,
    FST_SYSCALL_METADATA,
)


RETIRE = 1 << 0
BRANCH = 1 << 4
CONDITIONAL = 1 << 5
TAKEN = 1 << 9
MICRO_OP = 1 << 10
LAST_MICRO_OP = 1 << 11
SERIALIZE = 1 << 13
DESTINATION_CLASS_COUNTS_MARKER = 1 << 31
DEPENDENCY_HEADER = struct.Struct("<8sIIIIQQQ")
DEPENDENCY_ROW = struct.Struct("<QII")
IO_HEADER = struct.Struct("<8sIIIIQQQ")
IO_ENTRY = struct.Struct("<QB7x")


def record(
    *,
    op_class: int,
    flags: int,
    dependencies: tuple[int, ...] = (),
    producer_class: int = 0,
    n_src: int = 0,
    n_dst: int = 1,
) -> bytes:
    inline = list(dependencies[:4]) + [0] * (4 - len(dependencies[:4]))
    producer_classes = [7, 7, 7, 7]
    if dependencies:
        producer_classes[0] = producer_class
    producer_classes[0] |= n_dst << 3
    return FST_RECORD.pack(
        0x1000, 0, 0, 0, *inline, 0, flags, op_class, n_src, n_dst,
        *producer_classes, DESTINATION_CLASS_COUNTS_MARKER,
    )


def write_trace(
    directory: Path, core: int, records: list[bytes],
    dependencies: dict[int, tuple[int, ...]], *, with_io: bool,
) -> None:
    features = (
        FEATURE_DESTINATION_CLASSES
        | FEATURE_PRIVILEGE_RECORDS
        | FEATURE_COMPLETE_DEPENDENCIES
        | (FEATURE_IO_DOMAINS if with_io else 0)
    )
    fst = directory / f"core{core}.fst"
    fst.write_bytes(
        FST_HEADER.pack(
            b"FSTRC01\0", 7, FST_HEADER.size, FST_RECORD.size, core,
            len(records), features, 0, 0, 0, 1,
        ) + b"".join(records)
    )
    rows = []
    extras = []
    for ordinal, distances in sorted(dependencies.items()):
        if len(distances) <= 4:
            continue
        rows.append(
            DEPENDENCY_ROW.pack(
                ordinal, len(distances) - 4, record_hash(records[ordinal])
            )
        )
        extras.extend(distances[4:])
    dependency = Path(str(fst) + ".deps")
    dependency.write_bytes(
        DEPENDENCY_HEADER.pack(
            b"FSTDEP1\0", 1, DEPENDENCY_HEADER.size, core, 0,
            len(records), len(rows), len(extras),
        )
        + b"".join(rows)
        + (struct.pack(f"<{len(extras)}I", *extras) if extras else b"")
    )
    Path(str(fst) + ".asmap").write_bytes(
        FST_ASMAP_HEADER.pack(
            b"FSTASM1\0", 1, FST_ASMAP_HEADER.size,
            FST_ASMAP_ENTRY.size, core, len(records), 1, 0,
        )
        + FST_ASMAP_ENTRY.pack(0, 0x1000)
    )
    if with_io:
        Path(str(fst) + ".io").write_bytes(
            IO_HEADER.pack(
                b"FSTIOV1\0", 1, IO_HEADER.size, IO_ENTRY.size,
                core, len(records), 1, 0,
            )
            + IO_ENTRY.pack(0, 1)
        )
    (directory / f"functional-boundary-core{core}.json").write_text(
        json.dumps({
            "core_id": core,
            "warmup_records": 0,
            "measurement_records": len(records),
            "measurement_user_records": sum(
                int(FST_RECORD.unpack(item)[10]) >= -1
                for item in records
            ),
        }),
        encoding="utf-8",
    )


class FstProducerGateTest(unittest.TestCase):
    def create_pair(
        self, root: Path, *, qemu_op_class: int = 2,
        qemu_dependency_class: int = 0,
    ) -> tuple[Path, Path]:
        gem5 = root / "gem5"
        qemu = root / "qemu"
        gem5.mkdir()
        qemu.mkdir()
        for core in range(4):
            gem5_records = [
                record(
                    op_class=1,
                    flags=RETIRE | MICRO_OP | LAST_MICRO_OP,
                ),
                record(
                    op_class=-3,
                    flags=RETIRE | MICRO_OP | LAST_MICRO_OP,
                ),
                record(
                    op_class=2,
                    flags=RETIRE | BRANCH | CONDITIONAL | TAKEN
                    | MICRO_OP | LAST_MICRO_OP | SERIALIZE,
                    dependencies=(2,), producer_class=0, n_src=1,
                ),
            ]
            qemu_records = [
                record(
                    op_class=1,
                    flags=RETIRE | MICRO_OP | LAST_MICRO_OP,
                ),
                record(
                    op_class=qemu_op_class,
                    flags=RETIRE | BRANCH | CONDITIONAL | TAKEN
                    | MICRO_OP | LAST_MICRO_OP | SERIALIZE,
                    dependencies=(1,),
                    producer_class=qemu_dependency_class,
                    n_src=1,
                ),
            ]
            write_trace(
                gem5, core, gem5_records, {2: (2,)}, with_io=False
            )
            write_trace(
                qemu, core, qemu_records,
                {1: (1,)}, with_io=True,
            )
        return gem5, qemu

    def test_feature_report_keeps_per_core_and_aggregate_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gem5, qemu = self.create_pair(Path(temporary))
            result = producer_report(
                workload="same-name",
                gem5_fst_dir=gem5,
                qemu_fst_dir=qemu,
            )
        core = result["producers"]["gem5"]["per_core"]["0"]
        self.assertEqual(
            result["scope"], "producer-local-measurement-fst-features"
        )
        self.assertEqual(core["cpl3_uops"], 2)
        self.assertEqual(core["cpl0_uops"], 1)
        self.assertEqual(core["macros"], 2)
        self.assertEqual(core["op_class"], {"1": 1, "2": 1})
        self.assertEqual(core["dependencies"]["total"], 1)
        self.assertEqual(
            result["producers"]["gem5"]["aggregate"]["cpl3_uops"], 8
        )
        self.assertEqual(
            result["delta"]["per_core"]["0"]["cpl3_uops"][
                "qemu_minus_gem5"
            ],
            0,
        )

    def test_op_class_mismatch_is_reported_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gem5, qemu = self.create_pair(
                Path(temporary), qemu_op_class=3
            )
            result = producer_report(
                workload="same-name",
                gem5_fst_dir=gem5,
                qemu_fst_dir=qemu,
            )
        self.assertEqual(
            result["delta"]["aggregate"]["op_class"]["2"][
                "qemu_minus_gem5"
            ],
            -4,
        )
        self.assertNotIn("status", result)
        self.assertNotIn("certificate", result)

    def test_dependency_mismatch_is_reported_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gem5, qemu = self.create_pair(
                Path(temporary), qemu_dependency_class=1
            )
            result = producer_report(
                workload="same-name",
                gem5_fst_dir=gem5,
                qemu_fst_dir=qemu,
            )
        self.assertEqual(
            result["delta"]["aggregate"]["dependencies"][
                "producer_classes"
            ]["0"]["qemu_minus_gem5"],
            -4,
        )
        self.assertEqual(
            result["delta"]["aggregate"]["dependencies"][
                "producer_classes"
            ]["1"]["qemu_minus_gem5"],
            4,
        )

    def test_zero_reference_bucket_has_null_relative_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gem5, qemu = self.create_pair(
                Path(temporary), qemu_op_class=3,
            )
            result = producer_report(
                workload="same-name",
                gem5_fst_dir=gem5,
                qemu_fst_dir=qemu,
            )
        delta = result["delta"]["aggregate"]["op_class"]["3"]
        self.assertEqual(delta["gem5"], 0)
        self.assertEqual(delta["qemu"], 4)
        self.assertIsNone(delta["relative_to_gem5"])

    def test_invalid_dependency_companion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gem5, qemu = self.create_pair(Path(temporary))
            dependency = gem5 / "core0.fst.deps"
            dependency.write_bytes(dependency.read_bytes()[:-1])
            with self.assertRaisesRegex(
                ValueError, "dependency table size/count mismatch",
            ):
                producer_report(
                    workload="same-name",
                    gem5_fst_dir=gem5,
                    qemu_fst_dir=qemu,
                )

    def test_non_microop_completes_a_macro(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gem5, qemu = self.create_pair(root)
            for directory in (gem5, qemu):
                records = [
                    record(op_class=1, flags=RETIRE),
                    record(
                        op_class=2,
                        flags=RETIRE | MICRO_OP | LAST_MICRO_OP,
                    ),
                ]
                write_trace(
                    directory, 0, records, {},
                    with_io=directory == qemu,
                )
            result = producer_report(
                workload="same-name",
                gem5_fst_dir=gem5,
                qemu_fst_dir=qemu,
            )
        self.assertEqual(
            result["producers"]["gem5"]["per_core"]["0"]["macros"], 2
        )


if __name__ == "__main__":
    unittest.main()
