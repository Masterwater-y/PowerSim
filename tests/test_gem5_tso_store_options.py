"""Validate the gem5 SE runner's TSO store option contract."""
import argparse
import ast
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/gem5/run_uarch_stats_se.py"


def load_runner_helpers():
    source = RUNNER.read_text()
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"effective_sq_entries", "configure_o3_cores"}
    ]
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(module, str(RUNNER), "exec"), namespace)
    return namespace


def load_fs_roi_runner():
    runner = ROOT.parent / "TCSim/scripts/gem5_fs_roi.py"
    if not runner.is_file():
        return None
    spec = importlib.util.spec_from_file_location("test_gem5_fs_roi", runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCore:
    def __init__(self):
        self.fetchWidth = None
        self.decodeWidth = None
        self.renameWidth = None
        self.dispatchWidth = None
        self.issueWidth = None
        self.wbWidth = None
        self.commitWidth = None
        self.numROBEntries = None
        self.numPhysIntRegs = None
        self.numPhysFloatRegs = None
        self.numPhysVecRegs = None
        self.numPhysCCRegs = None
        self.instQueues = []
        self.LQEntries = None
        self.SQEntries = None
        self.tsoStore = None
        self.tsoStoreBufferSize = None
        self.mmu = type("MMU", (), {"dtb": type("DTB", (), {"size": None})()})()


class WrappedCore:
    def __init__(self, core=None):
        self.core = core or FakeCore()

    def get_simobject(self):
        return self.core


def options(mode="serial", buffer_size=114, sq_entries=32):
    return argparse.Namespace(
        tso_store=mode,
        tso_store_buffer_size=buffer_size,
        sq_entries=sq_entries,
        fetch_width=8,
        decode_width=8,
        rename_width=8,
        dispatch_width=8,
        issue_width=8,
        wb_width=8,
        commit_width=8,
        rob_entries=192,
        phys_int_regs=256,
        phys_float_regs=256,
        phys_vec_regs=256,
        phys_cc_regs=1280,
        branch_predictor="tournament",
        iq_entries=64,
        lq_entries=32,
        dtlb_entries=64,
    )


class TSOStoreOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_runner_helpers()

    def configure(self, args):
        wrapped = WrappedCore()
        processor = type(
            "Processor", (), {"_switchable_cores": {"switch": [wrapped]}}
        )()
        self.helpers["configure_o3_cores"](processor, args)
        return wrapped.core

    def configure_legacy(self, args):
        core = FakeCore()
        del core.tsoStore
        del core.tsoStoreBufferSize
        wrapped = WrappedCore(core)
        processor = type(
            "Processor", (), {"_switchable_cores": {"switch": [wrapped]}}
        )()
        self.helpers["configure_o3_cores"](processor, args)
        return core

    def test_serial_ignores_buffer_size(self):
        first = self.configure(options("serial", 114, 32))
        second = self.configure(options("serial", 7, 32))
        self.assertEqual(first.SQEntries, 32)
        self.assertEqual(second.SQEntries, 32)
        self.assertEqual(first.tsoStore, "serial")
        self.assertEqual(second.tsoStore, "serial")

    def test_buffer_uses_configured_size(self):
        core = self.configure(options("buffer", 114, 32))
        self.assertEqual(core.SQEntries, 114)
        self.assertEqual(core.tsoStore, "buffer")
        self.assertEqual(core.tsoStoreBufferSize, 114)

    def test_nonpositive_buffer_size_is_rejected_in_both_modes(self):
        for mode in ("serial", "buffer"):
            with self.assertRaisesRegex(ValueError, "must be positive"):
                self.configure(options(mode, 0, 32))

    def test_legacy_binary_supports_serial_and_rejects_buffer(self):
        core = self.configure_legacy(options("serial", 7, 32))
        self.assertEqual(core.SQEntries, 32)
        with self.assertRaisesRegex(ValueError, "requires a gem5 binary"):
            self.configure_legacy(options("buffer", 114, 32))


class TSOStoreFSDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_fs_roi_runner()

    def setUp(self):
        if self.runner is None:
            self.skipTest("sibling TCSim FS driver is not installed")

    def test_serial_environment_does_not_require_sidecar_inputs(self):
        with patch.dict(os.environ, {}, clear=True):
            environment = self.runner.gem5_environment()
        self.assertNotIn("FASTSIM_EFFECTIVE_TARGET_GENERATOR", environment)
        self.assertNotIn("FASTSIM_EVENT_DICTIONARY", environment)

    def test_tao_environment_discovers_canonical_sidecar_inputs(self):
        with patch.dict(os.environ, {}, clear=True):
            environment = self.runner.gem5_environment(
                require_effective_target=True
            )
        self.assertEqual(
            Path(environment["FASTSIM_EFFECTIVE_TARGET_GENERATOR"]),
            ROOT / "tools/generate_fs_effective_target.py",
        )
        self.assertEqual(
            Path(environment["FASTSIM_EVENT_DICTIONARY"]),
            ROOT / "configs/pmu-event-dictionary-v1.json",
        )

    def test_tao_environment_preserves_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generator = root / "generator.py"
            dictionary = root / "dictionary.json"
            generator.touch()
            dictionary.touch()
            overrides = {
                "FASTSIM_EFFECTIVE_TARGET_GENERATOR": str(generator),
                "FASTSIM_EFFECTIVE_TARGET_PYTHON": "/custom/python",
                "FASTSIM_EVENT_DICTIONARY": str(dictionary),
            }
            with patch.dict(os.environ, overrides, clear=True):
                environment = self.runner.gem5_environment(
                    require_effective_target=True
                )
            for name, value in overrides.items():
                self.assertEqual(environment[name], value)

    def test_no_record_capture_preserves_final_config_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            result = root / "result"
            scratch.mkdir()
            profile = scratch / "uarch_profile.json"
            profile.write_text('{"source": "final-config"}\n')
            target = self.runner.preserve_uarch_profile(
                scratch, result, keep_scratch=False
            )
            self.assertEqual(
                target, result / "tao_trace/uarch_profile.json"
            )
            self.assertEqual(
                target.read_text(), '{"source": "final-config"}\n'
            )
            self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()
