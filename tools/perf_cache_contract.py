"""Ten SPR event contracts. Native/request evidence is audited by its producer adapter.

No legacy or path-class reference fallback. Coverage and hardware certification
are independent: a comparable simulator estimate is never a hardware certificate.
"""
from copy import deepcopy
import json
from pathlib import Path

DICTIONARY = Path(__file__).resolve().parents[1] / 'configs/perf-cache-event-dictionary-v2.json'
BINDINGS = {
    'perf_retired_loads': ('MEM_INST_RETIRED.ALL_LOADS', 0xd0, 0x81),
    'perf_retired_stores': ('MEM_INST_RETIRED.ALL_STORES', 0xd0, 0x82),
    'perf_l1d_load_hits': ('MEM_LOAD_RETIRED.L1_HIT', 0xd1, 0x01),
    'perf_l1d_load_misses': ('MEM_LOAD_RETIRED.L1_MISS', 0xd1, 0x08),
    'perf_l1d_load_fb_hits': ('MEM_LOAD_RETIRED.FB_HIT', 0xd1, 0x40),
    'perf_l2_accesses': ('L2_RQSTS.REFERENCES', 0x24, 0xff),
    'perf_l2_demand_misses': ('L2_RQSTS.ALL_DEMAND_MISS', 0x24, 0x27),
    'perf_l2_writebacks': ('L2_TRANS.L2_WB', 0x23, 0x40),
    'perf_llc_accesses_core': ('LONGEST_LAT_CACHE.REFERENCE', 0x2e, 0x4f),
    'perf_llc_misses_core': ('LONGEST_LAT_CACHE.MISS', 0x2e, 0x41),
}
SOURCES = {'fastsim': 'model-self-generated-events', 'gem5': 'native-o3-ruby-observers',
           'hardware': 'perf-event-open-raw'}


def integer(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}: expected nonnegative integer')
    return value


def required_capabilities(name):
    if name.startswith('perf_retired') or name.startswith('perf_l1d'):
        caps=['macro_units','isa_membership','retirement_context','roi_edges']
        if name.startswith('perf_l1d'):caps+=['member_service_facts','source_qualification']
        return caps
    if name=='perf_l2_writebacks':
        return ['writeback_observer','writeback_direction_mapping','event_context','roi_edges']
    caps=['data_reads','rfo','code_reads','speculative_code','speculative_data',
          'merge_policy','retry_policy','event_context','roi_edges']
    if name!='perf_l2_demand_misses':caps+=['software_prefetch','hardware_prefetch_l1_l2']
    if name.startswith('perf_llc'):caps+=['core_origin_filter','llc_data_state_mapping']
    return caps


def load_dictionary(path=None):
    document=json.loads(Path(path or DICTIONARY).read_text())
    if (document.get('schema')!='fastsim-perf-cache-event-dictionary-v2'
            or document.get('contract_id')!='spr-perf-cache-v2'
            or set(document.get('events',{}))!=set(BINDINGS)):
        raise ValueError('invalid ten-field perf cache dictionary')
    target=document.get('hardware_target',{})
    if (target.get('vendor'),target.get('family'),target.get('model'),target.get('event_table_version'),target.get('event_table_sha256'))!=('GenuineIntel',6,143,'1.40','95caad89cbe4c96647a540897251e1998edc7eee8efb2dc84a5b13f7d94ce102'):
        raise ValueError('unsupported hardware event target')
    for name,(event,code,mask) in BINDINGS.items():
        row=document['events'][name]
        retired=name.startswith('perf_retired') or name.startswith('perf_l1d')
        if (row.get('hardware_event'),row.get('event'),row.get('umask'))!=(event,code,mask):
            raise ValueError(f'{name}: wrong hardware encoding')
        if (row.get('domain')!=('retirement' if retired else 'request')
                or row.get('speculative') is not (not retired)
                or row.get('unit')!=('architectural-retired-macro-instruction' if retired else ('cache-writeback-transaction' if name=='perf_l2_writebacks' else 'cache-request'))
                or row.get('required_capabilities')!=required_capabilities(name)):
            raise ValueError(f'{name}: invalid domain/coverage definition')
    return document


