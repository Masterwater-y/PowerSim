#!/usr/bin/env python3
"""Bounded, fixed-service load-response witness; NOT a CPI prediction.

Raise selected load completion floors to their already recorded responses,
propagate four functional register edges and ordered width-limited retirement.
Keep observed issue/retire floors and issue-to-completion durations. Do not
replay FU/IQ/LSQ/StoreSet, frontend, cache, admission, DRAM or checkpoint state.
An exact zero-change replay is mandatory. Missing incoming edges stay frozen;
outgoing consumers beyond the window are not included. No gem5 timing input.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def response_floors(samples):
    return {s['sequence']: max(
        [s['actual_completion_cycle']] +
        [e['response_cycle'] for e in s['memory_events']
         if not e['instruction_fetch'] and not e['write'] and e['blocks_retirement']])
        for s in samples}


def replay(samples, floors, commit_width, execute_to_commit):
    if not samples or commit_width < 1 or execute_to_commit < 0:
        raise ValueError('invalid window/retirement configuration')
    completion = {}
    slots = Counter()
    previous_retire = 0
    results = []
    previous_sequence = samples[0]['sequence'] - 1
    for s in samples:
        seq = s['sequence']
        if seq != previous_sequence + 1:
            raise ValueError('requires contiguous, unique, ordered source sequences')
        previous_sequence = seq
        old_issue, old_done, old_retire = (
            s['actual_issue_cycle'], s['actual_completion_cycle'], s['actual_retire_cycle'])
        if not 0 <= old_issue <= old_done <= old_retire:
            raise ValueError('non-monotone observed pipeline stages')
        issue = old_issue
        for dist in s['producer_dists'][:4]:
            if dist < 0:
                raise ValueError('negative producer distance')
            if dist and seq - dist in completion:
                issue = max(issue, completion[seq - dist])
        done = max(old_done + issue - old_issue, floors.get(seq, old_done))
        retire = max(old_retire, previous_retire, done + execute_to_commit)
        while slots[retire] >= commit_width:
            retire += 1
        slots[retire] += 1
        completion[seq] = done
        previous_retire = retire
        results.append((issue, done, retire))
    return results


def analyze(samples, commit_width, execute_to_commit):
    original = [(s['actual_issue_cycle'], s['actual_completion_cycle'],
                 s['actual_retire_cycle']) for s in samples]
    if replay(samples, {}, commit_width, execute_to_commit) != original:
        raise ValueError('zero-change replay failed; cannot interpret counterfactual')
    floors = response_floors(samples)
    moved = {s['sequence']: floors[s['sequence']] - s['actual_completion_cycle']
             for s in samples if floors[s['sequence']] > s['actual_completion_cycle']}
    corrected = replay(samples, floors, commit_width, execute_to_commit)
    displacements = [n[2] - o[2] for n, o in zip(corrected, original)]
    sequences = {s['sequence'] for s in samples}
    return {
        'schema': 'fastsim-fixed-response-retire-witness-v1',
        'zero_change_identity': True,
        'samples': len(samples), 'begin': samples[0]['sequence'], 'end': samples[-1]['sequence'],
        'load_uops_completed_before_response': len(moved),
        'local_completion_gap_sum_not_retire_cycles': sum(moved.values()),
        'local_completion_gap_max': max(moved.values(), default=0),
        'conditional_endpoint_retire_displacement': displacements[-1],
        'conditional_max_retire_displacement': max(displacements),
        'first_conditional_retire_displacement': next(
            ({'sequence': s['sequence'], 'cycles': d}
             for s, d in zip(samples, displacements) if d), None),
        'incoming_register_edges_outside_window': sum(
            bool(d) and s['sequence'] - d not in sequences
            for s in samples for d in s['producer_dists'][:4]),
        'actual_model_cpi_effect': None,
        'limitations': [
            'fixed observed service and issue/retire floors: only adds necessary load-response bounds',
            'no FU/IQ/LSQ/StoreSet/frontend/cache/admission/DRAM/checkpoint replay',
            'incoming state frozen; outgoing consumers beyond this window excluded',
            'not a bound on a full-model repair and not a sum of per-request CPI benefits',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--core', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    stats = json.loads(args.audit.read_text())
    result = analyze(stats['cores'][args.core]['response_frontier_audit'],
                     stats['configuration']['commit_width'],
                     stats['configuration']['execute_to_commit'])
    result.update({'input': str(args.audit.resolve()), 'core': args.core,
                   'input_sha256': hashlib.sha256(args.audit.read_bytes()).hexdigest()})
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
