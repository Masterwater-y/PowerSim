#!/usr/bin/env python3
"""Bounded host native-task admission/RSS check; not output correctness or timing."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import tarfile
import time


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def extract_inputs(path, destination, limit=2 << 30):
    reserved = {'admission.json', 'case.json', 'launcher.log', 'prepare.log',
                'copy-0.stdout', 'copy-0.stderr'}
    with tarfile.open(path, 'r:*') as archive:
        members = archive.getmembers()
        seen = set(); total = 0
        if len(members) > 10000:
            raise ValueError('too many input archive members')
        for member in members:
            name = PurePosixPath(member.name)
            if (not name.parts or name.is_absolute() or '..' in name.parts
                    or name.parts[0] in reserved or str(name) in seen
                    or not (member.isfile() or member.isdir())):
                raise ValueError('unsafe input archive member: '+member.name)
            seen.add(str(name)); total += member.size
            if total > limit:
                raise ValueError('input archive exceeds extraction byte budget')
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    while True:
                        block = source.read(8 << 20)
                        if not block: break
                        output.write(block)
        return dict(bytes=total, members=len(members))


def group_memory(group):
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            # comm can contain spaces and parentheses. The final ')' delimits it.
            stat = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[2]) != group: continue
            status = (entry/'status').read_text()
            rss = re.search(r'^VmRSS:\s*(\d+) kB', status, re.M)
            swap = re.search(r'^VmSwap:\s*(\d+) kB', status, re.M)
            if stat[0] != 'Z' and (rss is None or swap is None):
                raise RuntimeError('missing live process RSS/swap evidence: '+entry.name)
            rows.append(dict(pid=int(entry.name), rss_bytes=int(rss[1])*1024 if rss else 0,
                             swap_bytes=int(swap[1])*1024 if swap else 0))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return rows


def stop_group(process):
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=3)
    # A parent can exit before its remaining group members. Terminate those too.
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass


def admitted_tasks(text, worker_text, row):
    expected = row['native_workers']
    if f'[fastsim-native] identities-ready workers={expected}\n' not in text:
        return None
    if text.count('[fastsim-rate] post-switch-census begin') != 1:
        raise ValueError('missing or repeated full process census')
    census = text.split('[fastsim-rate] post-switch-census begin', 1)[1]
    if '[fastsim-rate] post-switch-census end' not in census:
        raise ValueError('incomplete process census')
    census = census.split('[fastsim-rate] post-switch-census end', 1)[0]
    processes = re.findall(r'copy=0 pid=(\d+).*VmRSS:\s*(\d+) kB.*VmSwap:\s*(\d+) kB.*Threads:\s*(\d+)', census)
    tasks = re.findall(r'copy=0 task_tid=(\d+) task_tgid=(\d+)', census)
    workers = [dict(index=int(index), tid=int(tid), cpu=int(cpu), phase=phase)
               for index, tid, cpu, phase in re.findall(
                   r'\[fastsim-native\] ready worker=(\d+) tid=(\d+) cpu=(\d+) phase=(\S+)', worker_text)]
    if len(processes) != 1:
        raise ValueError('expected one complete application process census')
    pid, rss, swap, count = map(int, processes[0])
    tids = {int(tid) for tid, _ in tasks}
    if (len(tasks) != count or len(tids) != count or count < expected
            or any(int(tgid) != pid for _, tgid in tasks) or pid not in tids or swap):
        raise ValueError('application census population or swap mismatch')
    if (len(workers) != expected or {w['index'] for w in workers} != set(range(expected))
            or len({w['tid'] for w in workers}) != expected
            or not {w['tid'] for w in workers}.issubset(tids)
            or any(w['cpu'] != row['host_cpus'][w['index'] % row['cores']] for w in workers)):
        raise ValueError('native ready worker identity or affinity mismatch')
    if any(not Path(f'/proc/{pid}/task/{tid}').exists() for tid in tids):
        raise ValueError('census task disappeared before ROI admission')
    return dict(application_pid=pid, census_task_ids=sorted(tids), ready_workers=workers,
                census_rss_bytes=rss*1024)


def run(case_path, output, wall_seconds=900, rss_limit=28 << 30):
    start = time.monotonic()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('refuse to reuse a nonempty preflight output directory')
    receipt = dict(schema='fastsim-native-speed-host-admission-v1', valid=False,
        scope='host ROI admission and memory only; no full-output, gem5, CPI or throughput certification',
        pid=None, peak_rss_bytes=0, peak_swap_bytes=0, rss_limit_bytes=rss_limit,
        argv=None, prepare_argv=None, environment={}, reason=None)
    process = None
    try:
        row = json.loads(Path(case_path).read_text())
        (output/'case.json').write_text(json.dumps(row, indent=2)+'\n')
        cores = row['cores']; workers = row['native_workers']; cpu_pool = row['host_cpus']
        if (type(cores) is not int or not 1 <= cores <= 1024
                or type(workers) is not int or not cores <= workers <= 1024
                or len(cpu_pool) < cores or len(set(cpu_pool)) != len(cpu_pool)
                or any(type(cpu) is not int or cpu not in os.sched_getaffinity(0) for cpu in cpu_pool)):
            raise ValueError('invalid native task/core/host CPU population')
        cpus = cpu_pool[:cores]
        row['host_cpus'] = cpus
        receipt['effective_host_cpus'] = cpus
        for key in ('binary', 'launcher', 'input_archive'):
            row[key] = str(Path(row[key]).resolve(strict=True))
        receipt.update(case_id=row['case_id'], case_json_sha256=sha256(case_path),
                       input_paths={key: row[key] for key in ('binary', 'launcher', 'input_archive')})
        receipt['input_hashes'] = {key: sha256(path) for key, path in receipt['input_paths'].items()}
        receipt['extraction'] = extract_inputs(row['input_archive'], output)
        env = dict(os.environ, GEM5_DISABLE_ROI_MARKER='1')
        for key in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'OMP_PROC_BIND', 'OMP_PLACES',
                    'FASTSIM_RATE_READY_CHECKPOINT', 'FASTSIM_RATE_SERIAL_INIT',
                    'FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES'):
            env.pop(key, None)
        if row.get('native_omp', False): env['OMP_NUM_THREADS'] = str(cores)
        if row.get('native_warmup_after_identities', False):
            env['FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES'] = '1'
        env['LD_LIBRARY_PATH'] = '/opt/gcc-11/lib64'
        receipt['environment'] = {key: env.get(key) for key in
            ('GEM5_DISABLE_ROI_MARKER', 'OMP_NUM_THREADS', 'LD_LIBRARY_PATH',
             'FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES')}

        def sample(current):
            memory = group_memory(current.pid)
            rss = sum(member['rss_bytes'] for member in memory)
            swap = sum(member['swap_bytes'] for member in memory)
            receipt['peak_rss_bytes'] = max(receipt['peak_rss_bytes'], rss)
            receipt['peak_swap_bytes'] = max(receipt['peak_swap_bytes'], swap)
            receipt['last_group_memory'] = memory
            if swap: raise RuntimeError('nonzero process-group VmSwap')
            if rss > rss_limit: raise RuntimeError('process-group RSS limit exceeded')
            if time.monotonic()-start > wall_seconds: raise RuntimeError('host ROI admission deadline')

        if row.get('prepare_args'):
            receipt['prepare_argv'] = [row['binary'], *shlex.split(row['prepare_args'])]
            with (output/'prepare.log').open('w') as log:
                process = subprocess.Popen(receipt['prepare_argv'], cwd=output, env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                while process.poll() is None:
                    sample(process); time.sleep(.05)
                if process.returncode: raise RuntimeError('official input preparation failed')
                stop_group(process); process = None
        receipt['argv'] = [row['launcher'], '--cpus', ','.join(map(str, cpus)),
                           '--native-workers', str(workers), '--', row['binary'], *shlex.split(row['args'])]
        with (output/'launcher.log').open('w') as log:
            process = subprocess.Popen(receipt['argv'], cwd=output, env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            receipt['pid'] = process.pid
            while True:
                sample(process)
                text = (output/'launcher.log').read_text(errors='replace')
                worker_log = output/'copy-0.stderr'
                evidence = admitted_tasks(text, worker_log.read_text(errors='replace') if worker_log.exists() else '', row)
                if evidence:
                    if not receipt['last_group_memory']:
                        raise RuntimeError('missing live process-group memory evidence')
                    receipt.update(evidence)
                    receipt['valid'] = True
                    break
                if process.poll() is not None:
                    raise RuntimeError('workload exited before complete native ROI admission')
                time.sleep(.05)
    except Exception as error:
        receipt['valid'] = False; receipt['reason'] = str(error)
    finally:
        if process is not None:
            stop_group(process)
            receipt['returncode_after_deliberate_stop'] = process.returncode
        if receipt.get('input_hashes'):
            final = {key: sha256(path) for key, path in receipt['input_paths'].items()}
            receipt['final_input_hashes'] = final
            if final != receipt['input_hashes']:
                receipt['valid'] = False; receipt['reason'] = 'frozen input identity changed during preflight'
        receipt['wall_seconds'] = time.monotonic()-start
        (output/'admission.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case-json', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--wall-seconds', type=int, default=900)
    args = parser.parse_args()
    result = run(args.case_json, args.output, args.wall_seconds)
    print(json.dumps(result, indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
