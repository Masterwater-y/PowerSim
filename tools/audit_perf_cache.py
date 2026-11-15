"""Audit v2 native observations against current FST and independent primitives.

This adapter never selects legacy perf_* or TaoTrace path-class cache fields.
Retirement population is independently scanned by the maintained retirement
auditor. Native request primitives are reduced separately from final suppliers.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from audit_perf_retirement import (audit_perf_retirement, scan_functional_retirement,
                                   load_macro_boundaries, validate_boundary_provenance,
                                   POP_FIELDS, TAIL_FIELDS, SCOPES)
from thread_state_graph import has_certified_functional_ownership, load_capture_evidence
from perf_cache_contract import load_dictionary, integer, validate_metric
from time_accounting import assemble_gem5_time_accounting

KINDS=('data_read','rfo','code_read','software_prefetch','hardware_prefetch_l1',
       'hardware_prefetch_l2','hardware_prefetch_l3','hardware_prefetch','writeback','unknown')
OUTCOMES=('hit','tag_miss','permission_upgrade','merged_miss','remote_supply')
BOUNDS=('core_id','source_file','source_record_count','measurement_begin_ordinal',
        'measurement_end_ordinal_exclusive','common_cut_id','common_start_tick',
        'common_end_tick','task_ids')


def recount_requests(scope):
    primitives=scope.get('request_primitives',{})
    primitives=primitives.get('levels',primitives)
    if not set(('private_l2','llc')).issubset(primitives) or set(primitives)-set(('l1d','private_l2','llc')):
        raise ValueError('missing native per-level request primitives')
    reduced={}
    for level,kinds in primitives.items():
        if not isinstance(kinds,dict) or set(kinds)-set(KINDS):
            raise ValueError('unknown native request primitive kind')
        normalized={}
        for kind,counts in kinds.items():
            if set(counts)!=set(OUTCOMES):raise ValueError('incomplete native lookup outcome taxonomy')
            normalized[kind]={name:integer(value,f'{level}:{kind}:{name}') for name,value in counts.items()}
        reduced[level]=normalized
    l2=reduced['private_l2'];llc=reduced['llc']
    def summed(kinds,selected,outcomes):
        return sum(kinds.get(kind,{}).get(outcome,0) for kind in selected for outcome in outcomes)
    l2k=[k for k in l2 if k not in ('writeback','unknown')]
    llck=[k for k in llc if k not in ('writeback','unknown','hardware_prefetch_l3')]
    return dict(perf_l2_accesses=summed(l2,l2k,[o for o in OUTCOMES if o!='merged_miss']),
                perf_l2_demand_misses=summed(l2,('data_read','rfo','code_read'),('tag_miss',)),
                perf_l2_writebacks=integer(scope.get('private_l2_outgoing_writebacks'),'native outgoing writebacks'),
                perf_llc_accesses_core=summed(llc,llck,OUTCOMES),
                perf_llc_misses_core=summed(llc,llck,('tag_miss',)))


def normalized_scope(raw,scope,cores,tasks,dictionary,window):
    events={};retired=integer(raw.get('retired_macro_instructions'),'native macro population')
    raw_ret=raw.get('retirement_events',{});raw_req=raw.get('request_events',{})
    ret_names={k for k,v in dictionary['events'].items() if v['domain']=='retirement'}
    req_names=set(dictionary['events'])-ret_names
    if set(raw_ret)!=ret_names or set(raw_req)!=req_names or set(raw.get('retirement_unknown',{}))!=ret_names:
        raise ValueError('native v2 event/unknown coverage mismatch')
    recounted=recount_requests(raw)
    if any(raw_req[k]!=v or type(raw_req[k]) is not int for k,v in recounted.items()):
        raise ValueError('native request counts differ from independent primitive reduction')
    for name,binding in dictionary['events'].items():
        value=integer((raw_ret if binding['domain']=='retirement' else raw_req)[name],name)
        declared=raw.get('event_capabilities',{}).get(name,{})
        if not isinstance(declared,dict) or any(type(v) is not bool for v in declared.values()):
            raise ValueError('invalid native capability flag')
        coverage={key:declared.get(key,False) for key in binding['required_capabilities']}
        if binding['domain']=='retirement':
            unknown=integer(raw['retirement_unknown'][name],name+':unknown')
            if unknown>retired:raise ValueError('native unknown qualification exceeds macro population')
            cap='member_service_facts' if name.startswith('perf_l1d') else 'isa_membership'
            if unknown and coverage.get(cap):raise ValueError('native unknown source/ISA contradicts coverage')
        events[name]=dict(event=binding['event'],umask=binding['umask'],unit=binding['unit'],domain=binding['domain'],
                          diagnostic_value=value,value=value if all(coverage.values()) else None,
                          coverage=coverage,hardware_validated=False)
    metric=dict(schema='gem5-perf-cache-v2',contract_id=dictionary['contract_id'],
                hardware_target=deepcopy(dictionary['hardware_target']),evidence_source='native-o3-ruby-observers',
                oracle_only=True,fst_input=False,
                measurement=dict(scope=scope,window_id=window,core_ids=cores,task_ids=tasks if scope=='workload_task' else [],context_policy='event-edge-context-v1'),
                retired_macro_instructions=retired,events=events)
    return validate_metric(metric,'gem5')


def sum_metrics(metrics,scope,dictionary):
    result=deepcopy(metrics[0]);result['measurement']['scope']=scope
    result['measurement']['core_ids']=sorted(c for m in metrics for c in m['measurement']['core_ids'])
    result['retired_macro_instructions']=sum(m['retired_macro_instructions'] for m in metrics)
    for field,row in result['events'].items():
        row['diagnostic_value']=sum(m['events'][field]['diagnostic_value'] for m in metrics)
        row['coverage']={k:all(m['events'][field]['coverage'][k] for m in metrics) for k in row['coverage']}
        row['value']=row['diagnostic_value'] if all(row['coverage'].values()) else None
        if any('evidence_gaps' in m['events'][field] for m in metrics):
            row['evidence_gaps']=[gap for m in metrics for gap in m['events'][field].get('evidence_gaps',[])]
        if any('unknown_qualification_count' in m['events'][field] for m in metrics):
            row['unknown_qualification_count']=sum(m['events'][field].get('unknown_qualification_count',0) for m in metrics)
        if any('request_unknown_diagnostics' in m['events'][field] for m in metrics):
            children=[m['events'][field].get('request_unknown_diagnostics',{}) for m in metrics]
            row['request_unknown_diagnostics']={key:(sum(c[key] for c in children) if all(type(c.get(key)) is int for c in children) else None)
                for key in ('observer_unknown_requests','unknown_primitive_lookups')}
            row['request_unknown_diagnostics']['attribution']='observer/request-class diagnostics; not a per-event missing-count bound'
    return validate_metric(result,'gem5')


def audit_request_population(trace):
    """Keep request evidence independent of retirement qualification failures.

    Current FST/owner/clocks and native identity remain mandatory. Missing or
    inconsistent retirement membership is evidence to report, not a replacement
    cache counter. No native count is repaired with the functional scan.
    """
    evidence=load_capture_evidence(trace)
    meta=evidence['metadata'];graph=evidence['graph']
    if not meta or not has_certified_functional_ownership(graph):
        raise ValueError('cache population requires certified functional ownership')
    tasks=sorted(t['os_tid'] for t in graph['tasks'])
    clocks=[json.loads((trace/f'oracle/time-accounting-core{m["core_id"]}.json').read_text()) for m in meta]
    clock=assemble_gem5_time_accounting(clocks,cores=len(meta))
    if clock['elapsed_status']!='available':raise ValueError('cache common clock unavailable')
    expected={f'perf-retirement-core{m["core_id"]}.json' for m in meta}
    if {p.name for p in (trace/'oracle').glob('perf-retirement-core*.json')}!=expected:
        raise ValueError('missing or extra retirement identity sidecar participants')
    per_core=[];gaps=[];retirement_provenance={};boundary_provenance={}
    for m in meta:
        path=trace/f'oracle/perf-retirement-core{m["core_id"]}.json'
        original=path.read_bytes();row=json.loads(original)
        required=dict(schema='gem5-perf-retirement-v1',oracle_only=True,fst_input=False,
                      population_source='gem5-o3-committed-DynInst',
                      source_classification='native-ruby-service-facts',
                      unit='architectural-retired-macro-instruction',
                      population_contract='task-oncpu-switch-entry-v1',
                      core_id=m['core_id'],source_file=m['source_file'],
                      source_record_count=m['source_record_count'],
                      measurement_begin_ordinal=m['warmup_records'],
                      measurement_end_ordinal_exclusive=m['source_record_count'],
                      common_start_tick=clock['common_start_tick'],common_end_tick=clock['common_end_tick'],
                      common_cut_id='first-core-target-common-end-v1',task_ids=tasks,
                      perf_equivalence='unverified')
        if any(row.get(k)!=v for k,v in required.items()):
            raise ValueError('native retirement source/participant/boundary mismatch')
        if row.get('capabilities',{}).get('memory_event_eligibility_policy')=='functional-retirement-v1':
            with (trace/m['source_file']).open('rb') as functional:
                functional.seek(32)
                if not int.from_bytes(functional.read(8),'little')&128:
                    raise ValueError('native event eligibility requires functional retirement companion')
        scanned=scan_functional_retirement(trace,m,evidence['rows'],tasks,
            load_event_policy=row.get('capabilities',{}).get('load_event_policy','legacy-demand-only-v1'))
        facts=load_macro_boundaries(trace,m)
        if facts:boundary_provenance.update(facts['provenance'])
        coverage=row.get('coverage',{})
        def gap(field,observed,expected,scope='physical'):
            gaps.append(dict(core_id=m['core_id'],scope=scope,field=field,
                             observed=observed,expected=expected))
        for name in ('common_cut_complete','kernel_stream_complete','initial_owner',
                     'target_drain_complete','functional_records_match','functional_macros_match'):
            if coverage.get(name) is not True:gap(name,coverage.get(name),True)
        for name in ('read_failures','identity_failures','unretained_hook_events','owner_unknown',
                     'pending_native','observed_leading_incomplete_macros','observed_boundary_discontinuities'):
            count=integer(coverage.get(name),name)
            if count:gap(name,count,0)
        if set(row.get('scopes',{}))!=set(SCOPES):raise ValueError('retirement scope participants mismatch')
        for scope in SCOPES:
            r=row['scopes'][scope]
            if r.get('available') is not True:gap('retirement_population_available',False,True,scope)
            for name in (*POP_FIELDS,*TAIL_FIELDS):
                observed=r.get(name) if name in POP_FIELDS else r.get('coverage',{}).get(name)
                if observed is not None:integer(observed,name)
                if observed!=scanned[scope][name]:gap(name,observed,scanned[scope][name],scope)
        if not facts:
            gap('complete_functional_macro_boundary_facts',False,True)
        retirement_provenance[str(path.resolve())]=hashlib.sha256(original).hexdigest()
        per_core.append(dict(core_id=m['core_id'],scopes=scanned))
    return dict(valid=True,task_ids=tasks,per_core=per_core,
                source_provenance=evidence['provenance'],evidence_gaps=gaps,
                macro_boundary_provenance=boundary_provenance,
                retirement_sidecar_provenance=retirement_provenance,
                retirement_qualification_complete=not gaps)


def audit_perf_cache(trace_dir, *, include_input_identity=False, allow_incomplete=False):
    trace=Path(trace_dir);dictionary=load_dictionary();oracle=trace/'oracle'
    paths=sorted(oracle.glob('perf-cache-core*.json'))
    if not paths:raise ValueError('missing v2 native cache request sidecar participants; no legacy fallback')
    audited=audit_request_population(trace) if allow_incomplete else audit_perf_retirement(trace)
    refs={r['core_id']:r for r in audited['per_core']}
    expected={f'perf-cache-core{core}.json' for core in refs}
    if {p.name for p in paths}!=expected:raise ValueError('missing or extra v2 cache sidecar participants')
    identity=None
    if include_input_identity:
        from perf_cache_source import collect_source_identity
        plan=trace.parent/'thread-state.plan'
        identity=collect_source_identity(trace/'manifest.txt',plan if plan.is_file() else '')
        if identity['status']!='complete':raise ValueError('functional record window ordinals unavailable')
        if set(identity['participants'])!=set(refs):raise ValueError('functional input participant mismatch')
    per_core=[];provenance=dict(audited.get('retirement_sidecar_provenance',{}));window=None
    gaps=list(audited.get('evidence_gaps',[]))
    for path in paths:
        original=path.read_bytes();raw=json.loads(original);core=raw.get('core_id')
        if core not in refs:raise ValueError('wrong native cache participant identity')
        ret=json.loads((oracle/f'perf-retirement-core{core}.json').read_text())
        if identity:
            source=next(r for r in identity['sources'] if r['core_id']==core)
            if (source['source_core_id']!=core or source['source_record_count']!=ret['source_record_count']
                    or source['score_begin_ordinal']!=ret['measurement_begin_ordinal']
                    or source['score_end_ordinal_exclusive']!=ret['measurement_end_ordinal_exclusive']):
                raise ValueError('functional input/native ordinal window boundaries differ')
        if (raw.get('schema')!='gem5-perf-cache-observations-v2' or raw.get('oracle_only') is not True
                or raw.get('fst_input') is not False or raw.get('evidence_source')!='native-o3-ruby-observers'
                or any(raw.get(k)!=ret.get(k) for k in BOUNDS)):
            raise ValueError('native v2 cache source/participant/boundary mismatch')
        observer=raw.get('observer',{})
        if observer.get('schema') not in (
                'native-cache-request-observer-v1',
                'native-cache-request-observer-v2'):
            raise ValueError('missing native request observer facts')
        if integer(observer.get('overflow'),'native observer overflow'):raise ValueError('native request observer overflow')
        # v2 adds per-window transaction conservation; the identity must
        # close even when transactions legitimately cross the common end.
        if observer.get('schema')=='native-cache-request-observer-v2':
            # residual = issued + inflight_begin - released - superseded - inflight_end
            cons=raw.get('native_observer',{}).get('window_conservation',{})
            for k in ('inflight_begin','inflight_end','released','superseded',
                      'balance_residual'):
                integer(cons.get(k),'window_conservation:'+k)
            if cons['balance_residual'] != 0:
                raise ValueError('native request window conservation mismatch')
        unknown=integer(observer.get('unknown_requests'),'native unknown requests')
        if set(raw.get('scopes',{}))!=set(SCOPES):raise ValueError('native cache scope mismatch')
        scopes={}
        for scope in SCOPES:
            r=raw['scopes'][scope]
            if r.get('retired_macro_instructions')!=refs[core]['scopes'][scope]['retired_macro_instructions']:
                if not allow_incomplete:raise ValueError('native cache current-FST macro population mismatch')
                gaps.append(dict(core_id=core,scope=scope,field='cache_macro_population',
                                 observed=r.get('retired_macro_instructions'),
                                 expected=refs[core]['scopes'][scope]['retired_macro_instructions']))
            for field in ('perf_retired_loads','perf_retired_stores'):
                if r.get('retirement_events',{}).get(field)!=refs[core]['scopes'][scope][field]:
                    if not allow_incomplete:raise ValueError('native cache current-FST retirement membership mismatch')
                    gaps.append(dict(core_id=core,scope=scope,field=field,
                                     observed=r.get('retirement_events',{}).get(field),expected=refs[core]['scopes'][scope][field]))
            scopes[scope]=normalized_scope(r,scope,[core],audited['task_ids'],dictionary,raw['common_cut_id'])
            if allow_incomplete:
                primitive_levels=r['request_primitives'].get('levels',r['request_primitives'])
                unknown_lookups=sum(v.get('unknown',{}).get(o,0) for v in primitive_levels.values() for o in OUTCOMES)
                for field,binding in dictionary['events'].items():
                    e=scopes[scope]['events'][field]
                    e['evidence_gaps']=[g for g in gaps if g['core_id']==core and g['scope'] in ('physical',scope)]
                    if binding['domain']=='retirement':
                        e['unknown_qualification_count']=integer(r['retirement_unknown'][field],field+':unknown')
                        if e['evidence_gaps']:
                            e['coverage']['macro_units']=False
                            e['value']=None
                    else:
                        e['request_unknown_diagnostics']=dict(
                            observer_unknown_requests=unknown if scope=='physical' else None,
                            unknown_primitive_lookups=unknown_lookups,
                            attribution='observer/request-class diagnostics; not a per-event missing-count bound')
                scopes[scope]=validate_metric(scopes[scope],'gem5')
            primitive_levels=r.get('request_primitives',{})
            primitive_levels=primitive_levels.get('levels',primitive_levels)
            primitive_unknown=sum(level.get('unknown',{}).get(o,0) for level in primitive_levels.values() for o in OUTCOMES)
            if (unknown or primitive_unknown) and any(scopes[scope]['events'][f]['coverage'].get(k,False)
                               for f in dictionary['events'] if dictionary['events'][f]['domain']=='request'
                               for k in ('data_reads','rfo','code_reads')):
                raise ValueError('unknown native request class contradicts full population coverage')
        for field,binding in dictionary['events'].items():
            p=scopes['physical']['events'][field]['diagnostic_value']
            for a,b in [('user','kernel'),('workload_task','other_task')]:
                children=scopes[a]['events'][field]['diagnostic_value']+scopes[b]['events'][field]['diagnostic_value']
                complete=(binding['domain']=='retirement' or scopes['physical']['events'][field]['coverage'].get('event_context',False))
                if children>p or complete and children!=p:
                    raise ValueError('native cache scope conservation mismatch')
        if path.read_bytes()!=original:raise ValueError('native cache source changed during audit')
        if window is not None and window!=raw['common_cut_id']:raise ValueError('native cache common window mismatch')
        window=raw['common_cut_id'];per_core.append(dict(core_id=core,scopes=scopes))
        provenance[str(path.resolve())]=hashlib.sha256(original).hexdigest()
    if load_capture_evidence(trace)['provenance']!=audited['source_provenance']:
        raise ValueError('native cache underlying FST/owner source changed during audit')
    validate_boundary_provenance(audited.get('macro_boundary_provenance',{}))
    if any(not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest
           for path,digest in provenance.items()):
        raise ValueError('native cache sidecar changed during audit')
    result=dict(schema='perf-cache-native-audit-v2',valid=True,contract_id=dictionary['contract_id'],
                task_ids=audited['task_ids'],source_provenance=provenance,
                retirement_source_provenance=audited['source_provenance'],per_core=per_core,
                scopes={scope:sum_metrics([r['scopes'][scope] for r in per_core],scope,dictionary) for scope in SCOPES})
    # Reuse already scanned and provenance-checked populations for retirement adapters.
    result['functional_retirement_populations']={str(r['core_id']):
        {scope:{key:r['scopes'][scope][key] for key in POP_FIELDS} for scope in SCOPES}
        for r in audited['per_core']}
    if allow_incomplete:
        result.update(evidence_gaps=gaps,retirement_qualification_complete=not gaps,
                      qualification_policy='incomplete-errors-explicit-v1')
    if identity:
        from perf_cache_source import collect_source_identity,validate_identity
        validate_identity(identity,collect_source_identity(trace/'manifest.txt',plan if plan.is_file() else ''))
        result['input_identity']=identity
    return result


def compare_report(prediction, audited, *, include_estimates=False, allow_incomplete_errors=False):
    """Validate model aggregates and independently pair each native scope.

    A producer without a proven source-window identity may expose diagnostics,
    but can never acquire a scored field by naming the native common window.
    """
    from perf_cache_contract import compare_metrics
    if (prediction.get('schema')!='fastsim-perf-cache-report-v2'
            or prediction.get('contract_id')!=load_dictionary()['contract_id']
            or audited.get('valid') is not True
            or set(prediction.get('scopes',{}))!=set(SCOPES)):
        raise ValueError('missing independent v2 model cache namespace')
    cores=prediction.get('per_core',[])
    ids=[r.get('core_id') for r in cores]
    if any(type(c) is not int or c<0 for c in ids) or len(ids)!=len(set(ids)) or set(ids)!={r['core_id'] for r in audited['per_core']}:
        raise ValueError('model/native cache per-core participant mismatch')
    normalized={};comparison_native=deepcopy(audited['scopes'])
    native_cores={r['core_id']:r for r in audited['per_core']}
    for scope in SCOPES:
        total=validate_metric(prediction['scopes'][scope],'fastsim')
        if total['measurement']['scope']!=scope or total['measurement']['core_ids']!=sorted(ids):
            raise ValueError('aggregate model cache scope/core identity mismatch')
        children=[];population_gaps=[]
        for core in cores:
            if set(core.get('scopes',{}))!=set(SCOPES):raise ValueError('model per-core cache scopes incomplete')
            m=validate_metric(core['scopes'][scope],'fastsim')
            if m['measurement']['core_ids']!=[core['core_id']]:raise ValueError('model per-core cache identity mismatch')
            if any(m['measurement'][k]!=total['measurement'][k] for k in ('scope','window_id','task_ids','context_policy')):
                raise ValueError('model child scope/window/task identity mismatch')
            reference_macros=native_cores[core['core_id']]['scopes'][scope]['retired_macro_instructions']
            if m['retired_macro_instructions']!=reference_macros:
                if not allow_incomplete_errors:raise ValueError('model/native per-core macro population mismatch')
                population_gaps.append(dict(core_id=core['core_id'],scope=scope,field='model_native_macro_population',
                                            observed=m['retired_macro_instructions'],expected=reference_macros))
            children.append(m)
        if total['retired_macro_instructions']!=sum(c['retired_macro_instructions'] for c in children):
            raise ValueError('model per-core macro sum mismatch')
        if not allow_incomplete_errors and total['retired_macro_instructions']!=audited['scopes'][scope]['retired_macro_instructions']:
            raise ValueError('model/native cache macro population mismatch')
        if population_gaps:
            for row in comparison_native[scope]['events'].values():
                row['evidence_gaps']=row.get('evidence_gaps',[])+population_gaps
        for field,row in total['events'].items():
            if row['diagnostic_value']!=sum(c['events'][field]['diagnostic_value'] for c in children):
                raise ValueError('model per-core event sum mismatch')
            if any(row['coverage'][k] and not all(c['events'][field]['coverage'][k] for c in children) for k in row['coverage']):
                raise ValueError('aggregate model coverage exceeds per-core coverage')
        normalized[scope]=total
    result=dict(schema='perf-cache-validation-v2',hardware_equivalence='unverified',scopes={})
    for scope,f in normalized.items():
        g=comparison_native[scope]
        a=f['measurement'];b=g['measurement']
        if a==b:
            result['scopes'][scope]=compare_metrics(f,g)
            continue
        if (any(a[k]!=b[k] for k in ('scope','core_ids','task_ids','context_policy'))
                or a.get('window_id')!='accepted-model-window-v1'
                or any(row['available'] or row['coverage']['roi_edges'] for row in f['events'].values())):
            raise ValueError('model/native cache counting scope/window mismatch')
        # Explicitly incomparable model window, not a rewritten certification.
        result['scopes'][scope]=dict(schema='perf-cache-comparison-v2',measurement=a,
            reference_measurement=b,reference_producer='gem5',hardware_equivalence='unverified',
            events={field:dict(status='unavailable',predicted=None,reference=None,
                predicted_per_1k_inst=None,reference_per_1k_inst=None,
                missing_model_capabilities=sorted(set(row['missing_capabilities']+['source_window_identity'])),
                missing_reference_capabilities=g['events'][field]['missing_capabilities'],
                model_diagnostic=row['diagnostic_value'],reference_diagnostic=g['events'][field]['diagnostic_value'])
                for field,row in f['events'].items()})
    if include_estimates:
        from perf_cache_source import validate_identity
        from perf_cache_contract import compare_estimates
        if not prediction.get('input_identity') or not audited.get('input_identity'):
            raise ValueError('estimated comparison requires independently verified functional input identity')
        validate_identity(prediction['input_identity'],audited['input_identity'])
        result['input_identity']=audited['input_identity']['source_identity']
        result['estimated_scopes']={scope:compare_estimates(f,comparison_native[scope],input_identity_verified=True,
                                                          allow_incomplete_errors=allow_incomplete_errors)
                                    for scope,f in normalized.items()}
        if allow_incomplete_errors:result['evidence_gaps']=audited.get('evidence_gaps',[])
    return result


def aggregate_report_accuracy(reports):
    from perf_cache_contract import aggregate_accuracy
    return {scope:aggregate_accuracy([r['perf_cache_validation']['scopes'][scope] for r in reports
                                     if r.get('status')=='passed' and r.get('perf_cache_validation',{}).get('schema')=='perf-cache-validation-v2'])
            for scope in SCOPES}


def aggregate_report_estimates(reports):
    from perf_cache_contract import aggregate_estimates
    return {scope:aggregate_estimates([r['perf_cache_validation']['estimated_scopes'][scope] for r in reports
                if r.get('status')=='passed' and scope in r.get('perf_cache_validation',{}).get('estimated_scopes',{})])
            for scope in SCOPES}
