#!/usr/bin/env python3
"""Audit a frozen-window physical stats snapshot; never select CPI/cache truth."""
import argparse
import configparser
import hashlib
import json
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()
    before = Path(path).stat()
    with Path(path).open('rb') as source:
        for data in iter(lambda: source.read(8 << 20), b''):
            digest.update(data)
    after = Path(path).stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            'file changed while hashing: ' + str(path))
    return digest.hexdigest()


def schedule_tick(start, end, quantum):
    require(quantum >= 0 and end > start, 'invalid common window or quantum')
    # This producer's statistics::schedStatEvent adds simQuantum itself.
    when = end - quantum
    require(when > start, 'not enough lookahead to register common-end stats')
    return when


def select_snapshot(text, start, end):
    blocks = re.findall(
        r'^---------- Begin Simulation Statistics ----------\s*\n.*?'
        r'^---------- End Simulation Statistics\s+----------\s*$', text,
        re.MULTILINE | re.DOTALL)
    matches = []
    for block in blocks:
        ticks = re.findall(r'^finalTick\s+(\d+)\b', block, re.MULTILINE)
        require(len(ticks) == 1, 'stats block has missing/duplicate finalTick')
        if int(ticks[0]) == end:
            elapsed = re.findall(r'^simTicks\s+(\d+)\b', block, re.MULTILINE)
            require(len(elapsed) == 1 and int(elapsed[0]) == end - start,
                    'common-end stats reset does not match frozen common start')
            matches.append(block)
    require(len(matches) == 1, 'expected exactly one common-end stats block')
    return matches[0].strip() + '\n'


def read_cores(directory, prefix):
    result = {}
    for path in Path(directory).glob(prefix + '-core*.json'):
        core = int(re.fullmatch(re.escape(prefix) + r'-core(\d+)\.json', path.name)[1])
        require(core not in result, 'duplicate core')
        result[core] = json.loads(path.read_text())
    require(result and set(result) == set(range(len(result))), 'missing/noncontiguous cores')
    return result


def compare_boundaries(reference, observed):
    require(set(reference) == set(observed) and bool(reference), 'participant cores differ')
    for core, expected in reference.items():
        actual = observed[core]
        require(expected.get('measurement_closed') is True and
                actual.get('measurement_closed') is True, 'incomplete functional boundary')
        # All functional counts and policy fields must agree, not just totals.
        require(actual == expected, 'functional boundary differs on core ' + str(core))


def reference_window(source):
    source = Path(source)
    directory = source / 'oracle' if (source / 'oracle').exists() else source / 'tao_trace/oracle'
    bounds = read_cores(directory, 'time-accounting')
    windows = {(x['common_start_tick'], x['common_end_tick']) for x in bounds.values()}
    require(len(windows) == 1 and all(x['common_cut_complete'] for x in bounds.values()),
            'incomplete/inconsistent reference common window')
    start, end = windows.pop()
    require(0 <= start < end, 'invalid reference window')
    return start, end, len(bounds)


def validate_detailed_queues(config_path, cores):
    config = configparser.ConfigParser()
    config.read(config_path)
    detailed = [s for s in config.sections() if config[s].get('type') == 'BaseO3CPU']
    require(len(detailed) == cores, 'detailed core count differs')
    for section in config.sections():
        obj = config[section]
        require(obj.get('eventq_index', '0') == '0' or obj.get('type') == 'X86KvmCPU',
                'active detailed/memory object outside queue0: ' + section)


def validate_target_exit(result, end):
    # 86 is the simulator event code consumed by the frozen runner; the
    # host process still exits 0 and is checked independently by its guard.
    require(result.get('cause') == 'a thread reached the max instruction count' and
            result.get('exit_code') == 86 and result.get('tick', 0) >= end,
            'capture did not reach the intended target-drain exit')


