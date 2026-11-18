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
SPR_MISS_POLICY='spr-retired-load-miss-v2'
MEMBERSHIP_DICTIONARY=DICTIONARY.with_name('retired-load-cache-membership-v1.json')
MEMBERSHIP_KEYS=('count','unknown','nonmember','exact_count','estimated_count',
                 'exact_nonmember','estimated_nonmember')


def validate_membership_event(event, bound):
    """Closed per-event classification, including negative membership."""
    values={k:integer(event.get(k),k) for k in MEMBERSHIP_KEYS}
    if (values['count']+values['unknown']+values['nonmember']!=bound or
            values['count']!=values['exact_count']+values['estimated_count'] or
            values['nonmember']!=values['exact_nonmember']+values['estimated_nonmember']):
        raise ValueError('retired membership certainty/population conservation failed')
    rules={r['id']:r['certainty'] for r in json.loads(MEMBERSHIP_DICTIONARY.read_text())['reasons']}
    reasons=event.get('reasons')
    if not isinstance(reasons,dict) or set(reasons)-set(rules):
        raise ValueError('retired membership reason dictionary mismatch')
    buckets=dict(exact=0,estimated=0,unclassified=0)
    for reason,count in reasons.items():
        buckets[rules[reason]]+=integer(count,reason)
    if (buckets['exact']!=values['exact_count']+values['exact_nonmember'] or
            buckets['estimated']!=values['estimated_count']+values['estimated_nonmember'] or
            buckets['unclassified']!=values['unknown']):
        raise ValueError('retired membership reason/certainty conservation failed')


def sum_membership_events(events):
    events=list(events)
    fields=MEMBERSHIP_KEYS if 'nonmember' in events[0] else ('count','unknown')
    result={k:sum(e[k] for e in events) for k in fields}
    if 'nonmember' in events[0]:
        result['reasons']={}
        for event in events:
            for reason,value in event['reasons'].items():
                result['reasons'][reason]=result['reasons'].get(reason,0)+value
    return result


def validate_miss_estimates(model):
    """Validate separately from qualified counts; estimates need no FB score."""
    keys=('estimated_count','estimate_fallback_loads','estimated_merged_loads')
    for block in (model,*model['per_core']):
        for row in block['scopes'].values():
            bound=row['events']['retired_loads']['count']
            for name in MISS_EVENTS:
                event=row['events'][name]
                if event.get('estimate_policy') not in ('bounded-line-fill-v1',SPR_MISS_POLICY):
                    raise ValueError('missing retired miss estimate policy')
                if event.get('estimate_policy')==SPR_MISS_POLICY:
                    parts={key:integer(event.get(key),key) for key in (
                        'prediction_nonmember','prediction_unknown','prediction_exact_count',
                        'prediction_estimated_count','prediction_exact_nonmember',
                        'prediction_estimated_nonmember')}
                    if (event['estimated_count']+parts['prediction_nonmember']+
                            parts['prediction_unknown']!=bound or
                            parts['prediction_exact_count']+parts['prediction_estimated_count']!=event['estimated_count'] or
                            parts['prediction_exact_nonmember']+parts['prediction_estimated_nonmember']!=parts['prediction_nonmember']):
                        raise ValueError('SPR prediction certainty/population mismatch')
                for key in keys:
                    if integer(event.get(key),key)>bound:
                        raise ValueError('retired miss estimate exceeds load population')
    for scope,row in model['scopes'].items():
        for name in MISS_EVENTS:
            event_keys=keys
            if row['events'][name]['estimate_policy']==SPR_MISS_POLICY:
                event_keys+=('prediction_nonmember','prediction_unknown','prediction_exact_count',
                             'prediction_estimated_count','prediction_exact_nonmember',
                             'prediction_estimated_nonmember')
            for key in event_keys:
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
                if source=='native' and report.get('membership_policy'):
                    if report['membership_policy'] not in ('retired-load-cache-membership-v1',SPR_MISS_POLICY):
                        raise ValueError('unsupported native membership policy')
                    if name not in ('retired_loads','retired_stores'):
                        validate_membership_event(event,bound)
    for scope,total in report['scopes'].items():
        if any(total[k]!=sum(c['scopes'][scope][k] for c in cores) for k in POP):
            raise ValueError('retired cache per-core population sum mismatch')
        for name,e in total['events'].items():
            if any(e[k]!=sum(c['scopes'][scope]['events'][name][k] for c in cores) for k in ('count','unknown')):
                raise ValueError('retired cache per-core event sum mismatch')
            if source=='native' and 'nonmember' in e:
                summed=sum_membership_events(c['scopes'][scope]['events'][name] for c in cores)
                if any(e.get(k)!=v for k,v in summed.items()):
                    raise ValueError('retired cache per-core certainty/reason sum mismatch')
    return {c['core_id']:c for c in cores}