def validate_metric(metric, producer):
    dictionary=load_dictionary()
    if producer not in SOURCES or metric.get('schema')!=producer+'-perf-cache-v2':
        raise ValueError('missing v2 cache PMU namespace/schema; no legacy fallback')
    if metric.get('contract_id')!=dictionary['contract_id'] or metric.get('hardware_target')!=dictionary['hardware_target']:
        raise ValueError('perf cache target/definition mismatch')
    if metric.get('evidence_source')!=SOURCES[producer] or metric.get('fst_input') is not False:
        raise ValueError('perf cache evidence source mismatch or oracle input')
    if producer=='gem5' and metric.get('oracle_only') is not True:
        raise ValueError('native cache reference must be oracle-only')
    if producer=='hardware':
        read=metric.get('perf_read',{})
        enabled=integer(read.get('time_enabled'),'hardware perf time_enabled')
        running=integer(read.get('time_running'),'hardware perf time_running')
        if not enabled or running!=enabled or read.get('scaled') is not False:
            raise ValueError('hardware perf multiplex/running evidence invalid')
    measurement=metric.get('measurement',{})
    cores=measurement.get('core_ids',[]);tasks=measurement.get('task_ids',[])
    if (measurement.get('scope') not in ('physical','user','kernel','workload_task','other_task')
            or not isinstance(measurement.get('window_id'),str) or not measurement['window_id']
            or measurement.get('context_policy')!='event-edge-context-v1'
            or not isinstance(cores,list) or not cores
            or any(type(c) is not int or c<0 for c in cores) or len(cores)!=len(set(cores))
            or not isinstance(tasks,list) or any(type(t) is not int or t<0 for t in tasks)
            or len(tasks)!=len(set(tasks))):
        raise ValueError('invalid perf cache measurement scope/window/core/context')
    denominator=integer(metric.get('retired_macro_instructions'),'macro denominator')
    if set(metric.get('events',{}))!=set(BINDINGS):
        raise ValueError('missing or extra v2 cache event')
    normalized=deepcopy(metric)
    for name,binding in dictionary['events'].items():
        row=normalized['events'][name]
        if any(row.get(k)!=binding[k] for k in ('event','umask','unit','domain')):
            raise ValueError(f'{name}: event identity or unit mismatch')
        integer(row.get('diagnostic_value'),name+':diagnostic_value')
        if binding['domain']=='retirement' and row['diagnostic_value']>denominator:
            raise ValueError(f'{name}: retired event exceeds macro population')
        coverage=row.get('coverage',{})
        if (not isinstance(coverage,dict)
                or any(type(coverage.get(k)) is not bool for k in binding['required_capabilities'])):
            raise ValueError(f'{name}: invalid/missing coverage declaration')
        missing=[k for k in binding['required_capabilities'] if not coverage[k]]
        available=not missing
        if available:
            integer(row.get('value'),name+':value')
            if row['value']!=row['diagnostic_value']:
                raise ValueError(f'{name}: published value differs from counted evidence')
        elif row.get('value') is not None:
            raise ValueError(f'{name}: unavailable coverage must have null value')
        if type(row.get('hardware_validated')) is not bool:
            raise ValueError(f'{name}: missing independent hardware status')
        # A flag is not a certification artifact. Producer adapters must audit
        # hardware evidence before a future contract may expose a strict status.
        row.update(available=available,missing_capabilities=missing,
                   per_1k_inst=1000*row['value']/denominator if available and denominator else None)
        estimate_keys={'estimated_value','estimate_status','estimate_policy','assumptions','missing_components'}
        if estimate_keys.intersection(row):
            if producer!='fastsim' or not estimate_keys.issubset(row):
                raise ValueError(f'{name}: incomplete or non-model estimate declaration')
            if row['estimate_policy']!='accepted-model-events-zero-unmodeled-v1':
                raise ValueError(f'{name}: unknown estimate policy')
            assumptions=row['assumptions'];components=row['missing_components']
            if (not isinstance(assumptions,list) or not assumptions
                    or any(not isinstance(a,str) or not a.strip() for a in assumptions)
                    or not isinstance(components,list) or any(not isinstance(c,str) for c in components)
                    or len(components)!=len(set(components)) or set(components)!=set(missing)):
                raise ValueError(f'{name}: invalid estimate assumptions/missing components')
            status=row['estimate_status']
            if status=='approximate':
                v=integer(row['estimated_value'],name+':estimated_value')
                if v!=row['diagnostic_value'] or binding['domain']=='retirement' and v>denominator:
                    raise ValueError(f'{name}: estimated count differs from policy or macro population')
            elif status!='unavailable' or row['estimated_value'] is not None:
                raise ValueError(f'{name}: invalid estimate status/value')
            row['estimated_per_1k_inst']=(1000*row['estimated_value']/denominator
                if status=='approximate' and denominator else None)
    return normalized


