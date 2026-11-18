"""Completion-time comparison for the same retained physical FST population.

This metric includes non-task contexts. It is not workload-task active CPI.
All simulators use their own time units; cycle proxies use the reference clock.
Saved oracle audits give provisional evidence until freshly rebound to contents.
"""

import math


def compare_elapsed(
    *,
    reference_seconds,
    reference_hz,
    model_seconds,
    participants,
    macros,
    uops,
    expected_macros,
    expected_uops
):
    if participants <= 0 or len(macros) != participants or len(uops) != participants:
        raise ValueError("missing physical participant")
    if list(macros) != list(expected_macros) or list(uops) != list(expected_uops):
        raise ValueError("per-core retained population mismatch")
    if not all(
        math.isfinite(v) and v > 0
        for v in [reference_seconds, reference_hz, model_seconds]
    ):
        raise ValueError("invalid completion time or reference clock")
    denominator = sum(macros)
    if denominator <= 0:
        raise ValueError("empty macro population")
    reference = reference_seconds * reference_hz * participants / denominator
    model = model_seconds * reference_hz * participants / denominator
    return dict(
        metric="retained-physical-completion-time-v1",
        reference_seconds=reference_seconds,
        model_seconds=model_seconds,
        relative_error=(model_seconds - reference_seconds) / reference_seconds,
        reference_elapsed_macro_cycle_proxy=reference,
        model_elapsed_macro_cycle_proxy=model,
        absolute_error_cycles_per_macro=abs(model - reference),
        active_cpi=False,
        formal_validation=False,
    )
