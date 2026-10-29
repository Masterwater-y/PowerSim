#!/usr/bin/env python3
"""Run one trace producer with global FST and free-disk limits; never score PMUs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import resource
import subprocess
import time


def fst_bytes(root):
    total = 0
    def scan_error(error):
        # Concurrent tests/collectors may retire temporary directories. Other
        # I/O failures must still fail closed rather than undercounting usage.
        if not isinstance(error, FileNotFoundError):raise error
    for directory, _, names in os.walk(root, followlinks=False, onerror=scan_error):
        for name in names:
            if not name.endswith('.fst'):continue
            path=Path(directory)/name
            try:
                if not path.is_symlink() and path.is_file():total+=path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def stop_reason(policy, *, total, case, case_cap, free):
    if total >= policy['total_fst_cap_bytes'] - policy['total_fst_stop_margin_bytes']:
        return 'global-fst-limit'
    if free < policy['minimum_free_disk_bytes']:
        return 'free-disk-reserve'
    if case >= case_cap:
        return 'case-fst-limit'
    return None


class CaptureSignal(Exception):
    pass


def no_core_dump():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    from roi_capture_policy import WarmupDeadline
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case-cap-bytes', type=int, required=True)
    parser.add_argument('--warmup-wall-seconds',type=int,default=0,help='Limit detailed warmup after checkpoint; zero disables this additional guard')
    parser.add_argument('--wall-seconds', type=int, default=21600)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    policy = json.loads((root / 'configs/trace-storage-policy.json').read_text())
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.case_cap_bytes <= 0 or args.wall_seconds <= 0:
        parser.error('positive limits and command required')
    output.relative_to(root)
    if output.exists():
        parser.error('output already exists; refusing to overwrite evidence')
    initial = fst_bytes(root)
    reserve = policy['total_fst_cap_bytes'] - policy['total_fst_stop_margin_bytes']
    if initial + args.case_cap_bytes > reserve:
        parser.error('insufficient global FST headroom for the case budget')
    if shutil.disk_usage(root).free < policy['minimum_free_disk_bytes'] + args.case_cap_bytes:
        parser.error('insufficient free disk for case budget and reserve')
    output.mkdir(parents=True)
    (output / 'capture-command.json').write_text(json.dumps(dict(
        argv=command, initial_fst_bytes=initial, policy=policy,
        case_cap_bytes=args.case_cap_bytes), indent=2) + '\n')
    warmup_guard=WarmupDeadline(args.warmup_wall_seconds)
    start = time.monotonic()
    reason, code, process = None, None, None
    def interrupted(signum, frame):
        raise CaptureSignal(signal.Signals(signum).name)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        with (output / 'capture.log').open('w') as log:
            process = subprocess.Popen(command, cwd=root, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       preexec_fn=no_core_dump)
            while process.poll() is None:
                case = fst_bytes(output)
                total = fst_bytes(root)
                free = shutil.disk_usage(root).free
                # Extra per-case 256MB margin covers producer flushing while it stops.
                reason = stop_reason(policy, total=total, case=case,
                                     case_cap=max(1, args.case_cap_bytes - min(256000000, args.case_cap_bytes//10)), free=free)
                if not reason and time.monotonic() - start >= args.wall_seconds:
                    reason = 'wall-time-limit'
                if not reason:
                    with (output/'capture.log').open('rb') as observed:
                        observed.seek(max(0, observed.seek(0, 2)-65536))
                        tail=observed.read()
                    reason=warmup_guard.observe(tail.decode('utf-8',errors='replace'),time.monotonic())
                    if b': fatal:' in tail or b': panic:' in tail:
                        reason = 'producer-fatal'
                status = dict(pid=process.pid, elapsed_seconds=time.monotonic()-start,
                              case_fst_bytes=case, total_fst_bytes=total, free_bytes=free,
                              guard_reason=reason,
                              detailed_warmup_wall_seconds=(None if warmup_guard.started is None else (warmup_guard.measurement_started or time.monotonic())-warmup_guard.started),
                              measurement_open=warmup_guard.measured)
                temporary = output / 'capture-status.json.tmp'
                temporary.write_text(json.dumps(status, indent=2) + '\n')
                temporary.replace(output / 'capture-status.json')
                if reason:
                    os.killpg(process.pid, signal.SIGTERM)
                    break
                time.sleep(1)
            try:
                code = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
    except CaptureSignal as error:
        reason='signal-'+str(error)
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                code=process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code=process.wait()
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if process is not None:
            # poll()/wait() reap only the leader. An exited launcher can leave
            # writers alive in its session, so receipts must not release that
            # group's capacity reservation until every descendant is stopped.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                if reason is None:
                    reason = 'orphan-producer-group'
                time.sleep(.05)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # A producer can finish between polls after printing both milestones.
        # Refresh final state before recording its receipt, including fast FS
        # smokes; elapsed warmup remains an observation at polling resolution.
        final_log = output / 'capture.log'
        if final_log.exists():
            final_reason = warmup_guard.observe(
                final_log.read_text(errors='replace'), time.monotonic())
            if reason is None:
                reason = final_reason
        receipt = dict(returncode=code, guard_reason=reason,
                       detailed_warmup_wall_seconds=(None if warmup_guard.started is None else (warmup_guard.measurement_started or time.monotonic())-warmup_guard.started),
                       measurement_open=warmup_guard.measured,
                       wall_seconds=time.monotonic()-start,
                       case_fst_bytes=fst_bytes(output), total_fst_bytes=fst_bytes(root),
                       free_bytes=shutil.disk_usage(root).free)
        if reason is None:
            reason=stop_reason(policy,total=receipt['total_fst_bytes'],
                               case=receipt['case_fst_bytes'],case_cap=args.case_cap_bytes,
                               free=receipt['free_bytes'])
            receipt['guard_reason']=reason
        (output / 'capture-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt), flush=True)
    if reason or code != 0:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
