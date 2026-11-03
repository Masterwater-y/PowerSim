"""Guard collection scope, target arguments, resource limits and resume semantics."""
import copy
import importlib
import itertools
import json
import sys
import tempfile
import os
import time
import signal
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    collector = importlib.import_module("collect_uarch_common_end")
except ModuleNotFoundError:
    collector = None


class CollectionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(collector, "common-end uarch collector is not implemented")
        self.matrix = json.loads((ROOT / "configs/spec2026-uarch-generalization-c4-c8-v3.json").read_text())
        self.tso_matrix = json.loads(
            (
                ROOT
                / "configs/spec2026-tso-buffer114-baseline-40-v1.json"
            ).read_text()
        )
        self.tso_matrix_v2 = json.loads(
            (
                ROOT
                / "configs/spec2026-tso-buffer114-baseline-40-v2.json"
            ).read_text()
        )
        self.tso_matrix_v3 = json.loads(
            (
                ROOT
                / "configs/spec2026-tso-buffer114-baseline-40-v3.json"
            ).read_text()
        )

    def test_every_profile_workload_and_core_has_its_own_task(self):
        collector.validate_design(self.matrix)
        tasks = collector.tasks_for(self.matrix)
        self.assertEqual(len(tasks), 136)
        self.assertEqual(len({task["case_id"] for task in tasks}), 136)
        self.assertEqual(sum(task["cores"] for task in tasks), 816)
        counts = {name: sum(t["design_class"] == name for t in tasks)
                  for name in ("baseline", "single", "multi")}
        self.assertEqual(counts, {"baseline": 8, "single": 64, "multi": 64})
        self.assertEqual({t["workload"]["name"] for t in tasks},
                         {"706.stockfish_r", "731.astcenc_r", "811.tealeaf_s", "854.graph500_s"})

    def test_joint_design_covers_every_pair_of_levels_twice(self):
        keys = ("l1d_size", "l2_size", "l3_size_per_bank", "rob_entries")
        joints = [p for p in self.matrix["profiles"] if p["design_class"] == "multi"]
        for a, b in itertools.combinations(keys, 2):
            pairs = [(p["gem5"][a], p["gem5"][b]) for p in joints]
            self.assertEqual(len(set(pairs)), 4)
            self.assertTrue(all(pairs.count(pair) == 2 for pair in set(pairs)))

    def test_tso_baseline_matrix_is_exactly_40_buffer114_cases(self):
        for matrix in (
            self.tso_matrix,
            self.tso_matrix_v2,
            self.tso_matrix_v3,
        ):
            collector.validate_design(matrix)
            tasks = collector.tasks_for(matrix)
            self.assertEqual(len(tasks), 40)
            self.assertEqual(len({task["case_id"] for task in tasks}), 40)
            self.assertEqual(sum(task["cores"] for task in tasks), 600)
            self.assertEqual(
                {task["cores"] for task in tasks}, {4, 8, 16, 32}
            )
            self.assertEqual(
                matrix["required_tso"],
                {"mode": "buffer", "buffer_size": 114, "buffer_active": True},
            )
        self.assertEqual(
            self.tso_matrix_v2["required_checkpoint_recovery"],
            {
                "timeout_seconds": 7200,
                "max_output_mib": 256,
                "post_workbegin_insts_per_core": 1,
                "tao_emit_records": False,
            },
        )
        self.assertEqual(
            self.tso_matrix_v3["required_checkpoint_recovery"],
            self.tso_matrix_v2["required_checkpoint_recovery"],
        )

    def test_tso_baseline_rejects_wrong_mode_or_capacity(self):
        for mode, size in (("serial", 114), ("buffer", 32)):
            args = type(
                "Options",
                (),
                {"tso_store": mode, "tso_store_buffer_size": size},
            )()
            with self.assertRaisesRegex(ValueError, "matrix requires"):
                collector.validate_tso_selection(args, self.tso_matrix)
        args = type(
            "Options",
            (),
            {"tso_store": "buffer", "tso_store_buffer_size": 114},
        )()
        collector.validate_tso_selection(args, self.tso_matrix)

    def test_v2_tso_baseline_locks_checkpoint_recovery_budget(self):
        args = type(
            "Options",
            (),
            {
                "checkpoint_recovery_timeout": 7200,
                "checkpoint_recovery_max_mib": 256,
            },
        )()
        collector.validate_checkpoint_recovery_selection(
            args, self.tso_matrix_v2
        )
        collector.validate_checkpoint_recovery_selection(args, self.tso_matrix)
        args.checkpoint_recovery_timeout = 86400
        with self.assertRaisesRegex(ValueError, "requires checkpoint recovery"):
            collector.validate_checkpoint_recovery_selection(
                args, self.tso_matrix_v2
            )

    def test_v2_tso_baseline_locks_runtime_libstdcxx(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_lib = Path(directory)
            library = runtime_lib / "libstdc++.so.6"
            library.write_bytes(b"frozen-runtime")
            matrix = copy.deepcopy(self.tso_matrix_v2)
            matrix["required_runtime_libstdcxx_sha256"] = collector.sha256(
                library
            )
            args = type("Options", (), {"runtime_lib": runtime_lib})()
            self.assertEqual(
                collector.validate_runtime_library(args, matrix), library
            )
            collector.validate_runtime_library(
                type("Options", (), {"runtime_lib": None})(),
                self.tso_matrix,
            )
            with self.assertRaisesRegex(ValueError, "explicit runtime"):
                collector.validate_runtime_library(
                    type("Options", (), {"runtime_lib": None})(), matrix
                )
            library.write_bytes(b"changed-runtime")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                collector.validate_runtime_library(args, matrix)

    def test_v3_tso_baseline_locks_zstd_prefork_artifacts(self):
        zstd = next(
            workload
            for workload in self.tso_matrix_v3["workloads"]
            if workload["name"] == "777.zstd_r"
        )
        self.assertEqual(
            zstd["required_workload_binary_sha256"],
            collector.ZSTD_PREFORK_BINARY_SHA256,
        )
        self.assertEqual(
            zstd["required_aux_disk_sha256"],
            collector.ZSTD_PREFORK_DISK_SHA256,
        )
        for key in (
            "required_workload_binary_sha256",
            "required_aux_disk_sha256",
        ):
            matrix = copy.deepcopy(self.tso_matrix_v3)
            zstd = next(
                workload
                for workload in matrix["workloads"]
                if workload["name"] == "777.zstd_r"
            )
            zstd[key] = "0" * 64
            with self.assertRaisesRegex(ValueError, "Zstd pre-fork"):
                collector.validate_design(matrix)

    def test_tso_baseline_rejects_changed_workload_or_core_set(self):
        for mutate in (
            lambda matrix: matrix.update(core_counts=[4, 8, 16]),
            lambda matrix: matrix["workloads"].pop(),
            lambda matrix: matrix.update(required_binary_sha256="0" * 64),
        ):
            matrix = copy.deepcopy(self.tso_matrix)
            mutate(matrix)
            with self.assertRaises(ValueError):
                collector.validate_design(matrix)
        matrix = copy.deepcopy(self.tso_matrix_v2)
        matrix["required_runtime_libstdcxx_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "runtime libstd"):
            collector.validate_design(matrix)

    def test_missing_core_or_profile_is_rejected(self):
        for mutate in (lambda m: m.update(core_counts=[4]),
                       lambda m: m["profiles"].pop(),
                       lambda m: m["workloads"].pop()):
            matrix = copy.deepcopy(self.matrix)
            mutate(matrix)
            with self.assertRaises(ValueError):
                collector.validate_design(matrix)

    def test_wrong_total_llc_override_is_rejected(self):
        matrix = copy.deepcopy(self.matrix)
        matrix["profiles"][-1]["fastsim"]["cache.llc.size"] = "16MiB"
        with self.assertRaises(ValueError):
            collector.validate_design(matrix)

    def test_jobs_fill_available_host_without_exceeding_memory(self):
        self.assertEqual(collector.choose_jobs(136, 192, 1900 * 2**30, 12, 64), 136)
        self.assertEqual(collector.choose_jobs(136, 192, 160 * 2**30, 12, 64), 8)
        self.assertEqual(collector.choose_jobs(136, 6, 1900 * 2**30, 12, 64), 6)
        with self.assertRaises(ValueError):
            collector.choose_jobs(136, 192, 64 * 2**30, 12, 64)

    def test_progress_output_ignores_a_closed_pipe(self):
        class ClosedPipe:
            def write(self, _value):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

        self.assertFalse(collector.emit("progress", file=ClosedPipe()))

    def test_formal_only_uses_the_matrix_case_count(self):
        source = (ROOT / "scripts/collect_uarch_formal_only.py").read_text()
        self.assertIn("case_count = len(tasks)", source)
        self.assertIn("expected_cases=case_count", source)
        self.assertIn("total=case_count", source)
        self.assertIn("formal_cases=case_count", source)
        self.assertNotIn("/136", source)

    def test_capture_command_sets_common_end_and_per_bank_geometry(self):
        task = next(t for t in collector.tasks_for(self.matrix)
                    if t["profile"]["id"] == "j8_large_all" and t["cores"] == 8)
        command = collector.capture_command(task, self.matrix, Path("/binary/gem5.opt"),
                    Path("/repo/TCSim"), Path("/resources"), Path("/checkpoint"),
                    Path("/output/case"), 10000000)
        def arg(flag):
            self.assertEqual(command.count(flag), 1)
            return command[command.index(flag) + 1]
        self.assertEqual(arg("--roi-stop-policy"), "any-core")
        self.assertEqual(arg("--roi-user-records-per-core"), "10000000")
        self.assertEqual(arg("--tao-functional-user-target"), "10000000")
        self.assertEqual(arg("--l3-size"), "16MiB")
        self.assertEqual(arg("--rob-entries"), "256")
        self.assertEqual(arg("--num-cores"), "8")
        self.assertIn("--tao-functional-warmup", command)
        self.assertIn("--tao-functional-include-kernel", command)
        self.assertNotIn("--tao-native-response-jsonl", command)
        self.assertNotIn("--tao-functional-user-only", command)
        self.assertNotIn("--tso-store", command)
        self.assertNotIn("--tso-store-buffer-size", command)

    def test_buffer_capture_and_prepare_commands_carry_tso_identity(self):
        task = collector.tasks_for(self.matrix)[0]
        command = collector.capture_command(
            task, self.matrix, Path("/binary/gem5.opt"),
            Path("/repo/TCSim"), Path("/resources"), Path("/checkpoint"),
            Path("/output/case"), 10000, "buffer", 114,
        )
        self.assertEqual(command.count("--tso-store"), 1)
        self.assertEqual(command[command.index("--tso-store") + 1], "buffer")
        self.assertEqual(command.count("--tso-store-buffer-size"), 1)
        self.assertEqual(
            command[command.index("--tso-store-buffer-size") + 1], "114"
        )

        args = type(
            "Options",
            (),
            {
                "output": Path("/output"),
                "resources": Path("/resources"),
                "tso_store": "buffer",
                "tso_store_buffer_size": 114,
            },
        )()
        prepare = collector.prepare_command(
            args, self.matrix, task, Path("/preparation")
        )
        self.assertEqual(prepare[prepare.index("--tso-store") + 1], "buffer")
        self.assertEqual(
            prepare[prepare.index("--tso-store-buffer-size") + 1], "114"
        )

    def test_checkpoint_recovery_command_is_no_record_and_bounded(self):
        task = collector.tasks_for(self.tso_matrix)[0]
        command = collector.checkpoint_recovery_command(
            task,
            self.tso_matrix,
            Path("/binary/gem5.opt"),
            Path("/repo/TCSim"),
            Path("/resources"),
            Path("/checkpoint"),
            Path("/output/recovery"),
            "buffer",
            114,
        )
        self.assertIn("--resume-at-roi", command)
        self.assertIn("--wait-for-roi-workbegin", command)
        self.assertIn("--tao-no-records", command)
        self.assertEqual(
            command[command.index("--max-insts-per-core") + 1], "1"
        )
        self.assertNotIn("--roi-user-records-per-core", command)
        self.assertNotIn("--tao-functional-user-target", command)
        self.assertEqual(
            command[command.index("--tso-store") + 1], "buffer"
        )
        self.assertEqual(
            command[command.index("--tso-store-buffer-size") + 1], "114"
        )

    def test_checkpoint_recovery_log_requires_workbegin_and_roi_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "recovery.log"
            log.write_text(
                "[fs-ckpt] WORKBEGIN tick=123\n"
                "[fs-ckpt] MAX_INSTS reached at tick=456 "
                "phase=roi target=1 deltas=[1] exit_code=0\n"
            )
            gate = collector.validate_checkpoint_recovery_log(log)
            self.assertEqual(
                gate,
                {
                    "workbegin_tick": 123,
                    "post_workbegin_exit_tick": 456,
                    "workbegin_events": 1,
                },
            )
            log.write_text(
                "[fs-ckpt] MAX_INSTS reached at tick=456 "
                "phase=roi target=1 deltas=[1] exit_code=0\n"
            )
            with self.assertRaisesRegex(ValueError, "did not reach"):
                collector.validate_checkpoint_recovery_log(log)
            log.write_text("[fs-ckpt] WORKBEGIN tick=123\n")
            with self.assertRaisesRegex(ValueError, "did not complete"):
                collector.validate_checkpoint_recovery_log(log)
            log.write_text(
                "[fs-ckpt] MAX_INSTS reached at tick=456 "
                "phase=roi target=1 deltas=[1] exit_code=0\n"
                "[fs-ckpt] WORKBEGIN tick=123\n"
            )
            with self.assertRaisesRegex(ValueError, "preceded source"):
                collector.validate_checkpoint_recovery_log(log)
            log.write_text(
                "[fs-ckpt] WORKBEGIN tick=456\n"
                "[fs-ckpt] MAX_INSTS reached at tick=123 "
                "phase=roi target=1 deltas=[1] exit_code=0\n"
            )
            with self.assertRaisesRegex(ValueError, "exit tick precedes"):
                collector.validate_checkpoint_recovery_log(log)

    def test_checkpoint_recovery_receipt_is_strict_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = collector.tasks_for(self.tso_matrix)[0]
            checkpoint = root / "checkpoint"
            checkpoint.mkdir()
            (checkpoint / "m5.cpt").write_text("filename=memory.pmem\n")
            (checkpoint / "metadata.json").write_text("{}")
            (checkpoint / "memory.pmem").write_bytes(b"memory")
            preparation = {
                "checkpoint": str(checkpoint),
                "files": collector.checkpoint_snapshot(checkpoint),
                "request": {"schema": "test-checkpoint-request-v1"},
            }
            args = type(
                "Options",
                (),
                {
                    "output": root,
                    "binary": Path("/binary/gem5.opt"),
                    "resources": Path("/resources"),
                    "tso_store": "buffer",
                    "tso_store_buffer_size": 114,
                    "checkpoint_recovery_timeout": 10,
                    "checkpoint_recovery_max_mib": 1,
                },
            )()

            class FakeSupervisor:
                calls = 0

                def run(
                    self, command, log, environment, timeout, *,
                    output_root=None, max_output_bytes=None,
                ):
                    self.calls += 1
                    self.command = command
                    self.output_root = output_root
                    self.max_output_bytes = max_output_bytes
                    log.write_text(
                        "[fs-ckpt] WORKBEGIN tick=123\n"
                        "[fs-ckpt] MAX_INSTS reached at tick=456 "
                        "phase=roi target=1 deltas=[1] exit_code=0\n"
                    )

            stale = (
                root / "preparation" / task["case_id"]
                / "checkpoint-recovery"
            )
            stale.mkdir(parents=True)
            (stale / "interrupted.txt").write_text("preserve me")
            supervisor = FakeSupervisor()
            first = collector.probe_checkpoint_recovery(
                args, self.tso_matrix, task, preparation, supervisor
            )
            second = collector.probe_checkpoint_recovery(
                args, self.tso_matrix, task, preparation, supervisor
            )
            self.assertEqual(supervisor.calls, 1)
            self.assertEqual(first, second)
            self.assertEqual(first["state"], "complete")
            self.assertEqual(first["gate"]["workbegin_tick"], 123)
            self.assertEqual(
                first["contract"]["gem5_binary_sha256"],
                self.tso_matrix["required_binary_sha256"],
            )
            self.assertIn("--tao-no-records", supervisor.command)
            self.assertEqual(supervisor.output_root.name, "checkpoint-recovery")
            self.assertEqual(supervisor.max_output_bytes, 1024 * 1024)
            self.assertFalse(
                list((root / "preparation").rglob("*.fst"))
            )
            interrupted = list(
                (root / "preparation" / task["case_id"]).glob(
                    "checkpoint-recovery.interrupted-*"
                )
            )
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(
                (interrupted[0] / "interrupted.txt").read_text(),
                "preserve me",
            )

    def test_checkpoint_recovery_rejects_record_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = collector.tasks_for(self.tso_matrix)[0]
            checkpoint = root / "checkpoint"
            checkpoint.mkdir()
            (checkpoint / "m5.cpt").write_text("filename=memory.pmem\n")
            (checkpoint / "metadata.json").write_text("{}")
            (checkpoint / "memory.pmem").write_bytes(b"memory")
            preparation = {
                "checkpoint": str(checkpoint),
                "files": collector.checkpoint_snapshot(checkpoint),
                "request": {"schema": "test-checkpoint-request-v1"},
            }
            args = type(
                "Options",
                (),
                {
                    "output": root,
                    "binary": Path("/binary/gem5.opt"),
                    "resources": Path("/resources"),
                    "tso_store": "buffer",
                    "tso_store_buffer_size": 114,
                    "checkpoint_recovery_timeout": 10,
                    "checkpoint_recovery_max_mib": 1,
                },
            )()

            class FakeSupervisor:
                def run(
                    self, command, log, environment, timeout, *,
                    output_root=None, max_output_bytes=None,
                ):
                    log.write_text(
                        "[fs-ckpt] WORKBEGIN tick=123\n"
                        "[fs-ckpt] MAX_INSTS reached at tick=456 "
                        "phase=roi target=1 deltas=[1] exit_code=0\n"
                    )
                    trace = output_root / "trace-scratch" / "core0.fst"
                    trace.parent.mkdir()
                    trace.write_bytes(b"unexpected")

            with self.assertRaisesRegex(ValueError, "emitted FST"):
                collector.probe_checkpoint_recovery(
                    args, self.tso_matrix, task, preparation, FakeSupervisor()
                )
            receipt = collector.load(
                root / "preparation" / task["case_id"]
                / "checkpoint-recovery.json"
            )
            self.assertEqual(receipt["state"], "failed")

    def test_failed_recovery_invalidates_prepared_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = collector.tasks_for(self.tso_matrix)[0]
            checkpoint = root / "checkpoints" / "case" / "checkpoint"
            checkpoint.mkdir(parents=True)
            (checkpoint / "m5.cpt").write_text("filename=memory.pmem\n")
            (checkpoint / "metadata.json").write_text("{}")
            (checkpoint / "memory.pmem").write_bytes(b"memory")
            folder = root / "preparation" / task["case_id"]
            collector.save(folder / "prepared.json", {"checkpoint": str(checkpoint)})
            args = type("Options", (), {"output": root})()
            collector.reject_preparation_after_recovery_failure(
                args,
                task,
                {"checkpoint": str(checkpoint)},
                TimeoutError("did not reach WORKBEGIN"),
            )
            self.assertFalse(checkpoint.exists())
            self.assertEqual(
                len(list(checkpoint.parent.glob("checkpoint.failed-*"))), 1
            )
            self.assertFalse((folder / "prepared.json").exists())
            self.assertEqual(
                len(list(folder.glob("prepared.recovery-failed-*.json"))), 1
            )
            rejection = list(
                folder.glob("checkpoint-recovery-rejection-*.json")
            )
            self.assertEqual(len(rejection), 1)
            self.assertIn(
                "did not reach WORKBEGIN",
                collector.load(rejection[0])["error"],
            )

    def test_smoke_safety_runway_is_not_only_one_million_instructions(self):
        task = collector.tasks_for(self.matrix)[0]
        cmd = collector.capture_command(task, self.matrix, Path("/binary"),
                    Path("/repo"), Path("/resources"), Path("/checkpoint"),
                    Path("/output"), 10000)
        self.assertGreaterEqual(int(cmd[cmd.index("--roi-safety-max-insts-per-core") + 1]), 10000000)

    def test_resume_rejects_changed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            collector.freeze_json(path, {"binary_sha256": "a", "target": 10000000})
            collector.freeze_json(path, {"target": 10000000, "binary_sha256": "a"})
            with self.assertRaisesRegex(ValueError, "changed|mismatch|different"):
                collector.freeze_json(path, {"binary_sha256": "b", "target": 10000000})
            self.assertEqual(json.loads(path.read_text())["binary_sha256"], "a")

    def test_complete_requires_exact_expected_case_set(self):
        self.assertFalse(collector.complete_set(["a", "b"], {"a": "complete"}))
        self.assertFalse(collector.complete_set(["a", "b"], {"a": "complete", "b": "failed"}))
        self.assertFalse(collector.complete_set(["a", "b"], {"a": "complete", "b": "complete", "c": "complete"}))
        self.assertTrue(collector.complete_set(["a", "b"], {"a": "complete", "b": "complete"}))

    def test_checkpoint_snapshot_rejects_missing_memory_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "m5.cpt").write_text("[memory]\nfilename=system.physmem.store0.pmem\n")
            (root / "metadata.json").write_text("{}")
            with self.assertRaises(ValueError):
                collector.checkpoint_snapshot(root)
            memory = root / "system.physmem.store0.pmem"
            memory.write_bytes(b"memory")
            before = collector.checkpoint_snapshot(root)
            memory.write_bytes(b"changed-memory")
            self.assertNotEqual(before, collector.checkpoint_snapshot(root))

    def test_completed_receipt_detects_lost_or_modified_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "core0.fst").write_bytes(b"fst")
            (root / "core0.fst.deps").write_bytes(b"deps")
            receipt = collector.artifact_snapshot(root)
            collector.check_snapshot(root, receipt)
            (root / "core0.fst.deps").unlink()
            with self.assertRaises(ValueError):
                collector.check_snapshot(root, receipt)

    def test_native_gate_checks_false_flags_and_missing_participants(self):
        good = dict(cores=4, native_hierarchy_semantic_comparable=True,
                    structural_conservation=True, target_drain_complete=True)
        collector.require_native_gate(good, 4, {0, 1, 2, 3})
        for key in ("native_hierarchy_semantic_comparable", "structural_conservation", "target_drain_complete"):
            with self.assertRaises(ValueError):
                collector.require_native_gate(dict(good, **{key: False}), 4, {0, 1, 2, 3})
        with self.assertRaises(ValueError):
            collector.require_native_gate(good, 4, {0, 1, 2})

    def test_timeout_terminates_entire_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = ("import subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
            supervisor = collector.Supervisor()
            with self.assertRaises(TimeoutError):
                supervisor.run([sys.executable, "-c", code, str(root / "child.pid")],
                               root / "run.log", os.environ.copy(), 1)
            child = int((root / "child.pid").read_text())
            for _ in range(30):
                stat = Path(f"/proc/{child}/stat")
                if not stat.exists() or stat.read_text().split(") ", 1)[1][0] == "Z":
                    break
                time.sleep(0.1)
            else:
                self.fail("grandchild remained alive after parent timeout")

    def test_output_limit_terminates_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = (
                "from pathlib import Path; import sys,time; "
                "Path(sys.argv[1]).write_bytes(b'x' * 4096); time.sleep(60)"
            )
            supervisor = collector.Supervisor()
            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                supervisor.run(
                    [sys.executable, "-c", code, str(root / "payload.bin")],
                    root / "run.log",
                    os.environ.copy(),
                    10,
                    output_root=root,
                    max_output_bytes=1024,
                )

    def test_prepare_command_matches_real_runner_positional_cli(self):
        runner = ROOT.parent / "TCSim/scripts/gem5_fs_roi.py"
        if not runner.is_file():
            self.skipTest("sibling TCSim parser not installed")
        spec = importlib.util.spec_from_file_location("test_roi_runner", runner)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        task = collector.tasks_for(self.matrix)[0]
        args = type("Options", (), {"output": Path("/output"), "resources": Path("/resources")})()
        command = collector.prepare_command(args, self.matrix, task, Path("/preparation"))
        with patch.object(sys, "argv", command[1:]):
            parsed, argv = module.parse_args()
        self.assertEqual((parsed.action, parsed.workload, parsed.cores), ("prepare", "706.stockfish_r", 4))
        self.assertFalse(parsed.reuse_binary_mismatch)
        self.assertFalse(parsed.reuse_restore_config_mismatch)

    def test_damaged_checkpoint_is_preserved_and_reprepared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = type("Options", (), {"output": root, "resources": root, "prepare_timeout": 5,
                         "binary": collector.DEFAULT_BINARY})()
            task = collector.tasks_for(self.matrix)[0]
            checkpoint = root / "checkpoints/test/checkpoint"
            checkpoint.mkdir(parents=True)
            (checkpoint / "m5.cpt").write_text("filename=memory.pmem\n")
            (checkpoint / "metadata.json").write_text("{}")
            (checkpoint / "memory.pmem").write_bytes(b"memory")
            collector.save(checkpoint.parent / "request.json", {"request": "original"})
            receipt = root / "preparation" / task["case_id"] / "prepared.json"
            collector.save(receipt, dict(checkpoint=str(checkpoint), files=collector.checkpoint_snapshot(checkpoint), request={"request": "original"}))
            (checkpoint / "memory.pmem").unlink()
            class FakeSupervisor:
                stopped = collector.threading.Event()
                def run(self, command, log, environment, timeout):
                    checkpoint.mkdir()
                    (checkpoint / "m5.cpt").write_text("filename=memory.pmem\n")
                    (checkpoint / "metadata.json").write_text("{}")
                    (checkpoint / "memory.pmem").write_bytes(b"new memory")
                    log.write_text(f"[gem5-fs-roi] entry={checkpoint.parent}\n")
            row = collector.prepare_task(args, self.matrix, task, FakeSupervisor())
            self.assertEqual(Path(row["checkpoint"]), checkpoint)
            self.assertEqual(len(list(checkpoint.parent.glob("checkpoint.failed-*"))), 1)
            self.assertEqual(len(list(receipt.parent.glob("prepared.failed-*.json"))), 1)

    def test_failed_restart_invalidates_previous_completion_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = type("Options", (), {"output": root, "matrix": collector.DEFAULT_MATRIX,
                         "disk_reserve_gib": 1})()
            for phase in ("smoke", "formal"):
                collector.save(root / phase / "collection-finished.json", {"state": "complete", "cases": 136})
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                with patch.object(collector, "preflight", side_effect=ValueError("test preflight failure")):
                    with self.assertRaises(ValueError):
                        collector.collect(args)
                for phase in ("smoke", "formal"):
                    self.assertNotEqual(collector.load(root / phase / "collection-finished.json")["state"], "complete")
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    def test_detached_start_records_preflight_failure_and_releases_lock(self):
        # Invalid matrix fails before KVM or gem5. This exercises the actual
        # detached process/flock entrypoint without launching a simulation.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "invalid.json"
            matrix.write_text("{")
            command = [sys.executable, str(ROOT / "tools/collect_uarch_common_end.py"),
                       "start", "--matrix", str(matrix), "--output", str(root / "output")]
            process = subprocess.run(command, text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            for _ in range(50):
                state = root / "output/collector-state.json"
                if state.exists() and collector.load(state).get("state") == "failed":
                    break
                time.sleep(0.1)
            else:
                self.fail("detached failure state was not persisted")
            for _ in range(50):
                try:
                    with collector.exclusive(root / "output"):
                        break
                except ValueError:
                    time.sleep(0.1)
            else:
                self.fail("detached worker did not release its inherited lock")
            self.assertIn("Expecting property name", collector.load(state)["error"])

    def test_source_change_during_formal_phase_cannot_publish_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "frozen-input.txt"
            source.write_text("original")
            args = type("Options", (), {"output": root, "matrix": collector.DEFAULT_MATRIX,
                         "disk_reserve_gib": 1})()
            report = dict(jobs=24, prepare_jobs=24, finalize_jobs=8,
                          inputs={str(source): collector.file_stamp(source)})
            selected = collector.tasks_for(self.matrix)[0]["case_id"]
            def fake_capture(options, matrix, task, preparation, phase, *rest):
                if phase == "formal" and task["case_id"] == selected:
                    source.write_text("changed during formal")
                return dict(case_id=task["case_id"], state="complete")
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                with patch.object(collector, "preflight", return_value=report), \
                     patch.object(collector, "freeze_runtime"), \
                     patch.object(collector, "prepare_task", return_value={"checkpoint": "unused"}), \
                     patch.object(
                         collector,
                         "probe_checkpoint_recovery",
                         return_value={
                             "gate": {"workbegin_tick": 1},
                             "output_bytes": 1,
                         },
                     ), \
                     patch.object(collector, "capture_task", side_effect=fake_capture), \
                     patch("builtins.print"):
                    with self.assertRaisesRegex(ValueError, "artifact changed"):
                        collector.collect(args)
                self.assertEqual(collector.load(root / "smoke/collection-finished.json")["state"], "complete")
                self.assertNotEqual(collector.load(root / "formal/collection-finished.json")["state"], "complete")
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    unittest.main()
