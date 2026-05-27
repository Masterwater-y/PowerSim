#!/usr/bin/env python3
"""compare_oracle.py — 逐条对比 ref_simulator 的 coh_pred 与 probe 的 coh_oracle。

用法:
    compare_oracle.py <mem_events.jsonl> <pred.jsonl>
- 仅比对 oracle_source==0 的行（来自 packet 的真值）。
- oracle_source==1 行 (line-state fallback) 不参与 0-diff 校验，仅打印分布。
- 不一致样本写入 mismatch.csv（最多前 200 条）。
"""
import json
import sys
import collections
import os

COH = {0: "UNK", 1: "L1", 2: "R_CLEAN", 3: "R_DIRTY", 4: "LLC",
       5: "DRAM", 6: "WB", 7: "L2"}


def main():
    if len(sys.argv) != 3:
        print("Usage: compare_oracle.py <mem_events.jsonl> <pred.jsonl>",
              file=sys.stderr)
        sys.exit(2)
    ev_path, pr_path = sys.argv[1], sys.argv[2]
    preds = {}
    with open(pr_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            r = json.loads(ln)
            # V5: pred 流中既有 request 又有 commit；strict-eval 用 request 轴。
            #   key = (event_type, seq, core_id) —— seq 在每核内独立递增，
            #   且 request/commit 共享同一 mem_event_counter，因此 (event_type,
            #   seq) 联合后即唯一。
            # V9.5: ifetch 事件不参与 d-side coh_pred 校验（独立 i-* 字段，
            #   走 compare_ifetch.py），此处直接跳过。
            et = r.get('event_type', 'commit')
            if et == 'ifetch':
                continue
            key = (et, r['seq'], r.get('core_id', 0))
            preds[key] = r['coh_pred']

    n = 0
    n_strict = 0
    matched = 0
    mismatched = 0
    fallback = 0
    n_evict = 0
    n_prefetch = 0
    n_commit = 0
    cm = collections.Counter()  # confusion matrix (oracle, pred)
    src_dist = collections.Counter()
    mismatch_samples = []
    with open(ev_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            r = json.loads(ln)
            et = r.get('event_type', 'commit')
            # V4/V5: 静默状态更新事件 + commit 行不参与 strict-eval；
            #   strict-eval 仅在 request 轴进行（packet 真值时刻）。
            if et == 'evict':
                n_evict += 1
                continue
            if et == 'prefetch':
                n_prefetch += 1
                continue
            if et == 'commit':
                n_commit += 1
                continue
            if et == 'ifetch':
                # V9.5：i-side cache 事件，走独立 i-* 校验脚本，不计入 d-side
                continue
            # et == 'request'
            n += 1
            src_dist[r.get('oracle_source', -1)] += 1
            seq = r['seq']
            oracle = r['coh_oracle']
            key = ('request', seq, r.get('core_id', 0))
            pred = preds.get(key, -1)
            if r.get('oracle_source', -1) == 1:
                fallback += 1
                continue
            n_strict += 1
            cm[(oracle, pred)] += 1
            if oracle == pred:
                matched += 1
            else:
                mismatched += 1
                if len(mismatch_samples) < 200:
                    mismatch_samples.append(r)

    rate = (matched / n_strict * 100) if n_strict else 0.0
    print(f"request events     : {n}")
    print(f"commit events      : {n_commit}")
    print(f"silent events      : evict={n_evict} prefetch={n_prefetch}")
    print(f"oracle_source dist : {dict(src_dist)}  (0=packet,1=fallback)")
    print(f"strict-eval rows   : {n_strict}  (only request & oracle_source==0)")
    print(f"matched            : {matched}  ({rate:.4f}%)")
    print(f"mismatched         : {mismatched}")
    print(f"fallback skipped   : {fallback}")
    print()
    print("confusion matrix (oracle -> pred, count):")
    keys = sorted(set(o for o, _ in cm) | set(p for _, p in cm))
    head = "  oracle\\pred " + " ".join(f"{COH.get(k, k):>8}" for k in keys)
    print(head)
    for o in keys:
        row = f"  {COH.get(o, o):>11} " + " ".join(
            f"{cm.get((o, p), 0):>8}" for p in keys)
        print(row)
    if mismatch_samples:
        out = os.path.join(os.path.dirname(pr_path) or '.', 'mismatch.csv')
        with open(out, 'w') as g:
            g.write("seq,core,cl,is_store,oracle,pred,oracle_source,"
                    "commit_tick\n")
            for r in mismatch_samples:
                g.write(f"{r['seq']},{r['core_id']},"
                        f"{r['cacheline_addr']},{r['is_store']},"
                        f"{r['coh_oracle']},"
                        f"{preds.get(('request', r['seq'], r['core_id']), -1)},"
                        f"{r.get('oracle_source', -1)},"
                        f"{r.get('commit_tick', 0)}\n")
        print(f"\nmismatch samples (first {len(mismatch_samples)}) saved -> {out}")
    if mismatched:
        sys.exit(1)


if __name__ == '__main__':
    main()