def compare_estimates(prediction, reference, *, input_identity_verified=False,
                      allow_incomplete_errors=False):
    """Evaluate model approximations without weakening reference/source gates.

    A complete reference and identical measurement can score an approximate
    model. An incomplete reference or an explicitly approximated request window
    yields diagnostic differences only, never a hardware accuracy headline.
    """
    producer=next((p for p in ('gem5','hardware') if reference.get('schema')==p+'-perf-cache-v2'),None)
    if producer is None:raise ValueError('missing native/hardware reference namespace')
    f=validate_metric(prediction,'fastsim');g=validate_metric(reference,producer)
    a=f['measurement'];b=g['measurement'];same=a==b
    if not same and (not input_identity_verified or a['window_id']!='accepted-model-window-v1'
            or any(a[k]!=b[k] for k in ('scope','core_ids','task_ids','context_policy'))):
        raise ValueError('estimate source/scope/context/window mismatch')
    out=dict(schema='perf-cache-estimate-comparison-v1',contract_id=f['contract_id'],
             measurement=a,reference_measurement=b,reference_producer=producer,
             window_status='matched' if same else 'source-record-window-approximation',
             hardware_equivalence='unverified',events={})
    for name in BINDINGS:
        x=f['events'][name];y=g['events'][name]
        row=dict(status='unavailable',predicted=x.get('estimated_value'),reference=None,
                 assumptions=x.get('assumptions',[]),missing_model_components=x['missing_capabilities'],
                 missing_reference_capabilities=y['missing_capabilities'],reasons=[])
        if x.get('estimate_status')!='approximate':row['reasons'].append('model_estimate_unavailable')
        elif x['domain']=='request' and a['scope']!='physical' and (
                not x['coverage'].get('event_context') or not y['coverage'].get('event_context')):
            row['reasons'].append('event_context')
        else:
            complete=y['available'] and same
            r=y['value'] if y['available'] else y['diagnostic_value'];p=x['estimated_value']
            row.update(status='scored_estimate' if complete else 'diagnostic_only',
                       reference=r,reference_status='complete' if y['available'] else 'native-diagnostic-incomplete',
                       predicted_per_1k_inst=x['estimated_per_1k_inst'],
                       reference_per_1k_inst=1000*r/g['retired_macro_instructions'] if g['retired_macro_instructions'] else None,
                       signed_difference=p-r,absolute_difference=abs(p-r),
                       absolute_percentage_difference=100*abs(p-r)/r if r else (0 if not p else None))
            if not same:row['reasons'].append('counting_window_approximation')
            if not y['available']:row['reasons'].append('reference_incomplete')
            if allow_incomplete_errors:
                gaps=list(y.get('evidence_gaps',[]))
                if f['retired_macro_instructions']!=g['retired_macro_instructions']:
                    gaps.append(dict(field='model_native_macro_population',scope=a['scope'],
                                     observed=f['retired_macro_instructions'],expected=g['retired_macro_instructions']))
                complete=complete and not gaps
                row.update(error_evidence='complete' if complete else 'incomplete',
                           absolute_error=abs(p-r),signed_error=p-r,
                           relative_error=(p-r)/r if r else (0 if not p else None),
                           absolute_relative_error=abs(p-r)/r if r else (0 if not p else None),
                           absolute_percentage_error=100*abs(p-r)/r if r else (0 if not p else None),
                           evidence_gaps=gaps,
                           reference_unknown_qualification_count=y.get('unknown_qualification_count'),
                           reference_request_unknown_diagnostics=y.get('request_unknown_diagnostics'),
                           model_macro_denominator=f['retired_macro_instructions'],
                           reference_macro_denominator=g['retired_macro_instructions'])
                pr=row['predicted_per_1k_inst'];rr=row['reference_per_1k_inst']
                row['per_1k_inst_errors']=(dict(signed_error=pr-rr,absolute_error=abs(pr-rr),
                    relative_error=(pr-rr)/rr if rr else (0 if not pr else None),
                    absolute_percentage_error=100*abs(pr-rr)/rr if rr else (0 if not pr else None))
                    if pr is not None and rr is not None else
                    dict(signed_error=None,absolute_error=None,relative_error=None,absolute_percentage_error=None))
                if gaps:row['reasons'].append('retirement_source_evidence_incomplete')
                if not complete:row['status']='diagnostic_only'
        out['events'][name]=row
    return out


