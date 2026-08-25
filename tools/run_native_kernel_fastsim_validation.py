#!/usr/bin/env python3
"""Replay native-kernel FST cases and score cache/branch miss diagnostics.

Branch-miss truth comes from the original gem5 BPred redirect outcome carried
to retirement in kernel_events.json.  Cache-miss truth comes only from the
drained native Ruby/SLICC sideband.  The legacy retirement-time DynInst branch
comparison and TaoTrace path-class cache fields are deliberately never scored.
"""

import argparse
import csv
import hashlib
import json
import math
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from audit_p1_native_response_sideband import audit_result as audit_native_result
from validate_kernel_events_oracle import BRANCH_MISS_SOURCE


CASE_SCHEMA = "fastsim-native-kernel-validation-case-v2"
SUMMARY_SCHEMA = "fastsim-native-kernel-validation-summary-v2"
PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"
CACHE_ORACLE_SOURCE = "taotrace-path-class-v3"
EVENT_DICTIONARY = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "pmu-event-dictionary-v1.json"
)
NATIVE_AUDITOR = Path(__file__).resolve().with_name(
    "audit_p1_native_response_sideband.py"
)
ORACLE_VALIDATOR = Path(__file__).resolve().with_name(
    "validate_kernel_events_oracle.py"
)
PMU_EVENT_NAMES = {
    "branch_misses": "retired_branch_misses",
    "l1d_tag_misses": "l1d_tag_misses",
    "private_l2_tag_misses": "private_l2_tag_misses",
    "llc_tag_misses": "llc_tag_misses",
}
EXPECTED_PMU_MAPPINGS = {
    "branch_misses": "proxy",
    "l1d_tag_misses": "proxy",
    "private_l2_tag_misses": "proxy",
    "llc_tag_misses": "diagnostic",
}
CACHE_LEVEL_BY_FIELD = {
    "l1d_tag_misses": "l1d",
    "private_l2_tag_misses": "l2",
    "llc_tag_misses": "llc",
}
PMU_FIELDS = tuple(PMU_EVENT_NAMES)
SYSCALL_TRANSITION_EXTRA_USER_UOPS = 24


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    return json.loads(path.read_text())


def semantic_sha256(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=None)
def _sha256_file(path_text, size, mtime_ns):
    del size, mtime_ns  # They form the cache key and deliberately remain unused.
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_file(path):
    path = path.resolve()
    stat = path.stat()
    return _sha256_file(str(path), stat.st_size, stat.st_mtime_ns)


def config_include_chain(path):
    """Return the selected config followed by its transitive base configs.

    FastSim permits one ``config.include`` per file, resolved relative to the
    containing file.  The validation fingerprint must therefore cover every
    transitive input, not just the leaf config passed on the command line.
    """
    chain = []
    active = set()
    current = Path(path).resolve()
    while True:
        identity = str(current)
        if identity in active:
            raise ValueError(f"cyclic config.include involving: {identity}")
        active.add(identity)
        chain.append(current)

        include_value = None
        for raw_line in current.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not key or not value:
                continue
            if len(value) >= 2 and (
                (value[0] == '"' and value[-1] == '"')
                or (value[0] == "'" and value[-1] == "'")
            ):
                value = value[1:-1]
            if key == "config.include":
                include_value = value

        if include_value is None:
            return chain
        included = Path(include_value)
        if not included.is_absolute():
            included = current.parent / included
        current = included.resolve()


def load_pmu_event_contract(path):
    document = load_json(path)
    if document.get("contract_id") != PMU_CONTRACT_ID:
        raise ValueError(
            f"{path}: PMU contract must be {PMU_CONTRACT_ID!r}"
        )
    events = document.get("events")
    if not isinstance(events, dict):
        raise ValueError(f"{path}: events must be an object")
    selected = {}
    for field, event_name in PMU_EVENT_NAMES.items():
        event = events.get(event_name)
        if not isinstance(event, dict):
            raise ValueError(f"{path}: missing PMU event {event_name!r}")
        if event.get("report_field") != field:
            raise ValueError(
                f"{path}: {event_name}.report_field must be {field!r}"
            )
        expected_mapping = EXPECTED_PMU_MAPPINGS[field]
        if event.get("mapping") != expected_mapping:
            raise ValueError(
                f"{path}: {event_name}.mapping must be {expected_mapping!r}"
            )
        gem5_semantics = str(event.get("gem5", ""))
        if field == "branch_misses" and (
            "persistent original BPred redirect bit" not in gem5_semantics
            or event.get("gem5_oracle_source") != BRANCH_MISS_SOURCE
        ):
            raise ValueError(
                f"{path}: {event_name} is not bound to persistent retired "
                "BPred truth"
            )
        if field in CACHE_LEVEL_BY_FIELD and "native v5:" not in gem5_semantics:
            raise ValueError(
                f"{path}: {event_name} is not bound to native SLICC truth"
            )
        selected[field] = {
            "event": event_name,
            "mapping": expected_mapping,
            "unit": event.get("unit"),
            "gem5_semantics": gem5_semantics,
            "fastsim_semantics": event.get("fastsim"),
        }
    return selected


