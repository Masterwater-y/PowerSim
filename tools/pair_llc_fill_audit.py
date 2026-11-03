#!/usr/bin/env python3
"""Pair oracle-only native fills with accepted model members. No path-class truth."""
import argparse
from bisect import bisect_left
from collections import Counter,defaultdict
import hashlib
import json
import mmap
from pathlib import Path
import struct


def sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def validate_stream(rows,producer):
    if len(rows)<2 or rows[0].get('schema')!='llc-fill-audit-v1' or rows[0].get('producer')!=producer:
        raise ValueError('wrong LLC audit schema/producer')
    if rows[0].get('line_bytes')!=64 or type(rows[0].get('stride')) is not int or rows[0]['stride']<1:
        raise ValueError('invalid LLC audit line/sampling contract')
    tail=rows[-1]
    if tail.get('type')!='end' or tail.get('completed') is not True or tail.get('truncated') is not False or tail.get('omitted')!=0:
        raise ValueError('missing or truncated LLC audit closure')
    if tail.get('rows')!=len(rows)-2:
        raise ValueError('LLC audit row conservation failure')
    for row in rows[1:-1]:
        if row.get('type') not in ('member','fill','evict','core_begin','core_end','measurement','squash'):
            raise ValueError('unknown LLC audit row')
        if 'line' in row and (type(row['line']) is not int or row['line']<0 or row['line']%rows[0]['stride']):
            raise ValueError('invalid or unsampled physical line')


def members(rows):
    result={}
    for row in rows[1:-1]:
        if row['type']!='member':continue
        key=(row['core'],row['ordinal'],row['line'])
        if key in result:raise ValueError('duplicate LLC member anchor')
        result[key]=row
    return result


def pair_records(native,model):
    validate_stream(native,'native');validate_stream(model,'model')
    if native[0]['stride']!=model[0]['stride']:raise ValueError('sampling contract mismatch')
    n=members(native);m=members(model)
    if any(q['measurement'] and q['flags']&2 and not q['flags']&12 and q['resolved']
           and not q['source']&1024 and key not in m for key,q in n.items()):
        raise ValueError('known native load missing from successful model audit')
    history=defaultdict(list);squashes=defaultdict(list)
    committed={(r['core'],r['context'],r['sequence'],r['line']) for r in n.values()}
    for row in native[1:-1]:
        if row['type'] in ('fill','evict'):history[row['line']].append(row)
        elif row['type']=='squash':squashes[row['core']].append(row)
    ticks={}
    for line,events in history.items():
        events.sort(key=lambda r:r['tick'])
        ticks[line]=[r['tick'] for r in events]
    origins=Counter();counts=Counter();examples=[]
    for key,p in m.items():
        if key not in n:raise ValueError('model member absent from native stream')
        q=n[key]
        if any(p[k]!=q[k] for k in ('pc','flags','measurement')):
            raise ValueError('PC/flags/phase mismatch at raw source anchor')
        if not p['measurement']:continue
        counts['measurement_member_lines']+=1
        if p['flags']&12:counts['excluded_atomic_or_write_members']+=1;continue
        if q.get('split') or p.get('split'):
            counts['excluded_split_members']+=1;continue
        predicted=bool(p['estimate']&4)
        if not q['resolved'] or q['source']&1024:
            counts['native_unknown_members']+=1;continue
        actual=bool(q['source']&256)
        counts['known_members']+=1
        if actual and not predicted:counts['known_false_negatives']+=1
        if actual and predicted:counts['known_true_positives']+=1
        if actual or not predicted:continue
        counts['known_false_positives']+=1
        events=history.get(p['line'],[]);times=ticks.get(p['line'],[])
        admission=q['admission_tick'];response=q['response_tick']
        # Strict earlier tick: equal ticks do not prove event ordering.
        index=bisect_left(times,admission)-1 if admission else -1
        prior=events[index] if index>=0 else None
        origin='unobserved_fill'
        if admission in times:
            origin='same_tick_ambiguity'
        elif prior and prior['type']=='fill':
            role=prior['role']
            origin={3:'code',4:'pte',5:'software_prefetch',6:'prepare',7:'hardware_prefetch'}.get(role,'unknown_request_role')
            if role in (1,2):
                ident=(prior['core'],prior['context'],prior['sequence'],prior['line'])
                if prior['identity_valid'] and ident in committed:origin='committed_demand'
                elif prior['identity_valid'] and any(s['cutoff']<prior['sequence']<=s['youngest'] for s in squashes[prior['core']]):origin='squashed_demand'
                else:origin='unresolved_demand_origin'
        elif response and any(e['type']=='fill' and admission<=e['tick']<response for e in events):
            origin='overlapping_fill'
        origins[origin]+=1
        if len(examples)<64:
            examples.append(dict(core=key[0],ordinal=key[1],line=key[2],origin=origin,
                native_admission_tick=admission,native_response_tick=response,prior_event=prior))
    counts['native_read_member_lines_without_model']=sum(
        key not in m and bool(q['flags']&2) and not q['flags']&12 and q['measurement'] for key,q in n.items())
    return dict(schema='llc-fill-pairing-v1',unit='sampled load-member cache line; not architectural macro PMU',
        hardware_equivalence='unverified',stride=native[0]['stride'],
        **{k:counts[k] for k in ['measurement_member_lines','excluded_atomic_or_write_members','excluded_split_members','known_members',
            'native_unknown_members','known_true_positives','known_false_negatives','known_false_positives',
            'native_read_member_lines_without_model']},known_false_positive_origins=dict(origins),examples=examples,
        attribution_semantics='observed fill provenance before native admission; not isolated counterfactual causal contribution')


