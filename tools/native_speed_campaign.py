#!/usr/bin/env python3
"""Bounded parallel native speed collection; source acceptance is audited separately."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time


def write_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)


def build_capture_command(root, experiment, row, phase, cpus):
    root=Path(root);e=Path(experiment);f=e/'frozen';case=row['case_id']
    output=e/('source' if phase=='source' else 'checkpoint-creation')/case
    args=['taskset','-c',','.join(map(str,cpus)),str(f/'gem5.opt'),
          '--outdir='+str(output),str(f/'scripts/run_gem5_sync_probe.py'),
          '--workload-binary',row['binary'],'--workload-args',row['args'],
          '--input-archive',row['input_archive'],'--participant-cores',str(row['cores']),
          '--rate-copies','1','--native-workers',str(row['native_workers']),
          '--rate-launcher',row['launcher'],'--rate-private-cwd',
          '--functional-user-target',str(row['target_uops']),
          '--warmup-insts',str(row['warmup_insts']),
          '--functional-syscall-microops','--x86-large-memory',
          '--mem-size','32GiB','--mem-channels','8',
          '--tso-store','buffer','--tso-store-buffer-size','114',
          '--thread-state','--scheduler-profile',str(f/'kernel-scheduler-profile.json'),
          '--kvm-quantum-us','1000','--max-ticks','500000000000000',
          '--housekeeping-cpu','0','--staging-size',row.get('staging_size','512m')]
    if row['native_omp']:args+=['--native-omp-num-threads']
    if row.get('native_warmup_after_identities',False):args+=['--native-warmup-after-identities']
    if row.get('prepare_args'):args+=['--prepare-workload-args',row['prepare_args']]
    args+=['--restore-boot-checkpoint' if phase=='source' else '--save-boot-checkpoint',
           str(e/'checkpoints'/case)]
    if phase!='source':args+=['--checkpoint-only']
    return args


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(16<<20),b''):h.update(block)
    return h.hexdigest()


def source_gate_ready(path):
    path=Path(path)
    if not path.exists():return False
    ready=json.loads(path.read_text())
    if ready.get('valid') is False and ready.get('status')=='preparing':return False
    if ready.get('valid') is not True or ready.get('raw_syscall_microops') is not True:
        raise ValueError('native raw syscall smoke did not qualify')
    return True


def capture_phases(source, checkpoint, resume_unstarted=False):
    if Path(source).exists():raise RuntimeError('source output already exists; refusing overwrite')
    if Path(checkpoint).exists():
        if not resume_unstarted or not (Path(checkpoint)/'fastsim-checkpoint.json').is_file():
            raise RuntimeError('existing checkpoint requires explicit unstarted-source resume')
        return ['source']
    return ['checkpoint','source']


def run(experiment, jobs=4, source_gate=None, resume_unstarted=False, cpu_offset=0):
    e=Path(experiment).resolve();root=e.parents[1];f=e/'frozen'
    sys.path.insert(0,str(f/'tools'))
    from capture_reservations import ReservationPool
    rows=json.loads((e/'cases.json').read_text())
    freeze=json.loads((e/'freeze.json').read_text())
    for path,digest in freeze['files'].items():
        if file_hash(path)!=digest:raise RuntimeError('frozen input changed: '+path)
    policy=json.loads((root/'configs/trace-storage-policy.json').read_text())
    pool=ReservationPool(root,root/'tmp/native-capture-reservations.json',
        limit=policy['total_fst_cap_bytes']-policy['total_fst_stop_margin_bytes'],
        minimum_free=policy['minimum_free_disk_bytes'])
    lock=threading.Lock();states={r['case_id']:dict(phase='queued',qualified=False) for r in rows}
    def status(case,**values):
        with lock:
            states[case].update(values,updated_unix=time.time())
            write_json(e/'campaign-status.json',dict(schema='native-speed-campaign-v1',
                pid=os.getpid(),expected_cases=len(rows),jobs=jobs,policy=policy,
                states=states,qualified_cases=sum(s.get('qualified',False) for s in states.values()),
                free_bytes=shutil.disk_usage(root).free))
    env=dict(os.environ,LD_LIBRARY_PATH='/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64',
             M5_OVERRIDE_PY_SOURCE='false')
    py=sys.executable
    groups=[list(range(a+cpu_offset,a+cpu_offset+24)) for a in [0,24,48,72]]
    if not all(set(cpus)<=os.sched_getaffinity(0) for cpus in groups[:jobs]):
        raise RuntimeError('campaign CPU groups outside process affinity')
    audit_slots=threading.Semaphore(2)
    def command_run(case,name,command,timeout=None):
        directory=e/'receipts'/case;directory.mkdir(parents=True,exist_ok=True)
        write_json(directory/(name+'-command.json'),command)
        with (directory/(name+'.log')).open('w') as out:
            result=subprocess.run(command,cwd=root,env=env,stdout=out,
                                  stderr=subprocess.STDOUT,timeout=timeout)
        if result.returncode:raise RuntimeError(name+' returned '+str(result.returncode))
    def worker(row,slot):
        case=row['case_id'];source=e/'source'/case;reserved=False
        cpus=groups[slot];row_path=f/'cases'/(case+'.json')
        try:
            phases=capture_phases(source,e/'checkpoints'/case,resume_unstarted)
            status(case,phase='host-admission')
            admission=e/'host'/case/'admission.json'
            if not admission.exists():
                command_run(case,'host-admission',[py,str(f/'tools/preflight_native_speed_roi.py'),
                    '--case-json',str(row_path),'--output',str(e/'host'/case),
                    '--wall-seconds','1800'],timeout=1900)
            a=json.loads(admission.read_text())
            if not a['valid'] or a['case_json_sha256']!=file_hash(row_path):
                raise RuntimeError('host admission invalid or stale')
            status(case,phase='capacity-admission')
            deadline=time.monotonic()+row['whole_run_wall_seconds']
            while not pool.claim(source,row['source_fst_cap_bytes']):
                status(case,phase='waiting-for-capacity')
                if time.monotonic()>deadline:raise RuntimeError('capacity unavailable; pending budget requires inspection')
                time.sleep(5)
            reserved=True
            for phase in phases:
                if phase=='source' and source_gate is not None:
                    gate_deadline=time.monotonic()+7200
                    while not source_gate_ready(source_gate):
                        status(case,phase='waiting-for-native-smoke')
                        if time.monotonic()>gate_deadline:
                            raise RuntimeError('native smoke has not qualified; source remains unstarted')
                        time.sleep(2)
                status(case,phase=phase)
                output=e/('source' if phase=='source' else 'checkpoint-creation')/case
                cap=row['source_fst_cap_bytes'] if phase=='source' else 1000000000
                command=[py,str(f/'tools/trace_capture_storage.py'),'--root',str(root),
                    '--output',str(output),'--case-cap-bytes',str(cap),
                    '--wall-seconds',str(row['whole_run_wall_seconds'] if phase=='source' else 7200),
                    '--warmup-wall-seconds','0','--',
                    *build_capture_command(root,e,row,phase,cpus)]
                command_run(case,phase,command)
                receipt=json.loads((output/'capture-receipt.json').read_text())
                if receipt['returncode']!=0 or receipt['guard_reason']:
                    raise RuntimeError('abnormal '+phase+' receipt')
                if phase=='checkpoint' and not (e/'checkpoints'/case/'fastsim-checkpoint.json').exists():
                    raise RuntimeError('checkpoint not saved')
            status(case,phase='source-audits')
            with audit_slots:
                command_run(case,'source-audits',[py,str(f/'tools/prepare_native_speed_source.py'),
                    '--source',str(source),'--case-json',str(row_path),'--frozen-root',str(e)],timeout=7200)
            ready=json.loads((source/'source-ready.json').read_text())
            if not ready['valid']:raise RuntimeError('source not qualified')
            status(case,phase='qualified',qualified=True,source_ready=ready)
        except BaseException as error:
            status(case,phase='failed',qualified=False,error=repr(error))
        finally:
            if reserved:
                # Retain reservations for a producer with no final receipt.
                if not source.exists() or (source/'capture-receipt.json').exists():pool.release(source)
    status(rows[0]['case_id'],phase='queued')
    # One stable CPU slot per worker; each queue proceeds independently.
    partitions=[rows[i::jobs] for i in range(jobs)]
    def queue(slot):
        for row in partitions[slot]:worker(row,slot)
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        for done in as_completed([executor.submit(queue,i) for i in range(jobs)]):done.result()
    write_json(e/'completion.json',dict(expected_cases=len(rows),
        qualified_cases=sum(s.get('qualified',False) for s in states.values()),states=states,
        finished_unix=time.time(),policy=policy))
    return all(s.get('qualified',False) for s in states.values())


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment',type=Path,required=True)
    p.add_argument('--jobs',type=int,choices=range(1,5),default=4)
    p.add_argument('--source-gate',type=Path,help='qualified native raw-syscall smoke receipt; gates source collection, not checkpoint preparation')
    p.add_argument('--resume-unstarted',action='store_true',help='reuse verified checkpoint only when no source directory exists; runner revalidates payload and identity')
    p.add_argument('--cpu-offset',type=int,choices=(0,96),default=0)
    a=p.parse_args();raise SystemExit(0 if run(a.experiment,a.jobs,a.source_gate,a.resume_unstarted,a.cpu_offset) else 1)
