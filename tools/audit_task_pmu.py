#!/usr/bin/env python3
"""Audit native task PMU against current functional ownership, never proxy cache truth."""
from collections import defaultdict
import argparse
import json
import mmap
from pathlib import Path
import struct

from audit_p1_native_response_sideband import (
    add_native_population, empty_native_population, validate_summary_population,
)
from thread_state_graph import (ACCOUNTING_CONTRACT, COMMON_CUT_ID,
                                has_certified_functional_ownership, load_capture_evidence)
from time_accounting import assemble_gem5_time_accounting

FIELDS = ('retired_instructions', 'retired_uops', 'memory_uops', 'branches', 'branch_misses')


def flatten(population):
    hierarchy = population['native']['hierarchy']
    return {**population['commit'], 'l1d_tag_misses': hierarchy['l1d']['tag_misses'],
            'private_l2_tag_misses': hierarchy['l2']['tag_misses'],
            'llc_tag_misses': hierarchy['llc']['tag_misses']}


def empty_groups():
    return {name: {'commit': dict.fromkeys(FIELDS, 0), 'native': empty_native_population()}
            for name in ('task', 'other')}


def count(value, where):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f'{where}: expected a nonnegative integer')
    return value


def functional_populations(trace, meta, rows):
    """Independent raw-source scan; the switch record belongs to previous owner."""
    stream = sorted((r for r in rows if r['core_id'] == meta['core_id']),
                    key=lambda r: (r['record_ordinal'], r['boundary'] != 'before_record'))
    owner = stream[0]['os_tid']
    switches = {r['record_ordinal']: r for r in stream if r['event_kind'] == 'sched_switch'}
    populations = defaultdict(lambda: dict.fromkeys(FIELDS[:-1], 0))
    source = trace / meta['source_file']
    with source.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        raw_syscalls = bool(struct.unpack_from("<Q", data, 32)[0] & (1 << 8))
        for ordinal in range(meta['source_record_count']):
            flags, op = struct.unpack_from('<Hh', data, 72 + 64 * ordinal + 50)
            if raw_syscalls and op == -1:
                raise ValueError('raw syscall trace contains collapsed marker')
            if ordinal >= meta['warmup_records'] and flags & 1:
                target = populations[owner]
                target['retired_uops'] += 1 + (24 if op == -1 else 0)
                target['retired_instructions'] += not flags & 1024 or bool(flags & 2048)
                target['memory_uops'] += bool(flags & 14)
                # Canonical syscall marker replaces x86 syscall's 25 O3 UOPs,
                # including its one wrip control UOP, with a non-branch record.
                target['branches'] += bool(flags & 16) + (1 if op == -1 else 0)
            if ordinal in switches:
                switch = switches[ordinal]
                if switch['prev_pid'] != owner:
                    raise ValueError('functional owner transition population mismatch')
                owner = switch['next_pid']
    return dict(populations)


