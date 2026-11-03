#!/usr/bin/env python3
"""Validate an exact-binary debug replay and join selected ROB-head loads.

An LSQ sent event binds instruction sequence to packet address/admission tick.
A unique ProtocolTrace Done with the same core/address and latency-derived
admission tick supplies the response. Ambiguous/split joins fail closed.
"""

import argparse
import json
from pathlib import Path
import re

from collect_tail_timing import sha256, write_json


SENT = re.compile(r'^(\d+): .*switch(\d+)\.core.*Memory request \(pkt: \w+ \[([0-9a-f]+):[^]]+\]\) from inst \[sn:(\d+)\] was sent ')
DONE = re.compile(r'^\s*(\d+)\s+(\d+)\s+Seq\s+Done.*\[0x([0-9a-f]+), line [^]]+\]\s+(\d+) cycles')
ARRIVAL = re.compile(r'^(\d+): board.memory.mem_ctrl(\d+)\.dram: Address: 0x([0-9a-f]+) Rank (\d+) Bank (\d+) Row (\d+)')
COMMAND = re.compile(r'^(\d+): board.memory.mem_ctrl(\d+)\.dram: Timing access to addr 0x([0-9a-f]+), rank/bank/row (\d+) (\d+) (\d+)')
REFRESH = re.compile(r'^(\d+): board.memory.mem_ctrl(\d+)\.dram\.rank(\d+): (Refresh due|Scheduling next request after refreshing)')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--debug-collection', type=Path, required=True)
    p.add_argument('--stage-collection', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    debug, stage = args.debug_collection, args.stage_collection
    provenance = json.loads((debug / 'collection.json').read_text())
    original = Path(provenance['original'])
    if json.loads((debug / 'completion.json').read_text())['returncode'] != 0:
        raise ValueError('debug collection failed')
    if provenance['gem5_sha256'] != provenance['original_gem5_sha256']:
        raise ValueError('debug replay must use original binary')
    cpls = (debug / 'trace/oracle/cpl_class.jsonl').read_text().splitlines()
    old_cpls = (original / 'oracle/cpl_class.jsonl').read_text().splitlines()
    cpls = {r['core_id']: r for r in map(json.loads, cpls)}
    if cpls != {r['core_id']: r for r in map(json.loads, old_cpls)}:
        raise ValueError('debug replay changed full-ROI CPL metrics')
    hashes = []
    for core in sorted(cpls):
        new = next((debug / 'trace').glob('*switch%d.*records.micro.fst' % core))
        old = original / 'tao_trace' / ('core%d.fst' % core)
        a, b = sha256(old), sha256(new)
        if a != b:
            raise ValueError('debug replay changed FST core %d' % core)
        hashes.append({'core': core, 'sha256': a})
    gaps = json.loads((stage / 'dense-gem5-gaps.json').read_text())
    paired = json.loads((stage / 'dense-paired.json').read_text())
    pairs = {p['micro_seq']: p for p in paired['cores'][0]['pairs']}
    core = gaps['core']
    period = cpls[core]['clock_period_ticks']
    fs = json.loads((stage / 'fastsim-dense.json').read_text())
    fs_samples = {s['sequence']: s for s in fs['cores'][core]['response_frontier_audit']}
    sent, done, arrivals, commands, refresh_starts, refresh_intervals = {}, [], [], [], {}, []
    log_path = debug / 'gem5/timing-debug.log'
    with log_path.open() as source:
        for line in source:
            if 'Memory request' in line:
                m = SENT.search(line)
                if m:
                    tick, c, addr, seq = m.groups()
                    sent.setdefault((int(c), int(seq)), []).append((int(tick), int(addr, 16)))
            elif ' Seq ' in line and 'Done' in line:
                m = DONE.search(line)
                if m:
                    tick, c, addr, latency = m.groups()
                    done.append((int(tick), int(c), int(addr, 16), int(latency)))
            elif ' Address:' in line or 'Timing access to addr' in line:
                m = ARRIVAL.search(line) or COMMAND.search(line)
                if m:
                    t, channel, addr, rank, bank, row = m.groups()
                    event = (int(t), int(channel), int(addr, 16), int(rank), int(bank), int(row))
                    (arrivals if ' Address:' in line else commands).append(event)
            elif 'refresh' in line.lower():
                m = REFRESH.search(line)
                if m:
                    tick, channel, rank, kind = m.groups()
                    key = (int(channel), int(rank))
                    if kind == 'Refresh due':
                        refresh_starts[key] = int(tick)
                    elif key in refresh_starts:
                        refresh_intervals.append((key, refresh_starts.pop(key), int(tick)))
    witnesses = []
    for head in gaps['issued_load_head_cycles']['top_uops']:
        pair = pairs[head['micro_seq']]
        requests = sent.get((core, pair['inst_seq_num']), [])
        if len(requests) != 1:
            raise ValueError('selected request is split, missing or retried after admission')
        issue, address = requests[0]
        responses = [d for d in done if d[1] == core and d[2] == address and
                     d[0] - d[3] * period == issue]
        if len(responses) != 1:
            raise ValueError('ambiguous selected native response')
        response = responses[0][0]
        if issue < head['issue_cycle'] * period or response > head['commit_cycle'] * period:
            raise ValueError('stage/debug timing identity mismatch')
        candidates = [a for a in arrivals if a[2] == address // 64 * 64 and issue <= a[0] <= response]
        if len(candidates) != 1:
            raise ValueError('selected DRAM arrival ambiguous or not present')
        arrival = candidates[0]
        candidates = [c for c in commands if c[1:] == arrival[1:] and arrival[0] <= c[0] <= response]
        if len(candidates) != 1:
            raise ValueError('selected DRAM scheduling event ambiguous')
        command = candidates[0]
        episodes = [(start, end) for key, start, end in refresh_intervals
                    if key == (arrival[1], arrival[3]) and start <= command[0] and end >= arrival[0]]
        s = fs_samples[pair['record_ordinal']]
        memory = [e for e in s['memory_events'] if not e['instruction_fetch']]
        if len(memory) != 1 or memory[0]['line'] != address // 64:
            raise ValueError('FastSim memory identity mismatch')
        witnesses.append({
            'micro_seq': pair['micro_seq'], 'inst_seq_num': pair['inst_seq_num'],
            'pc': pair['pc'], 'address': address, 'rob_head_zero_commit_cycles': head['cycles'],
            'gem5_issue_tick': issue, 'gem5_response_tick': response,
            'gem5_issue_to_admission_cycles': issue / period - head['issue_cycle'],
            'gem5_admission_to_response_cycles': (response - issue) / period,
            'gem5_response_to_commit_cycles': head['commit_cycle'] - response / period,
            'dram_arrival_tick': arrival[0], 'dram_schedule_tick': command[0],
            'dram_channel': arrival[1], 'dram_rank': arrival[3], 'dram_bank': arrival[4],
            'dram_row': arrival[5], 'dram_queue_until_schedule_cycles': (command[0] - arrival[0]) / period,
            'refresh_episodes_ticks': episodes,
            'scheduled_at_refresh_resume': any(end == command[0] for _, end in episodes),
            'fastsim_path': memory[0]['path'],
            'fastsim_canonical_arrival_cycle': memory[0]['canonical_dram_arrival_cycle'],
            'fastsim_canonical_command_cycle': memory[0]['canonical_dram_command_cycle'],
            'fastsim_corrected_issue_cycle': memory[0]['corrected_issue_cycle'],
            'fastsim_issue_to_response_cycles': memory[0]['latency_cycles'],
            'fastsim_issue_to_retire_cycles': pair['fastsim_issue_to_retire'],
        })
    result = {
        'schema': 'fastsim-tail-dram-witness-v1', 'full_fst_and_cpl_equal': True,
        'fst_sha256': hashes, 'debug_log_sha256': sha256(log_path),
        'clock_period_ticks': period, 'witnesses': witnesses,
        'selected_head_cycles': sum(w['rob_head_zero_commit_cycles'] for w in witnesses),
        'all_load_head_cycles': gaps['issued_load_head_cycles']['sum_cycles'],
        'window_gem5_cycles': gaps['elapsed_cycles'],
        'window_fastsim_cycles': paired['cores'][0]['pairs'][-1]['fastsim_retire_from_roi'] -
                                 paired['cores'][0]['pairs'][0]['fastsim_retire_from_roi'],
        'limitations': ['selected top ROB-head loads, not all request MLP',
                        'DRAM schedule call time is not the eventual RD command tick',
                        'local service gaps do not establish global CPI improvement'],
    }
    write_json(args.output, result)
    print('witnesses', len(witnesses), 'head_cycles', result['selected_head_cycles'])


if __name__ == '__main__':
    main()
