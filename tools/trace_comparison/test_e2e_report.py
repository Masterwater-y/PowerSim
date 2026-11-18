import copy
import unittest
from report_e2e import validate_timing, summarize_rates


class E2EReportTest(unittest.TestCase):
    def timing(self):
        return dict(
            policy="preparation-included-extra-validation-excluded-v1",
            trusted_input=True,
            preprocessing_included=True,
            warmup_included=True,
            validation_receipt_sha256="a" * 64,
            start_ns=10**9,
            end_ns=5 * 10**9,
            e2e_seconds=4,
            process_wall_seconds=5,
        )

    def test_valid_primary_interval(self):
        validate_timing(self.timing(), "a" * 64)

    def test_missing_preparation_rejected(self):
        t = self.timing()
        t["preprocessing_included"] = False
        with self.assertRaises(ValueError):
            validate_timing(t, "a" * 64)

    def test_wrong_receipt_rejected(self):
        with self.assertRaises(ValueError):
            validate_timing(self.timing(), "b" * 64)

    def test_unbound_duration_rejected(self):
        t = self.timing()
        t["e2e_seconds"] = 1
        with self.assertRaises(ValueError):
            validate_timing(t, "a" * 64)

    def test_nonpositive_or_nonfinite_time_rejected(self):
        for v in [0, -1, float("nan"), float("inf")]:
            with self.subTest(v=v):
                t = self.timing()
                t["e2e_seconds"] = v
                with self.assertRaises(ValueError):
                    validate_timing(t, "a" * 64)

    def test_model_end_after_process_rejected(self):
        t = self.timing()
        t["process_wall_seconds"] = 3
        with self.assertRaises(ValueError):
            validate_timing(t, "a" * 64)

    def test_batch_rate_uses_total_work_over_total_time(self):
        rows = [
            dict(user_uops=100_000_000, e2e_seconds=1, e2e_user_mups=100),
            dict(user_uops=1_000_000, e2e_seconds=100, e2e_user_mups=0.01),
        ]
        result = summarize_rates(rows)
        self.assertEqual(result["batch_e2e_user_mups"], 1)
        self.assertEqual(result["median_e2e_user_mups"], 50.005)
        self.assertEqual(result["total_e2e_seconds"], 101)

    def test_empty_batch_rejected(self):
        with self.assertRaises(ValueError):
            summarize_rates([])


if __name__ == "__main__":
    unittest.main()
