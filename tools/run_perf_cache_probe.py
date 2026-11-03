#!/usr/bin/env python3
"""Pinned SPR raw-event probes. Reports observations, never three-way accuracy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

from perf_cache_contract import BINDINGS, load_dictionary, integer

ROOT=Path(__file__).resolve().parents[1]
GROUPS=(('perf_retired_loads','perf_retired_stores','perf_l1d_load_hits'),
        ('perf_retired_loads','perf_l1d_load_misses','perf_l1d_load_fb_hits'),
        ('perf_l2_accesses','perf_l2_demand_misses','perf_llc_misses_core'),
        ('perf_l1d_load_misses','perf_l2_demand_misses','perf_llc_misses_core'),
        ('perf_l2_writebacks','perf_llc_accesses_core','perf_retired_stores'),
        ('perf_l2_writebacks','diagnostic_l2_lines_out_non_silent','diagnostic_l2_lines_in'),
        ('retired_loads','retired_stores','retired_l1_load_hits'),
        ('retired_l1_load_misses','retired_l2_load_misses','retired_l3_load_misses'),
        ('retired_l1_load_hits','retired_load_fb_hits','retired_l2_load_hits'),
        ('retired_l3_load_hits','retired_loads','retired_stores'))


def validate_record(row):
    if row.get('schema')!='hardware-perf-cache-probe-v1':raise ValueError('invalid hardware probe schema')
    group=integer(row.get('group'),'group')
    if group>=len(GROUPS):raise ValueError('unknown counter group')
    if not integer(row.get('iterations'),'iterations'):raise ValueError('empty iterations')
    enabled=integer(row.get('time_enabled'),'time_enabled');running=integer(row.get('time_running'),'time_running')
    if not enabled or enabled!=running:raise ValueError('multiplexed or unscheduled hardware counter group')
    if set(row.get('counts',{}))!=set((*GROUPS[group],'instructions')):raise ValueError('partial counter group')
    for field,value in row['counts'].items():integer(value,field)
    if not row['counts']['instructions']:raise ValueError('zero macro denominator')
    return row


def summarize_records(rows):
    selected={}
    for row in rows:
        validate_record(row)
        # Capacity is part of the case key; no pooling across cache levels.
        key=row.get('label',f"{row['case']}-{row['working_set_bytes']}");case=selected.setdefault(key,{})
        for field,count in row['counts'].items():
            if field=='instructions':continue
            case.setdefault(field,[]).append((count,1000*count/row['counts']['instructions'],count/row['iterations']))
    return {name:{'hardware_equivalence_certified':False,**{
        field:dict(samples=len(values),raw_median=statistics.median(v[0] for v in values),
                   raw_min=min(v[0] for v in values),raw_max=max(v[0] for v in values),
                   per_1k_inst_median=statistics.median(v[1] for v in values),
                   per_iteration_median=statistics.median(v[2] for v in values))
        for field,values in case.items()}} for name,case in selected.items()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--iterations',type=int,default=200000)
    parser.add_argument('--repeat',type=int,default=3)
    parser.add_argument('--cpu',type=int,default=min(os.sched_getaffinity(0)))
    parser.add_argument('--case',action='append',help='Explicit case:working-set-bytes; repeat for a subset')
    parser.add_argument('--retired-cache',action='store_true',help='Use retired-load L1/L2/L3 events and their independent event dictionary')
    parser.add_argument('--groups',default=None,help='Comma-separated raw counter group numbers')
    parser.add_argument('--quick',action='store_true',help='Use L1/L2-capacity diagnostics only; no claim of LLC capacity')
    args=parser.parse_args()
    if args.iterations<=0 or args.repeat<=0 or args.cpu not in os.sched_getaffinity(0):parser.error('invalid iterations/repeat/CPU')
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    cpuinfo=Path('/proc/cpuinfo').read_text().split('\n\n')[0]
    identity={line.split(':',1)[0].strip():line.split(':',1)[1].strip() for line in cpuinfo.splitlines() if ':' in line}
    if identity.get('vendor_id')!='GenuineIntel' or identity.get('cpu family')!='6' or identity.get('model')!='143':
        parser.error('raw encodings require the bound Intel SPR family6/model143')
    source=ROOT/'tests/workloads/perf_cache_probe.c';snapshot=out/'perf_cache_probe.c'
    snapshot.write_bytes(source.read_bytes());source=snapshot;binary=out/'perf_cache_probe'
    compile_cmd=['gcc','-O2','-std=gnu11','-Wall','-Wextra',str(source),'-o',str(binary)]
    subprocess.run(compile_cmd,check=True,capture_output=True)
    cases=[('load',4096),('forward',4096),('fb',4096),('split_mixed',4096),('cmps',4096),
           ('walk',256*1024),('store_walk',256*1024)]
    if not args.quick:cases += [('walk',16*1024*1024),('walk',256*1024*1024),('store_walk',16*1024*1024)]
    if args.case:
        cases=[]
        for case in args.case:
            name,size=case.split(':',1);cases.append((name,int(size)))
    default_groups='6,7,8,9' if args.retired_cache else '0,1,2,3,4'
    groups=[int(g) for g in (args.groups or default_groups).split(',')]
    if any((g>=6)!=args.retired_cache for g in groups):parser.error('retired and request groups require separate collection modes')
    if not groups or len(groups)!=len(set(groups)) or any(g<0 or g>=len(GROUPS) for g in groups):parser.error('invalid counter group set')
    rows=[];commands=[]
    for kind,size in cases:
        for group in groups:
            for repeat in range(args.repeat):
                cmd=[str(binary),kind,str(args.iterations),str(size),str(group),str(args.cpu)]
                result=subprocess.run(cmd,text=True,capture_output=True)
                if result.returncode:
                    (out/'failed-command.json').write_text(json.dumps(dict(argv=cmd,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr),indent=2)+'\n')
                    raise RuntimeError(f'hardware group failed: {cmd}, exit={result.returncode}')
                row=validate_record(json.loads(result.stdout));row.update(label=f'{kind}-{size}',repeat=repeat,argv=cmd)
                if row['cpu']!=args.cpu:raise ValueError('probe migrated from pinned CPU')
                rows.append(row);commands.append(cmd)
        (out/'runs.json').write_text(json.dumps(rows,indent=2)+'\n')
    manifest=dict(purpose='raw hardware event/unit/capacity observations; not three-way PMU accuracy certification',
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  dictionary=(json.loads((ROOT/'configs/retired-load-cache-events-v1.json').read_text()) if args.retired_cache else load_dictionary()),
                  metric_namespace='retired_cache' if args.retired_cache else 'perf_cache',identity=identity,core=args.cpu,exclusive_host=False,
                  scope='self-thread user-only, exclude_kernel/hv, pinned leader, no inheritance or scaling',
                  compiler=subprocess.check_output(['gcc','--version'],text=True).splitlines()[0],compile_command=compile_cmd,
                  commands=commands,baseline_subtracted=False)
    for name,value in [('manifest',manifest),('summary',summarize_records(rows))]:
        (out/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(dict(output=str(out),runs=len(rows),all_nonmultiplexed=True,hardware_equivalence_certified=False)))


if __name__=='__main__':main()
