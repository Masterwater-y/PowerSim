#!/usr/bin/env python3
"""compare_ifetch.py — V9.5 i-cache 4 字段 bit-exact 校验。

oracle 端 ifetch 行字段：i_path_class / i_coh_oracle / i_mesi_before
ref_sim 端 ifetch 行字段：i_path_class / i_coh_oracle / i_mesi_before /
                          i_oracle_source

对齐 key: (seq, core_id)；逐字段 0-diff，否则记录 mismatch。

用法:
    compare_ifetch.py <mem_events.jsonl> <pred.jsonl>
"""
import collections
import json
import os
import sys

FIELDS = ('i_path_class', 'i_coh_oracle', 'i_mesi_before')


def main():
    if len(sys.argv) != 3:
        print("Usage: compare_ifetch.py <mem_events.jsonl> <pred.jsonl>",
              file=sys.stderr)
        sys.exit(2)
    ev_path, pr_path = sys.argv[1], sys.argv[2]
    preds = {}
    with open(pr_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            r = json.loads(ln)
            if r.get('event_type') != 'ifetch':
                continue
            preds[(r['seq'], r.get('core_id', 0))] = r

    n = matched = mismatched = 0
    per_field_mismatch = collections.Counter()
    samples = []
    with open(ev_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            r = json.loads(ln)
            if r.get('event_type') != 'ifetch':
                continue
            n += 1
            key = (r['seq'], r.get('core_id', 0))
            p = preds.get(key)
            if p is None:
                mismatched += 1
                per_field_mismatch['__missing__'] += 1
                if len(samples) < 50:
                    samples.append((key, r, None))
                continue
            ok = True
            for fld in FIELDS:
                if int(r.get(fld, -1)) != int(p.get(fld, -2)):
                    per_field_mismatch[fld] += 1
                    ok = False
            if ok:
                matched += 1
            else:
                mismatched += 1
                if len(samples) < 50:
                    samples.append((key, r, p))

    rate = (matched / n * 100) if n else 0.0
    print(f"ifetch events     : {n}")
    print(f"matched           : {matched}  ({rate:.4f}%)")
    print(f"mismatched        : {mismatched}")
    print(f"per-field misses  : {dict(per_field_mismatch)}")
    if samples:
        out = os.path.join(os.path.dirname(pr_path) or '.', 'mismatch.ifetch.csv')
        with open(out, 'w') as g:
            g.write("seq,core,cl,o_path,o_coh,o_mesi,p_path,p_coh,p_mesi\n")
            for key, r, p in samples:
                seq, cid = key
                cl = r.get('cacheline_addr', 0)
                o = (r.get('i_path_class', -1), r.get('i_coh_oracle', -1),
                     r.get('i_mesi_before', -1))
                pp = (p.get('i_path_class', -1) if p else -1,
                      p.get('i_coh_oracle', -1) if p else -1,
                      p.get('i_mesi_before', -1) if p else -1)
                g.write(f"{seq},{cid},{cl},"
                        f"{o[0]},{o[1]},{o[2]},"
                        f"{pp[0]},{pp[1]},{pp[2]}\n")
        print(f"mismatch samples  -> {out}")
    if mismatched:
        sys.exit(1)


if __name__ == '__main__':
    main()
