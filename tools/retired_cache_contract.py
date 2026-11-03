"""Retired-load cache contract. Never aliases speculative request counters."""
import hashlib
import json
from pathlib import Path
from audit_perf_retirement import SCOPES, integer

DICTIONARY=Path(__file__).resolve().parents[1]/'configs/retired-load-cache-events-v1.json'
EVENTS={
    'retired_loads':dict(hardware_event='MEM_INST_RETIRED.ALL_LOADS',event=0xd0,umask=0x81),
    'retired_stores':dict(hardware_event='MEM_INST_RETIRED.ALL_STORES',event=0xd0,umask=0x82),
    'retired_l1_load_hits':dict(hardware_event='MEM_LOAD_RETIRED.L1_HIT',event=0xd1,umask=1),
    'retired_l1_load_misses':dict(hardware_event='MEM_LOAD_RETIRED.L1_MISS',event=0xd1,umask=8),
    'retired_load_fb_hits':dict(hardware_event='MEM_LOAD_RETIRED.FB_HIT',event=0xd1,umask=64),
    'retired_l2_load_hits':dict(hardware_event='MEM_LOAD_RETIRED.L2_HIT',event=0xd1,umask=2),
    'retired_l2_load_misses':dict(hardware_event='MEM_LOAD_RETIRED.L2_MISS',event=0xd1,umask=16),
    'retired_l3_load_hits':dict(hardware_event='MEM_LOAD_RETIRED.L3_HIT',event=0xd1,umask=4),
    'retired_l3_load_misses':dict(hardware_event='MEM_LOAD_RETIRED.L3_MISS',event=0xd1,umask=32),
}
POP=('retired_macro_instructions','retired_uops')
MISS_EVENTS=('retired_l1_load_misses','retired_l2_load_misses','retired_l3_load_misses')


def validate_miss_estimates(model):
    """Validate separately from qualified counts; estimates need no FB score."""
    keys=('estimated_count','estimate_fallback_loads','estimated_merged_loads')
    for block in (model,*model['per_core']):
        for row in block['scopes'].values():
            bound=row['events']['retired_loads']['count']
            for name in MISS_EVENTS:
                event=row['events'][name]
                if event.get('estimate_policy')!='bounded-line-fill-v1':
                    raise ValueError('missing retired miss estimate policy')
                for key in keys:
                    if integer(event.get(key),key)>bound:
                        raise ValueError('retired miss estimate exceeds load population')
    for scope,row in model['scopes'].items():
        for name in MISS_EVENTS:
            for key in keys:
                if row['events'][name][key]!=sum(c['scopes'][scope]['events'][name][key] for c in model['per_core']):
                    raise ValueError('retired miss estimate per-core sum mismatch')


def validate_report(report,source):
    if (report.get('schema')!='retired-load-cache-'+('report' if source=='model' else 'reference')+'-v1'
            or report.get('hardware_equivalence')!='unverified'
            or report.get('unit')!='architectural-retired-macro-instruction'
            or set(report.get('scopes',{}))!=set(SCOPES)):
        raise ValueError('missing independent retired cache namespace or contract')
    cores=report.get('per_core',[]);ids=[c.get('core_id') for c in cores]
    if not ids or any(type(i) is not int or i<0 for i in ids) or len(ids)!=len(set(ids)):
        raise ValueError('invalid retired cache participants')
    for block in [report,*cores]:
        if set(block.get('scopes',{}))!=set(SCOPES):raise ValueError('retired cache scope mismatch')
        for scope,row in block['scopes'].items():
            for key in POP:integer(row.get(key),key)
            if set(row.get('events',{}))!=set(EVENTS):raise ValueError('retired cache event set mismatch')
            for name,event in row['events'].items():
                count=integer(event.get('count'),name);unknown=integer(event.get('unknown'),name+' unknown')
                bound=row['events']['retired_stores' if name=='retired_stores' else 'retired_loads']['count']
                if count>row['retired_macro_instructions'] or (unknown>bound if name in ('retired_loads','retired_stores') else count+unknown>bound):
                    raise ValueError('retired cache event exceeds architectural population')
                if source=='model' and any(event.get(k)!=EVENTS[name][k] for k in ('event','umask')):
                    raise ValueError('retired cache hardware event encoding mismatch')
    for scope,total in report['scopes'].items():
        if any(total[k]!=sum(c['scopes'][scope][k] for c in cores) for k in POP):
            raise ValueError('retired cache per-core population sum mismatch')
        for name,e in total['events'].items():
            if any(e[k]!=sum(c['scopes'][scope]['events'][name][k] for c in cores) for k in ('count','unknown')):
                raise ValueError('retired cache per-core event sum mismatch')
    return {c['core_id']:c for c in cores}


