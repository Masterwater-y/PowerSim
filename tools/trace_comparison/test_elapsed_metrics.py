import unittest
from elapsed_metrics import compare_elapsed


class ElapsedMetricsTest(unittest.TestCase):
    def arguments(self):
        return dict(
            reference_seconds=2,
            reference_hz=100,
            model_seconds=3,
            participants=2,
            macros=[10, 30],
            uops=[20, 50],
            expected_macros=[10, 30],
            expected_uops=[20, 50],
        )

    def test_reference_clock_and_makespan(self):
        d = compare_elapsed(**self.arguments())
        self.assertEqual(d["relative_error"], 0.5)
        self.assertEqual(d["absolute_error_cycles_per_macro"], 5)
        self.assertFalse(d["active_cpi"])

    def test_equal_totals_do_not_hide_per_core_mismatch(self):
        a = self.arguments()
        a["macros"] = [20, 20]
        with self.assertRaises(ValueError):
            compare_elapsed(**a)

    def test_missing_participant(self):
        a = self.arguments()
        a["participants"] = 3
        with self.assertRaises(ValueError):
            compare_elapsed(**a)


if __name__ == "__main__":
    unittest.main()