def load_rows(path):
    with Path(path).open() as f:return [json.loads(line) for line in f if line.strip()]


def validate_model_artifact(path,simulation):
    metadata=simulation.get('llc_fill_audit',{})
    path=Path(path)
    if (metadata.get('sha256')!=sha256(path)
            or metadata.get('size_bytes')!=path.stat().st_size):
        raise ValueError('model audit is not the exact validated replay artifact')


def validate_native_capture(binding_path,native_path,simulation,result_dir):
    """Verify the host-sealed complete capture bundle, not hot bytes alone."""
    from perf_cache_source import validate_identity
    b=json.loads(Path(binding_path).read_text())
    if b.get('schema')!='native-llc-fill-capture-binding-v1' or Path(b['result_dir']).resolve()!=Path(result_dir).resolve():
        raise ValueError('native capture bundle identity mismatch')
    validate_identity(b['functional_source_identity'],simulation['functional_source_identity'])
    if b['native_audit_sha256']!=sha256(native_path):
        raise ValueError('native audit changed since capture sealing')
    files=b.get('files',{})
    case=Path(result_dir).resolve()
    expected={k:case/v for k,v in dict(capture_command='capture-command.json',config_ini='config.ini',
        effective_target='effective-target.json',capture_receipt='capture-receipt.json',
        trace_metadata='tao_trace/trace.json',thread_state_plan='thread-state.plan').items()}
    oracle={p.name:p.resolve() for p in (case/'oracle').iterdir() if p.is_file()}
    if not oracle:raise ValueError('native capture oracle bundle missing')
    expected.update({'oracle/'+name:path for name,path in oracle.items()})
    if not set(expected).union({'gem5_binary','runner'})<=files.keys():
        raise ValueError('native capture provenance incomplete')
    for name,path in expected.items():
        if Path(files[name]['path']).resolve()!=path.resolve():
            raise ValueError('native capture artifact belongs to another capture: '+name)
    for name,row in files.items():
        if row['sha256']!=sha256(row['path']):
            raise ValueError('native capture artifact changed: '+name)
    command=json.loads(expected['capture_command'].read_text())['argv']
    command_paths={Path(arg).resolve() for arg in command if Path(arg).is_absolute()}
    if any(Path(files[k]['path']).resolve() not in command_paths for k in ('gem5_binary','runner')):
        raise ValueError('native producer binary/runner absent from actual capture command')