def watch_actual_common_end(m5, trace_dir, period, cores, on_dump):
    """Poll a synchronous producer close marker after each queue0 CPU tick.

    Only valid after switching all active detailed CPUs/memory to queue0.
    The existing producer closes cpi-core*.json in finalizeCplMeasurement,
    synchronously at the first-core target, before the CPU tick returns.
    """
    require(period > 0 and cores > 0, 'invalid watcher clock/population')
    marker = Path(trace_dir) / 'oracle/cpi-core0.json'
    require(not marker.exists(), 'stale common-end marker at measurement start')
    state = dict(polls=0, dumped=False)
    queue = m5.event.getEventQueue(0)

    def poll():
        state['polls'] += 1
        now = int(m5.curTick())
        if marker.exists():
            markers = read_cores(marker.parent, 'cpi')
            require(set(markers) == set(range(cores)), 'incomplete common-end close markers')
            m5.stats.dump()
            state.update(dumped=True, common_end_tick=now)
            on_dump(now, state['polls'])
        else:
            queue.schedule(event, now + period)

    event = m5.event.create(poll, priority=90)
    state['event'] = event  # Retain the Python event for its whole scheduled lifetime.
    now = int(m5.curTick())
    # Reset ticks need not be CPU edges. Never poll on reset-relative phases.
    queue.schedule(event, (now // period + 1) * period)
    return state


def audit_actual_window(source):
    """Certify only this capture's internal window, never old-source equality."""
    source = Path(source).resolve()
    start, end, cores = reference_window(source)
    boundaries = read_cores(source / 'tao_trace', 'functional-boundary')
    require(set(boundaries) == set(range(cores)) and
            all(x.get('measurement_closed') is True and x.get('common_end_tick') == end and
                x.get('participant_count') == cores and
                x.get('measurement_policy') == 'first-core-target-common-end-v1'
                for x in boundaries.values()), 'functional boundary core set/window differs')
    validate_detailed_queues(source / 'config.ini', cores)
    metadata = json.loads((source / 'common-end-stats-schedule.json').read_text())
    require(metadata.get('mode') == 'actual-common-end-queue0-close-marker' and
            metadata.get('detailed_queue0_verified') is True and
            metadata.get('reset_tick') == metadata.get('common_start_tick') == start and
            metadata.get('common_end_tick') == end and
            metadata.get('participant_cores') == cores and
            end % metadata['clock_period_ticks'] == 0,
            'dynamic observer does not match actual common window')
    snapshot = select_snapshot((source / 'stats.txt').read_text(), start, end)
    result = json.loads((source / 'sync-run-result.json').read_text())
    validate_target_exit(result, end)
    receipt = json.loads((source / 'capture-receipt.json').read_text())
    require(receipt.get('returncode') == 0 and receipt.get('guard_reason') is None and
            receipt.get('measurement_open') is True, 'capture guard rejected run')
    traces = list((source / 'tao_trace').glob('*.fst'))
    require(len(traces) == cores, 'FST core population differs')
    output = source / 'stats-common-end.txt'
    if output.exists():
        require(output.read_text() == snapshot, 'existing snapshot differs from raw block')
    else:
        output.write_text(snapshot)
    return dict(schema='fastsim-actual-common-end-stats-audit-v1', same_window_valid=True,
                frozen_reference_match='not-tested', source=str(source),
                participant_cores=cores, common_start_tick=start, common_end_tick=end,
                sim_ticks=end-start, actual_exit_tick=result['tick'],
                snapshot=str(output), snapshot_sha256=sha256(output),
                scope='global queue0 physical stats after CPU events at common-end tick; not retired-demand cohort',
                crossing_requests='counter-owned arrival/service/completion population; no drain included')


def audit(reference, source):
    reference, source = Path(reference).resolve(), Path(source).resolve()
    require(reference != source, 'cannot audit/rewrite the reference in place')
    start, end, cores = reference_window(reference)
    # Raw producer oracles live below tao_trace before source preparation.
    observed_accounts = read_cores(source / 'tao_trace/oracle', 'time-accounting')
    require(set(observed_accounts) == set(range(cores)) and
            all(x.get('common_start_tick') == start and x.get('common_end_tick') == end and
                x.get('common_cut_complete') is True for x in observed_accounts.values()),
            'oracle common window differs from frozen reference')
    reference_boundaries = read_cores(reference / 'tao_trace', 'functional-boundary')
    observed_boundaries = read_cores(source / 'tao_trace', 'functional-boundary')
    require(set(reference_boundaries) == set(observed_boundaries) == set(range(cores)),
            'functional boundary core set differs from account population')
    compare_boundaries(reference_boundaries, observed_boundaries)
    config = (source / 'config.ini').read_text()
    quantum = re.findall(r'^sim_quantum=(\d+)$', config, re.MULTILINE)
    require(len(quantum) == 1, 'missing/ambiguous quantum')
    instrumentation = json.loads((source / 'common-end-stats-schedule.json').read_text())
    require(instrumentation['common_start_tick'] == start and
            instrumentation['common_end_tick'] == end and
            instrumentation['reset_tick'] == start and
            instrumentation['sim_quantum_ticks'] == int(quantum[0]) and
            (instrumentation.get('detailed_queue0_verified') is True if
             instrumentation.get('mode') == 'actual-common-end-queue0-close-marker' else
             instrumentation['sched_event_when'] == schedule_tick(start, end, int(quantum[0]))),
            'snapshot schedule differs from frozen window')
    snapshot = select_snapshot((source / 'stats.txt').read_text(), start, end)
    ref_fst = {p.name: p for p in (reference / 'tao_trace').glob('*.fst')}
    new_fst = {p.name: p for p in (source / 'tao_trace').glob('*.fst')}
    require(len(ref_fst) == cores and ref_fst.keys() == new_fst.keys(), 'FST core set differs')
    hashes = {}
    for name, path in sorted(ref_fst.items()):
        old_hash, new_hash = sha256(path), sha256(new_fst[name])
        require(old_hash == new_hash, 'FST content differs: ' + name)
        hashes[name] = old_hash
    # The existing exit/drain must still complete normally, after the snapshot.
    result = json.loads((source / 'sync-run-result.json').read_text())
    validate_target_exit(result, end)
    output = source / 'stats-common-end.txt'
    if output.exists():
        require(output.read_text() == snapshot, 'existing snapshot differs from exact raw block')
    else:
        output.write_text(snapshot)
    return dict(schema='fastsim-common-end-physical-stats-audit-v1', valid=True,
                reference=str(reference), source=str(source), participant_cores=cores,
                common_start_tick=start, common_end_tick=end, sim_ticks=end-start,
                actual_exit_tick=result['tick'], sim_quantum_ticks=int(quantum[0]),
                snapshot=str(output), snapshot_sha256=sha256(output), fst_sha256=hashes,
                functional_boundaries_equal=True, fst_content_equal=True,
                scope='global physical stats at end of common-end tick; not retired-demand cohort',
                crossing_requests='native counters retain their own arrival/service/completion semantics; no drain included')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--actual-window', action='store_true',
                        help='check this capture only; does not certify frozen-reference equality')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'audit output already exists')
    require(args.actual_window != bool(args.reference), 'choose --actual-window or --reference')
    result = audit_actual_window(args.source) if args.actual_window else audit(args.reference, args.source)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
