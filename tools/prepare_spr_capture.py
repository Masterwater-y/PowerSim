#!/usr/bin/env python3
"""Prepare a completed native SPR capture without changing captured evidence.

Only derived input/identity artifacts are installed. Existing different outputs
are rejected. This adapter does not run FastSim, select native cache references,
aggregate CPI, or certify accuracy. Run the maintained native validator separately.
Only all-traced O3 topologies without extra_cores are supported. Build the native
functional auditor first. Its shared-library environment is inherited from the
caller (for this workspace: LD_LIBRARY_PATH=/opt/gcc-11/lib64).
"""
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import mmap
import runpy
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from audit_fst_syscall_metadata import audit_file
from common_end_capture import POLICY, validate_common_end
from audit_fst_static_instruction_maps import audit_map, FEATURE_ASID_STATIC_MAP

ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct('<8sIIIIQQ4Q')
DEPS = struct.Struct('<8sIIIIQQQ')
REQUEST_SCHEMA = 'fastsim-spr-collection-request-v1'
# Native functional capabilities; bit 3 is conditional syscall metadata.
NATIVE_REQUIRED_FEATURES = 0xff5
NATIVE_COMPANIONS = ('.deps', '.mreq', '.fsem', '.xlat.gz', '.ffe.gz', '.imap', '.vmap', '.asmap')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def sha256(path):
    digest = hashlib.sha256()
    before = Path(path).stat()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(8 << 20), b''):
            digest.update(block)
    after = Path(path).stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            f'capture changed while hashing: {path}')
    return digest.hexdigest()


def participant_rows(directory, pattern, cores, label):
    rows = [load(path) for path in sorted(directory.glob(pattern))]
    ids = [row.get('core_id') for row in rows]
    require(len(ids) == cores and all(type(value) is int for value in ids)
            and sorted(ids) == list(range(cores)), f'unexpected {label} participants: {ids}')
    return sorted(rows, key=lambda row: row['core_id'])


def optional_evidence(trace, prefix, cores, sources, boundaries, requested):
    if not requested:
        return dict(status='not-requested')
    metas = participant_rows(trace, prefix + '-metadata-core*.json', cores, prefix)
    found = {path.name for path in trace.glob(prefix + '-core*.jsonl')}
    require(found == {f'{prefix}-core{core}.jsonl' for core in range(cores)},
            f'{prefix} event participants differ from request')
    event_count = 0
    for core, meta in enumerate(metas):
        require(meta.get('source_file') == sources[core].name and
                meta.get('source_record_count') == boundaries[core]['total_records'],
                f'{prefix} core {core}: source/population mismatch')
        rows = [json.loads(line) for line in (trace / f'{prefix}-core{core}.jsonl').read_text().splitlines() if line.strip()]
        require(type(meta.get('event_count')) is int and meta['event_count'] == len(rows),
                f'{prefix} core {core}: event count mismatch')
        event_count += len(rows)
    return dict(status='pending' if event_count else 'unavailable', events=event_count,
                reason=None if event_count else 'no captured events')


def validate_slice_macro_counts(fst, boundary):
    """Match WarmupInstructionTraceSource::completes_instruction exactly.

    Manifest counts include every retiring macro completion, including hardware
    entry members and syscall instructions; scoring eligibility is separate.
    Partial common-end macros remain uncounted without trimming their UOPs.
    """
    require(boundary['measurement_instructions'] > 0,
            'functional measurement instruction count must be greater than zero')
    warmup_end = 72 + 64 * boundary['warmup_records']
    hot_end = 72 + 64 * boundary['total_records']
    observed = []
    with fst.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        for begin, end in ((72, warmup_end), (warmup_end, hot_end)):
            view = memoryview(mapped)[begin:end]
            try:
                observed.append(sum(bool(flags & 1 and (not flags & 1024 or flags & 2048))
                                    for (flags,) in struct.iter_unpack('<50xH12x', view)))
            finally:
                view.release()
    require(observed == [boundary['warmup_instructions'], boundary['measurement_instructions']],
            f'core {boundary["core_id"]}: functional warmup/measurement record/instruction count mismatch: '
            f'observed {observed}, declared {[boundary["warmup_instructions"], boundary["measurement_instructions"]]}')


