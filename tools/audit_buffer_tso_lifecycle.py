#!/usr/bin/env python3
"""Offline store-demand witnesses; never a complete prepare/TSO certification.

Native v7 records identify mandatory demands, not optional prepares. Timing
labels and native facts remain oracle-only and are never fed to FastSim.
"""
import argparse
from collections import Counter
import configparser
import hashlib
import json
from pathlib import Path

from audit_causal_prefix_reference import FLAG_FIELDS, read_asmap
from audit_tail_timing_inputs import HEADER, RECORD
from audit_tail_timing_pairs import core_pairs
from audit_tail_timing_pairs import selected_records
from audit_gem5_commit_gaps import load_native_facts
from audit_p1_native_response_sideband import audit_result, audit_core
from collect_tail_timing import sha256, write_json
from validate_fs_oracle_identity import validate_result_identity


def assert_audit_neutral(audit, control):
    for key in ('scope_metrics', 'threads'):
        a, b = audit[key], control[key]
        if key == 'scope_metrics':
            a = {k: v for k, v in a.items() if k != 'throughput'}
            b = {k: v for k, v in b.items() if k != 'throughput'}
        if a != b:
            raise ValueError('instrumentation changed target ' + key)
    if 'cores' in audit or 'cores' in control:
        if len(audit['cores']) != len(control['cores']):
            raise ValueError('instrumentation changed core count')
        for a, b in zip(audit['cores'], control['cores']):
            keys = [k for k in a if k in ('core', 'instructions', 'uops', 'cycles', 'memory_accesses')
                    or k.startswith(('buffer_tso_', 'l1d_', 'l1i_', 'l2_', 'o3_', 'ruby_sequencer_'))]
            if any(a[k] != b[k] for k in keys):
                raise ValueError('instrumentation changed core requests/queues/caches')


def raw_trace_path(trace, core, cores):
    paths = list(trace.glob('*switch%0*d.*records.micro.jsonl' % (len(str(cores - 1)), core)))
    if len(paths) != 1:
        raise ValueError('expected exactly one native functional trace for core %d' % core)
    return paths[0]


def validate_samples(samples):
    sequences = [s['sequence'] for s in samples]
    if not sequences or any(a >= b for a, b in zip(sequences, sequences[1:])):
        raise ValueError('audit samples must have unique increasing sequences')


def validate_stats_binding(path, original, validation_path=None):
    validation_path = validation_path or path.with_name('validation.json')
    validation = json.loads(validation_path.read_text())
    if validation['status'] != 'passed' or Path(validation['fastsim_output']).resolve() != path.resolve():
        raise ValueError('stats lack a matching passed maintained validation')
    if Path(validation['result_dir']).resolve() != original.resolve():
        raise ValueError('FastSim validation names a different source case')
    inputs = validation['validation_fingerprint']['inputs']
    expected = original / 'tao_trace/manifest.txt'
    if Path(inputs['manifest']['path']).resolve() != expected.resolve() or \
            inputs['manifest']['sha256'] != sha256(expected):
        raise ValueError('FastSim validation manifest does not match the paired source')
    archived = inputs.get('archived_fastsim_output')
    if archived is None or archived['sha256'] != sha256(path) or \
            Path(archived['path']).resolve() != path.resolve():
        raise ValueError('offline pairing requires maintained rescoring with the exact stats hash')
    return dict(validation=str(validation_path), validation_sha256=sha256(validation_path),
                stats_sha256=sha256(path), manifest_sha256=sha256(expected))


def validate_native_inputs(result):
    summary = audit_result(result)
    if not summary['native_hierarchy_semantic_comparable'] or not summary['target_drain_complete']:
        raise ValueError('native source/conservation/drain gate failed')
    document = json.loads((result / 'oracle/kernel_events.json').read_text())
    accounting = {row['core_id']: row['memory_accounting'] for row in document['per_core']}
    for row in summary['per_core']:
        core = row['core_id']
        parsed = audit_core(result / ('oracle/native-response-core%d.jsonl' % core),
                            core, accounting[core])
        if parsed['pending_without_response_uops'] or parsed['response_without_native_fact_uops']:
            raise ValueError('native JSONL did not drain')
        for key in ('committed_memory_uops', 'native_admission_fragments',
                    'native_hierarchy_request_fragments', 'native_response_fragments',
                    'native_ruby_pmu_by_scope'):
            if parsed[key] != row[key]:
                raise ValueError('native JSONL/summary mismatch: ' + key)
    return summary


