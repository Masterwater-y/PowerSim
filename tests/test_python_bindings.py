#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

import fastsim_py


class FastSimPythonBindingTest(unittest.TestCase):
    def test_manifest_backed_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "profile.cfg"
            trace = root / "core0.jsonl"
            manifest = root / "manifest.txt"

            config.write_text(
                "\n".join(
                    [
                        "measurement.scope = user",
                        "sim.cores = 1",
                        "sim.chunk_instructions = 64",
                        "sim.lookahead_chunks = 2",
                        "sim.interval_target_uops = 64",
                        "sim.interval_max_cycles = 32",
                        "sim.interval_scheduler = time_epoch",
                        "sim.interval_reweave_passes = 1",
                        "core.model = interval_weave",
                    ]
                )
                + "\n"
            )
            trace.write_text(
                "\n".join(
                    '{{"pc":{},"op_class":1}}'.format(
                        0x1000 + index * 4
                    )
                    for index in range(10_000)
                )
                + "\n"
            )
            manifest.write_text("0 gem5-jsonl core0.jsonl\n")

            simulator = fastsim_py.DvfsSession(
                str(config),
                str(manifest),
                measurement_scope="user",
                reference_frequency_hz=3_000_000_000,
                initial_core_frequencies_hz=[4_500_000_000],
            )
            result = simulator.advance_time_ns(100)
            self.assertEqual(result.cores[0].frequency_hz, 4_500_000_000)
            self.assertEqual(result.cores[0].cycles, 450)
            self.assertGreater(result.cores[0].retired_instructions, 0)

    def test_windowed_dvfs_session(self) -> None:
        simulator = fastsim_py.DvfsSession.synthetic(
            cores=2,
            instructions_per_core=10_000,
            memory_percent=0,
            shared_percent=0,
            working_set_lines=1024,
            seed=91,
            reference_frequency_hz=3_000_000_000,
            initial_core_frequencies_hz=[
                4_500_000_000,
                1_500_000_000,
            ],
        )

        self.assertEqual(simulator.core_count, 2)
        self.assertEqual(simulator.measurement_scope, "user")
        self.assertEqual(simulator.reference_frequency_hz, 3_000_000_000)
        self.assertEqual(
            simulator.initial_core_frequencies_hz,
            [4_500_000_000, 1_500_000_000],
        )

        first = simulator.advance_time_ns(100)
        self.assertEqual(first.window_id, 1)
        self.assertEqual(first.start_time_fs, 0)
        self.assertEqual(first.end_time_fs, 100_000_000)
        self.assertFalse(first.finished)
        self.assertEqual([core.cycles for core in first.cores], [450, 150])
        self.assertTrue(all(core.cpi_available for core in first.cores))
        self.assertTrue(all(core.cpi is not None for core in first.cores))
        first_dict = first.to_dict()
        self.assertEqual(
            first_dict["schema"], fastsim_py.WINDOW_RESULT_SCHEMA
        )
        self.assertEqual(first_dict["cores"][0]["cycles"], 450)

        with self.assertRaises(ValueError):
            simulator.set_core_frequencies([3_000_000_000])

        simulator.set_core_frequencies(
            [1_500_000_000, 4_500_000_000]
        )
        self.assertEqual(
            simulator.current_core_frequencies_hz,
            [1_500_000_000, 4_500_000_000],
        )
        second = simulator.advance(
            fastsim_py.SimulationWindow.simulated_time_ns(100)
        )
        self.assertEqual(second.window_id, 2)
        self.assertEqual(second.start_time_fs, first.end_time_fs)
        self.assertEqual(second.end_time_fs, 200_000_000)
        self.assertEqual([core.cycles for core in second.cores], [150, 450])

        instruction = simulator.advance_instructions(1000)
        self.assertGreaterEqual(instruction.retired_instructions, 1000)
        self.assertEqual(
            instruction.instruction_overshoot,
            instruction.retired_instructions - 1000,
        )

        windows = 0
        while not simulator.finished():
            result = simulator.advance_instructions(5000)
            windows += 1
            self.assertLess(windows, 100)
        self.assertTrue(result.finished)


if __name__ == "__main__":
    unittest.main()