def compare_retired_cache(model,reference,*,misses_only=False,allow_incomplete_events=False):
    if allow_incomplete_events and not misses_only:
        raise ValueError('per-event incomplete mode requires retired misses')
    m=validate_report(model,'model');r=validate_report(reference,'native')
    if (set(m)!=set(r) or not model.get('input_identity')
            or model['input_identity']!=reference.get('input_identity')
            or model.get('task_ids')!=reference.get('task_ids')):
        raise ValueError('retired cache input/task/core mismatch')
    if misses_only:
        validate_miss_estimates(model)
        policies={row['events'][name]['estimate_policy'] for block in (model,*model['per_core'])
                  for row in block['scopes'].values() for name in MISS_EVENTS}
        if len(policies)!=1:
            raise ValueError('mixed retired miss prediction policies')
        prediction_policy=next(iter(policies))
        v2=reference.get('membership_policy')==SPR_MISS_POLICY
        if v2 != (prediction_policy==SPR_MISS_POLICY):
            raise ValueError('retired miss reference/prediction policy mismatch')
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
                    strict_scope=not v2 or scope=='workload_task'
                    if row['events'][name]['unknown'] and strict_scope and (v2 or not allow_incomplete_events):
                        raise ValueError(f'native retired-cache unknown membership: core={core} scope={scope} event={name} unknown={row["events"][name]["unknown"]}')
    result=dict(schema='retired-load-cache-comparison-v1',hardware_equivalence='unverified',
                input_identity=model['input_identity'],task_ids=reference['task_ids'],
                evidence_gaps=reference.get('evidence_gaps',[]),scopes={})
    if misses_only:
        result.update(primary_events=list(MISS_EVENTS),prediction_policy=prediction_policy,
                      acceptance_scope='workload_task' if v2 else 'three-retired-load-misses; FB-hit accuracy not required')
        if allow_incomplete_events:
            result['per_event_incomplete_mode'] = True
    for scope,total in model['scopes'].items():
        for core in m:
            if any(m[core]['scopes'][scope][k]!=r[core]['scopes'][scope][k] for k in POP):
                raise ValueError('retired cache per-core functional population mismatch')
        native=reference['scopes'][scope];events={}
        for name,p in total['events'].items():
            if misses_only and name not in MISS_EVENTS:continue
            q=native['events'][name];pv=p['estimated_count'] if misses_only else p['count'];rv=q['count']
            denom=total['retired_macro_instructions'];available=p.get('estimate_available') is True
            native_unknown = bool(misses_only and (allow_incomplete_events or v2) and
                (q['unknown'] or native['events']['retired_loads']['unknown']))
            scored = available and not native_unknown
            events[name]=dict(hardware_event=EVENTS[name]['hardware_event'],
                predicted=pv if available else None,reference=rv,
                model_unknown=p['unknown'],reference_unknown=q['unknown'],
                absolute_error=abs(pv-rv) if scored else None,
                signed_error=pv-rv if scored else None,
                relative_error=(pv-rv)/rv if scored and rv else None,
                ape_percent=abs(pv-rv)*100/rv if scored and rv else None,
                predicted_per_1k_inst=pv*1000/denom if available and denom else None,
                reference_per_1k_inst=rv*1000/denom if denom and not native_unknown else None,
                status=('unavailable-native-unknown' if native_unknown else
                        'incomplete-evidence' if available else 'unavailable'),
                missing_evidence=['target-ISA/service-event qualification unverified',
                    'wrongpath/prefetch indirect cache-state effects not fully modeled']+
                    (['unknown service members'] if p['unknown'] or q['unknown'] else []))
            if 'exact_count' in q:
                events[name].update(reference_exact_count=q['exact_count'],
                    reference_estimated_count=q['estimated_count'],
                    reference_membership_policy=reference.get('membership_policy'))
                if q['estimated_count'] or q['estimated_nonmember']:
                    events[name]['missing_evidence'].append('native reference contains explicit service-to-event estimates')
            if misses_only:
                events[name].update(model_known_count=p['count'],
                    estimate_policy=p['estimate_policy'],
                    estimate_fallback_loads=p['estimate_fallback_loads'],
                    estimated_merged_loads=p['estimated_merged_loads'],
                    prediction_domain='full eligible-load model estimate; zero-unknown complete native reference')
        result['scopes'][scope]=dict(retired_macro_instructions=total['retired_macro_instructions'],events=events)
    if misses_only and allow_incomplete_events:
        result['primary_complete'] = all(
            row['status'] == 'incomplete-evidence' for row in
            result['scopes']['workload_task']['events'].values())
    return result