def validation_fingerprint(case, args):
    result_dir = Path(case["result_dir"])
    oracle_dir = result_dir / "oracle"
    native_summaries = sorted(oracle_dir.glob("native-summary-core*.json"))
    actual_summaries = {path.name for path in native_summaries}
    expected_summaries = {
        f"native-summary-core{core}.json" for core in range(int(case["cores"]))
    }
    if actual_summaries != expected_summaries:
        raise ValueError(
            f"{result_dir}: native summaries must be exactly "
            f"{sorted(expected_summaries)}, found {sorted(actual_summaries)}"
        )
    paths = {
        "fastsim": args.fastsim,
        "event_dictionary": args.event_dictionary,
        "validator": Path(__file__),
        "native_auditor": NATIVE_AUDITOR,
        "oracle_validator": ORACLE_VALIDATOR,
        "request": result_dir / "request.json",
        "trace": result_dir / "tao_trace/trace.json",
        "manifest": result_dir / "tao_trace/manifest.txt",
        "kernel_events": oracle_dir / "kernel_events.json",
    }
    for depth, config_path in enumerate(config_include_chain(args.config)):
        name = "config" if depth == 0 else f"config_include/{depth:04d}"
        paths[name] = config_path
    for summary in native_summaries:
        paths[f"native_summary/{summary.name}"] = summary
    inputs = {
        name: {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
        }
        for name, path in sorted(paths.items())
    }
    payload = {
        "case_schema": CASE_SCHEMA,
        "measurement_scope": "user-plus-kernel",
        "cores": int(case["cores"]),
        "inputs": inputs,
    }
    return {"sha256": semantic_sha256(payload), **payload}


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def relative_error(predicted, reference):
    if predicted is None or reference is None:
        return None
    predicted = float(predicted)
    reference = float(reference)
    if reference == 0:
        return 0.0 if predicted == 0 else None
    return (predicted - reference) / reference


def error_row(predicted, reference, contract, reference_source):
    if (
        isinstance(predicted, bool)
        or not isinstance(predicted, int)
        or predicted < 0
    ):
        raise ValueError(f"FastSim {contract['event']} must be a nonnegative integer")
    if (
        isinstance(reference, bool)
        or not isinstance(reference, int)
        or reference < 0
    ):
        raise ValueError(f"gem5 {contract['event']} must be a nonnegative integer")
    signed = predicted - reference
    relative = relative_error(predicted, reference)
    return {
        "fastsim": predicted,
        "gem5": reference,
        "predicted": predicted,
        "reference": reference,
        "signed_error": signed,
        "absolute_error": abs(signed),
        "relative_error": relative,
        "absolute_relative_error": (
            abs(relative) if relative is not None else None
        ),
        "absolute_percentage_error": (
            abs(relative) * 100.0 if relative is not None else None
        ),
        "event": contract["event"],
        "mapping": contract["mapping"],
        "unit": contract["unit"],
        "reference_source": reference_source,
        "gem5_semantics": contract["gem5_semantics"],
        "fastsim_semantics": contract["fastsim_semantics"],
    }


