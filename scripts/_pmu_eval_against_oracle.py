#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
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
LLC_MISS_SET = {COH_DRAM}
CHA_CLOCK_PERIOD_PS = 1000


class Directory:
    def __init__(self) -> None:
        self.lines = {}
        self.snp = 0

    def _get(self, cl: int):
        rec = self.lines.get(cl)
        if rec is None:
            rec = {"state": 0, "owner": -1, "sharers": set()}
            self.lines[cl] = rec
        return rec

    def step(self, core_id: int, cl: int, is_store: bool) -> None:
        rec = self._get(cl)
        st, owner, sharers = rec["state"], rec["owner"], rec["sharers"]
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
                others = sharers - {core_id}
                if others:
                    self.snp += 1
                rec["state"] = 3
                rec["owner"] = core_id
                rec["sharers"] = set()


def iter_jsonl(path: Path):
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("{"):
                yield json.loads(s)


def load_cacheline_bits(profile_path: Path) -> int:
    prof = json.loads(profile_path.read_text())
    line_b = int(prof.get("cache", {}).get("l1d", {}).get("line_b", 64))
    if line_b <= 0 or (line_b & (line_b - 1)) != 0:
        raise RuntimeError(f"uarch_profile cache.l1d.line_b not pow2: {line_b}")
    bits = 0
    while line_b > 1:
        line_b >>= 1
        bits += 1
    return bits


def make_counters():
    return {
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
        "cha.clockticks": 0,
        "cha.dir_lookup.snp": 0,
        "cha.core_snp.any_one": 0,
        "_max_commit_tick": 0,
    }


def aggregate(rows, key: str, cacheline_bits: int):
    counters = make_counters()
    directory = Directory()
    for row in rows:
        event_type = row.get("event_type", "commit")
        commit_tick = int(row.get("commit_tick", 0))
        if commit_tick > counters["_max_commit_tick"]:
            counters["_max_commit_tick"] = commit_tick

        if event_type == "commit":
            cl = int(row.get("cacheline_addr", 0)) >> cacheline_bits
            cid = int(row.get("core_id", 0))
            is_store = row.get("is_store", 0) == 1
            if is_store:
                counters["l1d.stores"] += 1
            else:
                counters["l1d.loads"] += 1
            directory.step(cid, cl, is_store)
            continue

        if event_type == "ifetch":
            coh = int(row.get("i_coh_oracle", 0))
            if coh in L1_MISS_SET:
                counters["l1d.load_misses"] += 1
            if coh in L2_MISS_SET:
                counters["l2.misses"] += 1
            if coh in LLC_MISS_SET:
                counters["llc.load_misses"] += 1
                counters["cha.tor_inserts.ia_miss_drd"] += 1
                counters["cha.requests.reads"] += 1
            continue

        if event_type != "request":
            continue

        coh = int(row.get(key, 0))
        is_store = row.get("is_store", 0) == 1
        if coh in L1_MISS_SET:
            if is_store:
                counters["l1d.store_misses"] += 1
            else:
                counters["l1d.load_misses"] += 1
        if coh in L2_MISS_SET:
            counters["l2.misses"] += 1
        if coh in LLC_MISS_SET:
            if is_store:
                counters["llc.store_misses"] += 1
            else:
                counters["llc.load_misses"] += 1
        if is_store:
            counters["cha.requests.writes"] += 1
        else:
            counters["cha.requests.reads"] += 1
            if coh == COH_DRAM:
                counters["cha.tor_inserts.ia_miss_drd"] += 1

    counters["cha.clockticks"] = counters["_max_commit_tick"] // CHA_CLOCK_PERIOD_PS
    counters["cha.dir_lookup.snp"] = directory.snp
    counters["cha.core_snp.any_one"] = directory.snp
    del counters["_max_commit_tick"]
    return counters


def metric_entry(oracle: int, refsim: int):
    abs_err = refsim - oracle
    err_pct = ((abs_err / oracle) * 100.0) if oracle else (0.0 if refsim == 0 else None)
    return {
        "oracle": int(oracle),
        "refsim": int(refsim),
        "abs_err": int(abs_err),
        "err_pct": err_pct,
        "exact_match": bool(oracle == refsim),
    }


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Evaluate standalone ref_sim PMU counters against gem5 oracle mem_events."
    )
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--summary-out")
    ap.add_argument("--pred-out")
    ap.add_argument("--refsim-bin")
    return ap