def compare_retired_cache_expectations(model,expectation,reference,*,small_native_unknown_limit=0):
    """Score separate fractional estimates after the existing source gates.

    Never changes the legacy report, the native reference or an unknown count.
    The optional <=2 native unknown allowance is explicit/provisional only.
    """
    import math
    if type(small_native_unknown_limit) is not int or not 0<=small_native_unknown_limit<=2:
        raise ValueError('small native unknown allowance must be 0..2')
    if (reference.get('membership_policy')!=SPR_MISS_POLICY or
            expectation.get('semantic_policy')!=SPR_MISS_POLICY):
        raise ValueError('service expectation requires current native SPR miss semantic policy')
    validate_miss_estimates(model)
    if any(row['events'][name]['estimate_policy']!=SPR_MISS_POLICY
           for block in (model,*model['per_core']) for row in block['scopes'].values() for name in MISS_EVENTS):
        raise ValueError('service expectation legacy input semantic policy mismatch')
    base=compare_retired_cache(model,reference,misses_only=False)
    if (expectation.get('schema')!='retired-service-expectation-v1' or expectation.get('available') is not True
            or expectation.get('model') not in ('bounded-v1','probabilistic-v1')
            or expectation.get('unit')!='architectural-retired-macro-instruction'
            or expectation.get('hardware_equivalence')!='unverified' or expectation.get('scale')!=65536
            or expectation.get('timing_feedback') is not False or expectation.get('synthetic_requests') is not False
            or expectation.get('owner_coverage')!='complete'
            or expectation.get('input_identity')!=model['input_identity']
            or expectation.get('task_ids')!=model.get('task_ids')):
        raise ValueError('service expectation identity/policy/coverage mismatch')
    if reference.get('evidence_gaps'):raise ValueError('native retired-cache coverage incomplete')
    cores=expectation.get('per_core',[]);ids=[c.get('core_id') for c in cores]
    if len(ids)!=len(set(ids)) or set(ids)!={c['core_id'] for c in model['per_core']}:
        raise ValueError('service expectation core mismatch')
    ecores={c['core_id']:c for c in cores};mcores={c['core_id']:c for c in model['per_core']}
    rcores={c['core_id']:c for c in reference['per_core']}
    counters=('eligible_loads','unknown_source_loads','clock_unavailable_loads','correlation_uncertain_loads','fallback_loads')
    for block,legacy in [(expectation,model),*((ecores[k],mcores[k]) for k in ids)]:
        if set(block.get('scopes',{}))!=set(SCOPES):raise ValueError('service expectation scopes missing')
        for scope,row in block['scopes'].items():
            old=legacy['scopes'][scope];bound=old['events']['retired_loads']['count']
            if any(row.get(k)!=old[k] for k in POP) or row.get('eligible_loads')!=bound:
                raise ValueError('service expectation functional/eligible population mismatch')
            for k in counters:
                if integer(row.get(k),k)>bound:raise ValueError('service expectation coverage exceeds population')
            if set(row.get('events',{}))!=set(MISS_EVENTS):raise ValueError('service expectation event mismatch')
            previous=bound*65536
            for name in MISS_EVENTS:
                e=row['events'][name];q=integer(e.get('expected_q16'),'expected_q16')
                lo=integer(e.get('lower_q16'),'lower_q16');hi=integer(e.get('upper_q16'),'upper_q16')
                value=e.get('expected_count')
                if (not lo<=q<=hi<=bound*65536 or q>previous or type(value) not in (int,float)
                        or not math.isfinite(value) or not math.isclose(value,q/65536,rel_tol=1e-13,abs_tol=1e-9)):
                    raise ValueError('invalid/nonfinite/non-nested service expectation')
                previous=q
    for scope,total in expectation['scopes'].items():
        for k in (*POP,*counters):
            if total[k]!=sum(c['scopes'][scope][k] for c in cores):raise ValueError('service expectation per-core population sum mismatch')
        for name in MISS_EVENTS:
            for k in ('expected_q16','lower_q16','upper_q16'):
                if total['events'][name][k]!=sum(c['scopes'][scope]['events'][name][k] for c in cores):
                    raise ValueError('service expectation per-core probability sum mismatch')
    for core in ids:
        for scope,row in rcores[core]['scopes'].items():
            if ecores[core]['scopes'][scope]['eligible_loads']!=row['events']['retired_loads']['count']:
                raise ValueError('service expectation native eligible population mismatch')
            if row['events']['retired_loads']['unknown']:
                raise ValueError('native eligible load population unknown')
    if any(reference['scopes']['workload_task']['events'][k]['unknown']>small_native_unknown_limit for k in MISS_EVENTS):
        raise ValueError('native retired-cache unknown membership exceeds explicit allowance')
    result=dict(schema='retired-service-expectation-comparison-v1',hardware_equivalence='unverified',
                prediction_kind='conditional expectation; not observed event counts',primary_events=list(MISS_EVENTS),prediction_policy=expectation['model'],
                input_identity=model['input_identity'],task_ids=model['task_ids'],model=expectation['model'],
                small_native_unknown_limit=small_native_unknown_limit,scopes={})
    for scope,row in expectation['scopes'].items():
        result['scopes'][scope]={'retired_macro_instructions':row['retired_macro_instructions'],'events':{}}
        for name in MISS_EVENTS:
            pred=row['events'][name]['expected_count'];native=reference['scopes'][scope]['events'][name]
            ref=native['count'];unknown=native['unknown']
            allowed=unknown<=small_native_unknown_limit
            relative=(pred-ref)/ref if ref and allowed else None
            info=dict(base['scopes'][scope]['events'][name])
            info.update(predicted=pred,reference=ref,reference_unknown=unknown,model_unknown=row['unknown_source_loads'],
                absolute_error=abs(pred-ref) if allowed else None,signed_error=pred-ref if allowed else None,
                relative_error=relative,ape_percent=abs(relative)*100 if relative is not None else None,
                status='small-unknown-provisional' if unknown and allowed else 'unavailable-native-unknown' if unknown else 'provisional-expectation',
                estimate_policy=expectation['model'],prediction_domain='full eligible macro population conditional expectation')
            denominator=row['retired_macro_instructions']
            info['predicted_per_1k_inst']=pred*1000/denominator if denominator else None
            info['relative_error_if_all_unknown_miss']=(pred-(ref+unknown))/(ref+unknown) if ref+unknown and allowed else None
            result['scopes'][scope]['events'][name]=info
    return result


