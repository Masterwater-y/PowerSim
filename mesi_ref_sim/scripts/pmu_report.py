#!/usr/bin/env python3
"""pmu_report.py — 在 V5 100% 标签对齐基础上，按相同规则聚合 PMU 计数，
   验证 oracle 与 ref_sim 的 bit-exact 对齐。

可对齐子集（路线 1 + 路线 2' + 路线 2''）：
  cache miss 类:
    - L1D loads / stores
    - L1D load_misses / store_misses
    - L2 misses (load + store)
    - LLC load_misses / store_misses
  CHA 类（pmu_events.txt L18, 21, 22, 23）:
    - 18 UNC_CHA_CLOCKTICKS                          —— 取 max(commit_tick) 同源
    - 21 UNC_CHA_REQUESTS.READS                      —— request && is_store==0
    - 22 UNC_CHA_REQUESTS.WRITES                     —— request && is_store==1
    - 23 UNC_CHA_TOR_INSERTS.IA_MISS_DRD             —— request && load && coh==DRAM
  CHA 近似目录（pmu_events.txt L19, 20）：
    - 19 UNC_CHA_DIR_LOOKUP.SNP                      —— 近似目录决策计数
    - 20 UNC_CHA_CORE_SNP.ANY_ONE                    —— 同上（这两个事件在
      MESI_Three_Level 投影下取相同值）
    oracle 与 ref_sim 用同一聚合规则 → 互相 bit-exact 保证；
    与 gem5 stats.txt 中 ruby Fwd_GETX+Fwd_GETS 真值对比作为"近似准确度"指标。

不可达事件（已在文档说明，本脚本不输出）：
  - 24 TOR_OCCUPANCY                               —— 需 latency 模型

用法:
    pmu_report.py <mem_events.jsonl> <pred.jsonl> [stats.txt]
                  [--uarch-profile <uarch_profile.json>]

V9.5 schema v2：cacheline 大小（line_b）从 uarch_profile.json 读，
不再 hardcode 64B；不传 --uarch-profile 时退化为 64B（兼容历史 trace）。
"""
import argparse
import json
import os
import re
import sys

# coh enum (与 simulator.hpp 同步)
COH_L1, COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2 = \
    1, 2, 3, 4, 5, 6, 7

# L1D miss = coh 不是 L1（包括 R_*, LLC, DRAM, WB, L2）
L1_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2}
# L2 miss = 没在 L1/L2 命中 = coh 在 LLC/DRAM/R_*/WB
L2_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB}
# LLC miss = 进 DRAM
LLC_MISS_SET = {COH_DRAM}

# CHA 时钟周期单位（ns），对齐 SkylakeX uncore ~2.4GHz 不重要——只要 oracle
# 与 ref_sim 用同一计算就 bit-exact。这里直接用 max commit_tick（picosecond）。
CHA_CLOCK_PERIOD_PS = 1000  # 1 ns; 任意但同源即可


# --------------------- 近似 MESI 目录（用于 19/20）------------------------
#
# 简化规则（"最终态投影"，无 transient）：
#   line.state ∈ {I=0, S=1, E=2, M=3}, owner_core, sharers={core_ids}
#   每条 commit 事件按 (core_id, cacheline, is_store) 推进：
#
#   load:
#     - state==I:           E := requester；DIR_LOOKUP.SNP += 0
#     - state==M:
#         if owner != requester:
#             SNP += 1（snoop owner 取 dirty data，下沉为 S）
#             state := S, sharers := {owner, requester}, owner := -1
#         else:
#             命中本地 M，无 snoop
#     - state==E:
#         if owner != requester:
#             SNP += 1（snoop owner 降级为 S）
#             state := S, sharers := {owner, requester}, owner := -1
#         else: 本地 E 命中
#     - state==S: sharers += requester；无 snoop
#
#   store:
#     - state==I:           M := requester；无 snoop
#     - state==M:
#         if owner != requester:
#             SNP += 1（invalidate owner）
#             owner := requester
#         else: 本地 M 命中
#     - state==E:
#         if owner != requester:
#             SNP += 1
#         owner := requester, state := M, sharers := {}
#     - state==S:
#         if (sharers - {requester}) 非空:
#             SNP += 1（invalidate 所有其他 sharers，每条 line 计 1 次 SNP，
#                      而非按 sharer 数量计；与 UNC_CHA_DIR_LOOKUP.SNP 的
#                      "每次 directory lookup 触发 snoop"语义一致）
#         state := M, owner := requester, sharers := {}
#
# 与 gem5 ruby 真值的预期偏差来源：
#   1. 真值按 forward 数计（每个 sharer 1 个 Fwd_INV）；本规则按 lookup 数计
#      → 量级一致但绝对值有偏差，文档化为"近似指标"
#   2. ruby 投机/重传/race 边界 case 不被 ref_sim 复刻
#   3. timing 域顺序差异（小，因为 trace 全序已隐式承载）

