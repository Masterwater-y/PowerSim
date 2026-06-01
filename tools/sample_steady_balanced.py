#!/usr/bin/env python3
"""跨 workload 稳定期均衡采样器（V9.5 schema v2）。

输入：每个 workload 的 gem5 run 目录，目录下含
  tao_trace/board.processor.coresN.core.tao_trace.tao_trace.records.micro.jsonl
  tao_trace/board.processor.coresN.core.tao_trace.tao_trace.labels.micro.jsonl
records / labels 行级 1:1，与 build_micro_dataset.py 同源。

采样规则：
  - 历史上曾跳过 core0（旧版 V9.x 之前 ROI 边界不严，core0 含 ~30k
    初始化行）。**V10 之后** `m5_work_begin/end` 严格限定 ROI，core0
    在 ROI 内是 main thread 的真实业务计算（pthread_harness 让 main
    thread 直接以 tid=0 运行 worker），不再剔除。默认 business_cores
    = (0, 1, 2, 3)；如需复现旧行为可通过 --exclude-cores 0 指定。
  - 每条 (core, thread) 的稳定窗 = 行序号在
    [head_skip, 1 - tail_skip] 之间（默认 5% / 5%）。
  - 每 workload 目标行数 = total_target / n_workloads；若稳定窗容量不足
    则取尽，缺口按其他 workload 稳定窗容量比例分摊。
  - 每 workload 内目标行数按 (core, thread) 稳定窗容量比例分配；窗内
    使用 numpy.linspace 等距 stride 采样，保持原 trace 时序。
  - 输出与 build_micro_dataset.py 字段一致（meta / input / uarch_context /
    labels），fetch_latency 仍取相对原 trace 内 prev_fetch 的差值（来自
    完整窗口预扫描），exec_latency = ready_tick - fetch_tick。
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict


def core_id_of(name: str) -> int:
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


def detect_n_cores(run_dir: str) -> int:
    """从 run 目录推断当前实验的 core 数：
    扫 tao_trace/board.processor.coresN.* 文件名取 max(N)+1；
    扫不到（异常情况）回退到 4。
    注：曾尝试用 uarch_profile.json 的 cache.l1d 数量做 fallback，但该字段
    可能含 L1I+L1D+L2+L3 多 level 而被高估，已废弃。"""
    rec_files = glob.glob(os.path.join(
        run_dir, 'tao_trace',
        'board.processor.cores*.core.tao_trace.tao_trace.records.micro.jsonl'))
    cores = set()
    for f in rec_files:
        cid = core_id_of(os.path.basename(f))
        if cid >= 0:
            cores.add(cid)
    if cores:
        return max(cores) + 1
    return 4


def scan_pair(records_path, labels_path):
    """生成 (record_obj, label_obj, fetch_lat, exec_lat)；
    fetch_lat 相对原 trace 内 prev_fetch 计算。"""
    prev_fetch = {}
    with open(records_path) as fr, open(labels_path) as fl:
        for lr, ll in zip(fr, fl):
            jr = json.loads(lr)
            jl = json.loads(ll)
            tid = jr['thread_id']
            ft = jl['fetch_tick']
            rt = jl['ready_tick']
            ct = jl['commit_tick']
            if tid not in prev_fetch:
                fetch_lat = 0
                is_head = 1
            else:
                fetch_lat = ft - prev_fetch[tid]
                is_head = 1 if fetch_lat > 0 else 0
            prev_fetch[tid] = ft
            exec_lat = rt - ft
            yield jr, jl, fetch_lat, exec_lat, is_head


def collect_workload(run_dir, head_skip, tail_skip, context_warmup_skip,
                     business_cores=(0, 1, 2, 3)):
    """返回 dict[(core, tid)] -> list[(rec, lab, flat, elat)]，仅稳定窗内。

    V10 起 business_cores 默认含 core0（ROI 严格隔离后，main thread 在
    core0 上跑的也是真实业务计算）。如需复现旧"剔除 core0"行为，调用
    方传 business_cores=(1, 2, 3) 即可。"""
    by_key = defaultdict(list)
    rec_files = sorted(glob.glob(os.path.join(
        run_dir, 'tao_trace',
        'board.processor.cores*.core.tao_trace.tao_trace.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    for rf in rec_files:
        cid = core_id_of(os.path.basename(rf))
        if cid not in business_cores:
            continue
        lf = rf.replace('.records.micro.jsonl', '.labels.micro.jsonl')
        if not os.path.exists(lf):
            print(f"WARN: missing labels for core {cid}: {lf}", file=sys.stderr)
            continue
        # 第一次预扫，缓存
        rows = list(scan_pair(rf, lf))
        # 按 thread 分组并截取稳定窗
        by_tid = defaultdict(list)
        for r, l, fl, el, ih in rows:
            by_tid[r['thread_id']].append((r, l, fl, el, ih))
        for tid, lst in by_tid.items():
            n = len(lst)
            if n < 100:
                continue
            lo = max(int(n * head_skip), int(context_warmup_skip))
            hi = int(n * (1.0 - tail_skip))
            if hi - lo < 50:
                continue
            by_key[(cid, tid)] = lst[lo:hi]
    return by_key


def stride_pick(items, n_target):
    if n_target <= 0:
        return []
    n = len(items)
    if n_target >= n:
        return items
    # 等距索引
    step = n / n_target
    idx = [int(i * step) for i in range(n_target)]
    # 去重 (n_target < n, 偶发重复)
    seen = set()
    out = []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(items[i])
    return out


def emit(workload, core_id, tid, packed, out_fp, idx_in_thread):
    r, l, fl, el, ih = packed
    sample = {
        "meta": {
            "workload": workload,
            "core_id": core_id,
            "thread_id": tid,
            "micro_seq": r['micro_seq'],
            "pick_idx": idx_in_thread,
        },
        "input": {
            "macro_pc": r['macro_pc'],
            "micro_pc": r['micro_pc'],
            "vaddr": r['vaddr'],
            "size": r['size'],
            "is_load": r['is_load'],
            "is_store": r['is_store'],
            "is_atomic": r['is_atomic'],
            "is_branch": r['is_branch'],
            "is_branch_cond": r['is_branch_cond'],
            "is_branch_indirect": r['is_branch_indirect'],
            "is_call": r['is_call'],
            "is_return": r['is_return'],
            "is_int": r['is_int'],
            "is_fp": r['is_fp'],
            "is_simd": r['is_simd'],
            "is_serialize": r['is_serialize'],
            "is_microop": r['is_microop'],
            "is_last_microop": r['is_last_microop'],
            "n_src": r['n_src'],
            "n_dst": r['n_dst'],
            "producer_dists": r['producer_dists'],
            "producer_classes": r['producer_classes'],
        },
        "uarch_context": {
            "seq_num": r['seq_num'],
            "paddr": r['paddr'],
            "cacheline_addr": r['cacheline_addr'],
            # V10 方案 B：paddr-line 真值；旧 raw 缺该字段时回退到
            # cacheline_addr（vaddr-line），与下游 build_micro_dataset / pack /
            # dataset.py 的 COMPAT-OLD-50M 路径一致。全 V10+ 后该 fallback 可删。
            "cacheline_paddr": r.get('cacheline_paddr', r['cacheline_addr']),
            "mesi_before": r['mesi_before'],
            "coh_oracle": r['coh_oracle'],
            "sharer_bucket": r['sharer_bucket'],
            "owner_dist": r['owner_dist'],
            "dirty_owner": r['dirty_owner'],
            "path_class": r['path_class'],
            "inval_fanout": r['inval_fanout'],
            "same_line_recent": r['same_line_recent'],
            "oracle_source": r['oracle_source'],
            # i-side 4 字段（schema v2）
            "i_path_class": r.get('i_path_class', -1),
            "i_coh_oracle": r.get('i_coh_oracle', -1),
            "i_mesi_before": r.get('i_mesi_before', -1),
            "i_oracle_source": r.get('i_oracle_source', -1),
        },
        "labels": {
            "fetch_tick": l['fetch_tick'],
            "ready_tick": l['ready_tick'],
            "commit_tick": l['commit_tick'],
            "mispredicted": l['mispredicted'],
            "fetch_latency": fl,
            "execution_latency": el,
            "is_fetch_group_head": ih,
        },
    }
    out_fp.write(json.dumps(sample, separators=(',', ':')))
    out_fp.write('\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='append', required=True,
                    metavar='NAME=DIR',
                    help='workload 名称与 gem5 run 目录映射，可多次指定')
    ap.add_argument('--target', type=int, required=True,
                    help='总目标样本数（如 3000000）')
    ap.add_argument('--out', required=True)
    ap.add_argument('--head-skip', type=float, default=0.05)
    ap.add_argument('--tail-skip', type=float, default=0.05)
    ap.add_argument('--context-warmup-skip', type=int, default=128,
                    help='每个 (core, thread) 稳定窗额外跳过的 ROI 内前 N 条 µop')
    ap.add_argument('--exclude-cores', default='',
                    help='逗号分隔的 core_id 列表，从采样池剔除（默认空，'
                         '即 core0..3 全部纳入）。'
                         '如旧 V9.x 行为可传 --exclude-cores 0。')
    args = ap.parse_args()

    runs = []
    for r in args.run:
        if '=' not in r:
            sys.exit(f"--run 必须形如 NAME=DIR，got: {r}")
        name, dir_ = r.split('=', 1)
        runs.append((name, dir_))

    # business_cores: 自动按 run_dir 实际 core 数 - --exclude-cores
    n_cores_runs = [detect_n_cores(d) for _, d in runs]
    n_cores_max = max(n_cores_runs) if n_cores_runs else 4
    excluded = set()
    if args.exclude_cores.strip():
        for tok in args.exclude_cores.split(','):
            tok = tok.strip()
            if tok:
                excluded.add(int(tok))
    business_cores = tuple(c for c in range(n_cores_max) if c not in excluded)
    print(f"[business_cores] n_cores_per_run={n_cores_runs} -> universe=range({n_cores_max})  "
          f"excluded={sorted(excluded)}  business_cores={business_cores}",
          file=sys.stderr)

    # 1) 各 workload 收集稳定窗
    pools = {}      # name -> dict[(core, tid)] -> list
    capacity = {}   # name -> int
    for name, d in runs:
        pools[name] = collect_workload(d, args.head_skip, args.tail_skip,
                                       args.context_warmup_skip,
                                       business_cores=business_cores)
        capacity[name] = sum(len(v) for v in pools[name].values())
        print(f"[scan] {name:20s} steady-cap = {capacity[name]:>12,}",
              file=sys.stderr)

    total_cap = sum(capacity.values())
    if total_cap < args.target:
        sys.exit(f"steady capacity {total_cap:,} < target {args.target:,}")

    # 2) 每 workload 配额：先均分，cap 不足则将 deficit 按比例分摊到其余
    n = len(runs)
    base = args.target // n
    quotas = {}
    deficit = 0
    rich = []
    for name, _ in runs:
        if capacity[name] <= base:
            quotas[name] = capacity[name]
            deficit += base - capacity[name]
        else:
            quotas[name] = base
            rich.append(name)
    # 余数（target 不整除 n）补到 rich 第一个
    rem = args.target - sum(quotas.values()) - deficit
    if rem > 0 and rich:
        quotas[rich[0]] += rem
    # 把 deficit 按 rich 的剩余空间比例分摊
    while deficit > 0 and rich:
        rich_left = {n_: capacity[n_] - quotas[n_] for n_ in rich}
        total_left = sum(rich_left.values())
        if total_left <= 0:
            break
        new_rich = []
        for n_ in rich:
            share = int(round(deficit * rich_left[n_] / total_left))
            give = min(share, rich_left[n_])
            quotas[n_] += give
            deficit -= give
            if capacity[n_] > quotas[n_]:
                new_rich.append(n_)
        rich = new_rich
    # 总数对齐（int 舍入误差）
    delta = args.target - sum(quotas.values())
    for n_ in (rich + [r[0] for r in runs]):
        if delta == 0:
            break
        if capacity[n_] - quotas[n_] >= delta:
            quotas[n_] += delta
            delta = 0
    print(file=sys.stderr)
    for name, _ in runs:
        print(f"[quota] {name:20s} {quotas[name]:>10,} / {capacity[name]:>12,}",
              file=sys.stderr)
    print(f"[quota] {'TOTAL':20s} {sum(quotas.values()):>10,} / {args.target:,}",
          file=sys.stderr)

    # 3) 每 workload 内按 (core, tid) 容量比例分摊配额，stride 采样
    n_emitted = 0
    coverage = defaultdict(int)
    with open(args.out, 'w') as out_fp:
        for name, _ in runs:
            quota = quotas[name]
            if quota == 0:
                continue
            wpool = pools[name]
            keys = sorted(wpool.keys())
            sub_cap = {k: len(wpool[k]) for k in keys}
            wcap = sum(sub_cap.values())
            # 按比例分
            sub_quota = {}
            assigned = 0
            for k in keys[:-1]:
                q = int(quota * sub_cap[k] / wcap)
                q = min(q, sub_cap[k])
                sub_quota[k] = q
                assigned += q
            if keys:
                last = keys[-1]
                sub_quota[last] = min(quota - assigned, sub_cap[last])
            # 等距 stride
            for k in keys:
                picks = stride_pick(wpool[k], sub_quota[k])
                for j, packed in enumerate(picks):
                    emit(name, k[0], k[1], packed, out_fp, j)
                    n_emitted += 1
                    coverage[name] += 1

    print(file=sys.stderr)
    print(f"=== emitted total = {n_emitted:,} -> {args.out} ===",
          file=sys.stderr)
    for name, _ in runs:
        print(f"  {name:20s} {coverage[name]:>10,}", file=sys.stderr)


if __name__ == '__main__':
    main()