def audit_retired_cache(trace_dir):
    # Reuse the maintained auditor: FST/owner identities, exact ordinal bounds,
    # common endpoint, native source and primitive conservation remain required.
    from audit_perf_cache import audit_perf_cache
    trace=Path(trace_dir)
    audited=audit_perf_cache(trace,include_input_identity=True,allow_incomplete=True)
    populations=audited['functional_retirement_populations']
    cores=[];policies=set()
    for core in audited['per_core']:
        cid=core['core_id'];path=trace/f'oracle/perf-cache-core{cid}.json'
        original=path.read_bytes()
        if hashlib.sha256(original).hexdigest()!=audited['source_provenance'].get(str(path.resolve())):
            raise ValueError('retired cache sidecar changed since source audit')
        raw=json.loads(original);scopes={}
        for scope,row in raw['scopes'].items():
            new=row.get('retired_cache',{})
            if new.get('schema')!='retired-load-cache-v1':raise ValueError('native retired cache source absent; no proxy fallback')
            policies.add(new.get('membership_policy'))
            scopes[scope]=dict(retired_macro_instructions=row['retired_macro_instructions'],
                retired_uops=populations[str(cid)][scope]['retired_uops'],events=new['events'])
            # UOP population is reused from the maintained independent FST scan.
        cores.append(dict(core_id=cid,scopes=scopes))
    totals={}
    for scope in SCOPES:
        totals[scope]={k:sum(c['scopes'][scope][k] for c in cores) for k in POP}
        totals[scope]['events']={name:sum_membership_events(c['scopes'][scope]['events'][name] for c in cores)
                                for name in EVENTS}
    result=dict(schema='retired-load-cache-reference-v1',source='native-retirement-service-facts',
        hardware_equivalence='unverified',unit='architectural-retired-macro-instruction',
        input_identity=audited['input_identity']['source_identity'],task_ids=audited['task_ids'],
        per_core=cores,scopes=totals,evidence_gaps=audited.get('evidence_gaps',[]))
    if len(policies)!=1:raise ValueError('mixed retired membership policies')
    if next(iter(policies)) is not None:
        result.update(membership_policy=next(iter(policies)),
            membership_dictionary_sha256=hashlib.sha256(MEMBERSHIP_DICTIONARY.read_bytes()).hexdigest())
    validate_report(result,'native')
    return result


