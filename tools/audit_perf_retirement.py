"""Independent architectural population audit; never substitutes path-class cache truth."""
import argparse
from copy import deepcopy
import json
import hashlib
import math
import mmap
from pathlib import Path
import struct

from thread_state_graph import COMMON_CUT_ID, has_certified_functional_ownership, load_capture_evidence
from time_accounting import assemble_gem5_time_accounting
from fst_memory_request_roles import load_request_roles
from fst_retirement_eligibility import load_retirement_eligibility

SCOPES = ('physical', 'user', 'kernel', 'workload_task', 'other_task')
RAW_FIELDS = ('perf_retired_loads', 'perf_retired_stores')
SOURCE_FIELDS = ('perf_l1d_load_hits', 'perf_l1d_load_misses', 'perf_l1d_load_fb_hits')
POP_FIELDS = ('retired_macro_instructions', 'retired_uops', *RAW_FIELDS)
TAIL_FIELDS = ('incomplete_macros', 'incomplete_uops', 'scope_crossing_macros',
               'warmup_spanning_macros')
OPTIONAL_COUNTS = ('native_retired_uops',)
OPTIONAL_COVERAGE = ('mixed_source_loads', 'forwarded_loads', 'leading_incomplete_macros',
                     'unresolved_source_uops', 'unsupported_loads', 'rom_entry_macros',
                     'rom_microcode_macros', 'boundary_discontinuities',
                     'all_history_incomplete_macros','all_history_incomplete_uops','excluded_hardware_entry_macros','precise_fault_abandoned_macros')
EVENT_DICTIONARY = Path(__file__).resolve().parents[1] / 'configs/perf-retirement-event-dictionary-v1.json'


def load_event_dictionary(path=EVENT_DICTIONARY):
    document = json.loads(Path(path).read_text())
    if (document.get('schema') != 'fastsim-perf-retirement-events-v1'
            or document.get('perf_equivalence') != 'unverified'
            or set(document.get('events', {})) != set((*RAW_FIELDS, *SOURCE_FIELDS))):
        raise ValueError('invalid retirement event dictionary')
    for field, event in document['events'].items():
        if (event.get('unit') != 'architectural-retired-macro-instruction'
                or event.get('mapping') != 'proxy'
                or event.get('reference_source') != ('functional-commit' if field in RAW_FIELDS else 'native-ruby-service-facts')):
            raise ValueError(f'{field}: invalid retirement mapping or reference source')
    return document


def integer(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f'{label}: expected nonnegative integer')
    return value


def blank():
    return {name: dict.fromkeys((*POP_FIELDS, *TAIL_FIELDS), 0) for name in SCOPES}


def load_macro_boundaries(trace, meta):
    """Read complete exceptional starts; ordinary last-micro bits stay in FST."""
    trace=Path(trace);core=meta['core_id']
    path=trace/f'macro-boundary-metadata-core{core}.json'
    events_path=trace/f'macro-boundary-core{core}.jsonl'
    if not path.is_file():
        if events_path.exists():raise ValueError('macro boundary facts lack complete metadata')
        return None
    original=path.read_bytes();m=json.loads(original)
    required=dict(schema='fastsim-functional-macro-boundary-metadata-v1',core_id=core,
                  source_file=meta['source_file'],source_record_count=meta['source_record_count'],
                  measurement_begin_ordinal=meta['warmup_records'],complete=True,dropped_events=0,
                  incomplete_scope='measured-functional-members-v2',event_file=events_path.name)
    if any(m.get(k)!=v for k,v in required.items()):raise ValueError('incomplete or misbound macro boundary facts')
    content=events_path.read_bytes();rows=[json.loads(s) for s in content.splitlines() if s.strip()]
    if integer(m.get('event_count'),'macro boundary event count')!=len(rows):
        raise ValueError('macro boundary fact count mismatch')
    events={};previous=-1
    for r in rows:
        ordinal=integer(r.get('record_ordinal'),'macro boundary ordinal')
        integer(r.get('context_id'),'macro boundary context');integer(r.get('pc'),'macro boundary PC')
        if (r.get('schema')!='fastsim-functional-macro-boundary-v1' or r.get('core_id')!=core
                or ordinal<=previous or ordinal>=meta['source_record_count']
                or any(type(r.get(k)) is not bool for k in ('starts_macro','rom_entry','debug_prefix','had_open_macro'))
                or (r['rom_entry'] or r['debug_prefix']) and not r['starts_macro']
                or not r['starts_macro'] and r['had_open_macro']):
            raise ValueError('invalid or unordered macro boundary fact')
        events[ordinal]=r;previous=ordinal
    provenance={str(path.resolve()):hashlib.sha256(original).hexdigest(),
                str(events_path.resolve()):hashlib.sha256(content).hexdigest()}
    return dict(events=events,provenance=provenance)