def bind_source(native,model,result_dir,simulation):
    from perf_cache_source import collect_source_identity,validate_identity
    trace=Path(result_dir)/'tao_trace'
    identity=collect_source_identity(trace/'manifest.txt',Path(result_dir)/'thread-state.plan')
    validate_identity(simulation.get('functional_source_identity',{}),identity)
    if model[0].get('source_identity')!=identity['source_identity']:
        raise ValueError('model audit content identity differs from FST')
    boundaries=json.loads((trace/'trace.json').read_text())['functional_boundaries']
    endings=[r for r in native if r.get('type')=='core_end']
    model_endings=[r for r in model if r.get('type')=='core_end']
    begins=[r for r in native if r.get('type')=='core_begin']
    ids={int(k) for k in boundaries}
    if len(endings)!=len(ids) or len(begins)!=len(ids) or {r['core'] for r in endings}!=ids or {r['core'] for r in begins}!=ids:
        raise ValueError('native core closure/participant mismatch')
    if len(model_endings)!=len(ids) or {r['core'] for r in model_endings}!=ids:
        raise ValueError('model accepted population closure missing')
    sources={s['core_id']:s for s in identity['sources']}
    ends={r['core']:r for r in endings}
    model_ends={r['core']:r for r in model_endings}
    maps={};handles=[]
    try:
        for core in ids:
            # Content identity helper has already bound each complete FST and
            # functional request companion. End rows independently bind the
            # producer's rolling hot-record hash, not a sampled subset.
            source=sources[core]
            fst=next(Path(a['path']) for a in source['artifacts'] if a['role']=='fst')
            with Path(str(fst)+'.mreq').open('rb') as companion:
                h=struct.unpack('<8sIIIIQQQII',companion.read(56))
            b=boundaries[str(core)];e=ends[core]
            if (e['records']!=b['total_records'] or e['warmup_records']!=b['warmup_records'] or e.get('hot_hash')!=h[7]
                    or e['tick']!=b['common_end_tick']):
                raise ValueError('native whole-source population/hash mismatch')
            if any(model_ends[core][k]!=e[k] for k in ('records','warmup_records')):
                raise ValueError('model accepted raw-source population mismatch')
            handle=fst.open('rb');handles.append(handle);maps[core]=mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ)
        for stream in (native,model):
            for row in stream[1:-1]:
                if row['type']!='member':continue
                core=row['core'];ordinal=row['ordinal']
                if core not in ids or not 0<=ordinal<boundaries[str(core)]['total_records']:
                    raise ValueError('member outside source core/ordinal')
                data=maps[core];offset=72+64*ordinal
                pc,address=struct.unpack_from('<QQ',data,offset);size,flags=struct.unpack_from('<HH',data,offset+48)
                if (pc!=row['pc'] or flags!=row['flags'] or not size or not address//64<=row['line']<=(address+size-1)//64
                        or row.get('split')!=((address//64)!=((address+size-1)//64))
                        or row['measurement']!=(ordinal>=boundaries[str(core)]['warmup_records'])):
                    raise ValueError('audit member disagrees with actual functional source')
    finally:
        for data in maps.values():data.close()
        for handle in handles:handle.close()
    return identity['source_identity']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native',type=Path,required=True);p.add_argument('--model',type=Path,required=True)
    p.add_argument('--validation-report',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();v=json.loads(args.validation_report.read_text())
    if v.get('status')!='passed' or v.get('errors'):raise ValueError('maintained native source/population gate did not pass')
    simulation=json.loads(Path(v['fastsim_output']).read_text())
    if v.get('fastsim_output_sha256')!=sha256(v['fastsim_output']):
        raise ValueError('prediction changed since maintained validation')
    validate_model_artifact(args.model,simulation)
    n=load_rows(args.native);m=load_rows(args.model);validate_stream(n,'native');validate_stream(m,'model')
    identity=bind_source(n,m,v['result_dir'],simulation)
    binding=Path(v['result_dir'])/'llc-fill-capture-binding.json'
    validate_native_capture(binding,args.native,simulation,v['result_dir'])
    result=pair_records(n,m);result['input_identity']=identity
    result['validation_report']=str(args.validation_report.resolve())
    result['provenance']={str(f.resolve()):sha256(f)
                          for f in [args.native,args.model,args.validation_report,Path(v['fastsim_output']),binding]}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('examples','provenance')},indent=2))

if __name__=='__main__':main()
