from __future__ import annotations

import importlib.util
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_branch_replay_suite.py")
SPEC = importlib.util.spec_from_file_location("run_branch_replay_suite", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SUITE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUITE)


def test_select_traces_filters_and_deduplicates_split_aliases():
    base = {
        "trace_id": "trace-a",
        "trace_dir": "/trace/a",
        "cache_dir": "/cache/a",
        "workload": "W_a",
        "workload_role": "train_base",
        "seed": 0,
        "n_cores": 4,
    }
    manifest = {
        "splits": {
            "one": [base, {**base, "trace_id": "trace-c1", "n_cores": 1}],
            "alias": [base],
        }
    }
    rows = SUITE.select_traces(
        manifest, splits=["one", "alias"], core_counts={4, 8, 16, 32}
    )
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "trace-a"
    assert rows[0]["selected_splits"] == ["one", "alias"]


def test_aggregate_keeps_trace_equal_and_pooled_metrics_separate():
    rows = [
        {
            "branches": 100,
            "replay_misses": 20,
            "gem5_misses": 10,
            "miss_count_abs_relative_error": 1.0,
            "miss_rate_abs_error_pp": 10.0,
            "functional_history_mismatches": 0,
        },
        {
            "branches": 1000,
            "replay_misses": 90,
            "gem5_misses": 100,
            "miss_count_abs_relative_error": 0.1,
            "miss_rate_abs_error_pp": 1.0,
            "functional_history_mismatches": 0,
        },
    ]
    result = SUITE.aggregate_rows(rows)
    assert result["trace_equal_count_mape"] == 0.55
    assert result["trace_equal_rate_mae_pp"] == 5.5
    assert result["pooled_count_abs_relative_error"] == 0.0
    assert result["pooled_rate_abs_error_pp"] == 0.0
