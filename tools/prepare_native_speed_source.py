#!/usr/bin/env python3
"""Prepare a captured native speed source using frozen maintained auditors.

This is a source/identity/population gate, not an accuracy score or a retired
cache unknown-count gate. It never changes an FST or selects cache truth from
kernel path-class fields. Unknown optional syscall ABI fields remain unknown.
"""
from collections import Counter
import argparse
import configparser
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys


RAW_SYSCALL_MICROOPS = 1 << 8
HEADER = struct.Struct('<8sIIIIQQ4Q')


def load(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.preparing')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha256(path):
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 << 20), b''):
            digest.update(block)
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) ==
            (after.st_size, after.st_mtime_ns), 'file changed while hashing: ' + str(path))
    return digest.hexdigest()


def frozen_layout(root):
    root = Path(root).resolve()
    frozen = root / 'frozen' if (root / 'frozen').is_dir() else root
    for name in ('tools', 'configs', 'shared'):
        require((frozen / name).is_dir(), 'missing frozen directory: ' + str(frozen / name))
    for name in ('uarch_profile.hh', 'lru_banked.hh'):
        require((frozen / 'shared' / name).is_file(), 'missing frozen shared header: ' + name)
    require((frozen / 'configs/pmu-event-dictionary-v1.json').is_file(),
            'missing frozen PMU event dictionary')
    return frozen