def validate_boundary_provenance(provenance):
    for path,digest in provenance.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:
            raise ValueError('macro boundary source changed during audit')


def scan_functional_retirement(trace, meta, rows, task_ids, *, request_provenance=None, load_event_policy="legacy-demand-only-v1"):
    """Scan prehistory too; only a measured last-micro retires a macro event."""
    if load_event_policy not in ('legacy-demand-only-v1','spr-software-prefetch-v1'):
        raise ValueError('unsupported retirement load event policy')
    trace = Path(trace)
    events = [r for r in rows if r['core_id'] == meta['core_id']]
    initial = [r for r in events if r['event_kind'] == 'initial_owner']
    if len(initial) != 1 or initial[0]['record_ordinal'] != 0:
        raise ValueError('retirement population lacks initial owner at source start')
    owner = initial[0]['os_tid']
    switches = {r['record_ordinal']: r for r in events if r['event_kind'] == 'sched_switch'}
    if len(switches) != sum(r['event_kind'] == 'sched_switch' for r in events):
        raise ValueError('duplicate retirement owner switch')
    facts=load_macro_boundaries(trace,meta)
    boundaries=facts['events'] if facts else {}
    result = blank()
    if facts:
        for r in result.values():r.update(all_history_incomplete_macros=0,all_history_incomplete_uops=0)
    start = None; members = measured_members = 0; read = write = crossing = False; identity = None

    def add(delta, kernel, at_owner=None):
        selected_owner=owner if at_owner is None else at_owner
        selected = ('physical', 'kernel' if kernel else 'user',
                    'workload_task' if selected_owner in task_ids else 'other_task')
        for scope in selected:
            for key, value in delta.items(): result[scope][key] += value

    def tail():
        if facts:
            delta=dict(incomplete_macros=int(bool(measured_members)),incomplete_uops=measured_members,
                       all_history_incomplete_macros=1,all_history_incomplete_uops=members)
        else:delta=dict(incomplete_macros=1,incomplete_uops=members)
        add(delta,identity[1],identity[0])

    source = trace / meta['source_file']
    with source.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        count = meta['source_record_count']; begin = meta['warmup_records']
        if len(data) < 72 + 64 * count:
            raise ValueError('truncated retirement FST source')
        request_roles, role_provenance = load_request_roles(source, data, meta['core_id'], count)
        hardware_entries, precise_aborts, origin_provenance=load_retirement_eligibility(source,data,meta['core_id'],count)
        role_provenance.update(origin_provenance)
        if precise_aborts:
            for r in result.values():r['precise_fault_abandoned_macros']=0
        if request_provenance is not None: request_provenance.update(role_provenance)
        raw_syscalls = bool(struct.unpack_from("<Q", data, 32)[0] & (1 << 8))
        for ordinal in range(count):
            flags, op = struct.unpack_from('<Hh', data, 72 + 64 * ordinal + 50)
            if raw_syscalls and op == -1:
                raise ValueError("raw syscall trace contains collapsed marker")
            kernel = op < -1
            boundary=boundaries.get(ordinal)
            if ordinal in precise_aborts:
                if start is None or not boundary or not boundary['rom_entry'] or not boundary['had_open_macro']:
                    raise ValueError('precise fault origin lacks matching open macro boundary/prefix')
                add(dict(precise_fault_abandoned_macros=int(bool(measured_members))),identity[1],identity[0])
            if boundary:
                pc=struct.unpack_from('<Q',data,72+64*ordinal)[0]
                if not flags&1 or pc!=boundary['pc'] or boundary['had_open_macro']!=(start is not None):
                    raise ValueError('macro boundary fact disagrees with current FST prefix')
                if boundary['starts_macro'] and start is not None:
                    tail();start=None;members=measured_members=0
                    read=write=crossing=False;identity=None
            if flags & 1:
                if start is None: start = ordinal
                if identity is not None and identity != (owner, kernel): crossing = True
                identity = (owner, kernel); members += 1
                measured_members+=ordinal>=begin
                # Preserve historical sources; the explicit new producer
                # policy counts ISA hints in SPR ALL_LOADS, without a store.
                if ordinal in hardware_entries:
                    pass # internal interrupt-entry work, not an x86 memory instruction
                elif request_roles.get(ordinal) == 2:
                    read |= load_event_policy == 'spr-software-prefetch-v1'
                else:
                    read |= bool(flags & (2 | 8)); write |= bool(flags & (4 | 8))
                if ordinal >= begin: add({'retired_uops': 1}, kernel)
                if not flags & 1024 or flags & 2048:
                    if ordinal >= begin:
                        add(dict(retired_macro_instructions=1, perf_retired_loads=int(read),
                                 perf_retired_stores=int(write), scope_crossing_macros=int(crossing),
                                 warmup_spanning_macros=int(start < begin)), kernel)
                    start = None; members = measured_members = 0; read = write = crossing = False; identity = None
            if ordinal in switches:
                event = switches[ordinal]
                if owner != event['prev_pid']: raise ValueError('retirement owner discontinuity')
                owner = event['next_pid']
        if start is not None:
            tail()
    if facts:validate_boundary_provenance(facts['provenance'])
    validate_boundary_provenance(role_provenance)
    return result


