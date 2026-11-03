#!/usr/bin/env python3
"""Run controller-only same-offered-flow diagnostics, never score workload CPI."""
import argparse
from collections import defaultdict,deque
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from controller_oracle_common import read_packets,selects,validate_final_stream,required_controller_sum,validate_projection
from audit_dram_mechanisms import TIMINGS

ROOT=Path(__file__).resolve().parents[1]
ENV=dict(os.environ,LD_LIBRARY_PATH='/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64',PYTHONDONTWRITEBYTECODE='1')
VARIANTS=['native','quantized','quantized-no-refresh','native-no-refresh']

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()

def stats(path):
    text=path.read_text()
    if text.count('Begin Simulation Statistics')!=1:raise ValueError('require one oracle stats snapshot')
    result={}
    for line in text.splitlines():
        fields=line.split()
        if len(fields)>=2:
            try:result[fields[0]]=float(fields[1])
            except ValueError:pass
    return result

def requested(path,controllers,identities=None):
    streams=[[] for _ in controllers]
    if identities is not None:
        rows={}
        for text in path.read_text().splitlines():
            f=text.split();tick,line,identity=map(int,f[:3]);kind=f[3] if len(f)>3 else 'R'
            if identity in rows:raise ValueError('duplicate input identity')
            rows[identity]=dict(tick=tick,addr=line*64,cmd=1 if kind=='R' else 4,identity=identity)
        used=set()
        for i,ids in enumerate(identities):
            for identity in ids:
                if identity in used or identity not in rows:raise ValueError('invalid oracle input binding')
                used.add(identity);x=rows[identity]
                if not selects(controllers[i]['dram']['range'],x['addr']):raise ValueError('oracle routed address outside controller')
                streams[i].append(x)
        if used!=set(rows):raise ValueError('oracle omitted input identities')
        return streams
    for text in path.read_text().splitlines():
        f=text.split();tick,line,identity=map(int,f[:3]);kind=f[3] if len(f)>3 else 'R'
        selected=[i for i,c in enumerate(controllers) if selects(c['dram']['range'],line*64)]
        if len(selected)!=1:raise ValueError('invalid source range')
        streams[selected[0]].append(dict(tick=tick,addr=line*64,cmd=1 if kind=='R' else 4,identity=identity))
    return streams

