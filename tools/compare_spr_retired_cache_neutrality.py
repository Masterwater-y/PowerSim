#!/usr/bin/env python3
"""Strict same-input native FS observer neutrality check; never scores CPI."""
import argparse
import json
from pathlib import Path

from retired_cache_contract import audit_retired_cache, SPR_MISS_POLICY


def counters(source):
    result = {}
    for line in (source / 'stats.txt').read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and (fields[0] in ('simTicks', 'simInsts') or
                fields[0].endswith(('.numCycles', '.commitStats0.numInsts'))):
            result[fields[0]] = fields[1]
    if not result:
        raise ValueError('native execution counters missing')
    return result


def compare(before, after):
    references = []
    targets = []
    starts = []
    for source in (before, after):
        preparation = json.loads((source / 'spr-capture-preparation.json').read_text())
        if preparation.get('target_identity_valid') is not True:
            raise ValueError('separate native identity gate missing')
        target = json.loads((source / 'effective-target.json').read_text())
        targets.append({k:v for k,v in target.items() if k != 'source'})
        references.append(audit_retired_cache(source / 'tao_trace'))
        request=json.loads((source / 'sync-run-request.json').read_text())
        starts.append(request.get('staged_checkpoint', {}))
    old, new = references
    if new.get('membership_policy') != SPR_MISS_POLICY:
        raise ValueError('retired membership observer not activated')
    same_input = old['input_identity'] == new['input_identity']
    same_target = targets[0] == targets[1]
    same_start = (all(s.get('restored') and s.get('receipt_sha256') and
                      s.get('identity') for s in starts) and starts[0] == starts[1])
    same_cores = [c['core_id'] for c in old['per_core']] == [c['core_id'] for c in new['per_core']]
    same_population = all(
        a['scopes'][scope]['retired_macro_instructions'] ==
        b['scopes'][scope]['retired_macro_instructions'] and
        a['scopes'][scope]['retired_uops'] == b['scopes'][scope]['retired_uops'] and
        all(a['scopes'][scope]['events'][name] == b['scopes'][scope]['events'][name]
            for name in ('retired_loads','retired_stores'))
        for a,b in [(old,new),*zip(old['per_core'],new['per_core'])]
        for scope in a['scopes']) and same_cores
    legacy = []
    windows = []
    native_summaries = []
    for core in old['per_core']:
        cid = core['core_id']
        rows = [json.loads((source / 'tao_trace' / 'oracle' /
                          f'perf-cache-core{cid}.json').read_text())
                for source in (before, after)]
        a, b = rows
        keys = ('source_record_count', 'measurement_begin_ordinal',
                'measurement_end_ordinal_exclusive', 'common_start_tick',
                'common_end_tick', 'common_cut_id', 'task_ids')
        windows.append(dict(core_id=cid, equal=all(a[k] == b[k] for k in keys)))
        for scope in a['scopes']:
            keys = ('retirement_events', 'retirement_unknown',
                    'request_events', 'request_primitives')
            legacy.append(dict(core_id=cid, scope=scope,
                equal=all(a['scopes'][scope][k] == b['scopes'][scope][k] for k in keys)))
        summaries=[json.loads((source / 'tao_trace' / 'oracle' /
                              f'native-summary-core{cid}.json').read_text())
                   for source in (before,after)]
        native_summaries.append(dict(core_id=cid,equal=summaries[0]==summaries[1]))
    a, b = counters(before), counters(after)
    result = dict(schema='spr-retired-cache-native-neutrality-v1',
        scope='paired short native FS capture; whole-execution counter equality, no CPI calibration',
        same_functional_input=same_input, same_target=same_target,
        same_staged_start=same_start, same_participants=same_cores,
        same_retired_population=same_population, same_execution_counters=a == b,
        windows=windows, legacy_observations=legacy, before_counters=a, after_counters=b,
        native_summaries=native_summaries,
        source_evidence_complete=not old.get('evidence_gaps') and not new.get('evidence_gaps'),
        active_membership_policy=new['membership_policy'],
        hardware_equivalence='unverified')
    result['passed'] = same_start and same_input and same_target and same_population and a == b and all(
        row['equal'] for row in windows + legacy + native_summaries) and result['source_evidence_complete']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True, type=Path)
    parser.add_argument('--after', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = compare(args.before, args.after)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
