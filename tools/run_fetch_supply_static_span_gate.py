#!/usr/bin/env python3
"""Run a strict, resumable static-instruction-span accuracy gate.

Every case is reconstructed from its accuracy ``pipeline.json``.  The runner
uses the pipeline's user/kernel configs and repeats all runtime overrides that
formed the baseline. It can isolate the static instruction-span Fetch model,
the ROI-entry page-state model, or their accepted combination.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


SCOPES = ("user", "user-plus-kernel")
DRAM_SIZE = 3 * 1024 ** 3


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", action="append", type=Path, required=True)
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--core-slots", type=int, default=160)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--static-span", choices=("true", "false"), default="true",
        help="Enable the candidate or run a same-host baseline control.",
    )
    parser.add_argument(
        "--roi-entry-page-state",
        choices=("true", "false"),
        default="false",
        help="Explicitly enable or disable ROI-entry page-state replay.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        choices=SCOPES,
        help="Scope to run; repeat as needed. Defaults to both scopes.",
    )
    return parser.parse_args()


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("cannot read JSON {}: {}".format(path, error))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.recovering-{}".format(path.name, os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(str(temporary), str(path))


class CoreBudget:
    def __init__(self, capacity):
        self.capacity = capacity
        self.available = capacity
        self.condition = threading.Condition()

    def acquire(self, amount):
        amount = min(amount, self.capacity)
        with self.condition:
            while self.available < amount:
                self.condition.wait()
            self.available -= amount
        return amount

    def release(self, amount):
        with self.condition:
            self.available += amount
            self.condition.notify_all()


def task_key(task):
    return "{:02d}c/{}/{}".format(
        task["cores"], task["workload"], task["scope"]
    )


def output_path(output_root, task):
    return (
        output_root
        / "cases"
        / "{:02d}c-{}".format(task["cores"], task["workload"])
        / "{}.json".format(task["scope"])
    )


def validate_result(path, task):
    result = load_json(path)
    configuration = result.get("configuration", {})
    expected = task["pipeline"]
    failures = []
    checks = {
        "measurement_scope": task["scope"],
        "cores": task["cores"],
        "fetch_buffer_refill_latency": expected["fetch_buffer_refill_latency"],
        "fetch_supply_model": False,
        "fetch_supply_static_instruction_span": task["static_span"],
        "page_fault_roi_entry_page_state_model": task[
            "roi_entry_page_state"
        ],
        "fetch_supply_speculative_shadow": False,
        "allow_cross_page_without_virtual_token": True,
        "allow_mmio_escape": True,
    }
    for name, value in checks.items():
        if configuration.get(name) != value:
            failures.append(
                "configuration.{}={!r}, expected {!r}".format(
                    name, configuration.get(name), value
                )
            )
    dtlb = configuration.get("dtlb", {})
    if dtlb.get("miss_model") != expected["dtlb_miss_model"]:
        failures.append("DTLB miss model mismatch")
    if (
        expected["dtlb_miss_model"] == "timing_walk"
        and dtlb.get("page_walk_latency")
        != expected["dtlb_page_walk_latency"]
    ):
        failures.append("DTLB page-walk latency mismatch")
    if configuration.get("dram", {}).get("size_bytes") != DRAM_SIZE:
        failures.append("DRAM size mismatch")

    totals = result.get("totals", {})
    if totals.get("fetch_block_response_conserved") is not True:
        failures.append("Fetch response wait is not conserved")
    if totals.get("speculative_fetch_shadow_response_conserved") is not True:
        failures.append("speculative Fetch response wait is not conserved")
    cross = totals.get("fetch_supply_cross_block_instructions")
    extra = totals.get("fetch_supply_cross_block_extra_requests")
    if not isinstance(cross, int) or not isinstance(extra, int) or cross != extra:
        failures.append("cross-block instruction/request population mismatch")
    scope_metrics = result.get("scope_metrics", {})
    if scope_metrics.get("cycles_per_user_uop", 0) <= 0:
        failures.append("missing positive CPI")
    if not task["static_span"] and not task["roi_entry_page_state"]:
        accuracy_scope = task["scope"].replace("-", "_")
        formal = load_json(Path(task["accuracy"]))
        formal_cpi = formal["cycles_per_user_uop"][accuracy_scope]["predicted"]
        if scope_metrics.get("cycles_per_user_uop") != formal_cpi:
            failures.append(
                "baseline CPI {!r}, expected bit-identical {!r}".format(
                    scope_metrics.get("cycles_per_user_uop"), formal_cpi
                )
            )
    if failures:
        raise ValueError("{}: {}".format(path, "; ".join(failures)))
    return result


def build_tasks(pipelines, static_span, roi_entry_page_state, scopes):
    tasks = []
    identities = set()
    for pipeline_path in pipelines:
        pipeline_path = pipeline_path.resolve()
        pipeline = load_json(pipeline_path)
        for field in (
            "user_config",
            "effective_kernel_config",
            "dtlb_miss_model",
            "dtlb_page_walk_latency",
            "fetch_buffer_refill_latency",
            "cases",
        ):
            if field not in pipeline:
                raise ValueError("{}: missing {}".format(pipeline_path, field))
        if set(pipeline.get("measurement_scopes", [])) != set(SCOPES):
            raise ValueError("{}: incomplete scope contract".format(pipeline_path))
        for case in pipeline["cases"]:
            identity = (case["cores"], case["workload"])
            if identity in identities:
                raise ValueError("duplicate case {}".format(identity))
            identities.add(identity)
            for scope in scopes:
                tasks.append(
                    {
                        "cores": case["cores"],
                        "workload": case["workload"],
                        "scope": scope,
                        "manifest": case["manifest"],
                        "accuracy": case["accuracy"],
                        "pipeline_path": str(pipeline_path),
                        "pipeline": pipeline,
                        "static_span": static_span,
                        "roi_entry_page_state": roi_entry_page_state,
                    }
                )
    tasks.sort(key=lambda item: (-item["cores"], item["workload"], item["scope"]))
    return tasks


def command_for(fastsim, output, task):
    pipeline = task["pipeline"]
    config = (
        pipeline["user_config"]
        if task["scope"] == "user"
        else pipeline["effective_kernel_config"]
    )
    command = [
        str(fastsim),
        "simulate",
        "--config",
        config,
        "--manifest",
        task["manifest"],
        "--measurement-scope",
        task["scope"],
        "--cores",
        str(task["cores"]),
        "--allow-cross-page-without-virtual-token",
        "true",
        "--fetch-buffer-refill-latency",
        str(pipeline["fetch_buffer_refill_latency"]),
        "--dtlb-miss-model",
        pipeline["dtlb_miss_model"],
        "--allow-mmio-escape",
        "true",
        "--dram-size",
        str(DRAM_SIZE),
        "--fetch-supply-model",
        "false",
        "--fetch-supply-static-instruction-span",
        "true" if task["static_span"] else "false",
        "--page-fault-roi-entry-page-state-model",
        "true" if task["roi_entry_page_state"] else "false",
        "--fetch-supply-speculative-shadow",
        "false",
        "--output",
        str(output),
    ]
    if pipeline["dtlb_miss_model"] == "timing_walk":
        insert = command.index("--allow-mmio-escape")
        command[insert:insert] = [
            "--dtlb-page-walk-latency",
            str(pipeline["dtlb_page_walk_latency"]),
        ]
    return command


def run_task(fastsim, output_root, task, retries, budget):
    final = output_path(output_root, task)
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        validate_result(final, task)
        return {"status": "reused", "output": str(final)}
    except (OSError, ValueError):
        pass

    held = budget.acquire(task["cores"])
    try:
        last_error = None
        for attempt in range(1, retries + 2):
            recovering = final.with_name(
                ".{}.recovering-{}-{}".format(final.name, os.getpid(), attempt)
            )
            if recovering.exists():
                recovering.unlink()
            log = final.with_suffix(".log")
            command = command_for(fastsim, recovering, task)
            with log.open("a") as output:
                output.write(
                    "\n[attempt {}] {}\n".format(
                        attempt, " ".join(command)
                    )
                )
                output.flush()
                completed = subprocess.run(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                output.write("[return-code {}]\n".format(completed.returncode))
            if completed.returncode == 0:
                try:
                    validate_result(recovering, task)
                    os.replace(str(recovering), str(final))
                    result = validate_result(final, task)
                    return {
                        "status": "completed",
                        "output": str(final),
                        "cpi": result["scope_metrics"]["cycles_per_user_uop"],
                        "wall_time_seconds": result.get("wall_time_seconds"),
                    }
                except (OSError, ValueError) as error:
                    last_error = str(error)
            else:
                last_error = "FastSim exit {}".format(completed.returncode)
            if recovering.exists():
                recovering.unlink()
            if attempt <= retries:
                time.sleep(min(5 * attempt, 30))
        raise RuntimeError(last_error or "unknown FastSim failure")
    finally:
        budget.release(held)


def main():
    args = parse_args()
    if args.jobs <= 0 or args.core_slots <= 0 or args.retries < 0:
        raise SystemExit("jobs/core-slots must be positive and retries nonnegative")
    fastsim = args.fastsim.resolve()
    if not fastsim.is_file():
        raise SystemExit("missing FastSim binary: {}".format(fastsim))
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        static_span = args.static_span == "true"
        roi_entry_page_state = args.roi_entry_page_state == "true"
        scopes = tuple(args.scope or SCOPES)
        if len(set(scopes)) != len(scopes):
            raise ValueError("duplicate --scope")
        tasks = build_tasks(
            args.pipeline, static_span, roi_entry_page_state, scopes
        )
    except ValueError as error:
        raise SystemExit(str(error))
    expected_tasks = 40 * len(scopes)
    if len(tasks) != expected_tasks:
        raise SystemExit(
            "formal gate requires {} scope tasks; found {}".format(
                expected_tasks, len(tasks)
            )
        )

    state = {
        "schema": "fastsim-model-combination-gate-v2",
        "candidate": {
            "core.fetch_supply_static_instruction_span": static_span,
            "page_fault.roi_entry_page_state_model": roi_entry_page_state,
        },
        "fastsim": str(fastsim),
        "pipelines": [str(path.resolve()) for path in args.pipeline],
        "expected_tasks": len(tasks),
        "jobs": args.jobs,
        "core_slots": args.core_slots,
        "started_unix_seconds": time.time(),
        "tasks": {},
    }
    state_path = output_root / "status.json"
    atomic_json(state_path, state)
    budget = CoreBudget(args.core_slots)
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_tasks = {
            executor.submit(
                run_task, fastsim, output_root, task, args.retries, budget
            ): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(future_tasks):
            task = future_tasks[future]
            key = task_key(task)
            try:
                outcome = future.result()
            except Exception as error:  # pylint: disable=broad-except
                outcome = {"status": "failed", "error": str(error)}
                failures.append(key)
            outcome["updated_unix_seconds"] = time.time()
            state["tasks"][key] = outcome
            state["completed_tasks"] = sum(
                item.get("status") in ("completed", "reused")
                for item in state["tasks"].values()
            )
            state["failed_tasks"] = sum(
                item.get("status") == "failed"
                for item in state["tasks"].values()
            )
            atomic_json(state_path, state)
            print(
                "[gate] {}/{} {} {}".format(
                    state["completed_tasks"], len(tasks), key, outcome["status"]
                ),
                flush=True,
            )
    state["finished_unix_seconds"] = time.time()
    state["status"] = "failed" if failures else "completed"
    atomic_json(state_path, state)
    if failures:
        print("failed tasks: {}".format(", ".join(failures)), file=sys.stderr)
        return 1
    print("[gate] completed all {} tasks".format(len(tasks)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
