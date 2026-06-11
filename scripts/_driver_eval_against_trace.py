#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path


COH_L1 = 1
COH_R_CLEAN = 2
COH_R_DIRTY = 3
COH_LLC = 4
COH_DRAM = 5
COH_WB = 6
COH_L2 = 7

L1_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2}
L2_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB}


class Directory:
    def __init__(self):
        self.lines = {}
        self.snp = 0

    def _get(self, cl):
        rec = self.lines.get(cl)
        if rec is None:
            rec = {"state": 0, "owner": -1, "sharers": set()}
            self.lines[cl] = rec
        return rec

    def step(self, core_id: int, cl: int, is_store: bool) -> None:
        rec = self._get(cl)
        st, owner, sh = rec["state"], rec["owner"], rec["sharers"]
        if not is_store:
            if st == 0:
                rec["state"] = 2
                rec["owner"] = core_id
                rec["sharers"] = set()
            elif st == 3:
                if owner != core_id:
                    self.snp += 1
                    rec["state"] = 1
                    rec["sharers"] = {owner, core_id}
                    rec["owner"] = -1
            elif st == 2:
                if owner != core_id:
                    self.snp += 1
                    rec["state"] = 1
                    rec["sharers"] = {owner, core_id}
                    rec["owner"] = -1
            elif st == 1:
                rec["sharers"].add(core_id)
        else:
            if st == 0:
                rec["state"] = 3
                rec["owner"] = core_id
                rec["sharers"] = set()
            elif st == 3:
                if owner != core_id:
                    self.snp += 1
                rec["owner"] = core_id
            elif st == 2:
                if owner != core_id:
                    self.snp += 1
                rec["state"] = 3
                rec["owner"] = core_id
                rec["sharers"] = set()
            elif st == 1:
                others = sh - {core_id}
                if others:
                    self.snp += 1
                rec["state"] = 3
                rec["owner"] = core_id
                rec["sharers"] = set()


def load_cacheline_bits(profile_path: Path) -> int:
    if not profile_path.is_file():
        return 6
    prof = json.loads(profile_path.read_text())
    line_b = int(prof.get("cache", {}).get("l1d", {}).get("line_b", 64))
    bits = 0
    while line_b > 1:
        line_b >>= 1
        bits += 1
    return bits


def warmup_commit_cutoffs(trace_dir: Path, warmup_records_per_core: int) -> dict[int, int]:
    """Return per-core commit_tick cutoffs for the first N records.

    Rows/events with commit_tick <= cutoff are replayed as warmup but excluded
    from oracle PMU counters.  The directory state still observes those rows.
    """
    n_warm = max(0, int(warmup_records_per_core))
    if n_warm <= 0:
        return {}
    cutoffs: dict[int, int] = {}
    for lbl_path in sorted(glob.glob(str(trace_dir / "*.labels.micro.jsonl")), key=core_id_of):
        cid = core_id_of(lbl_path)
        cutoff = None
        for idx, row in enumerate(iter_jsonl(Path(lbl_path)), start=1):
            if idx > n_warm:
                break
            cutoff = int(row.get("commit_tick", 0))
        if cutoff is not None:
            cutoffs[cid] = int(cutoff)
    return cutoffs


