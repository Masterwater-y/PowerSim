#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import fst_pmu_compare
from tools.audit_fst_static_instruction_maps import audit_map
from tools.build_fst_instruction_map import (
    FST_HEADER,
    IMAP_HEADER,
    load_rows,
    write_map,
)
from tools.fst_pipeline import __main__ as pipeline_cli
from tools.taotrace_fst.collect import _require_collection_root
from tools.taotrace_fst.normalize import normalize


def stats_document(
    static_span_lookups: tuple[int, ...] = (1, 1, 1, 1),
) -> dict:
    return {
        "schema": "fastsim-stats-v5",
        "measurement_scope": "user",
        "totals": {"functional_warmup_enabled": True},
        "scope_metrics": {},
        "cores": [
            {
                "core": core,
                "fetch_supply_static_span_lookups": lookups,
                "fetch_supply_static_span_unavailable": int(lookups == 0),
            }
            for core, lookups in enumerate(static_span_lookups)
        ],
        "configuration": {
            "cores": 4,
            "core_frequencies_hz": [3_000_000_000] * 4,
            "l1d": {"size_bytes": 32 * 1024},
            "l2": {"size_bytes": 1024 * 1024},
            "llc": {"size_bytes": 64 * 1024**2},
            "dram": {"size_bytes": 3 * 1024**3},
        },
    }


