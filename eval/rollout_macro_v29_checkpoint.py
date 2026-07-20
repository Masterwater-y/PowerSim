#!/usr/bin/env python3
"""Strictly reload a macro-v29 checkpoint and run label-free rollout."""
from __future__ import annotations

import argparse
from argparse import Namespace
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping

import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.macro_v29_rollout import MacroV29ModelPredictor, model_free_rollout
from train.macro_v29_dataset import (
    CachedSemanticSource,
    CachedTokenSource,
    MacroContractError,
    PackedTraceMacroContext,
    ParquetInstructionResolver,
    SemanticVariantInstructionResolver,
)
from train.train_macro_v29 import (
    build_timing_model,
    load_trainable,
    text_variant_for,
)


POSTHOC_INTERVENTIONS = (
    "full_real",
    "no_offline_hidden",
    "no_lora",
    "semantic_permute",
    "no_llm_branch",
)


def _apply_posthoc_intervention(
    model: torch.nn.Module,
    intervention: str,
) -> dict[str, Any]:
    """Apply an evaluation-only intervention after strict checkpoint load."""

    name = str(intervention)
    if name not in POSTHOC_INTERVENTIONS:
        raise ValueError(f"unsupported post-hoc intervention {name!r}")
    core = model.module if hasattr(model, "module") else model
    report: dict[str, Any] = {
        "name": name,
        "checkpoint_file_mutated": False,
        "online_path_executed": True,
    }
    if name == "no_offline_hidden":
        gate = getattr(core, "semantic_gate", None)
        if gate is None:
            raise MacroContractError(
                "no_offline_hidden requires cached semantic input"
            )
        report["semantic_gate_logit_before"] = float(gate.detach().float())
        report["semantic_gate_probability_before"] = float(
            torch.sigmoid(gate.detach().float())
        )
        with torch.no_grad():
            gate.fill_(float("-inf"))
        report.update({
            "semantic_gate_probability_after": 0.0,
            "anchor_retained": True,
            "online_qwen_retained": True,
        })
    elif name == "no_lora":
        lora_parameters = [
            (parameter_name, parameter)
            for parameter_name, parameter in core.named_parameters()
            if ".lora_A." in parameter_name or ".lora_B." in parameter_name
        ]
        if not lora_parameters:
            raise MacroContractError("no_lora found no LoRA parameters")
        before_sum_squares = sum(
            float(parameter.detach().float().square().sum())
            for _, parameter in lora_parameters
        )
        with torch.no_grad():
            for _, parameter in lora_parameters:
                parameter.zero_()
        report.update({
            "zeroed_lora_tensors": len(lora_parameters),
            "lora_l2_before": before_sum_squares ** 0.5,
            "lora_l2_after": 0.0,
            "pretrained_qwen_retained": True,
        })
    elif name == "no_llm_branch":
        report["semantic_mode_before"] = str(core.config.semantic_mode)
        core.config.semantic_mode = "side_only"
        report.update({
            "semantic_mode_after": "side_only",
            "online_qwen_executed": True,
            "llm_branch_zeroed_after_projection": True,
        })
    elif name == "semantic_permute":
        report.update({
            "semantic_and_anchor_permuted_together": True,
            "model_parameters_mutated": False,
        })
    else:
        report["model_parameters_mutated"] = False
    return report


def _abs_relative_error(predicted: float, truth: float) -> float:
    return abs(float(predicted) - float(truth)) / max(abs(float(truth)), 1.0e-12)


