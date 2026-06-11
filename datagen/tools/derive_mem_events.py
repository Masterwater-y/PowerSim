#!/usr/bin/env python3
"""从 records.micro + labels.micro 派生 all_mem_events.merged.jsonl。

同时派生 ifetch 事件：
  - 对每个 core，在 (fetch_tick, macro_pc cacheline_v) 首次出现时发一条 ifetch；
  - 该事件携带 records.micro 中已复制到每条 µop 上的 i-side oracle 字段，
    供 ref_sim 重放后与 pred.ifetch 对齐，再由 build_inference_input.py
    按 (core, i_cl, fetch_tick) 回填到对应 µop。

对每条 ld/st µop 写两行：
  { event_type: "request", seq, core_id, cacheline_addr, is_store,
    coh_oracle, oracle_source, commit_tick }
  { event_type: "commit",  seq, core_id, thread_id, vaddr, cacheline_addr,
    is_store, size, pc, commit_tick, coh_oracle, oracle_source,
    mesi_before, sharer_bucket, owner_dist, dirty_owner,
    path_class, inval_fanout, same_line_recent }
seq 按 (core_id) 内分别单调递增；request/commit 共享同一个 seq，
ifetch 使用独立 seq 命名空间。比较脚本按 event_type 过滤，不要求跨类型唯一。
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
        ifetch_seq_in_core = 0
        seen_ifetch = set()  # (fetch_tick, cacheline_addr_v)
        with open(rf) as fr, open(lf) as fl:
            for lr, ll in zip(fr, fl):
                jr = json.loads(lr)
                jl = json.loads(ll)
                ft = int(jl['fetch_tick'])
                i_cl_v = int(jr['macro_pc']) & ~63
                i_cl_p = int(jr.get('cacheline_paddr', jr.get('cacheline_addr', 0))) & ~63
                ifk = (ft, i_cl_v)
                if ifk not in seen_ifetch:
                    seen_ifetch.add(ifk)
                    ife = {
                        'seq': ifetch_seq_in_core,
                        'event_type': 'ifetch',
                        'core_id': cid,
                        'cacheline_addr': i_cl_v,
                        'cacheline_addr_v': i_cl_v,
                        'cacheline_addr_p': i_cl_p,
                        'cache_level': 4,
                        'fetch_tick': ft,
                        'commit_tick': ft,
                        'i_path_class': jr.get('i_path_class', 0),
                        'i_coh_oracle': jr.get('i_coh_oracle', 0),
                        'i_mesi_before': jr.get('i_mesi_before', 0),
                        'i_mshr_depth': jr.get('i_mshr_depth', 0),
                        'itlb_hit': jr.get('itlb_hit', 0),
                        'i_walker_levels': jr.get('i_walker_levels', 0),
                        'i_walker_dram_misses': jr.get('i_walker_dram_misses', 0),
                        'i_bank_id': jr.get('i_bank_id', 0),
                        'i_llc_set_residency': jr.get('i_llc_set_residency', 0),
                        'i_llc_set_lru_pos': jr.get('i_llc_set_lru_pos', 0),
                    }
                    rows.append((ft, 0, ifetch_seq_in_core, ife))
                    ifetch_seq_in_core += 1
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
                rows.append((ct, 1, seq, req))   # request 排 commit 前
                rows.append((ct, 2, seq, com))

    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    n_req = 0
    n_com = 0
    n_ifetch = 0
    with open(args.out, 'w') as fo:
        for _, _, _, ev in rows:
            fo.write(json.dumps(ev, separators=(',', ':')))
            fo.write('\n')
            if ev['event_type'] == 'ifetch':
                n_ifetch += 1
            elif ev['event_type'] == 'request':
                n_req += 1
            else:
                n_com += 1
    print(f"derive_mem_events: ifetch={n_ifetch} request={n_req} commit={n_com} -> {args.out}")


if __name__ == '__main__':
    main()