def summarize_oracle(directory,source,requests,reset):
    manifest=json.loads((directory/'oracle.json').read_text());actual=json.loads((directory/'config.json').read_text())['system']['controllers']
    if not isinstance(actual,list):actual=[actual]
    if manifest['requests_sha256']!=sha(requests):raise ValueError('oracle input stream hash mismatch')
    if len(actual)!=len(source) or len(manifest['copied'])!=len(source):raise ValueError('oracle controller population mismatch')
    mismatches=[]
    for i,clone in enumerate(manifest['copied']):
        for kind,obj in [('controller',actual[i]),('dram',actual[i]['dram'])]:
            for k,entry in clone[kind]['fields'].items():
                original=source[i] if kind=='controller' else source[i]['dram']
                if original.get(k)!=entry['source']:mismatches.append([i,kind,k,'source parameter changed'])
                if obj.get(k)!=entry['effective']:mismatches.append([i,kind,k,entry['effective'],obj.get(k)])
        if actual[i]['dram']['range']!=source[i]['dram']['range']:mismatches.append([i,'range'])
    if mismatches:raise ValueError('oracle parameter readback mismatch: '+str(mismatches[:8]))
    desired=requested(requests,source,manifest['requested_identities']);pairs=[];delayed=ambiguous=0;accepted_rows=[]
    for i,wanted in enumerate(desired):
        req=read_packets((directory/('req-%d.pb'%i)).read_bytes())
        resp=read_packets((directory/('resp-%d.pb'%i)).read_bytes())
        if len(req)!=len(wanted):raise ValueError('oracle failed to inject complete request population')
        pending=defaultdict(deque)
        for x,y in zip(wanted,req):
            if (x['addr'],x['cmd'])!=(y['addr'],y['cmd']) or y['tick']<x['tick']:raise ValueError('oracle ingress identity/order mismatch')
            delayed+=y['tick']!=x['tick'];key=(y['addr'],y['cmd'])
            ambiguous+=bool(pending[key]);pending[key].append((x,y))
            accepted_rows.append((x['identity'],y['tick'],y['addr']//64,'R' if y['cmd']==1 else 'W'))
        for r in resp:
            cmd=1 if r['cmd'] in (2,3) else 4 if r['cmd'] in (5,6) else None
            key=(r['addr'],cmd)
            if not pending[key]:raise ValueError('unmatched oracle response')
            x,y=pending[key].popleft()
            if r['tick']<y['tick']:raise ValueError('response before request')
            pairs.append(dict(identity=x['identity'],channel=i,read=cmd==1,requested=x['tick'],accepted=y['tick'],response=r['tick']))
        if any(pending.values()):raise ValueError('oracle simulation ended with outstanding requests')
    (directory/'accepted.tsv').write_text(''.join('%d %d %d %s\n'%(tick,line,identity,kind)
        for identity,tick,line,kind in sorted(accepted_rows,key=lambda x:(x[1],x[0]))))
    # pkt_id is requestor ID, not a unique transaction ID. FIFO address pairing
    # preserves all-population sums; overlapping-address per-member attribution
    # is not certified, especially in mixed read/write/forwarding streams.
    reads=[x for x in pairs if x['read'] and x['requested']>=reset]
    with (directory/'pairs.csv').open('w') as f:
        writer=csv.writer(f);writer.writerow(['identity','channel','kind','requested','accepted','response'])
        for x in sorted(pairs,key=lambda x:x['identity']):
            writer.writerow([x['identity'],x['channel'],'R' if x['read'] else 'W-ack',x['requested'],x['accepted'],x['response']])
    s=stats(directory/'stats.txt')
    names=['readBursts','writeBursts','readRowHits','writeRowHits','totQLat','totMemAccLat']
    physical={k:required_controller_sum(s,len(source),'dram.'+k) for k in names}
    for k in ['readReqs','writeReqs','numRdRetry','numWrRetry','numWriteReadTurnArounds']:
        physical[k]=required_controller_sum(s,len(source),k)
    n=physical['readBursts']
    result=dict(variant=manifest['variant'],parameter_readback_valid=True,requests=sum(map(len,desired)),
        read_response_samples=len(reads),delayed_ingress_requests=delayed,same_offered_and_accepted_arrivals=delayed==0,
        repeated_address_requests=ambiguous,individual_address_fifo_pairing='repetition is not measured concurrency; per-member pairing unverified for overlapping mixed flows; global sums unaffected',
        physical_and_response_population_counts_equal=n==len(reads),
        population_basis='physical service after stats reset versus responses selected by requested-tick; count equality does not prove member equality',
        physical=physical,arrival_to_command_ticks=physical['totQLat']/n if n else None,
        read_responses_minus_serviced_bursts=len(reads)-n,
        arrival_to_data_ticks=physical['totMemAccLat']/n if n else None,
        row_hit_fraction=physical['readRowHits']/n if n else None,
        read_request_to_response_ticks=sum(x['response']-x['accepted'] for x in reads)/len(reads) if reads else None,
        offered_to_response_ticks=sum(x['response']-x['requested'] for x in reads)/len(reads) if reads else None,
        identity='requested channel/order/address/cmd; monitored pkt_id is requestor ID',
        initial_state=manifest['initial_state'])
    (directory/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def oracle_job(job):
    directory,source,requests,until,reset,variant,binary=job;directory.mkdir(parents=True,exist_ok=False)
    cmd=[str(binary),'--outdir='+str(directory),str(ROOT/'tools/gem5_controller_oracle.py'),
         '--config-json',str(source),'--requests',str(requests),'--until',str(until),'--variant',variant,'--reset-tick',str(reset)]
    start=time.monotonic()
    with (directory/'run.log').open('x') as log:r=subprocess.run(cmd,cwd=ROOT,env=ENV,stdout=log,stderr=subprocess.STDOUT)
    (directory/'command.json').write_text(json.dumps(dict(argv=cmd,returncode=r.returncode,wall_seconds=time.monotonic()-start),indent=2)+'\n')
    if r.returncode:raise RuntimeError(str(directory/'run.log'))
    c=json.loads(source.read_text())['board']['memory']['mem_ctrl']
    result=summarize_oracle(directory,c,requests,reset)
    print(directory.parent.name,variant,'complete; retries',result['physical']['numRdRetry'],flush=True)
    return result

def make_cases():
    channel=8;column=128;bank=channel*column;rank=bank*16;row=rank*2
    out={}
    out['row_hit']=[(1000+i*150,i*channel,'R') for i in range(8)]
    out['row_conflict']=[(1000+i*150,(i%2)*row,'R') for i in range(8)]
    out['late_row_hit']=[(500,bank,'R'),(1000,0,'R'),(1030,bank,'R')]
    out['row_cap']=[(1000+i,i*channel,'R') for i in range(16)]+[(1016,row,'R')]
    out['adaptive_conflict']=[(1000,0,'R'),(1001,row,'R'),(1002,channel,'R')]
    out['bank_parallel']=[(1000,i*bank,'R') for i in range(8)]
    out['rank_switch']=[(1000,0,'R'),(1001,rank,'R'),(1002,channel,'R')]
    out['channel_parallel']=[(1000,i,'R') for i in range(8)]
    out['read_queue_full']=[(1000+i,(i%2)*row+(i//2)*channel,'R') for i in range(128)]
    out['refresh_boundary']=[(1000,0,'R'),(23400,0,'R'),(23425,channel,'R'),(24600,2*channel,'R')]
    out['write_drain']=[(1000+i,(i%16)*bank+(i//16)*row,'W') for i in range(130)]+[(2000+i,rank+(i+1)*channel,'R') for i in range(20)]
    out['write_merge']=[(1000+i,0,'W') for i in range(130)]+[(2000+i,rank+(i+1)*channel,'R') for i in range(20)]
    out['write_forward']=[(1000,0,'W'),(1001,0,'R')]
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'gem5-fs/build/X86_MESI_Three_Level/gem5.opt')
    p.add_argument('--fs-probe',type=Path,default=ROOT/'build/fastsim_replay_dram_probe')
    p.add_argument('--base-config',type=Path,required=True);p.add_argument('--stream',type=Path)
    p.add_argument('--prediction',type=Path);p.add_argument('--jobs',type=int,default=4)
    p.add_argument('--arrival-field',choices=['arrival','projected_arrival'],default='arrival',
                   help='projected_arrival shifts demand reads, retaining original MSHR path delay; writes stay on original axis')
    p.add_argument('--variants',choices=VARIANTS,nargs='+',default=VARIANTS)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    source=a.source.resolve();output=a.output.resolve();base=a.base_config.resolve();binary=a.binary.resolve()
    captured=json.loads(source.read_text());ctrl=captured['board']['memory']['mem_ctrl'];d=ctrl[0]['dram']
    clock=captured['board']['clk_domain']['clock'];period=int(clock[0] if isinstance(clock,list) else clock)
    jobs=[];cases={};projection_coverage=None
    if a.stream:
        if not a.prediction:raise ValueError('stream requires prediction')
        rows=list(csv.DictReader(a.stream.open()));prediction=json.loads(a.prediction.read_text())
        observed=prediction['totals']['dram_controller_reads']
        validate_final_stream(rows,observed,prediction['configuration']['dram']['frontend_latency'],prediction['configuration']['dram']['backend_latency'])
        if a.arrival_field=='projected_arrival':projection_coverage=validate_projection(rows)
        path=output/'requests.tsv'
        if any(a.arrival_field not in r or not r[a.arrival_field] for r in rows):raise ValueError('arrival field unavailable')
        ordered=sorted(enumerate(rows),key=lambda x:(int(x[1][a.arrival_field]),x[0]))
        path.write_text(''.join('%d %s %d %s\n'%(int(r[a.arrival_field])*period,r['line'],i,r['kind']) for i,r in ordered))
        reset=prediction['totals']['functional_warmup_barrier_cycles']*period
        until=max(int(r[a.arrival_field]) for r in rows)*period+2000000
        cases['workload']=dict(requests=path,reset=reset,until=until,mixed=any(r['kind']=='W' for r in rows))
    else:
        if a.arrival_field!='arrival':raise ValueError('projected arrival requires a stream')
        geometry=(len(ctrl),int(d['ranks_per_channel']),int(d['banks_per_rank']),
                  int(d['device_rowbuffer_size'])*int(d['devices_per_rank']),d['addr_mapping'])
        if geometry!=(16,2,16,8192,'RoRaBaCoCh'):raise ValueError('micro-flow address fixtures require captured 16-controller geometry')
        for name,rows in make_cases().items():
            directory=output/name;directory.mkdir()
            path=directory/'requests.tsv'
            path.write_text(''.join('%d %d %d %s\n'%(t*period,line,i,kind) for i,(t,line,kind) in enumerate(rows)))
            cases[name]=dict(requests=path,reset=0,until=max(t for t,_,_ in rows)*period+2000000,mixed=any(k=='W' for _,_,k in rows))
    for name,c in cases.items():
        for variant in a.variants:jobs.append((output/name/variant,source,c['requests'],c['until'],c['reset'],variant,binary))
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:list(pool.map(oracle_job,jobs))
    if not a.stream:
        for name,c in cases.items():
            for unit in ['ticks','cycles']:
                cfg=output/name/('fs-'+unit+'.cfg');text=base.read_text()
                if unit=='ticks':
                    for key,original in TIMINGS.items():text+='\ndram.'+key+' = '+str(d[original])
                    text+='\ndram.frontend_latency = '+str(ctrl[0]['static_frontend_latency'])
                    text+='\ndram.backend_latency = '+str(ctrl[0]['static_backend_latency'])
                cfg.write_text(text+'\n')
                requests=c['requests']
                if unit=='cycles':
                    requests=output/name/'fs-cycle-requests.tsv'
                    requests.write_text(''.join('%d %s %s %s\n'%(int(f[0])//period,f[1],f[2],f[3]) for f in (line.split() for line in c['requests'].read_text().splitlines())))
                cmd=[str(a.fs_probe.resolve()),str(cfg),str(requests),'mixed' if c['mixed'] else 'frfcfs','64']
                with (output/name/('fs-'+unit+'.tsv')).open('x') as out,(output/name/('fs-'+unit+'.log')).open('x') as err:
                    r=subprocess.run(cmd,cwd=ROOT,env=ENV,stdout=out,stderr=err)
                if r.returncode:raise RuntimeError(str(output/name/('fs-'+unit+'.log')))
                if not c['mixed']:
                    oracle_variant='native-no-refresh' if unit=='ticks' else 'quantized-no-refresh'
                    accepted=output/name/oracle_variant/'accepted.tsv'
                    if unit=='cycles':
                        data=accepted.read_text().splitlines()
                        if all(int(line.split()[0])%period==0 for line in data):
                            accepted=output/name/'accepted-cycles.tsv'
                            accepted.write_text(''.join('%d %s %s %s\n'%(int(f[0])//period,f[1],f[2],f[3]) for f in (line.split() for line in data)))
                        else:continue # non-cycle-aligned admission cannot be rounded silently
                    extra_cmd=[str(a.fs_probe.resolve()),str(cfg),str(accepted),'frfcfs','64']
                    with (output/name/('fs-'+unit+'-accepted.tsv')).open('x') as out,(output/name/('fs-'+unit+'-accepted.log')).open('x') as err:
                        r=subprocess.run(extra_cmd,cwd=ROOT,env=ENV,stdout=out,stderr=err)
                    if r.returncode:raise RuntimeError('accepted-flow FS probe failed')
    summary=dict(schema='controller-differential-v2',cases={},CPI_claim=False,
        arrival_field=a.arrival_field,
        projection_coverage=projection_coverage,
        projection_limitations='demand reads only; original MSHR path delay retained; writebacks unchanged; not a coupled CPU/cache re-solve',
        response_cohort='requested tick >= original FS warmup barrier; use original stream phase/identity for projected-flow comparisons')
    for name in cases:
        summary['cases'][name]={v:json.loads((output/name/v/'summary.json').read_text()) for v in a.variants}
    summary['inputs']={str(x):sha(x) for x in [source,base,binary,ROOT/'tools/gem5_controller_oracle.py',ROOT/'tools/controller_oracle_common.py',a.fs_probe.resolve(),Path(__file__)]}
    if a.stream:
        summary['inputs'][str(a.stream.resolve())]=sha(a.stream)
        summary['inputs'][str(a.prediction.resolve())]=sha(a.prediction)
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')

if __name__=='__main__':main()