def validate_capture(source, case):
    """Bind successful collection and workload identity to the requested row."""
    source = Path(source)
    receipt = load(source / 'capture-receipt.json')
    require(receipt.get('returncode') == 0 and receipt.get('guard_reason') is None,
            'capture failed or stopped by a guard')
    require(receipt.get('measurement_open') is True, 'measurement did not open')
    warmup_wall = receipt.get('detailed_warmup_wall_seconds')
    wall_limit = case.get('warmup_wall_limit_seconds', case.get('warmup_wall_seconds', 600))
    require(isinstance(warmup_wall, (int, float)) and not isinstance(warmup_wall, bool) and
            math.isfinite(warmup_wall) and warmup_wall >= 0, 'warmup wall observation unavailable or invalid')
    require(isinstance(wall_limit, (int, float)) and math.isfinite(wall_limit) and wall_limit >= 0,
            'invalid warmup wall limit')
    require(wall_limit == 0 or warmup_wall <= wall_limit + 5, 'warmup wall budget exceeded')
    request = load(source / 'sync-run-request.json')
    args = request['arguments']
    expected = dict(omp_threads=case['cores'], extra_cores=case.get('extra_cores', 0),
                    rate_copies=case.get('rate_copies', 1), native_workers=case['native_workers'],
                    functional_user_target=case.get('target_uops', 10000000),
                    warmup_insts=case.get('warmup_insts', 500000),
                    mem_size=case.get('memsize', case.get('mem_size', '32GiB')),
                    mem_channels=case.get('mem_channels', 8),
                    x86_large_memory=case.get('x86_large_memory', True),
                    ruby_functional_backing_store=case.get('ruby_functional_backing_store', False),
                    tso_store=case.get('tso_store', 'buffer'),
                    tso_store_buffer_size=case.get('tso_store_buffer_size', 114),
                    functional_syscall_microops=True, trace_format='fst', thread_state=True)
    for key in ('l1i_size', 'l1d_size', 'l2_size', 'l3_size', 'l1i_assoc', 'l1d_assoc',
                'l2_assoc', 'l3_assoc', 'num_l3_banks', 'rob_entries', 'iq_entries',
                'lq_entries', 'sq_entries', 'dtlb_entries', 'clk'):
        if key in case:
            expected[key] = case[key]
    if case.get('native_warmup_after_identities') is True:
        expected['native_warmup_after_identities'] = True
    if 'native_omp' in case:
        expected['native_omp_num_threads'] = case['native_omp']
    if 'args' in case:
        expected['workload_args'] = case['args']
    for key, value in expected.items():
        require(args.get(key) == value, 'capture request mismatch: ' + key)
    for key, argument in (('binary', 'workload_binary'), ('launcher', 'rate_launcher')):
        if case.get(key):
            require(Path(args[argument]).resolve() == Path(case[key]).resolve(),
                    'capture path mismatch: ' + key)
    if 'input_archive' in case:
        archives = case['input_archive']
        if isinstance(archives, str):
            archives = [archives]
        require([str(Path(p).resolve()) for p in args.get('input_archive', [])] ==
                [str(Path(p).resolve()) for p in archives], 'input archive mismatch')
    binary = Path(case['binary'])
    observed = sha256(binary)
    require(request['binary_identity']['binary_sha256'] == observed,
            'workload binary identity differs from captured request')
    if 'binary_sha256' in case:
        require(case['binary_sha256'] == observed, 'workload binary differs from frozen case')
    log = (source / 'capture.log').read_text(errors='replace')
    milestones = ('CHECKPOINT marker: switch KVM -> O3+Ruby',
                  'WARMUP instruction budget reached: measurement window open',
                  'functional_user_target reached: common-end, finish')
    positions = [log.find(text) for text in milestones]
    require(min(positions) >= 0 and positions == sorted(positions),
            'missing or unordered capture milestones')
    if case.get('native_warmup_after_identities') is True:
        armed_text = 'native identities ready: warmup instruction budget armed'
        armed_position = log.find(armed_text)
        require(log.count(armed_text) == 1 and positions[0] < armed_position < positions[1],
                'native identities warmup armed milestone missing or out of order')
        phase_path = source / 'capture-phases.jsonl'
        require(phase_path.is_file(), 'native identities warmup phase ledger missing')
        phases = [json.loads(line) for line in phase_path.read_text().splitlines() if line.strip()]
        names = ('switch-complete', 'native-identities-ready', 'warmup-armed',
                 'measurement-open', 'common-end')
        selected, indices = [], []
        for name in names:
            matches = [(index, row) for index, row in enumerate(phases) if row.get('phase') == name]
            require(len(matches) == 1, 'native identities warmup phase missing or duplicated: ' + name)
            indices.append(matches[0][0])
            selected.append(matches[0][1])
        require(indices == sorted(indices), 'native identities warmup phase order mismatch')
        for row in selected:
            tick, wall = row.get('tick'), row.get('wall_time')
            require(isinstance(tick, int) and not isinstance(tick, bool) and tick >= 0 and
                    isinstance(wall, (int, float)) and not isinstance(wall, bool) and
                    math.isfinite(wall) and wall >= 0, 'invalid native identities warmup phase clock')
        ticks = [row['tick'] for row in selected]
        walls = [row['wall_time'] for row in selected]
        require(walls == sorted(walls), 'native identities warmup phase wall order mismatch')
        # Callback ledger order and guest ticks prove readiness before arming;
        # serial and host-log byte offsets are never compared across streams.
        require(ticks == sorted(ticks) and ticks[1] == ticks[2] and ticks[3] > ticks[2],
                'native identities warmup phase tick order mismatch')
    require(': fatal:' not in log and ': panic:' not in log, 'fatal/panic in capture log')
    serial = (source / 'board.pc.com_1.device').read_text(errors='replace')
    require('Out of memory:' not in serial and 'Killed process' not in serial,
            'guest OOM evidence')
    require('[fastsim-native] identities-ready workers=' + str(case['native_workers']) in serial,
            'missing native ready identities')
    return request