def aggregate_oracle_pmu(mem_events_path: Path, cacheline_bits: int,
                         warmup_cutoffs: dict[int, int] | None = None) -> dict:
    # PMU alignment is restricted to d-side (request + commit streams).
    # ifetch events are intentionally skipped because the driver consumes a
    # retire-stream functional_parquet and cannot reconstruct IFU fetch ticks.
    directory = Directory()
    c = {
        "l1d.loads": 0,
        "l1d.stores": 0,
        "l1d.load_misses": 0,
        "l1d.store_misses": 0,
        "l2.misses": 0,
        "llc.load_misses": 0,
        "llc.store_misses": 0,
        "cha.requests.reads": 0,
        "cha.requests.writes": 0,
        "cha.tor_inserts.ia_miss_drd": 0,
        "cha.dir_lookup.snp": 0,
        "cha.core_snp.any_one": 0,
    }
    for row in iter_jsonl(mem_events_path):
        et = row.get("event_type", "commit")
        cid = int(row.get("core_id", 0))
        cutoff = (warmup_cutoffs or {}).get(cid)
        count_pmu = cutoff is None or int(row.get("commit_tick", 0)) > cutoff
        if et == "commit":
            cl = int(row.get("cacheline_addr", 0)) >> cacheline_bits
            is_store = int(row.get("is_store", 0)) == 1
            snp_before = directory.snp
            directory.step(cid, cl, is_store)
            if count_pmu:
                if is_store:
                    c["l1d.stores"] += 1
                else:
                    c["l1d.loads"] += 1
                c["cha.dir_lookup.snp"] += max(0, directory.snp - snp_before)
            continue
        if et == "ifetch":
            continue
        if et != "request":
            continue
        coh = int(row.get("coh_oracle", 0))
        is_store = int(row.get("is_store", 0)) == 1
        if not count_pmu:
            continue
        if coh in L1_MISS_SET:
            if is_store:
                c["l1d.store_misses"] += 1
            else:
                c["l1d.load_misses"] += 1
        if coh in L2_MISS_SET:
            c["l2.misses"] += 1
        if coh == COH_DRAM:
            if is_store:
                c["llc.store_misses"] += 1
            else:
                c["llc.load_misses"] += 1
        if is_store:
            c["cha.requests.writes"] += 1
        else:
            c["cha.requests.reads"] += 1
            if coh == COH_DRAM:
                c["cha.tor_inserts.ia_miss_drd"] += 1
    c["cha.core_snp.any_one"] = c["cha.dir_lookup.snp"]
    return c


def metric_entry(oracle: int, driver: int) -> dict:
    abs_err = driver - oracle
    err_pct = ((abs_err / oracle) * 100.0) if oracle else (0.0 if driver == 0 else None)
    return {
        "oracle": int(oracle),
        "driver": int(driver),
        "abs_err": int(abs_err),
        "err_pct": err_pct,
        "exact_match": bool(oracle == driver),
    }


def core_id_of(path: str) -> int:
    name = os.path.basename(path)
    m = re.search(r"cores?(\d+)", name)
    if not m:
        raise ValueError(f"cannot infer core id from {path}")
    return int(m.group(1))


def iter_jsonl(path: Path):
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s.startswith("{"):
                continue
            yield json.loads(s)


def parse_wall_s(run_dir: Path) -> float | None:
    candidates = [run_dir / "time.txt", run_dir / "session.time.txt"]
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text()
        m = re.search(r"elapsed=([0-9.]+)", text)
        if m:
            return float(m.group(1))
    return None


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Evaluate driver infer.jsonl against sliced records/labels truth."
    )
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--summary-out")
    ap.add_argument("--ticks-per-cycle", type=float, default=333.0)
    ap.add_argument(
        "--eval-warmup-records-per-core",
        type=int,
        default=0,
        help=(
            "Skip the first N aligned records of each core when computing "
            "latency/CPI/branch diagnostics. The driver still consumes those "
            "rows, so ref-sim/cache/window state is warmed before measurement."
        ),
    )
    return ap