def build_pmu_comparison(fastsim_pmu, oracle_aggregate, native_audit, contract):
    if oracle_aggregate.get("pmu_source") != CACHE_ORACLE_SOURCE:
        raise ValueError(
            "cache reference requires taotrace-path-class-v3"
        )
    if oracle_aggregate.get("pmu_contract_id") != PMU_CONTRACT_ID:
        raise ValueError("oracle PMU contract is incompatible")
    if oracle_aggregate.get("branch_miss_source") != BRANCH_MISS_SOURCE:
        raise ValueError(
            "branch-miss reference requires " + BRANCH_MISS_SOURCE
        )
    if not native_audit.get("native_hierarchy_semantic_comparable", False):
        raise ValueError(
            "cache-miss reference requires a fully conserved native SLICC hierarchy"
        )
    native_scope = native_audit.get("scope_metrics", {}).get(
        "user_plus_kernel", {}
    ).get("native_ruby_pmu")
    if not isinstance(native_scope, dict):
        raise ValueError("native audit lacks user-plus-kernel Ruby PMU")
    hierarchy = native_scope.get("hierarchy")
    if not isinstance(hierarchy, dict):
        raise ValueError("native audit lacks Ruby hierarchy populations")
    oracle_pmu = oracle_aggregate.get("pmu_user_plus_kernel")
    if not isinstance(oracle_pmu, dict):
        raise ValueError("oracle lacks user-plus-kernel PMU")

    comparison = {
        "branch_misses": error_row(
            fastsim_pmu.get("branch_misses"),
            oracle_pmu.get("branch_misses"),
            contract["branch_misses"],
            f"kernel_events-v3:{BRANCH_MISS_SOURCE}",
        )
    }
    for field, level in CACHE_LEVEL_BY_FIELD.items():
        level_population = hierarchy.get(level)
        if not isinstance(level_population, dict):
            raise ValueError(f"native audit lacks {level} hierarchy population")
        comparison[field] = error_row(
            fastsim_pmu.get(field),
            level_population.get("tag_misses"),
            contract[field],
            f"native-ruby-slicc:{level}.tag_misses",
        )
    return comparison


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def aggregate_pmu_accuracy(reports):
    scored = [
        report
        for report in reports
        if report.get("status") == "passed"
        and report.get("pmu_validation", {}).get("status") == "scored"
    ]
    aggregate = {}
    for field in PMU_FIELDS:
        rows = [report["pmu"][field] for report in scored]
        apes = [
            float(row["absolute_percentage_error"])
            for row in rows
            if row.get("absolute_percentage_error") is not None
            and math.isfinite(float(row["absolute_percentage_error"]))
        ]
        reference_total = sum(int(row["reference"]) for row in rows)
        predicted_total = sum(int(row["predicted"]) for row in rows)
        absolute_error_total = sum(int(row["absolute_error"]) for row in rows)
        sources = sorted({row["reference_source"] for row in rows})
        mappings = sorted({row["mapping"] for row in rows})
        if len(sources) > 1 or len(mappings) > 1:
            raise ValueError(f"inconsistent PMU contract for {field}")
        aggregate[field] = {
            "cases": len(rows),
            "finite_ape_cases": len(apes),
            "mape_percent": sum(apes) / len(apes) if apes else None,
            "p50_ape_percent": percentile(apes, 0.50),
            "p90_ape_percent": percentile(apes, 0.90),
            "p99_ape_percent": percentile(apes, 0.99),
            "max_ape_percent": max(apes) if apes else None,
            "wape_percent": (
                absolute_error_total / reference_total * 100.0
                if reference_total
                else None
            ),
            "signed_bias_percent": (
                (predicted_total - reference_total) / reference_total * 100.0
                if reference_total
                else None
            ),
            "predicted_total": predicted_total,
            "reference_total": reference_total,
            "absolute_error_total": absolute_error_total,
            "mapping": mappings[0] if mappings else EXPECTED_PMU_MAPPINGS[field],
            "reference_source": sources[0] if sources else None,
        }
    return aggregate


def percent_text(value):
    return "N/A" if value is None else f"{value:.6f}%"


def discover_cases(matrices):
    cases = {}
    for matrix in matrices:
        try:
            status = load_json(matrix / "status.json")
        except (OSError, json.JSONDecodeError):
            continue
        for key, task in status.get("tasks", {}).items():
            sample = task.get("sample", {})
            completed = sample.get("status") == "completed"
            reused = (
                sample.get("status") == "skipped"
                and sample.get("result_dir")
            )
            if not (completed or reused):
                continue
            result_text = sample.get("result_dir")
            if not result_text:
                continue
            core_text, workload = key.split("/", 1)
            cores = int(core_text[:-1] if core_text.endswith("c") else core_text)
            case_id = f"{cores:02d}c-{workload}"
            cases[case_id] = {
                "case_id": case_id,
                "cores": cores,
                "workload": workload,
                "matrix": str(matrix.resolve()),
                "result_dir": str(Path(result_text).resolve()),
            }
    return [cases[key] for key in sorted(cases)]


