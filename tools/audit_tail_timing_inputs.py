#!/usr/bin/env python3
"""Audit frozen tail input boundaries and oracle availability, without replay.

FST fingerprints cover an explicitly bounded prefix, not the whole file.
Aggregate native summaries do not qualify as matched lifecycle evidence.
"""

import argparse
import configparser
import hashlib
import itertools
import json
from pathlib import Path
import struct

from collect_tail_timing import sha256, write_json


CASES = [
    'dse-l1d64k8-c04-811.tealeaf_s',
    'dse-rob256-c08-811.tealeaf_s',
    'dse-baseline-c04-811.tealeaf_s',
    'dse-baseline-c08-811.tealeaf_s',
    'dse-llc32m-c04-811.tealeaf_s',
    'formal-16c-811.tealeaf_s',
]
HEADER = struct.Struct('<8sIIIIQQ4Q')
RECORD = struct.Struct('<QQQQ4IHHhBB4BI')


def prefix(path, skip, take):
    with path.open('rb') as source:
        header = HEADER.unpack(source.read(HEADER.size))
        if header[:4] != (b'FSTRC01\0', 7, HEADER.size, RECORD.size):
            raise ValueError('unsupported FST: ' + str(path))
        if header[5] < skip + take:
            raise ValueError('FST slice out of bounds: ' + str(path))
        source.seek(HEADER.size + skip * RECORD.size)
        data = source.read(take * RECORD.size)
        if len(data) != take * RECORD.size:
            raise ValueError('truncated FST: ' + str(path))
    values = RECORD.unpack(data[:RECORD.size])
    return {
        'file': str(path), 'file_bytes': path.stat().st_size,
        'header_records': header[5], 'header_core': header[4],
        'feature_flags': header[6], 'prefix_skip_records': skip,
        'prefix_take_records': take,
        'prefix_sha256': hashlib.sha256(data).hexdigest(),
        'first_pc': values[0], 'first_address': values[1],
    }