def audit_task_pmu(trace_dir):
    trace = Path(trace_dir)
    evidence = load_capture_evidence(trace)
    metadata, rows, graph = (evidence[k] for k in ('metadata', 'rows', 'graph'))
    # The PMU retains the assembly switch-entry owner population. Independent
    # BaseCPU active-clock availability belongs to a separate lifecycle domain
    # and cannot certify, or invalidate, these functional owner facts.
    if not has_certified_functional_ownership(graph):
        raise ValueError('task PMU requires certified complete functional ownership')
    tasks = sorted(t['os_tid'] for t in graph['tasks'])
    expected_files = {f'task-pmu-core{i}.json' for i in range(len(metadata))}
    paths = sorted((trace / 'oracle').glob('task-pmu-core*.json'))
    if {p.name for p in paths} != expected_files:
        raise ValueError('missing or extra task PMU source participants')
    clocks = [json.loads((trace / f'oracle/time-accounting-core{i}.json').read_text())
              for i in range(len(metadata))]
    clock = assemble_gem5_time_accounting(clocks, cores=len(metadata))
    if clock['elapsed_status'] != 'available':
        raise ValueError('task PMU requires conserved common clock')
    combined = empty_groups()
    per_core = []
    for meta in metadata:
        path = trace / f"oracle/task-pmu-core{meta['core_id']}.json"
        row = json.loads(path.read_text())
        required = dict(schema='fastsim-task-pmu-v1', oracle_only=True, fst_input=False,
                        scope='workload-task', population_contract=ACCOUNTING_CONTRACT,
                        hierarchy_source='ruby-slicc-controller-actions',
                        branch_miss_source='taotrace-retired-bpred-v1', common_cut_id=COMMON_CUT_ID,
                        core_id=meta['core_id'], source_record_count=meta['source_record_count'],
                        measurement_begin_ordinal=meta['warmup_records'],
                        measurement_end_ordinal_exclusive=meta['source_record_count'],
                        common_start_tick=clock['common_start_tick'],
                        common_end_tick=clock['common_end_tick'], task_ids=tasks)
        if any(row.get(k) != v for k, v in required.items()):
            raise ValueError(f'{path}: task source schema/contract/population/bounds mismatch')
        coverage = row.get('coverage', {})
        if any(coverage.get(k) is not True for k in
               ('initial_owner', 'common_cut_complete', 'kernel_stream_complete', 'target_drain_complete')):
            raise ValueError(f'{path}: incomplete task native coverage/drain')
        if any(count(coverage.get(k), k) != 0 for k in
               ('read_failures', 'identity_failures', 'unretained_hook_events', 'owner_unknown', 'pending_native')):
            raise ValueError(f'{path}: failed task native coverage')
        actual = functional_populations(trace, meta, rows)
        owners = row.get('owners')
        if not isinstance(owners, list):
            raise ValueError(f'{path}: missing owner populations')
        seen = set()
        local = empty_groups()
        for owner in owners:
            pid = count(owner.get('os_tid'), 'owner ID')
            if pid in seen:
                raise ValueError(f'{path}: duplicate owner population')
            seen.add(pid)
            commit = {k: count(owner.get(k), k) for k in FIELDS}
            expected = actual.get(pid, dict.fromkeys(FIELDS[:-1], 0))
            if any(commit[k] != expected[k] for k in expected):
                raise ValueError(f'{path}: owner {pid} functional population mismatch: {commit} vs {expected}')
            if commit['branch_misses'] > commit['branches']:
                raise ValueError(f'{path}: branch misses exceed branch population')
            native = validate_summary_population(owner.get('native'), f'{path}:owner{pid}')
            if (native['memory_uops'] != commit['memory_uops']
                    or native['hierarchy_incomplete_uops'] != 0
                    or native['no_ruby_uops'] > native['memory_uops']
                    or native['ruby_admission_fragments'] != native['ruby_response_fragments']
                    or native['ruby_hierarchy_request_fragments'] + native['sequencer_coalesced_fragments']
                       != native['ruby_admission_fragments']
                    or native['hierarchy']['l1d']['accesses'] != native['ruby_hierarchy_request_fragments']):
                raise ValueError(f'{path}: owner {pid} native population not conserved')
            for groups in (combined, local):
                target = groups['task' if pid in tasks else 'other']
                for k in FIELDS: target['commit'][k] += commit[k]
                add_native_population(target['native'], native)
        if set(actual) - seen:
            raise ValueError(f'{path}: omitted owner population')
        per_core.append({'core_id': meta['core_id'], 'owners': owners,
                         'pmu': flatten(local['task']), 'other_pmu': flatten(local['other'])})
    if load_capture_evidence(trace)['provenance'] != evidence['provenance']:
        raise ValueError('task PMU functional source changed during audit')
    task = combined['task']; pmu = flatten(task)
    return dict(schema='fastsim-task-pmu-audit-v1', valid=True, scope='workload-task',
                population_contract=ACCOUNTING_CONTRACT, task_ids=tasks, pmu=pmu,
                other=flatten(combined['other']), native=task['native'], per_core=per_core,
                source_provenance=evidence['provenance'], perf_equivalence='unverified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        result = audit_task_pmu(args.trace_dir)
        path = args.output or args.trace_dir / 'oracle/task-pmu-audit.json'
        path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'output': str(path), 'pmu': result['pmu']}))
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
