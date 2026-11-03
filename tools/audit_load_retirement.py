#!/usr/bin/env python3
"""Partition paired retirement spans without treating request residence as CPI.

Native acceptance/callback timestamps are oracle-only. FastSim corrected issue
is not a measured Sequencer acceptance. Missing fragment/parent/ready evidence
stays unavailable; load completeTick is never interpreted as data readiness.
"""

import argparse
from collections import Counter
import json
from pathlib import Path

from audit_buffer_tso_lifecycle import (
    assert_audit_neutral, audit_case, validate_stats_binding,
)
from collect_tail_timing import write_json


CATEGORIES = (
    'productive', 'not_fetched', 'fetched_not_issued',
    'load_before_acceptance', 'load_acceptance_to_callback',
    'load_after_callback', 'load_unresolved', 'issued_store',
    'issued_other', 'unknown_auxiliary',
)


def integral(value):
    if isinstance(value, bool) or int(value) != value:
        raise ValueError('nonintegral cycle')
    return int(value)


def load_edges(pair, sample, period, origin):
    if period <= 0:
        raise ValueError('nonpositive clock period')
    if not pair['is_load'] or pair['is_store'] or pair['is_atomic']:
        return None, 'not-ordinary-load'
    if pair['memory_event_scope'] != 'data-hierarchy':
        return None, 'outside-data-hierarchy'
    native = pair.get('gem5_native')
    if not native or not native.get('response_timestamps_available'):
        return None, 'native-timestamps-unavailable'
    if native.get('terminal_no_ruby') or native.get('attribution_source') != 'packet':
        return None, 'native-not-packet'
    if any(native[key] != 1 for key in
           ('line_requests', 'admission_count', 'response_count', 'hierarchy_request_count')):
        return None, 'native-fragment-identity-unavailable'
    if native['aliased_admissions'] or native['coalesced']:
        return None, 'native-parent-identity-unavailable'
    events = [event for event in sample['memory_events']
              if not event['instruction_fetch']]
    if len(events) != 1 or events[0]['write'] or not events[0]['blocks_retirement']:
        return None, 'fastsim-not-single-blocking-read'
    first = native['native_first_admission_tick'] - origin
    last = native['native_last_admission_tick'] - origin
    response = native['native_last_response_tick'] - origin
    if first != last or response < first:
        raise ValueError('invalid native single-request lifecycle')
    if first % period or response % period:
        return None, 'native-subcycle-boundary'
    event = events[0]
    offset = sample['actual_retire_cycle'] - pair['fastsim_retire_from_roi']
    edges = {
        'gem5': (first // period, response // period),
        'fastsim': (integral(event['corrected_issue_cycle']) - offset,
                    integral(event['response_cycle']) - offset),
    }
    for name, (acceptance, callback) in edges.items():
        fetch, issue, retire = stages(pair, name)
        if not issue <= acceptance <= callback <= retire:
            return None, name + '-lifecycle-outside-issued-retire'
    return edges, None


def stages(pair, name):
    if name == 'gem5':
        retire = integral(pair['gem5_elapsed_from_roi'])
        issue = retire - integral(pair['gem5_issue_to_commit'])
        fetch = issue - integral(pair['gem5_fetch_to_issue'])
    else:
        retire = integral(pair['fastsim_retire_from_roi'])
        issue = retire - integral(pair['fastsim_issue_to_retire'])
        fetch = issue - integral(pair['fastsim_fetch_to_issue'])
    if not fetch <= issue <= retire:
        raise ValueError('nonmonotonic stages')
    return fetch, issue, retire


def partition(previous, pair, name, edges, unknown):
    fetch, issue, retire = stages(pair, name)
    if retire < previous:
        raise ValueError('out-of-order retirement')
    result = Counter()
    if retire == previous:
        return result
    if unknown:
        result['unknown_auxiliary'] = retire - previous
        return result
    result['productive'] = 1
    begin, end = previous + 1, retire

    def add(category, lower, upper):
        result[category] += max(0, min(end, upper) - max(begin, lower))

    add('not_fetched', begin, fetch)
    add('fetched_not_issued', fetch, issue)
    if pair['is_load'] and not pair['is_store'] and not pair['is_atomic']:
        if edges is None:
            add('load_unresolved', issue, retire)
        else:
            acceptance, callback = edges[name]
            add('load_before_acceptance', issue, acceptance)
            add('load_acceptance_to_callback', acceptance, callback)
            add('load_after_callback', callback, retire)
    else:
        add('issued_store' if pair['is_store'] else 'issued_other', issue, retire)
    if sum(result.values()) != retire - previous:
        raise ValueError('retirement gap does not conserve')
    return result


def analyze_core(joined, samples):
    pairs = joined['pairs']
    if not pairs:
        raise ValueError('empty paired window')
    by_sequence = {sample['sequence']: sample for sample in samples}
    if len(by_sequence) != len(samples):
        raise ValueError('duplicate FastSim samples')
    ordinals = [pair['record_ordinal'] for pair in pairs]
    if ordinals != sorted(set(ordinals)):
        raise ValueError('duplicate or unordered pairs')
    auxiliary = set(joined['auxiliary_milestones_skipped'])
    missing = set(range(ordinals[0], ordinals[-1] + 1)) - set(ordinals)
    if missing != {value for value in auxiliary if ordinals[0] < value < ordinals[-1]}:
        raise ValueError('unexplained missing hardware record')
    ledgers = {name: Counter() for name in ('gem5', 'fastsim')}
    previous = {name: stages(pairs[0], name)[2] for name in ledgers}
    first = dict(previous)
    exclusions = Counter()
    loads = []
    gaps = []
    for index, pair in enumerate(pairs):
        sample = by_sequence[pair['record_ordinal']]
        if sample['pc'] != pair['pc']:
            raise ValueError('sample PC differs from paired identity')
        edges, reason = load_edges(pair, sample, joined['clock_period_ticks'],
                                   joined['gem5_roi_origin_tick'])
        if reason:
            exclusions[reason] += 1
        else:
            timings = {}
            for name in ledgers:
                fetch, issue, retire = stages(pair, name)
                acceptance, callback = edges[name]
                timings[name] = {
                    'issue_to_request_start': acceptance - issue,
                    'request_start_to_callback': callback - acceptance,
                    'callback_to_retire': retire - callback,
                    'issue_to_retire': retire - issue,
                }
            timings['difference'] = {
                key: timings['fastsim'][key] - timings['gem5'][key]
                for key in timings['gem5']}
            timings.update(record_ordinal=pair['record_ordinal'], pc=pair['pc'],
                           line=next(event['line'] for event in sample['memory_events']
                                     if not event['instruction_fetch']),
                           native=pair['gem5_native'])
            timings['fastsim_callback_to_completion'] = (
                sample['actual_completion_cycle'] - sample['memory_response_cycle'])
            loads.append(timings)
        if index == 0:
            continue
        unknown = pair['record_ordinal'] != ordinals[index - 1] + 1
        gap = {}
        for name in ledgers:
            gap[name] = partition(previous[name], pair, name, edges, unknown)
            ledgers[name].update(gap[name])
            previous[name] = stages(pair, name)[2]
        difference = {key: gap['fastsim'][key] - gap['gem5'][key] for key in CATEGORIES}
        gaps.append(dict(record_ordinal=pair['record_ordinal'], pc=pair['pc'],
                         difference=difference, span_difference=sum(difference.values())))
    results = {}
    for name, counts in ledgers.items():
        span = previous[name] - first[name]
        if sum(counts.values()) != span:
            raise ValueError('retirement span does not conserve')
        results[name] = dict(span_cycles=span, categories={key: counts[key] for key in CATEGORIES})
    difference = {key: ledgers['fastsim'][key] - ledgers['gem5'][key] for key in CATEGORIES}
    return dict(
        core=joined['core'], records=len(pairs), first_ordinal=ordinals[0], last_ordinal=ordinals[-1],
        ledgers=results, difference=difference, span_difference=sum(difference.values()),
        native_whole_diagnostic_idle_budget=joined['idle_cycle_budget'],
        span_difference_active_bounds=[
            sum(difference.values()),
            sum(difference.values()) + min(joined['idle_cycle_budget'], results['gem5']['span_cycles'])],
        conserved=True, auxiliary_records=sorted(auxiliary), exclusions=dict(exclusions),
        single_unaliased_loads=len(loads),
        first_load_stage_difference=next((row for row in loads
                                         if any(row['difference'].values())), None),
        largest_negative_gaps=sorted(gaps, key=lambda row: row['span_difference'])[:10],
        load_stage_difference_histograms={
            key: dict(sorted(Counter(row['difference'][key] for row in loads).items()))
            for key in ('issue_to_request_start', 'request_start_to_callback', 'callback_to_retire')},
        native_data_ready_available=False, native_parent_identity_available=False,
        full_roi_attribution=False, actual_cpi_contribution=None,
    )


def analyze_readiness(samples, configuration):
    if not samples:
        raise ValueError('empty FastSim window')
    ordinals = [sample['sequence'] for sample in samples]
    if ordinals != list(range(ordinals[0], ordinals[-1] + 1)):
        raise ValueError('FastSim readiness requires a dense ordered window')
    observed = {}
    counts = Counter()
    witnesses = []
    for sample in samples:
        issue = integral(sample['actual_issue_cycle'])
        done = integral(sample['actual_completion_cycle'])
        retire = integral(sample['actual_retire_cycle'])
        if not issue <= done <= retire:
            raise ValueError('invalid FastSim pipeline stages')
        responses = [event['response_cycle'] for event in sample['memory_events']
                     if not event['instruction_fetch'] and not event['write']
                     and event['blocks_retirement']]
        if sample['has_load'] and not sample['has_store'] and responses:
            counts['loads_with_blocking_response'] += 1
            gap = done - max(responses)
            counts['completion_before_response'] += gap < 0
            counts['completion_before_configured_ready_floor'] += (
                gap < configuration['load_response_to_ready'])
            if gap < configuration['load_response_to_ready']:
                witnesses.append(dict(sequence=sample['sequence'], kind='load-ready-floor',
                                      response=max(responses), completion=done, retire=retire))
        distances = sample['producer_dists'][:4] + sample.get('producer_dist_extensions', [])
        for distance in distances:
            if distance < 0:
                raise ValueError('negative register dependency distance')
            if not distance:
                continue
            producer = sample['sequence'] - distance
            if producer not in observed:
                counts['incoming_register_edges_outside_window'] += 1
                continue
            counts['checked_register_edges'] += 1
            if issue < observed[producer]:
                counts['consumer_issue_before_producer_completion'] += 1
                witnesses.append(dict(sequence=sample['sequence'], kind='register-ready',
                                      producer=producer, issue=issue, completion=observed[producer]))
        observed[sample['sequence']] = done
    return dict(
        records=len(samples), first_ordinal=ordinals[0], last_ordinal=ordinals[-1],
        counts={key: counts[key] for key in (
            'loads_with_blocking_response', 'completion_before_response',
            'completion_before_configured_ready_floor', 'checked_register_edges',
            'consumer_issue_before_producer_completion', 'incoming_register_edges_outside_window')},
        witnesses=witnesses[:20], native_comparison=False, full_roi_attribution=False,
        limitation='Internal recorded-edge consistency only; does not prove source dependency completeness or native timing.',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('audit-stats', 'control-stats',
                 'audit-validation', 'control-validation', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--collection', type=Path)
    parser.add_argument('--native-result', type=Path)
    parser.add_argument('--fastsim-only', action='store_true')
    parser.add_argument('--core', type=int, action='append', required=True)
    args = parser.parse_args()
    if not args.fastsim_only and (args.collection is None or args.native_result is None):
        parser.error('paired mode requires --collection and --native-result')
    try:
        stats = json.loads(args.audit_stats.read_text())
        if args.fastsim_only:
            control = json.loads(args.control_stats.read_text())
            assert_audit_neutral(stats, control)
            source = Path(json.loads(args.control_validation.read_text())['result_dir'])
            bindings = [validate_stats_binding(path, source, validation)
                        for path, validation in (
                            (args.audit_stats, args.audit_validation),
                            (args.control_stats, args.control_validation))]
            results = [dict(core=core, **analyze_readiness(
                stats['cores'][core]['response_frontier_audit'], stats['configuration']))
                for core in args.core]
            write_json(args.output, dict(schema='fastsim-load-retirement-audit-v1',
                                        status='fastsim-internal-only', bindings=bindings,
                                        cores=results))
            print(json.dumps(results))
            return
        gate = audit_case(args, include_pairs=True)
        results = [analyze_core(core['paired_stages'],
                               stats['cores'][core['core']]['response_frontier_audit'])
                   for core in gate['cores']]
        for core in gate['cores']:
            del core['paired_stages']
            del core['witnesses']
        output = dict(
            schema='fastsim-load-retirement-audit-v1', status='paired-window-diagnostic',
            gates=gate, cores=results, limitations=[
                'Partitions are endpoint spans, not whole-ROI CPI or additive causal contributions.',
                'FastSim corrected issue is a request start, not certified Sequencer acceptance.',
                'Native callback-to-retire includes unobserved ready/writeback/dependency/retirement edges.',
                'Aliased or split native requests have no exact parent/fragment join and remain unresolved.',
                'Idle is not timestamped; gem5 span may include idle, with the whole diagnostic idle budget in gates.',
                'A different gem5 binary remains diagnostic even after functional prefix matching.',
            ])
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        write_json(args.output, dict(schema='fastsim-load-retirement-audit-v1',
                                    status='rejected', reason=str(error)))
        raise SystemExit(str(error))
    write_json(args.output, output)
    for result in results:
        print(result['core'], result['records'], result['span_difference'], result['difference'])


if __name__ == '__main__':
    main()