def verify_store_identity(sample, record, fact, line_bytes):
    if fact is None or fact['thread_id'] != record['thread_id'] or fact['core_id'] != record['core_id']:
        raise ValueError('native thread/core identity differs from functional record')
    if sample['pc'] != record['macro_pc']:
        raise ValueError('FastSim store PC differs from functional record')
    first = record['paddr'] // line_bytes
    last = (record['paddr'] + record['size'] - 1) // line_bytes
    events = [e for e in sample['memory_events'] if e['write'] and not e['instruction_fetch']]
    if record['size'] <= 0 or sorted(e['line'] for e in events) != list(range(first, last + 1)):
        raise ValueError('FastSim store data lines differ from native functional record')


def verify_prefix(fst, raw_path, core, end):
    """Verify every ordinal from zero, including warmup, through the sample.

    This checks instruction/memory identity, not all dependency sidecars or
    runtime timing. Do not call it whole-model input equivalence.
    """
    digest = hashlib.sha256()
    with fst.open('rb') as source, raw_path.open() as raw:
        h = HEADER.unpack(source.read(HEADER.size))
        if h[:5] != (b'FSTRC01\0', 7, HEADER.size, RECORD.size, core) or not 0 < end <= h[5]:
            raise ValueError('FST prefix header/range mismatch')
        asmap = read_asmap(Path(str(fst) + '.asmap'), core, h[5])
        as_index = 0
        for ordinal in range(end):
            binary = source.read(RECORD.size)
            value = RECORD.unpack(binary)
            text = next(raw, None)
            if text is None:
                raise ValueError('native functional prefix truncated')
            row = json.loads(text)
            while as_index + 1 < len(asmap) and asmap[as_index + 1][0] <= ordinal:
                as_index += 1
            # Gem5's auxiliary syscall marker is not a committed micro-op;
            # FST encodes it as op_class=-1 plus serialize/retire flags.
            # Validate its identity explicitly instead of shifting ordinals.
            if row.get('is_syscall') and 'micro_seq' not in row:
                if (value[0], value[1], value[8], value[9], value[10], core, asmap[as_index][1]) != (
                        row['macro_pc'], row['syscall_number'], 0, 8193, -1,
                        row['core_id'], row['address_space_id']):
                    raise ValueError('auxiliary syscall identity mismatch at ordinal %d' % ordinal)
                digest.update(binary)
                digest.update(text.encode())
                continue
            fields = dict(core_id=core, macro_pc=value[0], size=value[8],
                          address_space_id=asmap[as_index][1])
            for bit, name in FLAG_FIELDS.items():
                fields[name] = int(bool(value[9] & (1 << bit)))
            if value[9] & 14:
                fields['paddr'] = value[1]
            if value[9] & 16:
                fields.update(branch_target=value[2], branch_next_pc=value[3])
            mismatches = {k: [v, row.get(k)] for k, v in fields.items() if row.get(k) != v}
            if (value[10] < -1) != (row.get('cpl') != 3):
                mismatches['kernel_privilege'] = [value[10] < -1, row.get('cpl')]
            if mismatches:
                raise ValueError('functional prefix mismatch core={} ordinal={} fields={}'.format(
                    core, ordinal, mismatches))
            digest.update(binary)
            digest.update(text.encode())
    return dict(matched_records=end, core=core, paired_prefix_sha256=digest.hexdigest(),
                checked='PC, memory PA/size/type, branch identity, privilege, ASID; all prefix ordinals',
                dependency_sidecars_verified=False)


