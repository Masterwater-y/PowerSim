#!/usr/bin/env python3
"""从 records.micro + labels.micro 派生 all_mem_events.merged.jsonl。

仅取 ld/st µop（is_load==1 或 is_store==1），按 commit_tick 全序合并 4 核。
对每条 ld/st µop 写两行：
  { event_type: "request", seq, core_id, cacheline_addr, is_store,
    coh_oracle, oracle_source, commit_tick }
  { event_type: "commit",  seq, core_id, thread_id, vaddr, cacheline_addr,
    is_store, size, pc, commit_tick, coh_oracle, oracle_source,
    mesi_before, sharer_bucket, owner_dist, dirty_owner,
    path_class, inval_fanout, same_line_recent }
seq 按 (core_id) 内单调递增（与 V5 strict-eval 假设一致：(event_type, seq, core_id) 唯一键）。
"""
import argparse
import glob
import json
import os
import re


def core_id_of(name):
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--detailed-dir', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    rec_files = sorted(
        glob.glob(os.path.join(args.detailed_dir,
                               '*.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    lab_files = sorted(
        glob.glob(os.path.join(args.detailed_dir,
                               '*.labels.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))

    rows = []
    for rf, lf in zip(rec_files, lab_files):
        cid = core_id_of(os.path.basename(rf))
        seq_in_core = 0
        with open(rf) as fr, open(lf) as fl:
            for lr, ll in zip(fr, fl):
                jr = json.loads(lr)
                jl = json.loads(ll)
                if jr['is_load'] == 0 and jr['is_store'] == 0:
                    continue
                ct = jl['commit_tick']
                seq = seq_in_core
                seq_in_core += 1
                req = {
                    'seq': seq,
                    'event_type': 'request',
                    'core_id': cid,
                    'cacheline_addr': jr['cacheline_addr'],
                    'is_store': jr['is_store'],
                    'coh_oracle': jr['coh_oracle'],
                    'oracle_source': jr['oracle_source'],
                    'commit_tick': ct,
                }
                com = {
                    'seq': seq,
                    'event_type': 'commit',
                    'core_id': cid,
                    'thread_id': jr['thread_id'],
                    'vaddr': jr['vaddr'],
                    'cacheline_addr': jr['cacheline_addr'],
                    'is_store': jr['is_store'],
                    'size': jr['size'],
                    'pc': jr['macro_pc'],
                    'commit_tick': ct,
                    'coh_oracle': jr['coh_oracle'],
                    'oracle_source': jr['oracle_source'],
                    'mesi_before': jr['mesi_before'],
                    'sharer_bucket': jr['sharer_bucket'],
                    'owner_dist': jr['owner_dist'],
                    'dirty_owner': jr['dirty_owner'],
                    'path_class': jr['path_class'],
                    'inval_fanout': jr['inval_fanout'],
                    'same_line_recent': jr['same_line_recent'],
                }
                rows.append((ct, 0, req))   # request 排 commit 前
                rows.append((ct, 1, com))

    rows.sort(key=lambda x: (x[0], x[1]))
    n_req = 0
    n_com = 0
    with open(args.out, 'w') as fo:
        for _, _, ev in rows:
            fo.write(json.dumps(ev, separators=(',', ':')))
            fo.write('\n')
            if ev['event_type'] == 'request':
                n_req += 1
            else:
                n_com += 1
    print(f"derive_mem_events: request={n_req} commit={n_com} -> {args.out}")


if __name__ == '__main__':
    main()
