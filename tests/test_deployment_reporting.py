"""Text deployment report aggregation semantics."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.inference.reporting import render_deployment_text_report


class TestDeploymentReporting(unittest.TestCase):
    def test_report_uses_workload_macro_and_splits_heldout(self):
        common = {
            "n_cores": 4,
            "n_steps": 10,
            "pred_roi_cpi": 1.0,
            "true_roi_cpi": 1.0,
            "window_cpi_mape_p90": 0.3,
            "pred_branch_miss_rate": 0.1,
            "true_branch_miss_rate": 0.1,
            "branch_miss_rate_abs_error": 0.0,
            "chunk_cpi_mape_mean": 0.2,
        }
        report = {
            "run": {"checkpoint": "best.pt", "split": "train,test_business"},
            "aggregate": {
                "n_chunks": 2,
                "roi_valid_label_uops": 512,
                "roi_uops": 512,
                "roi_label_coverage": 1.0,
                "pred_roi_cpi": 1.0,
                "true_roi_cpi": 1.0,
                "global_roi_cpi_error": 0.0,
                "pred_branch_miss_rate": 0.1,
                "true_branch_miss_rate": 0.1,
                "branch_miss_rate_relative_error": 0.0,
                "branch_miss_rate_abs_error": 0.0,
            },
            "traces": [
                dict(
                    common,
                    workload="W_base",
                    roi_cpi_error=0.0,
                    window_cpi_mape_mean=0.1,
                    branch_miss_rate_relative_error=0.1,
                ),
                dict(
                    common,
                    workload="W_business_heldout",
                    roi_cpi_error=1.0,
                    window_cpi_mape_mean=0.5,
                    branch_miss_rate_relative_error=0.5,
                ),
            ],
        }
        text = render_deployment_text_report(report)
        self.assertIn("    4 all          2     50.00", text)
        self.assertIn("    4 train/base   1      0.00", text)
        self.assertIn("    4 heldout      1    100.00", text)
        self.assertIn("W_business_heldout", text)
        self.assertIn("pooled micro aggregate (not the headline", text)


if __name__ == "__main__":
    unittest.main()
