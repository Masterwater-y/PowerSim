#!/usr/bin/env python3
"""Read final accepted store observations. Diagnostic only; no CPI scorer."""
import argparse
from array import array
from collections import Counter, defaultdict
import configparser
import hashlib
import json
import mmap
from pathlib import Path
import struct
from compare_store_demand_latency import read_gem5_st

FIELDS = ('core sequence dispatch issue retire eligible prior_release send release prepare permission '
          'role level fragments line flags callback source_sequence ordinal phase').split()
ROW = struct.Struct('<20Q')

def interval_union(intervals, begin, end):
    total = 0; right = begin
    for a, b in sorted(intervals):
        a = max(a, begin); b = min(b, end)
        if b > max(a, right): total += b - max(a, right)
        right = max(right, b)
    return total

def inspect_record(r):
    times = [r[k] for k in ['dispatch','issue','retire','eligible','send','release']]
    if times != sorted(times): raise ValueError('non-monotonic store stage')
    callback = None
    if r['flags'] & 8:
        if not r['send'] <= r['callback'] <= r['release']:
            raise ValueError('invalid callback stage')
        callback = r['callback'] - r['send']
    return dict(residence=r['release']-r['dispatch'],
                post_retirement=r['release']-r['retire'],
                admission=r['eligible']-r['retire'], fifo_wait=r['send']-r['eligible'],
                service=r['release']-r['send'], callback_service=callback)