def audit_process_scope(source, case, audit_syscall):
    """Cover all ready-census process members and syscall entries in both phases."""
    source = Path(source)
    trace = source / 'tao_trace'
    serial = (source / 'board.pc.com_1.device').read_text(errors='replace')
    begin, end = ('[fastsim-rate] post-switch-census begin',
                  '[fastsim-rate] post-switch-census end')
    require(serial.count(begin) == serial.count(end) == 1 and serial.index(begin) < serial.index(end),
            'missing/ambiguous final O3 census')
    census = serial.split(begin)[1].split(end)[0]
    copies = case.get('rate_copies', 1)
    process_rows = re.findall(r'copy=(\d+) pid=(\d+)[^\n]*Threads:\s*(\d+)', census)
    require(len(process_rows) == copies, 'missing/duplicate copy census')
    processes = {int(i): int(pid) for i, pid, _ in process_rows}
    counts = {int(i): int(n) for i, _, n in process_rows}
    require(set(processes) == set(range(copies)) and len(set(processes.values())) == copies,
            'ambiguous copy/process census')
    thread_rows = [tuple(map(int, v)) for v in re.findall(
        r'copy=(\d+) task_tid=(\d+) task_tgid=(\d+)', census)]
    require(len({tid for _, tid, _ in thread_rows}) == len(thread_rows), 'duplicate native TID census')
    roster = {tid: tgid for _, tid, tgid in thread_rows}
    for copy, pid in processes.items():
        members = [tid for i, tid, tgid in thread_rows if i == copy and tgid == pid]
        require(len(members) == counts[copy] and pid in members, 'incomplete native thread census')
    require(all(i in processes and processes[i] == tgid for i, _, tgid in thread_rows),
            'copy/TGID mismatch')
    require(roster and set(roster.values()) == set(processes.values()), 'incomplete ready roster')
    account = load(source / 'oracle/thread-state-accounting.json')
    require(account.get('valid') is True and account.get('ownership_status') == 'complete'
            and account.get('unknown_cycles') == 0, 'incomplete owner accounting')
    # Native retirement-stop counters replace per_core's cycle view. The
    # independently audited switch-owner intervals remain in the preserved
    # proxy namespace; they prove process population, never active CPI.
    owner_source = 'recorded_owner_proxy' if 'recorded_owner_proxy' in account else 'per_core'
    owner_view = account.get('recorded_owner_proxy', account)
    owner_rows = owner_view.get('per_core') if isinstance(owner_view, dict) else None
    require(isinstance(owner_rows, list) and len(owner_rows) == case['cores'] and
            all(isinstance(row, dict) and row.get('core_id', core) == core and
                isinstance(row.get('owner_spans'), list) for core, row in enumerate(owner_rows)),
            'incomplete owner span participants')
    marked, identities, syscall_audits = {}, [], []
    retired, owner_ticks, syscalls = Counter(), Counter(), Counter()
    for core in range(case['cores']):
        boundary = load(trace / ('functional-boundary-core%d.json' % core))
        warmup = boundary['warmup_records']
        events = [json.loads(line) for line in (trace / ('thread-state-core%d.jsonl' % core)).read_text().splitlines()
                  if line.strip()]
        for event in events:
            if event['event_kind'] != 'identity':
                continue
            tid, tgid = event['os_tid'], event['os_tgid']
            require(roster.get(tid) == tgid, 'identity outside ready census')
            require(event['phase'] == 'warmup' and event['record_ordinal'] < warmup,
                    'compute worker identity missed stat entry')
            require(tid not in marked, 'duplicate compute worker identity')
            marked[tid] = tgid
            identities.append(dict(core=core, tid=tid, tgid=tgid,
                                   identity_ordinal=event['record_ordinal'], measurement_begin_ordinal=warmup))
        native = load(source / ('oracle/task-pmu-core%d.json' % core))
        for owner in native['owners']:
            retired[owner['os_tid']] += owner['retired_uops']
        for span in owner_rows[core]['owner_spans']:
            ticks = span['end_tick'] - span['start_tick']
            require(ticks >= 0, 'negative owner span')
            owner_ticks[span['owner_pid']] += ticks
        meta = load(trace / ('thread-state-metadata-core%d.json' % core))
        fst = trace / meta['source_file']
        with fst.open('rb') as handle:
            header = HEADER.unpack(handle.read(HEADER.size))
        require(header[6] & RAW_SYSCALL_MICROOPS and not header[6] & 2,
                'source lacks raw syscall micro-op capability')
        audited = audit_syscall(fst)
        require(audited['core_id'] == core and audited['records'] == boundary['total_records'],
                'syscall/FST core population mismatch')
        # The maintained parser validates every sparse row and its real-UOP
        # anchor. op_class==-1 is intentionally absent from a raw stream.
        require(sum(audited['syscall_numbers'].values()) == audited['syscalls'],
                'syscall metadata population mismatch')
        syscalls.update({int(k): v for k, v in audited['syscall_numbers'].items()})
        syscall_audits.append(audited)
    require(len(marked) == case['native_workers'] and set(marked.values()) == set(processes.values()),
            'missing per-copy compute identity')
    creation = {number: syscalls[number] for number in (56, 57, 58, 435) if syscalls[number]}
    require(not creation, 'new task syscall in retained warmup+measurement: ' + repr(creation))
    unmarked = {tid: dict(tgid=tgid, retired_uops=retired[tid], owner_ticks=owner_ticks[tid])
                for tid, tgid in roster.items() if tid not in marked}
    require(all(v['retired_uops'] == 0 and v['owner_ticks'] == 0 for v in unmarked.values()),
            'unmarked same-process member active in measurement: ' + repr(unmarked))
    return dict(schema='fastsim-rate-process-scope-gate-v1', valid=True,
                scope='all ready-census process members; no later fork/clone in retained warmup+measurement',
                owner_span_source=owner_source,
                processes=processes, ready_thread_roster=roster, marked_compute_tasks=marked,
                identity_before_measurement=identities, unmarked_members=unmarked,
                task_creation_syscalls=creation, source_syscall_numbers=dict(syscalls),
                syscall_metadata_audits=syscall_audits, owner_unknown_cycles=0,
                measurement_scope_equivalence='unmarked process members have zero retired UOPs and observed owner ticks',
                measurement_macro_instructions=account['task_macro_instructions'])


