import pathlib
import tempfile
import unittest

from e2e_timing import elapsed_seconds, parse_endpoints

ROOT = pathlib.Path(__file__).resolve().parents[2]


class E2ETimingTest(unittest.TestCase):
    def test_preparation_and_simulation_are_both_included(self):
        # Launch=1s, preparation finishes=4s, simulation/drain ends=6s.
        self.assertEqual(elapsed_seconds(10**9, {0: 6 * 10**9}, 7 * 10**9, 1), 5)

    def test_missing_participant_rejected(self):
        with self.assertRaises(ValueError):
            elapsed_seconds(1, {0: 3}, 5, 2)

    def test_invalid_clock_order_rejected(self):
        for end in [0, 1, 6]:
            with self.subTest(end=end), self.assertRaises(ValueError):
                elapsed_seconds(1, {0: end}, 5, 1)

    def test_native_marker(self):
        self.assertEqual(
            parse_endpoints("[TRACEBENCH-END] host_monotonic_ns=123\n", "zsim", 4),
            {0: 123},
        )

    def test_sniper_uses_last_completed_core(self):
        text = "\n".join(
            f"[FST-ENDPOINT:{c}] complete_macros=10 model_elapsed_fs=100 stop=0 host_monotonic_ns={v}"
            for c, v in [(0, 200), (1, 300)]
        )
        self.assertEqual(
            elapsed_seconds(100, parse_endpoints(text, "sniper", 2), 400, 2), 2e-7
        )

    def test_stopped_sniper_rejected(self):
        with self.assertRaises(ValueError):
            parse_endpoints(
                "[FST-ENDPOINT:0] complete_macros=10 model_elapsed_fs=100 stop=1 host_monotonic_ns=200",
                "sniper",
                1,
            )

    def test_duplicate_marker_rejected(self):
        with self.assertRaises(ValueError):
            parse_endpoints(
                "[TRACEBENCH-END] host_monotonic_ns=123\n[TRACEBENCH-END] host_monotonic_ns=124",
                "fastsim",
                1,
            )


if __name__ == "__main__":
    unittest.main()
