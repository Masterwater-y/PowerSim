#!/usr/bin/env python3
"""Formal-only driver for a frozen common-end experiment.

The maintained collector runs preparation -> smoke -> formal and refuses to
start formal unless every smoke case passes. This driver skips ONLY that smoke
gate: it reuses the already-prepared checkpoints in an existing output
directory and runs the same capture_task/finalize/validate path for the formal
phase. It does not modify the frozen binary, tools, matrix, or any smoke
artifact, and it writes only under <output>/formal. Completed formal cases are
resumed like the main collector.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import collect_uarch_common_end as collector
from collect_uarch_common_end import (
    GIB, POLICY, load, now, save, emit, exclusive, complete_set, check_snapshot,
)


def parse_args():
    sys.argv = [sys.argv[0], "run", *sys.argv[1:]]
    args = collector.parse_args()
    if args.action != "run":
        raise SystemExit("internal argument construction error")
    return args


def load_preparation(output, tasks):
    preparation = {}
    missing = []
    for task in tasks:
        receipt = output / "preparation" / task["case_id"] / "prepared.json"
        if not receipt.is_file():
            missing.append(task["case_id"])
            continue
        preparation[task["case_id"]] = load(receipt)
    if missing:
        raise SystemExit(
            "missing prepared checkpoints; run the main collector preparation "
            "first: " + ", ".join(missing[:8]) +
            (f" ... (+{len(missing)-8} more)" if len(missing) > 8 else ""))
    return preparation


def run_formal(args):
    matrix = load(args.matrix)
    collector.validate_design(matrix)
    report = collector.preflight(args, matrix)
    collector.freeze_runtime(args, matrix, report)
    target = matrix["target_records_per_core"]
    tasks = collector.tasks_for(matrix)
    case_count = len(tasks)
    preparation = load_preparation(args.output, tasks)

    phase_root = args.output / "formal"
    marker = phase_root / "collection-finished.json"
    if marker.exists() and load(marker).get("state") == "complete":
        emit(f"formal already complete: {marker}")
        return

    supervisor = collector.Supervisor()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: supervisor.stop())

    save(args.output / "collector-pid.json",
         dict(pid=os.getpid(),
              start_ticks=collector.pid_identity(os.getpid()),
              mode="formal-only"))
    save(args.output / "collector-state.json",
         dict(state="running", phase="formal", mode="formal-only",
              completed=0, total=case_count, updated=now()))
    save(marker, dict(state="incomplete", expected_cases=case_count,
                      mode="formal-only", updated=now()))

    def disk_guard():
        import shutil
        while not supervisor.stopped.wait(5):
            if shutil.disk_usage(args.output).free < args.disk_reserve_gib * GIB:
                emit("disk reserve reached; stopping children")
                supervisor.stop()

    threading.Thread(target=disk_guard, daemon=True).start()

    extra = {p.name for p in (phase_root / "source").glob("*") if p.is_dir()} \
        - {t["case_id"] for t in tasks}
    if extra:
        raise ValueError(f"unexpected source cases in formal: {sorted(extra)}")

    slots = threading.Semaphore(report["finalize_jobs"])
    states, rows = {}, []
    emit(f"formal-only start: {case_count} cases, sample={report['jobs']}, "
         f"finalize={report['finalize_jobs']}, target={target}")
    try:
        with ThreadPoolExecutor(max_workers=report["jobs"]) as pool:
            pending = {pool.submit(collector.capture_task, args, matrix, task,
                                   preparation[task["case_id"]], "formal", target,
                                   supervisor, slots): task for task in tasks}
            for future in as_completed(pending):
                task = pending[future]
                try:
                    row = future.result()
                    states[task["case_id"]] = "complete"
                except (Exception, SystemExit) as error:
                    states[task["case_id"]] = "failed"
                    row = dict(case_id=task["case_id"], state="failed",
                               error=str(error))
                rows.append(row)
                done = sum(v == "complete" for v in states.values())
                emit(f"formal {len(states)}/{case_count} complete={done}: "
                     f"{task['case_id']} {states[task['case_id']]}")
                save(phase_root / "inventory.json",
                     dict(cases=rows, states=states, expected_cases=case_count,
                          mode="formal-only", updated=now()))
                save(args.output / "collector-state.json",
                     dict(state="running", phase="formal", mode="formal-only",
                          completed=done, processed=len(states), total=case_count,
                          updated=now()))

        check_snapshot(Path("/"),
                       {p.lstrip("/"): v for p, v in report["inputs"].items()})
        if not complete_set([t["case_id"] for t in tasks], states):
            raise RuntimeError(
                "formal incomplete; inspect formal/inventory.json and rerun "
                "this driver to resume")
        save(marker, dict(state="complete", cases=case_count, mode="formal-only",
                          finished=now()))
        save(args.output / "collector-state.json",
             dict(state="complete", phase="formal", formal_cases=case_count,
                  smoke_skipped=True, finished=now()))
        emit(f"formal-only complete: {case_count}/{case_count}")
    except (Exception, SystemExit) as error:
        save(args.output / "collector-state.json",
             dict(state="stopped" if supervisor.stopped.is_set() else "failed",
                  phase="formal", mode="formal-only", error=str(error),
                  finished=now()))
        raise
    finally:
        supervisor.stop()


def main():
    args = parse_args()
    with exclusive(args.output):
        run_formal(args)


if __name__ == "__main__":
    main()