def validate_input(case):
    result_dir = Path(case["result_dir"])
    request = load_json(result_dir / "request.json")
    trace = load_json(result_dir / "tao_trace/trace.json")
    sampling = request.get("sampling", {})
    errors = []
    if sampling.get("functional_include_kernel") is not True:
        errors.append("request is not functional_include_kernel")
    if sampling.get("functional_user_only") is True:
        errors.append("request also enables functional_user_only")
    if trace.get("trace_scope") != "user-plus-kernel":
        errors.append("trace_scope is not user-plus-kernel")
    if len(trace.get("functional_boundaries", {})) != case["cores"]:
        errors.append("functional boundary core count mismatch")
    manifest = result_dir / "tao_trace/manifest.txt"
    if not manifest.is_file():
        errors.append("manifest is missing")
    if errors:
        raise ValueError("; ".join(errors))
    return trace, manifest


def run_case(case, args):
    started = utc_now()
    case_dir = args.output_dir / "cases" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    report_path = case_dir / "validation.json"
    report = {
        **case,
        "schema": CASE_SCHEMA,
        "started_at_utc": started,
        "attempts": 1,
        "status": "failed",
        "errors": [],
    }
    try:
        trace, manifest = validate_input(case)
        fingerprint = validation_fingerprint(case, args)
        report["validation_fingerprint"] = fingerprint
        if report_path.is_file():
            try:
                previous = load_json(report_path)
                if (
                    previous.get("schema") == CASE_SCHEMA
                    and previous.get("result_dir") == case["result_dir"]
                    and previous.get("validation_fingerprint", {}).get("sha256")
                    == fingerprint["sha256"]
                ):
                    report["attempts"] = int(previous.get("attempts", 0)) + 1
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        event_contract = load_pmu_event_contract(args.event_dictionary)
        output_path = case_dir / "fastsim.json"
        command = [
            str(args.fastsim),
            "simulate",
            "--config",
            str(args.config),
            "--manifest",
            str(manifest),
            "--measurement-scope",
            "user-plus-kernel",
            "--cores",
            str(case["cores"]),
            "--output",
            str(output_path),
        ]
        process = subprocess.run(
            command,
            cwd=args.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        (case_dir / "fastsim.log").write_text(
            "command=" + " ".join(command) + "\n" + process.stdout
        )
        report["return_code"] = process.returncode
        if process.returncode:
            raise RuntimeError(f"FastSim exited with {process.returncode}")

        simulation = load_json(output_path)
        result_dir = Path(case["result_dir"])
        oracle = load_json(result_dir / "oracle/kernel_events.json")
        native_audit = audit_native_result(result_dir)
        scope = simulation["scope_metrics"]
        totals = simulation["totals"]
        configuration = simulation["configuration"]
        native = totals["native_kernel_trace"]
        pmu = scope["pmu"]
        boundaries = trace["functional_boundaries"].values()
        trace_user_uops = sum(
            int(row["measurement_user_records"]) for row in boundaries
        )
        boundaries = trace["functional_boundaries"].values()
        trace_kernel_uops = sum(
            int(row["measurement_records"])
            - int(row["measurement_user_records"])
            for row in boundaries
        )
        boundaries = trace["functional_boundaries"].values()
        trace_user_instructions = sum(
            int(row["measurement_user_instructions"]) for row in boundaries
        )
        boundaries = trace["functional_boundaries"].values()
        trace_kernel_instructions = sum(
            int(row["measurement_instructions"])
            - int(row["measurement_user_instructions"])
            for row in boundaries
        )

        errors = report["errors"]
        checks = {
            "measurement_scope": simulation.get("measurement_scope")
            == "user-plus-kernel",
            "native_mode": configuration.get("native_kernel_trace") is True,
            "pmu_source": scope.get("pmu_source")
            == "fastsim-functional-native-user-plus-kernel-v1",
            "pmu_contract": scope.get("pmu_contract_id") == PMU_CONTRACT_ID,
            "cache_oracle_source": oracle.get("aggregate", {}).get(
                "pmu_source"
            )
            == CACHE_ORACLE_SOURCE,
            "branch_oracle_source": oracle.get("aggregate", {}).get(
                "branch_miss_source"
            )
            == BRANCH_MISS_SOURCE,
            "oracle_pmu_contract": oracle.get("aggregate", {}).get(
                "pmu_contract_id"
            )
            == PMU_CONTRACT_ID,
            "native_hierarchy_semantic_comparable": native_audit.get(
                "native_hierarchy_semantic_comparable"
            )
            is True,
            "native_target_drain_complete": native_audit.get(
                "target_drain_complete"
            )
            is True,
            "kernel_records_present": int(native["records"]) > 0,
            "kernel_memory_present": int(native["memory_uops"]) > 0,
            "trace_user_uops_match": int(scope["user_trace_uops"])
            == trace_user_uops,
            "trace_kernel_uops_match": int(native["records"])
            == trace_kernel_uops,
            "trace_user_instructions_match": int(
                scope["user_trace_instructions"]
            )
            == trace_user_instructions,
            "trace_kernel_instructions_match": int(
                native["retired_instructions"]
            )
            == trace_kernel_instructions,
            "uop_conservation": int(scope["user_trace_uops"])
            + int(scope["native_kernel_trace_uops"])
            + int(totals["syscall_uops"])
            * SYSCALL_TRANSITION_EXTRA_USER_UOPS
            == int(pmu["retired_uops"]),
            "instruction_conservation": int(scope["user_trace_instructions"])
            + int(scope["native_kernel_trace_instructions"])
            == int(pmu["retired_instructions"]),
            "fastsim_llc_outcomes_conserved": totals.get(
                "llc_outcomes_conserved"
            )
            is True
            and all(
                cha.get("llc_outcomes_conserved") is True
                for cha in simulation.get("cha", [])
            )
            and int(pmu["llc_tag_accesses"])
            == sum(int(cha["requests"]) for cha in simulation.get("cha", [])),
            "synthetic_syscall_zero": int(
                totals["synthetic_syscall_kernel"]["events"]
            )
            == 0,
            "synthetic_page_fault_zero": int(
                totals["synthetic_page_fault_kernel"]["events"]
            )
            == 0,
            "synthetic_irq_zero": int(
                totals["synthetic_irq_kernel"]["events"]
            )
            == 0,
            "synthetic_total_zero": int(
                totals["synthetic_kernel_total"]["events"]
            )
            == 0,
        }
        errors.extend(name for name, passed in checks.items() if not passed)
        report["checks"] = checks

        oracle_aggregate = oracle["aggregate"]
        pmu_comparison = build_pmu_comparison(
            pmu, oracle_aggregate, native_audit, event_contract
        )
        fastsim_cpi = scope.get("perf_like_cpi")
        gem5_cpi = oracle_aggregate.get("perf_like_cpi_user_plus_kernel")
        cpi_error = relative_error(fastsim_cpi, gem5_cpi)
        report.update(
            {
                "trace_counts": {
                    "user_uops": trace_user_uops,
                    "kernel_uops": trace_kernel_uops,
                    "user_instructions": trace_user_instructions,
                    "kernel_instructions": trace_kernel_instructions,
                    "kernel_memory_uops": native["memory_uops"],
                    "syscall_markers": totals["syscall_uops"],
                    "syscall_transition_extra_user_uops": int(
                        totals["syscall_uops"]
                    )
                    * SYSCALL_TRANSITION_EXTRA_USER_UOPS,
                },
                "cpi": {
                    "fastsim": fastsim_cpi,
                    "gem5": gem5_cpi,
                    "relative_error": cpi_error,
                    "absolute_relative_error": (
                        abs(cpi_error) if cpi_error is not None else None
                    ),
                },
                "pmu": pmu_comparison,
                "pmu_validation": {
                    "status": "scored" if not errors else "source-gate-failed",
                    "scope": "user-plus-kernel",
                    "fields": list(PMU_FIELDS),
                    "branch_reference": f"kernel_events-v3:{BRANCH_MISS_SOURCE}",
                    "cache_reference": "native-ruby-slicc-controller-actions",
                    "path_class_cache_scored": False,
                    "hardware_pmu_formal": False,
                },
                "fastsim_output": str(output_path.resolve()),
                "status": "passed" if not errors else "failed",
            }
        )
    except Exception as error:  # Keep the watcher alive and report per-case failure.
        report["errors"].append(f"{type(error).__name__}: {error}")
    report["finished_at_utc"] = utc_now()
    atomic_json(report_path, report)
    return report


def existing_report(case, args):
    path = args.output_dir / "cases" / case["case_id"] / "validation.json"
    try:
        report = load_json(path)
        fingerprint = validation_fingerprint(case, args)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        report.get("schema") != CASE_SCHEMA
        or report.get("result_dir") != case["result_dir"]
        or report.get("validation_fingerprint", {}).get("sha256")
        != fingerprint["sha256"]
    ):
        return None
    if report.get("status") == "passed":
        return report
    if int(report.get("attempts", 0)) >= args.max_attempts:
        return report
    return None


