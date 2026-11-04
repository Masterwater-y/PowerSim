"""Directed entry tests without importing the gem5 runtime."""
import importlib.util
import ast
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace as NS
from types import ModuleType
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "gem5-spr/configs/fastsim/collection.py"


def load_policy():
    spec = importlib.util.spec_from_file_location("spr_collection_policy", POLICY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectionEntryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(POLICY.is_file(), "native SPR collection policy is missing")
        self.policy = load_policy()

    def args(self, *extra):
        return self.policy.parse_args([
            "--fastsim-root", str(ROOT), "--workload-binary", "/example/marked",
            "--participant-cores", "4", *extra])

    def test_native_structure_options_cannot_be_overridden_by_legacy_flags(self):
        for option in ("--rob-entries", "--iq-entries", "--tso-store",
                       "--l1d-size", "--clk", "--mem-channels"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.args(option, "8")

    def test_source_companions_enabled_without_claiming_task_ownership(self):
        args = self.args()
        options = self.policy.trace_options(args, "/capture", "/profile", {})
        self.assertEqual(options["trace_format"], "fst")
        for flag in ("functional_include_kernel", "functional_syscall_microops",
                     "functional_frontend", "functional_retirement", "functional_warmup"):
            self.assertIs(options[flag], True, flag)
        self.assertIs(options["require_roi"], False)
        self.assertEqual(options["functional_sync_marker_pc"], 0)
        self.assertNotIn("scheduler_profile_id", options)
        self.assertNotIn("fixed_work_source_core", options)

    def test_attaching_observers_never_writes_native_cpu_parameters(self):
        class NativeCPU:
            def __init__(self):
                object.__setattr__(self, "numROBEntries", 512)
                object.__setattr__(self, "instQueues", (128, 72, 48))
                object.__setattr__(self, "frontend", object())

            def __setattr__(self, name, value):
                if name != "tao_trace":
                    raise AssertionError("native CPU modified: " + name)
                object.__setattr__(self, name, value)

        cpus = [NativeCPU() for _ in range(4)]
        before = [vars(cpu).copy() for cpu in cpus]
        options = self.policy.trace_options(self.args(), "/capture", "/profile", {})
        self.policy.attach_traces(cpus, lambda **kw: NS(**kw), options)
        for cpu, original in zip(cpus, before):
            self.assertEqual({k: v for k, v in vars(cpu).items() if k != "tao_trace"}, original)
            self.assertIs(cpu.tao_trace.manager, cpu)

    def test_unproven_scheduler_profile_combinations_fail_closed(self):
        for extra in (("--thread-state",), ("--scheduler-profile", "/profile")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.policy.validate_options(self.args(*extra))

    def test_common_end_declares_the_actual_o3_participants(self):
        args = self.args("--functional-user-target", "1234")
        self.policy.validate_options(args)
        options = self.policy.trace_options(args, "/capture", "/profile", {})
        self.assertEqual(options["functional_user_target"], 1234)
        self.assertEqual(self.policy.participant_ids(args), [0, 1, 2, 3])

    def test_observer_profile_reads_native_objects_instead_of_old_profile_defaults(self):
        def cache(size, assoc, fill=0):
            return NS(size=size, assoc=assoc, fill_capacity=fill,
                      replacement_policy=NS(type="LRURP"))

        hierarchy = NS(
            _l1_controllers=[NS(Dcache=cache(49152, 12, 16), Icache=cache(32768, 8))],
            _l2_controllers=[NS(cache=cache(2097152, 16, 48))],
            _l3_controllers=[NS(L2cache=cache(12582912, 12), number_of_TBEs=256) for _ in range(8)])
        cpu = NS(
            fetchWidth=8, decodeWidth=8, renameWidth=6, dispatchWidth=6,
            issueWidth=12, wbWidth=12, commitWidth=8, numROBEntries=512,
            LQEntries=240, SQEntries=112,
            instQueues=[NS(numEntries=n) for n in (128, 72, 48)],
            mmu=NS(dtb=NS(size=96, dtlb_4k_ways=6),
                   itb=NS(size=256, itlb_4k_ways=8), stlb=NS(size=2048)))
        dram = NS(type="DDR5_4400_4x8", banks_per_rank=32, ranks_per_channel=1,
                  device_rowbuffer_size=256, devices_per_rank=4, read_buffer_size=64)
        memory = NS(get_memory_controllers=lambda: [NS(dram=dram) for _ in range(16)],
                    get_size=lambda: 2147483648)
        profile = self.policy.native_profile(cpu, hierarchy, memory, 4)
        self.assertEqual(profile["cache"]["l3"]["size_b"], 100663296)
        self.assertEqual(profile["cache"]["l3"]["num_banks"], 8)
        self.assertEqual(profile["native_cpu"]["iq_entries"], [128, 72, 48])
        self.assertEqual(profile["tlb"]["dtlb"], dict(entries=96, assoc=6))
        self.assertEqual(profile["dram"]["num_channels"], 16)
        self.assertEqual(profile["dram"]["interleaving_size_b"], 64)
        # A live-object change must propagate; a copied generic constant fails.
        hierarchy._l1_controllers[0].Dcache.fill_capacity = 7
        self.assertEqual(self.policy.native_profile(cpu, hierarchy, memory, 4)["mshr"]["l1d_entries"], 7)

    def test_invalid_window_and_trace_paths_are_rejected(self):
        for extra in (("--functional-user-target", "0"), ("--participant-cores", "0"),
                      ("--trace-subdir", "../outside"), ("--warmup-insts", "-1")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.policy.validate_options(self.args(*extra))

    def test_kvm_switch_collapses_queues_after_switching_to_native_o3(self):
        # gem5's compiled module is unavailable in a host Python test. Execute
        # the actual entry class against its stateful runtime boundaries.
        entry = POLICY.with_name("fs_capture.py")
        tree = ast.parse(entry.read_text())
        capture = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef) and n.name == "CaptureProcessor")

        class FrameworkProcessor:
            active = "KVM"

            def switch(self):
                self.active = "O3"

        namespace = {"SimpleSwitchableProcessor": FrameworkProcessor}
        exec(compile(ast.Module(body=[capture], type_ignores=[]), str(entry), "exec"), namespace)
        processor = namespace["CaptureProcessor"]()
        root = NS(sim_quantum=1000000000)
        event_state = NS(single_queue=False)

        def collapse():
            if processor.active != "O3":
                raise AssertionError("event queues collapsed before KVM handoff")
            event_state.single_queue = True

        event_module = ModuleType("_m5.event")
        event_module.enterSingleEventQueueMode = collapse
        objects_module = ModuleType("m5.objects")
        objects_module.Root = NS(getInstance=lambda: root)
        with patch.dict(sys.modules, {"_m5.event": event_module, "m5.objects": objects_module}):
            processor.switch()
        self.assertEqual(processor.active, "O3")
        self.assertTrue(event_state.single_queue)
        self.assertEqual(root.sim_quantum, 0)

    def test_workbegin_uses_actual_producer_state_including_raw_workid_zero(self):
        entry = POLICY.with_name("fs_capture.py")
        tree = ast.parse(entry.read_text())
        callback = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "on_workbegin")
        state = {"switched": True, "measurement_open": False}
        event = NS(code=0)
        producers = [NS(started=False) for _ in range(4)]
        resets, phases = [], []
        namespace = dict(
            args=NS(warmup_insts=0), state=state,
            traced=[NS(tao_trace=NS(functionalMeasurementStarted=lambda p=p: p.started))
                    for p in producers],
            simulator=NS(get_last_exit_event_code=lambda: event.code),
            m5=NS(stats=NS(reset=lambda: resets.append(event.code))),
            milestone=phases.append)
        exec(compile(ast.Module(body=[callback], type_ignores=[]), str(entry), "exec"), namespace)
        handler = namespace["on_workbegin"]
        with contextlib.redirect_stdout(io.StringIO()):
            for code in (0, 1):
                event.code = code
                self.assertIs(handler(), False)
                self.assertFalse(state["measurement_open"])
                self.assertEqual(resets, [])
            self.assertEqual(phases, ["raw-work-item-notification"] * 2)
            for producer in producers:
                producer.started = True
            event.code = 0
            self.assertIs(handler(), False)
            self.assertTrue(state["measurement_open"])
            self.assertEqual(resets, [0])
            self.assertEqual(phases[-1], "measurement-open")
            self.assertIs(handler(), False)
            self.assertEqual(phases[-1], "work-item-notification-after-measurement")
            self.assertEqual(resets, [0])
            producers[0].started = False
            with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                handler()
            self.assertEqual(resets, [0])


if __name__ == "__main__":
    unittest.main()
