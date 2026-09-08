import tempfile
import unittest
from pathlib import Path

from tools.qemu_fst._assets import _write_user_workload_disk
from tools.qemu_fst.capture import FstAssets, _capture_completed
from tools.qemu_fst.lower import _measurement_manifest
from tools.qemu_fst.workloads import (
    Workload, load_workloads, selected_workloads,
)


class QemuFstTest(unittest.TestCase):
    def test_capture_completion_requires_started_workload_and_all_shards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "qemu-system.log"
            log.write_text(
                "[qemu-fst-init] workload=case uid=1000 gid=1000\n"
            )
            for core in range(4):
                shard = root / f"thread.{core}.trace.gz"
                shard.write_bytes(b"trace")
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
            log.write_text(
                "[qemu-fst-init] workload=case uid=1000 gid=1000\n"
                "[qemu-fst-init] workload=case status=1\n"
            )
            (root / "thread.3.trace.gz").write_bytes(b"trace")
            self.assertTrue(
                _capture_completed(log, "case", root, expected_cores=4)
            )

    def test_selection_rejects_unknown_workload(self) -> None:
        workload = Workload(
            name="case", binary="case", argv=(), environment={},
            omp_threads=4,
        )
        self.assertEqual(selected_workloads((workload,), ("case",)), [workload])
        with self.assertRaisesRegex(ValueError, "unknown"):
            selected_workloads((workload,), ("missing",))

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

    def test_spec2026_workloads_are_c4_run_directories(self) -> None:
        descriptor = (
            Path(__file__).resolve().parents[1]
            / "configs/qemu_fst/spec2026_c4.json"
        )
        workloads = load_workloads(descriptor)
        self.assertTrue(workloads)
        self.assertTrue(all(workload.omp_threads == 4 for workload in workloads))
        self.assertTrue(all(workload.run_directory for workload in workloads))

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

    def test_measurement_manifest_preserves_exact_record_boundaries(self) -> None:
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