def data_translation_qualification(audit, cores, collector_source_root, requested):
    """Data provenance transport does not qualify a data-TLB timing model."""
    facts = audit.get('data_translation_facts', {})
    declared = facts.get('files_declared', 0)
    if not requested:
        require(facts.get('wire_version_3_files', 0) == 0,
                'data translation origins requested/declaration mismatch')
        return dict(status='legacy' if declared else 'not-declared',
                    origin_context_binding='unavailable', logical_mmu_controls_complete=False,
                    full_spr_profile_qualified=False)
    require(declared == cores and facts.get('wire_version_3_files') == cores and
            facts.get('wire_version_1_files') == facts.get('wire_version_2_files') == 0,
            'data translation origins requested/declaration mismatch')
    fields = ('memory_records', 'memory_bytes', 'byte_mapping_records', 'observed_path_records',
              'leaf_only_records', 'unknown_mapping_records', 'byte_mapping_bytes', 'observed_path_bytes',
              'leaf_only_bytes', 'unknown_mapping_bytes', 'special_request_bytes',
              'origin_retirement_asid_mismatch_records', 'mixed_origin_asid_records', 'historical_walk_root_records')
    require(all(type(facts.get(field)) is int and facts[field] >= 0 for field in fields),
            'invalid data translation populations')
    require(facts['observed_path_records'] + facts['leaf_only_records'] == facts['byte_mapping_records'] and
            facts['byte_mapping_records'] + facts['unknown_mapping_records'] == facts['memory_records'] and
            facts['observed_path_bytes'] + facts['leaf_only_bytes'] == facts['byte_mapping_bytes'] and
            facts['byte_mapping_bytes'] + facts['unknown_mapping_bytes'] == facts['memory_bytes'] and
            facts['special_request_bytes'] <= facts['byte_mapping_bytes'] and
            all(facts[field] <= facts['memory_records'] for field in fields[-3:]),
            'data translation populations do not conserve')
    require(collector_source_root is not None, 'data translation source policy root missing')
    source_root = Path(collector_source_root).resolve()
    module = runpy.run_path(str(source_root / 'src/cpu/o3/probe/generate_static_uop_policy.py'))
    policy = module['policy_digest'](source_root).hex()
    require(audit.get('static_uop_facts', {}).get('policy_identities') == [policy],
            'data translation compiled policy differs from selected collector source')
    return dict(status='transport-validated', schema='fst-dynamic-translations-v3',
        source_root=str(source_root), source_policy_sha256=policy,
        origin_context_binding='explicit-retirement-asid', token_namespace='retirement-asid-va4k-pa4k',
        byte_mapping_status='incomplete' if facts['unknown_mapping_bytes'] else 'complete',
        actual_path_status='incomplete' if facts['unknown_mapping_bytes'] or facts['leaf_only_bytes'] else 'complete',
        populations={field: facts[field] for field in fields},
        logical_mmu_controls_complete=False, full_spr_profile_qualified=False)


