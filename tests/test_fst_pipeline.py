import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import fst_pmu_compare
from tools.fst_pipeline import __main__ as pipeline_cli
from tools.fst_pipeline.descriptor import load_pipeline, select
from tools.qemu_fst.accept import _accept_one
from tools.qemu_fst._assets import _write_user_workload_disk
from tools.qemu_fst.capture import (
    FstAssets, _capture_completed, collect_workload,
)
from tools.qemu_fst.lower import _measurement_manifest
from tools.qemu_fst.workloads import (
    Workload, parse_memory_bytes, run_fastsim,
)


def _stats_document(
    *,
    warmup: bool,
    scope: str = "user",
    dram_size: int = 3 * 1024**3,
    static_span_lookups: tuple[int, ...] = (1, 1, 1, 1),
) -> dict:
    return {
        "schema": "fastsim-stats-v5",
        "measurement_scope": scope,
        "totals": {"functional_warmup_enabled": warmup},
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
            "dram": {"size_bytes": dram_size},
        },
    }


class DescriptorTest(unittest.TestCase):
    def test_formal_descriptor_keeps_graph500(self) -> None:
        pipeline = load_pipeline()
        self.assertEqual(len(pipeline.workloads), 10)
        self.assertIn(
            "854.graph500_s",
            [workload.name for workload in pipeline.workloads],
        )
        self.assertEqual(pipeline.user_fst_target, 10_000_000)
        self.assertEqual(pipeline.raw_macro_envelope, 10_000_000)
        self.assertEqual(pipeline.measurement_scope, "user")
        self.assertEqual(pipeline.memory, "3G")
        self.assertEqual(parse_memory_bytes(pipeline.memory), 3 * 1024**3)
        self.assertEqual(pipeline.isa_baseline, "x86-64")
        self.assertEqual(pipeline.timezone, "UTC")
        self.assertEqual(pipeline.network, "disabled")
        self.assertIn("root=/dev/sda2", pipeline.kernel_args)
        self.assertTrue(
            all(workload.omp_threads == 4 for workload in pipeline.workloads)
        )
        self.assertTrue(
            all(workload.run_directory for workload in pipeline.workloads)
        )
        self.assertEqual(
            [entry["name"] for entry in pipeline.reference_unavailable],
            ["854.graph500_s"],
        )

    def test_pilot_selection_is_qemu_workload_subset(self) -> None:
        pipeline = load_pipeline()
        selected = select(pipeline, (), pilots=True)
        self.assertEqual(
            [workload.name for workload in selected],
            ["706.stockfish_r", "710.omnetpp_r", "857.namd_s"],
        )

    def test_selection_rejects_unknown_workload(self) -> None:
        pipeline = load_pipeline()
        with self.assertRaisesRegex(ValueError, "unknown"):
            select(pipeline, ("missing",))