def validate_counts(row, label):
    if row.get('available') is not True:
        raise ValueError(f'{label}: retirement population unavailable')
    for field in (*POP_FIELDS, *SOURCE_FIELDS): integer(row.get(field), f'{label}:{field}')
    coverage = row.get('coverage', {})
    for field in (*TAIL_FIELDS, 'classified_loads', 'unclassified_loads'):
        integer(coverage.get(field), f'{label}:{field}')
    for field in OPTIONAL_COUNTS:
        if field in row: integer(row[field], f'{label}:{field}')
    for field in OPTIONAL_COVERAGE:
        if field in coverage: integer(coverage[field], f'{label}:{field}')
    if (coverage['classified_loads'] + coverage['unclassified_loads'] != row['perf_retired_loads']
            or sum(row[field] for field in SOURCE_FIELDS) != coverage['classified_loads']):
        raise ValueError(f'{label}: load source partition not conserved')
    if any(row[field] > row['retired_macro_instructions'] for field in RAW_FIELDS):
        raise ValueError(f'{label}: retired memory instructions exceed macro population')


def sum_scopes(rows):
    result = deepcopy(rows[0])
    for scope in SCOPES:
        for key in (*POP_FIELDS, *SOURCE_FIELDS):
            result[scope][key] = sum(row[scope][key] for row in rows)
        for key in (*TAIL_FIELDS, 'classified_loads', 'unclassified_loads'):
            result[scope]['coverage'][key] = sum(row[scope]['coverage'][key] for row in rows)
        for key in OPTIONAL_COUNTS:
            if any(key in row[scope] for row in rows):
                if not all(key in row[scope] for row in rows): raise ValueError('inconsistent optional native count coverage')
                result[scope][key] = sum(row[scope][key] for row in rows)
        for key in OPTIONAL_COVERAGE:
            if any(key in row[scope]['coverage'] for row in rows):
                if not all(key in row[scope]['coverage'] for row in rows): raise ValueError('inconsistent optional source coverage')
                result[scope]['coverage'][key] = sum(row[scope]['coverage'][key] for row in rows)
    return result


