#!/usr/bin/env python3
"""Compare scope-locked FastSim reports with a gem5 kernel-events oracle.

The ``user`` report supplies user-only CPI/PMU.  The ``user-plus-kernel``
report supplies combined CPI/PMU.  A single report is insufficient because synthetic
kernel service can overlap pre-existing pipeline stalls, so subtracting its
raw active-cycle counter from the final timeline is not overlap-safe.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from validate_kernel_events_oracle import validate_document


FORMAL_ORACLE_SCHEMA = "tcsim-gem5-fs-kernel-events-v3"
LEGACY_ORACLE_SCHEMA = "tcsim-gem5-fs-kernel-events-v2"
FASTSIM_SCHEMA = "fastsim-stats-v5"
PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"
EVENT_DICTIONARY = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "pmu-event-dictionary-v1.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle", type=Path)
    parser.add_argument("user_report", type=Path)
    parser.add_argument("user_plus_kernel_report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-legacy-oracle",
        action="store_true",
        help="Allow a diagnostic comparison without exact class PMU truth.",
    )
    return parser.parse_args()


def error_row(predicted: float, reference: float) -> dict:
    signed = predicted - reference
    if reference:
        relative = signed / reference
    elif predicted == 0:
        relative = 0.0
    else:
        relative = None
    return {
        "predicted": predicted,
        "reference": reference,
        "signed_error": signed,
        "absolute_error": abs(signed),
        "relative_error": relative,
        "absolute_percentage_error": (
            abs(relative) * 100.0 if relative is not None else None
        ),
    }


def scoped_metrics(report: dict, path: Path, expected_scope: str) -> dict:
    if report.get("schema") != FASTSIM_SCHEMA:
        raise SystemExit(f"{path}: expected schema {FASTSIM_SCHEMA}")
    actual_scope = report.get("measurement_scope")
    if actual_scope != expected_scope:
        raise SystemExit(
            f"{path}: expected measurement_scope={expected_scope}, "
            f"found {actual_scope!r}"
        )
    metrics = report.get("scope_metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("pmu"), dict):
        raise SystemExit(f"{path}: missing canonical scope_metrics/pmu")
    return metrics


def pmu_event_status() -> dict[str, dict]:
    dictionary = json.loads(EVENT_DICTIONARY.read_text(encoding="utf-8"))
    if dictionary.get("contract_id") != PMU_CONTRACT_ID:
        raise SystemExit(f"{EVENT_DICTIONARY}: incompatible PMU contract")
    status = {}
    for event_name, event in dictionary.get("events", {}).items():
        field = event.get("report_field")
        if not field:
            continue
        if field in status:
            raise SystemExit(
                f"{EVENT_DICTIONARY}: duplicate report_field {field!r}"
            )
        mapping = event.get("mapping")
        if mapping not in {"strict", "proxy", "diagnostic", "unavailable"}:
            raise SystemExit(
                f"{EVENT_DICTIONARY}: invalid mapping for {event_name}"
            )
        status[field] = {
            "event": event_name,
            "mapping": mapping,
            "formal_event_eligible": mapping == "strict",
        }
    return status


def main() -> int:
    args = parse_args()
    oracle = json.loads(args.oracle.read_text())
    user_report = json.loads(args.user_report.read_text())
    combined_report = json.loads(args.user_plus_kernel_report.read_text())
    try:
        validation = validate_document(oracle, 0.0)
    except ValueError as exc:
        raise SystemExit(f"invalid kernel-events oracle: {exc}")
    reference = oracle["aggregate"]
    exact_oracle = bool(validation["formal_pmu_eligible"])
    if not exact_oracle and not args.allow_legacy_oracle:
        raise SystemExit(
            "formal comparison requires a kernel-events-v3 oracle with "
            "exactly-once memory coverage; use --allow-legacy-oracle only "
            "for diagnostics"
        )
    user_metrics = scoped_metrics(user_report, args.user_report, "user")
    combined_metrics = scoped_metrics(
        combined_report,
        args.user_plus_kernel_report,
        "user-plus-kernel",
    )
    if exact_oracle:
        for scope, metrics in (
            ("user", user_metrics),
            ("user_plus_kernel", combined_metrics),
        ):
            if metrics.get("pmu_contract_id") != PMU_CONTRACT_ID:
                raise SystemExit(
                    f"{scope} report has incompatible PMU contract "
                    f"{metrics.get('pmu_contract_id')!r}"
                )
        if user_metrics.get("kernel_profile_contract") != "not-applicable":
            raise SystemExit("user report has an unexpected kernel profile")
        if (
            combined_metrics.get("kernel_profile_contract")
            != "p0-22-field"
        ):
            raise SystemExit(
                "formal user+kernel PMU comparison requires a 22-field "
                "P0 kernel profile"
            )
    combined_totals = combined_report["totals"]

    n_user = int(reference["n_user"])
    for scope, metrics in (
        ("user", user_metrics),
        ("user_plus_kernel", combined_metrics),
    ):
        # `retired_uops` is the replay/denominator count. The PMU object also
        # restores gem5/x86's fixed user-decoded syscall transition footprint
        # that is collapsed into one functional marker.
        traced_uops = int(metrics["user_trace_uops"])
        if traced_uops != n_user:
            raise SystemExit(
                f"{scope} report has {traced_uops} user uops; oracle has {n_user}"
            )
    if exact_oracle:
        expected_user_instructions = int(reference["user_retired_instructions"])
        for scope, metrics in (
            ("user", user_metrics),
            ("user_plus_kernel", combined_metrics),
        ):
            traced_instructions = int(metrics["user_trace_instructions"])
            if traced_instructions != expected_user_instructions:
                raise SystemExit(
                    f"{scope} report has {traced_instructions} user macro "
                    f"instructions; oracle has {expected_user_instructions}"
                )
    if int(user_metrics["synthetic_kernel_active_cycles"]) != 0:
        raise SystemExit("user report must have zero synthetic active cycles")

    predicted_user_cycles = int(user_metrics["sum_core_cycles"])
    predicted_combined_cycles = int(combined_metrics["sum_core_cycles"])
    predicted_user_cpi = float(user_metrics["cycles_per_user_uop"])
    predicted_combined_cpi = float(
        combined_metrics["cycles_per_user_uop"]
    )
    for scope, reported, cycles in (
        ("user", predicted_user_cpi, predicted_user_cycles),
        ("user_plus_kernel", predicted_combined_cpi, predicted_combined_cycles),
    ):
        derived = cycles / n_user if n_user else 0.0
        if abs(reported - derived) > 1e-9 * max(1.0, abs(derived)):
            raise SystemExit(
                f"{scope} cycles_per_user_uop is internally inconsistent"
            )

    scopes = {
        "user": error_row(
            predicted_user_cpi,
            reference.get("cycles_per_user_uop_user", reference["cpi_user"]),
        ),
        "user_plus_kernel": error_row(
            predicted_combined_cpi,
            reference.get(
                "cycles_per_user_uop_user_plus_kernel",
                reference["cpi_user_plus_kernel"],
            ),
        ),
    }
    perf_like_cpi = None
    if exact_oracle:
        perf_like_cpi = {
            "user": error_row(
                float(user_metrics["perf_like_cpi"]),
                float(reference["perf_like_cpi_user"]),
            ),
            "user_plus_kernel": error_row(
                float(combined_metrics["perf_like_cpi"]),
                float(reference["perf_like_cpi_user_plus_kernel"]),
            ),
            "status": {
                "user": user_metrics.get("perf_like_cpi_status"),
                "user_plus_kernel": combined_metrics.get(
                    "perf_like_cpi_status"
                ),
            },
        }
    predicted_pmu = {
        "user": user_metrics["pmu"],
        "user_plus_kernel": combined_metrics["pmu"],
    }
    reference_pmu = {
        "user": reference["pmu_user"],
        "user_plus_kernel": reference["pmu_user_plus_kernel"],
    }
    event_status = pmu_event_status()
    if exact_oracle:
        compared_fields = sorted(
            field
            for field, status in event_status.items()
            if status["mapping"] != "unavailable"
        )
    else:
        compared_fields = sorted(
            set(reference_pmu["user"])
            & set(reference_pmu["user_plus_kernel"])
        )
        event_status = {
            field: {
                "event": "legacy-unversioned",
                "mapping": "diagnostic",
                "formal_event_eligible": False,
            }
            for field in compared_fields
        }
    pmu = {}
    for scope in ("user", "user_plus_kernel"):
        missing = set(compared_fields) - set(reference_pmu[scope])
        missing |= set(compared_fields) - set(predicted_pmu[scope])
        if missing:
            raise SystemExit(
                f"{scope} report/oracle lacks contract PMU fields: "
                f"{sorted(missing)}"
            )
        pmu[scope] = {
            field: error_row(
                int(predicted_pmu[scope][field]),
                int(reference_pmu[scope][field]),
            )
            for field in compared_fields
        }

    cycle_components = {
        "syscall_kernel_cycles": error_row(
            int(combined_totals["synthetic_syscall_kernel"]["active_cycles"]),
            int(reference["syscall_kernel_cycles"]),
        ),
        "page_fault_kernel_cycles": error_row(
            int(
                combined_totals["synthetic_page_fault_kernel"]["active_cycles"]
            ),
            int(reference["page_fault_kernel_cycles"]),
        ),
        "irq_kernel_cycles": error_row(
            int(combined_totals["synthetic_irq_kernel"]["active_cycles"]),
            int(reference["irq_kernel_cycles"]),
        ),
        # FastSim does not yet infer scheduler entries from a user-only trace.
        # Keep the residual visible instead of folding it into IRQ/syscall cost.
        "scheduler_kernel_cycles": error_row(
            0, int(reference["scheduler_kernel_cycles"])
        ),
        "unknown_kernel_cycles": error_row(
            0, int(reference["unknown_kernel_cycles"])
        ),
        # Idle is deliberately outside both CPI numerators.
        "idle_cycles": error_row(0, int(reference["idle_cycles"])),
    }
    event_counts = {
        name: error_row(
            int(combined_totals[counter]["events"]),
            int(reference.get("event_counts", {}).get(name, 0)),
        )
        for name, counter in (
            ("syscall", "synthetic_syscall_kernel"),
            ("page_fault", "synthetic_page_fault_kernel"),
            ("irq", "synthetic_irq_kernel"),
        )
    }
    payload = {
        "schema": "fastsim-kernel-event-accuracy-v3",
        "oracle": str(args.oracle.resolve()),
        "fastsim_reports": {
            "user": str(args.user_report.resolve()),
            "user_plus_kernel": str(
                args.user_plus_kernel_report.resolve()
            ),
        },
        "n_user": n_user,
        "retired_instruction_denominators": {
            "user": int(reference.get("user_retired_instructions", 0)),
            "user_plus_kernel": int(
                reference.get("user_plus_kernel_retired_instructions", 0)
            ),
        },
        "formal_oracle_eligible": exact_oracle,
        "formal_accounting_eligible": exact_oracle,
        "pmu_source": reference.get("pmu_source"),
        "pmu_contract_id": reference.get("pmu_contract_id"),
        "pmu_event_status": event_status,
        "pmu_excluded_unavailable_fields": sorted(
            field
            for field, status in event_status.items()
            if status["mapping"] == "unavailable"
        ),
        "cycles_per_user_uop": scopes,
        "perf_like_cpi": perf_like_cpi,
        "kernel_cycle_components": cycle_components,
        "kernel_event_counts": event_counts,
        "pmu": pmu,
        "throughput": {
            "user": user_metrics.get("throughput", {}),
            "user_plus_kernel": combined_metrics.get("throughput", {}),
        },
        "wall_time_seconds": {
            "user": user_report.get("wall_time_seconds"),
            "user_plus_kernel": combined_report.get("wall_time_seconds"),
        },
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
