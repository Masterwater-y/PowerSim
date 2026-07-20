"""Label-free model predictor and free-running macro rollout."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Mapping

import numpy as np
import torch

from eval.macro_v29_scheduler import (
    MacroCursorState,
    MacroSchedulerError,
    MacroStep,
    apply_macro_step,
    select_macro_step,
    validate_finished,
)
from train.macro_v29_dataset import (
    CachedSemanticSource,
    CachedTokenSource,
    InstructionResolver,
    MacroContractError,
    PackedTraceMacroContext,
    assert_model_input_allowlist,
    collate_macro_contexts,
)


@dataclass(frozen=True)
class PredictedMacroContext:
    core_ids: tuple[int, ...]
    commit_time_macro: np.ndarray
    valid_macro_mask: np.ndarray
    branch_miss_probability: np.ndarray
    label_keys: tuple[str, ...]


class MacroV29ModelPredictor:
    """Construct label-free contexts and call a macro timing model."""

    def __init__(
        self,
        context: PackedTraceMacroContext,
        resolver: InstructionResolver,
        tokenizer: Any | None,
        model: torch.nn.Module,
        *,
        device: torch.device | str = "cpu",
        max_tokens: int = 4096,
        token_cache: CachedTokenSource | None = None,
        semantic_cache: CachedSemanticSource | None = None,
        parquet_path: str | None = None,
        activation_diagnostic_forwards: int = 0,
    ) -> None:
        self.context = context
        self.resolver = resolver
        self.tokenizer = tokenizer
        self.model = model
        self.semantic_input_mode = str(getattr(
            getattr(model, "config", None),
            "semantic_input_mode",
            "cached_macro_soft_token" if semantic_cache is not None else "native_token",
        ))
        self.device = torch.device(device)
        self.max_tokens = int(max_tokens)
        self.token_cache = token_cache
        self.semantic_cache = semantic_cache
        self.parquet_path = parquet_path
        self.activation_diagnostic_forwards = max(
            0, int(activation_diagnostic_forwards)
        )
        self._timing_s = {
            "context": 0.0,
            "collate": 0.0,
            "online_model": 0.0,
            "output_copy": 0.0,
            "predict_total": 0.0,
        }
        self._timing_calls = 0
        self._activation_sum_squares: Dict[str, float] = {}
        self._activation_counts: Dict[str, int] = {}

    def timing_report(self) -> Dict[str, Any]:
        calls = int(self._timing_calls)
        activation_rms = {
            key: float(
                (self._activation_sum_squares[key]
                 / max(1, self._activation_counts[key])) ** 0.5
            )
            for key in sorted(self._activation_sum_squares)
        }
        return {
            "calls": calls,
            "seconds": dict(self._timing_s),
            "mean_ms": {
                key: 1000.0 * value / max(1, calls)
                for key, value in self._timing_s.items()
            },
            "activation_rms": activation_rms,
            "activation_counts": dict(self._activation_counts),
        }

    def predict(
        self,
        cursors: Mapping[int, int],
        *,
        state_time_cycles: float,
        last_commit_cycles: Mapping[int, float],
    ) -> PredictedMacroContext:
        predict_started = time.perf_counter()
        stage_started = predict_started
        windows = self.context.context_from_cursors(
            cursors,
            self.resolver,
            self.tokenizer,
            state_tick=None,
            state_time_cycles=float(state_time_cycles),
            include_labels=False,
            last_commit_cycles=last_commit_cycles,
            max_tokens=self.max_tokens,
            token_cache=self.token_cache,
            semantic_cache=self.semantic_cache,
            parquet_path=self.parquet_path,
            semantic_input_mode=self.semantic_input_mode,
        )
        self._timing_s["context"] += time.perf_counter() - stage_started
        label_keys = tuple(sorted({
            key for window in windows for key in window.labels
        }))
        if label_keys:
            raise MacroContractError(
                f"free predictor received label keys {label_keys}"
            )
        for window in windows:
            assert_model_input_allowlist(window)
        stage_started = time.perf_counter()
        batch = collate_macro_contexts(
            [windows],
            pad_token_id=int(
                self.tokenizer.pad_token_id or 0
                if self.tokenizer is not None else 0
            ),
        )
        tensor_batch = {
            key: value.to(self.device)
            for key, value in batch.items()
            if torch.is_tensor(value)
        }
        self._timing_s["collate"] += time.perf_counter() - stage_started
        self.model.eval()
        if hasattr(self.model, "collect_activation_diagnostics"):
            self.model.collect_activation_diagnostics = bool(
                self._timing_calls < self.activation_diagnostic_forwards
            )
        stage_started = time.perf_counter()
        with torch.no_grad():
            output = self.model(tensor_batch)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        self._timing_s["online_model"] += time.perf_counter() - stage_started
        activation_statistics = output.get("activation_statistics", {})
        if isinstance(activation_statistics, Mapping):
            for name, values in activation_statistics.items():
                if not isinstance(values, Mapping):
                    continue
                sum_squares = values.get("sum_squares")
                count = values.get("count")
                if torch.is_tensor(sum_squares) and torch.is_tensor(count):
                    self._activation_sum_squares[name] = (
                        self._activation_sum_squares.get(name, 0.0)
                        + float(sum_squares.item())
                    )
                    self._activation_counts[name] = (
                        self._activation_counts.get(name, 0)
                        + int(count.item())
                    )
        stage_started = time.perf_counter()
        result = PredictedMacroContext(
            core_ids=tuple(int(window.control["core_id"]) for window in windows),
            commit_time_macro=(
                output["commit_time_macro"].detach().float().cpu().numpy()
            ),
            valid_macro_mask=(
                tensor_batch["valid_macro_mask"].detach().cpu().numpy().astype(bool)
            ),
            branch_miss_probability=(
                output["branch_miss_probability"].detach().float().cpu().numpy()
            ),
            label_keys=label_keys,
        )
        self._timing_s["output_copy"] += time.perf_counter() - stage_started
        self._timing_s["predict_total"] += time.perf_counter() - predict_started
        self._timing_calls += 1
        return result


def model_free_rollout(
    context: PackedTraceMacroContext,
    predictor: MacroV29ModelPredictor,
    *,
    target_stride_macro: int = 256,
    max_step_cycles: float = 1024.0,
    max_steps: int | None = None,
    stop_after_macros: int | None = None,
    progress_interval: int = 0,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Advance predicted macro cursors without consulting commit-time labels.

    ``max_steps``/``stop_after_macros`` create a bounded smoke rollout.  If
    neither limit is supplied, reaching every stream tail is mandatory and an
    exactly-once report is returned.
    """

    rollout_started = time.perf_counter()
    core_ids = tuple(int(value) for value in context.core_ids)
    begins = [context.views[core_id].macro_uop_begin for core_id in core_ids]
    ends = [context.views[core_id].macro_uop_end for core_id in core_ids]
    total_available_macros = int(sum(len(values) for values in ends))
    total_available_uops = int(sum(
        int(values[-1]) if len(values) else 0 for values in ends
    ))
    state = MacroCursorState.at_start(begins, global_time_cycles=0.0)
    last_commit = {core_id: 0.0 for core_id in core_ids}
    total_consumed = 0
    total_consumed_uops = 0
    per_core_retired_macros = {core_id: 0 for core_id in core_ids}
    per_core_retired_uops = {core_id: 0 for core_id in core_ids}
    resolver = getattr(predictor, "resolver", None)
    functional_available = bool(
        resolver is not None
        and all(hasattr(context.views[core_id], "arrays") for core_id in core_ids)
    )
    functional_counts: Dict[str, float | int] = {
        "memory_access_uops": 0,
        "read_uops": 0,
        "write_uops": 0,
        "architectural_branches": 0,
        "predicted_branch_misses": 0.0,
        "cross_core_line_access_uops": 0,
    }
    line_owners: Dict[int, set[int]] = {}
    zero_core_rows = 0
    capped_steps = 0
    min_step_consumed: int | None = None
    max_step_consumed = 0
    observed_label_keys: set[str] = set()
    scheduler_seconds = 0.0
    while True:
        active_rows = [
            row for row, core_id in enumerate(core_ids)
            if int(state.macro_cursors[row]) < context.views[core_id].n_macros
        ]
        if not active_rows:
            break
        if max_steps is not None and state.steps >= int(max_steps):
            break
        if stop_after_macros is not None and total_consumed >= int(stop_after_macros):
            break
        cursor_map = {
            core_id: int(state.macro_cursors[row])
            for row, core_id in enumerate(core_ids)
        }
        predicted = predictor.predict(
            cursor_map,
            state_time_cycles=state.global_time_cycles,
            last_commit_cycles=last_commit,
        )
        expected_active_ids = tuple(core_ids[row] for row in active_rows)
        if predicted.core_ids != expected_active_ids:
            raise MacroSchedulerError(
                f"predictor active cores {predicted.core_ids} != {expected_active_ids}"
            )
        observed_label_keys.update(predicted.label_keys)
        scheduler_started = time.perf_counter()
        selected = select_macro_step(
            predicted.commit_time_macro,
            predicted.valid_macro_mask,
            target_stride_macro=int(target_stride_macro),
            max_step_cycles=float(max_step_cycles),
        )
        full_consumed = np.zeros(len(core_ids), dtype=np.int64)
        full_consumed[np.asarray(active_rows, dtype=np.int64)] = (
            selected.consumed_macros
        )
        before_time = float(state.global_time_cycles)
        before_uop_cursors = state.uop_cursors.copy()
        step_line_accesses: Dict[int, Dict[int, int]] = {}
        for local, row in enumerate(active_rows):
            count = int(selected.consumed_macros[local])
            if count:
                core_id = core_ids[row]
                macro_begin = int(state.macro_cursors[row])
                macro_end = macro_begin + count
                uop_begin = int(begins[row][macro_begin])
                uop_end = int(ends[row][macro_end - 1])
                per_core_retired_macros[core_id] += count
                per_core_retired_uops[core_id] += uop_end - uop_begin
                last_commit[core_id] = before_time + float(
                    predicted.commit_time_macro[local, count - 1]
                )
                if functional_available:
                    view = context.views[core_id]
                    access = np.asarray(
                        view.arrays["access"][uop_begin:uop_end],
                        dtype=np.uint8,
                    )
                    physical_line = np.asarray(
                        view.arrays["physical_line"][uop_begin:uop_end],
                        dtype=np.int64,
                    )
                    memory = access > 0
                    functional_counts["memory_access_uops"] += int(memory.sum())
                    functional_counts["read_uops"] += int(
                        np.count_nonzero((access == 1) | (access == 3))
                    )
                    functional_counts["write_uops"] += int(
                        np.count_nonzero((access == 2) | (access == 3))
                    )
                    for line in physical_line[memory & (physical_line >= 0)]:
                        per_line = step_line_accesses.setdefault(int(line), {})
                        per_line[core_id] = per_line.get(core_id, 0) + 1
                    for local_macro in range(count):
                        pc = int(view.macro_pc[macro_begin + local_macro])
                        if not resolver.is_architectural_branch(pc):
                            continue
                        functional_counts["architectural_branches"] += 1
                        functional_counts["predicted_branch_misses"] += float(
                            predicted.branch_miss_probability[local, local_macro]
                        )
        if functional_available:
            # Treat all line effects selected in one global scheduler step as
            # a batch.  This avoids any core-ID ordering shortcut for ties.
            for line, per_core_accesses in step_line_accesses.items():
                prior = line_owners.get(line, set())
                current = set(per_core_accesses)
                for core_id, accesses in per_core_accesses.items():
                    if (prior - {core_id}) or (current - {core_id}):
                        functional_counts["cross_core_line_access_uops"] += int(
                            accesses
                        )
                line_owners.setdefault(line, set()).update(current)
        apply_macro_step(
            state,
            MacroStep(
                delta_cycles=selected.delta_cycles,
                consumed_macros=full_consumed,
                candidate_cycles=selected.candidate_cycles,
                capped=selected.capped,
            ),
            begins,
            ends,
        )
        step_total = int(full_consumed.sum())
        step_uops = int((state.uop_cursors - before_uop_cursors).sum())
        total_consumed += step_total
        total_consumed_uops += step_uops
        min_step_consumed = (
            step_total if min_step_consumed is None
            else min(min_step_consumed, step_total)
        )
        max_step_consumed = max(max_step_consumed, step_total)
        zero_core_rows += int(np.count_nonzero(selected.consumed_macros == 0))
        capped_steps += int(selected.capped)
        for row, core_id in enumerate(core_ids):
            cursor = int(state.macro_cursors[row])
            expected_uop = (
                int(begins[row][cursor]) if cursor < len(begins[row])
                else int(ends[row][-1])
            )
            if int(state.uop_cursors[row]) != expected_uop:
                raise MacroSchedulerError(
                    f"core {core_id} left a whole-macro UOP boundary"
                )
        scheduler_seconds += time.perf_counter() - scheduler_started
        bounded_stop = (
            (max_steps is not None and state.steps >= int(max_steps))
            or (
                stop_after_macros is not None
                and total_consumed >= int(stop_after_macros)
            )
        )
        should_emit = (
            progress is not None
            and int(progress_interval) > 0
            and (
                state.steps % int(progress_interval) == 0
                or total_consumed >= total_available_macros
                or bounded_stop
            )
        )
        if should_emit:
            elapsed = time.perf_counter() - rollout_started
            timing = (
                predictor.timing_report()
                if hasattr(predictor, "timing_report") else None
            )
            progress({
                "phase": "free_running",
                "step": int(state.steps),
                "active_cores": len(active_rows),
                "retired_macros": int(total_consumed),
                "total_macros": total_available_macros,
                "retired_uops": int(total_consumed_uops),
                "total_uops": total_available_uops,
                "macro_per_s": float(total_consumed / max(elapsed, 1.0e-12)),
                "elapsed_s": float(elapsed),
                "global_time_cycles": float(state.global_time_cycles),
                "delta_cycles": float(selected.delta_cycles),
                "retired_macros_per_step": float(
                    total_consumed / max(1, state.steps)
                ),
                "mean_step_ms": float(
                    1000.0 * elapsed / max(1, state.steps)
                ),
                "model_forwards": int(
                    timing.get("calls", state.steps)
                    if isinstance(timing, Mapping) else state.steps
                ),
                "predictor_timing": timing,
            })
    complete = all(
        int(state.macro_cursors[row]) == len(ends[row])
        for row in range(len(core_ids))
    )
    finished = validate_finished(state, ends) if complete else None
    if max_steps is None and stop_after_macros is None and not complete:
        raise MacroSchedulerError("unbounded free rollout did not finish")
    if sum(per_core_retired_macros.values()) != total_consumed:
        raise MacroSchedulerError("per-core/global retired macro totals differ")
    if sum(per_core_retired_uops.values()) != total_consumed_uops:
        raise MacroSchedulerError("per-core/global retired UOP totals differ")
    if complete and finished is not None:
        if int(finished["total_macros"]) != total_consumed:
            raise MacroSchedulerError("complete rollout macro total is not exactly once")
        if int(finished["total_uops"]) != total_consumed_uops:
            raise MacroSchedulerError("complete rollout UOP total is not exactly once")
    elapsed_s = time.perf_counter() - rollout_started
    return {
        "complete": bool(complete),
        "steps": int(state.steps),
        "total_available_macros": total_available_macros,
        "total_available_uops": total_available_uops,
        "total_consumed_macros": int(total_consumed),
        "total_consumed_uops": int(total_consumed_uops),
        "global_time_cycles": float(state.global_time_cycles),
        "macro_cursors": {
            str(core_id): int(state.macro_cursors[row])
            for row, core_id in enumerate(core_ids)
        },
        "uop_cursors": {
            str(core_id): int(state.uop_cursors[row])
            for row, core_id in enumerate(core_ids)
        },
        "predicted_last_commit_cycles": {
            str(core_id): float(last_commit[core_id]) for core_id in core_ids
        },
        "per_core_retired_macros": {
            str(core_id): int(per_core_retired_macros[core_id])
            for core_id in core_ids
        },
        "per_core_retired_uops": {
            str(core_id): int(per_core_retired_uops[core_id])
            for core_id in core_ids
        },
        "functional_state": {
            "available": functional_available,
            **functional_counts,
            "unique_physical_lines": int(len(line_owners)),
            "raw_line_ids_exposed": False,
            "update_order": "per-global-step-batch",
        },
        "min_consumed_per_step": int(min_step_consumed or 0),
        "max_consumed_per_step": int(max_step_consumed),
        "zero_core_rows": int(zero_core_rows),
        "capped_steps": int(capped_steps),
        "free_context_label_keys": sorted(observed_label_keys),
        "exactly_once": finished,
        "elapsed_s": float(elapsed_s),
        "steps_per_s": float(state.steps / max(elapsed_s, 1e-12)),
        "aggregate_macro_per_s": float(total_consumed / max(elapsed_s, 1e-12)),
        "aggregate_uop_per_s": float(total_consumed_uops / max(elapsed_s, 1e-12)),
        "retired_macros_per_model_forward": float(
            total_consumed / max(1, state.steps)
        ),
        "mean_step_ms": float(1000.0 * elapsed_s / max(1, state.steps)),
        "scheduler_seconds": float(scheduler_seconds),
        "predictor_timing": (
            predictor.timing_report()
            if hasattr(predictor, "timing_report") else None
        ),
    }