def compare_retired_cache(model,reference,*,misses_only=False):
    m=validate_report(model,'model');r=validate_report(reference,'native')
    if (set(m)!=set(r) or not model.get('input_identity')
            or model['input_identity']!=reference.get('input_identity')
            or model.get('task_ids')!=reference.get('task_ids')):
        raise ValueError('retired cache input/task/core mismatch')
    if misses_only:
        validate_miss_estimates(model)
        # User acceptance rule, 2026-10-26: native unknowns are missing
        # reference membership, not a low-error subset. Historical broad
        # diagnostics remain readable, but primary three-miss scoring rejects
        # incomplete qualification. No counts or populations are rewritten.
        if reference.get('evidence_gaps'):
            raise ValueError('native retired-cache coverage incomplete')
        for core,block in r.items():
            for scope,row in block['scopes'].items():
                if m[core]['scopes'][scope]['events']['retired_loads']['count']!=row['events']['retired_loads']['count']:
                    raise ValueError(f'eligible retired load population mismatch: core={core} scope={scope}')
                for name in ('retired_loads',*MISS_EVENTS):
                    if row['events'][name]['unknown']:
                        raise ValueError(f'native retired-cache unknown membership: core={core} scope={scope} event={name} unknown={row["events"][name]["unknown"]}')
    result=dict(schema='retired-load-cache-comparison-v1',hardware_equivalence='unverified',
                input_identity=model['input_identity'],task_ids=reference['task_ids'],
                evidence_gaps=reference.get('evidence_gaps',[]),scopes={})
    if misses_only:
        result.update(primary_events=list(MISS_EVENTS),prediction_policy='bounded-line-fill-v1',
                      acceptance_scope='three-retired-load-misses; FB-hit accuracy not required')
    for scope,total in model['scopes'].items():
        for core in m:
            if any(m[core]['scopes'][scope][k]!=r[core]['scopes'][scope][k] for k in POP):
                raise ValueError('retired cache per-core functional population mismatch')
        native=reference['scopes'][scope];events={}
        for name,p in total['events'].items():
            if misses_only and name not in MISS_EVENTS:continue
            q=native['events'][name];pv=p['estimated_count'] if misses_only else p['count'];rv=q['count']
            denom=total['retired_macro_instructions'];available=p.get('estimate_available') is True
            events[name]=dict(hardware_event=EVENTS[name]['hardware_event'],
                predicted=pv if available else None,reference=rv,
                model_unknown=p['unknown'],reference_unknown=q['unknown'],
                absolute_error=abs(pv-rv) if available else None,
                signed_error=pv-rv if available else None,
                relative_error=(pv-rv)/rv if available and rv else None,
                ape_percent=abs(pv-rv)*100/rv if available and rv else None,
                predicted_per_1k_inst=pv*1000/denom if available and denom else None,
                reference_per_1k_inst=rv*1000/denom if denom else None,
                status='incomplete-evidence' if available else 'unavailable',
                missing_evidence=['target-ISA/service-event qualification unverified',
                    'wrongpath/prefetch indirect cache-state effects not fully modeled']+
                    (['unknown service members'] if p['unknown'] or q['unknown'] else []))
            if misses_only:
                events[name].update(model_known_count=p['count'],
                    estimate_policy=p['estimate_policy'],
                    estimate_fallback_loads=p['estimate_fallback_loads'],
                    estimated_merged_loads=p['estimated_merged_loads'],
                    prediction_domain='full eligible-load model estimate; zero-unknown complete native reference')
        result['scopes'][scope]=dict(retired_macro_instructions=total['retired_macro_instructions'],events=events)
    return result