class PipelineCliTest(unittest.TestCase):
    def test_compare_requires_a_taotrace_source(self) -> None:
        with self.assertRaises(SystemExit):
            pipeline_cli.main(["compare", "--run-id", "case"])

    def test_compare_does_not_collect_taotrace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                pipeline_cli.fst_pmu_compare, "run", return_value=0
            ) as compare:
                code = pipeline_cli.main(
                    [
                        "compare", "--run-id", "case",
                        "--taotrace-root", str(root),
                    ]
                )
            self.assertEqual(code, 0)
            compare.assert_called_once()
            args = compare.call_args.args[0]
            self.assertEqual(
                args.output_root,
                pipeline_cli.DEFAULT_DIAGNOSTICS_ROOT
                / "comparisons/case/c04",
            )

    def test_lower_uses_explicit_raw_run(self) -> None:
        with mock.patch.object(
            pipeline_cli.qemu_accept, "lower_with_workloads", return_value=0
        ) as lower:
            code = pipeline_cli.main(
                [
                    "lower",
                    "--run-id", "lowered",
                    "--raw-run-id", "captured",
                    "--workload", "777.zstd_r",
                ]
            )
        self.assertEqual(code, 0)
        args = lower.call_args.args[0]
        self.assertEqual(args.output_root.name, "qemu")
        self.assertEqual(args.output_root.parent.name, "lowered")
        self.assertEqual(args.raw_root.parent.name, "captured")

    def test_replay_uses_explicit_run(self) -> None:
        with mock.patch.object(
            pipeline_cli.qemu_accept, "replay_with_workloads", return_value=0
        ) as replay:
            code = pipeline_cli.main(
                [
                    "replay",
                    "--run-id", "lowered",
                    "--workload", "811.tealeaf_s",
                ]
            )
        self.assertEqual(code, 0)
        args = replay.call_args.args[0]
        self.assertEqual(args.output_root.parent.name, "lowered")
        self.assertEqual(args.measurement_scope, "user")

    def test_prepare_forwards_shared_guest_environment(self) -> None:
        with mock.patch.object(
            pipeline_cli.qemu_prepare, "prepare_pipeline", return_value=0
        ) as prepare:
            code = pipeline_cli.main(["prepare"])
        self.assertEqual(code, 0)
        kwargs = prepare.call_args.kwargs
        self.assertEqual(kwargs["timezone"], "UTC")
        self.assertEqual(kwargs["isa_baseline"], "x86-64")

    def test_run_forwards_shared_capture_and_replay_environment(self) -> None:
        with mock.patch.object(
            pipeline_cli.qemu_accept, "run_with_workloads", return_value=0
        ) as run:
            code = pipeline_cli.main(
                ["run", "--run-id", "case", "--workload", "706.stockfish_r"]
            )
        self.assertEqual(code, 0)
        args = run.call_args.args[0]
        self.assertEqual(args.memory, "3G")
        self.assertEqual(args.network, "disabled")
        self.assertEqual(args.measurement_scope, "user")
        self.assertEqual(args.capture_instruction_limit, 10_000_000)
        self.assertIn("root=/dev/sda2", args.kernel_args)