def conserve(scopes, label):
    for row in scopes.values(): validate_counts(row, label)
    optional_counts = tuple(k for k in OPTIONAL_COUNTS if any(k in s for s in scopes.values()))
    for field in (*POP_FIELDS, *SOURCE_FIELDS, *optional_counts):
        if any(scopes['physical'][field] != scopes[a][field] + scopes[b][field]
               for a, b in (('user', 'kernel'), ('workload_task', 'other_task'))):
            raise ValueError(f'{label}: scope conservation failed for {field}')
    optional_coverage = tuple(k for k in OPTIONAL_COVERAGE if any(k in s['coverage'] for s in scopes.values()))
    for field in (*TAIL_FIELDS, 'classified_loads', 'unclassified_loads', *optional_coverage):
        if any(scopes['physical']['coverage'][field] !=
               scopes[a]['coverage'][field] + scopes[b]['coverage'][field]
               for a, b in (('user', 'kernel'), ('workload_task', 'other_task'))):
            raise ValueError(f'{label}: scope coverage conservation failed for {field}')


def audit_perf_retirement(trace_dir):
    trace = Path(trace_dir)
    evidence = load_capture_evidence(trace)
    meta, events, graph = (evidence[k] for k in ('metadata', 'rows', 'graph'))
    if not meta or not has_certified_functional_ownership(graph):
        raise ValueError('retirement requires complete certified functional ownership')
    tasks = sorted(t['os_tid'] for t in graph['tasks'])
    expected = {f'perf-retirement-core{m["core_id"]}.json' for m in meta}
    paths = sorted((trace / 'oracle').glob('perf-retirement-core*.json'))
    if {p.name for p in paths} != expected:
        raise ValueError('missing or extra retirement sidecar participants')
    clocks = [json.loads((trace / f'oracle/time-accounting-core{m["core_id"]}.json').read_text()) for m in meta]
    clock = assemble_gem5_time_accounting(clocks, cores=len(meta))
    if clock['elapsed_status'] != 'available': raise ValueError('retirement common clock unavailable')
    per_core = [];boundary_provenance={};request_provenance={}
    for m in meta:
        path = trace / f'oracle/perf-retirement-core{m["core_id"]}.json'
        row = json.loads(path.read_text())
        required = dict(schema='gem5-perf-retirement-v1', oracle_only=True, fst_input=False,
                        unit='architectural-retired-macro-instruction',
                        population_source='gem5-o3-committed-DynInst',
                        population_contract='task-oncpu-switch-entry-v1',
                        core_id=m['core_id'], source_file=m['source_file'],
                        source_record_count=m['source_record_count'],
                        measurement_begin_ordinal=m['warmup_records'],
                        measurement_end_ordinal_exclusive=m['source_record_count'],
                        common_start_tick=clock['common_start_tick'], common_end_tick=clock['common_end_tick'],
                        common_cut_id=COMMON_CUT_ID, task_ids=tasks,
                        source_classification='native-ruby-service-facts', perf_equivalence='unverified')
        if any(row.get(k) != v for k, v in required.items()):
            raise ValueError(f'{path}: retirement source/bounds/task contract mismatch')
        if row.get('capabilities',{}).get('memory_event_eligibility_policy')=='functional-retirement-v1':
            with (trace/m['source_file']).open('rb') as functional:
                functional.seek(32)
                if not int.from_bytes(functional.read(8),'little')&128:
                    raise ValueError('native event eligibility requires functional retirement companion')
        coverage = row.get('coverage', {})
        if any(coverage.get(k) is not True for k in ('initial_owner', 'common_cut_complete',
                                                   'kernel_stream_complete', 'target_drain_complete',
                                                   'functional_records_match', 'functional_macros_match')):
            raise ValueError(f'{path}: incomplete retirement source coverage')
        if any(integer(coverage.get(k), k) for k in ('read_failures', 'identity_failures',
                                                   'unretained_hook_events', 'owner_unknown', 'pending_native',
                                                   'observed_leading_incomplete_macros',
                                                   'observed_boundary_discontinuities')):
            raise ValueError(f'{path}: failed retirement source coverage')
        scopes = row.get('scopes', {})
        if set(scopes) != set(SCOPES): raise ValueError('retirement scope coverage mismatch')
        conserve(scopes, str(path))
        facts=load_macro_boundaries(trace,m)
        if row.get('capabilities',{}).get('incomplete_scope')=='measured-functional-members-v2' and not facts:
            raise ValueError('measured retirement tails require complete functional macro boundaries')
        if facts:boundary_provenance.update(facts['provenance'])
        scanned = scan_functional_retirement(trace, m, events, tasks,
                                             request_provenance=request_provenance,
                                             load_event_policy=row.get('capabilities',{}).get(
                                                 'load_event_policy','legacy-demand-only-v1'))
        for name in SCOPES:
            if 'precise_fault_abandoned_macros' in scanned[name] and scanned[name]['precise_fault_abandoned_macros']!=scopes[name]['coverage'].get('precise_fault_abandoned_macros'):
                raise ValueError('precise fault abandoned prefix population mismatch')
            for key in (*POP_FIELDS, *TAIL_FIELDS):
                observed = scopes[name].get(key) if key in POP_FIELDS else scopes[name]['coverage'].get(key)
                if observed != scanned[name][key]:
                    raise ValueError(f'{path}: {name}:{key} independent functional population mismatch '
                                     f'{observed} vs {scanned[name][key]}')
        per_core.append(dict(core_id=m['core_id'], scopes=scopes))
    if load_capture_evidence(trace)['provenance'] != evidence['provenance']:
        raise ValueError('retirement source changed during audit')
    validate_boundary_provenance(boundary_provenance)
    validate_boundary_provenance(request_provenance)
    return dict(schema='fastsim-perf-retirement-audit-v1', valid=True, task_ids=tasks,
                scopes=sum_scopes([r['scopes'] for r in per_core]), per_core=per_core,
                source_provenance=evidence['provenance'],macro_boundary_provenance=boundary_provenance,
                memory_request_provenance=request_provenance,
                perf_equivalence='unverified')


