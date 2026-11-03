#!/usr/bin/env python3
"""Store latency diagnostics only; never cache truth or an accuracy scorer.

Native hit/miss means isExternalHit, not a duration threshold. CPU send/SQ
release and Sequencer admission/callback differ; populations are not certified.
"""
import argparse
import configparser
import hashlib
import json
import math
from pathlib import Path


def read_gem5_st(path):
    scalars, vectors = {}, {}
    marker = '.RequestType.ST.'
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) < 2 or marker not in parts[0]:
            continue
        tail = parts[0].split(marker, 1)[1]
        if tail in ('latency_hist_seqr','hit_latency_hist_seqr','miss_latency_hist_seqr'):
            if tail in vectors:
                raise ValueError('multiple ST histogram dumps')
            vectors[tail] = [int(part.split()[0]) for part in line.split('|')[1:]]
        elif tail.endswith(('::samples','::mean','::bucket_size')):
            if tail in scalars:
                raise ValueError('multiple ST statistics dumps')
            scalars[tail] = float(parts[1])
    result = {}
    for name, base in [('all','latency'),('hit','hit_latency'),('miss','miss_latency')]:
        key = base + '_hist_seqr'
        if any(key+'::'+field not in scalars for field in ('samples','mean','bucket_size')):
            raise ValueError('missing native ST histogram ' + key)
        count, mean, width = (scalars[key+'::'+field] for field in ('samples','mean','bucket_size'))
        if not math.isfinite(count) or count < 0 or count != int(count) or width < 1 or width != int(width):
            raise ValueError('invalid ST histogram population or width')
        if count and (not math.isfinite(mean) or mean < 0):
            raise ValueError('invalid ST mean')
        buckets = vectors.get(key)
        if count and (buckets is None or len(buckets) != 10 or min(buckets) < 0 or sum(buckets) != count):
            raise ValueError('ST histogram does not conserve population')
        result[name] = dict(samples=int(count),mean_cycles=mean if count else None,
                            bucket_size=int(width),buckets=buckets if buckets is not None else [0]*10)
    if result['all']['samples'] != result['hit']['samples'] + result['miss']['samples']:
        raise ValueError('native ST hit/miss population does not conserve total')
    return result


def align_histograms(fastsim, gem5):
    width=max(fastsim['bucket_size'],gem5['bucket_size'])
    result=dict(bucket_size=width)
    for name,h in [('fastsim',fastsim),('gem5',gem5)]:
        old=h['bucket_size']
        if old < 1 or old & (old-1) or width % old:
            raise ValueError('histogram widths are not powers of two')
        buckets=[0]*10
        for i,count in enumerate(h['buckets']):
            buckets[i*old//width]+=count
        result[name]=buckets
    return result


def compare(source, prediction):
    source, prediction = Path(source), Path(prediction)
    stats = json.loads(prediction.read_text())
    frontier = stats['causal_frontier']
    observation = frontier['bounded_store_demand_observation']
    if observation['schema'] != 'fastsim-store-demand-observation-v1':
        raise ValueError('unknown demand observation schema')
    samples = observation['all']['samples']
    if samples != frontier['bounded_store_estimates']:
        raise ValueError('bounded estimates/observation population mismatch')
    for histograms in [observation['by_role'],observation['by_canonical_private_level']]:
        if (sum(h['samples'] for h in histograms) != samples or
                sum(h['sum_cycles'] for h in histograms) != observation['all']['sum_cycles']):
            raise ValueError('demand partition does not conserve population/time')
    for h in [observation['all'],*observation['by_role'],*observation['by_canonical_private_level']]:
        if len(h['buckets']) != 10 or sum(h['buckets']) != h['samples']:
            raise ValueError('demand histogram does not conserve population')
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(source/'config.ini')
    ruby = [(s,cfg[s].get('clk_domain')) for s in cfg.sections() if cfg[s].get('type')=='RubySystem']
    cores = [(s,cfg[s].get('clk_domain')) for s in cfg.sections() if cfg[s].get('type')=='BaseO3CPU']
    if not cores:
        cores = [(s,cfg[s].get('clk_domain')) for s in cfg.sections()
                 if cfg[s].get('type') in ('X86O3CPU','DerivO3CPU')]
    if len(ruby)!=1 or not cores or not ruby[0][1] or any(c!=ruby[0][1] for _,c in cores):
        raise ValueError('core and Ruby clock equivalence unproven')
    clock = cfg[ruby[0][1]].get('clock')
    if not clock or len(clock.split()) != 1:
        raise ValueError('ambiguous clock period')
    fingerprints = {str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (source/'stats.txt',source/'config.ini',prediction)}
    native=read_gem5_st(source/'stats.txt')
    return dict(case=source.name,gem5_st=native,fastsim=observation,
                aligned_all_histograms=align_histograms(observation['all'],native['all']),
                reference_clock_ticks=int(clock),population_equivalence='unverified',
                edge_equivalence='cpu-send-to-sq-release-vs-sequencer-admission-to-callback',
                accuracy_acceptance=False,fingerprints=fingerprints)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',required=True,type=Path)
    parser.add_argument('--stats-dir',required=True,type=Path)
    parser.add_argument('--cases',required=True,nargs='+')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if len(set(args.cases))!=len(args.cases):
        raise ValueError('duplicate cases')
    rows=[compare(args.source_root/case,args.stats_dir/(case+'.json')) for case in args.cases]
    args.output.write_text(json.dumps(dict(schema='fastsim-store-demand-diagnostics-v1',cases=rows),indent=2)+'\n')
    print('case | gem5 ST all count/mean | FastSim bounded count/mean | gem5 ST miss count/mean')
    for r in rows:
        g,f=r['gem5_st'],r['fastsim']
        print(r['case'],g['all']['samples'],g['all']['mean_cycles'],f['all']['samples'],
              f['all']['mean_cycles'],g['miss']['samples'],g['miss']['mean_cycles'],sep=' | ')


if __name__=='__main__':main()