class CompareTest(unittest.TestCase):
    def test_compare_rejects_wire_errors(self) -> None:
        with mock.patch.object(
            fst_pmu_compare,
            "_summarize_fst",
            return_value=[
                fst_pmu_compare.HeaderSummary(0, 1, 0, 0),
            ],
        ), mock.patch.object(
            fst_pmu_compare,
            "_summarize_fst_dir",
            return_value=[
                fst_pmu_compare.HeaderSummary(0, 1, 0, 0),
            ],
        ), self.assertRaisesRegex(ValueError, "incompatible"):
            fst_pmu_compare._compare_run(
                "case", Path("/qemu"), Path("/tao"), Path("/tao/stats.json")
            )

    def test_aggregate_uses_taotrace_as_reference(self) -> None:
        workloads = [
            {
                "workload": "a",
                "sections": {
                    "scope": [
                        {
                            "field": field,
                            "qemu_fst": 80 if field == "cpi" else 0,
                            "taotrace": 100 if field == "cpi" else 0,
                        }
                        for field in fst_pmu_compare.SCOPE_FIELDS
                    ],
                    "pmu": [
                        {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.PMU_FIELDS
                    ],
                    "memory_hierarchy_user": [
                        {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.MEMORY_HIERARCHY_FIELDS
                    ],
                    "throughput": [
                        {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.THROUGHPUT_FIELDS
                    ],
                },
            },
            {
                "workload": "b",
                "sections": {
                    "scope": [
                        {
                            "field": field,
                            "qemu_fst": 120 if field == "cpi" else 0,
                            "taotrace": 100 if field == "cpi" else 0,
                        }
                        for field in fst_pmu_compare.SCOPE_FIELDS
                    ],
                    "pmu": [
                        {"field": field, "qemu_fst": 1, "taotrace": 0}
                        if field == "remote_supplies"
                        else {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.PMU_FIELDS
                    ],
                    "memory_hierarchy_user": [
                        {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.MEMORY_HIERARCHY_FIELDS
                    ],
                    "throughput": [
                        {"field": field, "qemu_fst": 0, "taotrace": 0}
                        for field in fst_pmu_compare.THROUGHPUT_FIELDS
                    ],
                },
            },
        ]
        aggregate = fst_pmu_compare._aggregate_report(workloads)
        cpi = next(
            row
            for row in aggregate["frontend_timing"]["fields"]
            if row["field"] == "cpi"
        )
        self.assertAlmostEqual(cpi["mape"], 0.2)
        self.assertAlmostEqual(cpi["wape"], 0.2)
        self.assertAlmostEqual(cpi["signed_aggregate"], 0.0)
        remote = next(
            row
            for row in aggregate["producer_sensitive_memory"]["fields"]
            if row["field"] == "remote_supplies"
        )
        self.assertEqual(remote["both_zero_cases"], 1)
        self.assertEqual(remote["undefined_relative_cases"], 1)
        self.assertEqual(remote["finite_relative_cases"], 1)
        self.assertIsNone(remote["wape"])

    def test_frontend_gap_only_excludes_frontend_domain(self) -> None:
        workload = {
            "workload": "case",
            "static_span": {"comparable": False},
            "sections": {
                "scope": [
                    {"field": field, "qemu_fst": 1, "taotrace": 1}
                    for field in fst_pmu_compare.SCOPE_FIELDS
                ],
                "pmu": [
                    {"field": field, "qemu_fst": 1, "taotrace": 1}
                    for field in fst_pmu_compare.PMU_FIELDS
                ],
                "memory_hierarchy_user": [
                    {"field": field, "qemu_fst": 1, "taotrace": 1}
                    for field in fst_pmu_compare.MEMORY_HIERARCHY_FIELDS
                ],
                "throughput": [
                    {"field": field, "qemu_fst": 1, "taotrace": 1}
                    for field in fst_pmu_compare.THROUGHPUT_FIELDS
                ],
            },
        }
        aggregate = fst_pmu_compare._aggregate_report([workload])
        self.assertEqual(
            aggregate["functional_population"]["workloads"], ["case"]
        )
        self.assertEqual(
            aggregate["producer_sensitive_memory"]["workloads"], ["case"]
        )
        self.assertEqual(aggregate["frontend_timing"]["workloads"], [])
        self.assertTrue(
            all(
                row["cases"] == 0
                for row in aggregate["frontend_timing"]["fields"]
            )
        )

    def test_compare_rejects_cold_or_wrong_scope_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qemu = root / "qemu/case"
            taotrace = root / "taotrace/case"
            output = root / "output"
            (qemu / "replay").mkdir(parents=True)
            (taotrace / "replay").mkdir(parents=True)
            (qemu / "replay/stats.json").write_text(
                json.dumps(_stats_document(warmup=False)), encoding="utf-8"
            )
            (taotrace / "replay/stats.json").write_text(
                json.dumps(_stats_document(warmup=True)), encoding="utf-8"
            )
            args = argparse.Namespace(
                qemu_root=root / "qemu",
                taotrace_root=root / "taotrace",
                taotrace_dataset=None,
                output_root=output,
                workload=["case"],
            )
            with mock.patch.object(
                fst_pmu_compare, "_summarize_fst", return_value=[]
            ), mock.patch.object(
                fst_pmu_compare, "_summarize_fst_dir", return_value=[]
            ), self.assertRaisesRegex(ValueError, "warmup"):
                fst_pmu_compare.run(args)

            (qemu / "replay/stats.json").write_text(
                json.dumps(
                    _stats_document(warmup=True, scope="user-plus-kernel")
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                fst_pmu_compare, "_summarize_fst", return_value=[]
            ), mock.patch.object(
                fst_pmu_compare, "_summarize_fst_dir", return_value=[]
            ), self.assertRaisesRegex(ValueError, "measurement_scope=user"):
                fst_pmu_compare.run(args)

            (qemu / "replay/stats.json").write_text(
                json.dumps(
                    _stats_document(
                        warmup=True, dram_size=4 * 1024**3
                    )
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                fst_pmu_compare, "_summarize_fst", return_value=[]
            ), mock.patch.object(
                fst_pmu_compare, "_summarize_fst_dir", return_value=[]
            ), self.assertRaisesRegex(ValueError, "topology differs: dram"):
                fst_pmu_compare.run(args)

    def test_compare_consumes_origin_inference_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qemu = root / "qemu/706.stockfish_r"
            (qemu / "replay").mkdir(parents=True)
            (qemu / "replay/stats.json").write_text(
                json.dumps(_stats_document(warmup=True)), encoding="utf-8"
            )
            dataset = root / "dataset"
            case_dir = dataset / "cases/04c-706.stockfish_r/tao_trace"
            case_dir.mkdir(parents=True)
            (dataset / "index.json").write_text(
                json.dumps(
                    {
                        "oracle_validity": {
                            "pmu_contract_id": fst_pmu_compare.PMU_CONTRACT_ID
                        },
                        "cases": [
                            {"cores": 4, "workload": "706.stockfish_r"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            inference = root / "inference"
            tao_case = inference / "cases/04c-706.stockfish_r"
            tao_case.mkdir(parents=True)
            (tao_case / "user.json").write_text(
                json.dumps(_stats_document(warmup=True)), encoding="utf-8"
            )
            args = argparse.Namespace(
                qemu_root=root / "qemu",
                taotrace_dataset=dataset,
                taotrace_inference=inference,
                taotrace_root=None,
                cores=4,
                output_root=root / "output",
                workload=[],
            )
            with mock.patch.object(
                fst_pmu_compare, "_summarize_fst", return_value=[]
            ), mock.patch.object(
                fst_pmu_compare, "_summarize_fst_dir", return_value=[]
            ):
                code = fst_pmu_compare.run(args)
            self.assertEqual(code, 0)
            report = json.loads(
                (root / "output/pmu.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(report["workloads"]), 1)
            self.assertEqual(
                report["workloads"][0]["status"], "diagnostic_only"
            )


class QemuProducerTest(unittest.TestCase):
    def test_replay_uses_origin_fastsim_fs_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "stats.json"

            def run(command, **_kwargs):
                self.assertEqual(
                    command[command.index("--allow-mmio-escape") + 1],
                    "true",
                )
                self.assertEqual(
                    command[command.index("--dram-size") + 1],
                    str(3 * 1024**3),
                )
                output.write_text("{}\n", encoding="utf-8")
                return argparse.Namespace(returncode=0, stdout="")

            with mock.patch(
                "tools.qemu_fst.workloads.subprocess.run", side_effect=run
            ):
                run_fastsim(
                    fastsim=root / "fastsim",
                    config=root / "config",
                    manifest=root / "manifest",
                    output=output,
                    log=root / "fastsim.log",
                    dram_size=parse_memory_bytes("3G"),
                )

    def test_existing_acceptance_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "c04/case"
            (output / "fst").mkdir(parents=True)
            (output / "fst/manifest.txt").write_text("manifest\n")
            (output / "replay").mkdir()
            (output / "replay/stats.json").write_text("{}\n")
            asset = root / "asset"
            asset.write_text("asset\n")
            args = argparse.Namespace(output_root=root)
            with self.assertRaisesRegex(
                FileExistsError, "output is immutable"
            ):
                _accept_one(
                    args=args,
                    workload=Workload(
                        name="case", binary="case", argv=(), environment={},
                        omp_threads=4,
                    ),
                    assets=FstAssets(asset, asset, asset, asset, asset, asset),
                    converter=asset,
                    fastsim=asset,
                    replay_config=asset,
                )

    def test_capture_completion_requires_started_workload_and_all_shards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "qemu-system.log"
            log.write_text(
                "[qemu-fst-runner] workload=case uid=1000 gid=1000\n"
            )
            for core in range(4):
                (root / f"thread.{core}.trace.gz").write_bytes(b"trace")
            (root / ".capture-complete").write_text("complete\n")
            self.assertTrue(
                _capture_completed(log, "case", root, expected_cores=4)
            )
            self.assertFalse(
                _capture_completed(log, "other", root, expected_cores=4)
            )
            (root / "thread.3.trace.gz").unlink()
            self.assertFalse(
                _capture_completed(log, "case", root, expected_cores=4)
            )

    def test_capture_assets_are_explicit_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "asset"
            asset.write_text("x")
            FstAssets(asset, asset, asset, asset, asset, asset).require()
            with self.assertRaisesRegex(FileNotFoundError, "plugin"):
                FstAssets(
                    asset, asset, asset, asset, root / "missing", asset
                ).require()

    def test_capture_forwards_kernel_args_and_disables_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "asset"
            asset.write_text("x")
            output = root / "output"
            workload = Workload(
                name="case", binary="case", argv=(), environment={},
                omp_threads=4,
            )

            def run(command, **_kwargs):
                self.assertIn("--network", command)
                self.assertEqual(
                    command[command.index("--network") + 1], "disabled"
                )
                kernel_args = [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "--kernel-arg"
                ]
                self.assertEqual(
                    kernel_args, ["console=ttyS0", "root=/dev/sda2"]
                )
                output.mkdir(exist_ok=True)
                (output / "qemu-system.log").write_text(
                    "[qemu-fst-runner] workload=case uid=1000 gid=1000\n"
                )
                for core in range(4):
                    (output / f"thread.{core}.trace.gz").write_bytes(b"trace")
                (output / ".capture-complete").write_text("complete\n")
                return argparse.Namespace(returncode=0)

            with mock.patch(
                "tools.qemu_fst.capture.subprocess.run", side_effect=run
            ):
                result = collect_workload(
                    workload=workload,
                    raw_macro_envelope=100,
                    kernel_args=("console=ttyS0", "root=/dev/sda2"),
                    network="disabled",
                    assets=FstAssets(
                        asset, asset, asset, asset, asset, asset
                    ),
                    output_dir=output,
                )
            self.assertEqual(result.trace_dir, output)

    def test_workload_disk_accepts_complete_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            binary = run / "case.qemu"
            binary.write_bytes(b"\x7fELF")
            binary.chmod(0o755)
            (run / "input.dat").write_text("input\n")
            workload = Workload(
                name="case",
                binary="case.qemu",
                argv=("input.dat",),
                environment={"CASE_THREADS": "4"},
                omp_threads=4,
                run_directory=str(run),
            )
            staging = root / "staging"
            staging.mkdir()
            image = _write_user_workload_disk((workload,), staging)
            self.assertTrue(image.is_file())

    def test_measurement_manifest_preserves_exact_record_boundaries(
        self,
    ) -> None:
        manifest = _measurement_manifest(
            {
                "0": {
                    "warmup_instructions": 12,
                    "warmup_records": 24,
                    "measurement_instructions": 7,
                    "measurement_records": 10,
                },
                "1": {
                    "warmup_instructions": 15,
                    "warmup_records": 30,
                    "measurement_instructions": 8,
                    "measurement_records": 11,
                },
            },
            2,
        )
        self.assertEqual(
            manifest.splitlines(),
            [
                "0 fastsim-binary-warmup-slice core0.fst 0 12 7 24 10",
                "1 fastsim-binary-warmup-slice core1.fst 1 15 8 30 11",
            ],
        )


if __name__ == "__main__":
    unittest.main()