def write_summary(args, discovered):
    reports = []
    for case in discovered:
        path = args.output_dir / "cases" / case["case_id"] / "validation.json"
        try:
            report = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            report.get("schema") == CASE_SCHEMA
            and report.get("result_dir") == case["result_dir"]
        ):
            reports.append(report)
    passed = [row for row in reports if row.get("status") == "passed"]
    failed = [row for row in reports if row.get("status") == "failed"]
    terminal_failed = [
        row
        for row in failed
        if int(row.get("attempts", 0)) >= args.max_attempts
    ]
    cpi_errors = [
        row.get("cpi", {}).get("absolute_relative_error") for row in passed
    ]
    cpi_errors = [value for value in cpi_errors if value is not None]
    pmu_accuracy = aggregate_pmu_accuracy(reports)
    scored_pmu_cases = sum(
        row.get("status") == "passed"
        and row.get("pmu_validation", {}).get("status") == "scored"
        for row in reports
    )
    summary = {
        "schema": SUMMARY_SCHEMA,
        "updated_at_utc": utc_now(),
        "expected_cases": args.expected_cases,
        "discovered_completed_cases": len(discovered),
        "validated_cases": len(reports),
        "passed_cases": len(passed),
        "failed_cases": len(failed),
        "terminal_failed_cases": len(terminal_failed),
        "retryable_failed_cases": len(failed) - len(terminal_failed),
        "pending_cases": max(
            0, args.expected_cases - len(passed) - len(terminal_failed)
        ),
        "pass_semantics": (
            "native replay, oracle-source, and conservation gates only; "
            "PMU accuracy is reported separately"
        ),
        "pmu_scope": "user-plus-kernel",
        "pmu_fields": list(PMU_FIELDS),
        "pmu_scored_cases": scored_pmu_cases,
        "pmu_accuracy_status": "gem5-semantic-diagnostic-not-hardware-formal",
        "path_class_cache_scored": False,
        "pmu_accuracy": pmu_accuracy,
        "mean_absolute_cpi_relative_error": (
            sum(cpi_errors) / len(cpi_errors) if cpi_errors else None
        ),
        "max_absolute_cpi_relative_error": max(cpi_errors) if cpi_errors else None,
        "cases": [
            {
                "case_id": row["case_id"],
                "cores": row["cores"],
                "workload": row["workload"],
                "status": row["status"],
                "kernel_uops": row.get("trace_counts", {}).get("kernel_uops"),
                "kernel_instructions": row.get("trace_counts", {}).get(
                    "kernel_instructions"
                ),
                "fastsim_cpi": row.get("cpi", {}).get("fastsim"),
                "gem5_cpi": row.get("cpi", {}).get("gem5"),
                "absolute_cpi_relative_error": row.get("cpi", {}).get(
                    "absolute_relative_error"
                ),
                "pmu_validation_status": row.get("pmu_validation", {}).get(
                    "status"
                ),
                "pmu": row.get("pmu", {}),
                "errors": row.get("errors", []),
                "result_dir": row["result_dir"],
            }
            for row in sorted(reports, key=lambda item: item["case_id"])
        ],
    }
    atomic_json(args.output_dir / "summary.json", summary)
    csv_path = args.output_dir / "summary.csv"
    with csv_path.open("w", newline="") as handle:
        fields = [
            "case_id",
            "cores",
            "workload",
            "status",
            "kernel_uops",
            "kernel_instructions",
            "fastsim_cpi",
            "gem5_cpi",
            "absolute_cpi_relative_error",
            "pmu_validation_status",
            "result_dir",
        ]
        for field in PMU_FIELDS:
            fields.extend(
                (
                    f"{field}_predicted",
                    f"{field}_reference",
                    f"{field}_ape_percent",
                )
            )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in summary["cases"]:
            csv_row = {
                field: row.get(field)
                for field in fields
                if not any(
                    field.endswith(suffix)
                    for suffix in (
                        "_predicted",
                        "_reference",
                        "_ape_percent",
                    )
                )
            }
            for field in PMU_FIELDS:
                metric = row.get("pmu", {}).get(field, {})
                csv_row[f"{field}_predicted"] = metric.get("predicted")
                csv_row[f"{field}_reference"] = metric.get("reference")
                csv_row[f"{field}_ape_percent"] = metric.get(
                    "absolute_percentage_error"
                )
            writer.writerow(csv_row)
    lines = [
        "# FastSim native-kernel validation",
        "",
        f"- expected: {summary['expected_cases']}",
        f"- discovered: {summary['discovered_completed_cases']}",
        f"- replay/source gates passed: {summary['passed_cases']}",
        f"- failed: {summary['failed_cases']}",
        f"- pending: {summary['pending_cases']}",
        f"- cache/branch PMU cases scored: {summary['pmu_scored_cases']}",
        "- PMU status: gem5-semantic diagnostic, not hardware-PMU formal",
        "- cache reference: native Ruby/SLICC only; path-class cache is excluded",
        "",
        "| case | status | kernel UOPs | FastSim CPI | gem5 CPI | abs. rel. error |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary["cases"]:
        error = row["absolute_cpi_relative_error"]
        lines.append(
            f"| {row['case_id']} | {row['status']} | {row['kernel_uops']} | "
            f"{row['fastsim_cpi']} | {row['gem5_cpi']} | {error} |"
        )
    lines.extend(
        [
            "",
            "## Cache/branch miss PMU accuracy",
            "",
            "| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | "
            "WAPE | bias | reference source |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for field in PMU_FIELDS:
        metric = pmu_accuracy[field]
        lines.append(
            f"| {field} | {metric['mapping']} | "
            f"{metric['finite_ape_cases']}/{metric['cases']} | "
            f"{percent_text(metric['mape_percent'])} | "
            f"{percent_text(metric['p50_ape_percent'])} | "
            f"{percent_text(metric['p90_ape_percent'])} | "
            f"{percent_text(metric['p99_ape_percent'])} | "
            f"{percent_text(metric['wape_percent'])} | "
            f"{percent_text(metric['signed_bias_percent'])} | "
            f"{metric['reference_source'] or 'N/A'} |"
        )
    (args.output_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return summary


def collection_finished(path):
    if path is None or not path.is_file():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fastsim", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--event-dictionary", type=Path, default=EVENT_DICTIONARY
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--expected-cases", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--collection-exit-file", type=Path)
    args = parser.parse_args()
    args.repo_root = args.repo_root.resolve()
    args.output_dir = args.output_dir.resolve()
    args.fastsim = args.fastsim.resolve()
    args.config = args.config.resolve()
    args.event_dictionary = args.event_dictionary.resolve()
    args.matrix = [path.resolve() for path in args.matrix]
    if args.jobs <= 0 or args.expected_cases <= 0 or args.max_attempts <= 0:
        parser.error("jobs, expected-cases, and max-attempts must be positive")
    if args.poll_seconds <= 0:
        parser.error("poll-seconds must be positive")
    return args


def main():
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        discovered = discover_cases(args.matrix)
        pending = []
        for case in discovered:
            if existing_report(case, args) is None:
                pending.append(case)
        if pending:
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                futures = {
                    executor.submit(run_case, case, args): case for case in pending
                }
                for future in as_completed(futures):
                    report = future.result()
                    print(
                        f"[fastsim-native-validation] {report['case_id']} "
                        f"{report['status']}",
                        flush=True,
                    )
        summary = write_summary(args, discovered)
        print(
            "[fastsim-native-validation] "
            f"discovered={summary['discovered_completed_cases']}/"
            f"{args.expected_cases} passed={summary['passed_cases']} "
            f"failed={summary['failed_cases']} pending={summary['pending_cases']}",
            flush=True,
        )
        terminal = summary["passed_cases"] + summary["terminal_failed_cases"]
        if terminal >= args.expected_cases:
            return 0 if summary["terminal_failed_cases"] == 0 else 1
        if not args.watch:
            return 0 if summary["terminal_failed_cases"] == 0 else 1
        exit_code = collection_finished(args.collection_exit_file)
        if exit_code is not None and exit_code != 0:
            return 1
        if exit_code == 0 and len(discovered) < args.expected_cases:
            return 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