def instruction_translation_qualification(audit, cores, collector_source_root, requested):
    """Actual functional byte/path coverage is independent of MMU control support."""
    facts = audit.get('instruction_translation_facts', {})
    declared = facts.get('files_declared', 0)
    if not requested and not declared:
        return dict(status='not-declared', retained_macro_status='unavailable',
                    strict_full_macro_eligible=False, logical_mmu_controls_complete=False,
                    full_spr_profile_qualified=False)
    require(requested and declared == cores,
            'instruction translation requested/declaration population mismatch')
    require(facts.get('schema') in ('fst-instruction-translations-v1', 'fst-instruction-translations-v2'),
            'unsupported instruction translation schema')
    fields = ('macro_spans', 'observed_path_spans', 'leaf_only_spans', 'unknown_spans',
              'pseudo_not_applicable_spans', 'partial_macro_spans')
    require(all(type(facts.get(field)) is int and facts[field] >= 0 for field in fields),
            'invalid instruction translation populations')
    require(sum(facts[field] for field in fields[1:5]) == facts['macro_spans'] and
            facts['partial_macro_spans'] <= facts['macro_spans'],
            'instruction translation span populations do not conserve')
    context_fields = ('decode_context_known_spans', 'decode_context_unknown_spans',
                      'origin_decode_asid_mismatch_spans', 'origin_retirement_asid_mismatch_spans',
                      'decode_retirement_asid_mismatch_spans', 'context_crossing_spans',
                      'mixed_origin_asid_spans', 'historical_walk_root_spans')
    ordinary_spans = facts['macro_spans'] - facts['pseudo_not_applicable_spans']
    mapped_spans = facts['observed_path_spans'] + facts['leaf_only_spans']
    if facts['schema'] == 'fst-instruction-translations-v1':
        # V1 never serialized decoder ownership context. It cannot be recovered
        # from a matching retirement ASID or from a surviving global walk root.
        contexts = dict.fromkeys(context_fields, 0)
        contexts['decode_context_unknown_spans'] = ordinary_spans
    else:
        require(all(type(facts.get(field)) is int and facts[field] >= 0 for field in context_fields),
                'invalid instruction translation context populations')
        contexts = {field: facts[field] for field in context_fields}
        require(all(type(facts.get(field)) is int and facts[field] >= 0
                    for field in ('wire_version_1_files', 'wire_version_2_files')) and
                facts['wire_version_1_files'] + facts['wire_version_2_files'] == declared,
                'instruction translation wire version population mismatch')
        require(contexts['decode_context_known_spans'] + contexts['decode_context_unknown_spans'] == ordinary_spans,
                'instruction translation owner-context population mismatch')
        mismatch_counts = [contexts[field] for field in context_fields[2:5]]
        require(max(mismatch_counts) <= contexts['context_crossing_spans'] <= sum(mismatch_counts) and
                contexts['context_crossing_spans'] <= ordinary_spans and
                contexts['origin_decode_asid_mismatch_spans'] <= min(mapped_spans, contexts['decode_context_known_spans']) and
                contexts['origin_retirement_asid_mismatch_spans'] <= mapped_spans and
                contexts['decode_retirement_asid_mismatch_spans'] <= contexts['decode_context_known_spans'] and
                contexts['mixed_origin_asid_spans'] <= min(mapped_spans, contexts['context_crossing_spans']) and
                contexts['historical_walk_root_spans'] <= mapped_spans,
                'instruction translation context populations do not conserve')
    if contexts['context_crossing_spans']:
        context_alignment = 'cross-context'
    elif contexts['decode_context_unknown_spans'] or facts['unknown_spans']:
        context_alignment = 'unavailable'
    else:
        context_alignment = 'aligned'
    require(collector_source_root is not None, 'instruction translation source policy root missing')
    source_root = Path(collector_source_root).resolve()
    module = runpy.run_path(str(source_root / 'src/cpu/o3/probe/generate_static_uop_policy.py'))
    policy = module['policy_digest'](source_root).hex()
    require(audit.get('static_uop_facts', {}).get('policy_identities') == [policy],
            'instruction translation compiled policy differs from selected collector source')
    # Common-end may retain only a macro prefix while its frozen decode-time
    # witness still proves every instruction byte. Keep these evidence axes separate.
    byte_complete = not facts['unknown_spans']
    path_complete = byte_complete and not facts['leaf_only_spans']
    retained_macro_complete = not facts['partial_macro_spans']
    return dict(status='transport-validated', schema=facts['schema'],
        source_root=str(source_root), source_policy_sha256=policy,
        byte_mapping_status='complete' if byte_complete else 'incomplete',
        actual_path_status='complete' if path_complete else 'incomplete',
        retained_macro_status='complete' if retained_macro_complete else 'incomplete',
        # This is only full-macro instruction-input eligibility, not model readiness.
        strict_full_macro_eligible=path_complete and retained_macro_complete and context_alignment == 'aligned',
        observed_asid_alignment_status=context_alignment, context_populations=contexts,
        logical_mmu_controls_complete=False, full_spr_profile_qualified=False,
        populations={field: facts[field] for field in fields})