def audit(case, inventory_root):
    manifest = Path(case['manifest'])
    entries = [line.split() for line in manifest.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    original = Path(entries[0][2]).parent.parent
    trace = json.loads((original / 'tao_trace/trace.json').read_text())
    profile = json.loads((original / 'tao_trace/uarch_profile.json').read_text())
    request = json.loads((original / 'request.json').read_text())
    stats_path = inventory_root / 'runs' / case['case'] / 'current/stats.json'
    stats = json.loads(stats_path.read_text())
    classes = {row['core_id']: row for row in
               map(json.loads, (original / 'oracle/cpl_class.jsonl').read_text().splitlines())}
    errors = []
    cores = []
    for e in entries:
        core = int(e[0])
        if len(e) != 8 or e[1] != 'fastsim-binary-warmup-slice':
            raise ValueError('expected explicit warmup slice: ' + str(manifest))
        warm_i, measure_i, warm_u, measure_u = map(int, e[4:])
        boundary = trace['functional_boundaries'][str(core)]
        for key, expected in [('warmup_instructions', warm_i),
                              ('measurement_instructions', measure_i),
                              ('warmup_records', warm_u),
                              ('measurement_records', measure_u)]:
            if boundary[key] != expected:
                errors.append('core {} {} mismatch'.format(core, key))
        cpl = classes[core]
        period = cpl['clock_period_ticks']
        # Native trace omits idle UOPs. Its inclusive cycle target excludes
        # idle time but includes syscall, IRQ and other traced kernel classes.
        cycles = (cpl['measured_ticks'] - cpl['idle_ticks']) // period
        if (cpl['measured_ticks'] - cpl['idle_ticks']) % period:
            errors.append('core {} cycle fraction'.format(core))
        if cpl['user_functional_uops'] != boundary['measurement_user_records']:
            errors.append('core {} user denominator mismatch'.format(core))
        thread = next(t for t in stats['threads'] if t['thread'] == int(e[3]))
        for field, expected in [('records', measure_u), ('instructions', measure_i)]:
            if thread[field] != expected:
                errors.append('core {} FastSim {} mismatch'.format(core, field))
        fp = prefix(Path(e[2]), warm_u, min(4096, measure_u))
        if fp['header_core'] != core or not fp['feature_flags'] & 16:
            errors.append('core {} identity/privilege feature mismatch'.format(core))
        summary = json.loads((original / 'oracle' / ('native-summary-core%d.json' % core)).read_text())
        cores.append(dict(fp, core=core, logical_thread=int(e[3]),
                          warmup_uops=warm_u, warmup_instructions=warm_i,
                          measurement_uops=measure_u, measurement_instructions=measure_i,
                          user_uops=boundary['measurement_user_records'],
                          roi_first_tick=cpl['first_tick'], roi_last_tick=cpl['last_tick'],
                          ticks_per_cycle=period, gem5_traced_cycles=cycles,
                          fastsim_cycles=stats['cores'][core]['cycles'],
                          native_jsonl_enabled=summary['full_jsonl_enabled']))
    denominator = sum(c['user_uops'] if case['metric'] == 'cycles_per_user_uop'
                      else c['measurement_instructions'] for c in cores)
    reference = sum(c['gem5_traced_cycles'] for c in cores) / denominator
    if abs(reference - case['reference']) > 1e-10:
        errors.append('CPL-derived reference differs from frozen reference')
    cfg = stats['configuration']
    if cfg['interval_max_cycles'] != 1024:
        errors.append('Q is not 1024')
    if cfg['cores'] != len(cores) or trace['trace_scope'] != 'user-plus-kernel':
        errors.append('core count or scope mismatch')
    config_sha = sha256(original / 'config.ini')
    if config_sha != profile['source_config_sha256']:
        errors.append('uarch profile does not hash to source config')
    for name, gem5_name in [('l1d', 'l1d'), ('l2', 'l2'), ('llc', 'l3')]:
        if cfg[name]['size_bytes'] != profile['cache'][gem5_name]['size_b']:
            errors.append(name + ' capacity mismatch')
    gem5_config = configparser.ConfigParser(strict=False, interpolation=None)
    gem5_config.read(str(original / 'config.ini'))
    rob_sizes = [gem5_config.getint(s, 'numROBEntries') for s in gem5_config.sections()
                 if gem5_config.has_option(s, 'numROBEntries')]
    if len(rob_sizes) != len(cores) or any(n != cfg['rob_entries'] for n in rob_sizes):
        errors.append('ROB capacity mismatch')
    labels = sorted(str(p) for root in [original, original / 'tao_trace', original / 'oracle']
                    for p in root.glob('*.labels.micro.jsonl'))
    native = sorted(str(p) for p in (original / 'oracle').glob('native-response-core*.jsonl'))
    return {
        'case': case['case'], 'metric': case['metric'], 'reference': reference,
        'current_cpi': stats['scope_metrics'][case['metric']],
        'signed_error_percent': (stats['scope_metrics'][case['metric']] / reference - 1) * 100,
        'input_checks_pass': not errors, 'errors': errors, 'cores': cores,
        'manifest_sha256': sha256(manifest), 'config_ini_sha256': config_sha,
        'frozen_stats_sha256': sha256(stats_path), 'original': str(original),
        'gem5_binary_sha256': request['gem5']['binary_sha256'],
        'gem5_frequency_hz': profile['core']['freq_ghz'] * 1e9,
        'fastsim_frequency_hz': cfg['core_frequency_hz'],
        'stage_labels': labels, 'native_lifecycle_labels': native,
        'paired_event_evidence_ready': False,
        'missing_evidence': 'stage/lifecycle coverage and event identities must be validated',
        'thread_mapping_limit': 'manifest logical core/thread; guest OS TID unavailable',
        'clock_note': 'compare target cycles; derive label periods per core, no hardcoded 333',
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    inventory = json.loads(args.inventory.read_text())
    cases = [audit(inventory[k], args.inventory.parent) for k in CASES]
    comparisons = []
    for a, b in itertools.combinations(cases, 2):
        if len(a['cores']) != len(b['cores']):
            continue
        comparisons.append({
            'a': a['case'], 'b': b['case'],
            'identical_4096_record_measurement_prefix_cores': [
                x['core'] for x, y in zip(a['cores'], b['cores'])
                if x['prefix_sha256'] == y['prefix_sha256']],
            'same_warmup_records': [x['warmup_uops'] for x in a['cores']] ==
                                   [x['warmup_uops'] for x in b['cores']],
            'parameter_only_comparison_proven': False,
        })
    result = {'schema': 'fastsim-tail-timing-input-audit-v1', 'cases': cases,
              'cross_config_prefix_checks': comparisons,
              'all_input_checks_pass': all(c['input_checks_pass'] for c in cases)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    for c in cases:
        print(c['case'], 'PASS' if c['input_checks_pass'] else c['errors'],
              'error={:.4f}%'.format(c['signed_error_percent']),
              'stages={}'.format(len(c['stage_labels'])),
              'responses={}'.format(len(c['native_lifecycle_labels'])))
    if not result['all_input_checks_pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
