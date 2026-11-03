#!/usr/bin/env python3
"""Audit captured DRAM configuration and production decoder, without simulation.

This supplements audit_fs_profile_identity; it neither scores PMUs nor infers
CPI from controller means. Reference decoding follows AddrRange::getOffset and
DRAMInterface::decodePacket in the local gem5 source. Probe scenarios are fixed
synthetic streams, not observed gem5 request order.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import random
import re
import subprocess
from pathlib import Path

from audit_fs_profile_identity import target_cycles

TIMINGS = {
    't_cl': 'tCL', 't_rcd': 'tRCD', 't_rp': 'tRP', 't_ras': 'tRAS',
    't_rtp': 'tRTP', 't_rrd': 'tRRD', 't_rrd_l': 'tRRD_L', 't_xaw': 'tXAW',
    't_ccd_l': 'tCCD_L', 't_cs': 'tCS', 'burst_cycles': 'tBURST',
    't_cwl': 'tCWL', 't_rcd_wr': 'tRCD_WR', 't_ccd_l_wr': 'tCCD_L_WR',
    't_rtw': 'tRTW', 't_wtr': 'tWTR', 't_wtr_l': 'tWTR_L', 't_wr': 'tWR',
}
GEOMETRY = {
    'ranks_per_channel': 'ranks_per_channel', 'banks_per_channel': 'banks_per_rank',
    'bank_groups_per_rank': 'bank_groups_per_rank',
    'read_buffer_size': 'read_buffer_size', 'write_buffer_size': 'write_buffer_size',
    'activation_limit': 'activation_limit', 'max_accesses_per_row': 'max_accesses_per_row',
}
CTRL_FIELDS = {
    'write_high_threshold_percent': 'write_high_thresh_perc',
    'write_low_threshold_percent': 'write_low_thresh_perc',
    'min_reads_per_switch': 'min_reads_per_switch',
    'min_writes_per_switch': 'min_writes_per_switch',
}


def controllers(config):
    result = sorted(s for s in config.sections()
                    if re.fullmatch(r'board\.memory\.mem_ctrl\d+', s))
    if not result:
        raise ValueError('no DRAM controllers')
    return result


def audit_controllers(config, dram):
    period = int(config['board.clk_domain']['clock'])
    checks, refresh = [], []

    def add(ctrl, field, expected, actual):
        checks.append(dict(controller=ctrl, field=field, gem5=expected,
                           fastsim=actual, match=expected is not None and expected == actual))

    cs = controllers(config)
    add('all', 'channels', len(cs), dram.get('channels'))
    for ctrl in cs:
        d = config[config[ctrl]['dram']]
        for key, source in GEOMETRY.items():
            add(ctrl, key, int(d[source]) if source in d else None, dram.get(key))
        for key, source in TIMINGS.items():
            expected = target_cycles(int(d[source]), period) if source in d else None
            add(ctrl, key, expected, dram.get(key))
        for key, source in CTRL_FIELDS.items():
            add(ctrl, key, int(config[ctrl][source]) if source in config[ctrl] else None,
                dram.get(key))
        for key, source in [('frontend_latency', 'static_frontend_latency'),
                            ('backend_latency', 'static_backend_latency')]:
            expected = target_cycles(int(config[ctrl][source]), period) if source in config[ctrl] else None
            add(ctrl, key, expected, dram.get(key))
        add(ctrl, 'scheduler', config[ctrl].get('mem_sched_policy'), dram.get('scheduler'))
        row = (int(d['device_rowbuffer_size']) * int(d['devices_per_rank'])
               if 'device_rowbuffer_size' in d and 'devices_per_rank' in d else None)
        add(ctrl, 'row_bytes', row, dram.get('row_bytes'))
        add(ctrl, 'addr_mapping', d.get('addr_mapping'), 'RoRaBaCoCh')
        add(ctrl, 'page_policy_family', d.get('page_policy'), 'open_adaptive')
        refi = int(d['tREFI']) if 'tREFI' in d else None
        rfc = int(d['tRFC']) if 'tRFC' in d else None
        modeled=bool(dram.get('refresh_enabled',False))
        if modeled:
            for field,expected in [('t_refi_ticks',refi),('t_rfc_ticks',rfc),
                                   ('refresh_ticks_per_cycle',period)]:
                add(ctrl,field,expected,dram.get(field))
        refresh.append(dict(controller=ctrl, t_refi_ticks=refi, t_rfc_ticks=rfc,
                            t_refi_cycles=target_cycles(refi, period) if refi is not None else None,
                            t_rfc_cycles=target_cycles(rfc, period) if rfc is not None else None,
                            nominal_busy_fraction=rfc / refi if refi else None,
                            modeled=modeled,phase_semantics='FastSim-relative startup, native phase not certified'))
    return dict(direct_valid=all(x['match'] for x in checks), checks=checks,
                semantic_equivalence=False, refresh=refresh,
                limitations=[
                    'parameter equality does not establish command-calendar equivalence',
                    ('refresh/rank precharge enabled; native startup phase requires separate evidence'
                     if dram.get('refresh_enabled',False) else 'refresh/rank precharge lifecycle is disabled in compact DRAM'),
                    'read response capacity and hidden-bank preparation are approximate',
                    'page-policy queue visibility and row-cap handling require isolated probes',
                    'synthetic order sensitivity is not workload CPI attribution',
                ])


def parse_range(text):
    fields = [int(x) for x in text.split(':')]
    if len(fields) == 2:
        return fields[0], fields[1], 0, []
    if len(fields) < 4 or any(x <= 0 for x in fields[3:]):
        raise ValueError('unsupported DRAM range')
    return fields[0], fields[1], fields[2], fields[3:]


def remove_interleave(address, masks):
    # gem5 removes the least-significant set bit of every XOR mask.
    bits = sorted((mask & -mask).bit_length() - 1 for mask in masks)
    if len(set(bits)) != len(bits):
        raise ValueError('duplicate interleave removal bits')
    for i, bit in enumerate(bits):
        shift = bit - i
        address = ((address >> (shift + 1)) << shift) | (address & ((1 << shift) - 1))
    return address


def reference_decode(config, address, line_size, controller_names=None):
    found = []
    for channel, ctrl in enumerate(controllers(config) if controller_names is None else controller_names):
        d = config[config[ctrl]['dram']]
        if d['addr_mapping'] != 'RoRaBaCoCh':
            raise ValueError('unsupported reference address mapping')
        start, end, match, masks = parse_range(d['range'])
        selection = sum((bin(address & mask).count('1') % 2) << bit
                        for bit, mask in enumerate(masks))
        if not start <= address < end or selection != match:
            continue
        row_bytes = int(d['device_rowbuffer_size']) * int(d['devices_per_rank'])
        if row_bytes % line_size:
            raise ValueError('row size is not divisible by line size')
        local = remove_interleave(address, masks) - remove_interleave(start, masks)
        above = local // row_bytes
        bank = above % int(d['banks_per_rank'])
        above //= int(d['banks_per_rank'])
        rank = above % int(d['ranks_per_channel'])
        above //= int(d['ranks_per_channel'])
        # DRAMInterface rounds each assigned controller's capacity up to pow2.
        capacity = (end - start) >> len(masks)
        capacity = 1 << (capacity - 1).bit_length()
        rows = capacity // (row_bytes * int(d['banks_per_rank']) * int(d['ranks_per_channel']))
        if not rows:
            raise ValueError('invalid reference row geometry')
        found.append(dict(channel=channel, rank=rank, bank=bank,
                          bank_group=bank % int(d['bank_groups_per_rank']), row=above % rows))
    if len(found) != 1:
        raise ValueError('address does not select exactly one DRAM controller')
    return found[0]


def address_samples(config, line_size, count=4096):
    ranges = sorted({parse_range(config[config[c]['dram']]['range'])[:2]
                     for c in controllers(config)})
    rng, samples = random.Random(0), set()
    for start, end in ranges:
        samples.update([start, end - line_size])
        # Exhaust regional stripes, column, bank, rank and first row boundaries.
        for offset in [line_size * i for i in range(32)] + [
                (1 << bit) + delta for bit in range(6, 29)
                for delta in [-line_size, 0, line_size]]:
            if start <= start + offset < end:
                samples.add(start + offset)
        for _ in range(count):
            samples.add(rng.randrange(start // line_size, end // line_size) * line_size)
    return sorted(samples)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_probe_configuration(probe, scored_dram, line_size):
    if probe['line_size'] != line_size:
        raise ValueError('probe/reference cache line size mismatch')
    if probe['configuration'] != scored_dram:
        raise ValueError('probe configuration does not match scored FastSim configuration')


def audit_write_activity(text, expected_controller_ids):
    fields = ['writeReqs', 'writeBursts', 'dram.writeBursts', 'numWriteReadTurnArounds']
    pattern = re.compile(r'^board\.memory\.(mem_ctrl\d+)\.(writeReqs|writeBursts|dram\.writeBursts|numWriteReadTurnArounds)\s+(\d+)(?:\s|$)')
    values = {key: {} for key in fields}
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            controller, field, number = match.groups()
            controller = int(controller[len('mem_ctrl'):])
            if controller in values[field]:
                raise ValueError('duplicate controller stat; require a single common-end snapshot')
            values[field][controller] = int(number)
    population = set(values[fields[0]])
    if population != set(expected_controller_ids) or any(set(v) != population for v in values.values()):
        raise ValueError('incomplete physical controller write statistics')
    return dict({key: sum(v.values()) for key, v in values.items()},
                scope='physical-controller', per_controller=values)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gem5-config', type=Path, required=True)
    p.add_argument('--fastsim-report', type=Path, required=True)
    p.add_argument('--fastsim-config', type=Path, required=True)
    p.add_argument('--probe', type=Path, required=True)
    p.add_argument('--common-end-stats', type=Path,
                   help='Optional already-certified single common-end stats snapshot; physical write diagnostics only')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    c = configparser.ConfigParser(interpolation=None); c.read(a.gem5_config)
    report = json.loads(a.fastsim_report.read_text())
    fs = report['configuration']
    result = audit_controllers(c, fs['dram'])
    a.output.mkdir(parents=True, exist_ok=False)
    line_size = int(c['board.cache_hierarchy.ruby_system']['block_size_bytes'])
    samples = address_samples(c, line_size)
    sample_file = a.output / 'addresses.txt'
    sample_file.write_text(''.join(str(x) + '\n' for x in samples))
    probe_output = a.output / 'probe.json'
    subprocess.run([str(a.probe.resolve()), str(a.fastsim_config.resolve()),
                    str(sample_file.resolve()), str(probe_output.resolve())], check=True)
    probe = json.loads(probe_output.read_text())
    validate_probe_configuration(probe, fs['dram'], line_size)
    decoded = probe['addresses']
    if len(decoded) != len(samples):
        raise ValueError('probe did not return complete address population')
    mismatches = []
    controller_names = controllers(c)
    for address, row in zip(samples, decoded):
        expected = reference_decode(c, address, line_size, controller_names)
        if row['address'] != address or row['decoded'] != expected:
            mismatches.append(dict(address=address, reference=expected, probe=row))
    result['address_decode'] = dict(samples=len(samples), mismatches=mismatches,
                                    valid=not mismatches)
    result['inputs'] = {str(x.resolve()): sha256(x) for x in [
        a.gem5_config, a.fastsim_report, a.fastsim_config, a.probe,
        Path(__file__), Path(__file__).parents[1] / 'gem5-fs/src/base/addr_range.hh',
        Path(__file__).parents[1] / 'gem5-fs/src/mem/dram_interface.cc']}
    if a.common_end_stats:
        result['native_write_activity'] = audit_write_activity(
            a.common_end_stats.read_text(),
            {int(name.rsplit('mem_ctrl', 1)[1]) for name in controller_names})
        result['inputs'][str(a.common_end_stats.resolve())] = sha256(a.common_end_stats)
    result['probe'] = probe
    result['runtime_activation'] = {
        k: v for k, v in report.get('causal_frontier', {}).items()
        if k.startswith('dram_frfcfs_') or k.startswith('dram_write_queue_')}
    result['schema'] = 'fastsim-dram-mechanism-audit-v1'
    result['production_timing_changed'] = False
    (a.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(direct_valid=result['direct_valid'],
                         checks=len(result['checks']), address_decode=result['address_decode'])))
    return 0 if result['direct_valid'] and not mismatches else 1


if __name__ == '__main__':
    raise SystemExit(main())