def inspect_capture(source, workload_label=None, fastsim_root=ROOT, collector_source_root=None):
    """Validate original artifacts before creating any derived files."""
    source = Path(source).resolve()
    trace, oracle = source / 'tao_trace', source / 'tao_trace/oracle'
    request = load(source / 'sync-run-request.json')
    require(request.get('schema') == REQUEST_SCHEMA, 'unsupported native SPR request schema')
    args = request.get('arguments', {})
    require(type(args.get('omp_threads')) is int and args['omp_threads'] > 0 and
            type(args.get('extra_cores', 0)) is int and args.get('extra_cores', 0) >= 0,
            'native request core counts must be positive/nonnegative integers')
    for flag in ('functional_sync', 'thread_state', 'functional_asid_static_map', 'task_clock', 'functional_instruction_translations', 'functional_translation_origins'):
        require(type(args.get(flag, False)) is bool, f'native request {flag} must be boolean')
    require(not args.get('task_clock') or args.get('thread_state'),
            'task clock requires thread-state capture')
    require(args.get('extra_cores', 0) == 0,
            'unsupported untraced spares: extra_cores must be zero until hardware/source topology identities are separate')
    cores = args['omp_threads']
    require(type(cores) is int and cores > 0, 'invalid requested core count')
    participants = request.get('participant_core_ids')
    require(isinstance(participants, list) and all(type(core) is int for core in participants)
            and participants == list(range(cores)), 'native request participants must be exactly 0..N-1')
    require(args.get('trace_format') == 'fst' and args.get('trace_subdir', 'tao_trace') == 'tao_trace',
            'native SPR preparation requires tao_trace FST output')
    require(args.get('functional_include_kernel') is True and args.get('functional_syscall_microops') is True,
            'native request requires kernel stream and raw syscall micro-ops')
    target = args.get('functional_user_target')
    require(type(target) is int and target > 0, 'invalid measured-user-UOP target')
    result = load(source / 'sync-run-result.json')
    require(result.get('common_end') is True and result.get('measurement_open') is True,
            'capture has not completed its common-end measurement')
    binary = Path(args['workload_binary']).resolve()
    require(binary.is_file(), f'captured workload binary unavailable: {binary}')
    require(request.get('binary_identity', {}).get('binary_sha256') == sha256(binary),
            'workload binary hash differs from native request')
    boundary_rows = participant_rows(trace, 'functional-boundary-core*.json', cores, 'functional boundary')
    cpi = participant_rows(oracle, 'cpi-core*.json', cores, 'CPI')
    cpl = [json.loads(line) for line in (oracle / 'cpl_class.jsonl').read_text().splitlines() if line.strip()]
    gate = validate_common_end(boundary_rows, cpi, cpl, cores=cores, target=target)
    sources, per_core = {}, {}
    for fst in sorted(trace.glob('*.fst')):
        require(not any(char.isspace() for char in fst.name), 'FST filename cannot contain manifest whitespace')
        with fst.open('rb') as stream:
            raw = stream.read(HEADER.size)
        require(len(raw) == HEADER.size, f'truncated FST header: {fst}')
        header = HEADER.unpack(raw)
        core, records, features = header[4:7]
        require(core not in sources and core in range(cores), 'unexpected or duplicate FST participant')
        require(features & NATIVE_REQUIRED_FEATURES == NATIVE_REQUIRED_FEATURES and not features & 2,
                f'core {core}: missing native functional frontend/retirement/translation/request/dependency capabilities')
        if args.get('functional_asid_static_map'):
            require(features & FEATURE_ASID_STATIC_MAP,
                    f'core {core}: missing requested ASID static map capability')
        companions=NATIVE_COMPANIONS + (('.sreq',) if features & (1 << 13) else ()) + (('.uops.gz',) if features & (1 << 14) else ()) + (('.ixlat.gz',) if features & (1 << 15) else ())
        require(bool(features & (1 << 15)) == args.get('functional_instruction_translations', False),
                f'core {core}: instruction translation request/feature mismatch')
        for suffix in companions:
            require(Path(str(fst) + suffix).is_file(), f'core {core}: missing functional companion {suffix}')
        with gzip.open(str(fst) + '.xlat.gz', 'rb') as stream:
            xlat_header = stream.read(32)
        require(len(xlat_header) == 32, f'core {core}: truncated dynamic translation header')
        xlat_magic, xlat_version, xlat_size, xlat_core, xlat_flags, xlat_reserved = struct.unpack('<8sIIIIQ', xlat_header)
        require(xlat_magic == b'FSTXLT1\0' and xlat_version in (1, 2, 3) and
                xlat_size == (64 if xlat_version == 3 else 32) and xlat_core == core and
                xlat_flags == 1 and not xlat_reserved, f'core {core}: invalid dynamic translation header')
        require((xlat_version == 3) == args.get('functional_translation_origins', False),
                f'core {core}: data translation origins request/version mismatch')
        audited = audit_file(fst)
        boundary = boundary_rows[core]
        require(audited['records'] == records == boundary['total_records'] and
                boundary.get('trace_scope') == 'user-plus-kernel', f'core {core}: FST/boundary scope or population mismatch')
        for field in ('warmup_records', 'measurement_records', 'warmup_instructions', 'measurement_instructions'):
            require(type(boundary.get(field)) is int and boundary[field] >= 0,
                    f'core {core}: invalid boundary {field}')
        require(boundary['measurement_records'] > 0, f'core {core}: empty measurement')
        validate_slice_macro_counts(fst, boundary)
        deps = Path(str(fst) + '.deps')
        with deps.open('rb') as stream:
            raw_dep = stream.read(DEPS.size)
        require(len(raw_dep) == DEPS.size, f'core {core}: truncated dependency companion')
        dep = DEPS.unpack(raw_dep)
        require(dep[:6] == (b'FSTDEP1\0', 1, 48, core, 0, records) and
                deps.stat().st_size == 48 + dep[6] * 16 + dep[7] * 4,
                f'core {core}: invalid dependency companion')
        sources[core] = fst
        per_core[str(core)] = dict(boundary, fst=str(fst), records=records,
            fst_version=header[1], fst_features=features, complete_dependencies=True,
            functional_memory_facts_declared=bool(features & (1 << 13)),
            static_uop_facts_declared=bool(features & (1 << 14)),
            instruction_translations_declared=bool(features & (1 << 15)),
            dynamic_translation_version=xlat_version,
            functional_translation_origins=xlat_version == 3,
            syscall_metadata_count=header[8], source_file=fst.name)
    require(set(sources) == set(range(cores)), 'missing captured FST participants')
    # One full pass through the maintained native BinaryTraceSource checks
    # sparse rows and their hot-record bindings, full-stream mreq/fsem/xlat/FFE
    # hashes, dependency semantics, and retained frontend facts. Header checks
    # and hashing a corrupt companion never establish functional readiness.
    auditor = Path(fastsim_root).resolve() / 'build/fastsim_audit_retired_frontend'
    require(auditor.is_file(), f'missing maintained functional auditor: {auditor}; build the audit target first')
    audited = subprocess.run([str(auditor), *(str(sources[core]) for core in range(cores))],
                             capture_output=True, text=True)
    require(audited.returncode == 0,
            f'functional auditor failed: {audited.stderr.strip() or audited.stdout.strip()}')
    functional_audit = json.loads(audited.stdout)
    require(functional_audit.get('status') == 'passed' and functional_audit.get('files') == cores and
            functional_audit.get('records') == sum(row['total_records'] for row in boundary_rows),
            'functional auditor result does not match captured population')
    functional_audit['instruction_translation_qualification'] = instruction_translation_qualification(
        functional_audit, cores, collector_source_root or Path(fastsim_root) / 'gem5-spr',
        args.get('functional_instruction_translations', False))
    functional_audit['data_translation_qualification'] = data_translation_qualification(
        functional_audit, cores, collector_source_root or Path(fastsim_root) / 'gem5-spr',
        args.get('functional_translation_origins', False))
    functional_audit.update(auditor=str(auditor), auditor_sha256=sha256(auditor))
    for fst in sources.values():
        audit_map(fst)
    thread = optional_evidence(trace, 'thread-state', cores, sources, boundary_rows, args.get('thread_state') is True)
    sync = optional_evidence(trace, 'functional-sync', cores, sources, boundary_rows, args.get('functional_sync') is True)
    label = workload_label or binary.name
    require(bool(label.strip()), 'empty workload label')
    derived_request = dict(request, profile=dict(cores=cores),
        workload_selection=dict(workload=label, workload_args=args.get('workload_args', '')),
        sampling=dict(functional_include_kernel=True, functional_user_only=False,
            functional_syscall_microops=True, functional_warmup=True,
            warmup_insts=args.get('warmup_insts'), roi_insts=target,
            count_unit='user-uop', measurement_policy=POLICY))
    metadata = dict(schema='tcsim-gem5-fs-functional-trace-v1', cores=cores,
        converter=dict(path='native-spr-producer-fst7'), trace_scope='user-plus-kernel',
        functional_warmup_enabled=True, functional_syscall_microops=True,
        functional_translation_origins=args.get('functional_translation_origins', False),
        measurement_policy=POLICY, common_end=gate, per_core=per_core,
        functional_boundaries={str(core): row for core, row in enumerate(boundary_rows)})
    manifest = ''.join(f'{core} binary-warmup-slice {sources[core].name} {core} '
                       f'{row["warmup_instructions"]} {row["measurement_instructions"]} '
                       f'{row["warmup_records"]} {row["measurement_records"]}\n'
                       for core, row in enumerate(boundary_rows))
    # Hash every original FST companion, including extensions unknown to this
    # adapter. The original producer profile remains an independent artifact.
    excluded = {'manifest.txt', 'trace.json', 'uarch_profile.json', 'kernel_events.json',
                'thread-state-accounting.json'}
    evidence = {path for path in trace.rglob('*') if path.is_file() and path.name not in excluded}
    evidence.update(source / name for name in ('config.ini', 'sync-run-request.json', 'sync-run-result.json'))
    evidence.add(binary)
    if (source / 'uarch_profile.json').is_file():
        evidence.add(source / 'uarch_profile.json')
    hashes = {str(path.resolve()): dict(bytes=path.stat().st_size, sha256=sha256(path))
              for path in sorted(evidence)}
    return derived_request, metadata, manifest, gate, thread, sync, hashes, functional_audit


