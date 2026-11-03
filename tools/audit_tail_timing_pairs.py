#!/usr/bin/env python3
"""Join bounded FastSim milestones with recollected gem5 committed stages.

Uses the original ROI origin throughout. When idle interval timestamps are
absent, gives rigorous cumulative active-cycle bounds, never subtracts all idle
time from every window or silently treats load completeTick as data return.
"""

import argparse
import bisect
import json
from pathlib import Path
import re
import struct

from audit_tail_timing_inputs import HEADER, RECORD
from audit_gem5_commit_gaps import (
    load_native_facts,
    native_hierarchy_outcome,
)
from collect_tail_timing import sha256, write_json


SEQ = re.compile(rb'"micro_seq":\s*(\d+)')
AS_HEADER = struct.Struct('<8sIIIIQQQ')
AS_ENTRY = struct.Struct('<QQ')


def address_spaces(path):
    with Path(str(path) + '.asmap').open('rb') as source:
        header = AS_HEADER.unpack(source.read(AS_HEADER.size))
        if header[1:4] != (1, AS_HEADER.size, AS_ENTRY.size):
            raise ValueError('unsupported ASMAP')
        entries = list(AS_ENTRY.iter_unpack(source.read()))
    return [e[0] for e in entries], [e[1] for e in entries]


def selected_records(path, targets):
    result = {}
    last = max(targets)
    with path.open('rb') as source:
        ordinal = 0
        for line in source:
            if b'{' not in line:
                continue
            if ordinal in targets:
                result[ordinal] = json.loads(line)
            ordinal += 1
            if ordinal > last:
                break
    if set(result) != set(targets):
        raise ValueError('selected functional record missing')
    return result


def selected_labels(path, targets):
    result = {}
    last = max(targets)
    with path.open('rb') as source:
        for line in source:
            match = SEQ.search(line)
            if match is None:
                raise ValueError('label without micro_seq')
            seq = int(match.group(1))
            if seq in targets:
                if seq in result:
                    raise ValueError('duplicate label identity')
                result[seq] = json.loads(line)
            if seq > last:
                break
    if set(result) != set(targets):
        raise ValueError('selected stage label missing')
    return result


def memory_event_scope(sample, record, configuration):
    """Classify a verified functional identity, never infer a missing response.

    Native FS MMIO may be deliberately excluded from FastSim's data hierarchy.
    Such records still have pipeline timing; they are not ordinary load hits.
    Call only after verifying the FST/JSON PC, flags, address, size and ASID.
    """
    expected = (bool(record['is_load']), bool(record['is_store']))
    observed = (bool(sample['has_load']), bool(sample['has_store']))
    if observed == expected:
        return 'data-hierarchy' if any(expected) else 'non-data'
    dram_size = configuration.get('dram', {}).get('size_bytes')
    if (configuration.get('allow_mmio_escape') and dram_size is not None
            and any(expected) and not any(observed)
            and record.get('size', 0) > 0
            and record.get('paddr', -1) >= dram_size
            and not any(not e['instruction_fetch']
                        for e in sample.get('memory_events', []))):
        return 'mmio-escape-no-data-event'
    raise ValueError('FastSim ordinal mapping mismatch')


