#!/usr/bin/env python3
"""Replay native-kernel FST cases and score cache/branch miss diagnostics.

Branch-miss truth comes from the original gem5 BPred redirect outcome carried
to retirement in kernel_events.json.  Cache-miss truth comes only from the
drained native Ruby/SLICC sideband.  The legacy retirement-time DynInst branch
comparison and TaoTrace path-class cache fields are deliberately never scored.

Use --source-root with --stats-dir to rescore archived <case_id>.json outputs
without rerunning FastSim. Archived outputs are hashed, but their original
binary/configuration provenance is not certified by the currently installed
binary. Both modes use the same native-source and conservation gates.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from audit_p1_native_response_sideband import audit_result as audit_native_result
from audit_task_pmu import audit_task_pmu
from audit_perf_retirement import (
    EVENT_DICTIONARY as PERF_RETIREMENT_DICTIONARY,
    aggregate_perf_retirement_accuracy, audit_perf_retirement,
    compare_perf_retirement, load_event_dictionary as load_retirement_dictionary,
)
from audit_perf_cache import audit_perf_cache, compare_report as compare_perf_cache_report, aggregate_report_accuracy as aggregate_perf_cache_accuracy, aggregate_report_estimates
from retired_cache_contract import audit_retired_cache, compare_retired_cache, write_retired_cache_summary
from perf_cache_contract import DICTIONARY as PERF_CACHE_DICTIONARY, load_dictionary as load_perf_cache_dictionary
from validate_kernel_events_oracle import BRANCH_MISS_SOURCE
from time_accounting import (
    assemble_gem5_time_accounting,
    build_gem5_time_accounting,
    build_time_comparison,
    load_gem5_time_accounting,
)


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
        "event_dictionary": args.event_dictionary,
        "validator": Path(__file__),
        "native_auditor": NATIVE_AUDITOR,
        "oracle_validator": ORACLE_VALIDATOR,
        "time_accounting_helper": Path(__file__).with_name("time_accounting.py"),
        "thread_state_graph_helper": Path(__file__).with_name("thread_state_graph.py"),
        "thread_state_accounting_auditor": Path(__file__).with_name("audit_thread_state_accounting.py"),
        "task_clock_counter_auditor": Path(__file__).with_name("audit_task_clock.py"),
        "task_clock_initial_proof": Path(__file__).with_name("task_clock_initial.py"),
        "task_pmu_auditor": Path(__file__).with_name("audit_task_pmu.py"),
        "memory_request_role_auditor": Path(__file__).with_name("fst_memory_request_roles.py"),
        "request": result_dir / "request.json",
        "trace": result_dir / "tao_trace/trace.json",
        "manifest": result_dir / "tao_trace/manifest.txt",
        "kernel_events": oracle_dir / "kernel_events.json",
    }
    for name, path in (
        ("cpl_class", oracle_dir / "cpl_class.jsonl"),
        ("gem5_stats", result_dir / "stats.txt"),
        ("gem5_trace_stats", result_dir / "tao_trace/stats.txt"),
    ):
        if path.is_file():
            paths[name] = path
    stats_dir = getattr(args, "stats_dir", None)
    if stats_dir is not None:
        paths["archived_fastsim_output"] = stats_dir / f"{case['case_id']}.json"
    else:
        paths["fastsim"] = args.fastsim
        for depth, config_path in enumerate(config_include_chain(args.config)):
            name = "config" if depth == 0 else f"config_include/{depth:04d}"
            paths[name] = config_path
    # Functional request-role changes alter replay even when only CPI/native
    # diagnostics are requested. Cached validation must bind this input too.
    for companion in sorted((result_dir / "tao_trace").glob("*.fst.mreq")):
        paths[f"memory_requests/{companion.name}"] = companion
    # These functional flags/masks alter eligibility and replay even though the
    # 64-byte instruction stream remains unchanged. Bind them to rescoring.
    for companion in sorted((result_dir / "tao_trace").glob("*.fst.uops.gz")):
        paths[f"static_uop_facts/{companion.name}"] = companion
    for companion in sorted((result_dir / "tao_trace").glob("*.fst.sreq")):
        paths[f"functional_memory_facts/{companion.name}"] = companion
    for companion in sorted((result_dir / "tao_trace").glob("*.fst.xlat.gz")):
        paths[f"dynamic_translations/{companion.name}"] = companion
    paths['functional_retirement_auditor']=Path(__file__).with_name('fst_retirement_eligibility.py')
    hash_helper=Path(__file__).resolve().parents[1]/'build/fastsim_fst_stream_hash'
    if hash_helper.is_file():paths['functional_retirement_hash_helper']=hash_helper
    for companion in sorted((result_dir/'tao_trace').glob('*.fst.fsem')):
        paths['functional_retirement/'+companion.name]=companion
    for summary in native_summaries:
        paths[f"native_summary/{summary.name}"] = summary
    for timing in sorted(oracle_dir.glob("time-accounting-core*.json")):
        paths[f"time_accounting_oracle/{timing.name}"] = timing
    for task_pmu in sorted(oracle_dir.glob("task-pmu-core*.json")):
        paths[f"task_pmu_oracle/{task_pmu.name}"] = task_pmu
    retirement_requested = getattr(args, "perf_retirement", False)
    retired_cache_requested=getattr(args,"retired_cache",False)
    if retirement_requested or getattr(args,'perf_cache',False) or retired_cache_requested:
        for pattern in ('macro-boundary-core*.jsonl','macro-boundary-metadata-core*.json'):
            for boundary in sorted((result_dir/'tao_trace').glob(pattern)):
                paths['functional_macro_boundary/'+boundary.name]=boundary
    if retirement_requested:
        load_retirement_dictionary()
        paths["perf_retirement_auditor"] = Path(__file__).with_name("audit_perf_retirement.py")
        paths["perf_retirement_dictionary"] = PERF_RETIREMENT_DICTIONARY
        for sidecar in sorted(oracle_dir.glob("perf-retirement-core*.json")):
            paths[f"perf_retirement_oracle/{sidecar.name}"] = sidecar
    cache_requested = getattr(args, "perf_cache", False)
    if cache_requested or retired_cache_requested:
        load_perf_cache_dictionary()
        paths["perf_cache_auditor"] = Path(__file__).with_name("audit_perf_cache.py")
        paths["perf_cache_contract"] = Path(__file__).with_name("perf_cache_contract.py")
        paths["perf_cache_dictionary"] = PERF_CACHE_DICTIONARY
        paths["perf_cache_retirement_auditor"] = Path(__file__).with_name("audit_perf_retirement.py")
        if getattr(args,"perf_cache_estimates",False):
            paths["functional_source_identity_helper"] = Path(__file__).with_name("perf_cache_source.py")
        for sidecar in sorted(oracle_dir.glob("perf-cache-core*.json")):
            paths[f"perf_cache_oracle/{sidecar.name}"] = sidecar
        for sidecar in sorted(oracle_dir.glob("perf-retirement-core*.json")):
            paths[f"perf_cache_retirement/{sidecar.name}"] = sidecar
    if retired_cache_requested:
        paths["retired_cache_contract"] = Path(__file__).with_name("retired_cache_contract.py")
        paths["retired_cache_dictionary"] = Path(__file__).resolve().parents[1]/"configs/retired-load-cache-events-v1.json"
        paths["retired_cache_source_identity"] = Path(__file__).with_name("perf_cache_source.py")
    audited_time = oracle_dir / "thread-state-accounting.json"
    if audited_time.is_file():
        paths["audited_thread_state_accounting"] = audited_time
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
        "mode": "rescore-archived-output" if stats_dir is not None else "replay",
        "cores": int(case["cores"]),
        "perf_retirement_requested": retirement_requested,
        "perf_cache_requested": cache_requested,
        "retired_cache_requested": retired_cache_requested,
        "retired_cache_misses_only": getattr(args,'retired_cache_misses',False),
        "llc_fill_audit_requested": bool(os.environ.get("FASTSIM_LLC_AUDIT")),
        "llc_fill_audit_sampling": os.environ.get("FASTSIM_LLC_AUDIT_STRIDE","16"),
        "perf_cache_estimates_requested": getattr(args,"perf_cache_estimates",False),
        "perf_cache_incomplete_errors_requested": getattr(args,"perf_cache_incomplete_errors",False),
        "inputs": inputs,
    }
    if audited_time.is_file():
        # Report reuse must repeat the current-source audit gate. A saved audit
        # hash alone cannot detect changed functional facts or FST metadata.
        payload["current_time_accounting_evidence"] = load_gem5_time_accounting(
            result_dir, load_json(oracle_dir / "kernel_events.json"), cores=int(case["cores"]))
    if list(oracle_dir.glob("task-pmu-core*.json")):
        # The task auditor scans current FST populations; cached reports must
        # never bypass that check when non-event records have changed.
        try:
            payload["current_task_pmu_evidence"] = audit_task_pmu(result_dir / "tao_trace")
        except (OSError, KeyError, TypeError, ValueError) as error:
            payload["current_task_pmu_evidence"] = {"valid": False, "reason": str(error)}
    if retirement_requested:
        # Repeat the independent current-FST scan even for cached reports.
        payload["current_perf_retirement_evidence"] = audit_perf_retirement(result_dir / "tao_trace")
    if cache_requested:
        payload["current_perf_cache_evidence"] = audit_perf_cache(result_dir / "tao_trace",
            include_input_identity=getattr(args,"perf_cache_estimates",False),
            allow_incomplete=getattr(args,"perf_cache_incomplete_errors",False))
    if retired_cache_requested:
        payload["current_retired_cache_evidence"]=audit_retired_cache(result_dir/"tao_trace")
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


def build_task_pmu_comparison(metric, audited, contract):
    """Score the explicit task population, retaining the physical legacy API."""
    if (metric.get("status") != "available" or metric.get("scope") != "workload-task"
            or metric.get("population_contract") != "task-oncpu-switch-entry-v1"
            or not audited.get("valid") or metric.get("task_ids") != audited.get("task_ids")):
        raise ValueError("task PMU contract, availability or task population mismatch")
    predicted, reference = metric.get("pmu", {}), audited["pmu"]
    for key in ("retired_instructions", "retired_uops", "memory_uops", "branches", *PMU_FIELDS):
        values = [metric.get(population, {}).get(key) for population in ("pmu", "other_pmu", "full_pmu")]
        if (any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values)
                or values[0] + values[1] != values[2]):
            raise ValueError(f"task PMU {key} does not conserve task+other=full")
        if key in ("retired_instructions", "retired_uops", "memory_uops", "branches") and predicted[key] != reference[key]:
            raise ValueError(f"task PMU {key} population mismatch: {predicted[key]} vs {reference[key]}")
    if predicted["retired_instructions"] <= 0:
        raise ValueError("task PMU lacks a positive macro population")
    per_core = metric.get("per_core", [])
    reference_cores = {row["core_id"]: row for row in audited["per_core"]}
    if (len(per_core) != len(reference_cores) or
            {row.get("core") for row in per_core} != set(reference_cores)):
        raise ValueError("task PMU per-core population coverage mismatch")
    for row in per_core:
        reference_core = reference_cores[row["core"]]
        for key in ("retired_instructions", "retired_uops", "memory_uops", "branches", *PMU_FIELDS):
            values = [row.get(population, {}).get(key) for population in ("pmu", "other_pmu", "full_pmu")]
            if (any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values)
                    or values[0] + values[1] != values[2]):
                raise ValueError(f"task PMU per-core {key} conservation mismatch")
            if key in ("retired_instructions", "retired_uops", "memory_uops", "branches"):
                if any(row[pop][key] != reference_core[pop][key] for pop in ("pmu", "other_pmu")):
                    raise ValueError(f"task PMU per-core {key} population mismatch")
    for pop in ("pmu", "other_pmu", "full_pmu"):
        for key in ("retired_instructions", "retired_uops", "memory_uops", "branches", *PMU_FIELDS):
            if sum(row[pop][key] for row in per_core) != metric[pop][key]:
                raise ValueError(f"task PMU per-core {key} aggregate conservation mismatch")
    result = {field: error_row(predicted[field], reference[field], contract[field],
                              "task-pmu:taotrace-retired-bpred-v1" if field == "branch_misses"
                              else f"native-ruby-slicc:workload-task:{CACHE_LEVEL_BY_FIELD[field]}.tag_misses")
              for field in PMU_FIELDS}
    for field, row in result.items():
        row.update(population_scope="workload-task", population_contract="task-oncpu-switch-entry-v1",
                   fastsim_counter_path=f"scope_metrics.task_pmu.pmu.{field}")
        row["fastsim_semantics"] = ("recorded workload-task subset; data cache events exclude "
                                    "instruction and page-table requests; " + row["fastsim_semantics"])
    return result


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


def discover_cases(matrices, source_root=None):
    cases = {}
    if source_root is not None:
        for request_path in sorted(source_root.glob("*/request.json")):
            request = load_json(request_path)
            result = request_path.parent.resolve()
            cases[result.name] = {
                "case_id": result.name,
                "cores": int(request["profile"]["cores"]),
                "workload": request["workload_selection"]["workload"],
                "result_dir": str(result),
            }
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


def pmu_population_checks(scope, oracle_aggregate):
    """A full physical kernel stream may contain idle excluded by native truth.

    Replay conservation alone does not prove reference-population identity.
    Use the independent reference macro population, never task CPI counts.
    """
    def count(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    user = scope.get("user_trace_instructions")
    kernel = scope.get("native_kernel_trace_instructions")
    reference_user = oracle_aggregate.get("user_retired_instructions")
    reference_combined = oracle_aggregate.get("user_plus_kernel_retired_instructions")
    return {
        "pmu_user_instruction_population_match": count(user) and count(reference_user)
        and user == reference_user,
        "pmu_instruction_population_match": all(count(v) for v in (user, kernel, reference_combined))
        and user + kernel == reference_combined,
    }


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
        stats_dir = getattr(args, "stats_dir", None)
        if stats_dir is not None:
            output_path = stats_dir / f"{case['case_id']}.json"
            report["execution_mode"] = "rescore-archived-output"
            report["binary_config_provenance"] = "not-certified-by-rescoring"
        else:
            output_path = case_dir / "fastsim.json"
            report["execution_mode"] = "replay"
            command = [
                str(args.fastsim), "simulate", "--config", str(args.config),
                "--manifest", str(manifest), "--measurement-scope", "user-plus-kernel",
                "--cores", str(case["cores"]), "--output", str(output_path),
            ]
            if getattr(args,"perf_cache_estimates",False) or getattr(args,"retired_cache",False):command += ["--perf-cache-input-identity","true"]
            process = subprocess.run(
                command, cwd=args.repo_root, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            (case_dir / "fastsim.log").write_text(
                "command=" + " ".join(command) + "\n" + process.stdout
            )
            report["return_code"] = process.returncode
            if process.returncode:
                raise RuntimeError(f"FastSim exited with {process.returncode}")

        simulation = load_json(output_path)
        if simulation.get("llc_fill_audit"):
            report["fastsim_output_sha256"] = sha256_file(output_path)
            report["llc_fill_audit"] = simulation["llc_fill_audit"]
        result_dir = Path(case["result_dir"])
        oracle = load_json(result_dir / "oracle/kernel_events.json")
        native_audit = audit_native_result(result_dir)
        scope = simulation["scope_metrics"]
        task_metric = scope.get("task_pmu", {})
        task_mode = task_metric.get("status") not in (None, "disabled")
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
            **({} if task_mode else pmu_population_checks(scope, oracle["aggregate"])),
            "core_count": configuration.get("cores") == case["cores"],
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
            # Native capture declares a capability, not a nonzero event count.
            # A compute-only window may have no kernel records; kernel code
            # itself may also retire without any memory operations. The source
            # declaration and exact trace-count checks remain mandatory.
            "kernel_record_count_valid": int(native["records"]) >= 0,
            "kernel_memory_count_valid": 0 <= int(native["memory_uops"])
            <= int(native["records"]),
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
        time_comparison = build_time_comparison(
            simulation, oracle, load_gem5_time_accounting(
                result_dir, oracle, cores=case["cores"]
            )
        )
        report.update(time_comparison)
        report["pmu_validation"] = {"status": "source-gate-failed",
                                    "scope": "workload-task" if task_mode else "user-plus-kernel"}
        # These namespaces have independent source/population contracts. Keep
        # their audited estimates even if the stricter task-PMU gate below
        # fails (for example at a cut inside a collapsed syscall). The native
        # task gate still fails closed; none of its counts are rewritten.
        if getattr(args, "perf_cache", False):
            estimates_requested=getattr(args,"perf_cache_estimates",False)
            incomplete_errors=getattr(args,"perf_cache_incomplete_errors",False)
            cache_audit = audit_perf_cache(result_dir / "tao_trace",include_input_identity=estimates_requested,
                                          allow_incomplete=incomplete_errors)
            report["perf_cache_audit"] = cache_audit
            prediction=dict(scope.get("perf_cache", {}))
            if estimates_requested:prediction['input_identity']=simulation.get('functional_source_identity')
            report["perf_cache_validation"] = compare_perf_cache_report(prediction, cache_audit,include_estimates=estimates_requested,
                                                                         allow_incomplete_errors=incomplete_errors)
            report['perf_cache_evidence_status']=dict(
                incomplete_errors_requested=incomplete_errors,
                retirement_qualification_complete=cache_audit.get('retirement_qualification_complete',True),
                hardware_equivalence='unverified')
            checks["perf_cache_contract_and_source_match"] = True
        if getattr(args,"retired_cache",False):
            from perf_cache_source import collect_source_identity,validate_identity
            identity=simulation.get('functional_source_identity',{})
            current=collect_source_identity(manifest,result_dir/'thread-state.plan')
            validate_identity(identity,current)
            prediction=dict(scope.get('retired_cache',{}))
            prediction.update(input_identity=identity['source_identity'])
            if getattr(args,'retired_cache_misses',False):
                report['retired_cache_validation']=compare_retired_cache(
                    prediction,fingerprint['current_retired_cache_evidence'],misses_only=True)
            else:
                report['retired_cache_validation']=compare_retired_cache(prediction,fingerprint['current_retired_cache_evidence'])
            checks['retired_cache_source_population_match']=True
        if task_mode:
            task_audit = audit_task_pmu(result_dir / "tao_trace")
            report["task_pmu_audit"] = task_audit
            report["legacy_physical_population_checks"] = pmu_population_checks(scope, oracle_aggregate)
            pmu_comparison = build_task_pmu_comparison(task_metric, task_audit, event_contract)
            checks["task_pmu_population_match"] = True
        else:
            pmu_comparison = build_pmu_comparison(
                pmu, oracle_aggregate, native_audit, event_contract
            ) if all(pmu_population_checks(scope, oracle_aggregate).values()) else {}
        if getattr(args, "perf_retirement", False):
            retirement_audit = audit_perf_retirement(result_dir / "tao_trace")
            report["perf_retirement_audit"] = retirement_audit
            report["perf_retirement_validation"] = compare_perf_retirement(
                scope.get("perf_retirement", {}), retirement_audit,
                task_ids=task_metric.get("task_ids"))
            checks["perf_retirement_population_match"] = True
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
                **time_comparison,
                "pmu": pmu_comparison,
                "pmu_validation": {
                    "status": "scored" if not errors else "source-gate-failed",
                    "scope": "workload-task" if task_mode else "user-plus-kernel",
                    "population_contract": "task-oncpu-switch-entry-v1" if task_mode else "legacy-cpl-v3",
                    "fields": list(PMU_FIELDS),
                    "branch_reference": f"task-pmu:{BRANCH_MISS_SOURCE}" if task_mode else f"kernel_events-v3:{BRANCH_MISS_SOURCE}",
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
    active_cpi = [row for row in reports
                  if row.get("cpi", {}).get("status") == "scored-active"]
    legacy_cpi = [row for row in passed
                  if row.get("cpi", {}).get("status", "legacy-diagnostic")
                  == "legacy-diagnostic"]
    # Preserve old-only report diagnostics without mixing historical numerators
    # into an explicit active-CPI aggregate.
    active_by_policy = {}
    for row in active_cpi:
        policy = row['cpi'].get('active_end_policy', 'common-end-v1')
        active_by_policy.setdefault(policy, []).append(row)
    mixed_active_policies = len(active_by_policy) > 1
    homogeneous_active = [] if mixed_active_policies else active_cpi
    cpi_rows = homogeneous_active if active_cpi else legacy_cpi
    cpi_errors = [
        row.get("cpi", {}).get("absolute_relative_error") for row in cpi_rows
    ]
    cpi_errors = [value for value in cpi_errors if value is not None]
    absolute_cpi_errors = [
        abs(row["cpi"]["fastsim"] - row["cpi"]["gem5"])
        for row in cpi_rows
        if row.get("cpi", {}).get("fastsim") is not None
        and row.get("cpi", {}).get("gem5") is not None
    ]
    def cpi_accuracy(rows):
        errors = [abs(row["cpi"]["fastsim"] - row["cpi"]["gem5"])
                  for row in rows if row.get("cpi", {}).get("fastsim") is not None
                  and row.get("cpi", {}).get("gem5") is not None]
        return {"cases": len(errors), "cpi_mae": sum(errors) / len(errors) if errors else None,
                "absolute_error_unit": "cycles per macro instruction"}

    elapsed_rows = [row["elapsed"] for row in reports
                    if row.get("elapsed", {}).get("status") == "scored"]
    elapsed_errors = [row["absolute_error_seconds"] for row in elapsed_rows]
    pmu_scopes = sorted({row.get("pmu_validation", {}).get("scope", "user-plus-kernel")
                         for row in reports if row.get("status") == "passed"
                         and row.get("pmu_validation", {}).get("status") == "scored"})
    pmu_accuracy_by_scope = {
        scope: aggregate_pmu_accuracy([row for row in reports
                                      if row.get("pmu_validation", {}).get("scope", "user-plus-kernel") == scope])
        for scope in pmu_scopes}
    pmu_accuracy = pmu_accuracy_by_scope[pmu_scopes[0]] if len(pmu_scopes) == 1 else aggregate_pmu_accuracy([])
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
        "pmu_scope": pmu_scopes[0] if len(pmu_scopes) == 1 else "mixed" if pmu_scopes else "unavailable",
        "pmu_accuracy_by_scope": pmu_accuracy_by_scope,
        "primary_pmu_namespace": "retired_cache" if getattr(args,"retired_cache",False) else "native_tag_miss_diagnostics",
        "primary_retired_cache_events": ['retired_l1_load_misses','retired_l2_load_misses','retired_l3_load_misses']
            if getattr(args,'retired_cache_misses',False) else None,
        "pmu_fields": list(PMU_FIELDS),
        "pmu_scored_cases": scored_pmu_cases,
        "pmu_accuracy_status": "gem5-semantic-diagnostic-not-hardware-formal",
        "path_class_cache_scored": False,
        "cpi_accuracy_status": (
            "unavailable-mixed-active-end-policies; inspect per-policy aggregates"
            if mixed_active_policies else
            "active-only; inspect per-case instruction population match"
            if active_cpi else "legacy-diagnostic; active CPI not scored"
        ),
        "active_cpi_accuracy": cpi_accuracy(homogeneous_active),
        "active_cpi_accuracy_by_end_policy": {
            policy: cpi_accuracy(rows) for policy, rows in sorted(active_by_policy.items())},
        "legacy_cpi_accuracy": cpi_accuracy(legacy_cpi),
        "cpi_unavailable_cases": sum(row.get("cpi", {}).get("status") == "unavailable"
                                     for row in reports),
        "elapsed_accuracy": {"cases": len(elapsed_errors),
                             "mae_seconds": sum(elapsed_errors) / len(elapsed_errors)
                             if elapsed_errors else None},
        "cpi_mae": (
            sum(absolute_cpi_errors) / len(absolute_cpi_errors)
            if absolute_cpi_errors else None
        ),
        "cpi_absolute_error_unit": "cycles per macro instruction",
        "pmu_accuracy": pmu_accuracy,
        "perf_retirement_requested": getattr(args, "perf_retirement", False),
        "perf_retirement_accuracy_by_scope": aggregate_perf_retirement_accuracy(reports),
        "perf_cache_requested": getattr(args, "perf_cache", False),
        "perf_cache_accuracy_by_scope": aggregate_perf_cache_accuracy(reports),
        "perf_cache_estimates_requested": getattr(args,"perf_cache_estimates",False),
        "perf_cache_incomplete_errors_requested": getattr(args,"perf_cache_incomplete_errors",False),
        "perf_cache_estimates_by_scope": aggregate_report_estimates(reports),
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
                "cpi_status": row.get("cpi", {}).get("status", "legacy-diagnostic"),
                "active_scope": row.get("cpi", {}).get("active_scope"),
                "fastsim_active_source": row.get("cpi", {}).get("fastsim_source"),
                "gem5_active_source": row.get("cpi", {}).get("gem5_source"),
                "cpi_unavailable_reasons": row.get("cpi", {}).get("reasons", []),
                "elapsed_status": row.get("elapsed", {}).get("status", "unavailable"),
                "fastsim_elapsed_seconds": row.get("elapsed", {}).get("fastsim_seconds"),
                "gem5_elapsed_seconds": row.get("elapsed", {}).get("gem5_seconds"),
                "elapsed_absolute_error_seconds": row.get("elapsed", {}).get("absolute_error_seconds"),
                "elapsed_absolute_relative_error": row.get("elapsed", {}).get("absolute_relative_error"),
                "absolute_cpi_relative_error": row.get("cpi", {}).get(
                    "absolute_relative_error"
                ),
                "absolute_cpi_error": (
                    abs(row["cpi"]["fastsim"] - row["cpi"]["gem5"])
                    if row.get("cpi", {}).get("fastsim") is not None
                    and row.get("cpi", {}).get("gem5") is not None else None
                ),
                "cpi_instruction_population_match": row.get("cpi", {}).get(
                    "instruction_population_match"
                ),
                "pmu_validation_status": row.get("pmu_validation", {}).get(
                    "status"
                ),
                "pmu_scope": row.get("pmu_validation", {}).get("scope"),
                "pmu_population_contract": row.get("pmu_validation", {}).get("population_contract"),
                "pmu": row.get("pmu", {}),
                "perf_retirement_validation": row.get("perf_retirement_validation"),
                "retired_cache_validation": row.get("retired_cache_validation"),
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
            "cpi_status",
            "active_scope",
            "fastsim_active_source",
            "gem5_active_source",
            "elapsed_status",
            "fastsim_elapsed_seconds",
            "gem5_elapsed_seconds",
            "elapsed_absolute_error_seconds",
            "elapsed_absolute_relative_error",
            "absolute_cpi_relative_error",
            "absolute_cpi_error",
            "cpi_instruction_population_match",
            "pmu_scope",
            "pmu_population_contract",
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
        f"- CPI status: {summary['cpi_accuracy_status']}",
        f"- CPI MAE: {summary['cpi_mae']} cycles per macro instruction",
        f"- active CPI cases: {summary['active_cpi_accuracy']['cases']}; "
        f"legacy diagnostic cases: {summary['legacy_cpi_accuracy']['cases']}; "
        f"active unavailable cases: {summary['cpi_unavailable_cases']}",
        f"- elapsed cases: {summary['elapsed_accuracy']['cases']}; "
        f"elapsed MAE: {summary['elapsed_accuracy']['mae_seconds']} seconds",
        "",
        "| case | status | kernel UOPs | FastSim CPI | gem5 CPI | abs. rel. error | "
        "CPI absolute error (cyc/inst) | instruction population match | CPI status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in summary["cases"]:
        error = row["absolute_cpi_relative_error"]
        lines.append(
            f"| {row['case_id']} | {row['status']} | {row['kernel_uops']} | "
            f"{row['fastsim_cpi']} | {row['gem5_cpi']} | {error} | "
            f"{row['absolute_cpi_error']} | {row['cpi_instruction_population_match']} | "
            f"{row['cpi_status']} |"
        )
    lines.extend(["", "## Target elapsed time", "",
                  "| case | status | FastSim seconds | gem5 seconds | absolute error (s) | abs. rel. error |",
                  "|---|---|---:|---:|---:|---:|"])
    for row in summary["cases"]:
        lines.append(f"| {row['case_id']} | {row['elapsed_status']} | "
                     f"{row['fastsim_elapsed_seconds']} | {row['gem5_elapsed_seconds']} | "
                     f"{row['elapsed_absolute_error_seconds']} | {row['elapsed_absolute_relative_error']} |")
    lines.extend(["", "## Active cycle evidence", "",
                  "| case | active scope | FastSim source | gem5 source | unavailable reasons |",
                  "|---|---|---|---|---|"])
    for row in summary["cases"]:
        lines.append(f"| {row['case_id']} | {row['active_scope']} | "
                     f"{row['fastsim_active_source']} | {row['gem5_active_source']} | "
                     f"{', '.join(row['cpi_unavailable_reasons'])} |")
    lines.extend(
        [
            "",
            "## Native cache/branch diagnostics" if getattr(args,"retired_cache",False) else "## Cache/branch miss PMU accuracy",
            "",
            "| scope / counter | mapping | finite cases | MAPE | P50 | P90 | P99 | "
            "WAPE | bias | reference source |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for pmu_scope, scoped_accuracy in (pmu_accuracy_by_scope or {"unavailable": pmu_accuracy}).items():
        for field in PMU_FIELDS:
            metric = scoped_accuracy[field]
            lines.append(
                f"| {pmu_scope} / {field} | {metric['mapping']} | "
                f"{metric['finite_ape_cases']}/{metric['cases']} | "
                f"{percent_text(metric['mape_percent'])} | "
                f"{percent_text(metric['p50_ape_percent'])} | "
                f"{percent_text(metric['p90_ape_percent'])} | "
                f"{percent_text(metric['p99_ape_percent'])} | "
                f"{percent_text(metric['wape_percent'])} | "
                f"{percent_text(metric['signed_bias_percent'])} | "
                f"{metric['reference_source'] or 'N/A'} |"
            )
    if getattr(args,"retired_cache",False):
        lines += ["", "## Primary retired-load cache PMU", "",
                  "See [retired-cache-summary.md](retired-cache-summary.md) for raw counts, unknown qualifications and provisional errors. Native tag misses above remain separate diagnostics."]
    (args.output_dir / "summary.md").write_text("\n".join(lines) + "\n")
    if getattr(args, "perf_retirement", False):
        perf_lines = ["# Retirement-event diagnostics", "", "Hardware perf equivalence: unverified. Each scope/event and raw/rate population is separate.", "",
                      "| scope / event | unit | scored / cases | unavailable | finite APE / cases | MAPE | P50 | P90 | P99 | WAPE | signed bias |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for scope_name, events in summary["perf_retirement_accuracy_by_scope"].items():
            for name, event in events.items():
                for unit in ("raw", "per_1k_inst"):
                    values = event[unit]
                    perf_lines.append(f"| {scope_name} / {name} | {unit} | "
                                      f"{event['scored_cases']}/{event['cases']} | {event['unavailable_cases']} | "
                                      f"{values['finite_ape_cases']}/{values['cases']} | "
                                      f"{percent_text(values['mape_percent'])} | {percent_text(values['p50_ape_percent'])} | "
                                      f"{percent_text(values['p90_ape_percent'])} | {percent_text(values['p99_ape_percent'])} | "
                                      f"{percent_text(values['wape_percent'])} | {percent_text(values['signed_bias_percent'])} |")
        (args.output_dir / "perf-retirement-summary.md").write_text("\n".join(perf_lines) + "\n")
    if getattr(args, "perf_cache", False):
        cache_lines = ["# Ten hardware-bound cache PMU fields", "",
            "hardware equivalence: unverified; unavailable events are not scored. Native tag misses are separate diagnostics.", "",
            "| scope / field | unit | scored / cases | unavailable | finite APE | MAPE | max APE | WAPE | signed bias |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for scope_name, values in summary["perf_cache_accuracy_by_scope"].items():
            for name, event in values["events"].items():
                for unit in ("raw", "per_1k_inst"):
                    v = event[unit]
                    cache_lines.append(f"| {scope_name} / {name} | {unit} | {event['scored_cases']}/{event['cases']} | "
                        f"{event['unavailable_cases']} | {v['finite_ape_cases']} | {percent_text(v['mape_percent'])} | "
                        f"{percent_text(v['max_ape_percent'])} | {percent_text(v['wape_percent'])} | {percent_text(v['signed_bias_percent'])} |")
        (args.output_dir / "perf-cache-summary.md").write_text("\n".join(cache_lines) + "\n")
    if getattr(args,"perf_cache_estimates",False):
        lines=['# Approximate cache predictions','',
               'Hardware equivalence remains unverified. Model approximations do not suppress prediction values.',
               'Only complete compatible references enter accuracy; incomplete native references enter Diagnostic differences only.','',
               '## Estimated accuracy against complete references','',
               '| scope / field | unit | accuracy cases | MAPE | max APE | WAPE |',
               '|---|---|---:|---:|---:|---:|']
        for scope,values in summary['perf_cache_estimates_by_scope'].items():
            for name,event in values['events'].items():
                for unit,v in event['accuracy'].items():
                    lines.append(f"| {scope} / {name} | {unit} | {event['accuracy_cases']} | {percent_text(v['mape_percent'])} | {percent_text(v['max_ape_percent'])} | {percent_text(v['wape_percent'])} |")
        lines += ['', '## Diagnostic differences (not perf accuracy)','',
                  '| scope / field | unit | diagnostic cases | unavailable | mean absolute percentage difference | max difference % | weighted difference % |',
                  '|---|---|---:|---:|---:|---:|---:|']
        for scope,values in summary['perf_cache_estimates_by_scope'].items():
            for name,event in values['events'].items():
                for unit,v in event['diagnostic_differences'].items():
                    lines.append(f"| {scope} / {name} | {unit} | {event['diagnostic_cases']} | {event['unavailable_cases']} | {percent_text(v['mean_absolute_percentage_difference'])} | {percent_text(v['max_absolute_percentage_difference'])} | {percent_text(v['weighted_absolute_percentage_difference'])} |")
        if getattr(args,'perf_cache_incomplete_errors',False):
            lines += ['', '## Incomplete-evidence errors', '',
                      'Explicitly requested provisional errors against incomplete native observations. '
                      'Missing evidence is listed below; these rows do not enter complete-reference accuracy.', '',
                      '| scope / field | unit | cases | MAPE | max APE | WAPE | zero-reference/nonzero-model cases |',
                      '|---|---|---:|---:|---:|---:|---:|']
            for scope,values in summary['perf_cache_estimates_by_scope'].items():
                for name,event in values['events'].items():
                    for unit,v in event.get('incomplete_evidence_errors',{}).items():
                        lines.append(f"| {scope} / {name} | {unit} | {v['cases']} | {percent_text(v['mape_percent'])} | {percent_text(v['max_ape_percent'])} | {percent_text(v['wape_percent'])} | {v['zero_reference_nonzero_prediction_cases']} |")
            lines += ['', '### Per-case counts, errors and missing evidence', '',
                      '| case / scope / field | unit | model | native observation | signed error % | APE | absolute error | native unknown qualifications | request unknown diagnostics | missing evidence |',
                      '|---|---|---:|---:|---:|---:|---:|---:|---|---|']
            for report in reports:
                for scope,compared in report.get('perf_cache_validation',{}).get('estimated_scopes',{}).items():
                    for name,row in compared['events'].items():
                        if 'error_evidence' not in row:continue
                        gaps=sorted({str(g['field']) for g in row.get('evidence_gaps',[])})
                        missing='; '.join(['model: '+','.join(row['missing_model_components']),
                            'reference: '+','.join(row['missing_reference_capabilities']),
                            'source: '+','.join(gaps), 'reasons: '+','.join(row['reasons'])])
                        request_unknown=json.dumps(row.get('reference_request_unknown_diagnostics'),sort_keys=True)
                        for unit,predicted,reference,errors in [
                            ('raw',row['predicted'],row['reference'],row),
                            ('per_1k_inst',row['predicted_per_1k_inst'],row['reference_per_1k_inst'],row['per_1k_inst_errors'])]:
                            relative=errors['relative_error']
                            lines.append(f"| {report['case_id']} / {scope} / {name} | {unit} | {predicted} | {reference} | {percent_text(None if relative is None else relative*100)} | {percent_text(errors['absolute_percentage_error'])} | {errors['absolute_error']} | {row.get('reference_unknown_qualification_count')} | {request_unknown} | {missing} |")
        (args.output_dir/'perf-cache-estimates.md').write_text('\n'.join(lines)+'\n')
    if getattr(args,'retired_cache',False):write_retired_cache_summary(args.output_dir,reports)
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
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--matrix", action="append", type=Path)
    sources.add_argument("--source-root", type=Path,
                         help="Directory containing case directories with request.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fastsim", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--stats-dir", type=Path,
                        help="Rescore existing <case_id>.json files; do not run FastSim")
    parser.add_argument("--retired-cache",action="store_true",help="Compare retired load L1/L2/L3 cache events using native service facts; provisional errors retain unknown membership")
    parser.add_argument('--retired-cache-misses',action='store_true',
        help='Compare only three retired load miss point estimates; implies --retired-cache; no FB-hit accuracy gate')
    parser.add_argument("--perf-cache", action="store_true",
                        help="Audit ten hardware-bound cache event contracts and independent native request sidecars; no legacy fallback")
    parser.add_argument("--perf-cache-estimates",action="store_true",
                        help="Compare approximate predictions with content-bound inputs; incomplete references remain diagnostic differences")
    parser.add_argument('--perf-cache-incomplete-errors',action='store_true',
                        help='Report provisional errors with explicit evidence gaps; retire/request qualification is separate, structural source checks remain mandatory')
    parser.add_argument("--perf-retirement", action="store_true",
                        help="Require independent retirement sidecars and new FastSim namespace; no legacy fallback")
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
    if args.retired_cache_misses:args.retired_cache=True
    if args.perf_cache_estimates and not args.perf_cache:parser.error('--perf-cache-estimates requires --perf-cache')
    if args.perf_cache_incomplete_errors and not args.perf_cache_estimates:
        parser.error('--perf-cache-incomplete-errors requires --perf-cache-estimates')
    args.repo_root = args.repo_root.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.stats_dir is None and (args.fastsim is None or args.config is None):
        parser.error("replay requires --fastsim and --config (or use --stats-dir)")
    args.fastsim = args.fastsim.resolve() if args.fastsim else None
    args.config = args.config.resolve() if args.config else None
    args.stats_dir = args.stats_dir.resolve() if args.stats_dir else None
    args.source_root = args.source_root.resolve() if args.source_root else None
    args.event_dictionary = args.event_dictionary.resolve()
    args.matrix = [path.resolve() for path in (args.matrix or [])]
    if args.jobs <= 0 or args.expected_cases <= 0 or args.max_attempts <= 0:
        parser.error("jobs, expected-cases, and max-attempts must be positive")
    if args.poll_seconds <= 0:
        parser.error("poll-seconds must be positive")
    return args


def main():
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        discovered = discover_cases(args.matrix, args.source_root)
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
            return 0 if summary["passed_cases"] == args.expected_cases else 1
        exit_code = collection_finished(args.collection_exit_file)
        if exit_code is not None and exit_code != 0:
            return 1
        if exit_code == 0 and len(discovered) < args.expected_cases:
            return 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