def prepare(source, *, workload_label=None, fastsim_root=ROOT, collector_source_root=None):
    source, fastsim_root = Path(source).resolve(), Path(fastsim_root).resolve()
    request, metadata, manifest, gate, thread, sync, hashes, functional_audit = inspect_capture(source, workload_label, fastsim_root, collector_source_root)
    oracle_link = source / 'oracle'
    require((not oracle_link.exists() and not oracle_link.is_symlink()) or
            (oracle_link.is_symlink() and oracle_link.resolve() == source / 'tao_trace/oracle'),
            'conflicting existing output: oracle must link to tao_trace/oracle')
    tools = fastsim_root / 'tools'
    shared = fastsim_root / 'gem5-spr/taogen/shared'
    commands = []
    with tempfile.TemporaryDirectory(prefix='.spr-prepare-', dir=source) as temporary:
        stage = Path(temporary)
        (stage / 'config.ini').symlink_to(source / 'config.ini')
        dump(stage / 'request.json', request)
        dump(stage / 'tao_trace/trace.json', metadata)
        (stage / 'tao_trace/manifest.txt').write_text(manifest)
        dump(stage / 'common-end-audit.json', gate)

        def run(name, arguments, *, native_gate=False):
            require((tools / name).is_file(), f'missing maintained tool: {tools / name}')
            command = [sys.executable, str(tools / name), *map(str, arguments)]
            completed = subprocess.run(command, capture_output=True, text=True)
            commands.append(dict(tool=name, returncode=completed.returncode,
                                 options=[str(arg) for arg in arguments if str(arg).startswith('--')]))
            error = f'{name} failed: {completed.stderr.strip() or completed.stdout.strip()}'
            if native_gate:
                return dict(available=completed.returncode == 0,
                            error=None if completed.returncode == 0 else error,
                            scope='maintained kernel-event merge; native cache hierarchy not certified')
            require(completed.returncode == 0, error)

        try:
            kernel = participant_rows(source / 'tao_trace/oracle', 'kernel-events-core*.json', metadata['cores'], 'native kernel event')
            require(all(row.get('n_user') == metadata['functional_boundaries'][str(core)]['measurement_user_records']
                        for core, row in enumerate(kernel)), 'native kernel-event/CPI user population mismatch')
        except (OSError, ValueError, KeyError, TypeError) as error:
            native_evidence = dict(available=False, error=str(error),
                                   scope='native kernel-event participants/population; functional source is separate')
        else:
            native_evidence = run('merge_kernel_events_oracle_v3.py',
                [source / 'tao_trace/oracle', '--output', stage / 'kernel_events.json'], native_gate=True)
        run('generate_fs_effective_target.py', ['--config', source / 'config.ini',
            '--uarch-profile', stage / 'tao_trace/uarch_profile.json',
            '--effective-target', stage / 'effective-target.json', '--taotrace-shared-root', shared,
            '--event-dictionary', fastsim_root / 'configs/pmu-event-dictionary-v1.json'])
        require(load(stage / 'effective-target.json')['core']['count'] == metadata['cores'],
                'hardware/source core count mismatch: native topology must match traced participants')
        run('validate_fs_oracle_identity.py', ['--result', stage, '--output', stage / 'identity-validation.json',
            '--event-dictionary', fastsim_root / 'configs/pmu-event-dictionary-v1.json'])
        require(load(stage / 'identity-validation.json').get('valid') is True,
                'independent final-config target identity failed')
        if thread['status'] != 'not-requested':
            require(thread['status'] != 'unavailable', 'thread-state requested but no captured events')
            run('compile_thread_state.py', ['--trace-dir', source / 'tao_trace', '--output', stage / 'thread-state.plan',
                '--summary', stage / 'thread-state-summary.json', '--require-sources'])
            thread = dict(thread, status='available', plan='thread-state.plan')
        task_clock = dict(status='not-requested')
        if request['arguments'].get('task_clock'):
            contract = 'task-lifecycle-basecpu-active-v1'
            require(load(stage / 'thread-state-summary.json').get('task_clock_contract') == contract,
                    'requested task clock lacks functional lifecycle contract')
            clock_gate = run('audit_thread_state_accounting.py',
                ['--trace-dir', source / 'tao_trace', '--output',
                 stage / 'tao_trace/oracle/thread-state-accounting.json'], native_gate=True)
            if clock_gate['available']:
                audited_clock = load(stage / 'tao_trace/oracle/thread-state-accounting.json')
                task_clock = dict(status=audited_clock.get('active_status', 'unavailable'),
                    accounting_contract=audited_clock.get('accounting_contract'),
                    counter_domain=audited_clock.get('counter_domain'),
                    reason=audited_clock.get('active_reason') or audited_clock.get('limitations'),
                    scope='independently audited native task lifecycle counter; hardware perf unverified')
            else:
                task_clock = dict(status='unavailable', reason=clock_gate['error'],
                    scope='native task lifecycle counter evidence unavailable')
        if sync['status'] == 'pending':
            run('compile_functional_sync.py', ['--trace-dir', source / 'tao_trace', '--output', stage / 'functional-sync.plan', '--allow-truncated'])
            sync = dict(sync, status='available', plan='functional-sync.plan')
        # Identity output contains staging locations. Bind its reviewable paths
        # to the installed artifacts without altering identity/hash conclusions.
        identity = load(stage / 'identity-validation.json')
        for validation in identity.get('results', []):
            for field, relative in (('result_dir', '.'), ('config_ini', 'config.ini'),
                                    ('effective_target', 'effective-target.json'),
                                    ('uarch_profile', 'tao_trace/uarch_profile.json')):
                validation[field] = str((source / relative).resolve())
        dump(stage / 'identity-validation.json', identity)
        prepared_status = 'prepared' if native_evidence['available'] else 'input-prepared-native-gate-failed'
        if request['arguments'].get('task_clock') and task_clock['status'] != 'available':
            prepared_status = 'input-prepared-task-clock-unavailable'
        report = dict(schema='fastsim-spr-capture-preparation-v1',
            status=prepared_status,
            functional_input_ready=True, instruction_translation_input=functional_audit['instruction_translation_qualification'],
            data_translation_input=functional_audit['data_translation_qualification'],
            functional_source_audit=functional_audit, native_evidence=native_evidence, source=str(source),
            common_end=gate, thread_state=thread, functional_sync=sync, task_clock=task_clock,
            target_identity_valid=True,
            accuracy_evaluated=False, native_cache_reference_certified=False,
            native_reference_note='Run maintained run_native_kernel_fastsim_validation.py; no cache truth selected by this adapter',
            original_artifact_hashes=hashes, tools=commands)
        dump(stage / 'spr-capture-preparation.json', report)
        outputs = {path.relative_to(stage): path for path in stage.rglob('*') if path.is_file() and not path.is_symlink()}
        if Path('kernel_events.json') in outputs:
            outputs[Path('tao_trace/oracle/kernel_events.json')] = outputs.pop(Path('kernel_events.json'))
        # Detect every collision before installing any output.
        for relative, candidate in outputs.items():
            destination = source / relative
            require((not destination.exists() and not destination.is_symlink()) or
                    (not destination.is_symlink() and destination.is_file() and destination.read_bytes() == candidate.read_bytes()),
                    f'conflicting existing output: {destination}')
        for original, expected in hashes.items():
            require(sha256(original) == expected['sha256'], f'capture changed during preparation: {original}')
        if not oracle_link.is_symlink():
            oracle_link.symlink_to('tao_trace/oracle', target_is_directory=True)
        for relative, candidate in outputs.items():
            destination = source / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as output:
                    output.write(candidate.read_bytes())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--workload-label')
    parser.add_argument('--fastsim-root', type=Path, default=ROOT)
    parser.add_argument('--collector-source-root', type=Path, help='selected frozen native collector source; defaults to fastsim-root/gem5-spr')
    args = parser.parse_args()
    try:
        report = prepare(args.source, workload_label=args.workload_label, fastsim_root=args.fastsim_root,
                         collector_source_root=args.collector_source_root)
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        parser.exit(1, f'SPR capture preparation failed: {error}\n')
    print(json.dumps(dict(status=report['status'], source=report['source'],
        thread_state=report['thread_state'], functional_sync=report['functional_sync'],
        task_clock=report['task_clock'],
        functional_input_ready=True, instruction_translation_input=report['instruction_translation_input'],
        data_translation_input=report['data_translation_input'],
        native_evidence=report['native_evidence'], accuracy_evaluated=False), indent=2))
    return 0 if report['status'] == 'prepared' else 1


if __name__ == '__main__':
    raise SystemExit(main())
