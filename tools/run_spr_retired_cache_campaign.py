#!/usr/bin/env python3
"""Recollect and audit four SPR C4 retired-cache observer sources."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

from capture_reservations import ReservationPool, byte_snapshot
from retired_cache_contract import audit_retired_cache, MISS_EVENTS, SPR_MISS_POLICY

ROOT = Path(__file__).resolve().parents[1]
CAPS = dict(marian=8_000_000_000, graph500=8_000_000_000,
            xz=10_000_000_000, flac=14_000_000_000)
PYTHON = '/root/.pyenv/versions/3.11.14/bin/python3'


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(16 << 20), b''):
            digest.update(data)
    return digest.hexdigest()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def commands(args, row, slot):
    source = args.output / 'sources' / row['case_id']
    capture = [
        str(args.gem5), '--outdir=' + str(source),
        str(ROOT / 'gem5-spr/configs/fastsim/fs_capture.py'),
        '--fastsim-root', str(ROOT), '--workload-binary', row['binary'],
        '--workload-args', row['args'], '--participant-cores', '4',
        '--extra-cores', '0', '--functional-user-target', str(args.target),
        '--warmup-insts', str(args.warmup), '--input-archive', row['input_archive'],
        '--rate-copies', '1', '--native-workers', str(row['native_workers']),
        '--rate-launcher', row['launcher'], '--rate-private-cwd',
        '--native-warmup-after-identities', '--thread-state', '--task-clock',
        '--scheduler-profile', str(args.scheduler_profile),
        '--staging-size', row.get('staging_size', '512m'), '--mem-size', '32GiB',
        '--x86-large-memory', '--housekeeping-cpu', '0',
        '--kvm-quantum-us', '1000', '--max-ticks', '500000000000000']
    if row['native_omp']:
        capture.append('--native-omp-num-threads')
    if row.get('prepare_args'):
        capture.extend(['--prepare-workload-args', row['prepare_args']])
    if args.kernel:
        capture.extend(['--kernel', str(args.kernel.resolve())])
    if args.disk:
        capture.extend(['--disk', str(args.disk.resolve()),
                        '--disk-root-partition', args.disk_root_partition])
    guard = [
        PYTHON, str(ROOT / 'tools/trace_capture_storage.py'),
        '--root', str(ROOT), '--output', str(source),
        '--case-cap-bytes', str(CAPS[row['name']]), '--wall-seconds', '172800',
        '--warmup-wall-seconds', '0', '--', *capture]
    prepare = [
        PYTHON, str(ROOT / 'tools/prepare_spr_capture.py'), '--source', str(source),
        '--workload-label', row['workload']]
    low = slot * 24
    return dict(source=str(source), capture=guard, prepare=prepare,
                cpu_mask=f'{low}-{low+23},{low+96}-{low+119}')


class Campaign:
    def __init__(self, args, rows, plan):
        self.args, self.rows, self.plan = args, rows, plan
        self.lock = threading.RLock()
        self.children = {}
        self.stopped = False
        self.states = {r['case_id']: dict(phase='queued', passed=False) for r in rows}
        self.env = dict(os.environ, M5_OVERRIDE_PY_SOURCE='true',
            LD_LIBRARY_PATH='/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64:'
                            '/home/data00/xuhaoen/toolchains/protobuf/lib')
        self.pool = ReservationPool(ROOT, ROOT / 'tmp/native-capture-reservations.json',
                                    limit=395_000_000_000, minimum_free=35_000_000_000)

    def state(self, case, **updates):
        with self.lock:
            self.states[case].update(updates, updated_unix=time.time())
            dump(self.args.output / 'campaign-status.json',
                 dict(pid=os.getpid(), states=self.states, stopped=self.stopped))

    def stop(self, number, frame):
        with self.lock:
            self.stopped = True
            for process in self.children.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def command(self, case, stage, argv, mask):
        argv = ['taskset', '-c', mask, *argv]
        receipts = self.args.output / 'receipts' / case
        dump(receipts / (stage + '-command.json'),
             dict(argv=argv, cwd=str(ROOT), environment={
                 k:self.env[k] for k in ('LD_LIBRARY_PATH', 'M5_OVERRIDE_PY_SOURCE')}))
        self.state(case, phase=stage)
        with (receipts / (stage + '.log')).open('w') as log:
            with self.lock:
                if self.stopped:
                    raise RuntimeError('campaign interrupted')
                process = subprocess.Popen(argv, cwd=ROOT, env=self.env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.children[case] = process
                self.state(case, child_pid=process.pid)
            code = process.wait()
            with self.lock:
                self.children.pop(case, None)
        dump(receipts / (stage + '-exit.json'), dict(returncode=code))
        if code:
            raise RuntimeError(f'{stage} failed with {code}: {receipts / (stage + ".log")}')

    def worker(self, row):
        case = row['case_id']
        cmd = self.plan['commands'][case]
        source = Path(cmd['source'])
        claimed = False
        try:
            if source.exists():
                raise RuntimeError('source exists; evidence overwrite refused')
            if not self.pool.claim(source, CAPS[row['name']]):
                raise RuntimeError('FST/free-space reservation refused')
            claimed = True
            self.command(case, 'capture', cmd['capture'], cmd['cpu_mask'])
            receipt = json.loads((source / 'capture-receipt.json').read_text())
            if receipt['returncode'] or receipt['guard_reason']:
                raise RuntimeError('capture storage receipt not clean')
            self.command(case, 'prepare', cmd['prepare'], cmd['cpu_mask'])
            preparation = json.loads((source / 'spr-capture-preparation.json').read_text())
            if preparation.get('target_identity_valid') is not True:
                raise RuntimeError('separate SPR identity gate failed')
            self.state(case, phase='audit-retired-cache')
            reference = audit_retired_cache(source / 'tao_trace')
            if reference.get('membership_policy') != SPR_MISS_POLICY:
                raise RuntimeError('active retired observer policy missing')
            dump(self.args.output / 'audits' / (case + '.json'), reference)
            cores = []
            for core in reference['per_core']:
                events = core['scopes']['workload_task']['events']
                cores.append(dict(core_id=core['core_id'],
                    eligible_loads=events['retired_loads']['count'],
                    population_unknown=events['retired_loads']['unknown'],
                    events={name:events[name] for name in MISS_EVENTS}))
            zero = len(cores) == 4 and all(
                c['eligible_loads'] > 0 and c['population_unknown'] == 0 and
                all(e['unknown'] == 0 for e in c['events'].values()) for c in cores)
            self.state(case, phase='complete', passed=zero and not reference.get('evidence_gaps'),
                unknown_zero=zero,
                target_identity_valid=True, per_core=cores,
                source_evidence_gaps=reference.get('evidence_gaps', []),
                hardware_equivalence='unverified')
        except BaseException as error:
            self.state(case, phase='failed', error=repr(error), passed=False)
        finally:
            if claimed:
                if not source.exists() or (source / 'capture-receipt.json').exists():
                    self.pool.release(source)
                else:
                    self.state(case, reservation_retained=True)

    def run(self):
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, self.stop)
        with ThreadPoolExecutor(max_workers=len(self.rows)) as executor:
            for future in as_completed([executor.submit(self.worker, r) for r in self.rows]):
                future.result()
        result = dict(expected_cases=len(self.rows),
            passed_cases=sum(s['passed'] for s in self.states.values()),
            states=self.states, hardware_equivalence='unverified',
            acceptance='workload_task eligible loads and per-core retired L1/L2/L3 miss unknown zero',
            completed_unix=time.time())
        result['passed'] = result['passed_cases'] == len(self.rows)
        dump(self.args.output / 'completion.json', result)
        print(json.dumps(result, indent=2), flush=True)
        return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--roster', type=Path,
        default=ROOT / 'tmp/spec-speed-32g-c4-c8-c16-20261027-final/cases.json')
    parser.add_argument('--scheduler-profile', type=Path,
        default=ROOT / 'tmp/spr-asid-task-clock-20261104/kernel-scheduler-profile.json')
    parser.add_argument('--gem5', type=Path, default=ROOT / 'gem5-spr/build/X86/gem5.opt')
    parser.add_argument('--kernel', type=Path, help='local guest kernel')
    parser.add_argument('--disk', type=Path, help='local guest disk image')
    parser.add_argument('--disk-root-partition', default='1')
    parser.add_argument('--target', type=int, default=10_000_000)
    parser.add_argument('--warmup', type=int, default=500_000)
    parser.add_argument('--case', choices=tuple(CAPS))
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.relative_to(ROOT)
    rows = [r for r in json.loads(args.roster.read_text()) if r['cores'] == 4]
    if len(rows) != 4 or {r['name'] for r in rows} != set(CAPS):
        raise RuntimeError('four-case C4 roster identity mismatch')
    if args.case:
        rows = [r for r in rows if r['name'] == args.case]
    if args.target <= 0 or args.warmup < 0:
        raise ValueError('invalid functional window')
    plan = dict(commands={r['case_id']:commands(args,r,i) for i,r in enumerate(rows)},
        target=args.target, warmup=args.warmup, free_bytes=shutil.disk_usage(ROOT).free,
        fst_bytes=byte_snapshot(ROOT,[])[0], source_caps=CAPS)
    if not args.run:
        print(json.dumps(plan, indent=2))
        return 0
    if args.output.exists():
        raise RuntimeError('output exists; evidence overwrite refused')
    policy = json.loads((ROOT / 'configs/trace-storage-policy.json').read_text())
    if policy['total_fst_cap_bytes'] != 400_000_000_000 or policy['minimum_free_disk_bytes'] != 30_000_000_000:
        raise RuntimeError('configured storage policy differs from campaign gates')
    budget = sum(CAPS[r['name']] for r in rows)
    if plan['free_bytes'] < budget + 14_000_000_000 + 35_000_000_000:
        raise RuntimeError('actual free space insufficient for reservations and sidecars')
    if plan['fst_bytes'] + budget >= 395_000_000_000:
        raise RuntimeError('global FST headroom insufficient')
    if not set(range(192)) <= set(os.sched_getaffinity(0)):
        raise RuntimeError('campaign CPU masks unavailable')
    files = [args.gem5,args.roster,args.scheduler_profile,
        ROOT/'gem5-spr/configs/fastsim/fs_capture.py',
        ROOT/'gem5-spr/configs/fastsim/collection.py',
        ROOT/'gem5-spr/src/mem/retired_cache_membership.hh',
        ROOT/'gem5-spr/src/mem/spr_retired_load_miss.hh',
        ROOT/'configs/spr-retired-load-miss-v2.json',
        ROOT/'gem5-spr/src/mem/taotrace_response.hh',
        ROOT/'gem5-spr/src/cpu/o3/probe/perf_retirement.hh',
        ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc',
        ROOT/'configs/retired-load-cache-membership-v1.json',Path(__file__)]
    files.extend(path for path in (args.kernel, args.disk) if path is not None)
    for row in rows:
        if sha(row['binary']) != row['binary_sha256']:
            raise RuntimeError('workload executable changed')
        files.extend(Path(row[k]) for k in ('binary','input_archive','launcher'))
    dump(args.output/'freeze.json',dict(files={str(p):sha(p) for p in files}))
    dump(args.output/'plan.json',plan)
    return Campaign(args,rows,plan).run()


if __name__ == '__main__':
    raise SystemExit(main())
