#!/usr/bin/env python3
"""Compare the actual gem5 config.ini with an effective FastSim stats config.

Hardware parameters are public inputs; neither CPI nor oracle request times
are used to generate the optional, explicit experimental override.
"""

import argparse
import configparser
import hashlib
import json
from pathlib import Path


TIMINGS = {
    'tCL': 't_cl', 'tRCD': 't_rcd', 'tRP': 't_rp', 'tRAS': 't_ras',
    'tRTP': 't_rtp', 'tRRD': 't_rrd', 'tRRD_L': 't_rrd_l',
    'tXAW': 't_xaw', 'tBURST': 'burst_cycles', 'tCCD_L': 't_ccd_l',
    'tCS': 't_cs', 'static_frontend_latency': 'frontend_latency',
    'static_backend_latency': 'backend_latency',
}
MISSING = ['tREFI', 'tRFC', 'tCWL', 'tRCD_WR', 'tCCD_L_WR',
           'tRTW', 'tWR', 'tWTR', 'tWTR_L', 'tCK', 'tAAD', 'tPPD']
COUNTS = {
    'banks_per_rank': 'banks_per_channel',
    'ranks_per_channel': 'ranks_per_channel',
    'bank_groups_per_rank': 'bank_groups_per_rank',
    'read_buffer_size': 'read_buffer_size',
    'write_buffer_size': 'write_buffer_size',
    'max_accesses_per_row': 'max_accesses_per_row',
    'activation_limit': 'activation_limit',
    'min_reads_per_switch': 'min_reads_per_switch',
    'min_writes_per_switch': 'min_writes_per_switch',
    'write_high_thresh_perc': 'write_high_threshold_percent',
    'write_low_thresh_perc': 'write_low_threshold_percent',
}


def fingerprint(path):
    return {'path': str(path.resolve()),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def audit(ini_path, stats_path):
    ini = configparser.ConfigParser(interpolation=None)
    ini.optionxform = str
    ini.read(ini_path)
    stats = json.loads(stats_path.read_text())
    fs = stats['configuration']['dram']
    names = sorted(s for s in ini.sections() if ini[s].get('type') == 'MemCtrl')
    if not names:
        raise ValueError('no actual gem5 MemCtrl sections')
    periods = {int(ini[ini[n]['clk_domain']]['clock']) for n in names}
    if len(periods) != 1:
        raise ValueError('heterogeneous clock domains require separate conversion')
    period = periods.pop()
    controllers = [{**dict(ini[n]), **dict(ini[ini[n]['dram']])} for n in names]
    first = controllers[0]
    for name in list(TIMINGS) + MISSING + list(COUNTS) + [
            'addr_mapping', 'page_policy', 'device_rowbuffer_size', 'devices_per_rank']:
        if len({c[name] for c in controllers}) != 1:
            raise ValueError('nonuniform channel parameter: ' + name)
    timings = []
    for name, key in TIMINGS.items():
        tick = int(first[name])
        value = fs[key]
        timings.append(dict(gem5=name, fastsim=key, ticks=tick,
                            exact_core_cycles=tick / period,
                            ceil_core_cycles=(tick + period - 1) // period,
                            current_cycles=value,
                            current_minus_gem5_ticks=value * period - tick,
                            disabled=value == 0 and tick != 0))
    counts = [dict(gem5=n, fastsim=k, gem5_value=int(first[n]), current_value=fs[k])
              for n, k in COUNTS.items()]
    counts.extend([
        dict(gem5='MemCtrl count', fastsim='channels', gem5_value=len(names),
             current_value=fs['channels']),
        dict(gem5='device_rowbuffer_size * devices_per_rank', fastsim='row_bytes',
             gem5_value=int(first['device_rowbuffer_size']) * int(first['devices_per_rank']),
             current_value=fs['row_bytes']),
    ])
    return dict(schema='fastsim-gem5-dram-parameter-audit-v1',
                inputs=[fingerprint(ini_path), fingerprint(stats_path)],
                clock_period_ticks=period, timings=timings, counts=counts,
                gem5_address_mapping=first['addr_mapping'],
                gem5_page_policy=first['page_policy'],
                current_scheduler_runtime={k: v for k, v in stats['causal_frontier'].items()
                                           if 'dram_frfcfs' in k},
                gem5_channel_ranges=[ini[ini[n]['dram']]['range'] for n in names],
                parameters_without_independent_fastsim_fields={n: int(first[n]) for n in MISSING},
                limitations=['equal parameter names do not certify scheduling or queue lifetimes',
                             'ceil conversion is a conservative integer-cycle approximation; gem5 uses ticks'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gem5-config', type=Path, required=True)
    p.add_argument('--fastsim-stats', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--experimental-config', type=Path)
    p.add_argument('--base-config', type=Path)
    a = p.parse_args()
    if bool(a.experimental_config) != bool(a.base_config):
        p.error('experimental-config requires base-config and vice versa')
    result = audit(a.gem5_config, a.fastsim_stats)
    if a.experimental_config:
        # Isolate the disabled command calendar, preserving all already-active
        # latencies, topology, scheduler, Q and pipeline choices.
        overrides = {t['fastsim']: t['ceil_core_cycles']
                     for t in result['timings'] if t['disabled']}
        counts = {t['fastsim']: t for t in result['counts']}
        overrides['activation_limit'] = counts['activation_limit']['gem5_value']
        lines = ['# Diagnostic only: restore disabled gem5 command constraints.',
                 '# Not a claim of full gem5 controller equivalence.',
                 'config.include = ' + str(a.base_config.resolve()),
                 'sim.interval_max_cycles = 1024',
                 'core.response_pending_fill = false',
                 'core.response_pending_fill_load_admission = false',
                 'core.response_pending_fill_store_commit = false']
        lines.extend('dram.%s = %d' % item for item in overrides.items())
        a.experimental_config.write_text('\n'.join(lines) + '\n')
        result['experimental_overrides'] = overrides
        result['experimental_config'] = fingerprint(a.experimental_config)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({t['gem5']: [t['exact_core_cycles'], t['current_cycles']]
                      for t in result['timings']}))


if __name__ == '__main__':
    main()