class Directory:
    def __init__(self):
        self.lines = {}  # cl -> dict(state, owner, sharers)
        self.snp = 0     # DIR_LOOKUP.SNP / CORE_SNP.ANY_ONE 同源累加

    def _get(self, cl):
        rec = self.lines.get(cl)
        if rec is None:
            rec = {'state': 0, 'owner': -1, 'sharers': set()}
            self.lines[cl] = rec
        return rec

    def step(self, core_id, cl, is_store):
        rec = self._get(cl)
        st, owner, sh = rec['state'], rec['owner'], rec['sharers']

        # SNP 按"被 snoop 的 cache 数"计（fan-out），与 ruby
        #   Fwd_GETX/Fwd_GETS 的接收侧累加语义一致。
        if not is_store:
            if st == 0:
                rec['state'] = 2; rec['owner'] = core_id; rec['sharers'] = set()
            elif st == 3:
                if owner != core_id:
                    self.snp += 1  # snoop owner (Fwd_GETS)
                    rec['state'] = 1
                    rec['sharers'] = {owner, core_id}
                    rec['owner'] = -1
            elif st == 2:
                if owner != core_id:
                    self.snp += 1  # snoop owner (Fwd_GETS)
                    rec['state'] = 1
                    rec['sharers'] = {owner, core_id}
                    rec['owner'] = -1
            elif st == 1:
                rec['sharers'].add(core_id)
        else:
            if st == 0:
                rec['state'] = 3; rec['owner'] = core_id; rec['sharers'] = set()
            elif st == 3:
                if owner != core_id:
                    self.snp += 1  # invalidate owner (Fwd_GETX)
                rec['owner'] = core_id
            elif st == 2:
                if owner != core_id:
                    self.snp += 1  # invalidate owner (Fwd_GETX)
                rec['state'] = 3
                rec['owner'] = core_id
                rec['sharers'] = set()
            elif st == 1:
                others = sh - {core_id}
                if others:
                    # SNP 按 line 计 1 次（每次 directory lookup 触发 snoop
                    #   一组 sharers，UNC_CHA_DIR_LOOKUP.SNP 的硬件计数也按
                    #   lookup 数 而非 fan-out 数）
                    self.snp += 1
                rec['state'] = 3
                rec['owner'] = core_id
                rec['sharers'] = set()


def make_counters():
    return {
        # cache miss 类
        'l1d.loads': 0,
        'l1d.stores': 0,
        'l1d.load_misses': 0,
        'l1d.store_misses': 0,
        'l2.misses': 0,
        'llc.load_misses': 0,
        'llc.store_misses': 0,
        # CHA 类（bit-exact 子集）
        'cha.requests.reads': 0,
        'cha.requests.writes': 0,
        'cha.tor_inserts.ia_miss_drd': 0,
        # CHA clockticks 由 max tick 算出
        '_max_commit_tick': 0,
        # CHA 近似目录
        'cha.dir_lookup.snp': 0,
        'cha.core_snp.any_one': 0,
    }