def store_observation(sample, pair, period):
    if period <= 0:
        raise ValueError('clock period must be positive')
    if not pair['is_store'] or pair['is_load'] or pair['is_atomic'] or not sample['regular_store']:
        return {'excluded': 'not-ordinary-store'}
    if pair['memory_event_scope'] != 'data-hierarchy':
        return {'excluded': 'outside-data-hierarchy'}
    native = pair.get('gem5_native')
    if not native or not native['response_timestamps_available']:
        return {'excluded': 'native-response-timestamps-unavailable'}
    if native['terminal_no_ruby'] or any(native[k] != 1 for k in (
            'line_requests', 'admission_count', 'response_count', 'hierarchy_request_count')) or \
            native['coalesced'] or native['aliased_admissions']:
        return {'excluded': 'not-one-unaliased-mandatory-demand'}
    first = native['native_first_admission_tick']
    last = native['native_last_admission_tick']
    response = native['native_last_response_tick']
    if not 0 <= first == last <= response or (response - first) % period:
        raise ValueError('native single-demand timing is not ordered/cycle-aligned')
    send, release = sample['store_send_cycle'], sample['store_sq_release_cycle']
    if send < sample['actual_retire_cycle'] or release != send + sample['store_service_cycles']:
        raise ValueError('FastSim store response/SQ release relation violated')
    role = sample['buffer_tso_role']
    if role not in (0, 1, 2, 3):
        raise ValueError('unknown buffered store role')
    if role == 2 and not send < sample['buffer_tso_permission_ready_cycle'] == release:
        raise ValueError('invalid in-flight takeover observation')
    if role == 3 and send < sample['buffer_tso_permission_ready_cycle']:
        raise ValueError('prepared head sent before permission estimate')
    duration = (response - first) // period
    return dict(
        buffer_tso_role=role, gem5_admission_to_response_cycles=duration,
        fastsim_send_to_sq_release_cycles=release - send,
        service_difference_cycles=release - send - duration,
        native_l1_hit=native['l1d'].get('hits') == 1 and native['l1d'].get('accesses') == 1,
        native_prepare=None, full_lifecycle_verified=False,
        origin_caveat='FastSim send vs native Sequencer acceptance; pre-acceptance delays not paired')


def protocol_latencies(path):
    config = configparser.ConfigParser(interpolation=None)
    config.read(path)
    fields = ('mandatory_queue_latency', 'request_latency', 'response_latency',
              'dataAccessLatency', 'tagAccessLatency')
    return {section: {key: config[section][key] for key in fields if key in config[section]}
            for section in config.sections()
            if 'ruby_system' in section and any(key in config[section] for key in fields)}


