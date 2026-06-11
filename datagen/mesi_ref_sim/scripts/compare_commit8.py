#!/usr/bin/env python3
"""compare_commit8.py — 批量校验 ref_sim 输出的 commit 行 8 字段是否与
gem5 mem_events.merged.jsonl 中 commit 行 bit-exact 对齐。

用法:
    compare_commit8.py <mem_events.merged.jsonl> <pred.jsonl>
"""
import json
import sys
import collections

FIELDS = ["mesi_before", "coh_oracle", "sharer_bucket", "owner_dist",
          "dirty_owner", "path_class", "inval_fanout",
          "same_line_recent", "oracle_source"]


def load_commit(path):
    res = {}
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            r = json.loads(s)
            if r.get("event_type", "commit") != "commit":
                continue
            key = (r["seq"], r.get("core_id", 0))
            res[key] = r
    return res


def main():
    if len(sys.argv) != 3:
        print("Usage: compare_commit8.py <mem_events.jsonl> <pred.jsonl>",
              file=sys.stderr)
        sys.exit(2)
    truth = load_commit(sys.argv[1])
    preds = load_commit(sys.argv[2])
    n_match = 0
    n_total = 0
    per_field_mismatch = collections.Counter()
    samples = []
    for key, t in truth.items():
        p = preds.get(key)
        if p is None:
            continue
        n_total += 1
        ok = True
        for f in FIELDS:
            if t.get(f, 0) != p.get(f, 0):
                per_field_mismatch[f] += 1
                ok = False
                if len(samples) < 20:
                    samples.append((key, f, t.get(f), p.get(f)))
        if ok:
            n_match += 1
    rate = (n_match / n_total * 100) if n_total else 0.0
    print(f"commit total : {n_total}")
    print(f"all-8 match  : {n_match}  ({rate:.4f}%)")
    print(f"mismatch by field: {dict(per_field_mismatch)}")
    if samples:
        print("first mismatches:")
        for s in samples:
            print(f"  key={s[0]} field={s[1]} truth={s[2]} pred={s[3]}")
    sys.exit(0 if n_match == n_total else 1)


if __name__ == "__main__":
    main()