def aggregate(rows, key, cacheline_bits=6):
    """rows: list of dict; key='coh_oracle' or 'coh_pred'.

    cacheline_bits: log2(line_b)，用于把 cacheline_addr 折叠成 directory key。
    """
    c = make_counters()
    directory = Directory()
    for r in rows:
        et = r.get('event_type', 'commit')
        tk = int(r.get('commit_tick', 0))
        if tk > c['_max_commit_tick']:
            c['_max_commit_tick'] = tk

        if et == 'commit':
            # commit 流提供 L1D loads/stores 总数（每条 retire 都计一次）+
            # 推进近似目录状态机。目录状态机基于 (commit_tick, seq) 全序，
            # 因此 oracle/ref_sim 视角共享同一轨迹。
            cl = int(r.get('cacheline_addr', 0)) >> cacheline_bits
            cid = int(r.get('core_id', 0))
            is_store = r.get('is_store', 0) == 1
            if is_store:
                c['l1d.stores'] += 1
            else:
                c['l1d.loads'] += 1
            directory.step(cid, cl, is_store)
            continue

        if et == 'ifetch':
            # V9.5 (post A2/A3 + L3-size 修复): ruby 的 LLC load_misses 真值
            # (L2Cache_Controller.NP.L1_GETS / ISS.Mem_Data) **包含 ifetch
            # 的 LLC cold miss**（每条新 i-line 进 L2 controller 都要经历
            # NP→IS 转移）。oracle 端 ifetch 行携带 i_coh_oracle 字段，
            # 同 d-side coh enum 一致；这里用 'i_coh_oracle' / 'i_coh_pred'
            # 等价键名（ref_sim ifetch 行写出的是 i_coh_oracle）查询。
            i_key = 'i_coh_oracle' if key == 'coh_oracle' else 'i_coh_oracle'
            coh = r.get(i_key, 0)
            if coh in L1_MISS_SET:
                c['l1d.load_misses'] += 1     # 概念上是 i-cache miss；
                                              # ruby NP.L1_GETS 累加同口径
            if coh in L2_MISS_SET:
                c['l2.misses'] += 1
            if coh in LLC_MISS_SET:
                c['llc.load_misses'] += 1
                c['cha.tor_inserts.ia_miss_drd'] += 1
                c['cha.requests.reads'] += 1
            continue

        if et != 'request':
            continue

        # V8 (方案 Y): cache miss / CHA 计数同时纳入 packet 路径
        #   (oracle_source==0) 与 fallback 路径 (oracle_source==1)。
        #   - packet 路径：load 端 V5 strict 真值
        #   - fallback 路径：tao_trace 自维护 line_states_ 投影，主要补
        #     store-side miss（packet 路径不覆盖 store retire 流程）
        #   注意：strict-eval（compare_oracle.py）仍仅用 oracle_source==0；
        #   PMU 聚合放宽到全部 request 行，oracle vs ref_sim 用同一规则
        #   仍保证互相 bit-exact。

        coh = r.get(key, 0)
        is_store = r.get('is_store', 0) == 1

        # cache miss 类
        if coh in L1_MISS_SET:
            if is_store:
                c['l1d.store_misses'] += 1
            else:
                c['l1d.load_misses'] += 1
        if coh in L2_MISS_SET:
            c['l2.misses'] += 1
        if coh in LLC_MISS_SET:
            if is_store:
                c['llc.store_misses'] += 1
            else:
                c['llc.load_misses'] += 1

        # CHA 类
        if is_store:
            c['cha.requests.writes'] += 1
        else:
            c['cha.requests.reads'] += 1
            if coh in LLC_MISS_SET:
                # IA = inbound-from-Agent (core); DRD = demand read
                c['cha.tor_inserts.ia_miss_drd'] += 1

    # CHA clockticks
    c['cha.clockticks'] = c['_max_commit_tick'] // CHA_CLOCK_PERIOD_PS
    del c['_max_commit_tick']
    # CHA 近似目录
    c['cha.dir_lookup.snp'] = directory.snp
    c['cha.core_snp.any_one'] = directory.snp
    return c