def _signed_relative_error(predicted: float, truth: float) -> float:
    return (float(predicted) - float(truth)) / max(abs(float(truth)), 1.0e-12)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(quantile)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _deployment_metrics(
    context: PackedTraceMacroContext,
    rollout: Mapping[str, Any],
) -> dict[str, Any]:
    core_rows = list(context.meta["cores"])
    tick_per_cycle = float(context.meta["tick_per_cycle"])
    total_macros = sum(int(row["n_macros"]) for row in core_rows)
    has_uop_counts = all("n_uops" in row for row in core_rows)
    total_uops = (
        sum(int(row["n_uops"]) for row in core_rows)
        if has_uop_counts else None
    )
    true_cycle_sum = sum(
        float(row["full_macro_cpi"]) * int(row["n_macros"])
        for row in core_rows
    )
    predicted_cycle_sum = sum(
        float(value) for value in rollout["predicted_last_commit_cycles"].values()
    )
    first_tick = min(int(row["first_commit_tick"]) for row in core_rows)
    last_tick = max(int(row["last_commit_tick"]) for row in core_rows)
    true_makespan = float(last_tick - first_tick) / tick_per_cycle
    predicted_makespan = float(rollout["global_time_cycles"])
    complete = bool(rollout["complete"])
    retired_macros = int(rollout["total_consumed_macros"])
    retired_uops = (
        int(rollout["total_consumed_uops"])
        if "total_consumed_uops" in rollout else None
    )
    predicted_macro_cpi = predicted_cycle_sum / max(1, retired_macros)
    true_macro_cpi = true_cycle_sum / max(1, total_macros)
    predicted_micro_cpi = (
        predicted_cycle_sum / max(1, retired_uops)
        if retired_uops is not None else None
    )
    true_micro_cpi = (
        true_cycle_sum / max(1, total_uops)
        if total_uops is not None else None
    )

    per_core = []
    core_errors = []
    core_signed_errors = []
    for row in core_rows:
        core_id = int(row["core_id"])
        predicted_cycles = float(
            rollout["predicted_last_commit_cycles"][str(core_id)]
        )
        true_cycles = float(
            int(row["last_commit_tick"]) - int(row["first_commit_tick"])
        ) / tick_per_cycle
        error = _abs_relative_error(predicted_cycles, true_cycles) if complete else None
        signed_error = (
            _signed_relative_error(predicted_cycles, true_cycles)
            if complete else None
        )
        if error is not None:
            core_errors.append(error)
            core_signed_errors.append(float(signed_error))
        retired_core_macros = int(
            rollout["per_core_retired_macros"][str(core_id)]
        )
        retired_core_uops = (
            int(rollout["per_core_retired_uops"][str(core_id)])
            if "per_core_retired_uops" in rollout else None
        )
        true_core_macros = int(row["n_macros"])
        true_core_uops = int(row["n_uops"]) if "n_uops" in row else None
        per_core.append({
            "core_id": core_id,
            "retired_macros": retired_core_macros,
            "true_macros": true_core_macros,
            "retired_uops": retired_core_uops,
            "true_uops": true_core_uops,
            "predicted_cycles": predicted_cycles,
            "true_cycles": true_cycles,
            "cycle_abs_relative_error": error,
            "cycle_signed_relative_error": signed_error,
            "predicted_macro_cpi": (
                predicted_cycles / max(1, retired_core_macros)
            ),
            "true_macro_cpi": true_cycles / max(1, true_core_macros),
            "predicted_micro_cpi": (
                predicted_cycles / max(1, retired_core_uops)
                if retired_core_uops is not None else None
            ),
            "true_micro_cpi": (
                true_cycles / max(1, true_core_uops)
                if true_core_uops is not None else None
            ),
        })

    functional = rollout["functional_state"]
    predicted_branch_misses = float(functional["predicted_branch_misses"])
    branch_opportunities = int(functional["architectural_branches"])
    true_branch_misses = sum(int(row["n_branch_misses"]) for row in core_rows)
    true_branches = sum(int(row["n_branches"]) for row in core_rows)
    predicted_branch_rate = (
        predicted_branch_misses / max(1, branch_opportunities)
    )
    true_branch_rate = true_branch_misses / max(1, true_branches)
    return {
        "metric_scope": "full_roi" if complete else "bounded_prefix",
        "roi_completion_fraction": retired_macros / max(1, total_macros),
        "predicted_cycles_sum": predicted_cycle_sum,
        "true_cycles_sum": true_cycle_sum,
        "predicted_micro_cpi": predicted_micro_cpi,
        "true_micro_cpi": true_micro_cpi,
        "micro_cpi_abs_relative_error": (
            _abs_relative_error(predicted_micro_cpi, true_micro_cpi)
            if complete and predicted_micro_cpi is not None
            and true_micro_cpi is not None else None
        ),
        "micro_cpi_signed_error": (
            _signed_relative_error(predicted_micro_cpi, true_micro_cpi)
            if complete and predicted_micro_cpi is not None
            and true_micro_cpi is not None else None
        ),
        "predicted_macro_cpi": predicted_macro_cpi,
        "true_macro_cpi": true_macro_cpi,
        "macro_cpi_abs_relative_error": (
            _abs_relative_error(predicted_macro_cpi, true_macro_cpi)
            if complete else None
        ),
        "macro_cpi_signed_error": (
            _signed_relative_error(predicted_macro_cpi, true_macro_cpi)
            if complete else None
        ),
        "predicted_makespan": predicted_makespan,
        "true_makespan": true_makespan,
        "makespan_abs_relative_error": (
            _abs_relative_error(predicted_makespan, true_makespan)
            if complete else None
        ),
        "makespan_signed_error": (
            _signed_relative_error(predicted_makespan, true_makespan)
            if complete else None
        ),
        "core_cycle_mape_mean": (
            sum(core_errors) / len(core_errors) if core_errors else None
        ),
        "core_cycle_mape_p50": (
            statistics.median(core_errors) if core_errors else None
        ),
        "core_cycle_mape_p90": _percentile(core_errors, 0.90),
        "core_cycle_mape_p99": _percentile(core_errors, 0.99),
        "core_cycle_mape_max": max(core_errors) if core_errors else None,
        "core_cycle_signed_bias": (
            sum(core_signed_errors) / len(core_signed_errors)
            if core_signed_errors else None
        ),
        "predicted_branch_misses": predicted_branch_misses,
        "true_branch_misses": true_branch_misses,
        "branch_opportunities": branch_opportunities,
        "true_branches": true_branches,
        "predicted_branch_miss_rate": predicted_branch_rate,
        "true_branch_miss_rate": true_branch_rate,
        "branch_miss_count_abs_relative_error": (
            _abs_relative_error(predicted_branch_misses, true_branch_misses)
            if complete else None
        ),
        "branch_miss_rate_abs_error_pp": (
            100.0 * abs(predicted_branch_rate - true_branch_rate)
            if complete else None
        ),
        "per_core": per_core,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * float(value):.3f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--static-dict", required=True)
    parser.add_argument("--checkpoint", default="")
    parser.add_argument(
        "--max-steps", type=int, default=8,
        help="bounded rollout steps; <=0 runs the complete trace",
    )
    parser.add_argument(
        "--stride-macro", type=int, default=256,
        help="maximum macros advanced per active core and scheduler step",
    )
    parser.add_argument("--max-step-cycles", type=float, default=1024.0)
    parser.add_argument("--device", default="")
    parser.add_argument("--token-cache-root", default="")
    parser.add_argument("--semantic-cache-root", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--trace-index", type=int, default=1)
    parser.add_argument("--trace-count", type=int, default=1)
    parser.add_argument("--split", default="deployment_inference")
    parser.add_argument("--workload-role", default="")
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--gpu-id", default="")
    parser.add_argument("--log-path", default="")
    parser.add_argument(
        "--posthoc-intervention",
        choices=POSTHOC_INTERVENTIONS,
        default="full_real",
    )
    parser.add_argument("--intervention-seed", type=int, default=20260719)
    parser.add_argument("--activation-diagnostics", action="store_true")
    args = parser.parse_args()
    if int(args.progress_every) < 0:
        raise ValueError("--progress-every must be non-negative")

    run_dir = Path(args.run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    final = json.loads((run_dir / "final_report.json").read_text())
    checkpoint = Path(args.checkpoint or final["checkpoint"])
    trace_meta = json.loads((Path(args.trace_root) / "meta.json").read_text())
    workload = str(trace_meta["workload"])
    n_cores = int(trace_meta["n_cores"])
    total_macros = sum(int(row["n_macros"]) for row in trace_meta["cores"])
    target_stride = int(args.stride_macro)
    device = torch.device(
        args.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
    )
    if (
        str(run.get("online_backbone_type", "qwen_lora")) == "qwen_lora"
        and not bool(run["tiny_backbone"])
        and device.type != "cuda"
    ):
        raise RuntimeError("real Qwen checkpoint rollout requires CUDA")

    print("\n" + "=" * 96, flush=True)
    print(
        f"## [{int(args.trace_index)}/{int(args.trace_count)}] {workload} "
        f"c{n_cores:02d} macros={total_macros} K={int(trace_meta['K'])} "
        f"seed={int(args.seed)}",
        flush=True,
    )
    print(
        f"   cache={Path(args.trace_root).resolve()} split={args.split} "
        f"role={args.workload_role or 'unknown'} uops={int(trace_meta['n_uops'])}",
        flush=True,
    )
    print(
        f"   checkpoint={checkpoint} step={int(final.get('steps', 0))} "
        f"device={device} physical_gpu={args.gpu_id or 'default'} "
        f"amp={run.get('dtype', 'unknown')}",
        flush=True,
    )
    print(
        f"   mode=free target_stride={target_stride} "
        f"step_cycles=0..{float(args.max_step_cycles):g} "
        f"max_steps={'full' if int(args.max_steps) <= 0 else int(args.max_steps)}",
        flush=True,
    )
    print(
        f"   semantic_input={run.get('semantic_input_mode')} "
        f"core_mixer={run.get('core_mixer_mode')} "
        f"intervention={args.posthoc_intervention} "
        f"progress_every={int(args.progress_every)}",
        flush=True,
    )
    if args.log_path:
        print(f"   independent_log={Path(args.log_path).resolve()}", flush=True)

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        run["base_model"], local_files_only=not bool(run["allow_download"]),
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    torch.manual_seed(int(run["seed"]))
    model = build_timing_model(Namespace(**run), tokenizer).to(device).eval()
    load_report = load_trainable(
        model,
        checkpoint,
        expected_contract=run["checkpoint_contract"],
    )
    intervention_report = _apply_posthoc_intervention(
        model, args.posthoc_intervention,
    )

    base_resolver = ParquetInstructionResolver(args.static_dict)
    text_variant = text_variant_for(str(run["semantic_variant"]))
    resolver = (
        base_resolver
        if text_variant == "real"
        else SemanticVariantInstructionResolver(base_resolver, text_variant)
    )
    context = PackedTraceMacroContext(args.trace_root)
    token_cache = None
    semantic_cache = None
    input_mode = str(run.get("semantic_input_mode", "native_token"))
    if input_mode == "native_token" and args.token_cache_root:
        token_cache = CachedTokenSource(
            args.token_cache_root, tokenizer, variant=text_variant,
        )
    elif input_mode == "cached_macro_soft_token":
        cache_root = str(
            args.semantic_cache_root or run.get("semantic_cache_root", "")
        )
        if not cache_root:
            raise MacroContractError(
                "semantic checkpoint rollout requires --semantic-cache-root"
            )
        semantic_cache = CachedSemanticSource(
            cache_root,
            fixed_permutation_seed=(
                int(args.intervention_seed)
                if args.posthoc_intervention == "semantic_permute"
                else None
            ),
        )
        for key, value in semantic_cache.contract.items():
            if run["checkpoint_contract"].get(key) != value:
                raise MacroContractError(
                    f"rollout semantic cache contract mismatch at {key}"
                )
    elif input_mode == "learned_null_macro_token":
        if args.token_cache_root or args.semantic_cache_root:
            raise MacroContractError(
                "learned-null E rollout must not receive token/semantic cache"
            )
    else:
        raise MacroContractError(f"unsupported semantic input mode {input_mode}")
    predictor = MacroV29ModelPredictor(
        context,
        resolver,
        tokenizer,
        model,
        device=device,
        max_tokens=int(run["max_tokens"]),
        token_cache=token_cache,
        semantic_cache=semantic_cache,
        parquet_path=str(Path(args.static_dict).resolve()),
        activation_diagnostic_forwards=(
            1 if args.activation_diagnostics else 0
        ),
    )

    def emit_progress(event: Mapping[str, Any]) -> None:
        elapsed = float(event["elapsed_s"])
        retired = int(event["retired_macros"])
        total = int(event["total_macros"])
        percentage = 100.0 * retired / max(1, total)
        print(
            f"   [{workload} c{n_cores:02d}] window={int(event['step'])} "
            f"progress={percentage:.1f}% macros={retired}/{total} "
            f"macro/s={float(event['macro_per_s']):.1f}",
            flush=True,
        )
        print(
            f"      active={int(event['active_cores'])} "
            f"forwards={int(event['model_forwards'])} "
            f"useful_macros/step={float(event['retired_macros_per_step']):.1f} "
            f"global/delta={float(event['global_time_cycles']):.1f}/"
            f"{float(event['delta_cycles']):.1f} "
            f"avg_step={float(event['mean_step_ms']):.2f}ms wall={elapsed:.1f}s",
            flush=True,
        )
        timing = event.get("predictor_timing")
        if isinstance(timing, Mapping):
            mean_ms = timing.get("mean_ms", {})
            if isinstance(mean_ms, Mapping):
                print(
                    "      timing avg ms/forward "
                    "predict/context/collate/model/output = "
                    f"{float(mean_ms.get('predict_total', 0.0)):.2f}/"
                    f"{float(mean_ms.get('context', 0.0)):.2f}/"
                    f"{float(mean_ms.get('collate', 0.0)):.2f}/"
                    f"{float(mean_ms.get('online_model', 0.0)):.2f}/"
                    f"{float(mean_ms.get('output_copy', 0.0)):.2f}",
                    flush=True,
                )

    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    rollout = model_free_rollout(
        context,
        predictor,
        target_stride_macro=target_stride,
        max_step_cycles=float(args.max_step_cycles),
        max_steps=(None if int(args.max_steps) <= 0 else int(args.max_steps)),
        progress_interval=int(args.progress_every),
        progress=emit_progress,
    )
    if device.type == "cuda":
        rollout["gpu_peak_allocated_bytes"] = int(
            torch.cuda.max_memory_allocated(device)
        )
        rollout["gpu_peak_reserved_bytes"] = int(
            torch.cuda.max_memory_reserved(device)
        )
    else:
        rollout["gpu_peak_allocated_bytes"] = 0
        rollout["gpu_peak_reserved_bytes"] = 0
    metrics = _deployment_metrics(context, rollout)
    failures = []
    if rollout["free_context_label_keys"]:
        failures.append("checkpoint rollout exposed labels")
    if int(rollout["total_consumed_macros"]) <= 0:
        failures.append("checkpoint rollout made no macro progress")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "schema_version": "llmsim-macro-v29-deployment-trace-1",
        "contract": "macro-v29-checkpoint-free-rollout-v1",
        "trace_id": str(trace_meta["trace_id"]),
        "workload": workload,
        "workload_role": str(args.workload_role),
        "seed": int(args.seed),
        "n_cores": n_cores,
        "cache_dir": str(Path(args.trace_root).resolve()),
        "source_split": str(args.split),
        "run_dir": str(run_dir),
        "checkpoint": str(checkpoint),
        "checkpoint_step": int(final.get("steps", 0)),
        "semantic_variant": str(run["semantic_variant"]),
        "text_variant": text_variant,
        "semantic_input_mode": input_mode,
        "posthoc_intervention": str(args.posthoc_intervention),
        "posthoc_intervention_seed": int(args.intervention_seed),
        "posthoc_intervention_report": intervention_report,
        "semantic_cache_intervention": (
            semantic_cache.intervention_report
            if semantic_cache is not None else None
        ),
        "semantic_cache_manifest_hash": (
            semantic_cache.manifest_hash if semantic_cache is not None else None
        ),
        "device": str(device),
        "evaluation_contract": {
            "mode": "free",
            "target_stride_macro": target_stride,
            "max_step_cycles": float(args.max_step_cycles),
            "max_steps": int(args.max_steps),
            "progress_every": int(args.progress_every),
            "model_context_uses_oracle_timing": False,
            "predicted_context_used_for_labels": False,
            "throughput_headline_unit": "macro/s",
            "activation_diagnostics": bool(args.activation_diagnostics),
            "activation_diagnostic_forwards_per_trace": (
                1 if args.activation_diagnostics else 0
            ),
            "activation_diagnostic_scope": (
                "first_deployment_window"
                if args.activation_diagnostics else None
            ),
        },
        "checkpoint_load": load_report,
        "rollout": rollout,
        "free_running": rollout,
        "deployment_metrics": metrics,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    destination = None
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n")
    else:
        print(rendered, flush=True)

    timing = rollout.get("predictor_timing") or {}
    mean_ms = timing.get("mean_ms", {}) if isinstance(timing, Mapping) else {}
    print(f"\n## {workload} c{n_cores:02d} complete", flush=True)
    print(
        f"[result] complete={rollout['complete']} "
        f"scope={metrics['metric_scope']} "
        f"macro-CPI={metrics['predicted_macro_cpi']:.6f}/"
        f"{metrics['true_macro_cpi']:.6f} "
        f"error={_pct(metrics['macro_cpi_abs_relative_error'])} "
        f"macro/s={rollout['aggregate_macro_per_s']:.1f}",
        flush=True,
    )
    print(
        f"  rollout complete={rollout['complete']} scope={metrics['metric_scope']} "
        f"steps={rollout['steps']} global_cycles={rollout['global_time_cycles']:.3f} "
        f"macros={rollout['total_consumed_macros']}/"
        f"{rollout['total_available_macros']} "
        f"uops={rollout['total_consumed_uops']}/"
        f"{rollout['total_available_uops']}",
        flush=True,
    )
    print(
        "  macro CPI pred/label = "
        f"{metrics['predicted_macro_cpi']:.6f} / "
        f"{metrics['true_macro_cpi']:.6f}; relative error = "
        f"{_pct(metrics['macro_cpi_abs_relative_error'])}; "
        f"rollout coverage = {100.0 * metrics['roi_completion_fraction']:.2f}%",
        flush=True,
    )
    print(
        "  cycles pred/label = "
        f"{metrics['predicted_cycles_sum']:.3f} / "
        f"{metrics['true_cycles_sum']:.3f}; makespan pred/label/error = "
        f"{metrics['predicted_makespan']:.3f} / "
        f"{metrics['true_makespan']:.3f} / "
        f"{_pct(metrics['makespan_abs_relative_error'])}",
        flush=True,
    )
    print(
        "  per-core cycle error mean/p50/max = "
        f"{_pct(metrics['core_cycle_mape_mean'])} / "
        f"{_pct(metrics['core_cycle_mape_p50'])} / "
        f"{_pct(metrics['core_cycle_mape_max'])}",
        flush=True,
    )
    print(
        "  branch miss count pred/label = "
        f"{metrics['predicted_branch_misses']:.3f} / "
        f"{metrics['true_branch_misses']}; retired branches = "
        f"{metrics['branch_opportunities']}; rate pred/label = "
        f"{100.0 * metrics['predicted_branch_miss_rate']:.3f}% / "
        f"{100.0 * metrics['true_branch_miss_rate']:.3f}%; abs delta = "
        f"{metrics['branch_miss_rate_abs_error_pp'] if metrics['branch_miss_rate_abs_error_pp'] is not None else float('nan'):.3f} "
        "percentage-points",
        flush=True,
    )
    print(
        "  scheduler steps/forwards/target-stride/macros-per-forward = "
        f"{rollout['steps']} / {int(timing.get('calls', rollout['steps']))} / "
        f"{target_stride} / {rollout['retired_macros_per_model_forward']:.1f}; "
        f"capped/zero-core-rows={rollout['capped_steps']}/"
        f"{rollout['zero_core_rows']}",
        flush=True,
    )
    print(
        f"  throughput macro/s={rollout['aggregate_macro_per_s']:.1f} "
        f"windows/s={rollout['steps_per_s']:.3f}; "
        f"avg step={rollout['mean_step_ms']:.2f}ms; "
        f"wall={rollout['elapsed_s']:.3f}s; GPU peak alloc/reserved="
        f"{rollout['gpu_peak_allocated_bytes'] / (1024 ** 3):.2f}/"
        f"{rollout['gpu_peak_reserved_bytes'] / (1024 ** 3):.2f} GiB",
        flush=True,
    )
    print(
        "  timing avg ms/forward predict/context/collate/model/output/scheduler = "
        f"{float(mean_ms.get('predict_total', 0.0)):.2f} / "
        f"{float(mean_ms.get('context', 0.0)):.2f} / "
        f"{float(mean_ms.get('collate', 0.0)):.2f} / "
        f"{float(mean_ms.get('online_model', 0.0)):.2f} / "
        f"{float(mean_ms.get('output_copy', 0.0)):.2f} / "
        f"{1000.0 * rollout['scheduler_seconds'] / max(1, rollout['steps']):.2f}",
        flush=True,
    )
    for row in metrics["per_core"]:
        print(
            f"[core] id={row['core_id']} macros={row['retired_macros']}/"
            f"{row['true_macros']} cycles={row['predicted_cycles']:.3f}/"
            f"{row['true_cycles']:.3f} "
            f"cycle_error={_pct(row['cycle_abs_relative_error'])}",
            flush=True,
        )
    if destination is not None:
        print(f"[persisted] trace_result={destination.resolve()}", flush=True)
    print("=" * 96, flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
