import numpy as np

from tcsim.v30.exposure import (
    EXPOSURE_CAUSAL_FIELDS,
    EXPOSURE_FIELDS,
    build_causal_exposure,
    slice_exposure_window,
)
from tcsim.v30.pmu import cache_miss_pmu_error_report


def test_exposure_is_functional_and_window_local() -> None:
    producer = np.zeros((257, 4), dtype=np.uint32)
    memory = np.zeros(257, dtype=np.uint8)
    semantic = np.zeros(257, dtype=np.uint8)
    memory[0] = 1
    producer[2, 0] = 2
    producer[2, 1] = 2  # Duplicate operand must count as one consumer.
    producer[256, 0] = 256  # Outside the first K-window.
    causal = build_causal_exposure(producer, memory > 0)
    sidecar = {"causal": causal, "producer_distance": producer}
    window = slice_exposure_window(sidecar, memory, semantic, 0, 256, 256)

    assert window.shape == (256, len(EXPOSURE_FIELDS))
    assert np.isfinite(window).all()
    offset = len(EXPOSURE_CAUSAL_FIELDS)
    assert window[0, offset] > 0.0  # first consumer distance
    assert 0.0 < window[0, offset + 1] < 0.2  # unique fanout == 1
    # The consumer at absolute UOP 256 is not visible in window [0,256).
    expected_span = np.log1p(2) / np.log1p(256)
    assert np.isclose(window[0, offset + 3], expected_span)


def test_cache_miss_pmu_unavailable_is_explicit() -> None:
    report = cache_miss_pmu_error_report(
        trace_dir=None,
        expected_core_ids=[0],
        canonical_state={"events": 0},
        complete=True,
    )
    assert report["qualified"] is False
    assert report["status"] == "unavailable"
    assert report["reason"] == "trace_dir_not_recorded"
    assert report["oracle_columns_used_as_model_input"] is False