def audit_memory(source, case):
    source = Path(source)
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read(str(source / 'config.ini'))
    dram = load(source / 'effective-target.json')['dram']
    size = case.get('memsize', case.get('mem_size', '32GiB'))
    match = re.fullmatch(r'(\d+)([KMGT])iB', size)
    require(match is not None, 'unsupported case memory size: ' + size)
    wanted_bytes = int(match[1]) * 1024 ** ('KMGT'.index(match[2]) + 1)
    require(dram['size_b'] == wanted_bytes and dram['channels_per_range'] == case.get('mem_channels', 8),
            'effective memory geometry mismatch')
    controllers = [s for s in config.sections() if config[s].get('type') == 'MemCtrl']
    directories = [s for s in config.sections() if config[s].get('type') == 'MESI_Three_Level_Directory_Controller']
    require(len(controllers) == len(directories) == dram['num_channels'], 'controller/directory population mismatch')
    controller_ranges = [config[config[s]['dram']]['range'] for s in controllers]
    directory_ranges = [config[s]['addr_ranges'] for s in directories]
    require(Counter(controller_ranges) == Counter(directory_ranges), 'controller/directory ranges differ')
    require(config['board.cache_hierarchy.ruby_system'].get('access_backing_store') == 'false',
            'unexpected Ruby backing store')
    for section in controllers:
        interface = config[config[section]['dram']]
        require(interface.get('kvm_map') == interface.get('in_addr_map') == 'true',
                'DRAM is not the direct KVM functional memory')
    return dict(valid=True, mode='direct-ranged-DRAM-KVM-map', physical_ranges=dram['physical_ranges'],
                channels_per_range=dram['channels_per_range'], controllers=len(controllers),
                directory_ranges=directory_ranges, config_ini_sha256=sha256(source / 'config.ini'))


