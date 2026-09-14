#!/usr/bin/env python3
"""Offline identity/measurement gate for a causal FST prefix and gem5 JSONL.

Timing labels are diagnostic output only. This deliberately does not certify
CPI or treat TaoTrace ready_tick/complete_tick as a memory response timestamp.
The input directory contains coreN.fst files starting at source ordinal zero.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import struct

from audit_fst_dependencies import HEADER, RECORD, audit


FLAG_FIELDS = {
    1: 'is_load', 2: 'is_store', 3: 'is_atomic', 4: 'is_branch',
    5: 'is_branch_cond', 6: 'is_branch_indirect', 7: 'is_call',
    8: 'is_return', 9: 'branch_taken', 10: 'is_microop',
    11: 'is_last_microop', 13: 'is_serialize',
}


def require_equal(actual, expected, location):
    if actual != expected:
        raise ValueError('{}: {} != {}'.format(location, actual, expected))


def read_asmap(path, core, count):
    raw = path.read_bytes()
    magic, version, size, row_size, owner, records, rows, reserved = struct.unpack_from(
        '<8sIIIIQQQ', raw)
    require_equal((magic, version, size, row_size, owner, records, reserved),
                  (b'FSTASM1\0', 1, 48, 16, core, count, 0), str(path))
    require_equal(len(raw), 48 + rows * 16, 'asmap length')
    entries = [struct.unpack_from('<QQ', raw, 48 + i * 16) for i in range(rows)]
    if not entries or entries[0][0] != 0 or any(
            a[0] >= b[0] for a, b in zip(entries, entries[1:])):
        raise ValueError('asmap must cover a continuous prefix from zero')
    if entries[-1][0] >= count:
        raise ValueError('asmap transition exceeds prefix')
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fst-dir', required=True, type=Path)
    parser.add_argument('--gem5-trace-dir', required=True, type=Path)
    parser.add_argument('--gem5-config', required=True, type=Path)
    parser.add_argument('--stats', required=True, type=Path)
    parser.add_argument('--events', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    stats = json.loads(args.stats.read_text())
    config = stats['configuration']
    board = json.loads(args.gem5_config.read_text())['board']
    cpus = [entry['core'] for entry in board['processor']['switch']]
    require_equal(config['cores'], len(cpus), 'core count')
    period, = board['clk_domain']['clock']
    phases = {}
    for row in csv.DictReader(args.events.open()):
        if row['kind'] not in {'fetch', 'dispatch', 'ready', 'issue', 'writeback', 'retire'}:
            continue
        key = int(row['core']), int(row['sequence'])
        target = phases.setdefault(key, {})
        if row['kind'] in target:
            raise ValueError('duplicate phase: {}'.format(row))
        target[row['kind']] = int(row['cycle'])
        target['measured'] = bool(int(row['measured']))

    identity, witnesses, boundaries = [], [], []
    for core, cpu in enumerate(cpus):
        fst = args.fst_dir / 'core{}.fst'.format(core)
        dependency_check = audit(fst, require_complete=True)
        require_equal(dependency_check['core'], core, 'FST core identity')
        count = dependency_check['records']
        asmap = read_asmap(Path(str(fst) + '.asmap'), core, count)
        as_index = 0
        prefix = 'board.processor.switch{}.core.tao_trace.tao_trace.'.format(core)
        path = args.gem5_trace_dir / (prefix + 'records.micro.jsonl')
        labels = args.gem5_trace_dir / (prefix + 'labels.micro.jsonl')
        record_digest = hashlib.sha256()
        label_digest = hashlib.sha256()
        measured_start = None
        alu_witnesses = []
        with fst.open('rb') as f, path.open() as reference, labels.open() as timing:
            f.seek(HEADER.size)
            for ordinal in range(count):
                record = RECORD.unpack(f.read(RECORD.size))
                raw, label_raw = next(reference), next(timing)
                record_digest.update(raw.encode())
                label_digest.update(label_raw.encode())
                j, label = json.loads(raw), json.loads(label_raw)
                location = 'core {} ordinal {}'.format(core, ordinal)
                while as_index + 1 < len(asmap) and asmap[as_index + 1][0] <= ordinal:
                    as_index += 1
                fields = {
                    'core_id': core, 'micro_seq': ordinal + 1,
                    'macro_pc': record[0], 'size': record[8],
                    'op_class': record[10] if record[10] >= 0 else -record[10] - 2,
                    'n_src': record[11], 'n_dst': record[12],
                    'address_space_id': asmap[as_index][1],
                }
                if record[9] & 6:
                    fields['paddr'] = record[1]
                if record[9] & 16:
                    fields['branch_target'] = record[2]
                    fields['branch_next_pc'] = record[3]
                for bit, name in FLAG_FIELDS.items():
                    fields[name] = int(bool(record[9] & (1 << bit)))
                for name, value in fields.items():
                    require_equal(value, j[name], location + ' ' + name)
                require_equal(record[10] < -1, j['cpl'] != 3, location + ' privilege')
                require_equal((label['core_id'], label['micro_seq']),
                              (core, ordinal + 1), location + ' label identity')
                p = phases[core, ordinal]
                if p['measured'] and measured_start is None:
                    measured_start = ordinal
                # Only the initial non-memory, non-control segment is an ALU
                # phase witness. Do not infer cache data-ready or WB contention
                # from completeTick: existing collectors do not establish that.
                if len(alu_witnesses) == ordinal and not record[9] & (6 | 8 | 16 | 8192):
                    if label['ready_source'] != 0 or label['complete_tick'] < label['issue_tick']:
                        raise ValueError('missing initial ALU timing witness')
                    require_equal(label['ready_tick'], label['fetch_tick'] + label['complete_tick'],
                                  location + ' label completion convention')
                    alu_witnesses.append({
                        'ordinal': ordinal, 'pc': hex(record[0]),
                        'fastsim_phases': {k: v for k, v in p.items() if k != 'measured'},
                        'gem5_cycles_from_own_fetch': {
                            'issue': label['issue_tick'] / period,
                            'execution_complete': label['complete_tick'] / period,
                            'retire': (label['commit_tick'] - label['fetch_tick']) / period,
                        },
                    })
        boundary = json.loads((args.gem5_trace_dir /
                               'functional-boundary-core{}.json'.format(core)).read_text())
        require_equal(boundary['core_id'], core, 'boundary identity')
        boundaries.append({
            'core': core, 'prefix_records': count,
            'fastsim_measured_start_ordinal': measured_start,
            'gem5_measured_start_ordinal': boundary['warmup_records'],
            'same_measurement_start': measured_start == boundary['warmup_records'],
            'prefix_entirely_before_gem5_roi': count <= boundary['warmup_records'],
        })
        identity.append({
            'core': core, 'matched_records': count, 'functional_identity': True,
            'checked_fields': sorted(fields) + ['kernel_privilege'],
            'not_represented_in_hot_fst': ['gem5 seq_num', 'micro_pc'],
            'dependency_validation': dependency_check,
            'gem5_record_prefix_sha256': record_digest.hexdigest(),
            'gem5_label_prefix_sha256': label_digest.hexdigest(),
        })
        witnesses.append({'core': core, 'initial_alu_segment': alu_witnesses[:16]})

    ruby = board['cache_hierarchy']['ruby_system']
    memory = board['memory']['mem_ctrl']
    cpu = cpus[0]
    hardware = []

    def compare(name, actual, target):
        hardware.append({'field': name, 'fastsim': actual, 'gem5': target, 'equal': actual == target})

    for name, target in [('fetch_width', 'fetchWidth'), ('decode_width', 'decodeWidth'),
                         ('rename_width', 'renameWidth'), ('dispatch_width', 'dispatchWidth'),
                         ('issue_width', 'issueWidth'), ('writeback_width', 'wbWidth'),
                         ('commit_width', 'commitWidth'), ('rob_entries', 'numROBEntries'),
                         ('lq_entries', 'LQEntries'), ('sq_entries', 'SQEntries')]:
        compare(name, config[name], cpu[target])
    for name, target in [('l1d', ruby['l1_controllers'][0]['Dcache']),
                         ('l2', ruby['l2_controllers'][0]['cache']),
                         ('llc', ruby['l3_controllers'][0]['L2cache'])]:
        compare(name + '.size_bytes', config[name]['size_bytes'],
                target['size'] * (len(ruby['l3_controllers']) if name == 'llc' else 1))
        compare(name + '.associativity', config[name]['associativity'], target['assoc'])
    compare('cha_count', config['cha_count'], len(ruby['l3_controllers']))
    compare('dram.channels', config['dram']['channels'], len(memory))
    for name, target in [('ranks_per_channel', 'ranks_per_channel'),
                         ('banks_per_channel', 'banks_per_rank'),
                         ('bank_groups_per_rank', 'bank_groups_per_rank')]:
        compare('dram.' + name, config['dram'][name], memory[0]['dram'][target])
    compare('dram.size_bytes', config['dram']['size_bytes'],
            int(memory[0]['dram']['range'].split(':')[1]))

    result = {
        'scope': 'offline functional-prefix identity and phase diagnosis; not a CPI accuracy result',
        'gem5_clock_period_ticks': period,
        'inputs': {name: str(value.resolve()) for name, value in vars(args).items() if name != 'output'},
        'functional_identity': identity, 'measurement_boundaries': boundaries,
        'hardware_subset': hardware, 'initial_alu_phase_witnesses': witnesses,
        'cpi_comparison_qualified': False,
        'remaining_gaps': [
            'Prefix cut is an integration-test boundary, not the captured native-FS ROI.',
            'Ideal I-side/translation omits their latency and shared memory traffic.',
            'FCFS/conservative coherence and miss slots do not reproduce Ruby/FRFCFS/Sequencer.',
            'Cache/controller/DRAM service timing is not certified by the hardware-geometry subset check.',
            'Memory complete_tick/ready_tick is not certified as LSQ data return; never pair it with data callback.',
            'Per-core initial fetch offsets and speculative activity are not reproduced.',
        ],
    }
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'matched_records': sum(x['matched_records'] for x in identity),
                      'hardware_subset_mismatches': [x for x in hardware if not x['equal']],
                      'measurement_boundaries': boundaries, 'cpi_comparison_qualified': False}, indent=2))


if __name__ == '__main__':
    main()
