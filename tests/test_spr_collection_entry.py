"""Directed entry tests without importing the gem5 runtime."""
import importlib.util
import ast
import contextlib
import io
import re
from pathlib import Path
from types import SimpleNamespace as NS
from types import ModuleType
import sys
import unittest
import subprocess
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GEM5_ROOT = ROOT.parent / "gem5-deploy"
POLICY = GEM5_ROOT / "configs/fastsim/collection.py"


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

    def test_staged_checkpoint_requires_supported_launcher_protocol(self):
        with self.assertRaisesRegex(ValueError, "rate launcher"):
            self.policy.validate_options(self.args("--save-staged-checkpoint", "/example/cpt"))
        with self.assertRaisesRegex(ValueError, "requires --save"):
            self.policy.validate_options(self.args("--checkpoint-only"))
        args=self.args("--rate-copies", "1", "--save-staged-checkpoint", "/example/cpt",
                       "--checkpoint-only")
        self.policy.validate_options(args)
        self.assertEqual(args.save_boot_checkpoint, Path("/example/cpt"))
        restored=self.args("--rate-copies", "1", "--restore-staged-checkpoint", "/example/cpt")
        self.policy.validate_options(restored)
        self.assertEqual(restored.restore_boot_checkpoint, Path("/example/cpt"))

    def test_source_companions_enabled_without_claiming_task_ownership(self):
        args = self.args()
        options = self.policy.trace_options(args, "/capture", "/profile", {})
        self.assertEqual(options["trace_format"], "fst")
        for flag in ("functional_include_kernel", "functional_syscall_microops",
                     "functional_frontend", "functional_retirement", "functional_warmup",
                     "functional_asid_static_map"):
            self.assertIs(options[flag], True, flag)
        self.assertIs(options["require_roi"], False)
        self.assertEqual(options["functional_sync_marker_pc"], 0)
        self.assertNotIn("scheduler_profile_id", options)
        self.assertNotIn("fixed_work_source_core", options)

    def test_mmu_controls_observer_is_explicit_and_default_off(self):
        ordinary=self.policy.trace_options(self.args(),'/capture','/profile',{})
        enabled=self.policy.trace_options(self.args('--functional-mmu-controls'),'/capture','/profile',{})
        self.assertIs(ordinary['functional_mmu_controls'],False)
        self.assertIs(enabled['functional_mmu_controls'],True)
        self.assertEqual({k:v for k,v in ordinary.items() if k!='functional_mmu_controls'},
                         {k:v for k,v in enabled.items() if k!='functional_mmu_controls'})

    def test_instruction_translation_observer_is_explicit_and_default_off(self):
        ordinary = self.policy.trace_options(self.args(), "/capture", "/profile", {})
        enabled = self.policy.trace_options(self.args("--functional-instruction-translations"), "/capture", "/profile", {})
        self.assertIs(ordinary["functional_instruction_translations"], False)
        self.assertIs(enabled["functional_instruction_translations"], True)
        self.assertEqual({key: value for key, value in ordinary.items() if key != "functional_instruction_translations"},
                         {key: value for key, value in enabled.items() if key != "functional_instruction_translations"})

    def test_data_translation_origins_are_explicit_and_default_off(self):
        ordinary = self.policy.trace_options(self.args(), "/capture", "/profile", {})
        enabled = self.policy.trace_options(self.args("--functional-translation-origins"), "/capture", "/profile", {})
        self.assertIs(ordinary["functional_translation_origins"], False)
        self.assertIs(enabled["functional_translation_origins"], True)
        self.assertEqual({key: value for key, value in ordinary.items() if key != "functional_translation_origins"},
                         {key: value for key, value in enabled.items() if key != "functional_translation_origins"})
        source = (GEM5_ROOT / "src/cpu/o3/probe/tao_trace.cc").read_text()
        start = source.index("    if (functional_translation_origins_ &&")
        # This guard owns one unbraced throw statement. Later observer guards
        # have independent permissions and are not part of the data flag.
        end = source.index(";", start) + 1
        block = source[start:end]
        self.assertRegex(block, r'throw std::runtime_error\("[^"]*"\);\Z')
        self.assertIn("!FullSystem", block)
        self.assertIn("!functional_frontend_", block)
        self.assertIn("!functional_retirement_", block)
        self.assertIn("!functional_asid_static_map_", block)
        self.assertNotIn("task_clock_cpu_->", block)

    def test_core_functional_observer_is_default_off_and_startup_only_sets_observer(self):
        ordinary = self.policy.trace_options(self.args(), "/capture", "/profile", {})
        enabled = self.policy.trace_options(self.args("--functional-core-facts"), "/capture", "/profile", {})
        self.assertIs(ordinary["functional_core_facts"], False)
        self.assertIs(enabled["functional_core_facts"], True)
        self.assertEqual({key: value for key, value in ordinary.items() if key != "functional_core_facts"},
                         {key: value for key, value in enabled.items() if key != "functional_core_facts"})
        native = GEM5_ROOT / "src/cpu/o3"
        self.assertIn("bool coreFunctionalObservation = false;", (native / "cpu.hh").read_text())
        self.assertRegex((native / "probe/TaoTrace.py").read_text(),
                         r"functional_core_facts\s*=\s*Param.Bool\(False,")
        source = (native / "probe/tao_trace.cc").read_text()
        start = source.index("    if (functional_core_facts_) {")
        brace = source.index("{", start)
        depth, end = 1, brace + 1
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        block = re.sub(r"//[^\n]*", "", source[start:end])
        # Check the whole startup statement: guards may reject an unsupported
        # source, and the sole mutation enables pure snapshot observation.
        self.assertEqual(" ".join(block.split()), " ".join('''
            if (functional_core_facts_) {
                if (!FullSystem || !task_clock_cpu_ || !functional_frontend_ ||
                    !functional_retirement_ || !functional_asid_static_map_ || functional_user_only_)
                    throw std::runtime_error("core functional facts require complete FS O3 retired frontend source mode");
                task_clock_cpu_->coreFunctionalObservation = true;
            }
        '''.split()))

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

    def test_core_profile_resolves_every_scalar_without_parameter_writes(self):
        class ReadOnly:
            def __init__(self, **values):
                object.__setattr__(self, "values", values)

            def __getattr__(self, name):
                if name not in self.values:
                    raise AttributeError(name)
                return self.values[name]

            def __setattr__(self, name, value):
                raise AssertionError("profile mutated parameter " + name)

        class Proxy:
            def __init__(self, owner, value):
                self.owner, self.value = owner, value

            def unproxy(self, owner):
                self_test.assertIs(owner, self.owner)
                return self.value

        self_test = self
        cpu = ReadOnly()
        frontend = ReadOnly()
        cpu.values["frontend"] = frontend
        cpu_fields = ("numThreads", "numPhysIntRegs", "numPhysFloatRegs", "numPhysVecRegs",
                      "numPhysVecPredRegs", "numPhysMatRegs", "numPhysCCRegs")
        groups = {"instructionBytes": ("bufferBytes", "bytesPerCycle"), "idq": ("capacity",),
                  "mite": ("instructionsPerBatch", "opsPerCycle", "batchOps", "latency"),
                  "dsb": ("instructionsPerBatch", "opsPerCycle", "batchOps", "latency",
                          "numSets", "associativity", "opsPerEntry"),
                  "ms": ("instructionsPerBatch", "opsPerCycle", "batchOps", "latency")}
        originals = []

        def add(owner, name, value):
            proxy = Proxy(owner, value)
            owner.values[name] = proxy
            originals.append((owner, name, proxy))

        for i, name in enumerate(cpu_fields):
            add(cpu, name, 10 + i)
        for group, names in groups.items():
            obj = ReadOnly()
            frontend.values[group] = obj
            for i, name in enumerate(names):
                add(obj, name, 30 + i)
        add(frontend, "dsbToMiteDelay", 9)
        out = self.policy.native_core_profile(cpu)
        self.assertEqual(out["numThreads"], 10)
        self.assertEqual(out["physical_registers"],
                         dict(Int=11, Float=12, Vec=13, VecPred=14, Mat=15, CC=16))
        for group, names in groups.items():
            self.assertEqual(out["frontend"][group], {name: 30 + i for i, name in enumerate(names)})
        self.assertEqual(out["frontend"]["dsbToMiteDelay"], 9)
        for owner, name, proxy in originals:
            self.assertIs(getattr(owner, name), proxy)

    def test_core_profile_missing_and_unresolvable_parameters_are_distinct(self):
        out = self.policy.native_core_profile(NS(frontend=NS(idq=NS(capacity=17))))
        self.assertTrue(all(value is None for value in out["physical_registers"].values()))
        self.assertIsNone(out["numThreads"])
        self.assertEqual(out["frontend"]["idq"]["capacity"], 17)
        self.assertIsNone(out["frontend"]["instructionBytes"]["bufferBytes"])
        self.assertIsNone(out["frontend"]["dsbToMiteDelay"])

        class InvalidProxy:
            def unproxy(self, owner):
                raise AttributeError("unresolved fixture target")

        for cpu, parameter in ((NS(numPhysCCRegs=InvalidProxy()), "cpu.numPhysCCRegs"),
                               (NS(frontend=NS(idq=NS(capacity=InvalidProxy()))), "frontend.idq.capacity")):
            with self.subTest(parameter=parameter), self.assertRaisesRegex(ValueError, parameter):
                self.policy.native_core_profile(cpu)

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

    def test_large_memory_requires_explicit_split_range_opt_in(self):
        args = self.args("--mem-size", "32GiB")
        with self.assertRaisesRegex(ValueError, "x86-large-memory"):
            self.policy.validate_options(args)
        args = self.args("--mem-size", "32GiB", "--x86-large-memory")
        self.policy.validate_options(args)
        self.assertEqual(self.policy.memory_size_bytes(args.mem_size), 34359738368)
        self.assertEqual(args.mem_channels, 16)
        self.assertEqual(self.args().mem_size, "2GiB")

    def test_native_tasks_require_clock_and_identity_warmup(self):
        options = ["--rate-copies", "1", "--native-workers", "5",
                   "--rate-launcher", "/launcher", "--warmup-insts", "500000"]
        with self.assertRaisesRegex(ValueError, "task-clock"):
            self.policy.validate_options(self.args(*options))
        options += ["--thread-state", "--task-clock", "--scheduler-profile", "/profile"]
        with self.assertRaisesRegex(ValueError, "native-warmup-after-identities"):
            self.policy.validate_options(self.args(*options))
        options += ["--native-warmup-after-identities"]
        self.policy.validate_options(self.args(*options))

    def test_launcher_rejects_unavailable_sync_protocol(self):
        # Native business binaries have no synchronization marker. Preserve
        # the donor's rejection for native and ordinary rate-copy launches.
        tree = ast.parse((ROOT / "scripts/run_gem5_sync_probe.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "validate_rate_options")
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "donor", "exec"), namespace)
        donor = NS(validate_rate_options=namespace["validate_rate_options"])
        args = self.args("--rate-copies", "1", "--native-workers", "4",
            "--rate-launcher", "/launcher", "--warmup-insts", "500000",
            "--thread-state", "--task-clock", "--scheduler-profile", "/profile",
            "--native-warmup-after-identities", "--functional-sync")
        with self.assertRaisesRegex(ValueError, "synchronization protocol"):
            donor.validate_rate_options(args)
        args.native_workers = 0
        args.rate_copies = 4
        args.native_warmup_after_identities = False
        with self.assertRaisesRegex(ValueError, "synchronization protocol"):
            donor.validate_rate_options(args)

    def test_native_identity_handoff_arms_warmup_before_measurement(self):
        entry = POLICY.with_name("fs_capture.py")
        tree = ast.parse(entry.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "on_checkpoint")
        sys.path.insert(0, str(ROOT / "tools"))
        from roi_capture_policy import BoundedWarmup
        from spec_rate_checkpoint import RateCheckpointFlow
        stops, phases = [], []
        state = dict(switched=False, common_end=False, measurement_open=False)
        namespace = dict(args=NS(rate_copies=1, native_warmup_after_identities=True,
            warmup_insts=500000, save_boot_checkpoint=None),
            checkpoint_flow=RateCheckpointFlow("staged", False),
            state=state, milestone=phases.append,
            simulator=NS(switch_processor=lambda: None),
            warmup=BoundedWarmup(500000, defer_until_identities=True),
            traced=[NS(scheduleInstStopAnyThread=stops.append)])
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(entry), "exec"), namespace)
        callback = namespace["on_checkpoint"]
        callback()  # launcher staged checkpoint before fork
        self.assertEqual(stops, [])
        callback()  # actual compute-ready KVM -> O3 handoff
        self.assertEqual(stops, [])
        callback()  # every native worker identity committed under O3
        self.assertEqual(stops, [500000])
        self.assertEqual(phases[-2:], ["native-identities-ready", "warmup-armed"])

    def test_large_memory_guest_report_checks_visibility_before_staging(self):
        args = self.args("--mem-size", "32GiB", "--x86-large-memory")
        script = self.policy.guest_memory_boot_script(args, "#!/bin/sh\nset -e\necho staged\n")
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertEqual(script.count("#!/bin/sh"), 1)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            directory = Path(tmp)
            mem = directory / "meminfo"
            iomem = directory / "iomem"
            (directory / "swapoff").write_text("#!/bin/sh\nexit 0\n")
            (directory / "swapoff").chmod(0o755)
            # Fixture only replaces Linux procfs paths; execute real sh/awk.
            command = script.replace("/proc/meminfo", str(mem)).replace("/proc/iomem", str(iomem))
            env = dict(__import__('os').environ, PATH=str(directory) + ":/usr/bin:/bin")
            mem.write_text("MemTotal: 33000000 kB\nSwapTotal: 0 kB\n")
            iomem.write_text("00000000-bfffffff : System RAM\n100000000-83fffffff : System RAM\n")
            passed = subprocess.run(["sh"], input=command, text=True, capture_output=True, env=env)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertLess(passed.stdout.index("visibility-passed"), passed.stdout.index("staged"))
            for total, ranges in (("32000000", "100000000-83fffffff : System RAM\n"),
                                  ("33000000", "00000000-bfffffff : System RAM\n")):
                mem.write_text("MemTotal: " + total + " kB\n")
                iomem.write_text(ranges)
                failed = subprocess.run(["sh"], input=command, text=True, capture_output=True, env=env)
                self.assertNotEqual(failed.returncode, 0)
                self.assertNotIn("staged", failed.stdout)
        original = "#!/bin/sh\necho staged\n"
        self.assertEqual(self.policy.guest_memory_boot_script(self.args(), original), original)

    def test_kvm_switch_collapses_queues_after_switching_to_native_o3(self):
        # gem5's compiled module is unavailable in a host Python test. Execute
        # the actual entry class against its stateful runtime boundaries.
        entry = POLICY.with_name("fs_capture.py")
        tree = ast.parse(entry.read_text())
        capture = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef) and n.name == "CaptureProcessor")

        for enabled in (False, True):
            with self.subTest(boundaries=enabled):
                events=[]
                root=NS(sim_quantum=1000000000)
                event_state=NS(single_queue=False)
                class FrameworkProcessor:
                    active="KVM"
                    def switch(self):
                        events.append("switch")
                        self.active="O3"
                def arm(index):
                    self.assertEqual(processor.active,"O3")
                    self.assertTrue(event_state.single_queue)
                    self.assertEqual(root.sim_quantum,0)
                    self.assertEqual(events[:2],["switch","collapse"])
                    events.append("arm"+str(index))
                namespace={"SimpleSwitchableProcessor":FrameworkProcessor,
                           "args":NS(functional_architectural_boundaries=enabled),
                           "traced":[NS(tao_trace=NS(armArchitecturalBoundaryEpoch=lambda i=i:arm(i))) for i in range(2)]}
                exec(compile(ast.Module(body=[capture],type_ignores=[]),str(entry),"exec"),namespace)
                processor=namespace["CaptureProcessor"]()
                def collapse():
                    self.assertEqual(processor.active,"O3")
                    self.assertEqual(events,["switch"])
                    self.assertEqual(root.sim_quantum,1000000000)
                    events.append("collapse")
                    event_state.single_queue=True
                event_module=ModuleType("_m5.event")
                event_module.enterSingleEventQueueMode=collapse
                objects_module=ModuleType("m5.objects")
                objects_module.Root=NS(getInstance=lambda:root)
                with patch.dict(sys.modules,{"_m5.event":event_module,"m5.objects":objects_module}):
                    processor.switch()
                self.assertEqual(events,["switch","collapse"]+(["arm0","arm1"] if enabled else []))
                self.assertEqual(processor.active,"O3")
                self.assertTrue(event_state.single_queue)
                self.assertEqual(root.sim_quantum,0)

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