def write_retired_cache_summary(output,reports):
    lines=['# Retired load cache comparison','','FastSim predictions are model estimates; gem5 counts are independent native observations. Hardware equivalence unverified. '
           'Unknown membership and source gaps remain explicit. Main scope: workload_task.','',
           '| case | event | status | gem5 | FastSim | signed error % | absolute count error | native unknown | model unknown |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|']
    if any(r.get('retired_cache_validation',{}).get('primary_events') for r in reports):
        lines[2]+=' Primary events: retired L1/L2/L3 load misses only; FB-hit accuracy is not an acceptance gate.'
    if any(r.get('retired_cache_validation',{}).get('primary_complete') is False for r in reports):
        lines[2]+=' At least one case: not all three primary events are complete; unavailable events are excluded from accuracy.'
    buckets={name:[] for name in EVENTS}
    for report in reports:
        comp=report.get('retired_cache_validation',{})
        for name,row in comp.get('scopes',{}).get('workload_task',{}).get('events',{}).items():
            error=row['relative_error'];pct='N/A' if error is None else f'{error*100:+.4f}%'
            lines.append(f"| {report['case_id']} | {name} | {row['status']} | {row['reference']} | {row['predicted']} | {pct} | {row['absolute_error']} | {row['reference_unknown']} | {row['model_unknown']} |")
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