def prepare(source, case, frozen):
    source = Path(source).resolve()
    trace = source / 'tao_trace'
    frozen = frozen_layout(frozen)
    sys.path.insert(0, str(frozen / 'tools'))
    from common_end_capture import validate_common_end
    from audit_fst_syscall_metadata import audit_file
    request = validate_capture(source, case)
    write(source / 'preparation-case.json', case)
    request.update(profile=dict(cores=case['cores']),
                   workload_selection=dict(workload=case.get('workload', case['name']), official_variant='speed', official_input=True,
                                           input_size=case.get('input_size', 'official-refspeed-first-command'),
                                           phase=case.get('phase', 'native speed computational ROI')),
                   sampling=dict(functional_include_kernel=True, functional_user_only=False, functional_warmup=True,
                                 functional_syscall_microops=True,
                                 native_warmup_after_identities=case.get('native_warmup_after_identities') is True,
                                 roi_insts=case.get('target_uops', 10000000),
                                 count_unit='user-uop', measurement_policy='first-core-target-common-end-v1',
                                 tso_store=request['arguments']['tso_store'],
                                 tso_store_buffer_size=request['arguments']['tso_store_buffer_size'],
                                 tso_store_buffer_active=request['arguments']['tso_store'] == 'buffer'))
    write(source / 'request.json', request)
    if not (source / 'oracle').exists():
        (source / 'oracle').symlink_to('tao_trace/oracle', target_is_directory=True)
    bounds = [load(trace / ('functional-boundary-core%d.json' % c)) for c in range(case['cores'])]
    manifest = []
    for core, boundary in enumerate(bounds):
        require(boundary['warmup_records'] > 0 and boundary['measurement_records'] > 0, 'empty warmup/measurement')
        meta = load(trace / ('thread-state-metadata-core%d.json' % core))
        require(meta['core_id'] == core and meta['source_record_count'] == boundary['total_records'], 'metadata population mismatch')
        fst = trace / meta['source_file']
        with fst.open('rb') as handle:
            header = HEADER.unpack(handle.read(HEADER.size))
        require(header[6] & (RAW_SYSCALL_MICROOPS | 48) == (RAW_SYSCALL_MICROOPS | 48),
                'raw syscall, privilege and complete dependencies required on every core')
        layout = audit_file(fst)
        require(layout['core_id'] == core and layout['records'] == boundary['total_records'], 'FST population mismatch')
        manifest.append('%d binary-warmup-slice %s %d %d %d %d %d\n' % (
            core, fst, core, boundary['warmup_instructions'], boundary['measurement_instructions'],
            boundary['warmup_records'], boundary['measurement_records']))
    (source / 'replay').mkdir(exist_ok=True)
    for path in (trace / 'manifest.txt', source / 'replay/manifest.txt'):
        path.write_text(''.join(manifest))
    metadata = dict(schema='tcsim-gem5-fs-functional-trace-v1', cores=case['cores'],
                    converter=dict(path='native-producer-fst7'), trace_scope='user-plus-kernel',
                    functional_warmup_enabled=True, functional_syscall_microops=True,
                    measurement_policy='first-core-target-common-end-v1',
                    functional_boundaries={str(i): b for i, b in enumerate(bounds)},
                    per_core={str(i): b for i, b in enumerate(bounds)})
    write(trace / 'trace.json', metadata)
    py, tools = sys.executable, frozen / 'tools'
    commands = [
        ('merge-kernel', [py, str(tools / 'merge_kernel_events_oracle_v3.py'), str(source / 'oracle')]),
        ('generate-target', [py, str(tools / 'generate_fs_effective_target.py'), '--config', str(source / 'config.ini'),
                             '--uarch-profile', str(trace / 'uarch_profile.json'), '--effective-target', str(source / 'effective-target.json'),
                             '--taotrace-shared-root', str(frozen / 'shared')]),
        ('hardware-identity', [py, str(tools / 'validate_fs_oracle_identity.py'), '--result', str(source),
                               '--output', str(source / 'identity-validation.json'),
                               '--event-dictionary', str(frozen / 'configs/pmu-event-dictionary-v1.json')]),
        ('compile-thread-state', [py, str(tools / 'compile_thread_state.py'), '--trace-dir', str(trace),
                                  '--output', str(source / 'thread-state.plan'), '--summary', str(source / 'thread-state-summary.json'), '--require-sources']),
        ('owner-accounting', [py, str(tools / 'audit_thread_state_accounting.py'), '--trace-dir', str(trace),
                              '--output', str(source / 'oracle/thread-state-accounting.json')]),
        ('task-native-population', [py, str(tools / 'audit_task_pmu.py'), '--trace-dir', str(trace),
                                   '--output', str(source / 'oracle/task-pmu-audit.json')]),
        ('native-summary-conservation', [py, str(tools / 'audit_p1_native_response_sideband.py'), '--result', str(source),
                                         '--output', str(source / 'native-response-audit.json'), '--jobs', '1']),
    ]
    write(source / 'preparation-commands.json', commands)
    for name, command in commands:
        require(Path(command[1]).is_file(), 'missing frozen auditor: ' + command[1])
        print('AUDIT', name, flush=True)
        with (source / (name + '.log')).open('w') as output:
            subprocess.run(command, cwd=str(source), stdout=output, stderr=subprocess.STDOUT, check=True)
    process_scope = audit_process_scope(source, case, audit_file)
    write(source / 'process-scope-audit.json', process_scope)
    write(source / 'syscall-metadata-audit.json', dict(schema='fastsim-raw-syscall-source-audit-v1',
          valid=True, raw_microops=True, per_core=process_scope['syscall_metadata_audits'],
          optional_fields_policy='preserve validity; never infer unknown values'))
    cpi = [load(source / ('oracle/cpi-core%d.json' % c)) for c in range(case['cores'])]
    cpl = [json.loads(line) for line in (source / 'oracle/cpl_class.jsonl').read_text().splitlines() if line.strip()]
    common = validate_common_end(bounds, cpi, cpl, cores=case['cores'], target=case.get('target_uops', 10000000))
    write(source / 'common-end-audit.json', common)
    metadata['common_end'] = common
    write(trace / 'trace.json', metadata)
    write(source / 'functional-memory-audit.json', audit_memory(source, case))
    require(load(source / 'identity-validation.json')['valid'], 'independent target identity failed')
    require(load(source / 'oracle/task-pmu-audit.json')['valid'], 'strict task PMU population failed')
    native = load(source / 'native-response-audit.json')
    require(native['all_collection_eligible'] and native['all_target_drains_complete'], 'native conservation/drain failed')
    # Hash the actual functional input and all source/audit evidence only after
    # every gate. No retired-cache unknown-count requirement is hidden here.
    paths = {p.resolve() for p in trace.rglob('*') if p.is_file()}
    completed_logs = {'capture.log'} | {name + '.log' for name, _ in commands}
    # A caller may redirect this tool's stdout to a live preparation log in
    # source/. Its final SOURCE_READY line must not invalidate a stored hash.
    paths.update(p.resolve() for p in source.iterdir() if p.is_file() and p.name not in
                 ('source-ready.json', 'source-artifact-hashes.json') and
                 not p.name.endswith('.preparing') and
                 (p.suffix != '.log' or p.name in completed_logs))
    paths.update(p.resolve() for p in (source / 'oracle').rglob('*') if p.is_file())
    for tree in ('tools', 'configs', 'shared', 'scripts', 'build'):
        paths.update(p.resolve() for p in (frozen / tree).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts)
    for key in ('binary', 'launcher'):
        if case.get(key):
            paths.add(Path(case[key]).resolve())
    archives = case.get('input_archive', [])
    if isinstance(archives, str):
        archives = [archives]
    paths.update(Path(p).resolve() for p in archives)
    hashes = [dict(path=str(p), size_bytes=p.stat().st_size, sha256=sha256(p)) for p in sorted(paths)]
    write(source / 'source-artifact-hashes.json', dict(schema='fastsim-native-speed-source-hashes-v1', artifacts=hashes))
    ready = dict(valid=True, schema='fastsim-native-speed-source-ready-v1', case_id=case['case_id'],
                 scope='common-end, target identity, ownership/process/syscall population and native conservation',
                 fastest_core_target_user_uops=case.get('target_uops', 10000000), actual_user_uops=common['user_uops'],
                 owner_unknown_cycles=0, raw_syscall_microops=True,
                 native_warmup_after_identities=case.get('native_warmup_after_identities') is True,
                 frozen_root=str(frozen),
                 artifact_hashes_sha256=sha256(source / 'source-artifact-hashes.json'),
                 native_cache_truth_source='Ruby/SLICC native summaries', accuracy_scored=False,
                 retired_cache_unknown_gate='separate; not implied by source readiness',
                 hardware_perf_equivalence='unverified')
    write(source / 'source-ready.json', ready)
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--case-json', type=Path, required=True, help='one case JSON object')
    parser.add_argument('--frozen-root', type=Path, required=True, help='experiment root containing frozen/, or frozen/ itself')
    args = parser.parse_args()
    ready_path = args.source / 'source-ready.json'
    try:
        require(args.source.is_dir(), 'source directory does not exist')
        write(ready_path, dict(valid=False, status='preparing'))
        ready = prepare(args.source, load(args.case_json), args.frozen_root)
        print('SOURCE_READY', json.dumps(ready), flush=True)
        return 0
    except Exception as error:
        if args.source.is_dir():
            write(ready_path, dict(valid=False, status='failed', error=str(error)))
        print('SOURCE_REJECTED', str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
