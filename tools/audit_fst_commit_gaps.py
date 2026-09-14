#!/usr/bin/env python3
"""Audit committed-head gaps from FST v7 and matching gem5 stage labels.

The caller must supply a recollection whose functional stream was verified
against the FST. micro_seq is joined through non-syscall FST records, never by
raw file position. This is an offline oracle audit, not simulator input.
"""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
import mmap
from pathlib import Path
import struct


HEADER = struct.Struct('<8sIIIIQQ4Q')
FLAGS_OP = struct.Struct('<Hh')


def overlap(a, b, lo, hi):
    return max(0, min(b, hi) - max(a, lo))


class HeadGaps:
    """Partition (first retire, last retire] into disjoint cycle categories."""

    def __init__(self):
        self.first = None
        self.previous = None
        self.rows = 0
        self.counts = Counter()
        self.blocking_heads = Counter()
        self.top_loads = []

    def add(self, fetch, issue, retire, kind, identity):
        if not fetch <= issue <= retire:
            raise ValueError('nonmonotonic stages at %s' % identity)
        if self.previous is not None and retire < self.previous:
            raise ValueError('out-of-order retirement at %s' % identity)
        self.rows += 1
        if self.first is None:
            self.first = retire
        elif retire > self.previous:
            self.counts['productive_cycles'] += 1
            a, b = self.previous + 1, retire
            if a < b:
                self.counts['zero_commit_episodes'] += 1
                self.counts['not_fetched_cycles'] += overlap(a, b, 0, fetch)
                self.counts['fetched_not_issued_cycles'] += overlap(a, b, fetch, issue)
                issued = overlap(a, b, issue, retire)
                self.counts['issued_%s_cycles' % kind] += issued
                if issued:
                    self.blocking_heads[kind] += 1
                    if kind == 'load':
                        self.top_loads.append(dict(identity, cycles=issued,
                                                   issue=issue, retire=retire))
                        if len(self.top_loads) > 40:
                            self.top_loads.sort(key=lambda x: x['cycles'], reverse=True)
                            del self.top_loads[20:]
        self.previous = retire

    def result(self):
        names = ('not_fetched_cycles', 'fetched_not_issued_cycles',
                 'issued_load_cycles', 'issued_store_cycles', 'issued_other_cycles')
        counts = {k: self.counts[k] for k in names}
        zero = sum(counts.values())
        span = self.previous - self.first if self.rows else 0
        if span != zero + self.counts['productive_cycles']:
            raise ValueError('cycle partition does not conserve')
        return dict(rows=self.rows, first_retire=self.first,
                    last_retire=self.previous, span_cycles=span,
                    productive_cycles=self.counts['productive_cycles'],
                    zero_commit_cycles=zero, categories=counts,
                    zero_commit_episodes=self.counts['zero_commit_episodes'],
                    distinct_issued_blocking_heads=dict(self.blocking_heads),
                    top_load_heads=sorted(self.top_loads,
                                         key=lambda x: x['cycles'], reverse=True)[:20],
                    conserved=True)