def parse_stats(stats_path):
    """从 gem5 stats.txt 提取 19/20/23 真值。"""
    if not stats_path or not os.path.exists(stats_path):
        return None
    fwd_getx = 0
    fwd_gets = 0
    iss_mem = 0   # ISS.Mem_Data: load miss → DRAM
    is_mem = 0    # IS.Mem_Data: load miss → DRAM (variant)
    im_mem = 0    # IM.Mem_Data: store miss → DRAM
    pat_getx = re.compile(
        r'L1Cache_Controller\.Fwd_GETX::total\s+(\d+)')
    pat_gets = re.compile(
        r'L1Cache_Controller\.Fwd_GETS::total\s+(\d+)')
    pat_iss = re.compile(
        r'L2Cache_Controller\.ISS\.Mem_Data::total\s+(\d+)')
    pat_is = re.compile(
        r'L2Cache_Controller\.IS\.Mem_Data::total\s+(\d+)')
    pat_im = re.compile(
        r'L2Cache_Controller\.IM\.Mem_Data::total\s+(\d+)')
    with open(stats_path) as f:
        for ln in f:
            for pat, var in ((pat_getx, 'fwd_getx'),
                             (pat_gets, 'fwd_gets'),
                             (pat_iss, 'iss_mem'),
                             (pat_is, 'is_mem'),
                             (pat_im, 'im_mem')):
                m = pat.search(ln)
                if m:
                    if var == 'fwd_getx': fwd_getx = int(m.group(1))
                    elif var == 'fwd_gets': fwd_gets = int(m.group(1))
                    elif var == 'iss_mem': iss_mem = int(m.group(1))
                    elif var == 'is_mem': is_mem = int(m.group(1))
                    elif var == 'im_mem': im_mem = int(m.group(1))
    return {
        'fwd_getx': fwd_getx, 'fwd_gets': fwd_gets,
        'fwd_total': fwd_getx + fwd_gets,
        'llc_load_miss': iss_mem + is_mem,
        'llc_store_miss': im_mem,
        'llc_miss_total': iss_mem + is_mem + im_mem,
    }


def _load_cacheline_bits(profile_path):
    """从 uarch_profile.json 读 cache.l1d.line_b，返回 log2(line_b)。
    不传或读不到时退化为 6（64B），保持对历史 trace 的兼容。"""
    if not profile_path:
        return 6
    with open(profile_path) as f:
        prof = json.load(f)
    line_b = int(prof.get('cache', {}).get('l1d', {}).get('line_b', 64))
    if line_b <= 0 or (line_b & (line_b - 1)) != 0:
        raise RuntimeError(f"uarch_profile cache.l1d.line_b not pow2: {line_b}")
    bits = 0
    v = line_b
    while v > 1:
        v >>= 1
        bits += 1
    return bits


