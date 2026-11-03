#!/usr/bin/env python3
"""Analyze explicit same-flow controller artifacts; no CPI inference or scoring."""
import argparse
from collections import defaultdict
import csv
import json
import mmap
import math
from pathlib import Path
import re
import struct
from run_controller_differential import stats, sha, VARIANTS
from controller_oracle_common import validate_final_stream

def shapes(values,prefix,count):
    result={}
    for name in ['rdQLenPdf','dram.bytesPerActivate','dram.perBankRdBursts']:
        pattern=re.compile(re.escape(prefix)+r'(\d+)\.'+re.escape(name)+r'::(.+)$')
        data={}
        for key,value in values.items():
            m=pattern.fullmatch(key)
            if m:data.setdefault(m[1],{})[m[2]]=value if math.isfinite(value) else None
        for i in range(count):data.setdefault(str(i),None)
        result[name]=data
    bins=defaultdict(float)
    for distribution in result['rdQLenPdf'].values():
        if distribution is None:continue
        for key,value in distribution.items():
            if key.isdigit():bins[int(key)]+=value
    samples=sum(bins.values())
    result['arrival_queue_mean']=sum(k*v for k,v in bins.items())/samples if samples else None
    result['arrival_queue_samples']=samples
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['stream','prediction','oracle-dir','native-stats','output']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--store-audit',type=Path)
    p.add_argument('--variants',choices=VARIANTS,nargs='+',default=VARIANTS)
    a=p.parse_args();rows=list(csv.DictReader(a.stream.open()));prediction=json.loads(a.prediction.read_text())
    dram=prediction['configuration']['dram'];frontend=dram['frontend_latency'];backend=dram['backend_latency']
    validate_final_stream(rows,prediction['totals']['dram_controller_reads'],frontend,backend)
    measured={i:r for i,r in enumerate(rows) if r['phase']=='1' and r['kind']=='R'}
    store={};membership={};store_count=0
    if a.store_audit:
        keys={(int(r['core']),int(r['sequence']),int(r['ordinal']),int(r['line'])):i for i,r in measured.items()}
        if len(keys)!=len(measured):raise ValueError('duplicate measured read owner')
        record=struct.Struct('<20Q')
        with a.store_audit.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as view:
            if view[:8]!=b'FSSTLIF1' or struct.unpack_from('<II',view,8)!=(1,record.size):raise ValueError('invalid store audit')
            for start in range(16,len(view),record.size):
                r=record.unpack_from(view,start)
                if r[19]!=1:continue
                store_count+=1;identity=keys.get((r[0],r[17],r[18],r[14]))
                if identity is not None:
                    if identity in store:raise ValueError('duplicate store/read join')
                    store[identity]=dict(service=r[8]-r[7],role=r[11],core=r[0],send=r[7])
    groups={'all_reads':set(measured)}
    if a.store_audit:
        groups['store_reads']=set(store);groups['other_reads']=set(measured)-set(store)
        for core in sorted({int(r['core']) for r in measured.values()}):
            groups['store_core_'+str(core)]={i for i,r in store.items() if r['core']==core}
    native_config=a.native_stats.parent/'config.json'
    if not native_config.is_file():raise ValueError('native stats requires adjacent captured config.json')
    native_config_hash=sha(native_config);consumed=[native_config]
    run_summary=json.loads((a.oracle_dir.parent/'summary.json').read_text())
    arrival_field=run_summary.get('arrival_field','arrival')
    result=dict(schema='controller-differential-analysis-v1',unit='reference-cycles',CPI_claim=False,oracle_arrival_field=arrival_field,
        native_reference_scope='physical controller service after common-end stats reset; membership differs from FS',
        stream_reads=len(measured),store_audit_measurement_records=store_count,matched_store_reads=len(store),variants={})
    period=None;controller_count=None
    for variant in a.variants:
        directory=a.oracle_dir/variant;summary=json.loads((directory/'summary.json').read_text())
        manifest=json.loads((directory/'oracle.json').read_text())
        if manifest['source_sha256']!=native_config_hash:raise ValueError('oracle/native source configuration mismatch')
        consumed.extend(directory/name for name in ['summary.json','pairs.csv','config.json','stats.txt','oracle.json','command.json'])
        pairs={int(r['identity']):r for r in csv.DictReader((directory/'pairs.csv').open()) if r['kind']=='R' and int(r['identity']) in measured}
        if pairs.keys()!=measured.keys():raise ValueError('oracle missing measured read identity')
        system=json.loads((directory/'config.json').read_text())['system']
        clock=system['clk_domain']['clock'][0]
        if period is not None and clock!=period:raise ValueError('variant clock mismatch')
        period=clock;controller_count=len(system['controllers'])
        for identity,row in pairs.items():
            if int(row['requested'])!=int(measured[identity][arrival_field])*period:
                raise ValueError('oracle identity does not bind selected source arrival')
        overhead={i:c['static_frontend_latency']+c['static_backend_latency'] for i,c in enumerate(system['controllers'])}
        table={}
        for name,ids in groups.items():
            n=len(ids)
            if not n:continue
            fs=sum(int(measured[i]['response'])-int(measured[i]['arrival'])-frontend-backend for i in ids)/n
            native=sum(int(pairs[i]['response'])-int(pairs[i]['accepted'])-overhead[int(pairs[i]['channel'])] for i in ids)/n/period
            table[name]=dict(count=n,FS_arrival_to_data=fs,oracle_response_minus_static=native,FS_minus_oracle=fs-native,
                accepted_delay_mean=sum(int(pairs[i]['accepted'])-int(pairs[i]['requested']) for i in ids)/n/period,
                response_membership=('unique controller/address/command in complete input' if summary['repeated_address_requests']==0
                    else 'all-read mean invariant; subsets provisional under address FIFO attribution'))
            if name.startswith('store'):
                table[name]['FS_store_service_mean']=sum(store[i]['service'] for i in ids)/n
                table[name]['FS_store_exact250_fraction']=sum(store[i]['service']==250 for i in ids)/n
                table[name]['store_population']='matched first-fragment lifecycle identities; not all store requests'
                # Frozen-path diagnostic, not a native ST service observation:
                # replace only controller ingress-to-response, retain every
                # per-member FS non-controller segment and its original overlap.
                hybrid_ticks=[store[i]['service']*period+int(pairs[i]['response'])-int(pairs[i]['accepted'])
                    -(int(measured[i]['response'])-int(measured[i]['arrival']))*period for i in ids]
                table[name]['frozen_FS_path_oracle_controller_exact250_fraction']=sum(t==250*period for t in hybrid_ticks)/n
                table[name]['frozen_FS_path_oracle_controller_exact207_fraction']=sum(t==207*period for t in hybrid_ticks)/n
                table[name]['hybrid_not_native_ST_service']=True
        result['variants'][variant]=dict(summary=summary,groups=table,shapes=shapes(stats(directory/'stats.txt'),'system.controllers',controller_count))
    values=stats(a.native_stats)
    def total(field):
        selected={k:v for k,v in values.items() if re.fullmatch(r'board\.memory\.mem_ctrl\d+\.dram\.'+re.escape(field),k)}
        if len(selected)!=controller_count:raise ValueError('incomplete native controller stat '+field)
        return sum(selected.values())
    n=total('readBursts')
    result['native']=dict(read_bursts=n,arrival_to_data=total('totMemAccLat')/n/period,
        arrival_to_command=total('totQLat')/n/period,row_hit_fraction=total('readRowHits')/n,
        shapes=shapes(values,'board.memory.mem_ctrl',controller_count))
    result['reference_clock_ticks']=period
    result['decomposition']={}
    for name in groups if set(VARIANTS)<=set(a.variants) else []:
        if not groups[name]:continue
        g={v:result['variants'][v]['groups'][name]['oracle_response_minus_static'] for v in VARIANTS}
        fs=result['variants']['native']['groups'][name]['FS_arrival_to_data']
        result['decomposition'][name]=dict(FS_minus_G2=fs-g['quantized-no-refresh'],
            G2_minus_G1=g['quantized-no-refresh']-g['quantized'],G1_minus_G0=g['quantized']-g['native'],
            refresh_native=g['native']-g['native-no-refresh'],
            quantization_refresh_interaction=(g['quantized']-g['native'])-(g['quantized-no-refresh']-g['native-no-refresh']))
    result['inputs']={str(x.resolve()):sha(x) for x in [a.stream,a.prediction,a.native_stats,Path(__file__),a.oracle_dir.parent/'summary.json'] + consumed + ([a.store_audit] if a.store_audit else [])}
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')

if __name__=='__main__':main()
