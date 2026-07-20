"""Macro-prefix scheduler used by the LLMSim v29 native-token path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

import numpy as np


class MacroSchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class MacroStep:
    delta_cycles: float
    consumed_macros: np.ndarray
    candidate_cycles: np.ndarray
    capped: bool


@dataclass
class MacroCursorState:
    global_time_cycles: float
    macro_cursors: np.ndarray
    uop_cursors: np.ndarray
    steps: int = 0

    @classmethod
    def at_start(
        cls,
        macro_uop_begin: Sequence[np.ndarray],
        *,
        global_time_cycles: float,
    ) -> "MacroCursorState":
        macro = np.zeros(len(macro_uop_begin), dtype=np.int64)
        uop = np.asarray([
            int(values[0]) if len(values) else 0
            for values in macro_uop_begin
        ], dtype=np.int64)
        return cls(
            global_time_cycles=float(global_time_cycles),
            macro_cursors=macro,
            uop_cursors=uop,
        )


def select_macro_step(
    commit_time_macro: np.ndarray,
    valid_macro_mask: np.ndarray,
    *,
    target_stride_macro: int,
    max_step_cycles: float,
) -> MacroStep:
    """Select a global delta and the maximal consumable prefix of each core."""

    times = np.asarray(commit_time_macro, dtype=np.float64)
    valid = np.asarray(valid_macro_mask, dtype=np.bool_)
    if times.ndim != 2 or valid.shape != times.shape:
        raise MacroSchedulerError("times/mask must have identical [R,M] shape")
    if not 1 <= int(target_stride_macro) <= times.shape[1]:
        raise MacroSchedulerError(
            "target_stride_macro must be in [1, K_macro]"
        )
    if not np.isfinite(max_step_cycles) or max_step_cycles <= 0:
        raise MacroSchedulerError("max_step_cycles must be positive and finite")
    counts = valid.sum(axis=1, dtype=np.int64)
    if np.any(counts <= 0):
        raise MacroSchedulerError("scheduler received an inactive/empty core row")
    candidates = np.empty(times.shape[0], dtype=np.float64)
    for row, count in enumerate(counts):
        row_times = times[row, :count]
        if np.any(~np.isfinite(row_times)) or np.any(row_times < 0):
            raise MacroSchedulerError(f"core row {row} has negative/NaN time")
        if np.any(row_times[1:] < row_times[:-1]):
            raise MacroSchedulerError(f"core row {row} is not monotonic")
        position = min(int(target_stride_macro), int(count)) - 1
        candidates[row] = row_times[position]
    uncapped = float(candidates.min())
    delta = min(uncapped, float(max_step_cycles))
    consumed = np.asarray([
        int(np.count_nonzero(times[row, :count] <= delta))
        for row, count in enumerate(counts)
    ], dtype=np.int64)
    if int(consumed.sum()) <= 0:
        raise MacroSchedulerError(
            f"no progress at delta={delta}; candidates={candidates.tolist()}"
        )
    return MacroStep(
        delta_cycles=float(delta),
        consumed_macros=consumed,
        candidate_cycles=candidates,
        capped=bool(delta < uncapped),
    )


def apply_macro_step(
    state: MacroCursorState,
    step: MacroStep,
    macro_uop_begin: Sequence[np.ndarray],
    macro_uop_end: Sequence[np.ndarray],
) -> None:
    """Advance only whole macros and keep the UOP cursor at a macro boundary."""

    rows = len(macro_uop_begin)
    if len(macro_uop_end) != rows or state.macro_cursors.shape != (rows,):
        raise MacroSchedulerError("cursor/mapping core count mismatch")
    if step.consumed_macros.shape != (rows,):
        raise MacroSchedulerError("step core count mismatch")
    for row in range(rows):
        begins = np.asarray(macro_uop_begin[row], dtype=np.int64)
        ends = np.asarray(macro_uop_end[row], dtype=np.int64)
        if begins.shape != ends.shape:
            raise MacroSchedulerError(f"core {row} macro mapping shape mismatch")
        old = int(state.macro_cursors[row])
        count = int(step.consumed_macros[row])
        new = old + count
        if count < 0 or new > len(begins):
            raise MacroSchedulerError(f"core {row} cursor would leave the trace")
        expected_uop = int(begins[old]) if old < len(begins) else (
            int(ends[-1]) if len(ends) else 0
        )
        if int(state.uop_cursors[row]) != expected_uop:
            raise MacroSchedulerError(
                f"core {row} UOP cursor is not at macro {old} boundary"
            )
        state.macro_cursors[row] = new
        state.uop_cursors[row] = (
            int(begins[new]) if new < len(begins)
            else (int(ends[-1]) if len(ends) else 0)
        )
    state.global_time_cycles += float(step.delta_cycles)
    state.steps += 1


def validate_finished(
    state: MacroCursorState,
    macro_uop_end: Sequence[np.ndarray],
) -> Dict[str, int]:
    """Prove final macro and UOP cursors reach every stream tail."""

    total_macros = 0
    total_uops = 0
    for row, ends_value in enumerate(macro_uop_end):
        ends = np.asarray(ends_value, dtype=np.int64)
        expected_macro = len(ends)
        expected_uop = int(ends[-1]) if len(ends) else 0
        if int(state.macro_cursors[row]) != expected_macro:
            raise MacroSchedulerError(
                f"core {row} has {expected_macro - int(state.macro_cursors[row])} "
                "remaining macros"
            )
        if int(state.uop_cursors[row]) != expected_uop:
            raise MacroSchedulerError(
                f"core {row} UOP cursor {state.uop_cursors[row]} != {expected_uop}"
            )
        total_macros += expected_macro
        total_uops += expected_uop
    return {
        "steps": int(state.steps),
        "total_macros": int(total_macros),
        "total_uops": int(total_uops),
    }


def oracle_rollout(
    macro_end_ticks: Sequence[np.ndarray],
    macro_uop_begin: Sequence[np.ndarray],
    macro_uop_end: Sequence[np.ndarray],
    *,
    tick_per_cycle: float,
    start_tick: int,
    k_macro: int = 256,
    target_stride_macro: int = 256,
    max_step_cycles: float = 1024.0,
) -> Dict[str, float]:
    """Initial scheduler proof using labels only as an oracle predictor."""

    if tick_per_cycle <= 0:
        raise MacroSchedulerError("tick_per_cycle must be positive")
    state = MacroCursorState.at_start(
        macro_uop_begin,
        global_time_cycles=float(start_tick) / float(tick_per_cycle),
    )
    cap_events = 0
    zero_core_steps = 0
    while True:
        active = [
            row for row, values in enumerate(macro_end_ticks)
            if int(state.macro_cursors[row]) < len(values)
        ]
        if not active:
            break
        times = np.zeros((len(active), k_macro), dtype=np.float64)
        valid = np.zeros((len(active), k_macro), dtype=np.bool_)
        for local, row in enumerate(active):
            cursor = int(state.macro_cursors[row])
            end = min(len(macro_end_ticks[row]), cursor + k_macro)
            count = end - cursor
            absolute = np.asarray(
                macro_end_ticks[row][cursor:end], dtype=np.float64,
            ) / float(tick_per_cycle)
            relative = absolute - state.global_time_cycles
            # With S_macro == K_macro, an equal-time retirement group can cross
            # a window boundary.  The continuation is a valid zero-cycle step:
            # cursors still advance, so the rollout cannot stall.
            if np.any(relative < 0):
                raise MacroSchedulerError(
                    f"oracle produced a negative time on core {row}"
                )
            times[local, :count] = relative
            valid[local, :count] = True
        selected = select_macro_step(
            times,
            valid,
            target_stride_macro=target_stride_macro,
            max_step_cycles=max_step_cycles,
        )
        full_consumed = np.zeros(len(macro_end_ticks), dtype=np.int64)
        full_consumed[np.asarray(active, dtype=np.int64)] = selected.consumed_macros
        zero_core_steps += int(np.count_nonzero(selected.consumed_macros == 0))
        cap_events += int(selected.capped)
        apply_macro_step(
            state,
            MacroStep(
                delta_cycles=selected.delta_cycles,
                consumed_macros=full_consumed,
                candidate_cycles=selected.candidate_cycles,
                capped=selected.capped,
            ),
            macro_uop_begin,
            macro_uop_end,
        )
    finished = validate_finished(state, macro_uop_end)
    return {
        **{key: float(value) for key, value in finished.items()},
        "final_global_time_cycles": float(state.global_time_cycles),
        "cap_events": float(cap_events),
        "zero_core_rows": float(zero_core_steps),
    }