def audit_core(task):
    collection, entry, cpl = task
    collection = Path(collection)
    core, warm, take = int(entry[0]), int(entry[6]), int(entry[7])
    period = int(cpl['clock_period_ticks'])
    labels = list((collection / 'trace').glob('*switch%d.*labels.micro.jsonl' % core))
    if len(labels) != 1:
        raise ValueError('expected exactly one labels file for core %d' % core)
    result = HeadGaps()
    auxiliary = auxiliary_roi = hardware = user = kernel = 0
    ordinal = 0
    with Path(entry[2]).open('rb') as fst, labels[0].open('rb') as source:
        data = mmap.mmap(fst.fileno(), 0, access=mmap.ACCESS_READ)
        h = HEADER.unpack_from(data)
        if h[:5] != (b'FSTRC01\0', 7, HEADER.size, 64, core):
            raise ValueError('unsupported FST header or core mismatch')
        if h[5] < warm + take:
            raise ValueError('FST shorter than measurement slice')
        for line in source:
            while ordinal < warm + take:
                flags, op = FLAGS_OP.unpack_from(data, HEADER.size + ordinal * 64 + 50)
                if op != -1:
                    break
                auxiliary += 1
                auxiliary_roi += int(ordinal >= warm)
                ordinal += 1
            if ordinal >= warm + take:
                break
            label = json.loads(line)
            hardware += 1
            if (label['core_id'], label['thread_id'], label['micro_seq']) != (
                    core, int(entry[3]), hardware):
                raise ValueError('non-contiguous hardware micro_seq/core/thread at %d' % ordinal)
            if ordinal >= warm:
                fetch_tick = int(label['fetch_tick'])
                issue_tick = fetch_tick + int(label['issue_tick'])
                retire_tick = int(label['commit_tick'])
                if any(t % period for t in (fetch_tick, issue_tick, retire_tick)):
                    raise ValueError('tick is not cycle aligned')
                if not cpl['first_tick'] <= retire_tick <= cpl['last_tick']:
                    raise ValueError('measurement commit outside CPL ROI')
                kind = 'load' if flags & 2 else 'store' if flags & 4 else 'other'
                result.add(fetch_tick // period, issue_tick // period,
                           retire_tick // period, kind,
                           dict(record_ordinal=ordinal, micro_seq=hardware))
                user += int(op >= 0)
                kernel += int(op < -1)
            ordinal += 1
        data.close()
    if ordinal != warm + take or result.rows + auxiliary_roi != take:
        raise ValueError('short or nonconserved measurement join')
    out = result.result()
    elapsed = (cpl['last_tick'] - cpl['first_tick']) // period
    idle = cpl['idle_ticks'] // period
    out.update(core=core, labels=str(labels[0]), fst=entry[2],
               warmup_records=warm, measurement_records=take,
               auxiliary_records_without_o3_stage=auxiliary_roi,
               user_hardware_uops=user, kernel_hardware_uops=kernel,
               clock_period_ticks=period,
               cpl_elapsed_cycles=elapsed, cpl_idle_cycles=idle,
               cpl_active_cycles=(cpl['measured_ticks'] - cpl['idle_ticks']) // period,
               boundary_uncovered_cycles=elapsed - out['span_cycles'],
               elapsed_zero_commit_cycles_include_idle=True,
               active_issued_load_cycles_lower=max(0, out['categories']['issued_load_cycles'] - idle),
               active_issued_load_cycles_upper=out['categories']['issued_load_cycles'])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collection', type=Path, action='append', required=True)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    tasks, offsets = [], []
    for collection in args.collection:
        completion = json.loads((collection / 'completion.json').read_text())
        if completion['returncode'] != 0:
            raise ValueError('failed gem5 recollection')
        cpls = {r['core_id']: r for r in map(json.loads,
                (collection / 'trace/oracle/cpl_class.jsonl').read_text().splitlines())}
        entries = [line.split() for line in (collection / 'manifest.txt').read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith('#')]
        offsets.append((str(collection), len(tasks), len(entries)))
        for entry in entries:
            if entry[1] != 'fastsim-binary-warmup-slice' or len(entry) != 8:
                raise ValueError('expected record-bounded native warmup manifest')
            tasks.append((str(collection), entry, cpls[int(entry[0])]))
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(audit_core, tasks):
            results.append(result)
            print('audited core %d: %d records, %d zero-commit cycles' %
                  (result['core'], result['measurement_records'], result['zero_commit_cycles']), flush=True)
    output = dict(schema='fastsim-fst-gem5-committed-head-gaps-v1',
                  interpretation='Committed-only head stage, not speculative ROB occupancy or rename-full events. Idle is bounded, not assigned to a head category.',
                  collections=[dict(collection=name, cores=results[begin:begin+count])
                               for name, begin, count in offsets])
    args.output.write_text(json.dumps(output, indent=2) + '\n')


if __name__ == '__main__':
    main()