def compact_native_fact(fact, clock_period_ticks=None,
                        issue_tick=None, commit_tick=None):
    if fact is None:
        return None
    hierarchy = fact.get('native_hierarchy', {})
    result = {
        'outcome': native_hierarchy_outcome(fact),
        'attribution_source': fact.get('attribution_source'),
        'proxy_path_class': int(fact.get('proxy_path_class', 0)),
        'line_requests': int(fact.get('line_requests', 0)),
        'admission_count': int(fact.get('native_admission_count', 0)),
        'aliased_admissions': int(fact.get('native_aliased_admissions', 0)),
        'hierarchy_request_count': int(
            fact.get('native_hierarchy_request_count', 0)),
        'response_count': int(fact.get('native_response_count', 0)),
        'coalesced': int(fact.get('native_coalesced', 0)),
        'terminal_no_ruby': bool(fact.get('native_terminal_no_ruby')),
        'l1d': hierarchy.get('l1d', {}),
        'l2': hierarchy.get('l2', {}),
        'llc': hierarchy.get('llc', {}),
        'unique_fills': int(hierarchy.get('unique_fills', 0)),
        'ruby_memory_fetches': int(
            hierarchy.get('ruby_memory_fetches', 0)),
        'memory_read_transactions': int(
            hierarchy.get('memory_read_transactions', 0)),
    }
    timing_fields = (
        'native_first_admission_tick',
        'native_last_admission_tick',
        'native_last_response_tick',
    )
    timing_present = [field in fact for field in timing_fields]
    if any(timing_present) and not all(timing_present):
        raise ValueError('native lifecycle has a partial timing ledger')
    result['response_timestamps_available'] = False
    if all(timing_present) and result['response_count'] > 0:
        ticks = [int(fact[field]) for field in timing_fields]
        if any(value < 0 for value in ticks) or not (
                ticks[0] <= ticks[1] <= ticks[2]):
            raise ValueError('native lifecycle timestamps are not ordered')
        result.update(dict(zip(timing_fields, ticks)))
        result['response_timestamps_available'] = True
        if clock_period_ticks is not None:
            if clock_period_ticks <= 0:
                raise ValueError('clock period must be positive')
            result['timing_cycles'] = {
                'first_to_last_admission':
                    (ticks[1] - ticks[0]) / clock_period_ticks,
                'first_admission_to_last_response':
                    (ticks[2] - ticks[0]) / clock_period_ticks,
            }
            if issue_tick is not None:
                result['timing_cycles']['issue_to_first_admission'] = (
                    ticks[0] - int(issue_tick)
                ) / clock_period_ticks
            if commit_tick is not None:
                result['timing_cycles']['last_response_to_commit'] = (
                    int(commit_tick) - ticks[2]
                ) / clock_period_ticks
    return result


def has_native_response_timestamps(results):
    return any(
        (pair.get('gem5_native') or {}).get(
            'response_timestamps_available', False)
        for core in results for pair in core['pairs'])


def experimental_model_features(configuration):
    candidates = (
        'response_pending_fill',
        'ruby_sequencer_line_coalescing',
        'ruby_sequencer_load_admission',
        'store_post_commit_request',
        'interval_causal_timing',
        'interval_response_retime',
    )
    return [name for name in candidates if configuration.get(name)]


