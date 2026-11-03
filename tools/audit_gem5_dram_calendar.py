#!/usr/bin/env python3
"""Offline same-request DRAM differential; gem5 timestamps never enter inference.

The bounded capture starts with unknown bank/queue state. Ordered replay
conditions on gem5 selection times to isolate the command calendar. Arrival
replay instead exercises FastSim's existing FR-FCFS design. Neither is a CPI
prediction, and refresh is explicitly marked as unsupported by this model.
"""

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess

from audit_gem5_dram_parameters import TIMINGS, audit


LINE = re.compile(r'^(\d+): board.memory.mem_ctrl(\d+)([^:]*): (.*)')
ARRIVAL = re.compile(r'Address: (0x[0-9a-f]+) Rank (\d+) Bank (\d+) Row (\d+)')
SELECT = re.compile(r'Timing access to addr (0x[0-9a-f]+), rank/bank/row (\d+) (\d+) (\d+)')


def parse(path, channels, ranks, banks, row_bytes):
    arrivals, commands, current, pending = [], [], {}, defaultdict(deque)
    request_types = Counter()
    max_queue = defaultdict(int)
    refresh, refresh_start = [], {}
    boundary_commands = 0
    auto_precharges = 0
    command_contentions = 0
    for line in path.open():
        m = LINE.match(line)
        if not m:
            continue
        tick, channel, suffix, message = int(m[1]), int(m[2]), m[3], m[4]
        m = ARRIVAL.match(message)
        if m:
            address, rank, bank, row = int(m[1], 16), int(m[2]), int(m[3]), int(m[4])
            local = address // 64 // channels // (row_bytes // 64)
            decoded = (address // 64 % channels, local // banks % ranks,
                       local % banks, local // banks // ranks)
            if decoded != (channel, rank, bank, row):
                raise ValueError('address mapping mismatch')
            item = dict(id=len(arrivals), arrival=tick, channel=channel,
                        address=address, rank=rank, bank=bank, row=row)
            arrivals.append(item)
            pending[channel, address].append(item)
            continue
        m = SELECT.match(message)
        if m:
            address = int(m[1], 16)
            if pending[channel, address]:
                item = dict(pending[channel, address].popleft())
                if (item['rank'], item['bank'], item['row']) != tuple(map(int, m.groups()[1:])):
                    raise ValueError('selection geometry mismatch')
            else:
                boundary_commands += 1
                item = dict(id=-boundary_commands, address=address, channel=channel,
                            rank=int(m[2]), bank=int(m[3]), row=int(m[4]), arrival=None)
            item.update(schedule=tick, row_hit=True)
            commands.append(item)
            current[channel] = item
            continue
        if message.startswith('Activate at tick '):
            current[channel]['row_hit'] = False
            current[channel]['activation'] = int(message.split()[-1])
        elif message.startswith('Schedule RD/WR burst at tick '):
            current[channel]['command'] = int(message.split()[-1])
        elif message.startswith('Access to '):
            m = re.match(r'Access to (0x[0-9a-f]+), ready at (\d+) next burst at (\d+)', message)
            if m:
                if current[channel]['address'] != int(m[1], 16):
                    raise ValueError('response-ready address mismatch')
                current[channel]['ready'] = int(m[2])
        elif message.startswith('recvTimingReq: request '):
            request_types[message.split()[2]] += 1
        elif message.startswith('Read queue limit '):
            m = re.search(r'current size (\d+)', message)
            max_queue[channel] = max(max_queue[channel], int(m[1]))
        elif message.startswith('Auto-precharged bank:'):
            current[channel]['auto_precharged'] = True
            auto_precharges += 1
        elif 'Contention found on command bus' in message:
            command_contentions += 1
        elif suffix.startswith('.dram.rank'):
            rank = int(suffix.split('rank')[1])
            key = channel, rank
            if message == 'Refresh due':
                refresh_start[key] = tick
            elif message.startswith('Scheduling next request after refreshing') and key in refresh_start:
                refresh.append(dict(channel=channel, rank=rank, start=refresh_start.pop(key), end=tick))
    if set(request_types) != {'ReadReq'}:
        raise ValueError('this bounded probe supports read-only intervals: ' + str(request_types))
    if any('ready' not in c or 'command' not in c for c in commands):
        raise ValueError('capture must include complete MemCtrl/DRAM command blocks')
    return dict(arrivals=arrivals, commands=commands, refresh=refresh,
                request_types=dict(request_types), max_observed_read_plus_response_queue=dict(max_queue),
                unmatched_start_boundary_commands=boundary_commands,
                pending_at_end=sum(map(len, pending.values())),
                auto_precharges=auto_precharges, command_bus_contentions=command_contentions)


def run_probe(binary, config, requests, mode, window, output):
    command = [str(binary.resolve()), str(config.resolve()), str(requests.resolve()), mode, str(window)]
    with output.open('w') as target:
        subprocess.run(command, stdout=target, check=True)
    rows = {}
    for line in output.read_text().splitlines():
        ordinal, col, completion, hit = map(int, line.split())
        if ordinal in rows:
            raise ValueError('duplicate probe ordinal')
        rows[ordinal] = dict(command=col, completion=completion, row_hit=hit)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collection', required=True, type=Path)
    p.add_argument('--witness', required=True, type=Path)
    p.add_argument('--fastsim-stats', required=True, type=Path)
    p.add_argument('--base-config', required=True, type=Path)
    p.add_argument('--probe', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    collection = json.loads((a.collection/'collection.json').read_text())
    if json.loads((a.collection/'completion.json').read_text())['returncode'] != 0:
        raise ValueError('collection did not complete')
    original = Path(collection['original'])
    params = audit(original/'config.ini', a.fastsim_stats)
    witness = json.loads(a.witness.read_text())
    if not witness['full_fst_and_cpl_equal']:
        raise ValueError('witness provenance invalid')
    # Revalidate the new capture: do not reuse a previous run's hashes as proof.
    new_cpl = {x['core_id']: x for x in map(json.loads, (a.collection/'trace/oracle/cpl_class.jsonl').read_text().splitlines())}
    old_cpl = {x['core_id']: x for x in map(json.loads, (original/'oracle/cpl_class.jsonl').read_text().splitlines())}
    if new_cpl != old_cpl or collection['gem5_sha256'] != collection['original_gem5_sha256']:
        raise ValueError('new capture changed gem5 binary or full CPL')
    fst_checks = []
    for check in witness['fst_sha256']:
        file = next((a.collection/'trace').glob('*switch%d.*records.micro.fst' % check['core']))
        h = hashlib.sha256()
        with file.open('rb') as source:
            for block in iter(lambda: source.read(8 << 20), b''):
                h.update(block)
        if h.hexdigest() != check['sha256']:
            raise ValueError('new capture changed full FST')
        fst_checks.append(check)
    cfg = json.loads(a.fastsim_stats.read_text())['configuration']['dram']
    log = a.collection/'gem5/timing-debug.log'
    data = parse(log, cfg['channels'], cfg['ranks_per_channel'], cfg['banks_per_channel'], cfg['row_bytes'])
    period = params['clock_period_ticks']
    t0 = min(c['schedule'] for c in data['commands'])
    t0 = min(t0, min(c['arrival'] for c in data['arrivals']))
    columns = ['id', 'arrival', 'schedule', 'command', 'ready', 'channel', 'address', 'rank', 'bank', 'row', 'row_hit']
    with (a.out/'native-commands.tsv').open('w') as out:
        out.write('\t'.join(columns) + '\n')
        for c in data['commands']:
            out.write('\t'.join(str(c[k]) for k in columns) + '\n')
    for kind, requests, time_key in [('arrival', data['arrivals'], 'arrival'), ('ordered', data['commands'], 'schedule')]:
        (a.out/(kind+'.tsv')).write_text(''.join('%d %d %d\n' % (r[time_key]-t0, r['address']//64, r['id']) for r in requests))
    # Use exact ticks for captured constraints to separate quantization error
    # from missing mechanisms. Current parameters are scaled to the same unit.
    configs = {}
    for arm in ['current', 'captured', 'captured-causal']:
        path = a.out/(arm+'-ticks.cfg')
        lines = ['config.include = '+str(a.base_config.resolve())]
        for t in params['timings']:
            lines.append('dram.%s = %d' % (t['fastsim'], t['current_cycles']*period if arm=='current' else t['ticks']))
        limit = next(c['gem5_value'] for c in params['counts'] if c['fastsim']=='activation_limit')
        lines.append('dram.activation_limit = %d' % (cfg['activation_limit'] if arm=='current' else limit))
        lines.append('dram.frfcfs_causal_selection = ' + ('true' if arm=='captured-causal' else 'false'))
        path.write_text('\n'.join(lines)+'\n'); configs[arm]=path
    probes = {}
    for arm, path in configs.items():
        probes[arm+'-ordered'] = run_probe(a.probe, path, a.out/'ordered.tsv', 'ordered', 1, a.out/(arm+'-ordered.tsv'))
        # The C4/C8 topology heuristic reduces production to one candidate
        # and bypasses this batch solver. 8 and 64 isolate configured and
        # full-queue visibility offline; none changes the fixed core Q.
        for window in [1, 8, 64]:
            key = arm+'-arrival-w'+str(window)
            probes[key] = run_probe(a.probe, path, a.out/'arrival.tsv', 'frfcfs', window, a.out/(key+'.tsv'))
    selected = []
    for w in witness['witnesses']:
        matches = [c for c in data['commands'] if c['address']==w['address']//64*64 and c['schedule']==w['dram_schedule_tick']]
        if len(matches) != 1:
            raise ValueError('selected witness command not unique')
        c = matches[0]
        if c['arrival'] != w['dram_arrival_tick']:
            raise ValueError('selected witness arrival changed')
        tail = next(t['ticks'] for t in params['timings'] if t['gem5']=='tCL') + next(t['ticks'] for t in params['timings'] if t['gem5']=='tBURST')
        if c['ready'] != c['command']+tail:
            raise ValueError('native RD command to data-ready semantics mismatch')
        selected.append(dict(w, native_command_tick=c['command'], native_ready_tick=c['ready'],
                             native_row_hit=c['row_hit'],
                             arrival_to_command_cycles=(c['command']-c['arrival'])/period,
                             schedule_to_command_cycles=(c['command']-c['schedule'])/period,
                             native_probes={k:dict(command_error_cycles=(rows[c['id']]['command']+t0-c['command'])/period,
                                                  arrival_to_command_cycles=(rows[c['id']]['command']+t0-c['arrival'])/period,
                                                  row_hit=rows[c['id']]['row_hit']) for k,rows in probes.items()}))
    result = dict(schema='fastsim-gem5-dram-calendar-audit-v1', full_fst_and_cpl_equal=True,
                  fst_sha256=fst_checks, gem5_sha256=collection['gem5_sha256'],
                  debug_log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                  probe_binary_sha256=hashlib.sha256(a.probe.read_bytes()).hexdigest(),
                  source_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in [Path(__file__), Path('tools/replay_dram_probe.cpp'), Path('src/simulator.cpp')]},
                  clock_period_ticks=period, tick_origin=t0,
                  capture={k:v for k,v in data.items() if k not in ('arrivals','commands')},
                  arrival_count=len(data['arrivals']), command_count=len(data['commands']),
                  all_arrival_addresses_match_mapping=True,
                  selected=selected, limitations=[
                      'bounded capture starts with unknown bank and in-flight state',
                      'ordered replay conditions on gem5 selection times and is diagnostic only',
                      'arrival replay omits requests already queued at debug start',
                      'refresh, command bandwidth and full gem5 scheduling are not implemented by this probe',
                      'a same-arrival diagnostic is not a production CPI estimate'])
    (a.out/'calendar-audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['arrival_count','command_count','capture']}))
    for key in probes:
        errors = [x['native_probes'][key]['command_error_cycles'] for x in selected if not x['scheduled_at_refresh_resume']]
        print(key, '19 commands not selected at refresh resume, error min/median/max:', min(errors), statistics.median(errors), max(errors))


if __name__ == '__main__':
    main()