def aggregate_estimates(reports):
    valid=[r for r in reports if r.get('schema')=='perf-cache-estimate-comparison-v1']
    keys={(r['measurement']['scope'],r['measurement']['context_policy'],r['reference_producer']) for r in valid}
    if len(keys)>1:raise ValueError('cannot pool estimate scopes or reference producers')
    out=dict(schema='perf-cache-estimate-summary-v1',hardware_equivalence='unverified',events={})
    for name in BINDINGS:
        scored=[r['events'][name] for r in valid if r['events'][name]['status']=='scored_estimate']
        diagnostic=[r['events'][name] for r in valid if r['events'][name]['status']=='diagnostic_only']
        incomplete=[r for r in diagnostic if r.get('error_evidence')=='incomplete']
        entry=dict(cases=len(valid),accuracy_cases=len(scored),diagnostic_cases=len(diagnostic),
                   unavailable_cases=len(valid)-len(scored)-len(diagnostic),accuracy={},diagnostic_differences={})
        for unit,pk,rk in [('raw','predicted','reference'),('per_1k_inst','predicted_per_1k_inst','reference_per_1k_inst')]:
            def stats(rows):return error_statistics([(r[pk],r[rk]) for r in rows if r.get(pk) is not None and r.get(rk) is not None])
            entry['accuracy'][unit]=stats(scored)
            d=stats(diagnostic)
            entry['diagnostic_differences'][unit]={
                'cases':d['cases'],'finite_percentage_cases':d['finite_ape_cases'],
                'zero_reference_cases':d['zero_reference_cases'],
                'zero_reference_nonzero_prediction_cases':d['zero_reference_nonzero_prediction_cases'],
                'mean_absolute_percentage_difference':d['mape_percent'],
                'max_absolute_percentage_difference':d['max_ape_percent'],
                'weighted_absolute_percentage_difference':d['wape_percent'],
                'signed_bias_percent':d['signed_bias_percent'],'absolute_count_difference':d['absolute_count_error']}
            entry.setdefault('incomplete_evidence_errors',{})[unit]=stats(incomplete)
        entry['incomplete_evidence_error_cases']=len(incomplete)
        out['events'][name]=entry
    return out


