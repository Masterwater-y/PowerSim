#!/usr/bin/env python3
"""Compare disjoint committed-head cycle partitions on identity-paired windows."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_fst_commit_gaps import HeadGaps


def compare(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    if not data['instrumentation_target_metrics_equal']:
        raise ValueError('instrumentation changed target metrics')
    if data.get('experimental_model'):
        raise ValueError('experimental model cannot enter the baseline audit')
    results = []
    for core in data['cores']:
        pairs = core['pairs']
        ordinals = [p['record_ordinal'] for p in pairs]
        if len(set(ordinals)) != len(ordinals) or ordinals != sorted(ordinals):
            raise ValueError('duplicate or unordered record identity')
        missing = set(range(ordinals[0], ordinals[-1] + 1)) - set(ordinals)
        auxiliary = set(core.get('auxiliary_milestones_skipped', []))
        if missing != auxiliary:
            raise ValueError('not a dense window, or missing hardware records')
        # Dropped syscall facts would change the next architectural head in a
        # gap. Reject here instead of guessing a stage for an auxiliary record.
        if auxiliary:
            raise ValueError('paired window contains unstaged syscall facts')
        ledgers = {name: HeadGaps() for name in ('gem5', 'fastsim')}
        for p in pairs:
            kind = 'load' if p['is_load'] else 'store' if p['is_store'] else 'other'
            for name, ledger in ledgers.items():
                if name == 'gem5':
                    retire = p['gem5_elapsed_from_roi']
                    issue = retire - p['gem5_issue_to_commit']
                    fetch = issue - p['gem5_fetch_to_issue']
                else:
                    retire = p['fastsim_retire_from_roi']
                    issue = retire - p['fastsim_issue_to_retire']
                    fetch = issue - p['fastsim_fetch_to_issue']
                if any(t != int(t) for t in (fetch, issue, retire)):
                    raise ValueError('nonintegral local cycle')
                ledger.add(int(fetch), int(issue), int(retire), kind,
                           {k: p[k] for k in ('record_ordinal', 'micro_seq', 'pc')})
        g, f = (ledgers[name].result() for name in ('gem5', 'fastsim'))
        deltas = {k: f['categories'][k] - g['categories'][k]
                  for k in g['categories']}
        deltas['productive_cycles'] = f['productive_cycles'] - g['productive_cycles']
        span_delta = f['span_cycles'] - g['span_cycles']
        if sum(deltas.values()) != span_delta:
            raise ValueError('paired span delta does not conserve')
        results.append(dict(core=core['core'], first_ordinal=ordinals[0],
                            last_ordinal=ordinals[-1], gem5=g, fastsim=f,
                            fastsim_minus_gem5_cycles=deltas,
                            span_delta_cycles=span_delta,
                            gem5_whole_roi_idle_budget=core['idle_cycle_budget'],
                            interpretation='Endpoint span, not standalone CPI. Cycle differences are an accounting partition, not an additive causal intervention.'))
    return dict(source=str(path), source_sha256=hashlib.sha256(raw).hexdigest(),
                case=data['case'], gem5_sha256=data['gem5_sha256'],
                full_roi_collection=data['full_roi'], cores=results)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pairs', type=Path, action='append', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = dict(schema='fastsim-paired-rob-gap-comparison-v1',
                  windows=[compare(path) for path in args.pairs])
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for window in result['windows']:
        for core in window['cores']:
            print(window['case'], core['core'], core['span_delta_cycles'],
                  core['fastsim_minus_gem5_cycles'])


if __name__ == '__main__':
    main()
