"""Measurement-window gates: the slow cores need not reach the target."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from common_end_capture import validate_common_end


class CommonEndTests(unittest.TestCase):
    def fixture(self):
        boundaries = []
        cpi = []
        classes = []
        for core, count in enumerate((10000, 3700)):
            boundaries.append(dict(
                core_id=core, measurement_policy="first-core-target-common-end-v1",
                target_records=10000, target_reached=core == 0,
                measurement_started=True, measurement_closed=True,
                common_end_tick=100000, trigger_core=0, participant_count=2,
                stop_reason="first-core-user-target", measurement_user_records=count,
                warmup_records=100, measurement_records=count + 20,
                total_records=count + 120,
            ))
            cpi.append(dict(core_id=core, n_user=count))
            classes.append(dict(core_id=core, last_tick=100000))
        return boundaries, cpi, classes

    def test_unequal_progress_uses_actual_population(self):
        result = validate_common_end(*self.fixture(), cores=2, target=10000)
        self.assertEqual(result["user_uops"], 13700)
        self.assertEqual(result["trigger_core"], 0)

    def test_rejects_independent_metric_end(self):
        rows, cpi, classes = self.fixture()
        classes[1]["last_tick"] += 1
        with self.assertRaisesRegex(ValueError, "CPL.*end"):
            validate_common_end(rows, cpi, classes, cores=2, target=10000)

    def test_rejects_fixed_denominator_for_slow_core(self):
        rows, cpi, classes = self.fixture()
        cpi[1]["n_user"] = 10000
        with self.assertRaisesRegex(ValueError, "population"):
            validate_common_end(rows, cpi, classes, cores=2, target=10000)

    def test_rejects_legacy_and_unreached_trigger(self):
        for field, value in (("measurement_policy", None),
                             ("measurement_closed", False),
                             ("trigger_core", 1)):
            with self.subTest(field=field):
                rows, cpi, classes = copy.deepcopy(self.fixture())
                for row in rows:
                    row[field] = value
                with self.assertRaises(ValueError):
                    validate_common_end(rows, cpi, classes, cores=2, target=10000)


if __name__ == "__main__":
    unittest.main()