def compare_metrics(prediction, reference):
    producer=next((p for p in ('gem5','hardware') if reference.get('schema')==p+'-perf-cache-v2'),None)
    if producer is None: raise ValueError('missing native/hardware reference namespace')
    f=validate_metric(prediction,'fastsim');g=validate_metric(reference,producer)
    if f['measurement']!=g['measurement']:
        raise ValueError('perf cache scope/task/core/window mismatch')
    result=dict(schema='perf-cache-comparison-v2',contract_id=f['contract_id'],
                measurement=f['measurement'],reference_producer=producer,
                hardware_equivalence='unverified',events={})
    for name in BINDINGS:
        a,b=f['events'][name],g['events'][name]
        gaps=list(b.get('evidence_gaps',[]))
        available=a['available'] and b['available'] and not gaps and not a.get('evidence_gaps')
        row=dict(status='scored' if available else 'unavailable',
                 missing_model_capabilities=a['missing_capabilities'],
                 missing_reference_capabilities=b['missing_capabilities'],
                 predicted=a['value'] if available else None,
                 reference=b['value'] if available else None,
                 predicted_per_1k_inst=a['per_1k_inst'] if available else None,
                 reference_per_1k_inst=b['per_1k_inst'] if available else None,
                 model_diagnostic=a['diagnostic_value'],reference_diagnostic=b['diagnostic_value'])
        if gaps:row['missing_reference_evidence']=gaps
        result['events'][name]=row
    return result


def error_statistics(pairs):
    apes=[];absolute=0;reference=0;bias=0;zero=0;nonzero_zero=0
    for p,r in pairs:
        absolute+=abs(p-r);reference+=r;bias+=p-r
        if r:
            apes.append(100*abs(p-r)/r)
        else:
            zero+=1
            if p:nonzero_zero+=1
            else:apes.append(0)
    apes.sort()
    def percentile(q):
        if not apes:return None
        x=(len(apes)-1)*q;lo=int(x);hi=min(lo+1,len(apes)-1)
        return apes[lo]+(apes[hi]-apes[lo])*(x-lo)
    return dict(cases=len(pairs),finite_ape_cases=len(apes),zero_reference_cases=zero,
                zero_reference_nonzero_prediction_cases=nonzero_zero,
                mape_percent=sum(apes)/len(apes) if apes else None,
                max_ape_percent=max(apes) if apes else None,
                p50_ape_percent=percentile(.5),p90_ape_percent=percentile(.9),p99_ape_percent=percentile(.99),
                wape_percent=100*absolute/reference if reference else None,
                signed_bias_percent=100*bias/reference if reference else None,
                absolute_count_error=absolute)


def aggregate_accuracy(reports):
    result=dict(schema='perf-cache-accuracy-v2',hardware_equivalence='unverified',events={})
    valid=[r for r in reports if r.get('schema')=='perf-cache-comparison-v2']
    # Callers group by measurement scope AND reference producer. Never pool
    # hardware and native references, or two privilege/task populations.
    keys={(r.get('measurement',{}).get('scope'),r.get('measurement',{}).get('context_policy'),r.get('reference_producer')) for r in valid}
    if len(keys)>1:raise ValueError('cannot pool cache scopes/windows/reference producers')
    for name in BINDINGS:
        scored=[r['events'][name] for r in valid if r['events'][name]['status']=='scored']
        entry=dict(cases=len(valid),scored_cases=len(scored),unavailable_cases=len(valid)-len(scored))
        for unit,pk,rk in [('raw','predicted','reference'),('per_1k_inst','predicted_per_1k_inst','reference_per_1k_inst')]:
            entry[unit]=error_statistics([(r[pk],r[rk]) for r in scored if r[pk] is not None and r[rk] is not None])
        result['events'][name]=entry
    return result