def compare_perf_retirement(metric, audited, *, task_ids=None):
    load_event_dictionary()
    if (metric.get('schema') != 'fastsim-perf-retirement-v1' or not audited.get('valid')
            or metric.get('unit') != 'architectural-retired-macro-instruction'
            or metric.get('population_source') != 'accepted-functional-prefix'
            or metric.get('source_classification') != 'model-estimate-not-native-cache-truth'
            or metric.get('perf_equivalence') != 'unverified'):
        raise ValueError('missing or incompatible new retirement namespace; no legacy fallback')
    ids = task_ids if task_ids is not None else metric.get('task_ids')
    if ids != audited['task_ids']: raise ValueError('retirement task identity mismatch')
    if set(metric.get('scopes', {})) != set(SCOPES): raise ValueError('retirement scope mismatch')
    conserve(metric['scopes'], 'FastSim retirement')
    cores = {r['core_id']: r for r in metric.get('per_core', [])}
    refs = {r['core_id']: r for r in audited['per_core']}
    if len(cores) != len(metric.get('per_core', [])) or set(cores) != set(refs):
        raise ValueError('retirement per-core participant mismatch')
    for core, row in cores.items():
        conserve(row['scopes'], f'FastSim core{core}')
        for scope in SCOPES:
            for field in POP_FIELDS:
                if row['scopes'][scope][field] != refs[core]['scopes'][scope][field]:
                    raise ValueError(f'core{core}:{scope}:{field} retirement population mismatch')
            for field in TAIL_FIELDS:
                if row['scopes'][scope]['coverage'][field] != refs[core]['scopes'][scope]['coverage'][field]:
                    raise ValueError(f'core{core}:{scope}:{field} retirement functional coverage mismatch')
    summed = sum_scopes([r['scopes'] for r in cores.values()])
    for scope in SCOPES:
        for field in (*POP_FIELDS, *SOURCE_FIELDS):
            if metric['scopes'][scope][field] != summed[scope][field]:
                raise ValueError('FastSim retirement per-core aggregate conservation mismatch')
        for field in (*TAIL_FIELDS, 'classified_loads', 'unclassified_loads',
                      *(k for k in OPTIONAL_COVERAGE if k in metric['scopes'][scope]['coverage'])):
            if metric['scopes'][scope]['coverage'][field] != summed[scope]['coverage'][field]:
                raise ValueError('FastSim retirement per-core coverage conservation mismatch')
    result = {}
    for name in SCOPES:
        predicted, reference = metric['scopes'][name], audited['scopes'][name]
        fields = {}
        for field in (*RAW_FIELDS, *SOURCE_FIELDS):
            p, r = predicted[field], reference[field]
            available = field in RAW_FIELDS or all(
                row['coverage']['unclassified_loads'] == 0 and row['coverage']['scope_crossing_macros'] == 0
                for row in (predicted, reference))
            pd, rd = predicted['retired_macro_instructions'], reference['retired_macro_instructions']
            delta = p - r
            pr, rr = (1000*p/pd if pd else None), (1000*r/rd if rd else None)
            fields[field] = dict(status='scored-functional-population' if field in RAW_FIELDS else
                                  'scored-native-service-diagnostic' if available else 'unavailable-source-coverage',
                                  fastsim=p, gem5=r, absolute_error=abs(delta) if available else None,
                                  signed_error=delta if available else None,
                                  absolute_percentage_error=(abs(delta)/r*100 if r else 0 if p == 0 else None) if available else None,
                                  signed_relative_error=(delta/r if r else 0 if p == 0 else None) if available else None,
                                  fastsim_per_1k_inst=pr, gem5_per_1k_inst=rr,
                                  rate_absolute_error=abs(pr-rr) if available and pr is not None and rr is not None else None,
                                  rate_absolute_percentage_error=(abs(pr-rr)/rr*100 if rr else 0 if pr == 0 else None)
                                  if available and pr is not None and rr is not None else None,
                                  reference_source='gem5-perf-retirement-functional-commit' if field in RAW_FIELDS
                                  else 'native-ruby-service-facts', mapping='proxy', hardware_formal=False)
        result[name] = dict(retired_macro_instructions=reference['retired_macro_instructions'],
                            fields=fields, source_coverage=dict(fastsim=predicted['coverage'], gem5=reference['coverage']))
    return dict(schema='fastsim-perf-retirement-comparison-v1', status='population-validated',
                perf_equivalence='unverified', task_ids=audited['task_ids'], scopes=result,
                request_events_status='unavailable-full-code-prefetch-speculation-population')