def audit_case(args, include_pairs=False):
    collection = json.loads((args.collection / 'collection.json').read_text())
    completion = json.loads((args.collection / 'completion.json').read_text())
    if completion['returncode'] != 0 or completion['timed_out']:
        raise ValueError('native collection did not complete')
    original = Path(collection['original'])
    native_config = args.collection / 'gem5/config.ini'
    # Validate through the maintained entry point using a view of the raw
    # collection. Check the view points at this collection before trusting it.
    if (args.native_result / 'config.ini').resolve() != native_config.resolve() or \
            (args.native_result / 'tao_trace').resolve() != (args.collection / 'trace').resolve() or \
            (args.native_result / 'oracle').resolve() != (args.collection / 'trace/oracle').resolve():
        raise ValueError('native identity view does not name this collection')
    identities = [validate_result_identity(path) for path in (original, args.native_result)]
    if not all(row['valid'] for row in identities):
        raise ValueError('source/native hardware identity gate failed: ' + str(identities))
    targets = [json.loads((path / 'effective-target.json').read_text())
               for path in (original, args.native_result)]
    geometry_equal = ({k: v for k, v in targets[0].items() if k != 'source'} ==
                      {k: v for k, v in targets[1].items() if k != 'source'})
    latencies = [protocol_latencies(path) for path in (original / 'config.ini', native_config)]
    if not geometry_equal or latencies[0] != latencies[1]:
        raise ValueError('source/native hardware or protocol latency differs')
    audit = json.loads(args.audit_stats.read_text())
    control = json.loads(args.control_stats.read_text())
    assert_audit_neutral(audit, control)
    bindings = [validate_stats_binding(path, original, validation) for path, validation in (
        (args.audit_stats, args.audit_validation), (args.control_stats, args.control_validation))]
    native_gate = validate_native_inputs(args.native_result)
    configuration = audit['configuration']
    target = targets[1]
    pipeline = target['core']['pipeline']
    if configuration['cores'] != target['core']['count'] or \
            configuration['tso_store'] != pipeline['tsoStore'] or \
            configuration['tso_store_buffer_size'] != pipeline['tsoStoreBufferSize'] or \
            configuration['needs_tso'] != pipeline['needsTSO'] or any(
                round(1e12 / configuration[key]) != target['clock']['clock_period_ticks']
                for key in ('reference_frequency_hz', 'core_frequency_hz')):
        raise ValueError('FastSim clock/TSO geometry differs from source')
    if tuple(configuration['l1d'][k] for k in ('size_bytes', 'associativity', 'line_size')) != \
            tuple(target['cache']['l1d'][k] for k in ('size_b', 'assoc', 'line_b')):
        raise ValueError('FastSim L1D geometry differs from source')
    manifest = original / 'tao_trace/manifest.txt'
    entries = [line.split() for line in manifest.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    trace = args.collection / 'trace'
    cpls = {r['core_id']: r for r in map(json.loads, (trace / 'oracle/cpl_class.jsonl').read_text().splitlines())}
    original_cpls = {r['core_id']: r for r in map(json.loads, (original / 'oracle/cpl_class.jsonl').read_text().splitlines())}
    results = []
    for entry in entries:
        core = int(entry[0])
        if args.core is not None and core not in args.core:
            continue
        samples = audit['cores'][core]['response_frontier_audit']
        validate_samples(samples)
        boundary = json.loads((trace / ('functional-boundary-core%d.json' % core)).read_text())
        if int(entry[6]) != boundary['warmup_records'] or int(entry[4]) != boundary['warmup_instructions'] or \
                cpls[core]['first_tick'] != original_cpls[core]['first_tick']:
            raise ValueError('native/source ROI or warmup boundary differs')
        if cpls[core]['clock_period_ticks'] != target['clock']['clock_period_ticks']:
            raise ValueError('native CPL clock differs from hardware config')
        end = max(s['sequence'] for s in samples) + 1
        if min(s['sequence'] for s in samples) < int(entry[6]) or end > boundary['total_records']:
            raise ValueError('sample outside native diagnostic measurement window')
        fst = Path(entry[2])
        if not fst.is_absolute():
            fst = manifest.parent / fst
        entry[2] = str(fst.resolve())
        raw = raw_trace_path(trace, core, len(audit['cores']))
        prefix = verify_prefix(fst, raw, core, end)
        native_path = trace / ('oracle/native-response-core%d.jsonl' % core)
        joined = core_pairs(core, entry, audit, trace, cpls[core], native_path)
        by_sequence = {s['sequence']: s for s in samples}
        if include_pairs:
            load_targets = {pair['record_ordinal'] for pair in joined['pairs']
                            if pair['is_load'] and not pair['is_store']
                            and not pair['is_atomic']
                            and pair['memory_event_scope'] == 'data-hierarchy'}
            load_records = selected_records(raw, load_targets) if load_targets else {}
            for sequence, record in load_records.items():
                line_bytes = target['cache']['l1d']['line_b']
                first = record['paddr'] // line_bytes
                last = (record['paddr'] + record['size'] - 1) // line_bytes
                events = [event for event in by_sequence[sequence]['memory_events']
                          if not event['instruction_fetch']]
                if record['size'] <= 0 or any(event['write'] for event in events) or \
                        sorted(event['line'] for event in events) != list(range(first, last + 1)):
                    raise ValueError('FastSim load fragments differ from functional record')
        ordinary = [p for p in joined['pairs'] if p['is_store'] and not p['is_load'] and not p['is_atomic']
                    and p['memory_event_scope'] == 'data-hierarchy']
        records = selected_records(raw, {p['record_ordinal'] for p in ordinary}) if ordinary else {}
        facts = load_native_facts(native_path, core, {p['inst_seq_num'] for p in ordinary}) if ordinary else {}
        rows, excluded = [], Counter()
        for pair in joined['pairs']:
            sample = by_sequence[pair['record_ordinal']]
            if pair['record_ordinal'] in records:
                verify_store_identity(sample, records[pair['record_ordinal']], facts.get(pair['inst_seq_num']),
                                      target['cache']['l1d']['line_b'])
            observation = store_observation(sample, pair, cpls[core]['clock_period_ticks'])
            if 'excluded' in observation:
                excluded[observation['excluded']] += 1
                continue
            rows.append(dict(pair, fastsim=sample, store_demand=observation))
        histogram = Counter((r['store_demand']['buffer_tso_role'],
                             r['store_demand']['native_l1_hit'],
                             r['store_demand']['gem5_admission_to_response_cycles'],
                             r['store_demand']['fastsim_send_to_sq_release_cycles']) for r in rows)
        results.append(dict(core=core, prefix=prefix, stores=len(rows), exclusions=dict(excluded),
                            native_sha256=sha256(native_path),
                            first_milestone=joined['pairs'][0] if joined['pairs'] else None,
                            last_milestone=joined['pairs'][-1] if joined['pairs'] else None,
                            worst_proven_negative_milestone=joined['worst_proven_negative_milestone'],
                            service_histogram=[dict(role=k[0], native_l1_hit=k[1], gem5_cycles=k[2],
                                                    fastsim_cycles=k[3], count=v)
                                               for k, v in sorted(histogram.items())], witnesses=rows))
        if include_pairs:
            results[-1]['paired_stages'] = joined
    if not results:
        raise ValueError('no selected cores')
    return dict(schema='fastsim-buffer-tso-lifecycle-audit-v1', case=collection['case'],
                status=('paired-demand-diagnostic' if any(c['stores'] for c in results)
                        else 'no-store-witnesses'), hardware_identity=identities,
                source_recollection_geometry_equal=geometry_equal, protocol_latencies=latencies[1],
                native_source_conservation_drain_verified=True, stats_bindings=bindings,
                native_gate=native_gate,
                instrumentation_target_metrics_equal=True, full_lifecycle_verified=False,
                gem5_binary_same_as_source=collection['same_gem5_binary'],
                provenance={str(p): sha256(p) for p in (args.audit_stats, args.control_stats, manifest,
                                                       args.collection / 'collection.json')},
                cores=results, limitations=[
                    'No native per-store prepare admission/response/merge identity.',
                    'Only selected cores/windows are paired; no whole-ROI causal attribution or CPI claim.',
                    'Identity prefix excludes dependency sidecars; binary difference remains diagnostic.',
                    'FastSim store_send and native Sequencer acceptance have distinct admission semantics.'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collection', required=True, type=Path)
    p.add_argument('--native-result', required=True, type=Path)
    p.add_argument('--audit-stats', required=True, type=Path)
    p.add_argument('--control-stats', required=True, type=Path)
    p.add_argument('--audit-validation', type=Path, help='maintained rescore report; default stats sibling validation.json')
    p.add_argument('--control-validation', type=Path, help='maintained rescore report; default stats sibling validation.json')
    p.add_argument('--core', type=int, action='append')
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    try:
        result = audit_case(args)
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        write_json(args.output, dict(schema='fastsim-buffer-tso-lifecycle-audit-v1',
                                    status='rejected', full_lifecycle_verified=False, reason=str(exc)))
        raise SystemExit(str(exc))
    write_json(args.output, result)
    print(json.dumps({k: result[k] for k in ('case', 'status', 'full_lifecycle_verified')}))
    for core in result['cores']:
        print(json.dumps({k: core[k] for k in ('core', 'stores', 'service_histogram', 'exclusions')}))


if __name__ == '__main__':
    main()