def audit_retired_cache(trace_dir):
    # Reuse the maintained auditor: FST/owner identities, exact ordinal bounds,
    # common endpoint, native source and primitive conservation remain required.
    from audit_perf_cache import audit_perf_cache
    trace=Path(trace_dir)
    audited=audit_perf_cache(trace,include_input_identity=True,allow_incomplete=True)
    populations=audited['functional_retirement_populations']
    cores=[]
    for core in audited['per_core']:
        cid=core['core_id'];path=trace/f'oracle/perf-cache-core{cid}.json'
        original=path.read_bytes()
        if hashlib.sha256(original).hexdigest()!=audited['source_provenance'].get(str(path.resolve())):
            raise ValueError('retired cache sidecar changed since source audit')
        raw=json.loads(original);scopes={}
        for scope,row in raw['scopes'].items():
            new=row.get('retired_cache',{})
            if new.get('schema')!='retired-load-cache-v1':raise ValueError('native retired cache source absent; no proxy fallback')
            scopes[scope]=dict(retired_macro_instructions=row['retired_macro_instructions'],
                retired_uops=populations[str(cid)][scope]['retired_uops'],events=new['events'])
            # UOP population is reused from the maintained independent FST scan.
        cores.append(dict(core_id=cid,scopes=scopes))
    totals={}
    for scope in SCOPES:
        totals[scope]={k:sum(c['scopes'][scope][k] for c in cores) for k in POP}
        totals[scope]['events']={name:{k:sum(c['scopes'][scope]['events'][name][k] for c in cores)
                                       for k in ('count','unknown')} for name in EVENTS}
    result=dict(schema='retired-load-cache-reference-v1',source='native-retirement-service-facts',
        hardware_equivalence='unverified',unit='architectural-retired-macro-instruction',
        input_identity=audited['input_identity']['source_identity'],task_ids=audited['task_ids'],
        per_core=cores,scopes=totals,evidence_gaps=audited.get('evidence_gaps',[]))
    validate_report(result,'native')
    return result


def write_retired_cache_summary(output,reports):
    lines=['# Retired load cache comparison','','FastSim predictions are model estimates; gem5 counts are independent native observations. Hardware equivalence unverified. '
           'Unknown membership and source gaps remain explicit. Main scope: workload_task.','',
           '| case | event | gem5 | FastSim | signed error % | absolute count error | native unknown | model unknown |',
           '|---|---|---:|---:|---:|---:|---:|---:|']
    if any(r.get('retired_cache_validation',{}).get('primary_events') for r in reports):
        lines[2]+=' Primary events: retired L1/L2/L3 load misses only; FB-hit accuracy is not an acceptance gate.'
    buckets={name:[] for name in EVENTS}
    for report in reports:
        comp=report.get('retired_cache_validation',{})
        for name,row in comp.get('scopes',{}).get('workload_task',{}).get('events',{}).items():
            error=row['relative_error'];pct='N/A' if error is None else f'{error*100:+.4f}%'
            lines.append(f"| {report['case_id']} | {name} | {row['reference']} | {row['predicted']} | {pct} | {row['absolute_error']} | {row['reference_unknown']} | {row['model_unknown']} |")
            buckets[name].append(row)
    lines+=['','| event | finite cases | provisional MAPE | provisional WAPE | max APE |','|---|---:|---:|---:|---:|']
    for name,rows in buckets.items():
        valid=[r for r in rows if r['ape_percent'] is not None]
        if not valid:continue
        mape=sum(r['ape_percent'] for r in valid)/len(valid)
        scored=[r for r in rows if r['absolute_error'] is not None]
        denom=sum(r['reference'] for r in scored)
        wape=sum(r['absolute_error'] for r in scored)*100/denom if denom else None
        lines.append(f"| {name} | {len(valid)}/{len(rows)} | {mape:.4f}% | {wape:.4f}% | {max(r['ape_percent'] for r in valid):.4f}% |")
    (Path(output)/'retired-cache-summary.md').write_text('\n'.join(lines)+'\n')