def aggregate_perf_retirement_accuracy(reports):
    """Keep each scope, event, and raw/rate population separate."""
    comparisons = [r['perf_retirement_validation'] for r in reports
                   if r.get('status') == 'passed'
                   and r.get('perf_retirement_validation', {}).get('status') == 'population-validated']
    result = {}
    for scope in SCOPES:
        result[scope] = {}
        for field in (*RAW_FIELDS, *SOURCE_FIELDS):
            rows = [c['scopes'][scope]['fields'][field] for c in comparisons]
            scored = [r for r in rows if r['absolute_error'] is not None]
            entry = dict(cases=len(rows), scored_cases=len(scored), unavailable_cases=len(rows)-len(scored),
                         mapping='proxy', perf_equivalence='unverified')
            for kind, pk, rk, ek in (('raw', 'fastsim', 'gem5', 'absolute_percentage_error'),
                                      ('per_1k_inst', 'fastsim_per_1k_inst', 'gem5_per_1k_inst',
                                       'rate_absolute_percentage_error')):
                selected = [r for r in scored if r[pk] is not None and r[rk] is not None]
                apes = sorted(r[ek] for r in selected if r[ek] is not None and math.isfinite(r[ek]))
                reference = sum(r[rk] for r in selected)
                def percentile(q):
                    if not apes: return None
                    position = (len(apes)-1)*q; lo = int(position); hi = min(lo+1, len(apes)-1)
                    return apes[lo] + (apes[hi]-apes[lo])*(position-lo)
                entry[kind] = dict(cases=len(selected), finite_ape_cases=len(apes),
                    zero_reference_cases=sum(r[rk] == 0 for r in selected),
                    zero_reference_nonzero_prediction_cases=sum(r[rk] == 0 and r[pk] != 0 for r in selected),
                    mape_percent=sum(apes)/len(apes) if apes else None,
                    p50_ape_percent=percentile(.5), p90_ape_percent=percentile(.9), p99_ape_percent=percentile(.99),
                    wape_percent=100*sum(abs(r[pk]-r[rk]) for r in selected)/reference if reference else None,
                    signed_bias_percent=100*sum(r[pk]-r[rk] for r in selected)/reference if reference else None)
            result[scope][field] = entry
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit_perf_retirement(args.trace_dir)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'valid': result['valid'], 'output': str(args.output)}))
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__': main()