def main() -> None:
    args = build_argparser().parse_args()
    run_dir = Path(args.run_dir).resolve()
    dataset_dir = Path(args.dataset_dir).resolve()
    trace_dir = dataset_dir / "tao_trace"

    report_path = run_dir / "report.json"
    infer_path = run_dir / "infer.jsonl"
    if not report_path.is_file():
        raise SystemExit(f"missing report.json under {run_dir}")
    if not infer_path.is_file():
        raise SystemExit(f"missing infer.jsonl under {run_dir}")
    if not trace_dir.is_dir():
        raise SystemExit(f"missing tao_trace under {dataset_dir}")

    report = json.loads(report_path.read_text())
    wall_s = parse_wall_s(run_dir)
    mem_events_path = dataset_dir / "all_mem_events.merged.jsonl"
    profile_path = dataset_dir / "uarch_profile.json"

    pred = {}
    for row in iter_jsonl(infer_path):
        key = (int(row["core_id"]), int(row["thread_id"]), int(row["micro_seq"]))
        pred[key] = row

    records_files = sorted(
        glob.glob(str(trace_dir / "*.records.micro.jsonl")), key=core_id_of
    )
    labels_files = sorted(
        glob.glob(str(trace_dir / "*.labels.micro.jsonl")), key=core_id_of
    )
    if not records_files or len(records_files) != len(labels_files):
        raise SystemExit(f"records/labels files mismatch under {trace_dir}")

    aligned_rows = 0
    abs_fl = []
    abs_el = []
    truth_branch = 0
    truth_mp = 0
    pred_mp = 0
    tp = fp = fn = tn = 0

    prev_fetch = {}
    fc_pred = defaultdict(float)
    rc_pred = defaultdict(float)
    fc_truth = defaultdict(float)
    rc_truth = defaultdict(float)
    pred_macro = defaultdict(int)
    truth_macro = defaultdict(int)
    pred_mp_core = defaultdict(int)
    truth_mp_core = defaultdict(int)
    truth_branch_core = defaultdict(int)
    rows_core = defaultdict(int)
    fetch_pred_core = defaultdict(float)
    fetch_truth_core = defaultdict(float)
    fetch_abs_err_core = defaultdict(float)
    exec_pred_core = defaultdict(float)
    exec_truth_core = defaultdict(float)
    exec_abs_err_core = defaultdict(float)
    after_mispred_fetch_pred_core = defaultdict(float)
    after_mispred_fetch_truth_core = defaultdict(float)
    after_mispred_rows_core = defaultdict(int)
    prev_macro_mispred = defaultdict(bool)
    seen_core = defaultdict(int)
    measured_started = set()
    skipped_warmup_rows = defaultdict(int)

    for rec_path, lbl_path in zip(records_files, labels_files):
        cid = core_id_of(rec_path)
        with open(rec_path) as frec, open(lbl_path) as flbl:
            for rec_line, lbl_line in zip(frec, flbl):
                jr = json.loads(rec_line)
                jl = json.loads(lbl_line)
                key = (cid, int(jr["thread_id"]), int(jr["micro_seq"]))
                if key not in pred:
                    continue
                seen_core[cid] += 1
                if seen_core[cid] <= args.eval_warmup_records_per_core:
                    skipped_warmup_rows[cid] += 1
                    continue

                ft = int(jl["fetch_tick"])
                rt = int(jl["ready_tick"])
                kt = (cid, int(jr["thread_id"]))
                first_measured_for_thread = kt not in measured_started
                if first_measured_for_thread:
                    # Start the measured window at this row's fetch point.
                    # The driver still used previous rows to warm state, but
                    # the cross-boundary inter-fetch gap is not part of the
                    # measured interval.
                    fl_truth = 0.0
                    measured_started.add(kt)
                else:
                    fl_truth = (ft - prev_fetch[kt]) / float(args.ticks_per_cycle)
                prev_fetch[kt] = ft
                el_truth = (rt - ft) / float(args.ticks_per_cycle)

                p = pred[key]
                fl_pred = 0.0 if first_measured_for_thread else float(p["fetch_lat"])
                el_pred = float(p["exec_lat"])
                mp_prob = float(p.get("mispred", 0.0))
                is_macro = int(jr.get("is_last_microop", 0)) > 0 or int(jr.get("is_microop", 0)) == 0

                abs_fl.append(abs(fl_pred - fl_truth))
                abs_el.append(abs(el_pred - el_truth))
                aligned_rows += 1
                rows_core[cid] += 1
                fetch_pred_core[cid] += fl_pred
                fetch_truth_core[cid] += fl_truth
                fetch_abs_err_core[cid] += abs(fl_pred - fl_truth)
                exec_pred_core[cid] += el_pred
                exec_truth_core[cid] += el_truth
                exec_abs_err_core[cid] += abs(el_pred - el_truth)

                if prev_macro_mispred[kt]:
                    after_mispred_fetch_pred_core[cid] += fl_pred
                    after_mispred_fetch_truth_core[cid] += fl_truth
                    after_mispred_rows_core[cid] += 1

                if is_macro:
                    pred_macro[kt] += 1
                    truth_macro[kt] += 1

                fc_pred[kt] += fl_pred
                rc_pred[kt] = max(rc_pred[kt], fc_pred[kt] + el_pred)
                fc_truth[kt] += fl_truth
                rc_truth[kt] = max(rc_truth[kt], fc_truth[kt] + el_truth)

                if int(jr.get("is_branch", 0)) > 0 and is_macro:
                    truth_branch += 1
                    truth_branch_core[cid] += 1
                    truth = int(jl.get("mispredicted", 0))
                    hard = 1 if mp_prob >= 0.5 else 0
                    truth_mp += truth
                    pred_mp += hard
                    truth_mp_core[cid] += truth
                    pred_mp_core[cid] += hard
                    if truth and hard:
                        tp += 1
                    elif truth and not hard:
                        fn += 1
                    elif (not truth) and hard:
                        fp += 1
                    else:
                        tn += 1

                if is_macro:
                    prev_macro_mispred[kt] = (
                        int(jr.get("is_branch", 0)) > 0
                        and int(jl.get("mispredicted", 0)) > 0
                    )

    if not aligned_rows:
        raise SystemExit("no aligned rows found between infer.jsonl and tao_trace")

    sum_cycles_pred = sum(rc_pred.values())
    sum_cycles_truth = sum(rc_truth.values())
    sum_macro_pred = sum(pred_macro.values())
    sum_macro_truth = sum(truth_macro.values())
    cpi_pred = sum_cycles_pred / sum_macro_pred if sum_macro_pred else None
    cpi_truth = sum_cycles_truth / sum_macro_truth if sum_macro_truth else None
    cpi_err_pct = (
        (cpi_pred - cpi_truth) / cpi_truth * 100.0 if cpi_pred is not None and cpi_truth else None
    )

    fetch_lat_mae = sum(abs_fl) / len(abs_fl)
    fetch_lat_rmse = math.sqrt(sum(v * v for v in abs_fl) / len(abs_fl))
    exec_lat_mae = sum(abs_el) / len(abs_el)
    exec_lat_rmse = math.sqrt(sum(v * v for v in abs_el) / len(abs_el))

    per_core = {}
    per_core_diagnostics = {}
    for cid in sorted({cid for cid, _ in rc_pred.keys()}):
        cyc_pred = sum(v for (c, _), v in rc_pred.items() if c == cid)
        cyc_truth = sum(v for (c, _), v in rc_truth.items() if c == cid)
        mac_pred = sum(v for (c, _), v in pred_macro.items() if c == cid)
        mac_truth = sum(v for (c, _), v in truth_macro.items() if c == cid)
        exec_ready_tail_pred = cyc_pred - fetch_pred_core[cid]
        exec_ready_tail_truth = cyc_truth - fetch_truth_core[cid]
        cpi_pred_core = cyc_pred / mac_pred if mac_pred else None
        cpi_truth_core = cyc_truth / mac_truth if mac_truth else None
        per_core[str(cid)] = {
            "cycles_pred": cyc_pred,
            "cycles_truth": cyc_truth,
            "macro_pred": mac_pred,
            "macro_truth": mac_truth,
            "cpi_pred": cpi_pred_core,
            "cpi_truth": cpi_truth_core,
            "cpi_err_pct": (
                (cpi_pred_core - cpi_truth_core) / cpi_truth_core * 100.0
                if cpi_pred_core is not None and cpi_truth_core
                else None
            ),
            "branch_pred_mispred": pred_mp_core[cid],
            "branch_truth_mispred": truth_mp_core[cid],
            "branch_truth_committed": truth_branch_core[cid],
            "branch_truth_miss_rate": (
                truth_mp_core[cid] / truth_branch_core[cid]
                if truth_branch_core[cid]
                else None
            ),
            "aligned_rows": rows_core[cid],
            "fetch_sum_pred": fetch_pred_core[cid],
            "fetch_sum_truth": fetch_truth_core[cid],
            "fetch_sum_deficit": fetch_pred_core[cid] - fetch_truth_core[cid],
            "fetch_sum_err_pct": (
                (fetch_pred_core[cid] - fetch_truth_core[cid]) / fetch_truth_core[cid] * 100.0
                if fetch_truth_core[cid]
                else None
            ),
            "fetch_mae": (
                fetch_abs_err_core[cid] / rows_core[cid]
                if rows_core[cid]
                else None
            ),
            "exec_sum_pred": exec_pred_core[cid],
            "exec_sum_truth": exec_truth_core[cid],
            "exec_sum_deficit": exec_pred_core[cid] - exec_truth_core[cid],
            "exec_sum_err_pct": (
                (exec_pred_core[cid] - exec_truth_core[cid]) / exec_truth_core[cid] * 100.0
                if exec_truth_core[cid]
                else None
            ),
            "exec_mae": (
                exec_abs_err_core[cid] / rows_core[cid]
                if rows_core[cid]
                else None
            ),
            "exec_ready_tail_pred": exec_ready_tail_pred,
            "exec_ready_tail_truth": exec_ready_tail_truth,
            "exec_ready_tail_deficit": exec_ready_tail_pred - exec_ready_tail_truth,
            "exec_ready_tail_err_pct": (
                (exec_ready_tail_pred - exec_ready_tail_truth)
                / exec_ready_tail_truth
                * 100.0
                if exec_ready_tail_truth
                else None
            ),
            "cycle_deficit": cyc_pred - cyc_truth,
            "cycle_deficit_pct": (
                (cyc_pred - cyc_truth) / cyc_truth * 100.0
                if cyc_truth
                else None
            ),
            "after_mispred_rows": after_mispred_rows_core[cid],
            "after_mispred_fetch_sum_pred": after_mispred_fetch_pred_core[cid],
            "after_mispred_fetch_sum_truth": after_mispred_fetch_truth_core[cid],
            "after_mispred_fetch_sum_deficit": (
                after_mispred_fetch_pred_core[cid] - after_mispred_fetch_truth_core[cid]
            ),
            "after_mispred_fetch_sum_err_pct": (
                (
                    after_mispred_fetch_pred_core[cid] - after_mispred_fetch_truth_core[cid]
                )
                / after_mispred_fetch_truth_core[cid]
                * 100.0
                if after_mispred_fetch_truth_core[cid]
                else None
            ),
        }
        per_core_diagnostics[str(cid)] = {
            "aligned_rows": rows_core[cid],
            "macro_pred": mac_pred,
            "macro_truth": mac_truth,
            "fetch": {
                "pred_sum": fetch_pred_core[cid],
                "truth_sum": fetch_truth_core[cid],
                "deficit": fetch_pred_core[cid] - fetch_truth_core[cid],
                "err_pct": (
                    (fetch_pred_core[cid] - fetch_truth_core[cid]) / fetch_truth_core[cid] * 100.0
                    if fetch_truth_core[cid]
                    else None
                ),
                "mae": (
                    fetch_abs_err_core[cid] / rows_core[cid]
                    if rows_core[cid]
                    else None
                ),
            },
            "exec": {
                "pred_sum": exec_pred_core[cid],
                "truth_sum": exec_truth_core[cid],
                "deficit": exec_pred_core[cid] - exec_truth_core[cid],
                "err_pct": (
                    (exec_pred_core[cid] - exec_truth_core[cid]) / exec_truth_core[cid] * 100.0
                    if exec_truth_core[cid]
                    else None
                ),
                "mae": (
                    exec_abs_err_core[cid] / rows_core[cid]
                    if rows_core[cid]
                    else None
                ),
                "ready_tail_pred": exec_ready_tail_pred,
                "ready_tail_truth": exec_ready_tail_truth,
                "ready_tail_deficit": exec_ready_tail_pred - exec_ready_tail_truth,
                "ready_tail_err_pct": (
                    (exec_ready_tail_pred - exec_ready_tail_truth)
                    / exec_ready_tail_truth
                    * 100.0
                    if exec_ready_tail_truth
                    else None
                ),
            },
            "cycle": {
                "pred": cyc_pred,
                "truth": cyc_truth,
                "deficit": cyc_pred - cyc_truth,
                "err_pct": (
                    (cyc_pred - cyc_truth) / cyc_truth * 100.0
                    if cyc_truth
                    else None
                ),
                "cpi_pred": cpi_pred_core,
                "cpi_truth": cpi_truth_core,
            },
            "branch": {
                "truth_committed": truth_branch_core[cid],
                "truth_mispred": truth_mp_core[cid],
                "pred_mispred": pred_mp_core[cid],
                "truth_miss_rate": (
                    truth_mp_core[cid] / truth_branch_core[cid]
                    if truth_branch_core[cid]
                    else None
                ),
            },
            "after_mispred_fetch": {
                "rows": after_mispred_rows_core[cid],
                "pred_sum": after_mispred_fetch_pred_core[cid],
                "truth_sum": after_mispred_fetch_truth_core[cid],
                "deficit": (
                    after_mispred_fetch_pred_core[cid] - after_mispred_fetch_truth_core[cid]
                ),
                "err_pct": (
                    (
                        after_mispred_fetch_pred_core[cid]
                        - after_mispred_fetch_truth_core[cid]
                    )
                    / after_mispred_fetch_truth_core[cid]
                    * 100.0
                    if after_mispred_fetch_truth_core[cid]
                    else None
                ),
            },
        }

    throughput = {
        "rows": int(report["rows"]),
        "wall_s": wall_s,
        "rows_per_s": (float(report["rows"]) / wall_s) if wall_s else None,
    }

    oracle_pmu = None
    driver_pmu_eval = None
    driver_pmu = report.get("coord_counters", {}).get("pmu")
    if mem_events_path.is_file() and isinstance(driver_pmu, dict):
        oracle_warmup = int(getattr(args, "eval_warmup_records_per_core", 0))
        report_warmup = int(report.get("refsim_warmup_records_per_core", 0) or 0)
        if report_warmup and report_warmup != oracle_warmup:
            oracle_warmup = report_warmup
        warmup_cutoffs = warmup_commit_cutoffs(trace_dir, oracle_warmup)
        oracle_pmu = aggregate_oracle_pmu(
            mem_events_path, load_cacheline_bits(profile_path), warmup_cutoffs)
        metrics = {}
        for k, oracle_v in oracle_pmu.items():
            metrics[k] = metric_entry(int(oracle_v), int(driver_pmu.get(k, 0)))
        driver_pmu_eval = {
            "scope": (
                "d-side only (i-side excluded due to functional-trace input limitation); "
                f"first {oracle_warmup} rows/core replayed as PMU warmup"
            ),
            "warmup_records_per_core": oracle_warmup,
            "bit_exact_metrics": {
                "matched": sum(1 for v in metrics.values() if v["exact_match"]),
                "total": len(metrics),
            },
            "metrics": metrics,
        }

    summary = {
        "run_dir": str(run_dir),
        "dataset_dir": str(dataset_dir),
        "ticks_per_cycle": float(args.ticks_per_cycle),
        "eval_window": {
            "warmup_records_per_core": int(args.eval_warmup_records_per_core),
            "skipped_warmup_rows_by_core": {
                str(k): int(v) for k, v in sorted(skipped_warmup_rows.items())
            },
            "note": (
                "aligned_truth_eval/per_core diagnostics cover only rows after "
                "the per-core warmup skip. With timing-functional refsim warmup "
                "enabled, PMU counters also exclude the warmup prefix."
            ),
        },
        "throughput": throughput,
        "driver_report": {
            "cpi_macro_driver_native": report.get("cpi_macro"),
            "total_macro_driver_native": report.get("total_macro"),
            "total_cycle_driver_native": report.get("total_cycle"),
            "quantum_cycles": report.get("quantum_cycles"),
            "k_max": report.get("k_max"),
            "coord_counters": report.get("coord_counters"),
        },
        "aligned_truth_eval": {
            "aligned_rows": aligned_rows,
            "fetch_lat_mae": fetch_lat_mae,
            "fetch_lat_rmse": fetch_lat_rmse,
            "exec_lat_mae": exec_lat_mae,
            "exec_lat_rmse": exec_lat_rmse,
            "cpi_pred_sumsum": cpi_pred,
            "cpi_truth_sumsum": cpi_truth,
            "cpi_err_pct": cpi_err_pct,
            "cycles_pred_sum": sum_cycles_pred,
            "cycles_truth_sum": sum_cycles_truth,
            "macro_pred_sum": sum_macro_pred,
            "macro_truth_sum": sum_macro_truth,
            "branch_mispred_pred": pred_mp,
            "branch_mispred_truth": truth_mp,
            "branch_mispred_abs_err": pred_mp - truth_mp,
            "branch_mispred_err_pct": (
                ((pred_mp - truth_mp) / truth_mp) * 100.0 if truth_mp else None
            ),
            "branch_truth_committed": truth_branch,
            "precision": (tp / (tp + fp)) if (tp + fp) else None,
            "recall": (tp / (tp + fn)) if (tp + fn) else None,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
        },
        "per_core": per_core,
        "per_core_diagnostics": per_core_diagnostics,
    }
    if oracle_pmu is not None:
        summary["oracle_pmu"] = oracle_pmu
    if driver_pmu_eval is not None:
        summary["driver_pmu_eval"] = driver_pmu_eval

    summary_out = Path(args.summary_out).resolve() if args.summary_out else (run_dir / "driver_validation_summary.json")
    summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True))

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"summary -> {summary_out}")


if __name__ == "__main__":
    main()