def main() -> None:
    args = build_argparser().parse_args()
    dataset_dir = Path(args.dataset_dir).resolve()
    mem_events = dataset_dir / "all_mem_events.merged.jsonl"
    uarch_profile = dataset_dir / "uarch_profile.json"
    stats_path = dataset_dir / "stats.txt"
    refsim_bin = Path(args.refsim_bin).resolve() if args.refsim_bin else (
        Path(__file__).resolve().parents[1] / "infer/mesi_ref_sim/build/mesi_ref_sim"
    )
    if not mem_events.is_file():
        raise SystemExit(f"missing mem_events: {mem_events}")
    if not uarch_profile.is_file():
        raise SystemExit(f"missing uarch_profile: {uarch_profile}")
    if not refsim_bin.is_file():
        raise SystemExit(f"missing refsim bin: {refsim_bin}")

    if args.pred_out:
        pred_path = Path(args.pred_out).resolve()
        pred_path.parent.mkdir(parents=True, exist_ok=True)
        cleanup_tmp = False
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="pmu_refsim_", suffix=".jsonl", delete=False)
        pred_path = Path(tmp.name)
        tmp.close()
        cleanup_tmp = True

    subprocess.run(
        [str(refsim_bin), str(uarch_profile), str(mem_events), str(pred_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    oracle_rows = list(iter_jsonl(mem_events))
    pred_rows = list(iter_jsonl(pred_path))
    pred_idx = {}
    for row in pred_rows:
        key = (row.get("event_type", "commit"), int(row["seq"]), int(row.get("core_id", 0)))
        pred_idx[key] = int(row.get("coh_pred", 0))
    refsim_rows = []
    for row in oracle_rows:
        rr = dict(row)
        key = (row.get("event_type", "commit"), int(row["seq"]), int(row.get("core_id", 0)))
        rr["coh_pred"] = pred_idx.get(key, int(row.get("coh_oracle", 0)))
        refsim_rows.append(rr)

    cacheline_bits = load_cacheline_bits(uarch_profile)
    oracle_pmu = aggregate(oracle_rows, "coh_oracle", cacheline_bits)
    refsim_pmu = aggregate(refsim_rows, "coh_pred", cacheline_bits)

    metrics = {
        "cache.l1d.loads": metric_entry(oracle_pmu["l1d.loads"], refsim_pmu["l1d.loads"]),
        "cache.l1d.stores": metric_entry(oracle_pmu["l1d.stores"], refsim_pmu["l1d.stores"]),
        "cache.l1d.load_misses": metric_entry(oracle_pmu["l1d.load_misses"], refsim_pmu["l1d.load_misses"]),
        "cache.l1d.store_misses": metric_entry(oracle_pmu["l1d.store_misses"], refsim_pmu["l1d.store_misses"]),
        "cache.l2.misses": metric_entry(oracle_pmu["l2.misses"], refsim_pmu["l2.misses"]),
        "cache.llc.load_misses": metric_entry(oracle_pmu["llc.load_misses"], refsim_pmu["llc.load_misses"]),
        "cache.llc.store_misses": metric_entry(oracle_pmu["llc.store_misses"], refsim_pmu["llc.store_misses"]),
        "uncore_cha:CLOCKTICKS": metric_entry(oracle_pmu["cha.clockticks"], refsim_pmu["cha.clockticks"]),
        "uncore_cha:REQUESTS.READS": metric_entry(oracle_pmu["cha.requests.reads"], refsim_pmu["cha.requests.reads"]),
        "uncore_cha:REQUESTS.WRITES": metric_entry(oracle_pmu["cha.requests.writes"], refsim_pmu["cha.requests.writes"]),
        "uncore_cha:TOR_INSERTS.IA_MISS_DRD": metric_entry(
            oracle_pmu["cha.tor_inserts.ia_miss_drd"], refsim_pmu["cha.tor_inserts.ia_miss_drd"]
        ),
        "uncore_cha:DIR_LOOKUP.SNP": metric_entry(oracle_pmu["cha.dir_lookup.snp"], refsim_pmu["cha.dir_lookup.snp"]),
        "uncore_cha:CORE_SNP.ANY_ONE": metric_entry(
            oracle_pmu["cha.core_snp.any_one"], refsim_pmu["cha.core_snp.any_one"]
        ),
    }
    matched = sum(1 for v in metrics.values() if v["exact_match"])
    total = len(metrics)

    summary = {
        "dataset_dir": str(dataset_dir),
        "refsim_bin": str(refsim_bin),
        "stats_path": str(stats_path) if stats_path.is_file() else None,
        "pred_path": str(pred_path),
        "bit_exact_metrics": {"matched": matched, "total": total},
        "metrics": metrics,
    }

    summary_out = Path(args.summary_out).resolve() if args.summary_out else (dataset_dir / "pmu_validation_summary.json")
    summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"summary -> {summary_out}")

    if cleanup_tmp:
        try:
            os.unlink(pred_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
