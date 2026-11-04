"""Evidence-gated active CPI and independent target elapsed-time accounting.

Legacy CPL classes are a privilege/path classifier, not task ownership or an
unhalted-CPU oracle. Their conserved tick windows can prove elapsed time but
never establish exact active cycles. No time from this module enters replay.
"""
import json
import math
from pathlib import Path

FASTSIM_SCHEMA = "fastsim-time-accounting-v1"
GEM5_SCHEMA = "gem5-time-accounting-v1"
TASK_ACCOUNTING_CONTRACT = "task-lifecycle-basecpu-active-v1"
TASK_COUNTER_DOMAIN = "gem5-basecpu-active-cycles-v1"
TICK_FIELDS = (
    "user_ticks", "syscall_kernel_ticks", "page_fault_kernel_ticks",
    "irq_kernel_ticks", "scheduler_kernel_ticks", "idle_ticks",
    "unknown_kernel_ticks",
)


def finite_nonnegative(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def relative_error(predicted, reference):
    if predicted is None or reference is None:
        return None
    return (predicted - reference) / reference if reference else (
        0.0 if not predicted else None)


def build_gem5_time_accounting(rows, *, ticks_per_second, cores,
                               common_start_tick=None, common_end_tick=None):
    """Conserve per-core tick windows, requiring an explicit shared ROI.

    Identical recorded starts/ends are sufficient for a shared window. Start
    skew is never silently resolved using min(start). A producer's explicit
    common marker can anchor a window even when per-core first retirement
    times differ. Tick classification remains diagnostic only.
    """
    result = {
        "schema": GEM5_SCHEMA, "scope": "user-plus-kernel",
        "active_scope": "cpu-unhalted", "active_status": "proxy-cpl-classifier",
        "active_source": "taotrace-cpl-path-classifier",
        "active_cycles": None, "inactive_cycles": None, "unknown_cycles": None,
        "elapsed_status": "unavailable-tick-evidence",
        "elapsed_source": "cpl_class.jsonl:conserved-common-window",
        "elapsed_reference_cycles": None, "elapsed_time_seconds": None,
        "reference_frequency_hz": None, "ticks_per_second": ticks_per_second,
        "common_start_tick": None, "common_end_tick": None,
        "start_skew_ticks": None, "end_skew_ticks": None,
    }
    try:
        identities = [row["core_id"] for row in rows]
        if (not isinstance(cores, int) or cores <= 0
                or len(rows) != cores or set(identities) != set(range(cores))):
            raise ValueError("missing or duplicate participants")
        if not finite_nonnegative(ticks_per_second) or ticks_per_second == 0:
            raise ValueError("missing tick frequency")
        periods, starts, ends = set(), [], []
        for row in rows:
            values = [row[field] for field in TICK_FIELDS]
            first, last, period = (row["first_tick"], row["last_tick"],
                                   row["clock_period_ticks"])
            if (not all(finite_nonnegative(v) and isinstance(v, int)
                        for v in (*values, first, last, period)) or period == 0
                    or last <= first or row.get("tick_sum_matches") not in (1, True)
                    or row.get("measured_ticks") != last - first
                    or row.get("span_ticks") != last - first
                    or sum(values) != last - first):
                raise ValueError("unconserved tick window")
            periods.add(period)
            starts.append(first)
            ends.append(last)
        if len(periods) != 1:
            raise ValueError("different core clock periods")
        result["start_skew_ticks"] = max(starts) - min(starts)
        result["end_skew_ticks"] = max(ends) - min(ends)
        result["observed_span_ticks_diagnostic"] = max(ends) - min(starts)
        period = next(iter(periods))
        result["reference_frequency_hz"] = ticks_per_second / period
        if common_start_tick is None:
            if len(set(starts)) != 1:
                result["elapsed_status"] = "unavailable-common-start"
                return result
            common_start_tick = starts[0]
        if common_end_tick is None:
            if len(set(ends)) != 1:
                result["elapsed_status"] = "unavailable-common-end"
                return result
            common_end_tick = ends[0]
        if (not all(finite_nonnegative(v) and isinstance(v, int)
                    for v in (common_start_tick, common_end_tick))
                or common_start_tick > min(starts)
                or any(end != common_end_tick for end in ends)
                or common_end_tick <= common_start_tick):
            raise ValueError("common markers do not contain the measured windows")
        elapsed_ticks = common_end_tick - common_start_tick
        result.update({
            "elapsed_status": "available", "common_start_tick": common_start_tick,
            "common_end_tick": common_end_tick,
            "elapsed_reference_cycles": elapsed_ticks / period,
            "elapsed_time_seconds": elapsed_ticks / ticks_per_second,
        })
    except (KeyError, TypeError, ValueError) as error:
        result["elapsed_reason"] = str(error)
    return result


def assemble_gem5_time_accounting(rows, *, cores):
    """Combine explicit per-core oracle windows without summing elapsed time.

    Current scheduler-hook evidence remains a proxy and supplies no exact task
    active-cycle numerator. Missing participants or disagreeing clocks fail
    closed, including when old CPL summaries would otherwise be available.
    """
    result = {
        "schema": GEM5_SCHEMA, "scope": "user-plus-kernel", "oracle_only": True,
        "active_scope": "workload-task", "active_status": "proxy-scheduler-boundary",
        "active_source": "kernel-hook-observations", "active_cycles": None,
        "inactive_cycles": None, "unknown_cycles": None,
        "elapsed_status": "unavailable-oracle-coverage", "elapsed_reference_cycles": None,
        "elapsed_time_seconds": None, "reference_frequency_hz": None,
        "elapsed_source": "common-serial-marker-and-first-core-target",
        "participants": [],
    }
    try:
        identities = [row.get("core_id") for row in rows]
        if (not isinstance(cores, int) or isinstance(cores, bool) or cores <= 0
                or len(rows) != cores
                or not all(isinstance(core, int) and not isinstance(core, bool) for core in identities)
                or set(identities) != set(range(cores))):
            raise ValueError("missing or duplicate per-core oracle participants")
        windows = set()
        for row in rows:
            if (row.get("oracle_only") is not True
                    or row.get("active_scope") != "workload-task"
                    or row.get("active_status") != "proxy-scheduler-boundary"
                    or row.get("active_cycles") is not None):
                raise ValueError("unsupported active oracle scope/status")
            reason = _elapsed_reason(row, GEM5_SCHEMA, "user-plus-kernel")
            if reason:
                raise ValueError(reason)
            values = tuple(row.get(field) for field in
                           ("common_start_tick", "common_end_tick", "clock_period_ticks", "ticks_per_second"))
            if (not all(finite_nonnegative(v) and isinstance(v, int) for v in values)
                    or values[0] <= 0 or values[1] < values[0]
                    or values[2] == 0 or values[3] == 0):
                raise ValueError("invalid explicit common clock ticks")
            start, end, period, frequency = values
            expected = ((end-start)/period, (end-start)/frequency, frequency/period)
            for field, value in zip(("elapsed_reference_cycles", "elapsed_time_seconds", "reference_frequency_hz"), expected):
                if not math.isclose(row[field], value, rel_tol=1e-6, abs_tol=1e-15):
                    raise ValueError("per-core elapsed tick/frequency conservation failed")
            if row.get("elapsed_source") != result["elapsed_source"]:
                raise ValueError("missing or inconsistent common-marker source")
            windows.add(values)
        if len(windows) != 1:
            raise ValueError("per-core common start/end/frequency mismatch")
        start, end, period, frequency = next(iter(windows))
        result.update({"elapsed_status": "available", "common_start_tick": start,
                       "common_end_tick": end, "clock_period_ticks": period,
                       "ticks_per_second": frequency, "participants": sorted(identities),
                       "elapsed_reference_cycles": (end-start)/period,
                       "elapsed_time_seconds": (end-start)/frequency,
                       "reference_frequency_hz": frequency/period})
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        result["elapsed_reason"] = str(error)
    return result


def load_gem5_time_accounting(result_dir, oracle, *, cores):
    """Prefer explicit producer accounting; read old conserved CPL only for elapsed."""
    embedded = oracle.get("time_accounting", oracle.get("aggregate", {}).get("time_accounting"))
    # Historical CPU-wide providers retain their diagnostic behavior. Workload
    # task availability must be re-established from current native evidence;
    # a copied kernel-events object has no authority to bypass that audit.
    if (isinstance(embedded, dict) and embedded.get("active_scope") != "workload-task"
            and embedded.get("accounting_contract") != TASK_ACCOUNTING_CONTRACT
            and embedded.get("counter_domain") != TASK_COUNTER_DOMAIN):
        return embedded
    result_dir = Path(result_dir)
    explicit_paths = sorted((result_dir / "oracle").glob("time-accounting-core*.json"))
    audited_path = result_dir / "oracle/thread-state-accounting.json"
    if audited_path.is_file():
        # Bind a saved audit to the current capture's conserved participant
        # clocks. A stale/malformed audit must never promote proxy clocks.
        clock = assemble_gem5_time_accounting([], cores=cores)
        try:
            expected = {f"time-accounting-core{core}.json" for core in range(cores)}
            if {path.name for path in explicit_paths} != expected:
                raise ValueError("audited accounting lacks current per-core clocks")
            clock = assemble_gem5_time_accounting(
                [json.loads(path.read_text()) for path in explicit_paths], cores=cores)
            audited = json.loads(audited_path.read_text())
            if (not isinstance(audited, dict) or audited.get("schema") != GEM5_SCHEMA
                    or audited.get("oracle_only") is not True or audited.get("valid") is not True
                    or _elapsed_reason(audited, GEM5_SCHEMA, "user-plus-kernel")
                    or clock["elapsed_status"] != "available"
                    or any(audited.get(field) != clock.get(field) for field in
                           ("participants", "common_start_tick", "common_end_tick", "clock_period_ticks",
                            "ticks_per_second", "reference_frequency_hz", "elapsed_reference_cycles",
                            "elapsed_time_seconds", "scope"))):
                raise ValueError("audit identity/common-cut clock differs from current capture")
            if audited.get("active_status") == "available":
                from thread_state_graph import load_capture_evidence
                # The maintained validator passes the case root, whose
                # functional streams live in tao_trace/. The offline auditor
                # also accepts that trace directory directly. Select the
                # unique layout with current metadata, keeping ambiguity
                # fail-closed instead of checking a different capture.
                trace_dirs = [path for path in (result_dir, result_dir / "tao_trace")
                              if any(path.glob("thread-state-metadata-core*.json"))]
                if len(trace_dirs) != 1:
                    raise ValueError("missing or ambiguous current thread-state metadata directory")
                evidence = load_capture_evidence(trace_dirs[0])
                if (evidence["graph"]["summary"].get("task_clock_contract") != TASK_ACCOUNTING_CONTRACT
                        or audited.get("audit_provenance") != evidence["provenance"]
                        or sorted(audited.get("task_ids", [])) != sorted(task["os_tid"] for task in evidence["graph"]["tasks"])):
                    raise ValueError("audit provenance differs from current certified capture")
                reason = _active_reason(audited, GEM5_SCHEMA, "user-plus-kernel",
                                        audited.get("task_macro_instructions"))
                if reason:
                    raise ValueError(reason)
                # Re-run the maintained independent native counter/population
                # audit. Cached booleans, observation ticks and a header-only
                # source fingerprint cannot certify changed ledgers or fsem.
                from audit_thread_state_accounting import audit
                current = audit(trace_dirs[0])
                if current != audited:
                    raise ValueError("native counter audit differs from current capture")
                per_core = audited.get("per_core", [])
                if (len(per_core) != cores
                        or {row.get("core_id") for row in per_core} != set(range(cores))
                        or any(not isinstance(row.get("task_macro_instructions"), int)
                               or isinstance(row["task_macro_instructions"], bool)
                               or row["task_macro_instructions"] < 0 for row in per_core)
                        or sum(row["task_macro_instructions"] for row in per_core) != audited["task_macro_instructions"]):
                    raise ValueError("audit per-core task macro population does not conserve")
            return audited
        except (OSError, AttributeError, KeyError, TypeError, ValueError) as error:
            return {**clock, "active_status": "unavailable-invalid-audit", "active_cycles": None,
                    "active_reason": str(error)}
    if explicit_paths:
        expected = {f"time-accounting-core{core}.json" for core in range(cores)}
        if {path.name for path in explicit_paths} != expected:
            return assemble_gem5_time_accounting([], cores=cores)
        try:
            return assemble_gem5_time_accounting(
                [json.loads(path.read_text()) for path in explicit_paths], cores=cores)
        except (OSError, ValueError) as error:
            result = assemble_gem5_time_accounting([], cores=cores)
            result["elapsed_reason"] = str(error)
            return result
    cpl_path = result_dir / "oracle/cpl_class.jsonl"
    if not cpl_path.is_file():
        return None
    try:
        rows = [json.loads(line) for line in cpl_path.read_text().splitlines()
                if line.strip()]
        frequency = None
        # simFreq is the run's tick frequency; never assume gem5's usual 1e12.
        for path in (result_dir / "stats.txt", result_dir / "tao_trace/stats.txt"):
            if path.is_file():
                values = {float(parts[1]) for line in path.read_text().splitlines()
                          if (parts := line.split()) and parts[0] == "simFreq"
                          and len(parts) >= 2}
                if len(values) == 1:
                    frequency = next(iter(values))
                    break
        if frequency is None:
            emitted = {row.get("ticks_per_second") for row in rows}
            if len(emitted) == 1:
                frequency = next(iter(emitted))
        return build_gem5_time_accounting(rows, ticks_per_second=frequency, cores=cores)
    except (OSError, ValueError, TypeError) as error:
        return {"schema": GEM5_SCHEMA, "scope": "user-plus-kernel",
                "active_status": "unavailable-oracle",
                "elapsed_status": "unavailable-oracle", "reason": str(error)}


def _active_reason(accounting, schema, scope, denominator):
    if not isinstance(accounting, dict) or accounting.get("schema") != schema:
        return "missing-or-unsupported-time-accounting"
    if accounting.get("scope") != scope:
        return "measurement-scope-mismatch"
    if accounting.get("active_status") != "available":
        return str(accounting.get("active_status", "missing-active-status"))
    if accounting.get("active_scope") not in ("cpu-unhalted", "workload-task"):
        return "missing-or-unsupported-active-scope"
    if accounting["active_scope"] == "workload-task":
        if accounting.get("accounting_contract") != TASK_ACCOUNTING_CONTRACT:
            return "missing-or-incompatible-accounting-contract"
        if accounting.get("counter_domain") != TASK_COUNTER_DOMAIN:
            return "missing-or-incompatible-counter-domain"
        for field in ("read_failures", "identity_failures", "unretained_hook_events"):
            value = accounting.get(field, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value != 0:
                return "incomplete-collection:" + field
        tasks = accounting.get("task_ids")
        if (accounting.get("ownership_status") != "complete"
                or not isinstance(tasks, list) or not tasks
                or not all(isinstance(task, int) and not isinstance(task, bool)
                           and task > 0 for task in tasks)
                or len(set(tasks)) != len(tasks)):
            return "unavailable-task-population"
        if (not isinstance(denominator, int) or isinstance(denominator, bool)
                or denominator <= 0):
            return "invalid-or-missing-task-macro-instructions"
    if not isinstance(accounting.get("active_source"), str) or not accounting["active_source"]:
        return "missing-active-source"
    if not finite_nonnegative(accounting.get("active_cycles")):
        return "invalid-active-cycles"
    if (not finite_nonnegative(accounting.get("unknown_cycles"))
            or accounting["unknown_cycles"] != 0):
        return "unknown-state-coverage"
    if not finite_nonnegative(denominator) or denominator == 0:
        return "invalid-macro-denominator"
    return None


def _elapsed_reason(accounting, schema, scope):
    if not isinstance(accounting, dict) or accounting.get("schema") != schema:
        return "missing-or-unsupported-time-accounting"
    if accounting.get("scope") != scope:
        return "measurement-scope-mismatch"
    if accounting.get("elapsed_status") != "available":
        return str(accounting.get("elapsed_status", "missing-elapsed-status"))
    cycles, seconds, frequency = (accounting.get("elapsed_reference_cycles"),
                                  accounting.get("elapsed_time_seconds"),
                                  accounting.get("reference_frequency_hz"))
    if (not all(finite_nonnegative(v) for v in (cycles, seconds, frequency))
            or frequency == 0 or not math.isclose(cycles / frequency, seconds,
                                                rel_tol=1e-6, abs_tol=1e-15)):
        return "invalid-or-inconsistent-elapsed-clock"
    return None


def build_time_comparison(simulation, oracle, gem5_time=None):
    """Report legacy historical CPI separately from evidence-backed active CPI."""
    scope = simulation.get("measurement_scope")
    metrics = simulation.get("scope_metrics", {})
    aggregate = oracle.get("aggregate", {})
    fast_time = simulation.get("time_accounting")
    oracle_time = oracle.get("time_accounting", aggregate.get("time_accounting"))
    current_audit_supplied = gem5_time is not None
    if not current_audit_supplied:
        gem5_time = oracle_time
    denominator = metrics.get("perf_like_cpi_denominator_instructions")
    reference_denominator = aggregate.get("user_plus_kernel_retired_instructions")
    predicted, reference = None, None
    per_core_population_match = None
    reasons = []
    # CPL-derived proxy metadata does not turn an old saved output into new
    # active evidence, or erase its explicitly historical diagnostic.
    legacy = "time_accounting" not in simulation and (
        "time_accounting" not in oracle and "time_accounting" not in aggregate)
    if legacy:
        predicted = (metrics["sum_core_cycles"] / denominator
                     if denominator and "sum_core_cycles" in metrics
                     else metrics.get("perf_like_cpi"))
        reference = aggregate.get("perf_like_cpi_user_plus_kernel")
        status = "legacy-diagnostic"
    else:
        if (isinstance(gem5_time, dict) and not current_audit_supplied and
                (gem5_time.get("active_scope") == "workload-task" or
                 gem5_time.get("accounting_contract") == TASK_ACCOUNTING_CONTRACT or
                 gem5_time.get("counter_domain") == TASK_COUNTER_DOMAIN)):
            reasons.append("gem5:missing-current-native-counter-audit")
        # Workload-task cycles and physical-core macro populations are distinct
        # scopes. Each provider must supply its own owned-task denominator;
        # absence never falls back to physical-core scope_metrics/aggregate.
        if isinstance(fast_time, dict) and fast_time.get("active_scope") == "workload-task":
            denominator = fast_time.get("task_macro_instructions")
        if isinstance(gem5_time, dict) and gem5_time.get("active_scope") == "workload-task":
            reference_denominator = gem5_time.get("task_macro_instructions")
        for label, time, schema, count in (
                ("fastsim", fast_time, FASTSIM_SCHEMA, denominator),
                ("gem5", gem5_time, GEM5_SCHEMA, reference_denominator)):
            reason = _active_reason(time, schema, scope, count)
            if reason:
                reasons.append(f"{label}:{reason}")
        if not reasons and fast_time["active_scope"] != gem5_time["active_scope"]:
            reasons.append("active-scope-mismatch")
        if (not reasons and fast_time.get("accounting_contract") != gem5_time.get("accounting_contract")):
            reasons.append("accounting-contract-mismatch")
        if (not reasons and fast_time["active_scope"] == "workload-task"
                and set(fast_time["task_ids"]) != set(gem5_time["task_ids"])):
            reasons.append("task-population-mismatch")
        if (not reasons and fast_time.get("counter_domain") != gem5_time.get("counter_domain")):
            reasons.append("counter-domain-mismatch")
        if not reasons and fast_time["active_scope"] == "workload-task" and denominator != reference_denominator:
            reasons.append("task-macro-population-mismatch")
        if not reasons and fast_time["active_scope"] == "workload-task":
            populations=[]
            for provider in (fast_time,gem5_time):
                rows=provider.get("per_core")
                if (not isinstance(rows,list) or not rows or
                    any(not isinstance(row,dict) or not isinstance(row.get("core_id"),int) or
                        isinstance(row["core_id"],bool) or row["core_id"]<0 or
                        not isinstance(row.get("task_macro_instructions"),int) or
                        isinstance(row["task_macro_instructions"],bool) or row["task_macro_instructions"]<0 for row in rows)):
                    reasons.append("missing-or-invalid-per-core-task-population");break
                population={row["core_id"]:row["task_macro_instructions"] for row in rows}
                if (set(population)!=set(range(len(rows))) or len(population)!=len(rows) or
                    sum(population.values())!=provider["task_macro_instructions"]):
                    reasons.append("unconserved-per-core-task-population");break
                populations.append(population)
            per_core_population_match = not reasons and populations[0]==populations[1]
            if not reasons and not per_core_population_match:
                reasons.append("per-core-task-macro-population-mismatch")
        if not reasons:
            predicted = fast_time["active_cycles"] / denominator
            reference = gem5_time["active_cycles"] / reference_denominator
        status = "unavailable" if reasons else "scored-active"
    error = relative_error(predicted, reference)
    cpi = {
        "status": status, "reasons": reasons, "fastsim": predicted, "gem5": reference,
        "relative_error": error, "absolute_relative_error": abs(error) if error is not None else None,
        "absolute_error": abs(predicted-reference) if predicted is not None and reference is not None else None,
        "absolute_error_unit": "cycles per macro instruction",
        "instruction_population_match": (
            metrics.get("pmu", {}).get("retired_instructions") == reference_denominator
            if legacy else (finite_nonnegative(denominator)
                            and finite_nonnegative(reference_denominator)
                            and denominator == reference_denominator
                            and (not isinstance(fast_time,dict) or fast_time.get("active_scope")!="workload-task"
                                 or per_core_population_match is True))
        ),
        "per_core_instruction_population_match": per_core_population_match,
        "active_scope": fast_time.get("active_scope") if isinstance(fast_time, dict) else None,
        "accounting_contract": fast_time.get("accounting_contract") if isinstance(fast_time, dict) else None,
        "perf_equivalence": "unverified" if isinstance(fast_time, dict) and fast_time.get("active_scope") == "workload-task" else None,
        "fastsim_source": fast_time.get("active_source") if isinstance(fast_time, dict) else "legacy-sum-core-cycles",
        "gem5_source": gem5_time.get("active_source") if isinstance(gem5_time, dict) else "legacy-kernel-events-cpl-classifier",
    }
    elapsed_reasons = []
    for label, time, schema in (("fastsim", fast_time, FASTSIM_SCHEMA),
                                ("gem5", gem5_time, GEM5_SCHEMA)):
        reason = _elapsed_reason(time, schema, scope)
        if reason:
            elapsed_reasons.append(f"{label}:{reason}")
    predicted_seconds = fast_time.get("elapsed_time_seconds") if isinstance(fast_time, dict) else None
    reference_seconds = gem5_time.get("elapsed_time_seconds") if isinstance(gem5_time, dict) else None
    elapsed_error = None if elapsed_reasons else relative_error(predicted_seconds, reference_seconds)
    elapsed = {
        "status": "unavailable" if elapsed_reasons else "scored", "reasons": elapsed_reasons,
        "fastsim_seconds": predicted_seconds, "gem5_seconds": reference_seconds,
        "absolute_error_seconds": None if elapsed_reasons else abs(predicted_seconds-reference_seconds),
        "relative_error": elapsed_error,
        "absolute_relative_error": abs(elapsed_error) if elapsed_error is not None else None,
    }
    return {"cpi": cpi, "elapsed": elapsed,
            "time_accounting": {"fastsim": fast_time, "gem5": gem5_time}}