class PipelineGuardTest(unittest.TestCase):
    def test_taotrace_normalize_writes_portable_fst_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "board.processor.switch0.core.records.micro.fst"
            source.write_bytes(
                FST_HEADER.pack(
                    b"FSTRC01\0", 7, 72, 64, 0, 2, 1 << 2,
                    0, 0, 0, 1,
                )
                + bytes(2 * 64)
            )
            (root / "functional-boundary-core0.json").write_text(
                json.dumps({
                    "schema": "tcsim-functional-boundary-v1",
                    "functional_warmup_enabled": True,
                    "measurement_started": True,
                    "target_reached": True,
                    "trace_scope": "user",
                    "total_records": 2,
                    "warmup_records": 1,
                    "measurement_records": 1,
                    "warmup_instructions": 1,
                    "measurement_instructions": 1,
                }),
                encoding="utf-8",
            )
            with mock.patch(
                "tools.taotrace_fst.normalize.audit_map",
                return_value={
                    "present": True,
                    "instruction_rows": 1,
                    "operands_complete": True,
                },
            ):
                metadata = normalize(root, 1)
            self.assertEqual(metadata["per_core"]["0"]["fst"], "core0.fst")
            self.assertTrue((root / "core0.fst").is_file())

    def test_asid_scoped_imap_keeps_equal_pcs_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fst = root / "core0.fst"
            fst.write_bytes(
                FST_HEADER.pack(
                    b"FSTRC01\0", 7, 72, 64, 0, 2, 1 << 2,
                    0, 0, 0, 0,
                )
                + bytes(2 * 64)
            )
            asmap = Path(str(fst) + ".asmap")
            asmap.write_bytes(
                struct.pack(
                    "<8sIIIIQQQ", b"FSTASM1\0", 1, 48, 16, 0, 2, 2, 0
                )
                + struct.pack("<QQ", 0, 7)
                + struct.pack("<QQ", 1, 11)
            )
            decoded = root / "decoded.jsonl"
            decoded.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "address_space_id": address_space,
                            "pc": "0x1000",
                            "size": size,
                            "read_register_ids": [address_space],
                            "write_register_ids": [],
                        }
                    )
                    for address_space, size in ((7, 4), (11, 5))
                )
                + "\n",
                encoding="utf-8",
            )
            rows = load_rows(decoded)
            write_map(Path(str(fst) + ".imap"), 0, 2, rows, False)
            report = audit_map(fst)
            self.assertEqual(report["address_spaces"], [7, 11])
            self.assertEqual(report["instruction_rows"], 2)

            imap = Path(str(fst) + ".imap")
            data = bytearray(imap.read_bytes())
            data[:8] = b"FSTIMP2\0"
            imap.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "header"):
                audit_map(fst)

    def test_production_qemu_run_is_read_only(self) -> None:
        for command in ("run", "replay"):
            with self.subTest(command=command), mock.patch(
                "sys.stderr"
            ) as stderr:
                code = pipeline_cli.main([
                    command,
                    "--run-id", "production",
                    "--workload", "777.zstd_r",
                ])
                self.assertEqual(code, 2)
                self.assertIn(
                    "read-only promoted result",
                    "".join(
                        call.args[0] for call in stderr.write.call_args_list
                    ),
                )
        with mock.patch("sys.stderr") as stderr:
            code = pipeline_cli.main([
                "lower",
                "--run-id", "production",
                "--raw-run-id", "production",
                "--workload", "777.zstd_r",
            ])
            self.assertEqual(code, 2)
            self.assertIn(
                "read-only promoted result",
                "".join(
                    call.args[0] for call in stderr.write.call_args_list
                ),
            )

    def test_run_id_is_one_directory_name(self) -> None:
        for run_id in ("../production", "nested/run"):
            with self.subTest(run_id=run_id), mock.patch("sys.stderr"):
                self.assertEqual(
                    pipeline_cli.main([
                        "replay",
                        "--run-id", run_id,
                        "--workload", "777.zstd_r",
                    ]),
                    2,
                )

    def test_taotrace_collection_rejects_qemu_and_promoted_roots(self) -> None:
        protected = (
            PROJECT_ROOT / "var/qemu_fst/runs/candidate/taotrace/c04",
            PROJECT_ROOT
            / "var/qemu_fst/diagnostics/taotrace-reference/c04",
        )
        for root in protected:
            with self.subTest(root=root), self.assertRaisesRegex(
                ValueError, "QEMU run or promoted reference root"
            ):
                _require_collection_root(root)

    def test_compare_excludes_asymmetric_static_span_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qemu = root / "qemu/case"
            taotrace = root / "taotrace/case"
            output = root / "output"
            (qemu / "replay").mkdir(parents=True)
            (taotrace / "replay").mkdir(parents=True)
            (qemu / "replay/stats.json").write_text(
                json.dumps(stats_document()), encoding="utf-8"
            )
            (taotrace / "replay/stats.json").write_text(
                json.dumps(stats_document((1, 0, 1, 1))),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                qemu_root=root / "qemu",
                taotrace_root=root / "taotrace",
                taotrace_dataset=None,
                output_root=output,
                workload=["case"],
                reference_unavailable=set(),
            )
            headers = [
                fst_pmu_compare.HeaderSummary(
                    0,
                    1,
                    fst_pmu_compare.fst_wire.FEATURE_DESTINATION_CLASSES,
                    0,
                ),
            ]
            with mock.patch.object(
                fst_pmu_compare, "_summarize_fst", return_value=headers
            ), mock.patch.object(
                fst_pmu_compare, "_summarize_fst_dir", return_value=headers
            ):
                self.assertEqual(fst_pmu_compare.run(args), 0)
            report = json.loads(
                (output / "pmu.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["coverage"]["functional_compared"], ["case"]
            )
            self.assertEqual(report["coverage"]["frontend_compared"], [])
            self.assertEqual(
                report["coverage"]["frontend_reference_incomplete"], ["case"]
            )
            self.assertEqual(
                report["workloads"][0]["status"],
                "frontend_reference_incomplete",
            )
            cpi = next(
                row
                for row in report["aggregate"]["frontend_timing"]["fields"]
                if row["field"] == "cpi"
            )
            self.assertEqual(cpi["cases"], 0)
            retired = next(
                row
                for row in report["aggregate"]["functional_population"][
                    "fields"
                ]
                if row["field"] == "retired_instructions"
            )
            self.assertEqual(
                report["aggregate"]["functional_population"]["workloads"],
                ["case"],
            )
            self.assertEqual(retired["cases"], 0)


if __name__ == "__main__":
    unittest.main()
