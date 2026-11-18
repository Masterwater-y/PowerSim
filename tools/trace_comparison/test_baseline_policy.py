import unittest
from baseline_policy import require_parallel_zsim


class BaselinePolicyTest(unittest.TestCase):
    def test_parallel_is_primary(self):
        self.assertEqual(
            require_parallel_zsim({"cores": 8, "preparation_workers": 8}),
            "parallel-files",
        )

    def test_serial_not_silently_primary(self):
        for data in [{"cores": 4}, {"cores": 4, "preparation_workers": 1}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                require_parallel_zsim(data)

    def test_serial_requires_explicit_history(self):
        self.assertEqual(
            require_parallel_zsim({"cores": 4}, historical=True), "historical-serial"
        )

    def test_single_file_can_only_have_one_worker(self):
        self.assertEqual(
            require_parallel_zsim({"cores": 1, "preparation_workers": 1}),
            "parallel-files",
        )


if __name__ == "__main__":
    unittest.main()