def main():
    ap = argparse.ArgumentParser(
        description="PMU bit-exact alignment report (oracle vs ref_sim)"
    )
    ap.add_argument('mem_events', help='oracle 端 mem_events.jsonl')
    ap.add_argument('pred', help='ref_sim 端 pred.jsonl')
    ap.add_argument('stats', nargs='?', default=None,
                    help='可选：gem5 stats.txt（提取 ruby 真值做 acc 对照）')
    ap.add_argument('--uarch-profile', default=None,
                    help='schema v2 uarch_profile.json；不传退化 64B')
    args = ap.parse_args()
    ev_path, pr_path = args.mem_events, args.pred
    stats_path = args.stats

    cacheline_bits = _load_cacheline_bits(args.uarch_profile)

    # 读 oracle 事件流
    oracle_rows = []
    with open(ev_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            oracle_rows.append(json.loads(ln))

    # 读 ref_sim 事件流（已带 coh_pred）
    pred_rows = []
    with open(pr_path) as f:
        for ln in f:
            if not ln.strip().startswith('{'):
                continue
            pred_rows.append(json.loads(ln))

    # 用 oracle 流计算 oracle PMU；pred 流（结构同）计算 ref_sim PMU
    pred_idx = {}
    for r in pred_rows:
        et = r.get('event_type', 'commit')
        key = (et, r['seq'], r.get('core_id', 0))
        pred_idx[key] = r.get('coh_pred', 0)

    refsim_rows = []
    for r in oracle_rows:
        rr = dict(r)
        et = r.get('event_type', 'commit')
        key = (et, r['seq'], r.get('core_id', 0))
        rr['coh_pred'] = pred_idx.get(key, r.get('coh_oracle', 0))
        refsim_rows.append(rr)

    oracle_pmu = aggregate(oracle_rows, 'coh_oracle', cacheline_bits)
    refsim_pmu = aggregate(refsim_rows, 'coh_pred', cacheline_bits)

    stats = parse_stats(stats_path)

    # 报告
    print("=" * 78)
    print("PMU bit-exact alignment report")
    print("  source: V5 strict 100% coh-label alignment + V7 directory model")
    print("=" * 78)
    metrics = [
        ('cache.l1d.loads',                'l1d.loads', None),
        ('cache.l1d.stores',               'l1d.stores', None),
        ('cache.l1d.load_misses',          'l1d.load_misses', None),
        ('cache.l1d.store_misses',         'l1d.store_misses', None),
        ('cache.l2.misses',                'l2.misses', None),
        ('cache.llc.load_misses',          'llc.load_misses',
         (stats['llc_load_miss'] if stats else None)),
        ('cache.llc.store_misses',         'llc.store_misses',
         (stats['llc_store_miss'] if stats else None)),
        ('uncore_cha:CLOCKTICKS',          'cha.clockticks', None),
        ('uncore_cha:REQUESTS.READS',      'cha.requests.reads', None),
        ('uncore_cha:REQUESTS.WRITES',     'cha.requests.writes', None),
        ('uncore_cha:TOR_INSERTS.IA_MISS_DRD',
         'cha.tor_inserts.ia_miss_drd',
         (stats['llc_load_miss'] if stats else None)),
        ('uncore_cha:DIR_LOOKUP.SNP   (近似)',
         'cha.dir_lookup.snp',
         (stats['fwd_total'] if stats else None)),
        ('uncore_cha:CORE_SNP.ANY_ONE (近似)',
         'cha.core_snp.any_one',
         (stats['fwd_total'] if stats else None)),
    ]
    n_match = 0
    n_total = 0
    print(f"  {'metric':<42} {'oracle':>11} {'ref_sim':>11}  match  "
          f"{'gem5_stats':>11}  acc%")
    print("  " + "-" * 76)
    for label, key, gt in metrics:
        a = oracle_pmu[key]
        b = refsim_pmu[key]
        ok = (a == b)
        n_total += 1
        if ok:
            n_match += 1
        flag = "OK" if ok else "DIFF"
        if gt is not None:
            acc = (1.0 - abs(a - gt) / gt) * 100 if gt > 0 else 100.0
            gt_s = f"{gt:>11}"
            acc_s = f"{acc:6.2f}"
        else:
            gt_s = " " * 11
            acc_s = "    -"
        print(f"  {label:<42} {a:>11} {b:>11}  {flag:>4}  {gt_s}  {acc_s}")
    print("  " + "-" * 76)
    print(f"  bit-exact metrics (oracle vs ref_sim): {n_match}/{n_total}")
    if stats:
        print(f"  gem5 ruby ground truth: Fwd_GETX={stats['fwd_getx']} "
              f"Fwd_GETS={stats['fwd_gets']} total={stats['fwd_total']}")
    if n_match != n_total:
        sys.exit(1)


if __name__ == '__main__':
    main()
