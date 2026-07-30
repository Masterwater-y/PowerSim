from __future__ import annotations

from collections import Counter

from tcsim.v29.sampling import CoverageFirstTraceBalancedSampler


def test_coverage_epoch_shards_one_global_permutation_across_ranks():
    trace_ids = ["small"] * 2 + ["large"] * 7 + ["tiny"]
    samplers = [
        CoverageFirstTraceBalancedSampler(
            trace_ids, num_replicas=3, rank=rank, seed=29,
        )
        for rank in range(3)
    ]
    draws = []
    for sampler in samplers:
        sampler.set_epoch(0)
        local = list(sampler)
        assert len(local) == 4
        draws.extend(local)
    counts = Counter(draws)
    assert set(counts) == set(range(len(trace_ids)))
    assert sum(counts.values()) == 12
    assert sum(value - 1 for value in counts.values()) == 2


def test_coverage_resume_offset_is_exact_suffix():
    trace_ids = ["a"] * 5 + ["b"] * 8
    sampler = CoverageFirstTraceBalancedSampler(
        trace_ids, num_replicas=2, rank=1, seed=1234,
    )
    sampler.set_epoch(0)
    full = list(sampler)
    sampler.set_epoch(0, start_offset=3)
    assert list(sampler) == full[3:]
    assert len(sampler) == len(full) - 3


def test_later_epochs_are_deterministic_trace_balanced_replacement():
    trace_ids = ["small"] * 10 + ["large"] * 100
    first = CoverageFirstTraceBalancedSampler(trace_ids, seed=1234)
    second = CoverageFirstTraceBalancedSampler(trace_ids, seed=1234)
    first.set_epoch(1)
    second.set_epoch(1)
    draws = list(first)
    assert draws == list(second)
    assert len(draws) == len(trace_ids)
    selected_traces = Counter(trace_ids[index] for index in draws)
    # The two traces have equal total sampling weight despite a 10x size gap.
    assert 35 <= selected_traces["small"] <= 75
    assert 35 <= selected_traces["large"] <= 75
    assert len(set(draws)) < len(draws)


def test_configured_second_coverage_repeats_the_exact_resume_order():
    trace_ids = ["a"] * 5 + ["b"] * 8
    sampler = CoverageFirstTraceBalancedSampler(
        trace_ids, num_replicas=2, rank=1, seed=1234, coverage_epochs=2,
    )
    sampler.set_epoch(0)
    first = list(sampler)
    sampler.set_epoch(1)
    assert list(sampler) == first
    sampler.set_epoch(1, start_offset=3)
    assert list(sampler) == first[3:]
    sampler.set_epoch(2)
    assert len(list(sampler)) == len(first)