def histogram(counts):
    width = 1
    while counts and max(counts) // width >= 10: width *= 2
    buckets = [0] * 10
    for value, count in counts.items(): buckets[value // width] += count
    return dict(samples=sum(counts.values()), sum_cycles=sum(k*v for k,v in counts.items()),
                bucket_size=width, buckets=buckets)

def measurement_window(origin, duration):
    if origin<0 or duration<0:raise ValueError('invalid measurement window')
    return origin, origin+duration

def native_total(hist):
    return hist['samples']*hist['mean_cycles'] if hist['samples'] else 0

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['audit','prediction','baseline','source','output']:
        p.add_argument('--'+name, type=Path, required=True)
    a=p.parse_args(); prediction=json.loads(a.prediction.read_text());base=json.loads(a.baseline.read_text())
    cfg=configparser.ConfigParser(interpolation=None);cfg.read(a.source/'config.ini')
    ruby=[s for s in cfg.sections() if cfg[s].get('type')=='RubySystem']
    cpus=[s for s in cfg.sections() if cfg[s].get('type') in ('BaseO3CPU','X86O3CPU','DerivO3CPU')]
    if len(ruby)!=1 or not cpus:raise ValueError('missing CPU/Ruby clock evidence')
    domain=cfg[ruby[0]]['clk_domain']
    if any(cfg[s]['clk_domain']!=domain for s in cpus):raise ValueError('CPU/Ruby clock mismatch')
    period=int(cfg[domain]['clock'])
    frequencies=prediction['configuration']['core_frequencies_hz']
    if any(abs(f*period-10**12)>=period for f in frequencies):raise ValueError('model/source clock mismatch')
    if any(f!=prediction['configuration']['reference_frequency_hz'] for f in frequencies):
        raise ValueError('reference/core clock conversion required for clipping')
    stores=defaultdict(list); count=Counter(); last={}; phases=Counter()
    with a.audit.open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as stream:
        if stream[:8] != b'FSSTLIF1' or struct.unpack_from('<II',stream,8)!=(1,ROW.size):
            raise ValueError('unsupported lifecycle stream')
        if (len(stream)-16)%ROW.size: raise ValueError('truncated lifecycle stream')
        for values in ROW.iter_unpack(memoryview(stream)[16:]):
            r=dict(zip(FIELDS,values));inspect_record(r)
            key=(r['core'],r['phase'])
            if key in last and r['sequence']<=last[key]:raise ValueError('duplicate or rolled-back store')
            last[key]=r['sequence']; phases[r['phase']]+=1
            if r['phase']==1:
                stores[r['core']].append(values); count[r['core']]+=1
    result=dict(schema='store-lifecycle-audit-v1',unit='local-core-cycles',reference_clock_ticks=period,phases=dict(phases),cores=[])
    for core, raw in enumerate(prediction['cores']):
        hist=defaultdict(Counter); service_intervals=[]; residence_intervals=[]; stages=Counter();roles=Counter();levels=Counter();prepare=Counter()
        for values in stores[core]:
            r=dict(zip(FIELDS,values)); x=inspect_record(r)
            for k in ['residence','post_retirement','admission','fifo_wait','service']:
                hist[k][x[k]]+=1;stages[k]+=x[k]
            hist['level_'+str(r['level'])][x['service']]+=1
            hist['role_'+str(r['role'])][x['service']]+=1
            hist['role_level_'+str(r['role'])+'_'+str(r['level'])][x['service']]+=1
            roles[r['role']]+=1;levels[r['level']]+=1
            stages['task_members']+=bool(r['flags']&1);stages['kernel_members']+=bool(r['flags']&2)
            stages['task_service' if r['flags']&1 else 'other_task_service']+=x['service']
            stages['multi_fragment']+=r['fragments']!=1;stages['callback_unknown']+=x['callback_service'] is None
            if r['flags']&4:
                prepare['observed']+=1
                prepare['permission_ready_at_send']+=r['permission']<=r['send']
                prepare['inflight_at_send']+=r['prepare']<r['send']<r['permission']
                prepare['prepare_at_or_after_send']+=r['prepare']>=r['send']
            service_intervals.append((r['send'],r['release']))
            residence_intervals.append((r['dispatch'],r['release']))
        if count[core]!=raw['response_store_uops'] or stages['service']!=raw['response_store_send_to_response_cycles']:
            raise ValueError('audit does not conserve accepted store population/time')
        horizon=raw['cycles']
        origin=prediction['totals']['functional_warmup_barrier_cycles']
        begin,end=measurement_window(origin,horizon)
        busy=interval_union(service_intervals,begin,end)
        residence=interval_union(residence_intervals,begin,end)
        events=sorted([(t,delta) for begin,end in residence_intervals if end>begin for t,delta in [(begin,1),(end,-1)]])
        live=peak=0
        for _,delta in events:live+=delta;peak=max(live,peak)
        if live:raise ValueError('residence population not conserved')
        result['cores'].append(dict(core=core,samples=count[core],reported_cycles=horizon,window_begin=begin,window_end=end,
            stage_sums=dict(stages),histograms={k:histogram(v) for k,v in hist.items()},roles=dict(roles),levels=dict(levels),
            prepare=dict(prepare),service_busy_union_clipped=busy,service_sum_minus_clipped_union=stages['service']-busy,
            regular_store_residence_union_clipped=residence,regular_store_max_live=peak,
            not_in_regular_store_service=horizon-busy))
    result['native']=read_gem5_st(a.source/'stats-common-end.txt')
    native=result['native']['all']; total=sum(x['stage_sums']['service'] for x in result['cores'])
    result['comparison']=dict(fs_service_sum=total,native_sum_from_rounded_mean=native_total(native),
        native_sum_rounding_bound=0.5e-6*native['samples'],
        callback_alignment='unavailable: bounded model retains no independent protocol callback',
        membership_alignment='unverified: store UOP vs completed Sequencer ST requests',
        native_per_core_histograms='not present in global RequestType.ST histogram',
        causal_closure=False)
    # Compare all existing target fields; only explicitly named host values vary.
    def target(v):
        v=json.loads(json.dumps(v))
        for k in ['wall_time_seconds','wall_time_breakdown','throughput','input_identity_wall_seconds']:v.pop(k,None)
        v.get('scope_metrics',{}).pop('throughput',None)
        for k in ['frontier_waits','dram_frfcfs_wall_ns','timing_feedback_wall_ns','interval_schedule_batch_wall_ns',
                  'interval_weave_wall_ns','interval_commit_wall_ns','domain_phase_wall_ns']:
            v.get('causal_frontier',{}).pop(k,None)
        return v
    result['timing_neutral']=target(base)==target(prediction)
    if not result['timing_neutral']:
        result['different_sections']=[k for k in set(base)|set(prediction) if target(base).get(k)!=target(prediction).get(k)]
    result['official_active_cycles']=prediction['time_accounting']['active_cycles']
    result['sum_reported_core_cycles']=sum(c['cycles'] for c in prediction['cores'])
    result['inputs']={str(x.resolve()):sha(x) for x in [a.audit,a.prediction,a.baseline,a.source/'stats-common-end.txt',a.source/'config.ini',Path(__file__)]}
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(timing_neutral=result['timing_neutral'],samples=sum(count.values()),comparison=result['comparison']),indent=2))
    if not result['timing_neutral']:raise SystemExit(1)

if __name__=='__main__':main()
