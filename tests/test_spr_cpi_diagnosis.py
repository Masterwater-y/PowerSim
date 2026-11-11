"""Guard the functional-population boundary of controlled CPI experiments."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_spr_cpi_diagnosis import model_population, VARIANTS


class PopulationGateTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {
            "functional_source_identity": {"sha256": "full-functional-window"},
            "time_accounting": {
                "task_ids": [100, 101],
                "active_cycles": 1000,
                "per_core": [
                    {"core_id": 0, "task_macro_instructions": 10},
                    {"core_id": 1, "task_macro_instructions": 20},
                ],
            },
            "cores": [{"core": 0, "uops": 13}, {"core": 1, "uops": 22}],
        }

    def test_cycle_change_is_allowed_but_population_change_is_not(self):
        candidate = copy.deepcopy(self.baseline)
        candidate["time_accounting"]["active_cycles"] = 500
        self.assertEqual(model_population(candidate), model_population(self.baseline))
        # Preserve aggregate population while shifting one macro between cores.
        candidate["time_accounting"]["per_core"][0]["task_macro_instructions"] += 1
        candidate["time_accounting"]["per_core"][1]["task_macro_instructions"] -= 1
        self.assertNotEqual(model_population(candidate), model_population(self.baseline))

    def test_matching_aggregate_cannot_hide_changed_uop_or_task_identity(self):
        candidate = copy.deepcopy(self.baseline)
        candidate["cores"][0]["uops"] += 1
        candidate["cores"][1]["uops"] -= 1
        self.assertNotEqual(model_population(candidate), model_population(self.baseline))
        candidate = copy.deepcopy(self.baseline)
        candidate["time_accounting"]["task_ids"] = [100, 102]
        self.assertNotEqual(model_population(candidate), model_population(self.baseline))
        candidate = copy.deepcopy(self.baseline)
        candidate["functional_source_identity"]["sha256"] = "different-window"
        self.assertNotEqual(model_population(candidate), model_population(self.baseline))

    def test_port_control_retains_target_execution_latency(self):
        self.assertEqual(VARIANTS["gap"], {"core.spr_shared_fu_gap_aware": "true"})
        self.assertNotIn("core.spr_shared_fu_generic_latency",
                         VARIANTS["gap-window-prefetch"])


if __name__ == "__main__":
    unittest.main()