def core_pairs(core, entry, audit, trace_dir, cpl, native_responses=None):
    warm = int(entry[6])
    take = int(entry[7])
    samples = {s['sequence']: s for s in audit['cores'][core]['response_frontier_audit']
               if warm <= s['sequence'] < warm + take}
    if not samples:
        raise ValueError('no measurement milestones for core %d' % core)
    raw_path = next(trace_dir.glob('*switch%0*d.*records.micro.jsonl' %
                                   (len(str(len(audit['cores']) - 1)), core)))
    label_path = Path(str(raw_path).replace('.records.', '.labels.'))
    records = selected_records(raw_path, samples)
    # Auxiliary syscall facts have no committed-stage label. Keep them out of
    # the join explicitly; subsequent records retain their stream ordinal.
    auxiliary = [seq for seq, row in records.items() if 'micro_seq' not in row]
    records = {seq: row for seq, row in records.items() if 'micro_seq' in row}
    labels = selected_labels(label_path, {row['micro_seq'] for row in records.values()})
    native_facts = {}
    if native_responses is not None:
        native_targets = {
            int(row['seq_num']) for row in records.values()
            if row.get('is_load') or row.get('is_store') or
            row.get('is_atomic')
        }
        if native_targets:
            native_facts = load_native_facts(
                native_responses, core, native_targets)
        missing_native = sorted(native_targets - set(native_facts))
        if missing_native:
            raise ValueError(
                '%d selected memory instructions lack terminal native facts '
                '(first %d)' % (len(missing_native), missing_native[0]))
    fst = Path(entry[2])
    as_ordinals, as_values = address_spaces(fst)
    origin = audit['totals']['functional_warmup_barrier_cycles']
    period = cpl['clock_period_ticks']
    idle_total = cpl['idle_ticks'] // period
    pairs = []
    with fst.open('rb') as source:
        h = HEADER.unpack(source.read(HEADER.size))
        if h[:4] != (b'FSTRC01\0', 7, HEADER.size, RECORD.size) or h[4] != core:
            raise ValueError('FST header identity mismatch')
        for seq, row in sorted(records.items()):
            source.seek(HEADER.size + seq * RECORD.size)
            value = RECORD.unpack(source.read(RECORD.size))
            is_memory = bool(value[9] & 14)
            expected_as = as_values[bisect.bisect_right(as_ordinals, seq) - 1]
            expected = (value[0], bool(value[9] & 2), bool(value[9] & 4),
                        bool(value[9] & 8), value[10] < -1, expected_as)
            actual = (row['macro_pc'], bool(row['is_load']), bool(row['is_store']),
                      bool(row['is_atomic']), row['cpl'] != 3, row['address_space_id'])
            if expected != actual or (is_memory and (value[1], value[8]) !=
                                      (row['paddr'], row['size'])):
                raise ValueError('FST/JSON identity mismatch core=%d ordinal=%d' % (core, seq))
            label = labels[row['micro_seq']]
            if row['core_id'] != core or \
                    (row['core_id'], row['thread_id']) != (label['core_id'], label['thread_id']):
                raise ValueError('label core/thread identity mismatch')
            fs = samples[seq]
            if fs['pc'] != row['macro_pc']:
                raise ValueError('FastSim/FST PC identity mismatch')
            memory_scope = memory_event_scope(fs, row, audit['configuration'])
            ticks = label['commit_tick'] - cpl['first_tick']
            if ticks < 0 or ticks % period:
                raise ValueError('commit outside ROI or clock mismatch')
            if label['issue_tick'] < 0 or label['issue_tick'] % period or \
                    not 0 < label['fetch_tick'] <= label['fetch_tick'] + label['issue_tick'] <= label['commit_tick']:
                raise ValueError('invalid fetch/issue/commit stages')
            elapsed = ticks // period
            fs_retire = fs['actual_retire_cycle'] - origin
            lower = max(0, elapsed - idle_total)
            # Global idle budget bounds the unknown cumulative idle prefix.
            # These are bounds, not inferred per-window idle timestamps.
            fetch_issue = label['issue_tick'] / period
            issue_commit = (label['commit_tick'] - label['fetch_tick'] - label['issue_tick']) / period
            native_fact = compact_native_fact(
                native_facts.get(int(row['seq_num'])), period,
                label['fetch_tick'] + label['issue_tick'],
                label['commit_tick'])
            pairs.append({
                'record_ordinal': seq, 'roi_record_offset': seq - warm,
                'micro_seq': row['micro_seq'], 'inst_seq_num': row['seq_num'],
                'core_id': core, 'thread_id': row['thread_id'],
                'address_space_id': row['address_space_id'],
                'pc': row['macro_pc'], 'micro_pc': row['micro_pc'], 'cpl': row['cpl'],
                'is_load': bool(row['is_load']), 'is_store': bool(row['is_store']),
                'is_atomic': bool(row['is_atomic']),
                'memory_event_scope': memory_scope,
                'fastsim_retire_from_roi': fs_retire,
                'gem5_elapsed_from_roi': elapsed,
                'gem5_active_from_roi_lower': lower,
                'gem5_active_from_roi_upper': elapsed,
                'fastsim_minus_gem5_active_lower': fs_retire - elapsed,
                'fastsim_minus_gem5_active_upper': fs_retire - lower,
                'gem5_fetch_to_issue': fetch_issue,
                'gem5_issue_to_commit': issue_commit,
                'fastsim_fetch_to_issue': fs['actual_issue_cycle'] - fs['actual_fetch_cycle'],
                'fastsim_issue_to_retire': fs['actual_retire_cycle'] - fs['actual_issue_cycle'],
                'producer_dists': fs['producer_dists'],
                'gem5_producer_dists': row['producer_dists'],
                'gem5_native': native_fact,
            })
    negative = [p for p in pairs if p['fastsim_minus_gem5_active_upper'] < 0]
    substantial = [p for p in pairs if p['fastsim_minus_gem5_active_upper'] <= -10000]
    worst = min(pairs, key=lambda p: p['fastsim_minus_gem5_active_upper'])
    return {
        'core': core, 'paired_milestones': len(pairs), 'auxiliary_milestones_skipped': auxiliary,
        'mmio_escape_milestones': sum(p['memory_event_scope'] ==
                                      'mmio-escape-no-data-event' for p in pairs),
        'fastsim_roi_origin': origin, 'gem5_roi_origin_tick': cpl['first_tick'],
        'clock_period_ticks': period, 'idle_cycle_budget': idle_total,
        'first_proven_negative_milestone': negative[0] if negative else None,
        'first_at_least_10000_cycle_deficit': substantial[0] if substantial else None,
        'worst_proven_negative_milestone': worst if negative else None,
        'pairs': pairs,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collection', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--core', type=int, action='append')
    p.add_argument('--audit-stats', type=Path)
    p.add_argument('--control-stats', type=Path,
                   help='uninstrumented FastSim stats (default: COLLECTION/fastsim-control.json)')
    p.add_argument('--manifest', type=Path,
                   help='exact audit manifest (default: COLLECTION/manifest.txt)')
    p.add_argument('--native-response-semantics', action='store_true',
                   help='join oracle-only native Ruby outcomes and v7 lifecycle timing when available')
    p.add_argument('--allow-experimental-model', action='store_true',
                   help='allow a non-baseline FastSim mechanism after audit/control equality is verified')
    args = p.parse_args()
    d = args.collection
    if json.loads((d / 'completion.json').read_text())['returncode'] != 0:
        raise ValueError('collection did not complete')
    provenance = json.loads((d / 'collection.json').read_text())
    original = Path(provenance['original'])
    trace_dir = d / 'trace'
    cpls = {r['core_id']: r for r in map(json.loads,
            (trace_dir / 'oracle/cpl_class.jsonl').read_text().splitlines())}
    original_cpls = {r['core_id']: r for r in map(json.loads,
            (original / 'oracle/cpl_class.jsonl').read_text().splitlines())}
    audit_path = args.audit_stats or d / 'fastsim-audit.json'
    audit = json.loads(audit_path.read_text())
    control_path = args.control_stats or d / 'fastsim-control.json'
    control = json.loads(control_path.read_text())
    for key in ['scope_metrics', 'threads']:
        a, b = audit[key], control[key]
        if key == 'scope_metrics':
            a = {k: v for k, v in a.items() if k != 'throughput'}
            b = {k: v for k, v in b.items() if k != 'throughput'}
        if a != b:
            raise ValueError('instrumentation changed target ' + key)
    experimental_features = experimental_model_features(
        audit['configuration'])
    if audit['configuration']['interval_max_cycles'] != 1024 or \
            (experimental_features and not args.allow_experimental_model):
        raise ValueError('Q/experimental-model research boundary violated')
    manifest_path = args.manifest or d / 'manifest.txt'
    entries = [e.split() for e in manifest_path.read_text().splitlines()
               if e.strip() and not e.lstrip().startswith('#')]
    full = provenance['diagnostic_user_uops'] == provenance['original_user_uops']
    results = []
    for e in entries:
        core = int(e[0])
        boundary = json.loads((trace_dir / ('functional-boundary-core%d.json' % core)).read_text())
        for k, v in [('warmup_records', int(e[6])), ('measurement_records', int(e[7]))]:
            if boundary[k] != v:
                raise ValueError('recollection/manifest boundary mismatch')
        if cpls[core]['first_tick'] != original_cpls[core]['first_tick']:
            raise ValueError('recollection ROI origin differs')
        if full and cpls[core] != original_cpls[core]:
            raise ValueError('full recollection CPL metrics differ')
        if args.core is None or core in args.core:
            native_path = None
            if args.native_response_semantics:
                native_path = trace_dir / (
                    'oracle/native-response-core%d.jsonl' % core)
                if not native_path.is_file():
                    raise ValueError(
                        'native-response sideband missing for core %d' % core)
            results.append(core_pairs(
                core, e, audit, trace_dir, cpls[core], native_path))
    native_hashes = {}
    if args.native_response_semantics:
        for result_core in results:
            core = result_core['core']
            native_path = trace_dir / (
                'oracle/native-response-core%d.jsonl' % core)
            native_hashes[str(core)] = sha256(native_path)
    result = {
        'schema': 'fastsim-tail-timing-pairs-v1', 'case': provenance['case'],
        'full_roi': full, 'instrumentation_target_metrics_equal': True,
        'experimental_model': bool(experimental_features),
        'experimental_model_features': experimental_features,
        'fastsim_audit_sha256': sha256(audit_path),
        'fastsim_control_sha256': sha256(control_path),
        'manifest_sha256': sha256(manifest_path),
        'native_response_semantics_joined':
            args.native_response_semantics,
        'native_response_sha256_by_core': native_hashes,
        'gem5_sha256': provenance['gem5_sha256'], 'cores': results,
        'coverage': 'selected milestones; does not prove whole-stream identity',
        'causal_root_established': False,
        'memory_parallelism': None,
        'native_response_timestamps_available':
            has_native_response_timestamps(results),
        'limitations': ['native v6 inputs have hierarchy semantics but no admission/response ticks',
                        'native v7 timing remains oracle-only and is not an FST feature',
                        'idle interval timestamps unavailable: cumulative bounds only',
                        'load completeTick is address generation, not data response',
                        'milestone stage residence is not retirement critical-path attribution'],
    }
    write_json(args.output, result)
    for c in results:
        print(c['core'], c['paired_milestones'], 'worst', c['worst_proven_negative_milestone'])


if __name__ == '__main__':
    main()
